#!/bin/zsh
# Builds the guard's confirmation helper: guard/Clea.app, a tiny bundle whose only job is the Touch ID dialog the
# guard opens before anything goes out in the user's name. A bundle (not a bare binary) so the dialog shows Clea's
# name and icon. Needs the Xcode command line tools (swiftc). Usage: guard/build.sh [signing identity]
set -euo pipefail
cd "$(dirname "$0")"
APP=Clea.app
rm -rf $APP && mkdir -p $APP/Contents/MacOS $APP/Contents/Resources
swiftc -O -o $APP/Contents/MacOS/Clea ap_confirm.swift
ICON=${CLEA_ICON:-../desktop/build/icon.icns}
[ -f "$ICON" ] && cp "$ICON" $APP/Contents/Resources/Clea.icns
cat > $APP/Contents/Info.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Clea</string>
  <key>CFBundleDisplayName</key><string>Clea</string>
  <key>CFBundleExecutable</key><string>Clea</string>
  <key>CFBundleIdentifier</key><string>com.joaquincastellano.clea.confirm</string>
  <key>CFBundleIconFile</key><string>Clea</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHumanReadableCopyright</key><string>© 2026 Joaquin Castellano</string>
</dict></plist>
PLIST
codesign --force --sign "${1:--}" $APP
echo "built $(pwd)/$APP"
