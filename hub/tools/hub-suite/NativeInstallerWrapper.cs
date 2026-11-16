// Windows .NET Framework launcher for an audited onedir installer runtime.
// Application installation and uninstall behavior remain in installer.py.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

internal static class NativeInstallerWrapper
{
    private const string Prefix = "codex-hub-setup-";
    private const string ZipResource = "CodexHub.Installer.zip";
    private const string ManifestResource = "CodexHub.Installer.manifest";
    private sealed class Item { internal string Name; internal long Length; internal string Hash; }
    private sealed class CompletionContract { internal string Operation; internal string Target; internal bool AllowCancel; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateDirectoryW(string path, IntPtr securityAttributes);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint sharing, IntPtr security, uint disposition, uint flags, IntPtr template);

    private static SafeFileHandle GuardDirectory(string path)
    {
        // No write/delete sharing: a directory being traversed cannot be
        // renamed or converted to a junction by another handle mid-operation.
        SafeFileHandle handle = CreateFileW(path, 0x80, 1, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
        if (handle.IsInvalid) { handle.Dispose(); throw new IOException("Cannot guard installer directory.", new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error())); }
        try { RejectReparseChain(path); return handle; }
        catch { handle.Dispose(); throw; }
    }

    private static string Hash(Stream stream)
    {
        using (SHA256 sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    private static Stream Resource(string name, string expectedHash)
    {
        Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name);
        if (stream == null) throw new InvalidDataException("Missing installer resource.");
        if (Hash(stream) != expectedHash) { stream.Dispose(); throw new InvalidDataException("Installer resource integrity check failed."); }
        stream.Position = 0;
        return stream;
    }

    private static void ValidateName(string name)
    {
        if (String.IsNullOrEmpty(name) || name.IndexOf('\\') >= 0 || name.IndexOf(':') >= 0 || Path.IsPathRooted(name))
            throw new InvalidDataException("Invalid installer path.");
        foreach (string part in name.Split('/'))
        {
            if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(".", StringComparison.Ordinal) || part.EndsWith(" ", StringComparison.Ordinal) || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                throw new InvalidDataException("Invalid installer path component.");
            string stem = part.Split('.')[0].ToUpperInvariant();
            if (stem == "CON" || stem == "PRN" || stem == "AUX" || stem == "NUL" ||
                (stem.Length == 4 && (stem.StartsWith("COM", StringComparison.Ordinal) || stem.StartsWith("LPT", StringComparison.Ordinal)) && stem[3] >= '1' && stem[3] <= '9'))
                throw new InvalidDataException("Reserved installer path.");
        }
    }

    private static string Inside(string root, string name)
    {
        ValidateName(name);
        string target = Path.GetFullPath(Path.Combine(root, name.Replace('/', Path.DirectorySeparatorChar)));
        if (!target.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Installer path leaves extraction directory.");
        return target;
    }

    private static void RejectReparseChain(string path)
    {
        for (DirectoryInfo current = new DirectoryInfo(Path.GetFullPath(path)); current != null; current = current.Parent)
        {
            if (current.Exists && (current.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Installer directory may not contain a reparse point.");
        }
    }

    private static Dictionary<string, Item> Manifest()
    {
        Dictionary<string, Item> items = new Dictionary<string, Item>(StringComparer.OrdinalIgnoreCase);
        long totalBytes = 0;
        using (Stream source = Resource(ManifestResource, WrapperBuild.ManifestHash))
        using (StreamReader reader = new StreamReader(source, new UTF8Encoding(false, true)))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                string[] fields = line.Split('\t');
                long length;
                if (fields.Length != 3 || fields[0].Length != 64 || !Int64.TryParse(fields[1], out length) || length < 0)
                    throw new InvalidDataException("Invalid installer manifest.");
                foreach (char ch in fields[0]) if (!(ch >= '0' && ch <= '9') && !(ch >= 'a' && ch <= 'f')) throw new InvalidDataException("Invalid installer digest.");
                string name = new UTF8Encoding(false, true).GetString(Convert.FromBase64String(fields[2]));
                ValidateName(name);
                if (items.ContainsKey(name)) throw new InvalidDataException("Duplicate installer path.");
                totalBytes = checked(totalBytes + length);
                if (items.Count >= WrapperBuild.FileCount || totalBytes > WrapperBuild.TotalBytes)
                    throw new InvalidDataException("Installer manifest exceeds its frozen bounds.");
                items.Add(name, new Item { Name = name, Length = length, Hash = fields[0] });
            }
        }
        if (items.Count == 0 || items.Count != WrapperBuild.FileCount || totalBytes != WrapperBuild.TotalBytes || !items.ContainsKey(WrapperBuild.Entry)) throw new InvalidDataException("Installer manifest differs from frozen bounds.");
        return items;
    }

    private static void VerifyArchive(Dictionary<string, Item> items)
    {
        using (Stream source = Resource(ZipResource, WrapperBuild.ZipHash))
        using (ZipArchive zip = new ZipArchive(source, ZipArchiveMode.Read))
        {
            HashSet<string> seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (zip.Entries.Count != items.Count) throw new InvalidDataException("Installer member count differs from manifest.");
            byte[] buffer = new byte[65536];
            // Validate names, types, lengths and decompressed hashes before any
            // native extractor receives the archive. All bounds are frozen.
            foreach (ZipArchiveEntry entry in zip.Entries)
            {
                ValidateName(entry.FullName);
                Item item;
                if (!seen.Add(entry.FullName) || !items.TryGetValue(entry.FullName, out item) || entry.FullName != item.Name || entry.Length != item.Length)
                    throw new InvalidDataException("Unexpected installer member.");
                int unixType = (entry.ExternalAttributes >> 16) & 0xF000;
                if ((unixType != 0 && unixType != 0x8000) || (entry.ExternalAttributes & (int)FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException("Installer archive contains a link or special file.");
                using (Stream incoming = entry.Open())
                using (SHA256 sha = SHA256.Create())
                {
                    long total = 0;
                    int count;
                    while ((count = incoming.Read(buffer, 0, buffer.Length)) != 0)
                    {
                        total = checked(total + count);
                        if (total > item.Length) throw new InvalidDataException("Installer member exceeds its recorded size.");
                        sha.TransformBlock(buffer, 0, count, null, 0);
                    }
                    sha.TransformFinalBlock(new byte[0], 0, 0);
                    string digest = BitConverter.ToString(sha.Hash).Replace("-", "").ToLowerInvariant();
                    if (total != item.Length || digest != item.Hash) throw new InvalidDataException("Installer file integrity check failed.");
                }
            }
        }
    }

    private static void VerifyExtracted(string root, Dictionary<string, Item> items)
    {
        HashSet<string> seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        Stack<string> pending = new Stack<string>();
        pending.Push(root);
        while (pending.Count != 0)
        {
            string directory = pending.Pop();
            using (SafeFileHandle guard = GuardDirectory(directory))
            {
                foreach (string path in Directory.GetFileSystemEntries(directory))
                {
                    FileAttributes attributes = File.GetAttributes(path);
                    if ((attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Extracted installer contains a reparse point.");
                    string relative = path.Substring(root.Length + 1).Replace(Path.DirectorySeparatorChar, '/');
                    if (Inside(root, relative) != path) throw new InvalidDataException("Extracted installer path is not canonical.");
                    if ((attributes & FileAttributes.Directory) != 0) { pending.Push(path); continue; }
                    Item item;
                    if (!seen.Add(relative) || !items.TryGetValue(relative, out item) || relative != item.Name)
                        throw new InvalidDataException("Unexpected extracted installer file.");
                    using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                        if (file.Length != item.Length || Hash(file) != item.Hash) throw new InvalidDataException("Extracted installer file failed verification.");
                }
            }
        }
        if (seen.Count != items.Count) throw new InvalidDataException("Extracted installer is incomplete.");
    }

    private static void Extract(string root, Dictionary<string, Item> items)
    {
        VerifyArchive(items);
        string tar = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "tar.exe");
        if (!File.Exists(tar)) throw new FileNotFoundException("Windows tar.exe is required by this installer (Windows 10 1803 or later).", tar);
        string archive = Path.Combine(root, ".codex-installer-" + Guid.NewGuid().ToString("N") + ".zip");
        using (Stream resource = Resource(ZipResource, WrapperBuild.ZipHash))
        using (FileStream file = new FileStream(archive, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            resource.CopyTo(file);
        ProcessStartInfo start = new ProcessStartInfo(tar, "-xf " + Quote(archive) + " -C " + Quote(root));
        start.UseShellExecute = false;
        start.CreateNoWindow = true; // Extraction helper is never an interactive window.
        start.RedirectStandardError = true;
        StringBuilder errorText = new StringBuilder();
        using (Process process = new Process())
        {
            process.StartInfo = start;
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) {
                if (e.Data != null) lock (errorText) { if (errorText.Length < 32768) errorText.AppendLine(e.Data.Substring(0, Math.Min(e.Data.Length, 32768 - errorText.Length))); }
            };
            if (!process.Start()) throw new IOException("Cannot start Windows archive extraction.");
            process.BeginErrorReadLine();
            if (!process.WaitForExit(600000)) { process.Kill(); process.WaitForExit(); throw new IOException("Windows archive extraction exceeded its time limit."); }
            process.WaitForExit();
            if (process.ExitCode != 0) throw new IOException("Windows archive extraction failed: " + errorText.ToString());
        }
        File.Delete(archive);
        VerifyExtracted(root, items);
    }

    // The inverse of CommandLineToArgvW/MS CRT quoting, including empty arguments.
    internal static string Quote(string argument)
    {
        StringBuilder quoted = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in argument)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') { quoted.Append('\\', slashes * 2 + 1); quoted.Append('"'); }
            else { quoted.Append('\\', slashes); quoted.Append(ch); }
            slashes = 0;
        }
        quoted.Append('\\', slashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }

    private static string Option(string[] args, string name)
    {
        string value = null;
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == name && i + 1 < args.Length) value = args[++i];
            else if (args[i].StartsWith(name + "=", StringComparison.Ordinal)) value = args[i].Substring(name.Length + 1);
        }
        return value;
    }

    private static CompletionContract Contract(string[] args, bool quiet)
    {
        string scan = Option(args, "--scan-only"), remove = Option(args, "--remove"), uninstall = Option(args, "--uninstall");
        if (Array.IndexOf(args, "--help") >= 0 || Array.IndexOf(args, "-h") >= 0)
            return new CompletionContract { Operation = "help", Target = "" };
        if (scan != null) return new CompletionContract { Operation = "scan", Target = Path.GetFullPath(scan) };
        if (uninstall != null) return new CompletionContract { Operation = "uninstall_launch", Target = Path.GetFullPath(uninstall), AllowCancel = !quiet };
        if (remove != null) return new CompletionContract { Operation = "remove", Target = Path.GetFullPath(remove) };
        string target = quiet ? Path.GetFullPath(Option(args, "--install-dir") ??
            Path.Combine(Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "CodexHub")) : "";
        // The GUI lets the user choose a different destination. Its result is
        // bound to the target marker and the nonce instead of the initial field.
        return new CompletionContract { Operation = "install", Target = target, AllowCancel = !quiet };
    }

    private static Dictionary<string, object> ReadObject(string path, int limit)
    {
        RejectReparseChain(Path.GetDirectoryName(path));
        FileInfo info = new FileInfo(path);
        if (!info.Exists || (info.Attributes & FileAttributes.ReparsePoint) != 0 || info.Length <= 0 || info.Length > limit)
            throw new InvalidDataException("安装结果记录缺失或无效，不能确认操作已完成。");
        using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
        using (StreamReader reader = new StreamReader(file, new UTF8Encoding(false, true)))
        {
            if (file.Length > limit) throw new InvalidDataException("安装结果记录超出限制。");
            JavaScriptSerializer serializer = new JavaScriptSerializer { MaxJsonLength = limit, RecursionLimit = 12 };
            Dictionary<string, object> value = serializer.DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
            if (value == null) throw new InvalidDataException("安装结果记录不是有效对象。");
            return value;
        }
    }

    private static string Text(Dictionary<string, object> value, string key)
    {
        object field;
        if (!value.TryGetValue(key, out field) || !(field is string) || ((string)field).Length > 4096)
            throw new InvalidDataException("安装结果记录缺少有效字段：" + key);
        return (string)field;
    }

    private static bool SamePath(string left, string right)
    {
        return String.Equals(Path.GetFullPath(left).TrimEnd(Path.DirectorySeparatorChar),
            Path.GetFullPath(right).TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase);
    }

    private static string VerifyCompletion(string receiptPath, string nonce, CompletionContract contract)
    {
        Dictionary<string, object> receipt = ReadObject(receiptPath, 65536);
        object schema;
        if (!receipt.TryGetValue("schema", out schema) || !(schema is int) || (int)schema != 1 ||
            Text(receipt, "nonce") != nonce || Text(receipt, "operation") != contract.Operation)
            throw new InvalidDataException("安装结果记录不属于本次操作，不能确认操作已完成。");
        string target = Text(receipt, "target"), status = Text(receipt, "status"), version = Text(receipt, "version");
        if (version.Length == 0) throw new InvalidDataException("安装结果没有版本信息。");
        if (contract.Operation == "help" && status == "help") return null;
        if (!Path.IsPathRooted(target) || !String.Equals(target.TrimEnd(Path.DirectorySeparatorChar), Path.GetFullPath(target).TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase) ||
            (contract.Target != "" && !SamePath(target, contract.Target)))
            throw new InvalidDataException("安装结果中的目标目录与本次操作不一致。");
        if (status == "cancelled" && contract.AllowCancel) return null;
        if (contract.Operation == "install")
        {
            if (status != "installed" && status != "installed_cleanup_pending")
                throw new InvalidDataException("安装未完成，已保留现有程序和恢复记录。");
            Dictionary<string, object> marker = ReadObject(Path.Combine(target, ".hub-suite-install.json"), 8 * 1024 * 1024);
            object committed;
            if (!SamePath(Text(marker, "root"), target) || Text(marker, "operation_nonce") != nonce || Text(marker, "status") != status ||
                Text(marker, "version") != version || !marker.TryGetValue("installation_committed", out committed) || !(committed is bool) || !(bool)committed)
                throw new InvalidDataException("安装完成记录与目标目录不一致，不能确认操作已完成。");
            if (status == "installed_cleanup_pending")
                return "新版程序已安装并校验完成，但旧程序清理未完成。残余旧文件已保留，不影响新版使用；详情见升级记录。";
            return null;
        }
        if (contract.Operation == "remove" && status == "removed") return null;
        if (contract.Operation == "uninstall_launch" && status == "launched")
            return "卸载程序已启动，尚未确认文件移除完成。用户配置和个人文件将保留。";
        if (contract.Operation == "scan" && status == "scanned") return null;
        throw new InvalidDataException("操作尚未完成，已保留剩余文件；请检查安装或卸载记录后重试。");
    }

    private static void DeleteOwned(string directory, string root)
    {
        if (directory != root && !directory.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new IOException("Cleanup left extraction root.");
        using (SafeFileHandle directoryGuard = GuardDirectory(directory))
        {
            foreach (string entry in Directory.GetFileSystemEntries(directory))
            {
                FileAttributes attributes = File.GetAttributes(entry);
                if ((attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("Cleanup will not follow a reparse point.");
                if ((attributes & FileAttributes.Directory) != 0) DeleteOwned(entry, root);
                else File.Delete(entry);
            }
        }
        // Nonrecursive removal never follows a substituted directory link.
        Directory.Delete(directory, false);
    }

    [STAThread]
    private static int Main(string[] args)
    {
        // Keep redirected quiet diagnostics readable without changing the
        // user's console code page or any system setting.
        Console.SetError(new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false)) { AutoFlush = true });
        bool quiet = Array.IndexOf(args, "--quiet") >= 0;
        string ownedRoot = null;
        int result = 1;
        try
        {
            string prerequisiteFailure = WindowsRuntimePrerequisites.Validate(
                WindowsRuntimePrerequisites.Probe(WrapperBuild.RuntimeRequirements), WrapperBuild.RuntimeRequirements);
            if (prerequisiteFailure != null) throw new InvalidOperationException(prerequisiteFailure);
            Dictionary<string, Item> items = Manifest();
            string temporary = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar);
            if (temporary.Length == 2 && temporary[1] == ':') temporary += Path.DirectorySeparatorChar;
            RejectReparseChain(temporary);
            if (!Directory.Exists(temporary)) throw new DirectoryNotFoundException("Temporary directory does not exist.");
            string candidate = Path.Combine(temporary, Prefix + Guid.NewGuid().ToString("N"));
            if (!CreateDirectoryW(candidate, IntPtr.Zero)) throw new IOException("Cannot create a private installer directory.", new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()));
            ownedRoot = Path.GetFullPath(candidate);
            RejectReparseChain(ownedRoot);
            using (SafeFileHandle rootGuard = GuardDirectory(ownedRoot))
            {
                Extract(ownedRoot, items);
                CompletionContract contract = Contract(args, quiet);
                string nonce = Guid.NewGuid().ToString("N");
                string receipt = Path.Combine(ownedRoot, ".completion-" + nonce + ".json");
                string entry = Inside(ownedRoot, WrapperBuild.Entry);
                RejectReparseChain(Path.GetDirectoryName(entry));
                string outer = Process.GetCurrentProcess().MainModule.FileName;
                StringBuilder arguments = new StringBuilder();
                foreach (string argument in args) { if (arguments.Length != 0) arguments.Append(' '); arguments.Append(Quote(argument)); }
                ProcessStartInfo start = new ProcessStartInfo(entry, arguments.ToString());
                start.UseShellExecute = false;
                start.CreateNoWindow = quiet;
                start.WorkingDirectory = Environment.CurrentDirectory;
                start.EnvironmentVariables["CODEX_HUB_INSTALLER_OUTER_EXE"] = outer;
                start.EnvironmentVariables["PYINSTALLER_RESET_ENVIRONMENT"] = "1";
                start.EnvironmentVariables["CODEX_HUB_INSTALLER_RECEIPT_PATH"] = receipt;
                start.EnvironmentVariables["CODEX_HUB_INSTALLER_NONCE"] = nonce;
                start.EnvironmentVariables["CODEX_HUB_INSTALLER_OPERATION"] = contract.Operation;
                start.EnvironmentVariables["CODEX_HUB_INSTALLER_TARGET"] = contract.Target;
                int childExit;
                using (Process child = Process.Start(start)) { child.WaitForExit(); childExit = child.ExitCode; }
                // A process can disappear with exit code zero before returning
                // from Python. Only this invocation's durable receipt is success.
                if (childExit == 0)
                {
                    string notice = VerifyCompletion(receipt, nonce, contract);
                    if (notice != null)
                    {
                        Console.Error.WriteLine(notice);
                        if (!quiet) MessageBox.Show(notice, "Codex Hub 安装", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    }
                    result = 0;
                }
                else result = childExit;
            }
        }
        catch (Exception error)
        {
            result = 1;
            string message = "安装程序未能确认完成：" + error.Message;
            Console.Error.WriteLine(message);
            if (!quiet) MessageBox.Show(message, "Codex Hub 安装", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            if (ownedRoot != null)
            {
                for (int attempt = 0; attempt < 3; attempt++)
                {
                    try { DeleteOwned(ownedRoot, ownedRoot); break; }
                    catch (IOException) { if (attempt < 2) Thread.Sleep(100); }
                    catch (UnauthorizedAccessException) { break; }
                }
            }
        }
        return result;
    }
}
