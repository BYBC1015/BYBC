#!/usr/bin/env bash
set -euo pipefail

IOS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_ROOT="${HUB_IOS_OUTPUT:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/hub-ios-verification}"
mkdir -p "$OUTPUT_ROOT"
xcodebuild -version | tee "$OUTPUT_ROOT/xcode-version.txt"
plutil -lint "$IOS_ROOT/Hub/Info.plist" "$IOS_ROOT/Hub/PrivacyInfo.xcprivacy" "$IOS_ROOT/Hub.xcodeproj/project.pbxproj"

# This produces a simulator app only. It does not sign, export, install on a
# real iPhone, create App Store records, or upload anything to TestFlight.
xcodebuild -project "$IOS_ROOT/Hub.xcodeproj" -scheme Hub -configuration Debug \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath "$OUTPUT_ROOT/DerivedData" CODE_SIGNING_ALLOWED=NO \
  build | tee "$OUTPUT_ROOT/build.log"

if [[ "${HUB_IOS_SKIP_TESTS:-0}" != "1" ]]; then
  DEVICE_ID="$(xcrun simctl list devices available -j | python3 -c '
import json, sys
devices = json.load(sys.stdin)["devices"]
for runtime, entries in sorted(devices.items(), reverse=True):
    if ".iOS-" not in runtime:
        continue
    for device in entries:
        if device.get("isAvailable") and device["name"].startswith("iPhone"):
            print(device["udid"])
            sys.exit(0)
sys.exit("No installed iPhone simulator runtime; install one in Xcode Settings > Platforms.")
')"
  RESULT_BUNDLE="$OUTPUT_ROOT/HubTests-$(date +%Y%m%d%H%M%S).xcresult"
  set +e
  xcodebuild -project "$IOS_ROOT/Hub.xcodeproj" -scheme Hub -configuration Debug \
    -sdk iphonesimulator -destination "platform=iOS Simulator,id=$DEVICE_ID" \
    -destination-timeout 90 -parallel-testing-enabled NO \
    -derivedDataPath "$OUTPUT_ROOT/DerivedData" \
    -resultBundlePath "$RESULT_BUNDLE" \
    CODE_SIGNING_ALLOWED=NO test | tee "$OUTPUT_ROOT/test.log"
  TEST_STATUS=${PIPESTATUS[0]}
  set -e
  if [[ -d "$RESULT_BUNDLE" ]]; then
    mkdir -p "$OUTPUT_ROOT/screenshots"
    xcrun xcresulttool export attachments --path "$RESULT_BUNDLE" \
      --output-path "$OUTPUT_ROOT/screenshots" \
      > "$OUTPUT_ROOT/screenshot-export.log" 2>&1 || true
  fi
  if [[ "$TEST_STATUS" != "0" ]]; then
    exit "$TEST_STATUS"
  fi
fi
printf 'Unsigned simulator verification artifacts: %s\n' "$OUTPUT_ROOT"
