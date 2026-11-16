"""Isolated Windows wrapper checks; never installs or changes system libraries."""
from __future__ import annotations
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
import zipfile

import build_native_wrapper as builder

ROOT = Path(os.environ.get('CODEX_HUB_WRAPPER_QA_DIR', str(Path(tempfile.gettempdir()) / 'codex-hub-wrapper-qa')))
ROOT.mkdir(parents=True, exist_ok=True)

CHILD = r'''
using System; using System.Diagnostics; using System.IO; using System.Text;
using System.Collections.Generic; using System.Web.Script.Serialization;
internal class Child {
  static string Encode(string text) { return Convert.ToBase64String(Encoding.UTF8.GetBytes(text)); }
  static int Main(string[] args) { try { return Run(args); } catch(Exception error) { File.WriteAllText(Environment.GetEnvironmentVariable("CODEX_HUB_WRAPPER_QA_REPORT") + ".error",error.ToString()); return 89; } }
  static int Run(string[] args) {
    string report = Environment.GetEnvironmentVariable("CODEX_HUB_WRAPPER_QA_REPORT");
    string root = Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName);
    using (StreamWriter w = new StreamWriter(report, false, new UTF8Encoding(false))) {
      w.WriteLine(Encode(Environment.CurrentDirectory));
      w.WriteLine(Encode(Environment.GetEnvironmentVariable("CODEX_HUB_INSTALLER_OUTER_EXE")));
      w.WriteLine(Encode(root)); foreach(string arg in args) w.WriteLine(Encode(arg));
    }
    File.WriteAllText(report + ".ready", "ready");
    string outside = Environment.GetEnvironmentVariable("CODEX_HUB_WRAPPER_QA_JUNCTION");
    if (!String.IsNullOrEmpty(outside)) {
      for(int i=0;i<500&&!File.Exists(Path.Combine(root,"release.flag"));i++) System.Threading.Thread.Sleep(20);
      if(!File.Exists(Path.Combine(root,"release.flag")))return 88;
    }
    string mode = Environment.GetEnvironmentVariable("CODEX_HUB_WRAPPER_QA_RECEIPT") ?? "valid";
    if(mode != "missing") {
      string nonce=Environment.GetEnvironmentVariable("CODEX_HUB_INSTALLER_NONCE");
      string operation=Environment.GetEnvironmentVariable("CODEX_HUB_INSTALLER_OPERATION");
      string target=Environment.GetEnvironmentVariable("CODEX_HUB_INSTALLER_TARGET");
      if(String.IsNullOrEmpty(target))target=Path.Combine(Environment.CurrentDirectory,"gui-target");
      string status=operation=="install"?"installed":operation=="remove"?"removed":operation=="scan"?"scanned":operation=="help"?"help":"launched";
      if(mode=="pending")status="installed_cleanup_pending";
      if(mode=="remove_pending")status="uninstall_pending";
      if(mode=="wrong_target")target=Path.Combine(Environment.CurrentDirectory,"wrong-target");
      var serializer=new JavaScriptSerializer();
      if(operation=="install") {
        Directory.CreateDirectory(target);
        var marker=new Dictionary<string,object>{{"root",target},{"version","fixture"},{"installation_committed",true},{"operation_nonce",mode=="wrong_marker"?new String('0',32):nonce},{"status",status}};
        File.WriteAllText(Path.Combine(target,".hub-suite-install.json"),serializer.Serialize(marker),new UTF8Encoding(false));
      }
      var receipt=new Dictionary<string,object>{{"schema",1},{"nonce",mode=="wrong_nonce"?new String('0',32):nonce},{"version","fixture"},{"operation",mode=="wrong_operation"?"remove":operation},{"target",target},{"status",status},{"message","Synthetic fixture result"}};
      File.WriteAllText(Environment.GetEnvironmentVariable("CODEX_HUB_INSTALLER_RECEIPT_PATH"),serializer.Serialize(receipt),new UTF8Encoding(false));
    }
    return Int32.Parse(Environment.GetEnvironmentVariable("CODEX_HUB_WRAPPER_QA_EXIT") ?? "0");
  }
}
'''

PREREQUISITE_HARNESS = r'''
using System;using System.Collections.Generic;using P=WindowsRuntimePrerequisites;
class Harness {
static int cases=0;
static void Check(bool value,string name){if(!value)throw new Exception(name);cases++;}
static P.Requirement[] Rules(){return WrapperBuild.RuntimeRequirements;}
static P.SystemFact Good(){var f=new P.SystemFact{WindowsMajor=10,NativeArchitecture=9,ProcessIs64Bit=true};foreach(var rule in Rules())f.Files.Add(new P.FileFact{FileName=rule.FileName,Machine=0x8664,Version=rule.MinimumVersion,Names=new HashSet<string>(rule.Names),Ordinals=new HashSet<uint>(rule.Ordinals)});return f;}
static int Main(){try{
var r=Rules();var f=Good();Check(P.Validate(f,r)==null,"minimum accepted");
f=Good();foreach(var x in f.Files)x.Version=new Version(14,50,0,0);Check(P.Validate(f,r)==null,"newer accepted");
f=Good();f.WindowsMajor=6;Check(P.Validate(f,r).Contains("Windows 10"),"old Windows");
f=Good();f.NativeArchitecture=0;Check(P.Validate(f,r).Contains("x64"),"x86 OS");
f=Good();f.ProcessIs64Bit=false;Check(P.Validate(f,r).Contains("x64"),"x86 process");
f=Good();f.Files.Clear();Check(P.Validate(f,r).Contains(P.OfficialX64Download)&&P.Validate(f,r).Contains("VCRUNTIME140.dll"),"missing gives exact requirement and official source");
f=Good();f.Files[0].Version=new Version(14,41,0,0);Check(P.Validate(f,r).Contains("14.42.34438.0"),"old VC");
f=Good();f.Files[0].Machine=0x14c;Check(P.Validate(f,r).Contains("不是 x64"),"wrong PE machine");
f=Good();f.Files[0].Error="InvalidDataException";Check(P.Validate(f,r).Contains("文件无效"),"invalid PE");
f=Good();f.Files[0].Names.Clear();Check(P.Validate(f,r).Contains("缺少本版程序"),"missing named export");
f=Good();f.Files[2].Ordinals.Clear();Check(P.Validate(f,r).Contains("序号"),"missing ordinal export");
f=Good();f.Files.RemoveAt(2);Check(P.Validate(f,r).Contains("mfc140u.dll"),"missing actual MFC dependency");
bool malformed=false;try{P.ReadExports(new byte[10],new P.FileFact());}catch(System.IO.InvalidDataException){malformed=true;}Check(malformed,"truncated PE");
var actual=P.Probe(r);var error=P.Validate(actual,r);Check(error==null,"actual system: "+error);
Console.WriteLine("CASES "+cases+" OS "+actual.WindowsMajor+" ARCH "+actual.NativeArchitecture);
foreach(var x in actual.Files)Console.WriteLine(x.FileName+" version="+x.Version+" machine="+x.Machine+" names="+x.Names.Count+" ordinals="+x.Ordinals.Count);
return 0;
}catch(Exception e){Console.Error.WriteLine(e);return 1;}}
}
'''

@unittest.skipUnless(os.name == 'nt', 'Windows .NET Framework integration check')
class WrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix='fixture-', dir=ROOT))
        cls.payload = cls.work / 'payload'
        cls.payload.mkdir()
        source = cls.work / 'Child.cs'
        source.write_text(CHILD, encoding='utf-8')
        cls.child = cls.payload / 'InstallerCore.exe'
        subprocess.run([str(builder.CSC), '/nologo', '/target:winexe', '/reference:System.Web.Extensions.dll', '/out:' + str(cls.child), str(source)], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        (cls.payload / '中文 文件.txt').write_text('Synthetic resource content.', encoding='utf-8')
        nested = cls.payload / 'runtime-fixture'
        nested.mkdir()
        (nested / 'nested.txt').write_text('Nested runtime extraction.', encoding='utf-8')
        (nested / 'thirdparty-kept.dll').write_bytes(b'Synthetic nonexecuted third-party resource fixture')
        cls.wrapper = cls.work / 'wrapper' / 'Uninstall.exe'
        cls.wrapper.parent.mkdir()
        builder.build(cls.payload, 'InstallerCore.exe', cls.wrapper, version='fixture')

    def invoke(self, executable, args, exit_code=0, junction=None, receipt_mode='valid'):
        work = Path(tempfile.mkdtemp(prefix='run-', dir=self.work))
        temporary = work / '临时 空格'
        temporary.mkdir()
        report = work / 'child.txt'
        environment = os.environ.copy()
        environment.update(TEMP=str(temporary), TMP=str(temporary), LOCALAPPDATA=str(work / 'local'), CODEX_HUB_WRAPPER_QA_REPORT=str(report), CODEX_HUB_WRAPPER_QA_EXIT=str(exit_code), CODEX_HUB_WRAPPER_QA_RECEIPT=receipt_mode)
        if junction:
            environment['CODEX_HUB_WRAPPER_QA_JUNCTION'] = str(junction)
        with (work / 'stderr.txt').open('wb') as errors:
            process = subprocess.Popen([str(executable), *args], env=environment, cwd=work, stderr=errors, creationflags=subprocess.CREATE_NO_WINDOW)
        if junction:
            deadline = time.monotonic() + 8
            while not report.with_suffix('.txt.ready').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(report.is_file())
            lines = report.read_text(encoding='utf-8').splitlines()
            child_root = Path(base64.b64decode(lines[2]).decode('utf-8'))
            import _winapi
            _winapi.CreateJunction(str(junction), str(child_root / 'junction'))
            self.assertTrue((child_root / 'junction').is_junction())
            (child_root / 'release.flag').write_text('release', encoding='utf-8')
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        values = [base64.b64decode(line).decode('utf-8') for line in report.read_text(encoding='utf-8').splitlines()] if report.exists() else None
        return code, values, work, temporary

    def test_arguments_cwd_exit_code_and_cleanup(self):
        args = ['--quiet', '', '空 格', 'simple', 'embedded"quote', 'C:\\ending slash\\', '\\"', 'line\nbreak', 'tab\tvalue', '--install-dir', 'relative folder']
        code, values, work, temporary = self.invoke(self.wrapper, args, 23)
        self.assertEqual(code, 23)
        self.assertEqual(values[0], str(work))
        self.assertEqual(values[1], str(self.wrapper))
        self.assertEqual(values[3:], args)
        self.assertFalse(Path(values[2]).exists())
        self.assertEqual(list(temporary.iterdir()), [])

    def test_copied_uninstaller_is_self_contained(self):
        copied = self.work / 'copied helper' / 'Uninstall.exe'
        copied.parent.mkdir()
        shutil.copy2(self.wrapper, copied)
        code, values, _, temporary = self.invoke(copied, ['--quiet', '--remove', 'D:\\独立 工程'], 7)
        self.assertEqual(code, 7)
        self.assertEqual(values[1], str(copied))
        self.assertEqual(list(temporary.iterdir()), [])

    def test_zero_exit_without_receipt_is_not_success(self):
        code, values, _, temporary = self.invoke(self.wrapper, ['--quiet'], receipt_mode='missing')
        self.assertNotEqual(code, 0)
        self.assertIsNotNone(values)
        self.assertEqual(list(temporary.iterdir()), [])

    def test_completed_install_requires_matching_receipt_and_marker(self):
        code, _, _, temporary = self.invoke(self.wrapper, ['--quiet'])
        self.assertEqual(code, 0)
        self.assertEqual(list(temporary.iterdir()), [])
        for mode in ('wrong_nonce', 'wrong_operation', 'wrong_target', 'wrong_marker'):
            with self.subTest(mode=mode):
                code, _, _, temporary = self.invoke(self.wrapper, ['--quiet'], receipt_mode=mode)
                self.assertNotEqual(code, 0)
                self.assertEqual(list(temporary.iterdir()), [])

    def test_committed_install_pending_cleanup_is_explicit(self):
        code, _, work, temporary = self.invoke(self.wrapper, ['--quiet'], receipt_mode='pending')
        self.assertEqual(code, 0)
        self.assertIn('旧程序清理未完成', (work / 'stderr.txt').read_text(encoding='utf-8'))
        self.assertEqual(list(temporary.iterdir()), [])

    def test_uninstall_completion_is_distinct_from_launch_or_pending(self):
        code, _, _, _ = self.invoke(self.wrapper, ['--quiet', '--remove', 'isolated-target'])
        self.assertEqual(code, 0)
        code, _, _, _ = self.invoke(self.wrapper, ['--quiet', '--remove', 'isolated-target'], receipt_mode='remove_pending')
        self.assertNotEqual(code, 0)
        code, _, work, _ = self.invoke(self.wrapper, ['--quiet', '--uninstall', 'isolated-target'])
        self.assertEqual(code, 0)
        self.assertIn('尚未确认文件移除完成', (work / 'stderr.txt').read_text(encoding='utf-8'))

    def test_prerequisite_failure_stops_before_extraction(self):
        folder = self.work / 'unmet-prerequisite'
        folder.mkdir()
        profile = json.loads(builder.RUNTIME_PROFILE.read_text(encoding='utf-8'))
        profile['files'][0]['minimum_file_version'] = '9999.0.0.0'
        profile_path = folder / 'unmet.json'
        profile_path.write_text(json.dumps(profile), encoding='utf-8')
        exe = folder / 'Unmet.exe'
        builder.compile_wrapper(self.wrapper.with_name('Uninstall.wrapper-payload.zip'),
            self.wrapper.with_name('Uninstall.wrapper-payload.manifest'), 'InstallerCore.exe', exe,
            runtime_profile=profile_path)
        temporary = folder / 'temp'
        temporary.mkdir()
        environment = os.environ.copy()
        environment.update(TEMP=str(temporary), TMP=str(temporary), CODEX_HUB_WRAPPER_QA_REPORT=str(folder/'child.txt'))
        result = subprocess.run([str(exe), '--quiet'], env=environment, cwd=folder,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'https://aka.ms/vc14/vc_redist.x64.exe', result.stderr)
        self.assertIn(b'VCRUNTIME140.dll', result.stderr)
        self.assertFalse((folder / 'child.txt').exists())
        self.assertEqual(list(temporary.iterdir()), [])

    def test_prerequisite_matrix_and_actual_system_exports(self):
        source = self.work / 'PrerequisiteHarness.cs'
        source.write_text(PREREQUISITE_HARNESS, encoding='utf-8')
        executable = self.work / 'PrerequisiteHarness.exe'
        subprocess.run([str(builder.CSC), '/nologo', '/target:exe', '/platform:x64', '/out:'+str(executable),
            str(builder.PREREQUISITE_SOURCE), str(self.wrapper.with_name('Uninstall.wrapper-build.cs')),
            str(source)], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        result = subprocess.run([str(executable)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertIn(b'CASES 14', result.stdout)

    def malformed(self, name, *, members, manifest_rows):
        folder = self.work / name
        folder.mkdir()
        archive = folder / 'bad.zip'
        manifest = folder / 'bad.manifest'
        with zipfile.ZipFile(archive, 'w') as z:
            for member, body, mode in members:
                info = zipfile.ZipInfo(member)
                info.external_attr = mode << 16
                z.writestr(info, body)
        manifest.write_text(''.join(sha + '\t' + str(length) + '\t' + base64.b64encode(path.encode()).decode() + '\n' for path, length, sha in manifest_rows), encoding='utf-8')
        exe = folder / 'bad.exe'
        builder.compile_wrapper(archive, manifest, 'InstallerCore.exe', exe)
        code, values, _, temporary = self.invoke(exe, ['--quiet'])
        self.assertNotEqual(code, 0)
        self.assertIsNone(values)
        self.assertEqual(list(temporary.iterdir()), [])

    def test_rejects_traversal_before_execution(self):
        body = self.child.read_bytes(); sha = hashlib.sha256(body).hexdigest()
        self.malformed('traversal', members=[('InstallerCore.exe',body,stat.S_IFREG|0o644),('../escaped.txt',b'outside',stat.S_IFREG|0o644)], manifest_rows=[('InstallerCore.exe',len(body),sha),('../escaped.txt',7,hashlib.sha256(b'outside').hexdigest())])

    def test_rejects_case_collision(self):
        body = self.child.read_bytes(); sha = hashlib.sha256(body).hexdigest()
        self.malformed('case-collision', members=[('InstallerCore.exe',body,stat.S_IFREG|0o644),('installercore.exe',body,stat.S_IFREG|0o644)], manifest_rows=[('InstallerCore.exe',len(body),sha),('installercore.exe',len(body),sha)])

    def test_rejects_link_entry(self):
        body = self.child.read_bytes(); sha = hashlib.sha256(body).hexdigest()
        self.malformed('symlink', members=[('InstallerCore.exe',body,stat.S_IFLNK|0o777)], manifest_rows=[('InstallerCore.exe',len(body),sha)])

    def test_rejects_content_hash_mismatch(self):
        body = self.child.read_bytes()
        self.malformed('hash-mismatch', members=[('InstallerCore.exe',body,stat.S_IFREG|0o644)], manifest_rows=[('InstallerCore.exe',len(body),'0'*64)])

    def test_rejects_member_size_mismatch(self):
        body = self.child.read_bytes(); sha = hashlib.sha256(body).hexdigest()
        self.malformed('size-mismatch', members=[('InstallerCore.exe',body,stat.S_IFREG|0o644)], manifest_rows=[('InstallerCore.exe',len(body)-1,sha)])

    def test_cleanup_never_follows_child_created_junction(self):
        outside = self.work / 'outside-retained'
        outside.mkdir()
        marker = outside / 'retained.txt'
        marker.write_text('Must survive', encoding='utf-8')
        code, values, _, temporary = self.invoke(self.wrapper, ['--quiet'], junction=outside)
        self.assertEqual(code, 0)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'Must survive')
        self.assertTrue(Path(values[2]).exists())
        self.assertEqual(len(list(temporary.iterdir())), 1)

    def test_builder_rejects_payload_junction(self):
        linked = self.work / 'linked-payload'
        import _winapi
        _winapi.CreateJunction(str(self.payload), str(linked))
        self.assertTrue(linked.is_junction())
        with self.assertRaises(ValueError):
            builder.inventory(linked)

@unittest.skipUnless(os.name == 'nt', 'Windows packaged bootstrap contract')
class BootstrapContractTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix='contract-', dir=ROOT))
        self.runtime = self.work / 'runtime-installer'
        self.runtime.mkdir()
        self.nonce = '0123456789abcdef' * 2
        self.environment = {
            'CODEX_HUB_INSTALLER_RECEIPT_PATH': str(self.work / ('.completion-' + self.nonce + '.json')),
            'CODEX_HUB_INSTALLER_NONCE': self.nonce,
            'CODEX_HUB_INSTALLER_OPERATION': 'install',
            'CODEX_HUB_INSTALLER_TARGET': str(self.work / 'target'),
        }
        spec = importlib.util.spec_from_file_location('fixture_native_installer_main', Path(__file__).with_name('native_installer_main.py'))
        self.module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {'installer': types.SimpleNamespace(main=lambda: 23)}):
            spec.loader.exec_module(self.module)

    def validate(self, **changes):
        with mock.patch.dict(os.environ, {**self.environment, **changes}), mock.patch.object(sys, '_MEIPASS', str(self.runtime), create=True):
            self.module.validate_receipt_contract()

    def test_valid_contract_and_external_uninstaller_identity(self):
        self.validate()
        outer = self.work / 'Uninstall.exe'
        outer.write_bytes(b'MZsynthetic-not-executed')
        with mock.patch.dict(os.environ, {**self.environment, 'CODEX_HUB_INSTALLER_OUTER_EXE': str(outer)}), mock.patch.object(sys, '_MEIPASS', str(self.runtime), create=True), mock.patch.object(sys, 'executable', sys.executable):
            self.assertEqual(self.module.main(), 23)
            self.assertEqual(sys.executable, str(outer))

    def test_rejects_replayed_or_misdirected_contract(self):
        invalid = [
            {'CODEX_HUB_INSTALLER_NONCE': 'old'},
            {'CODEX_HUB_INSTALLER_RECEIPT_PATH': str(self.runtime / ('.completion-' + self.nonce + '.json'))},
            {'CODEX_HUB_INSTALLER_RECEIPT_PATH': str(self.work / 'unexpected.json')},
            {'CODEX_HUB_INSTALLER_OPERATION': 'unknown'},
            {'CODEX_HUB_INSTALLER_TARGET': 'relative'},
            {'CODEX_HUB_INSTALLER_OPERATION': 'remove', 'CODEX_HUB_INSTALLER_TARGET': ''},
        ]
        for changes in invalid:
            with self.subTest(changes=list(changes)):
                with self.assertRaises(RuntimeError):
                    self.validate(**changes)
        Path(self.environment['CODEX_HUB_INSTALLER_RECEIPT_PATH']).write_text('{}', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            self.validate()

if __name__ == '__main__':
    unittest.main(verbosity=2)
