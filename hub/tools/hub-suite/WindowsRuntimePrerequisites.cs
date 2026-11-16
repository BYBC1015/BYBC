// Fixed-system-path, read-only prerequisite checks. No DLL loading.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class WindowsRuntimePrerequisites
{
    internal const string OfficialPage = "https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist";
    internal const string OfficialX64Download = "https://aka.ms/vc14/vc_redist.x64.exe";
    internal sealed class Requirement
    {
        internal string FileName;
        internal Version MinimumVersion;
        internal string[] Names = new string[0];
        internal uint[] Ordinals = new uint[0];
    }
    internal sealed class FileFact
    {
        internal string FileName;
        internal Version Version;
        internal ushort Machine;
        internal HashSet<string> Names = new HashSet<string>(StringComparer.Ordinal);
        internal HashSet<uint> Ordinals = new HashSet<uint>();
        internal string Error;
    }
    internal sealed class SystemFact
    {
        internal uint WindowsMajor;
        internal ushort NativeArchitecture;
        internal bool ProcessIs64Bit;
        internal List<FileFact> Files = new List<FileFact>();
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct VersionInfo
    {
        internal uint Size, Major, Minor, Build, Platform;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] internal string ServicePack;
        internal ushort ServicePackMajor, ServicePackMinor, SuiteMask;
        internal byte ProductType, Reserved;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct SystemInfo
    {
        internal ushort Architecture, Reserved;
        internal uint PageSize;
        internal IntPtr MinimumAddress, MaximumAddress, ProcessorMask;
        internal uint ProcessorCount, ProcessorType, AllocationGranularity;
        internal ushort ProcessorLevel, ProcessorRevision;
    }
    [DllImport("ntdll.dll", CharSet = CharSet.Unicode)]
    private static extern int RtlGetVersion(ref VersionInfo version);
    [DllImport("kernel32.dll")]
    private static extern void GetNativeSystemInfo(out SystemInfo systemInfo);

    internal static string Validate(SystemFact facts, Requirement[] requirements)
    {
        if (facts.WindowsMajor < 10) return "此版本需要 Windows 10 或更新版本。请在受支持的 Windows 系统上运行。";
        if (facts.NativeArchitecture != 9 || !facts.ProcessIs64Bit) return "此安装包适用于 x64 电脑，请使用与系统架构匹配的版本。";
        List<string> problems = new List<string>();
        foreach (Requirement requirement in requirements)
        {
            FileFact file = facts.Files.Find(delegate(FileFact item) { return String.Equals(item.FileName, requirement.FileName, StringComparison.OrdinalIgnoreCase); });
            if (file == null) { problems.Add("缺少 " + requirement.FileName); continue; }
            if (!String.IsNullOrEmpty(file.Error)) { problems.Add(requirement.FileName + " 无法读取或文件无效：" + file.Error); continue; }
            if (file.Machine != 0x8664) { problems.Add(requirement.FileName + " 不是 x64 版本"); continue; }
            if (file.Version == null || file.Version < requirement.MinimumVersion)
            {
                problems.Add(requirement.FileName + " 当前 " + (file.Version == null ? "版本未知" : file.Version.ToString()) + "，需要 " + requirement.MinimumVersion + " 或更新版本");
                continue;
            }
            foreach (string name in requirement.Names)
                if (!file.Names.Contains(name)) { problems.Add(requirement.FileName + " 缺少本版程序需要的运行库接口（" + name + "）"); break; }
            foreach (uint ordinal in requirement.Ordinals)
                if (!file.Ordinals.Contains(ordinal)) { problems.Add(requirement.FileName + " 缺少本版程序需要的运行库接口（序号 " + ordinal + "）"); break; }
        }
        if (problems.Count == 0) return null;
        return "Windows 运行库尚未准备好：\n" + String.Join("\n", problems.ToArray()) +
            "\n\n请从微软官方安装或修复 Visual C++ v14 x64 运行库，然后重新运行本安装包。\n" +
            OfficialPage + "\n微软 x64 下载入口：" + OfficialX64Download;
    }

    internal static SystemFact Probe(Requirement[] requirements)
    {
        VersionInfo version = new VersionInfo();
        version.Size = (uint)Marshal.SizeOf(typeof(VersionInfo));
        if (RtlGetVersion(ref version) != 0) throw new IOException("无法读取 Windows 版本，请完成系统更新后重试。");
        SystemInfo info;
        GetNativeSystemInfo(out info);
        SystemFact facts = new SystemFact { WindowsMajor = version.Major, NativeArchitecture = info.Architecture, ProcessIs64Bit = Environment.Is64BitProcess };
        if (facts.WindowsMajor < 10 || facts.NativeArchitecture != 9 || !facts.ProcessIs64Bit) return facts;
        string directory = Environment.GetFolderPath(Environment.SpecialFolder.System);
        foreach (Requirement requirement in requirements)
        {
            // Requirement identifiers come from the frozen dependency manifest,
            // never from environment variables, PATH or user input.
            if (requirement.FileName != "VCRUNTIME140.dll" && requirement.FileName != "VCRUNTIME140_1.dll" && requirement.FileName != "mfc140u.dll")
                throw new InvalidDataException("Unexpected Windows runtime requirement.");
            string path = Path.Combine(directory, requirement.FileName);
            if (!File.Exists(path)) continue;
            FileFact fact = new FileFact { FileName = requirement.FileName };
            try
            {
                using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                {
                    if (file.Length < 256 || file.Length > 64 * 1024 * 1024) throw new InvalidDataException("不支持的运行库文件长度");
                    byte[] bytes = new byte[(int)file.Length];
                    int offset = 0, count;
                    while (offset != bytes.Length && (count = file.Read(bytes, offset, bytes.Length - offset)) != 0) offset += count;
                    if (offset != bytes.Length) throw new EndOfStreamException();
                    FileVersionInfo fixedVersion = FileVersionInfo.GetVersionInfo(path);
                    fact.Version = new Version(fixedVersion.FileMajorPart, fixedVersion.FileMinorPart, fixedVersion.FileBuildPart, fixedVersion.FilePrivatePart);
                    ReadExports(bytes, fact);
                }
            }
            catch (Exception error) { fact.Error = error.GetType().Name; }
            facts.Files.Add(fact);
        }
        return facts;
    }

    private static ushort U16(byte[] data, int offset)
    {
        if (offset < 0 || offset > data.Length - 2) throw new InvalidDataException("PE 结构越界");
        return BitConverter.ToUInt16(data, offset);
    }
    private static uint U32(byte[] data, int offset)
    {
        if (offset < 0 || offset > data.Length - 4) throw new InvalidDataException("PE 结构越界");
        return BitConverter.ToUInt32(data, offset);
    }
    private static int Rva(byte[] data, int sections, int count, uint rva)
    {
        for (int i = 0; i < count; i++)
        {
            int section = checked(sections + i * 40);
            uint address = U32(data, section + 12), size = U32(data, section + 16), raw = U32(data, section + 20);
            if (rva >= address && (ulong)rva < (ulong)address + size)
            {
                ulong offset = (ulong)raw + rva - address;
                if (offset >= (ulong)data.Length || offset > Int32.MaxValue) break;
                return (int)offset;
            }
        }
        throw new InvalidDataException("PE 导出指针无效");
    }
    internal static void ReadExports(byte[] data, FileFact facts)
    {
        if (U16(data, 0) != 0x5A4D) throw new InvalidDataException("缺少 PE 文件头");
        int pe = checked((int)U32(data, 0x3C));
        if (U32(data, pe) != 0x00004550) throw new InvalidDataException("PE 标识无效");
        facts.Machine = U16(data, pe + 4);
        if (facts.Machine != 0x8664) return;
        int sectionCount = U16(data, pe + 6), optionalSize = U16(data, pe + 20), optional = checked(pe + 24);
        if (U16(data, optional) != 0x20B || optionalSize < 120 || sectionCount == 0 || sectionCount > 96)
            throw new InvalidDataException("不支持的 PE 结构");
        int sections = checked(optional + optionalSize);
        uint exportRva = U32(data, optional + 112);
        if (exportRva == 0) throw new InvalidDataException("运行库缺少导出表");
        int exports = Rva(data, sections, sectionCount, exportRva);
        uint ordinalBase = U32(data, exports + 16), functions = U32(data, exports + 20), names = U32(data, exports + 24);
        if (functions > 100000 || names > functions) throw new InvalidDataException("运行库导出表长度无效");
        int functionTable = Rva(data, sections, sectionCount, U32(data, exports + 28));
        int nameTable = names == 0 ? 0 : Rva(data, sections, sectionCount, U32(data, exports + 32));
        int ordinalTable = names == 0 ? 0 : Rva(data, sections, sectionCount, U32(data, exports + 36));
        for (uint i = 0; i < functions; i++)
            if (U32(data, checked(functionTable + (int)i * 4)) != 0) facts.Ordinals.Add(checked(ordinalBase + i));
        for (uint i = 0; i < names; i++)
        {
            uint index = U16(data, checked(ordinalTable + (int)i * 2));
            if (index >= functions || !facts.Ordinals.Contains(checked(ordinalBase + index))) throw new InvalidDataException("运行库导出序号无效");
            int start = Rva(data, sections, sectionCount, U32(data, checked(nameTable + (int)i * 4)));
            int end = start;
            while (end < data.Length && end - start < 4096 && data[end] != 0) end++;
            if (end == data.Length || end - start == 4096) throw new InvalidDataException("运行库导出名称无效");
            facts.Names.Add(Encoding.ASCII.GetString(data, start, end - start));
        }
    }
}
