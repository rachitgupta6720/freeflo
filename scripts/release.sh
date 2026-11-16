#!/usr/bin/env bash
#
# release.sh — cut a new freeflo release in one command.
#
# Usage:
#   ./scripts/release.sh <version> ["release notes"]
#   ./scripts/release.sh 1.1.0
#   ./scripts/release.sh 1.1.0 "Adds Korean support and fixes the toggle race"
#
# What it does, in order:
#   1. Validates the version (X.Y.Z) and that it isn't already released.
#   2. Bumps the version in setup.py.
#   3. Rolls the CHANGELOG [Unreleased] section into a dated version section.
#   4. Builds the speech helper (Swift) and the .app bundle (py2app), runs a
#      scripted dictation through the built app, signs it (Developer ID +
#      notarization with FREEFLO_NOTARIZE=1, otherwise scripts/dev_sign.sh:
#      the FREEFLO_SIGN_IDENTITY certificate, or ad hoc), and packages
#      freeflo-macos26.zip.
#   5. Stamps the new version, download link + size into the landing page.
#   6. Commits everything, tags v<version>, and pushes (code + tag).
#   7. Publishes a GitHub Release with freeflo-macos26.zip attached.
#
# GitHub Pages redeploys the landing page automatically on push; its download
# button is stamped with this release's own download URL.
#
# WHY THE ZIP IS NOT CALLED freeflo.zip, AND WHY THE RELEASE IS NOT "LATEST"
# -------------------------------------------------------------------------
# This build needs macOS 26+ on Apple silicon. Every client up to 1.4.x (which
# ran on any macOS and on Intel) polls releases/latest and offers whatever is
# there as "freeflo.zip" to its user, with no check of what the build needs.
# Someone on macOS 14/15 would download it, drag it over their working app as
# instructed, and be left with an app that cannot open. So:
#   * the asset has its own name (ASSET below; engine/updater.py looks for the
#     same one), which a 1.4.x client can never download; and
#   * by default the release is published WITHOUT the "latest" flag, so
#     releases/latest stays on the last 1.4.x build and those clients are not
#     even told. New clients read the release list and find it anyway.
# Set FREEFLO_RELEASE_LATEST=1 to flag it latest after all: 1.4.x clients are
# then notified, and "Download update" opens the release page (there is no
# freeflo.zip to fetch), whose notes start with the requirement. Users who
# cannot upgrade keep getting that notification every 6 hours, which is why it
# is not the default.
#
set -euo pipefail

# --- locate repo root (script lives in scripts/) ---
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-}"
NOTES_ARG="${2:-}"
TAG="v${VERSION}"
DATE="$(date +%F)"
PAGE="docs/index.html"
REPO_URL="https://github.com/rachitgupta6720/freeflo"
# Download asset of this (macOS 26+) line. Kept in sync with _ASSET_NAME in
# engine/updater.py. NEVER "freeflo.zip": that name is what 1.4.x clients
# download (see the header).
ASSET="freeflo-macos26.zip"
# What the build needs (LSMinimumSystemVersion in setup.py, the helper's
# target in scripts/build_helper.sh) and the last release for everyone else.
MIN_MACOS="26.0"
ARCH="arm64"
LAST_LEGACY_TAG="v1.4.3"
PY="$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)"

die(){ echo "✗ $*" >&2; exit 1; }
step(){ echo; echo "▸ $*"; }

# --- 1. validate ---
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Version must look like X.Y.Z (got '${VERSION:-<none>}')."
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "Tag $TAG already exists."
gh release view "$TAG" >/dev/null 2>&1 && die "Release $TAG already exists on GitHub."
# The speech helper is compiled from helper/ with the Command Line Tools; it
# needs the macOS 26 SDK (SpeechAnalyzer). Checked here so a missing toolchain
# fails before anything is bumped or committed.
xcrun --find swiftc >/dev/null 2>&1 || die "swiftc not found — install the Xcode Command Line Tools (xcode-select --install)."
[ -x scripts/build_helper.sh ] || die "scripts/build_helper.sh is missing or not executable."
[ -x scripts/dev_sign.sh ] || die "scripts/dev_sign.sh is missing or not executable."
# Turbo mode's llama-server: $FREEFLO_LLAMA_SERVER, vendor/llama-server, or a
# static build at ~/llama.cpp (same search order as setup.py). A release
# without it would silently ship with Turbo unavailable, so insist.
LLAMA=""
for c in "${FREEFLO_LLAMA_SERVER:-}" "vendor/llama-server" "$HOME/llama.cpp/build-static/bin/llama-server"; do
    if [ -n "$c" ] && [ -x "$c" ]; then LLAMA="$c"; break; fi
done
[ -n "$LLAMA" ] || die "llama-server (static) not found — set FREEFLO_LLAMA_SERVER, put it at vendor/llama-server, or build llama.cpp with build-static (see README, Turbo mode)."

step "Releasing freeflo $TAG  ($DATE)"

# --- 2. bump setup.py ---
step "Bumping version in setup.py"
sed -i '' "s/'CFBundleVersion': '[^']*'/'CFBundleVersion': '$VERSION'/" setup.py
sed -i '' "s/'CFBundleShortVersionString': '[^']*'/'CFBundleShortVersionString': '$VERSION'/" setup.py

# --- 3. roll the changelog ---
step "Updating CHANGELOG.md"
awk -v ver="$VERSION" -v d="$DATE" '
  /^## \[Unreleased\]/ && !done { print; print ""; print "## [" ver "] - " d; done=1; next }
  { print }
' CHANGELOG.md > CHANGELOG.tmp && mv CHANGELOG.tmp CHANGELOG.md

# Extract this version'\''s notes for the GitHub release body.
NOTES="$(awk -v ver="$VERSION" '
  $0 ~ "^## \\[" ver "\\]" {grab=1; next}
  grab && /^## \[/ {exit}
  grab {print}
' CHANGELOG.md)"
[ -n "${NOTES_ARG}" ] && NOTES="${NOTES_ARG}

${NOTES}"
[ -z "${NOTES//[[:space:]]/}" ] && NOTES="freeflo $TAG"
# The requirement goes FIRST (a 1.4.x client shows only the first ~140
# characters of the notes in its notification), and the machine-readable
# marker goes last (engine/updater.py skips releases this Mac cannot run).
NOTES="Requires macOS ${MIN_MACOS%.0}+ on Apple silicon. On an older macOS or an Intel Mac do NOT install this: stay on freeflo ${LAST_LEGACY_TAG} (${REPO_URL}/releases/tag/${LAST_LEGACY_TAG}).

${NOTES}

<!-- freeflo-requires: macos=${MIN_MACOS} arch=${ARCH} -->"

# --- 4. build ---
step "Building freeflo.app (py2app) — this takes a minute"
# Bake in the Google OAuth client so the shipped app can offer backup — a
# double-clicked app has no shell env to read FREEFLO_GOOGLE_* from. The file
# is gitignored; setup.py bundles it into the app when present. Without the
# env vars the build still succeeds and backup simply stays unavailable.
# If the env vars are set they (re)generate the file; otherwise an existing
# google_client.json is kept as-is (never deleted — it may be the only copy of
# the credential, since it is gitignored).
if [ -n "${FREEFLO_GOOGLE_CLIENT_ID:-}" ] && [ -n "${FREEFLO_GOOGLE_CLIENT_SECRET:-}" ]; then
    printf '{"client_id":"%s","client_secret":"%s"}' \
        "$FREEFLO_GOOGLE_CLIENT_ID" "$FREEFLO_GOOGLE_CLIENT_SECRET" > google_client.json
    echo "  baked in Google OAuth client from FREEFLO_GOOGLE_* (backup enabled)"
elif [ -f google_client.json ]; then
    echo "  using existing google_client.json (backup enabled in this build)"
else
    echo "  no google_client.json and no FREEFLO_GOOGLE_* — building without backup"
fi
rm -rf build dist
# The helper goes into build/ (py2app's scratch dir too), so it is built AFTER
# the clean and BEFORE py2app, which copies it into Contents/Resources.
# With FREEFLO_NOTARIZE=1 it is signed with the Developer ID + hardened
# runtime here; otherwise ad-hoc.
if [ "${FREEFLO_NOTARIZE:-0}" = "1" ]; then
    : "${FREEFLO_SIGN_IDENTITY:?set FREEFLO_SIGN_IDENTITY to notarize}"
    CODESIGN_IDENTITY="$FREEFLO_SIGN_IDENTITY" scripts/build_helper.sh
else
    scripts/build_helper.sh
fi
"$PY" setup.py py2app >/dev/null
[ -d dist/freeflo.app ] || die "Build did not produce dist/freeflo.app."
[ -x dist/freeflo.app/Contents/Resources/freeflo-helper ] || die "The bundle has no freeflo-helper."

# Smoke test the bundle that is about to ship: the bundled helper must
# transcribe a clip (synthesised with `say`, so no microphone and no
# permission is involved), driven through the real app in self-test mode
# against a throwaway data folder.
step "Smoke-testing the built app"
SMOKE="$(mktemp -d)"
say -o "$SMOKE/clip.wav" --data-format=LEI16@16000 "This is the freeflo release smoke test."
FREEFLO_DATA_DIR="$SMOKE/data" FREEFLO_SELFTEST="$SMOKE/result.json" \
FREEFLO_SELFTEST_HOLD=4 FREEFLO_FAKE_MIC="$SMOKE/clip.wav" \
    perl -e 'alarm 90; exec @ARGV' dist/freeflo.app/Contents/MacOS/freeflo \
    || die "The built app did not complete its self-test."
"$PY" - "$SMOKE/result.json" <<'PYEOF' || die "Self-test failed — see the result above and ~/Library/Logs/freeflo/freeflo.log."
import json, sys
r = json.load(open(sys.argv[1]))
print('  self-test:', {k: r.get(k) for k in ('ok', 'locale', 'helper_version', 'stop_to_result_ms', 'error')})
sys.exit(0 if r.get('ok') and 'smoke test' in (r.get('text') or '').lower() else 1)
PYEOF
rm -rf "$SMOKE"

# --- 4b. codesign + notarize + staple (STAGED — off until a Developer ID exists) ---
# Flip on by exporting FREEFLO_NOTARIZE=1 along with:
#   FREEFLO_SIGN_IDENTITY  = "Developer ID Application: Your Name (TEAMID)"
#   FREEFLO_NOTARY_PROFILE = a keychain profile saved via:
#       xcrun notarytool store-credentials FREEFLO_NOTARY_PROFILE \
#         --apple-id you@example.com --team-id TEAMID --password <app-specific-pw>
# Until then the app is signed by scripts/dev_sign.sh: with FREEFLO_SIGN_IDENTITY
# naming a local (self-signed) code-signing certificate, or ad hoc without one.
# Either way it is not notarized, so Gatekeeper still warns on first open. See
# RELEASING.md, "Keeping permissions across updates (code signing)".
if [ "${FREEFLO_NOTARIZE:-0}" = "1" ]; then
    : "${FREEFLO_SIGN_IDENTITY:?set FREEFLO_SIGN_IDENTITY to notarize}"
    : "${FREEFLO_NOTARY_PROFILE:?set FREEFLO_NOTARY_PROFILE to notarize}"
    ENT="$ROOT/scripts/entitlements.plist"

    step "Codesigning freeflo.app (hardened runtime)"
    # Sign nested code (dylibs, the llama-server binary, frameworks)
    # inside-out, then the app itself. --deep is deprecated but reliable for
    # py2app bundles and signs every nested Mach-O it finds.
    codesign --force --deep --options runtime --timestamp \
        --entitlements "$ENT" --sign "$FREEFLO_SIGN_IDENTITY" dist/freeflo.app
    # --deep just re-signed the speech helper with the APP's entitlements.
    # Give it its own identity and its own minimal ones back (microphone
    # only), then re-seal the app around it.
    codesign --force --options runtime --timestamp \
        --identifier com.freeflo.helper \
        --entitlements "$ROOT/helper/entitlements.plist" \
        --sign "$FREEFLO_SIGN_IDENTITY" dist/freeflo.app/Contents/Resources/freeflo-helper
    codesign --force --options runtime --timestamp \
        --entitlements "$ENT" --sign "$FREEFLO_SIGN_IDENTITY" dist/freeflo.app
    codesign --verify --deep --strict --verbose=2 dist/freeflo.app

    step "Notarizing (submitting to Apple, waits for the verdict)"
    ( cd dist && ditto -c -k --sequesterRsrc --keepParent freeflo.app notarize.zip )
    xcrun notarytool submit dist/notarize.zip \
        --keychain-profile "$FREEFLO_NOTARY_PROFILE" --wait
    rm -f dist/notarize.zip

    step "Stapling the notarization ticket"
    xcrun stapler staple dist/freeflo.app
    xcrun stapler validate dist/freeflo.app
    echo "  signed + notarized + stapled"
else
    # setup.py copies namespace packages into the bundle after py2app seals it,
    # which breaks the seal. dev_sign.sh signs the helper, then the app, and
    # verifies: with FREEFLO_SIGN_IDENTITY set to a local code-signing
    # certificate users keep their permissions across updates; without one it
    # signs ad hoc and warns (permissions are asked again after the update).
    step "Signing freeflo.app"
    scripts/dev_sign.sh dist/freeflo.app || die "Signing failed."
    echo "  (not notarized — set FREEFLO_NOTARIZE=1 for the Developer ID + notarization path)"
fi

step "Packaging $ASSET"
( cd dist && ditto -c -k --sequesterRsrc --keepParent freeflo.app "$ASSET" )
BYTES="$(stat -f%z "dist/$ASSET")"
SIZE_MB="$(( BYTES / 1000000 ))"
echo "  $ASSET = ${SIZE_MB} MB"

# --- 5. stamp the landing page ---
step "Updating landing page ($PAGE)"
sed -i '' "s/<b class=\"verno\">v[0-9.]*<\/b>/<b class=\"verno\">v$VERSION<\/b>/" "$PAGE"
sed -i '' "s/\.zip · [0-9]* MB/.zip · ${SIZE_MB} MB/g" "$PAGE"
# Point the download buttons at THIS release's zip by tag (releases/latest may
# deliberately still be the last 1.4.x build), and say what it needs.
sed -E -i '' "s#releases/(latest/download|download/v[0-9.]+)/freeflo(-macos26)?\.zip#releases/download/$TAG/$ASSET#g" "$PAGE"
sed -i '' "s#<span>Mac only (Apple Silicon)\.#<span>Needs macOS ${MIN_MACOS%.0} or later on Apple silicon (<a href=\"${REPO_URL}/releases/tag/${LAST_LEGACY_TAG}\">older Mac? get ${LAST_LEGACY_TAG}</a>).#" "$PAGE"
grep -q "releases/download/$TAG/$ASSET" "$PAGE" || die "Could not stamp the download link into $PAGE."

# --- 6. commit, tag, push ---
step "Committing, tagging, and pushing"
git add setup.py CHANGELOG.md "$PAGE"
git commit -m "Release $TAG"
git tag -a "$TAG" -m "freeflo $TAG"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git push origin "$BRANCH"
git push origin "$TAG"

# --- 7. GitHub release ---
step "Publishing GitHub Release with $ASSET"
if [ "${FREEFLO_RELEASE_LATEST:-0}" = "1" ]; then
    LATEST_FLAG="--latest"
    echo "  flagged as the repository's latest release (1.4.x clients will be notified)"
else
    LATEST_FLAG="--latest=false"
    echo "  NOT flagged latest: releases/latest stays on the 1.4.x line (see the header)"
fi
gh release create "$TAG" "dist/$ASSET" \
  --title "freeflo $TAG" \
  --notes "$NOTES" \
  "$LATEST_FLAG"

echo
echo "✓ Released freeflo $TAG"
echo "  Release : $REPO_URL/releases/tag/$TAG"
echo "  Download: $REPO_URL/releases/download/$TAG/$ASSET"
echo "  Site    : https://rachitgupta6720.github.io/freeflo/  (Pages rebuilds in ~1-2 min)"
