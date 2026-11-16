"""Build a per-user, versioned MSI from a verified public payload; never install it."""
from __future__ import annotations

import argparse
import ctypes
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import uuid

NAMESPACE = uuid.UUID('A509A8C4-D62A-4F59-92E0-3AF33EC31663')
UPGRADE_CODE = '{' + str(uuid.uuid5(NAMESPACE, 'CodexHub/current-user/x64')).upper() + '}'
PROOF_PATH = 'msi-payload-manifest.json'
ENTRYPOINTS = ('CodexBridge.exe', 'HubDesktop.exe', 'HubAgentRuntime.exe')
EXCLUDED = {
    'Uninstall.exe': 'MSI owns standard uninstall; the legacy Python uninstaller is not shipped.',
    'tools/beat-workshop/runtime.json': 'Mutable preference is created by the application, never MSI-owned.',
    'tools/beat-workshop/assets/fluidsynth.zip': 'Audited duplicate development cache; extracted runtime and licenses remain.',
}
SCHEMAS = {
    'Property': '`Property` CHAR(72) NOT NULL, `Value` CHAR(0) NOT NULL LOCALIZABLE PRIMARY KEY `Property`',
    'Directory': '`Directory` CHAR(72) NOT NULL, `Directory_Parent` CHAR(72), `DefaultDir` CHAR(255) NOT NULL LOCALIZABLE PRIMARY KEY `Directory`',
    'Component': '`Component` CHAR(72) NOT NULL, `ComponentId` CHAR(38), `Directory_` CHAR(72) NOT NULL, `Attributes` SHORT NOT NULL, `Condition` CHAR(255), `KeyPath` CHAR(72) PRIMARY KEY `Component`',
    'File': '`File` CHAR(72) NOT NULL, `Component_` CHAR(72) NOT NULL, `FileName` CHAR(255) NOT NULL LOCALIZABLE, `FileSize` LONG NOT NULL, `Version` CHAR(72), `Language` CHAR(20), `Attributes` SHORT, `Sequence` SHORT NOT NULL PRIMARY KEY `File`',
    'MsiFileHash': '`File_` CHAR(72) NOT NULL, `Options` SHORT NOT NULL, `HashPart1` LONG NOT NULL, `HashPart2` LONG NOT NULL, `HashPart3` LONG NOT NULL, `HashPart4` LONG NOT NULL PRIMARY KEY `File_`',
    'Feature': '`Feature` CHAR(38) NOT NULL, `Feature_Parent` CHAR(38), `Title` CHAR(64) LOCALIZABLE, `Description` CHAR(255) LOCALIZABLE, `Display` SHORT, `Level` SHORT NOT NULL, `Directory_` CHAR(72), `Attributes` SHORT NOT NULL PRIMARY KEY `Feature`',
    'FeatureComponents': '`Feature_` CHAR(38) NOT NULL, `Component_` CHAR(72) NOT NULL PRIMARY KEY `Feature_`, `Component_`',
    'Media': '`DiskId` SHORT NOT NULL, `LastSequence` SHORT NOT NULL, `DiskPrompt` CHAR(64) LOCALIZABLE, `Cabinet` CHAR(255), `VolumeLabel` CHAR(32), `Source` CHAR(72) PRIMARY KEY `DiskId`',
    'InstallExecuteSequence': '`Action` CHAR(72) NOT NULL, `Condition` CHAR(255), `Sequence` SHORT PRIMARY KEY `Action`',
    'InstallUISequence': '`Action` CHAR(72) NOT NULL, `Condition` CHAR(255), `Sequence` SHORT PRIMARY KEY `Action`',
    'Condition': '`Feature_` CHAR(38) NOT NULL, `Level` SHORT NOT NULL, `Condition` CHAR(255) PRIMARY KEY `Feature_`, `Level`',
    'LaunchCondition': '`Condition` CHAR(255) NOT NULL, `Description` CHAR(255) NOT NULL LOCALIZABLE PRIMARY KEY `Condition`',
    'Upgrade': '`UpgradeCode` CHAR(38) NOT NULL, `VersionMin` CHAR(20), `VersionMax` CHAR(20), `Language` CHAR(255), `Attributes` LONG NOT NULL, `Remove` CHAR(255), `ActionProperty` CHAR(72) NOT NULL PRIMARY KEY `UpgradeCode`, `VersionMin`, `VersionMax`, `Language`, `Attributes`',
    'RemoveFile': '`FileKey` CHAR(72) NOT NULL, `Component_` CHAR(72) NOT NULL, `FileName` CHAR(255) LOCALIZABLE, `DirProperty` CHAR(72) NOT NULL, `InstallMode` SHORT NOT NULL PRIMARY KEY `FileKey`',
    'Registry': '`Registry` CHAR(72) NOT NULL, `Root` SHORT NOT NULL, `Key` CHAR(255) NOT NULL LOCALIZABLE, `Name` CHAR(255) LOCALIZABLE, `Value` CHAR(0) LOCALIZABLE, `Component_` CHAR(72) NOT NULL PRIMARY KEY `Registry`',
    'RegLocator': '`Signature_` CHAR(72) NOT NULL, `Root` SHORT NOT NULL, `Key` CHAR(255) NOT NULL, `Name` CHAR(255), `Type` SHORT PRIMARY KEY `Signature_`',
    # AppSearch requires Signature to exist, even when every search is a raw
    # registry value. Such searches must NOT have a Signature row: that would
    # turn them into file searches. Keep the other locator tables explicitly
    # empty too, so the complete standard search chain has a typed schema.
    'Signature': '`Signature` CHAR(72) NOT NULL, `FileName` CHAR(255) NOT NULL, `MinVersion` CHAR(20), `MaxVersion` CHAR(20), `MinSize` LONG, `MaxSize` LONG, `MinDate` LONG, `MaxDate` LONG, `Languages` CHAR(255) PRIMARY KEY `Signature`',
    'CompLocator': '`Signature_` CHAR(72) NOT NULL, `ComponentId` CHAR(38) NOT NULL, `Type` SHORT PRIMARY KEY `Signature_`',
    'IniLocator': '`Signature_` CHAR(72) NOT NULL, `FileName` CHAR(255) NOT NULL, `Section` CHAR(96) NOT NULL, `Key` CHAR(128) NOT NULL, `Field` SHORT, `Type` SHORT PRIMARY KEY `Signature_`',
    'DrLocator': '`Signature_` CHAR(72) NOT NULL, `Parent` CHAR(72), `Path` CHAR(255), `Depth` SHORT PRIMARY KEY `Signature_`, `Parent`, `Path`',
    'AppSearch': '`Property` CHAR(72) NOT NULL, `Signature_` CHAR(72) NOT NULL PRIMARY KEY `Property`, `Signature_`',
    'Shortcut': '`Shortcut` CHAR(72) NOT NULL, `Directory_` CHAR(72) NOT NULL, `Name` CHAR(128) NOT NULL LOCALIZABLE, `Component_` CHAR(72) NOT NULL, `Target` CHAR(255) NOT NULL, `Arguments` CHAR(255), `Description` CHAR(255) LOCALIZABLE, `Hotkey` SHORT, `Icon_` CHAR(72), `IconIndex` SHORT, `ShowCmd` SHORT, `WkDir` CHAR(72) PRIMARY KEY `Shortcut`',
}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identity(kind, value):
    return kind + '_' + hashlib.sha256(value.casefold().encode()).hexdigest()[:40]


def guid(value):
    return '{' + str(uuid.uuid5(NAMESPACE, value)).upper() + '}'


def maintenance_source_contract(product_code, package_code):
    product = str(uuid.UUID(product_code.strip('{}'))).upper()
    package = str(uuid.UUID(package_code.strip('{}'))).upper()
    return {'source_cache_relative_path': 'BYBC/CodexHubInstallerCache/' + product + '/' + package + '.msi',
            'cache_lifecycle': 'retained-for-repair-and-rollback', 'cache_removed_by_msi': False}


def valid_version(value):
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('MSI requires exactly three numeric version fields')
    if any(n > limit for n, limit in zip(map(int, value.split('.')), (255, 255, 65535))):
        raise ValueError('MSI version is outside supported limits')
    return value


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Invalid public payload path')
    parts = value.split('/')
    if any(not part or part in ('.', '..') or part.endswith((' ', '.'))
           or re.search(r'[<>:"|?*\x00-\x1f]', part)
           or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)
           for part in parts):
        raise ValueError('Unsafe public payload path: ' + value)
    value.encode('gbk', errors='strict')
    return PurePosixPath(value)


def reject_reparse(path):
    for item in (Path(path), *Path(path).parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), 'st_file_attributes', 0)
                                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('Reparse point in MSI input/output path')


def public_plan(payload, manifest_path=None, *, version=None):
    payload = Path(payload).absolute()
    reject_reparse(payload)
    manifest_path = Path(manifest_path or payload / 'bundle-manifest.json').absolute()
    reject_reparse(manifest_path)
    document = json.loads(manifest_path.read_text(encoding='utf-8'))
    version = valid_version(version or document['version'])
    rows, seen, skipped = [], set(), []
    for item in document['files']:
        relative = str(safe_relative(item['path']))
        if relative.casefold() in seen or relative in {PROOF_PATH, 'bundle-manifest.json', '.hub-suite-install.json'}:
            raise ValueError('Duplicate or reserved public payload path')
        seen.add(relative.casefold())
        source = payload.joinpath(*PurePosixPath(relative).parts)
        reject_reparse(source)
        if not source.is_file() or source.stat().st_size != item['bytes'] or digest(source) != item['sha256']:
            raise ValueError('Public input differs from manifest: ' + relative)
        if relative in EXCLUDED:
            skipped.append({'path': relative, 'reason': EXCLUDED[relative], 'sha256': item['sha256']})
            continue
        rows.append({'path': relative, 'bytes': item['bytes'], 'sha256': item['sha256'], 'source': str(source)})
    actual = set()
    for directory, folders, files in os.walk(payload, followlinks=False):
        for name in folders + files:
            reject_reparse(Path(directory) / name)
        actual.update((Path(directory) / name).relative_to(payload).as_posix().casefold() for name in files)
    if actual - {'bundle-manifest.json'} != seen:
        raise ValueError('Unmanifested or missing public payload files')
    if not set(ENTRYPOINTS).issubset(row['path'] for row in rows):
        raise ValueError('All three product entrypoints are required')
    if len(rows) > 32000 or sum(row['bytes'] for row in rows) >= 2_000_000_000:
        raise ValueError('Payload exceeds this single-cabinet profile')
    rows.sort(key=lambda row: row['path'])
    product_code = guid('product/current-user/x64/' + version)
    root_identity = 'component/current-user/x64/Programs/CodexHub-MSI/' + version + '/'
    return {'product_version': version, 'product_code': product_code, 'upgrade_code': UPGRADE_CODE,
            'default_relative_install_dir': 'Programs/CodexHub-MSI/' + version,
            'install_dir_property': 'INSTALLDIR', 'source_manifest_sha256': digest(manifest_path),
            'payload': str(payload), 'files': rows, 'excluded': skipped,
            'root_identity': root_identity, 'components': document.get('components', {})}


def short_long(name, unique):
    # Stable aliases avoid locale-dependent 8.3 generation. table_plan rejects
    # collisions before constructing a cabinet or installation database.
    return ('H' + hashlib.sha256(unique.encode()).hexdigest()[:7].upper()) + '|' + name


def table_plan(plan):
    rows = {table: [] for table in SCHEMAS}
    def add(table, *values):
        rows[table].append(list(values))
    version = plan['product_version']
    registry_key = 'Software\\BYBC\\CodexHubMSI\\' + version
    properties = {'ProductCode': plan['product_code'], 'UpgradeCode': plan['upgrade_code'],
        'ProductName': 'BYBC Hub ' + version, 'ProductVersion': version, 'ProductLanguage': '2052',
        'Manufacturer': 'BYBC', 'INSTALLLEVEL': '1', 'REBOOT': 'ReallySuppress',
        'MSIRESTARTMANAGERCONTROL': 'Disable', 'HUB_SHORTCUTS': '1',
        'SecureCustomProperties': 'INSTALLDIR;HUB_SHORTCUTS;HUB_OLDER_PRODUCTS;HUB_NEWER_PRODUCTS'}
    for key, value in properties.items(): add('Property', key, value)
    for row in [('TARGETDIR', None, 'SourceDir'), ('LocalAppDataFolder', 'TARGETDIR', '.'),
                ('HubPrograms', 'LocalAppDataFolder', 'Programs'), ('HubMsiRoot', 'HubPrograms', 'CodexHub-MSI'),
                ('INSTALLDIR', 'HubMsiRoot', version), ('ProgramMenuFolder', 'TARGETDIR', '.')]:
        add('Directory', *row)
    directories = {'.': 'INSTALLDIR'}
    for row in plan['files']:
        parent = PurePosixPath(row['path']).parent
        for directory in reversed([parent, *parent.parents]):
            name = directory.as_posix()
            if name in directories: continue
            directory_id = identity('D', name)
            directories[name] = directory_id
            add('Directory', directory_id, directories[directory.parent.as_posix()], short_long(directory.name, name))
    for sequence, row in enumerate(plan['files'], 1):
        name = row['path']
        file_id, component_id = identity('F', name), identity('C', name)
        component_guid = guid(plan['root_identity'] + name.casefold())
        row.update(file_id=file_id, component_id=component_id, component_guid=component_guid)
        add('Component', component_id, component_guid, directories[PurePosixPath(name).parent.as_posix()], 256, None, file_id)
        add('File', file_id, component_id, short_long(PurePosixPath(name).name, name), row['bytes'], None, None, 16896, sequence)
        add('FeatureComponents', 'Complete', component_id)
    add('Feature', 'Complete', None, 'BYBC Hub', None, 1, 1, 'INSTALLDIR', 0)
    add('Media', 1, len(plan['files']), None, '#payload.cab', None, None)
    main = next(row for row in plan['files'] if row['path'] == 'CodexBridge.exe')
    add('Registry', 'InstallDirectoryRecord', 1, registry_key, 'InstallDir', '[INSTALLDIR]', main['component_id'])
    add('RegLocator', 'PreviousInstallDirectory', 1, registry_key, 'InstallDir', 18)
    add('AppSearch', 'INSTALLDIR', 'PreviousInstallDirectory')
    # MSI reports VersionNT=603 even on Windows 10/11. Read the actual system
    # build using standard AppSearch; the native launcher also checks the OS.
    add('RegLocator', 'WindowsBuild', 2, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion', 'CurrentBuildNumber', 18)
    add('AppSearch', 'HUB_WINDOWS_BUILD', 'WindowsBuild')
    add('Component', 'ShortcutComponent', guid(plan['root_identity'] + 'shortcut'), 'ProgramMenuFolder', 260,
        'HUB_SHORTCUTS=1', 'ShortcutRecord')
    add('Registry', 'ShortcutRecord', 1, registry_key, 'ShortcutInstalled', '#1', 'ShortcutComponent')
    add('FeatureComponents', 'Complete', 'ShortcutComponent')
    add('Shortcut', 'HubShortcut', 'ProgramMenuFolder', short_long('BYBC Hub ' + version, 'shortcut-' + version),
        'ShortcutComponent', '[#' + main['file_id'] + ']', None, 'BYBC Hub', None, None, None, 1, 'INSTALLDIR')
    for relative, directory_id in directories.items():
        owner = next(row for row in plan['files'] if relative == '.' or row['path'].startswith(relative + '/'))
        add('RemoveFile', identity('R', relative), owner['component_id'], None, directory_id, 2)
    add('Upgrade', plan['upgrade_code'], None, version, None, 0, 'ALL', 'HUB_OLDER_PRODUCTS')
    add('Upgrade', plan['upgrade_code'], version, None, None, 258, None, 'HUB_NEWER_PRODUCTS')
    add('LaunchCondition', 'NOT ALLUSERS', 'This package installs only for the current user.')
    add('LaunchCondition', 'VersionNT64 AND HUB_WINDOWS_BUILD >= 17134', 'Windows 10 1803 or newer x64 is required.')
    add('LaunchCondition', 'Installed OR NOT HUB_NEWER_PRODUCTS', 'A newer BYBC Hub MSI version is already installed.')
    actions = [('FindRelatedProducts', 25), ('AppSearch', 50), ('LaunchConditions', 100),
        ('CostInitialize', 800), ('FileCost', 900), ('CostFinalize', 1000), ('InstallValidate', 1400),
        ('InstallInitialize', 1500), ('ProcessComponents', 1600), ('UnpublishFeatures', 1800),
        ('RemoveRegistryValues', 2600), ('RemoveShortcuts', 3200), ('RemoveFiles', 3500),
        ('InstallFiles', 4000), ('CreateShortcuts', 4500), ('WriteRegistryValues', 5000),
        ('RegisterUser', 6000), ('RegisterProduct', 6100), ('PublishFeatures', 6300), ('PublishProduct', 6400),
        ('InstallExecute', 6500), ('RemoveExistingProducts', 6550), ('InstallFinalize', 6600)]
    for action, sequence in actions: add('InstallExecuteSequence', action, None, sequence)
    for action, sequence in [('FindRelatedProducts', 25), ('AppSearch', 50), ('LaunchConditions', 100),
                             ('CostInitialize', 800), ('FileCost', 900), ('CostFinalize', 1000),
                             ('ExecuteAction', 1300)]:
        add('InstallUISequence', action, None, sequence)
    aliases = set()
    for directory, parent, name in rows['Directory']:
        if parent is None or name == '.': continue
        key = (parent, name.split('|')[0].casefold())
        if key in aliases: raise ValueError('Colliding MSI short filename')
        aliases.add(key)
    component_directories = {row[0]: row[2] for row in rows['Component']}
    for row in rows['File']:
        key = (component_directories[row[1]], row[2].split('|')[0].casefold())
        if key in aliases: raise ValueError('Colliding MSI short filename')
        aliases.add(key)
    return rows


class NativeMsi:
    """Only database/metadata APIs; deliberately exposes no install/uninstall API."""
    def __init__(self):
        if os.name != 'nt': raise RuntimeError('MSI construction requires Windows')
        system = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetSystemDirectoryW(system, len(system)): raise ctypes.WinError()
        self.system_directory = Path(system.value)
        self.api = ctypes.WinDLL(str(self.system_directory / 'msi.dll'))
        U, W, P = ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_void_p
        signatures = {
            'MsiOpenDatabaseW': [W, P, ctypes.POINTER(U)], 'MsiDatabaseOpenViewW': [U, W, ctypes.POINTER(U)],
            'MsiViewExecute': [U, U], 'MsiViewFetch': [U, ctypes.POINTER(U)], 'MsiViewClose': [U],
            'MsiCloseHandle': [U], 'MsiCreateRecord': [U], 'MsiRecordSetStringW': [U, U, W],
            'MsiRecordSetInteger': [U, U, ctypes.c_int], 'MsiRecordSetStreamW': [U, U, W],
            'MsiRecordGetStringW': [U, U, W, ctypes.POINTER(U)], 'MsiRecordGetFieldCount': [U],
            'MsiDatabaseCommit': [U], 'MsiDatabaseImportW': [U, W, W],
            'MsiGetSummaryInformationW': [U, W, U, ctypes.POINTER(U)],
            'MsiSummaryInfoSetPropertyW': [U, U, U, ctypes.c_int, P, W],
            'MsiSummaryInfoPersist': [U], 'MsiGetFileVersionW': [W, W, ctypes.POINTER(U), W, ctypes.POINTER(U)],
            'MsiGetFileHashW': [W, U, P],
        }
        for name, arguments in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, U

    @staticmethod
    def check(result, operation):
        if result: raise OSError(result, 'Windows Installer database operation failed: ' + operation)

    @contextmanager
    def database(self, path, create=False):
        handle = ctypes.c_uint()
        self.check(self.api.MsiOpenDatabaseW(str(path), ctypes.c_void_p(3 if create else 0), ctypes.byref(handle)), 'open')
        try: yield handle.value
        finally: self.api.MsiCloseHandle(handle)

    def execute(self, database, sql, values=(), stream=False):
        view = ctypes.c_uint()
        self.check(self.api.MsiDatabaseOpenViewW(database, sql, ctypes.byref(view)), sql)
        record = self.api.MsiCreateRecord(len(values)) if values else 0
        try:
            for index, value in enumerate(values, 1):
                if value is None: continue
                if stream and index == len(values): result = self.api.MsiRecordSetStreamW(record, index, str(value))
                elif isinstance(value, int): result = self.api.MsiRecordSetInteger(record, index, value)
                else: result = self.api.MsiRecordSetStringW(record, index, str(value))
                self.check(result, 'record field')
            self.check(self.api.MsiViewExecute(view, record), sql)
        finally:
            if record: self.api.MsiCloseHandle(record)
            self.api.MsiViewClose(view)
            self.api.MsiCloseHandle(view)

    def select(self, database, sql):
        view = ctypes.c_uint()
        self.check(self.api.MsiDatabaseOpenViewW(database, sql, ctypes.byref(view)), sql)
        rows = []
        try:
            self.check(self.api.MsiViewExecute(view, 0), sql)
            while True:
                record = ctypes.c_uint()
                result = self.api.MsiViewFetch(view, ctypes.byref(record))
                if result == 259: break
                self.check(result, 'fetch')
                try:
                    row = []
                    for index in range(1, self.api.MsiRecordGetFieldCount(record) + 1):
                        length = ctypes.c_uint(0)
                        result = self.api.MsiRecordGetStringW(record, index, None, ctypes.byref(length))
                        if result not in (0, 234): self.check(result, 'field length')
                        buffer = ctypes.create_unicode_buffer(length.value + 1)
                        length.value += 1
                        self.check(self.api.MsiRecordGetStringW(record, index, buffer, ctypes.byref(length)), 'field')
                        row.append(buffer.value)
                    rows.append(row)
                finally: self.api.MsiCloseHandle(record)
        finally:
            self.api.MsiViewClose(view)
            self.api.MsiCloseHandle(view)
        return rows

    def version_and_hash(self, path):
        version, language = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        vs, ls = ctypes.c_uint(256), ctypes.c_uint(256)
        result = self.api.MsiGetFileVersionW(str(path), version, ctypes.byref(vs), language, ctypes.byref(ls))
        if result == 0 and version.value: return version.value, language.value or None, None
        if result not in (0, 1006): self.check(result, 'file version')
        class FileHash(ctypes.Structure):
            _fields_ = [('size', ctypes.c_uint), ('parts', ctypes.c_uint * 4)]
        info = FileHash()
        info.size = ctypes.sizeof(info)
        self.check(self.api.MsiGetFileHashW(str(path), 0, ctypes.byref(info)), 'file hash')
        return None, None, [ctypes.c_int(value).value for value in info.parts]


def build_release(payload, output, *, work_directory, version=None):
    plan = public_plan(payload, version=version)
    output, work = Path(output).absolute(), Path(work_directory).absolute()
    reject_reparse(output.parent)
    reject_reparse(work.parent)
    source = Path(plan['payload'])
    if output.exists() or work.exists() or work.is_relative_to(source) or output.is_relative_to(source):
        raise ValueError('Use new build outputs outside the public payload')
    native = NativeMsi()
    work.mkdir(parents=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    proof = {key: plan[key] for key in ('product_code', 'product_version', 'upgrade_code', 'default_relative_install_dir', 'components')}
    proof.update(schema_version=1, packaging='per-user-msi',
                 files=[{key: row[key] for key in ('path', 'bytes', 'sha256')} for row in plan['files']],
                 files_scope='Payload files excluding this independently hashed immutable proof file.',
                 user_data='APPDATA and application-created runtime.json are outside MSI ownership.')
    proof_file = work / PROOF_PATH
    proof_file.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    plan['files'].append({'path': PROOF_PATH, 'source': str(proof_file), 'bytes': proof_file.stat().st_size, 'sha256': digest(proof_file)})
    plan['files'].sort(key=lambda row: row['path'])
    rows = table_plan(plan)
    for row, file_record in zip(plan['files'], rows['File']):
        version_value, language, file_hash = native.version_and_hash(row['source'])
        file_record[4:6] = [version_value, language]
        if file_hash: rows['MsiFileHash'].append([row['file_id'], 0, *file_hash])
    ddf = ['.OPTION EXPLICIT', '.Set CabinetNameTemplate=payload.cab',
        '.Set DiskDirectoryTemplate="' + str(work) + '"', '.Set CompressionType=MSZIP',
        '.Set Cabinet=on', '.Set Compress=on', '.Set MaxDiskSize=0',
        '.Set RptFileName="' + str(work / 'makecab.rpt') + '"',
        '.Set InfFileName="' + str(work / 'makecab.inf') + '"']
    ddf += ['"' + row['source'] + '" ' + row['file_id'] for row in plan['files']]
    encoding = 'cp' + str(ctypes.windll.kernel32.GetACP())
    (work / 'payload.ddf').write_bytes(('\r\n'.join(ddf) + '\r\n').encode(encoding, errors='strict'))
    result = subprocess.run([str(native.system_directory / 'makecab.exe'), '/F', str(work / 'payload.ddf')],
        cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
    (work / 'makecab.log').write_bytes(result.stdout)
    if result.returncode != 0 or not (work / 'payload.cab').is_file(): raise RuntimeError('System cabinet build failed')
    if any(digest(row['source']) != row['sha256'] for row in plan['files']): raise ValueError('Public input changed during cabinet build')
    (work / '_ForceCodepage.idt').write_bytes(b'\r\n\r\n936\t_ForceCodepage\r\n')
    package_code = '{' + str(uuid.uuid4()).upper() + '}'
    with native.database(output, create=True) as database:
        native.check(native.api.MsiDatabaseImportW(database, str(work), '_ForceCodepage.idt'), 'code page')
        for table, schema in SCHEMAS.items():
            native.execute(database, 'CREATE TABLE `' + table + '` (' + schema + ')')
            columns = re.findall(r'`([^`]+)`\s+(?:CHAR|SHORT|LONG)', schema)
            sql = 'INSERT INTO `' + table + '` (' + ','.join('`' + c + '`' for c in columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
            for values in rows[table]: native.execute(database, sql, values)
        native.execute(database, 'INSERT INTO `_Streams` (`Name`, `Data`) VALUES (?, ?)', ('payload.cab', work / 'payload.cab'), stream=True)
        summary = ctypes.c_uint()
        native.check(native.api.MsiGetSummaryInformationW(database, None, 20, ctypes.byref(summary)), 'summary')
        try:
            values = {1: 936, 2: 'Installation Database', 3: 'BYBC Hub ' + plan['product_version'], 4: 'BYBC',
                      7: 'x64;2052', 9: package_code, 14: 500, 15: 10}
            for key, value in values.items():
                native.check(native.api.MsiSummaryInfoSetPropertyW(summary, key, (2 if key == 1 else 3) if isinstance(value, int) else 30,
                    value if isinstance(value, int) else 0, None, None if isinstance(value, int) else value), 'summary property')
            native.check(native.api.MsiSummaryInfoPersist(summary), 'persist summary')
        finally: native.api.MsiCloseHandle(summary)
        native.check(native.api.MsiDatabaseCommit(database), 'commit')
    with native.database(output) as database:
        actual = {table: native.select(database, 'SELECT * FROM `' + table + '`') for table in SCHEMAS}
    for table, expected in rows.items():
        normalized = [[str(value) if value is not None else '' for value in row] for row in expected]
        if sorted(actual[table]) != sorted(normalized): raise ValueError('MSI table readback differs: ' + table)
    marker = next(row for row in plan['files'] if row['path'] == PROOF_PATH)
    report = {key: plan[key] for key in ('product_code', 'product_version', 'upgrade_code', 'default_relative_install_dir', 'install_dir_property', 'source_manifest_sha256', 'excluded')}
    report.update(package_code=package_code, msi=str(output), msi_sha256=digest(output), msi_bytes=output.stat().st_size,
        proof_relative_path=PROOF_PATH, proof_sha256=marker['sha256'], proof_component_code=marker['component_guid'],
        entrypoints=[{key: row[key] for key in ('path', 'sha256')} for row in plan['files'] if row['path'] in ENTRYPOINTS],
        file_count=len(plan['files']), component_count=len(rows['Component']), table_counts={key: len(value) for key, value in actual.items()},
        per_user_only=True, no_custom_actions=True, no_auto_start=True, no_legacy_cleanup=True,
        shortcut_property='HUB_SHORTCUTS', source_unchanged=True, database_codepage=936, summary_word_count=10,
        files=[{key: row[key] for key in ('path', 'bytes', 'sha256', 'file_id', 'component_guid')} for row in plan['files']])
    # The launcher registers this stable full-package source by installing
    # directly from it. It is not MSI-owned and must survive this transaction,
    # maintenance and uninstall; never delete the source in RemoveFiles.
    report.update(maintenance_source_contract(plan['product_code'], package_code))
    output.with_suffix('.build.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (work / 'database-readback.json').write_text(json.dumps(actual, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--work-directory', type=Path, required=True)
    arguments = parser.parse_args()
    result = build_release(arguments.payload, arguments.output, work_directory=arguments.work_directory)
    print(json.dumps({key: result[key] for key in ('msi', 'msi_sha256', 'msi_bytes', 'product_code', 'product_version', 'file_count')}, indent=2))


if __name__ == '__main__':
    main()
