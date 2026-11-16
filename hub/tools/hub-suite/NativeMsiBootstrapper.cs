// Per-user MSI launcher. Windows Installer owns every product-file transaction.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

internal static class NativeMsiBootstrapper
{
    internal sealed class PayloadFile { internal string Path; internal string Sha256; }
    internal sealed class Options { internal bool Quiet; internal bool Help; internal bool NoShortcuts; internal string InstallDirectory; internal string Log; internal string CacheDirectory; }
    internal sealed class Outcome { internal bool Success; internal bool Cancelled; internal bool RestartRequired; internal string Message; }
    private const int MaximumTailBytes = 65536;
    private const int MaximumTailCharacters = 16000;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateDirectoryW(string path, IntPtr security);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint sharing, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("msi.dll", CharSet = CharSet.Unicode)]
    private static extern int MsiQueryProductStateW(string product);
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("msi.dll", CharSet = CharSet.Unicode)]
    private static extern int MsiGetComponentPathW(string product, string component, StringBuilder path, ref uint length);

    internal static Options Parse(string[] args)
    {
        Options result = new Options();
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--quiet") result.Quiet = true;
            else if (args[i] == "--no-shortcuts") result.NoShortcuts = true;
            else if (args[i] == "--help" || args[i] == "-h") result.Help = true;
            else if ((args[i] == "--install-dir" || args[i] == "--log" || args[i] == "--cache-dir") && i + 1 < args.Length)
            {
                string name = args[i++], value = args[i];
                if (String.IsNullOrWhiteSpace(value) || value.IndexOf('"') >= 0 || value.IndexOf('\0') >= 0)
                    throw new ArgumentException("安装路径参数无效。");
                if (name == "--install-dir") result.InstallDirectory = System.IO.Path.GetFullPath(value);
                else if (name == "--cache-dir")
                {
                    if (!System.IO.Path.IsPathRooted(value)) throw new ArgumentException("安装源缓存必须使用独立的绝对目录。");
                    result.CacheDirectory = System.IO.Path.GetFullPath(value);
                }
                else result.Log = System.IO.Path.GetFullPath(value);
            }
            else throw new ArgumentException("不支持的参数。请使用 --help 查看安装选项。");
        }
        return result;
    }

    internal static string Quote(string argument)
    {
        StringBuilder value = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in argument)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') { value.Append('\\', slashes * 2 + 1); value.Append('"'); }
            else { value.Append('\\', slashes); value.Append(ch); }
            slashes = 0;
        }
        value.Append('\\', slashes * 2); value.Append('"');
        return value.ToString();
    }

    internal static string EngineArguments(string msi, string log, Options options)
    {
        // msiexec parses switches itself: quoting every argument produces 1639,
        // even though a managed child accepts that same command line.
        StringBuilder command = new StringBuilder("/i ").Append(Quote(msi));
        command.Append(options.Quiet ? " /qn" : " /qb");
        command.Append(" /norestart REBOOT=ReallySuppress MSIRESTARTMANAGERCONTROL=Disable /Lwe ");
        command.Append(Quote(log));
        if (options.InstallDirectory != null) command.Append(" INSTALLDIR=").Append(Quote(options.InstallDirectory));
        if (options.NoShortcuts) command.Append(" HUB_SHORTCUTS=0");
        return command.ToString();
    }

    internal static Outcome Classify(int code)
    {
        if (code == 0) return new Outcome { Success = true, Message = "安装完成。可从开始菜单打开 BYBC Hub " + MsiBootstrapBuild.Version + "。" };
        if (code == 3010) return new Outcome { Success = true, RestartRequired = true, Message = "安装已完成安排，等待重启后生效；当前尚未确认全部程序文件就绪。安装器不会自动重启。" };
        if (code == 1602) return new Outcome { Cancelled = true, Message = "安装已取消。" };
        if (code == 1618) return new Outcome { Message = "另一个 Windows 安装正在进行，请等待其结束后再试。" };
        return new Outcome { Message = "Windows Installer 未能完成安装（代码 " + code + "）。请查看安装日志。" };
    }

    internal static void ValidateWindowsBuild(WindowsRuntimePrerequisites.SystemFact facts)
    {
        if (facts.WindowsMajor == 10 && facts.WindowsBuild < 17134)
            throw new InvalidOperationException("此安装包需要 Windows 10 1803（内部版本 17134）或更新版本，也支持 Windows 11 x64。请先更新 Windows。");
    }

    internal static void RejectReparse(string path)
    {
        string full = System.IO.Path.GetFullPath(path);
        FileAttributes attributes;
        try { attributes = File.GetAttributes(full); }
        catch (FileNotFoundException) { attributes = 0; }
        catch (DirectoryNotFoundException) { attributes = 0; }
        if ((attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("安装路径不能包含目录联接或符号链接。");
        for (DirectoryInfo item = new DirectoryInfo(System.IO.Path.GetDirectoryName(full)); item != null; item = item.Parent)
            if (item.Exists && (item.Attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("安装路径不能包含目录联接或符号链接。");
    }

    private static string Hash(Stream stream)
    {
        using (SHA256 sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    internal static void ExtractMsi(Stream input, string destination, long expectedLength, string expectedHash)
    {
        if (expectedLength <= 0 || expectedLength > 2L * 1024 * 1024 * 1024) throw new InvalidDataException("安装包长度无效。");
        RejectReparse(destination);
        using (FileStream output = new FileStream(destination, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None))
        {
            byte[] buffer = new byte[1024 * 1024];
            long total = 0;
            int count;
            while ((count = input.Read(buffer, 0, buffer.Length)) != 0)
            {
                total += count;
                if (total > expectedLength) throw new InvalidDataException("内嵌安装包超出声明长度。");
                output.Write(buffer, 0, count);
            }
            output.Flush(true);
            if (total != expectedLength) throw new InvalidDataException("内嵌安装包不完整。");
            output.Position = 0;
            if (Hash(output) != expectedHash) throw new InvalidDataException("内嵌安装包校验失败，未启动安装。");
        }
    }

    internal static string CachedMsiPath(Options options)
    {
        string root = options.CacheDirectory ?? Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string path = System.IO.Path.GetFullPath(System.IO.Path.Combine(root, MsiBootstrapBuild.CacheRelativePath.Replace('/', '\\')));
        if (!path.StartsWith(System.IO.Path.GetFullPath(root).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("安装源缓存路径越界。");
        RejectReparse(path);
        return path;
    }

    private static void VerifyCachedMsi(string path, long length, string checksum)
    {
        RejectReparse(path);
        using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            if (file.Length != length || Hash(file) != checksum)
                throw new InvalidDataException("本产品安装源缓存校验失败；已保留原文件，未开始安装。");
    }

    internal static void CacheMsi(Stream input, string destination, long length, string checksum)
    {
        RejectReparse(destination);
        string directory = System.IO.Path.GetDirectoryName(destination);
        Directory.CreateDirectory(directory);
        RejectReparse(destination);
        using (SafeFileHandle guard = CreateFileW(directory, 0x80, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero))
        {
            if (guard.IsInvalid) throw new IOException("无法保护安装源缓存目录。");
            if (File.Exists(destination)) { VerifyCachedMsi(destination, length, checksum); return; }
            string pending = System.IO.Path.Combine(directory, ".pending-" + Guid.NewGuid().ToString("N") + ".msi");
            try
            {
                ExtractMsi(input, pending, length, checksum);
                try { File.Move(pending, destination); }
                catch (IOException)
                {
                    if (!File.Exists(destination)) throw;
                    // Another launcher may have published the exact same MSI.
                    // It can be reused only after a complete independent check.
                    VerifyCachedMsi(destination, length, checksum);
                }
            }
            finally
            {
                try { DeleteOwnFile(pending, directory); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }

    internal static int RunEngine(string executable, string arguments, bool quiet)
    {
        ProcessStartInfo start = new ProcessStartInfo(executable, arguments);
        start.UseShellExecute = false;
        start.CreateNoWindow = quiet;
        using (Process child = Process.Start(start))
        {
            // Never kill an in-progress Windows Installer transaction or infer
            // completion from elapsed time. Basic UI retains its Cancel button.
            child.WaitForExit();
            return child.ExitCode;
        }
    }

    internal static bool SamePath(string left, string right)
    {
        return String.Equals(System.IO.Path.GetFullPath(left).TrimEnd('\\', '/'), System.IO.Path.GetFullPath(right).TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase);
    }

    internal static void VerifyPayload(string root, string registeredProof, string expectedRoot)
    {
        VerifyInstallationPath(root, registeredProof, expectedRoot);
        foreach (PayloadFile item in MsiBootstrapBuild.VerifiedFiles)
        {
            string target = System.IO.Path.GetFullPath(System.IO.Path.Combine(root, item.Path.Replace('/', '\\')));
            if (!target.StartsWith(System.IO.Path.GetFullPath(root).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("安装文件标记包含越界路径。");
            RejectReparse(target);
            using (FileStream file = new FileStream(target, FileMode.Open, FileAccess.Read, FileShare.Read))
                if (Hash(file) != item.Sha256) throw new InvalidDataException("安装后关键文件校验未通过，未确认安装完成。");
        }
        // The marker hash is frozen into this launcher. Verify every owned file,
        // not just the executables: an old installed state cannot mask a failed
        // repair that left a missing/private runtime library behind.
        using (FileStream marker = new FileStream(registeredProof, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            if (marker.Length > 16 * 1024 * 1024) throw new InvalidDataException("安装清单超出限制。");
            string markerHash = null;
            foreach (PayloadFile item in MsiBootstrapBuild.VerifiedFiles) if (item.Path == MsiBootstrapBuild.ProofPath) markerHash = item.Sha256;
            if (markerHash == null || Hash(marker) != markerHash) throw new InvalidDataException("安装清单在验证期间发生变化。");
            marker.Position = 0;
            JavaScriptSerializer parser = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024, RecursionLimit = 12 };
            Dictionary<string, object> document;
            using (StreamReader reader = new StreamReader(marker, new UTF8Encoding(false, true)))
                document = parser.DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
            object schema, product, version, rows;
            if (document == null || !document.TryGetValue("schema_version", out schema) || !(schema is int) || (int)schema != 1 ||
                !document.TryGetValue("product_code", out product) || !String.Equals(product as string, MsiBootstrapBuild.ProductCode, StringComparison.OrdinalIgnoreCase) ||
                !document.TryGetValue("product_version", out version) || version as string != MsiBootstrapBuild.Version ||
                !document.TryGetValue("files", out rows) || !(rows is object[]))
                throw new InvalidDataException("安装清单身份不匹配。");
            object[] files = (object[])rows;
            if (files.Length == 0 || files.Length > 100000) throw new InvalidDataException("安装清单文件数无效。");
            HashSet<string> seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            long totalBytes = 0;
            foreach (object row in files)
            {
                Dictionary<string, object> item = row as Dictionary<string, object>;
                object relativeValue, sizeValue, checksumValue;
                if (item == null || !item.TryGetValue("path", out relativeValue) || !item.TryGetValue("bytes", out sizeValue) || !item.TryGetValue("sha256", out checksumValue))
                    throw new InvalidDataException("安装清单项目无效。");
                string relative = relativeValue as string, checksum = checksumValue as string;
                if (String.IsNullOrEmpty(relative) || relative.Length > 1024 || relative.IndexOf('\\') >= 0 || relative.IndexOf(':') >= 0 ||
                    System.IO.Path.IsPathRooted(relative) || !seen.Add(relative) || checksum == null || checksum.Length != 64 ||
                    String.Equals(relative, MsiBootstrapBuild.ProofPath, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("安装清单包含无效或重复路径。");
                foreach (char ch in checksum) if (!(ch >= '0' && ch <= '9') && !(ch >= 'a' && ch <= 'f')) throw new InvalidDataException("安装清单校验值无效。");
                foreach (string part in relative.Split('/'))
                    if (part == "" || part == "." || part == ".." || part.EndsWith(".") || part.EndsWith(" ") || part.IndexOfAny(System.IO.Path.GetInvalidFileNameChars()) >= 0)
                        throw new InvalidDataException("安装清单包含无效路径段。");
                if (!(sizeValue is int) && !(sizeValue is long)) throw new InvalidDataException("安装文件长度无效。");
                long size = Convert.ToInt64(sizeValue);
                totalBytes = checked(totalBytes + size);
                if (size < 0 || totalBytes > 16L * 1024 * 1024 * 1024) throw new InvalidDataException("安装文件长度超出限制。");
                string filePath = System.IO.Path.GetFullPath(System.IO.Path.Combine(root, relative.Replace('/', '\\')));
                if (!filePath.StartsWith(System.IO.Path.GetFullPath(root).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("安装清单包含越界路径。");
                RejectReparse(filePath);
                using (FileStream file = new FileStream(filePath, FileMode.Open, FileAccess.Read, FileShare.Read))
                    if (file.Length != size || Hash(file) != checksum) throw new InvalidDataException("安装后程序文件校验未通过：" + relative);
            }
            foreach (PayloadFile required in MsiBootstrapBuild.VerifiedFiles)
                if (required.Path != MsiBootstrapBuild.ProofPath && !seen.Contains(required.Path))
                    throw new InvalidDataException("安装清单缺少程序入口。");
        }
    }

    private static void VerifyInstallationPath(string root, string registeredProof, string expectedRoot)
    {
        if (!System.IO.Path.IsPathRooted(registeredProof) || !SamePath(root, expectedRoot) ||
            !SamePath(registeredProof, System.IO.Path.Combine(root, MsiBootstrapBuild.ProofPath)))
            throw new InvalidDataException("Windows Installer 登记的安装位置与本次目标不一致，未确认安装完成。");
    }

    private static void VerifyRegisteredProduct(Options options, bool verifyFiles)
    {
        if (MsiQueryProductStateW(MsiBootstrapBuild.ProductCode) != 5)
            throw new InvalidDataException("Windows Installer 未确认本产品已安装，未报告安装成功。");
        StringBuilder component = new StringBuilder(32768);
        uint length = (uint)component.Capacity;
        if (MsiGetComponentPathW(MsiBootstrapBuild.ProductCode, MsiBootstrapBuild.ProofComponentCode, component, ref length) != 3)
            throw new InvalidDataException("Windows Installer 未确认安装标记已落地。");
        string proof = component.ToString();
        string relative = MsiBootstrapBuild.ProofPath.Replace('/', '\\');
        if (!proof.EndsWith("\\" + relative, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Windows Installer 登记的安装标记路径不正确。");
        string root = proof.Substring(0, proof.Length - relative.Length - 1);
        // LocalAppDataFolder is a Windows known folder. Explicit INSTALLDIR is
        // used by isolated QA; no environment override alters the MSI scope.
        string expected = options.InstallDirectory ?? System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), MsiBootstrapBuild.DefaultDirectory.Replace('/', '\\'));
        VerifyInstallationPath(root, proof, expected);
        if (verifyFiles) VerifyPayload(root, proof, expected);
    }

    internal static string LogTail(string path)
    {
        if (!File.Exists(path)) return "";
        RejectReparse(path);
        using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            int first = stream.ReadByte(), second = stream.ReadByte();
            bool unicode = first == 255 && second == 254;
            long start = Math.Max(0, stream.Length - MaximumTailBytes);
            if (unicode && start % 2 != 0) start++;
            stream.Position = start;
            byte[] buffer = new byte[MaximumTailBytes];
            int count = 0, read;
            while (count < buffer.Length && (read = stream.Read(buffer, count, buffer.Length - count)) > 0) count += read;
            string text = (unicode ? Encoding.Unicode : Encoding.UTF8).GetString(buffer, 0, count).Replace("\0", "");
            if (text.Length > MaximumTailCharacters) text = text.Substring(text.Length - MaximumTailCharacters);
            return text;
        }
    }

    private static void DeleteOwnFile(string path, string root)
    {
        if (!SamePath(System.IO.Path.GetDirectoryName(path), root)) throw new IOException("拒绝清理非本次临时文件。");
        RejectReparse(path);
        if (File.Exists(path)) File.Delete(path);
    }

    [STAThread]
    private static int Main(string[] args)
    {
        Console.SetError(new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false)) { AutoFlush = true });
        Options options = null;
        string ownedRoot = null, package = null, rawLog = null, savedLog = null;
        FileStream logReservation = null;
        SafeFileHandle directoryGuard = null;
        int result = 1;
        string message = "安装未完成。";
        bool cancelled = false;
        try
        {
            options = Parse(args);
            if (options.Help)
            {
                string help = "BYBC Hub " + MsiBootstrapBuild.Version + "\n--quiet 静默安装\n--install-dir <目录> 指定独立安装位置\n--log <新文件> 保存安装日志\n--no-shortcuts 不创建开始菜单入口\n--cache-dir <绝对目录> 指定安装源缓存根（需长期保留，不能使用下载/临时清理目录）\n安装仅用于当前用户，不自动启动程序、关闭旧应用或重启电脑。";
                Console.Error.WriteLine(help);
                if (!options.Quiet) MessageBox.Show(help, "BYBC Hub 安装", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return 0;
            }
            WindowsRuntimePrerequisites.SystemFact facts = WindowsRuntimePrerequisites.Probe(MsiBootstrapBuild.RuntimeRequirements);
            ValidateWindowsBuild(facts);
            string failure = WindowsRuntimePrerequisites.Validate(facts, MsiBootstrapBuild.RuntimeRequirements);
            if (failure != null) throw new InvalidOperationException(failure);
            string temp = System.IO.Path.GetFullPath(System.IO.Path.GetTempPath());
            RejectReparse(temp);
            savedLog = options.Log ?? System.IO.Path.Combine(temp, "CodexHub-Install-" + Guid.NewGuid().ToString("N") + ".log");
            RejectReparse(savedLog);
            logReservation = new FileStream(savedLog, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
            ownedRoot = System.IO.Path.Combine(temp, "codex-hub-msi-" + Guid.NewGuid().ToString("N"));
            if (!CreateDirectoryW(ownedRoot, IntPtr.Zero)) { ownedRoot = null; throw new IOException("无法创建本次安装临时目录。"); }
            RejectReparse(ownedRoot);
            directoryGuard = CreateFileW(ownedRoot, 0x80, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
            if (directoryGuard.IsInvalid) throw new IOException("无法保护本次安装临时目录。");
            package = CachedMsiPath(options);
            rawLog = System.IO.Path.Combine(ownedRoot, "msi.log");
            using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("CodexHub.Package.msi"))
            {
                if (resource == null) throw new InvalidDataException("安装包资源缺失。");
                CacheMsi(resource, package, MsiBootstrapBuild.MsiBytes, MsiBootstrapBuild.MsiHash);
            }
            string engine = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "msiexec.exe");
            RejectReparse(engine);
            if (!File.Exists(engine)) throw new FileNotFoundException("找不到 Windows Installer，请修复 Windows 系统组件。");
            int code;
            // Keep the verified MSI immutable while Windows Installer reads it.
            using (FileStream packageGuard = new FileStream(package, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                if (packageGuard.Length != MsiBootstrapBuild.MsiBytes || Hash(packageGuard) != MsiBootstrapBuild.MsiHash)
                    throw new InvalidDataException("安装源缓存发生变化，未启动安装。");
                code = RunEngine(engine, EngineArguments(package, rawLog, options), options.Quiet);
            }
            Outcome outcome = Classify(code);
            message = outcome.Message;
            cancelled = outcome.Cancelled;
            if (outcome.Success) VerifyRegisteredProduct(options, !outcome.RestartRequired);
            result = code;
        }
        catch (Exception error) { result = 1; message = "未确认安装完成：" + error.Message; if (message.Length > 2000) message = message.Substring(0, 2000); }
        finally
        {
            if (logReservation != null)
            {
                try
                {
                    string diagnostic = "BYBC Hub " + MsiBootstrapBuild.Version + "\n" + message + "\nExitCode=" + result + "\nMSI SHA256=" + MsiBootstrapBuild.MsiHash + "\nInstallerSource=" + package + "\n";
                    if (rawLog != null) diagnostic += "\nWindows Installer 日志末尾（最多 16000 字符）：\n" + LogTail(rawLog);
                    byte[] bytes = new UTF8Encoding(false).GetBytes(diagnostic);
                    logReservation.Write(bytes, 0, bytes.Length); logReservation.Flush(true);
                }
                catch (IOException) { message += "\n日志未能完整保存。"; }
                finally { logReservation.Dispose(); }
            }
            if (options == null || !options.Help)
            {
                string notice = message + (savedLog == null ? "" : "\n安装日志：" + savedLog);
                Console.Error.WriteLine(notice);
                if (options == null ? Array.IndexOf(args, "--quiet") < 0 : !options.Quiet)
                    MessageBox.Show(notice, "BYBC Hub 安装", MessageBoxButtons.OK, cancelled || result == 0 || result == 3010 ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
            }
            // No recursion and no product/user paths. Unexpected files or locks
            // leave this GUID directory in place instead of broadening cleanup.
            try
            {
                if (ownedRoot != null)
                {
                    if (rawLog != null) DeleteOwnFile(rawLog, ownedRoot);
                }
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            finally { if (directoryGuard != null) directoryGuard.Dispose(); }
            if (ownedRoot != null) { try { Directory.Delete(ownedRoot, false); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
        }
        return result;
    }
}
