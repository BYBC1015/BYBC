"""Executable byte-boundary checks and the exported Android share contract."""

import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[2]
JAVA_ROOT = ROOT / "codex-hub/android/app/src/main/java/xyz/baicang/codexhub"
MANIFEST = ROOT / "codex-hub/android/app/src/main/AndroidManifest.xml"
ANDROID = "{http://schemas.android.com/apk/res/android}"


def test_share_receiver_only_opens_content_and_has_no_public_cache_or_storage_access():
    manifest = ET.parse(MANIFEST).getroot()
    permissions = {item.attrib[ANDROID + "name"] for item in manifest.findall("uses-permission")}
    assert not any("STORAGE" in permission or "READ_MEDIA" in permission for permission in permissions)
    activity = manifest.find("application/activity")
    assert activity.attrib[ANDROID + "launchMode"] == "singleTask"
    filters = activity.findall("intent-filter")
    actions = {action.attrib[ANDROID + "name"] for f in filters for action in f.findall("action")}
    assert "android.intent.action.SEND" in actions
    assert "android.intent.action.SEND_MULTIPLE" in actions
    view = next(f for f in filters if any(
        item.attrib[ANDROID + "name"] == "android.intent.action.VIEW" for item in f.findall("action")
    ))
    assert all(item.attrib.get(ANDROID + "scheme") == "content" for item in view.findall("data"))
    provider = manifest.find("application/provider")
    assert provider.attrib[ANDROID + "exported"] == "false"
    assert provider.attrib[ANDROID + "grantUriPermissions"] == "false"


def test_native_import_stays_local_until_the_existing_file_chooser_is_used():
    activity = (JAVA_ROOT / "MainActivity.java").read_text(encoding="utf-8")
    receiver = activity.split("private boolean receiveSharedFiles(Intent intent)", 1)[1].split(
        "private void showImportMessage", 1
    )[0]
    assert "checkUriPermission" in receiver
    assert "Intent.FLAG_GRANT_READ_URI_PERMISSION" in receiver
    assert "sharedAttachmentImporter.isExternalContent(uri)" in receiver
    assert "pendingSharedFiles.addAll(copied)" in receiver
    assert "completeFileSelection" not in receiver
    assert "loadPrimaryUrl" not in receiver
    assert "evaluateJavascript" not in receiver
    assert "submit" not in receiver
    assert "consumed.removeAll(pendingSharedFiles)" in activity
    assert "sharedAttachmentImporter.removeUnreferenced(importedSharedFiles)" in activity
    assert "if (receiveSharedFiles(intent)) return;" in activity
    assert "selectedGeneration == filePageGeneration" in activity
    assert "pendingFileGeneration == filePageGeneration" in activity
    assert activity.count("returnSharedFilesToQueue();") >= 2


def test_import_stream_limits_names_mime_and_interruption_are_executable(tmp_path):
    suffix = ".exe" if os.name == "nt" else ""
    local_jdk = ROOT / ".local-tools/jdk-17.0.19+10/bin"
    javac = str(local_jdk / f"javac{suffix}") if local_jdk.exists() else shutil.which("javac")
    java = str(local_jdk / f"java{suffix}") if local_jdk.exists() else shutil.which("java")
    if not javac or not java:
        pytest.skip("A JDK is required for the native copy-policy executable checks")
    harness = tmp_path / "SharedFilePolicyCheck.java"
    harness.write_text(r'''
package xyz.baicang.codexhub;
import java.io.*;
public final class SharedFilePolicyCheck {
    static void check(boolean condition) { if (!condition) throw new AssertionError(); }
    static final class Source extends InputStream {
        long left;
        Source(long size) { left = size; }
        public int read() { return left-- > 0 ? 1 : -1; }
        public int read(byte[] bytes, int offset, int length) {
            if (left <= 0) return -1;
            int count = (int) Math.min(left, length);
            left -= count;
            return count;
        }
    }
    static final class Sink extends OutputStream {
        long written;
        public void write(int value) { written++; }
        public void write(byte[] bytes, int offset, int length) { written += length; }
    }
    static void bounded(long size, long remaining, long limit, boolean succeeds) throws Exception {
        Sink sink = new Sink();
        boolean complete = false;
        try {
            check(SharedFilePolicy.copy(new Source(size), sink, remaining) == size);
            complete = true;
        } catch (IOException expected) { }
        check(complete == succeeds);
        check(sink.written <= limit);
        if (succeeds) check(sink.written == size);
    }
    public static void main(String[] args) throws Exception {
        bounded(200, 200, 200, true);
        bounded(201, 200, 200, false);
        bounded(0, 0, 0, true);
        bounded(1, 0, 0, false);
        bounded(SharedFilePolicy.MAX_FILE_BYTES, SharedFilePolicy.MAX_CACHE_BYTES,
            SharedFilePolicy.MAX_FILE_BYTES, true);
        bounded(SharedFilePolicy.MAX_FILE_BYTES + 1, SharedFilePolicy.MAX_CACHE_BYTES,
            SharedFilePolicy.MAX_FILE_BYTES, false);
        check(SharedFilePolicy.safeName("微信合同.pdf").equals("微信合同.pdf"));
        check(SharedFilePolicy.safeName("../合同\\附件\n.pdf").equals(".._合同_附件_.pdf"));
        check(SharedFilePolicy.safeName("..").equals("attachment"));
        String longName = "x".repeat(179) + "😀.pdf";
        check(SharedFilePolicy.safeName(longName).length() == 179);
        check(SharedFilePolicy.safeType(" Application/PDF ").equals("application/pdf"));
        check(SharedFilePolicy.safeType("text/html\r\nX-Test: yes").equals("application/octet-stream"));
        check(SharedFilePolicy.safeType(null).equals("application/octet-stream"));
        Thread.currentThread().interrupt();
        bounded(1, 200, 0, false);
        check(Thread.interrupted());
        System.out.println("shared-file-policy: passed");
    }
}
''', encoding="utf-8")
    compile_result = subprocess.run(
        [javac, "-encoding", "UTF-8", "-d", str(tmp_path), str(JAVA_ROOT / "SharedFilePolicy.java"), str(harness)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    result = subprocess.run(
        [java, "-cp", str(tmp_path), "xyz.baicang.codexhub.SharedFilePolicyCheck"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "shared-file-policy: passed" in result.stdout
