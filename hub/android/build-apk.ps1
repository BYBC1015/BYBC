param(
    [switch]$Clean,
    [switch]$Debug
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$JavaHome = Join-Path $RepoRoot ".local-tools\jdk-17.0.19+10"
$AndroidSdkRoot = Join-Path $RepoRoot ".local-tools\android-sdk"
$GradleBin = Join-Path $RepoRoot ".local-tools\gradle-8.10.2\bin"
$Variant = if ($Debug) { "debug" } else { "release" }
$ApkSource = Join-Path $PSScriptRoot "app\build\outputs\apk\$Variant\app-$Variant.apk"
$GradleConfig = Join-Path $PSScriptRoot "app\build.gradle"
$VersionMatch = Select-String -LiteralPath $GradleConfig -Pattern 'versionName\s+"([^"]+)"'
if (!$VersionMatch -or !$VersionMatch.Matches[0].Groups[1].Value) {
    throw "Unable to read versionName from: $GradleConfig"
}
$VersionName = $VersionMatch.Matches[0].Groups[1].Value
$ApkDist = Join-Path $PSScriptRoot "dist\CodexHub-v$VersionName-$Variant.apk"

if (!(Test-Path $JavaHome)) {
    throw "JDK not found: $JavaHome"
}
if (!(Test-Path $AndroidSdkRoot)) {
    throw "Android SDK not found: $AndroidSdkRoot"
}
if (!(Test-Path $GradleBin)) {
    throw "Gradle not found: $GradleBin"
}

$env:JAVA_HOME = $JavaHome
$env:ANDROID_HOME = $AndroidSdkRoot
$env:ANDROID_SDK_ROOT = $AndroidSdkRoot
$env:PATH = "$JavaHome\bin;$GradleBin;$AndroidSdkRoot\cmdline-tools\latest\bin;$AndroidSdkRoot\platform-tools;$env:PATH"

$BuildTask = if ($Debug) { "assembleDebug" } else { "assembleRelease" }
[string[]]$GradleTasks = if ($Clean) { @("clean", $BuildTask) } else { @($BuildTask) }
# Keep JDK 17 local sockets out of the system TEMP directory on Windows.
$PreviousJavaToolOptions = $env:JAVA_TOOL_OPTIONS
$SocketTemp = Join-Path $RepoRoot "tmp"
New-Item -ItemType Directory -Force -Path $SocketTemp | Out-Null
try {
    $env:JAVA_TOOL_OPTIONS = "$PreviousJavaToolOptions `"-Djdk.net.unixdomain.tmpdir=$SocketTemp`""
    gradle -p $PSScriptRoot @GradleTasks --no-daemon
    if ($LASTEXITCODE -ne 0) {
        throw "Android build failed with exit code $LASTEXITCODE; APK was not published."
    }
} finally {
    $env:JAVA_TOOL_OPTIONS = $PreviousJavaToolOptions
}
if (!(Test-Path -LiteralPath $ApkSource)) {
    throw "Android build did not produce an APK: $ApkSource"
}

New-Item -ItemType Directory -Force -Path (Split-Path $ApkDist) | Out-Null
Copy-Item -LiteralPath $ApkSource -Destination $ApkDist -Force

Write-Host "APK ready: $ApkDist"
