#!/bin/bash
# Build freeflo-helper (the Swift mic + on-device transcription helper).
#
#   scripts/build_helper.sh            optimized arm64 build -> build/freeflo-helper
#   scripts/build_helper.sh --debug    unoptimized build with debug info
#
# Needs only the Xcode Command Line Tools (swiftc + clang); no Xcode, no SwiftPM.
# The binary is ad-hoc signed. To sign for distribution instead:
#   CODESIGN_IDENTITY="Developer ID Application: ..." scripts/build_helper.sh
# which also enables the hardened runtime with helper/entitlements.plist.
#
# Exits non-zero on any failure.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/helper/Sources"
OUT_DIR="${FREEFLO_HELPER_OUT_DIR:-$ROOT/build}"
OBJ_DIR="$OUT_DIR/helper-obj"
OUT="$OUT_DIR/freeflo-helper"
MIN_MACOS="26.0"
TARGET="arm64-apple-macos$MIN_MACOS"

MODE="release"
if [ "${1:-}" = "--debug" ]; then MODE="debug"; fi

fail() { echo "build_helper: $*" >&2; exit 1; }

command -v xcrun >/dev/null 2>&1 || fail "xcrun not found; install the Xcode Command Line Tools (xcode-select --install)"
xcrun --find swiftc >/dev/null 2>&1 || fail "swiftc not found; install the Xcode Command Line Tools"
SDK="$(xcrun --sdk macosx --show-sdk-path)" || fail "no macOS SDK found"
[ -d "$SDK/System/Library/Frameworks/Speech.framework" ] || fail "SDK at $SDK has no Speech framework"
grep -q "SpeechAnalyzer" "$SDK/System/Library/Frameworks/Speech.framework/Modules/Speech.swiftmodule/"*macos.swiftinterface 2>/dev/null \
    || fail "SDK at $SDK predates SpeechAnalyzer (need the macOS 26 SDK or newer)"

mkdir -p "$OBJ_DIR"
rm -f "$OUT"

if [ "$MODE" = "debug" ]; then
    CLANG_OPT=(-O0 -g)
    SWIFT_OPT=(-Onone -g)
else
    CLANG_OPT=(-O2)
    SWIFT_OPT=(-O -whole-module-optimization)
fi

echo "build_helper: compiling ($MODE, $TARGET, SDK $(basename "$(readlink "$SDK" || echo "$SDK")"))"

# 1. Objective-C exception bridge.
xcrun clang -c -fobjc-arc "${CLANG_OPT[@]}" -target "$TARGET" -isysroot "$SDK" \
    -o "$OBJ_DIR/FFObjC.o" "$SRC/ObjC/FFObjC.m"

# 2. Swift sources, linked with the bridge and the embedded Info.plist.
#    Swift 5 language mode: the Speech/AVFoundation callbacks used here are
#    not yet annotated for Swift 6 strict concurrency.
xcrun swiftc "${SWIFT_OPT[@]}" -swift-version 5 -target "$TARGET" -sdk "$SDK" \
    -module-name FreefloHelper \
    -import-objc-header "$SRC/ObjC/FFObjC.h" \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$ROOT/helper/Info.plist" \
    -Xlinker -dead_strip \
    -o "$OUT" \
    "$SRC"/*.swift "$OBJ_DIR/FFObjC.o"

[ -x "$OUT" ] || fail "linker produced no binary at $OUT"

if [ "$MODE" = "release" ]; then
    xcrun strip -x "$OUT"
fi

# 3. Sign. Ad-hoc by default; Developer ID + hardened runtime when asked.
#    (codesign's "replacing existing signature" note is expected: the linker
#    already ad-hoc signs arm64 binaries. Its output is shown only on failure.)
SIGN_LOG="$OBJ_DIR/codesign.log"
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
    codesign --force --timestamp --options runtime \
        --entitlements "$ROOT/helper/entitlements.plist" \
        --identifier com.freeflo.helper \
        --sign "$CODESIGN_IDENTITY" "$OUT" >"$SIGN_LOG" 2>&1 || { cat "$SIGN_LOG" >&2; fail "codesign failed"; }
    SIGNED="signed: $CODESIGN_IDENTITY (hardened runtime)"
else
    codesign --force --sign - --identifier com.freeflo.helper "$OUT" >"$SIGN_LOG" 2>&1 \
        || { cat "$SIGN_LOG" >&2; fail "codesign failed"; }
    SIGNED="signed: ad-hoc"
fi
codesign --verify --strict "$OUT" || fail "signature verification failed"

# 4. Sanity checks: right architecture, and it runs.
ARCHS="$(xcrun lipo -archs "$OUT")"
[ "$ARCHS" = "arm64" ] || fail "expected an arm64 binary, got: $ARCHS"
VERSION="$("$OUT" --version)" || fail "built binary does not run"

SIZE_BYTES="$(stat -f %z "$OUT")"
SIZE_HUMAN="$(awk -v b="$SIZE_BYTES" 'BEGIN { printf "%.0f KB", b / 1024 }')"
echo "build_helper: $VERSION, $SIGNED"
echo "build_helper: $OUT ($SIZE_HUMAN, $SIZE_BYTES bytes, $ARCHS)"
