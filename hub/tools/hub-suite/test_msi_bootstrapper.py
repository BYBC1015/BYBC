"""Read-only MSI API and synthetic-process checks; never starts msiexec."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import build_msi_bootstrapper as builder

QA = Path(os.environ.get('CODEX_HUB_MSI_BOOTSTRAPPER_QA', str(Path(tempfile.gettempdir()) / 'codex-hub-msi-bootstrapper-qa')))
QA.mkdir(parents=True, exist_ok=True)
PRODUCT = '{7BD84813-EF2C-4F20-A829-1FF479EB0A85}'  # Synthetic, never registered.
COMPONENT = '{A5358151-336B-475A-ADE8-2579B7E76AD1}'
PACKAGE = '{B513731F-09E6-4D03-926A-AEF96350F063}'

CHILD = r'''
using System;using System.IO;using System.Text;using System.Threading;
class Child {
static int Main(string[] args){using(var output=new StreamWriter(Environment.GetEnvironmentVariable("HUB_MSI_TEST_REPORT"),false,new UTF8Encoding(false))){foreach(var arg in args)output.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes(arg)));}Thread.Sleep(Int32.Parse(Environment.GetEnvironmentVariable("HUB_MSI_TEST_DELAY")??"0"));return Int32.Parse(Environment.GetEnvironmentVariable("HUB_MSI_TEST_EXIT")??"0");}
}
'''

HARNESS = r'''
using System;using System.Diagnostics;using System.IO;using System.Reflection;using System.Text;
using B=NativeMsiBootstrapper;
class Harness {
static int Main(string[] args){try{
if(args[0]=="classify") {var r=B.Classify(Int32.Parse(args[1]));Console.WriteLine(r.Success+" "+r.Cancelled+" "+r.RestartRequired);return 0;}
if(args[0]=="parse") {B.Parse(new string[]{"ALLUSERS=1"});throw new Exception("Accepted unsupported property");}
if(args[0]=="parsecache") {B.Parse(new string[]{"--cache-dir",args[1]});Console.WriteLine("CACHE_OPTION");return 0;}
if(args[0]=="cache") {var options=B.Parse(new string[]{"--cache-dir",args[2]});string target=B.CachedMsiPath(options);using(var input=File.OpenRead(args[1]))B.CacheMsi(input,target,MsiBootstrapBuild.MsiBytes,MsiBootstrapBuild.MsiHash);Console.WriteLine("CACHE "+Convert.ToBase64String(Encoding.UTF8.GetBytes(target)));return 0;}
if(args[0]=="arguments") {var o=new B.Options{Quiet=true,NoShortcuts=true,InstallDirectory=args[4]};int result=B.RunEngine(args[1],B.EngineArguments(args[2],args[3],o),true);Console.WriteLine("CODE "+result);return 0;}
if(args[0]=="build") {B.ValidateWindowsBuild(new WindowsRuntimePrerequisites.SystemFact{WindowsMajor=10,WindowsBuild=UInt32.Parse(args[1])});Console.WriteLine("SUPPORTED_BUILD");return 0;}
if(args[0]=="basicui") {Console.WriteLine(B.EngineArguments("package.msi","log.txt",new B.Options()));return 0;}
if(args[0]=="run") {var watch=Stopwatch.StartNew();int code=B.RunEngine(args[1],B.Quote("synthetic spaced argument"),true);Console.WriteLine("CODE "+code+" MS "+watch.ElapsedMilliseconds);return 0;}
if(args[0]=="verify") {B.VerifyPayload(args[1],Path.Combine(args[1],MsiBootstrapBuild.ProofPath),args[2]);Console.WriteLine("FILES_VERIFIED");return 0;}
if(args[0]=="registered") {var method=typeof(B).GetMethod("VerifyRegisteredProduct",BindingFlags.NonPublic|BindingFlags.Static);method.Invoke(null,new object[]{new B.Options{Quiet=true,InstallDirectory=args[1]},true});throw new Exception("Unexpected registered synthetic product");}
if(args[0]=="extract") {using(var input=File.OpenRead(args[1]))B.ExtractMsi(input,args[2],Int64.Parse(args[3]),args[4]);Console.WriteLine("EXTRACTED");return 0;}
if(args[0]=="log") {string text=B.LogTail(args[1]);Console.WriteLine("CHARS "+text.Length+" END "+text.EndsWith("last-marker"));return 0;}
throw new Exception("Unknown mode");
}catch(Exception error){Console.WriteLine("ERROR "+(error is TargetInvocationException?error.InnerException:error).GetType().FullName);return 1;}}
}
'''


@unittest.skipUnless(os.name == 'nt', 'Windows Framework integration')
class MsiBootstrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix='fixture-', dir=QA))
        cls.payload = cls.work / 'public-files'
        cls.payload.mkdir()
        rows = []
        for name in sorted(builder.ENTRIES) + ['runtime-agent/required-runtime.dll', 'tools/中文 资料.txt']:
            path = cls.payload / name
            path.parent.mkdir(parents=True, exist_ok=True)
            body = ('Synthetic non-executed resource ' + name).encode()
            path.write_bytes(body)
            rows.append({'path': name, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
        marker = cls.payload / 'msi-payload-manifest.json'
        marker.write_text(json.dumps({'schema_version': 1, 'product_code': PRODUCT, 'product_version': '1.3.0', 'files': rows}, ensure_ascii=False), encoding='utf-8')
        cls.msi = cls.work / 'synthetic.msi'
        cls.msi.write_bytes(builder.OLE_HEADER + b'Synthetic bytes; not a real installable MSI')
        cls.proof = {'product_code': PRODUCT, 'product_version': '1.3.0', 'proof_component_code': COMPONENT,
                     'package_code': PACKAGE, 'source_cache_relative_path': 'BYBC/CodexHubInstallerCache/' + PRODUCT[1:-1] + '/' + PACKAGE[1:-1] + '.msi',
                     'cache_lifecycle': 'retained-for-repair-and-rollback', 'cache_removed_by_msi': False,
                     'per_user_only': True, 'no_custom_actions': True, 'no_auto_start': True, 'no_legacy_cleanup': True,
                     'msi_sha256': builder.digest(cls.msi), 'install_dir_property': 'INSTALLDIR',
                     'default_relative_install_dir': 'Programs/CodexHub-MSI/1.3.0',
                     'proof_relative_path': marker.name, 'proof_sha256': builder.digest(marker),
                     'entrypoints': [{'path': row['path'], 'sha256': row['sha256']} for row in rows if row['path'] in builder.ENTRIES]}
        cls.proof_path = cls.work / 'proof.json'
        cls.proof_path.write_text(json.dumps(cls.proof), encoding='utf-8')
        child_source = cls.work / 'Child.cs'
        child_source.write_text(CHILD, encoding='utf-8')
        cls.child = cls.work / 'SyntheticEngine.exe'
        subprocess.run([str(builder.CSC), '/nologo', '/target:exe', '/platform:x64', '/out:' + str(cls.child), str(child_source)], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        harness_source = cls.work / 'Harness.cs'
        harness_source.write_text(HARNESS, encoding='utf-8')
        cls.harness = cls.work / 'Harness.exe'
        builder.compile_bootstrapper(cls.msi, cls.proof, cls.harness, version='1.3.0', extra_sources=[harness_source], main_class='Harness')

    def invoke(self, *args, code=0, delay=0):
        report = self.work / ('child-' + next(tempfile._get_candidate_names()) + '.txt')
        environment = {**os.environ, 'HUB_MSI_TEST_REPORT': str(report), 'HUB_MSI_TEST_EXIT': str(code), 'HUB_MSI_TEST_DELAY': str(delay)}
        result = subprocess.run([str(self.harness), *map(str, args)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        values = [base64.b64decode(line).decode('utf-8') for line in report.read_text(encoding='utf-8').splitlines()] if report.exists() else None
        return result, values

    def test_success_restart_cancel_and_failure_are_distinct(self):
        expected = {0: 'True False False', 3010: 'True False True', 1602: 'False True False', 1603: 'False False False', 1618: 'False False False'}
        for code, text in expected.items():
            with self.subTest(code=code):
                result, _ = self.invoke('classify', code)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.decode().strip(), text)

    def test_standard_arguments_no_restart_no_app_shutdown_and_unicode(self):
        msi, log, target = self.work / '中文 空格.msi', self.work / '日志 空格.log', str(self.work / '目标 空格') + '\\'
        result, args = self.invoke('arguments', self.child, msi, log, target, code=3010)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(args, ['/i', str(msi), '/qn', '/norestart', 'REBOOT=ReallySuppress', 'MSIRESTARTMANAGERCONTROL=Disable', '/Lwe', str(log), 'INSTALLDIR=' + target, 'HUB_SHORTCUTS=0'])
        self.assertIn(b'CODE 3010', result.stdout)
        basic, _ = self.invoke('basicui')
        self.assertIn(b'"/qb"', basic.stdout)
        self.assertNotIn(b'/qb!', basic.stdout)
        self.assertNotIn(b'HUB_SHORTCUTS=0', basic.stdout)

    def test_real_windows_build_floor_matches_msi(self):
        before, _ = self.invoke('build', 17133)
        minimum, _ = self.invoke('build', 17134)
        self.assertEqual(before.returncode, 1)
        self.assertEqual(minimum.returncode, 0)

    def test_waits_for_engine_and_preserves_all_exit_codes(self):
        for code in (0, 3010, 1602, 1603):
            with self.subTest(code=code):
                result, _ = self.invoke('run', self.child, code=code, delay=300)
                fields = result.stdout.decode().split()
                self.assertEqual(fields[:2], ['CODE', str(code)])
                self.assertGreaterEqual(int(fields[3]), 280)

    def test_rejects_arbitrary_msi_properties(self):
        result, _ = self.invoke('parse')
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'ArgumentException', result.stdout)

    def test_full_payload_verification_allows_extra_user_files(self):
        (self.payload / 'personal-note.txt').write_text('Preserved user file fixture', encoding='utf-8')
        result, _ = self.invoke('verify', self.payload, self.payload)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_missing_or_changed_private_runtime_is_not_success(self):
        target = self.payload / 'runtime-agent/required-runtime.dll'
        original = target.read_bytes()
        try:
            target.unlink()
            result, _ = self.invoke('verify', self.payload, self.payload)
            self.assertEqual(result.returncode, 1)
            target.write_bytes(b'changed')
            result, _ = self.invoke('verify', self.payload, self.payload)
            self.assertEqual(result.returncode, 1)
        finally:
            target.write_bytes(original)

    def test_wrong_registered_target_is_rejected(self):
        result, _ = self.invoke('verify', self.payload, self.work / 'different-target')
        self.assertEqual(result.returncode, 1)

    def test_unregistered_product_is_not_success_read_only(self):
        result, _ = self.invoke('registered', self.payload)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'InvalidDataException', result.stdout)

    def test_complete_embedded_msi_hash_and_length_before_start(self):
        output = self.work / 'verified-copy.msi'
        result, _ = self.invoke('extract', self.msi, output, self.msi.stat().st_size, builder.digest(self.msi))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), self.msi.read_bytes())
        for label, size, checksum in [('hash', self.msi.stat().st_size, '0' * 64), ('length', self.msi.stat().st_size - 1, builder.digest(self.msi))]:
            result, _ = self.invoke('extract', self.msi, self.work / (label + '.msi'), size, checksum)
            self.assertEqual(result.returncode, 1)

    def test_logs_are_read_with_a_fixed_bound(self):
        source = self.work / 'large.log'
        source.write_text('synthetic line\n' * 100000 + 'last-marker', encoding='utf-16')
        result, _ = self.invoke('log', source)
        self.assertEqual(result.returncode, 0)
        fields = result.stdout.decode().split()
        self.assertLessEqual(int(fields[1]), 16000)
        self.assertEqual(fields[-1], 'True')

    def test_cache_is_atomic_persistent_reused_and_hash_checked(self):
        root = self.work / 'persistent-source'
        first, _ = self.invoke('cache', self.msi, root)
        self.assertEqual(first.returncode, 0, first.stdout)
        target = root / self.proof['source_cache_relative_path']
        self.assertEqual(target.read_bytes(), self.msi.read_bytes())
        modified = target.stat().st_mtime_ns
        again, _ = self.invoke('cache', self.msi, root)
        self.assertEqual(again.returncode, 0)
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertFalse(list(target.parent.glob('.pending-*')))
        target.write_bytes(b'wrong existing bytes must be preserved')
        invalid, _ = self.invoke('cache', self.msi, root)
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(target.read_bytes(), b'wrong existing bytes must be preserved')

    def test_cache_override_is_explicit_absolute_and_cannot_follow_junction(self):
        relative, _ = self.invoke('parsecache', 'relative-cache')
        self.assertEqual(relative.returncode, 1)
        outside = self.work / 'outside-cache'
        outside.mkdir()
        marker = outside / 'preserved.txt'
        marker.write_text('preserve', encoding='utf-8')
        link = self.work / 'cache-link'
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
        self.assertTrue(link.is_junction())
        result, _ = self.invoke('cache', self.msi, link)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(list(outside.iterdir()), [marker])

    def test_builder_rejects_unmatched_proof_and_unsafe_paths(self):
        bad = {**self.proof, 'msi_sha256': '0' * 64}
        path = self.work / 'wrong-proof.json'
        path.write_text(json.dumps(bad), encoding='utf-8')
        with self.assertRaises(ValueError):
            builder.build(self.msi, self.work / 'must-not-build.exe', proof_json=path, version='1.3.0')
        with self.assertRaises(ValueError):
            builder.validate_proof({**self.proof, 'proof_relative_path': '../outside.json'}, '1.3.0')
        with self.assertRaises(ValueError):
            builder.validate_proof({**self.proof, 'per_user_only': False}, '1.3.0')
        with self.assertRaises(ValueError):
            builder.validate_proof({**self.proof, 'source_cache_relative_path': '../elsewhere.msi'}, '1.3.0')

    def test_missing_prerequisite_stops_before_extraction(self):
        profile = json.loads(builder.RUNTIME_PROFILE.read_text(encoding='utf-8'))
        profile['files'][0]['minimum_file_version'] = '9999.0.0.0'
        path = self.work / 'unmet.json'
        path.write_text(json.dumps(profile), encoding='utf-8')
        executable = self.work / 'Unmet.exe'
        builder.compile_bootstrapper(self.msi, self.proof, executable, version='1.3.0', runtime_profile=path)
        temporary = self.work / 'unmet-temp'
        temporary.mkdir()
        result = subprocess.run([str(executable), '--quiet'], env={**os.environ, 'TEMP': str(temporary), 'TMP': str(temporary)}, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'https://aka.ms/vc14/vc_redist.x64.exe', result.stderr)
        self.assertEqual(list(temporary.iterdir()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
