#!/bin/zsh
# Build Astro Rig Monitor for Mac: a menu-bar app with its own Python and the monitor inside.
#
#     mac/build.sh              → both apps
#     mac/build.sh arm64        → Apple Silicon only
#     mac/build.sh x86_64       → Intel only (on Apple Silicon this needs Rosetta for pip)
#
# Results in mac/build/:
#     Apple Silicon/Astro Rig Monitor.app   and   Astro-Rig-Monitor-Apple-Silicon.zip
#     Intel/Astro Rig Monitor.app           and   Astro-Rig-Monitor-Intel.zip
#
# Needs Apple's command line tools (xcode-select --install) and an internet connection the
# first time (the Python build and packages are cached in ~/Library/Caches/astro-rig-monitor-build).
#
# Signing: set SIGN_IDENTITY to a code-signing certificate in your keychain so macOS sees each
# new build as the same app (the background item and permissions survive updates). Without it
# the app gets an ad-hoc signature, which works but macOS may ask again after each update.
set -eu
cd "$(dirname "$0")"
here=$PWD
repo=${here:h}

# Python from python-build-standalone (astral-sh), pinned by checksum.
PBS_TAG=20261110
PBS_PY=3.12.16
typeset -A PBS_SHA
PBS_SHA[arm64]=e956e60bc9465d577ef8062059aa05cd3a2665b4b8678a2ed24e432368330b8f
PBS_SHA[x86_64]=83f2922437e6d546d0fcdb8c11f150a8e52787e8d9ea8651b1974e20908eb1bc
typeset -A PBS_ARCH; PBS_ARCH[arm64]=aarch64; PBS_ARCH[x86_64]=x86_64
typeset -A LABEL; LABEL[arm64]="Apple Silicon"; LABEL[x86_64]=Intel

app_version=$(<VERSION)
arm_version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' $repo/arm/__init__.py)
build_no=$(git -C $repo rev-list --count HEAD 2>/dev/null || echo 1)
sign_id=${SIGN_IDENTITY:--}
cache=$HOME/Library/Caches/astro-rig-monitor-build
mkdir -p $cache build
echo "Astro Rig Monitor $app_version (monitor $arm_version), signing with: ${sign_id/#-/ad-hoc signature}"

[[ -f Resources/AppIcon.icns ]] || swift tools/make_icon.swift

build_one() {
  local arch=$1 label=${LABEL[$1]}
  local run=(); [[ $arch == x86_64 && $(uname -m) == arm64 ]] && run=(arch -x86_64)
  local name="cpython-$PBS_PY+$PBS_TAG-${PBS_ARCH[$arch]}-apple-darwin-install_only.tar.gz"
  local tarball=$cache/$name
  if [[ ! -f $tarball ]]; then
    echo "downloading $name"
    curl -fL --retry 3 -o $tarball.part "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/${name//+/%2B}"
    mv $tarball.part $tarball
  fi
  echo "${PBS_SHA[$arch]}  $tarball" | shasum -a 256 -c --quiet - || { echo "checksum mismatch: $tarball"; rm -f $tarball; exit 1; }

  # Assemble outside synced folders (iCloud adds attributes that codesign refuses).
  local work=${TMPDIR:-/tmp}/astro-rig-monitor-mac-$arch
  rm -rf $work; mkdir -p $work
  local app="$work/Astro Rig Monitor.app"
  local res="$app/Contents/Resources"
  mkdir -p "$app/Contents/MacOS" "$res"

  echo "[$label] Python $PBS_PY and packages"
  tar -xzf $tarball -C "$res"                      # → Resources/python
  local py="$res/python/bin/python3"
  $run "$py" -m pip install -q --disable-pip-version-check --no-warn-script-location --only-binary=:all: \
      --cache-dir $cache/pip-$arch -r $repo/requirements.txt
  # Leave out what the monitor never uses.
  local lib="$res/python/lib/python${PBS_PY%.*}"
  setopt local_options null_glob
  rm -rf $lib/test $lib/idlelib $lib/tkinter $lib/turtledemo $lib/ensurepip $lib/lib2to3 \
         $lib/site-packages/pip $lib/site-packages/pip-* $res/python/lib/tcl* $res/python/lib/tk* \
         $res/python/lib/libtcl* $res/python/lib/itcl* $res/python/lib/thread* $res/python/lib/pkgconfig \
         $res/python/include $res/python/share $lib/config-*/*.a \
         $lib/lib-dynload/_tkinter*.so $res/python/bin/{idle3*,pip*,pydoc3*,2to3*,python3*-config}
  rm -rf $lib/site-packages/PIL/_imagingtk*.so
  find "$res/python" -name __pycache__ -type d -prune -exec rm -rf {} +

  echo "[$label] the monitor"
  rsync -a --exclude __pycache__ $repo/arm "$res/"
  cp $repo/LICENSE $repo/config.example.toml "$res/"
  $run "$py" -m compileall -q -j 0 "$res/arm" "$lib" >/dev/null || true   # no writes needed at run time

  echo "[$label] the app"
  local target=(-target $arch-apple-macos13)
  swiftc -O $target -parse-as-library Sources/App/*.swift -o "$app/Contents/MacOS/Astro Rig Monitor"
  swiftc -O $target Sources/Launcher/main.swift -o "$app/Contents/MacOS/arm-launcher"
  sed -e "s/@APP_VERSION@/$app_version/" -e "s/@ARM_VERSION@/$arm_version/" -e "s/@BUILD@/$build_no/" \
      Resources/Info.plist > "$app/Contents/Info.plist"
  cp Resources/AppIcon.icns "$res/"

  echo "[$label] signing"
  xattr -cr "$app"
  local opts=(--force --timestamp=none --sign "$sign_id")
  [[ $sign_id != - ]] && opts+=(--options runtime)
  # Inner code first: every library and program in the Python, then the two programs, then the app.
  find "$res/python" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0 |
    while IFS= read -r -d '' f; do
      file -b "$f" | grep -q Mach-O && codesign $opts --entitlements Resources/python.entitlements "$f" 2>/dev/null
    done
  codesign $opts --entitlements Resources/launcher.entitlements "$app/Contents/MacOS/arm-launcher" 2>/dev/null
  codesign $opts "$app" 2>/dev/null
  codesign --verify --deep --strict "$app"

  local out="build/$label"
  rm -rf "$out"; mkdir -p "$out"
  ditto "$app" "$out/Astro Rig Monitor.app"
  local zip="build/Astro-Rig-Monitor-${label// /-}.zip"
  rm -f "$zip"
  ditto -c -k --keepParent "$app" "$zip"
  echo "[$label] built $out/Astro Rig Monitor.app and $zip ($(du -sh "$zip" | cut -f1))"
}

if (( $# )); then archs=($@); else archs=(arm64 x86_64); fi
for a in $archs; do
  [[ -n ${LABEL[$a]:-} ]] || { echo "unknown architecture $a (use arm64 or x86_64)"; exit 2; }
  build_one $a
done
