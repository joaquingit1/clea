#!/bin/zsh
# Builds the Clea a person downloads: Clea.app with everything it needs inside (the engine, its own Python, the built
# screen and the Touch ID helper) and a disk image of it. Nothing on the user's Mac is assumed beyond macOS, git and gh.
#
# usage: desktop/release.sh [out dir]        (default desktop/out; Apple Silicon only)
#
# The app is signed ad hoc: it runs, but macOS asks the person to allow it the first time (docs/setup.md says how).
# A download that opens with no question needs a Developer ID certificate and Apple's notarization, which this script
# does not do yet.
set -euo pipefail
ROOT=${0:A:h:h}
DESK=$ROOT/desktop
OUT=${1:-$DESK/out}
VERSION=$(node -p "require('$DESK/package.json').version")

# the Python the app carries: a standalone CPython build (github.com/astral-sh/python-build-standalone), pinned by hash
PY_TAG=20261026
PY_VERSION=3.13.18
PY_SHA256=8392b6ea51d94391befb2c31ebe75463f045d0b69dfcf5e1bb3234e1f5fa3405
PY_FILE=cpython-$PY_VERSION+$PY_TAG-aarch64-apple-darwin-install_only_stripped.tar.gz
PY_URL=https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/${PY_FILE//+/%2B}

[[ $(uname -m) == arm64 ]] || { echo "release.sh builds on Apple Silicon only"; exit 1; }
say() { print -P "%B==> $1%b"; }
mkdir -p $OUT/cache

say "the app's screen (ui/dist)"
(cd $ROOT/ui && if command -v pnpm >/dev/null; then pnpm install --frozen-lockfile && pnpm build; else npm install && npm run build; fi) >/dev/null

say "the window (Electron)"
(cd $DESK && npm ci --no-audit --no-fund >/dev/null && npx electron-packager . Clea --platform=darwin --arch=arm64 --out=$OUT --overwrite \
  --icon=build/icon.icns --app-bundle-id=com.joaquincastellano.clea --helper-bundle-id=com.joaquincastellano.clea.helper \
  --app-copyright="© 2026 Joaquin Castellano" --app-category-type=public.app-category.productivity \
  --ignore='^/out' --ignore='^/build' --ignore='^/tools' --ignore='^/release\.sh' --prune=true >/dev/null)
APP=$OUT/Clea-darwin-arm64/Clea.app
RES=$APP/Contents/Resources

say "the engine"
mkdir -p $RES/engine/ui $RES/engine/guard
rsync -a --exclude __pycache__ $ROOT/ap $ROOT/prompts $RES/engine/
rsync -a $ROOT/ui/dist $RES/engine/ui/
cp $ROOT/guard/ap_guard.py $RES/engine/guard/
cp $ROOT/LICENSE $ROOT/NOTICE $RES/ 2>/dev/null || true

say "Python $PY_VERSION"
[[ -f $OUT/cache/$PY_FILE ]] || curl -fsSL -o $OUT/cache/$PY_FILE $PY_URL
echo "$PY_SHA256  $OUT/cache/$PY_FILE" | shasum -a 256 -c - >/dev/null || { echo "the Python download does not match its hash"; exit 1; }
tar -xzf $OUT/cache/$PY_FILE -C $RES
PY=$RES/python/bin/python3
# what the engine imports beyond the standard library, as pyproject.toml names it
DEPS=("${(@f)$($PY -c "import tomllib,sys; print('\n'.join(tomllib.load(open(sys.argv[1],'rb'))['project']['dependencies']))" $ROOT/pyproject.toml)}")
$PY -m pip install --quiet --no-cache-dir --no-compile --disable-pip-version-check $DEPS
# not needed at run time: the installer, the test suite and the Tk interface
SITE=$RES/python/lib/python${PY_VERSION%.*}
rm -rf $SITE/site-packages/pip* $SITE/ensurepip $SITE/test $SITE/idlelib $SITE/tkinter $SITE/turtledemo $RES/python/lib/tcl* $RES/python/lib/tk* \
  $RES/python/lib/itcl* $RES/python/lib/thread* $RES/python/share $RES/python/include $RES/python/bin/pip* $RES/python/bin/idle* \
  $SITE/lib-dynload/_tkinter*
find $RES/python $RES/engine -name __pycache__ -type d -prune -exec rm -rf {} +

say "the Touch ID helper"
TMP=$(mktemp -d)
cp $ROOT/guard/ap_confirm.swift $ROOT/guard/build.sh $TMP/
CLEA_ICON=$DESK/build/icon.icns zsh $TMP/build.sh >/dev/null
mv $TMP/Clea.app $RES/engine/guard/Clea.app
rm -rf $TMP

say "signing (ad hoc)"
codesign --force --deep --sign - $APP 2>/dev/null
codesign --verify --deep --strict $APP

say "the disk image"
STAGE=$(mktemp -d)
ditto $APP $STAGE/Clea.app
ln -s /Applications $STAGE/Applications
DMG=$OUT/Clea-$VERSION-arm64.dmg
rm -f $DMG
hdiutil create -quiet -volname "Clea $VERSION" -srcfolder $STAGE -ov -format UDZO $DMG
rm -rf $STAGE
echo "$APP  ($(du -sh $APP | cut -f1))"
echo "$DMG  ($(du -h $DMG | cut -f1))"
shasum -a 256 $DMG
