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
#   4. Rebuilds the .app bundle (py2app) and repackages freeflo.zip.
#   5. Stamps the new version + download size into the landing page.
#   6. Commits everything, tags v<version>, and pushes (code + tag).
#   7. Publishes a GitHub Release with freeflo.zip attached.
#
# GitHub Pages redeploys the landing page automatically on push, and the
# download button (which points at releases/latest) picks up the new zip.
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
PY="$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)"

die(){ echo "✗ $*" >&2; exit 1; }
step(){ echo; echo "▸ $*"; }

# --- 1. validate ---
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Version must look like X.Y.Z (got '${VERSION:-<none>}')."
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "Tag $TAG already exists."
gh release view "$TAG" >/dev/null 2>&1 && die "Release $TAG already exists on GitHub."
[ -x "$HOME/whisper.cpp/build-static/bin/whisper-cli" ] || die "whisper-cli not found — build whisper.cpp first (see README)."

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

# --- 4. build ---
step "Building freeflo.app (py2app) — this takes a minute"
# Bake in the Google OAuth client so the shipped app can offer backup — a
# double-clicked app has no shell env to read FREEFLO_GOOGLE_* from. The file
# is gitignored; setup.py bundles it into the app when present. Without the
# env vars the build still succeeds and backup simply stays unavailable.
if [ -n "${FREEFLO_GOOGLE_CLIENT_ID:-}" ] && [ -n "${FREEFLO_GOOGLE_CLIENT_SECRET:-}" ]; then
    printf '{"client_id":"%s","client_secret":"%s"}' \
        "$FREEFLO_GOOGLE_CLIENT_ID" "$FREEFLO_GOOGLE_CLIENT_SECRET" > google_client.json
    echo "  baked in Google OAuth client (backup enabled in this build)"
else
    rm -f google_client.json
    echo "  no FREEFLO_GOOGLE_* env vars set — building without backup credentials"
fi
rm -rf build dist
"$PY" setup.py py2app >/dev/null
[ -d dist/freeflo.app ] || die "Build did not produce dist/freeflo.app."

# --- 4b. codesign + notarize + staple (STAGED — off until a Developer ID exists) ---
# Flip on by exporting FREEFLO_NOTARIZE=1 along with:
#   FREEFLO_SIGN_IDENTITY  = "Developer ID Application: Your Name (TEAMID)"
#   FREEFLO_NOTARY_PROFILE = a keychain profile saved via:
#       xcrun notarytool store-credentials FREEFLO_NOTARY_PROFILE \
#         --apple-id you@example.com --team-id TEAMID --password <app-specific-pw>
# Until then the app ships unsigned (Gatekeeper shows the "damaged" warning).
if [ "${FREEFLO_NOTARIZE:-0}" = "1" ]; then
    : "${FREEFLO_SIGN_IDENTITY:?set FREEFLO_SIGN_IDENTITY to notarize}"
    : "${FREEFLO_NOTARY_PROFILE:?set FREEFLO_NOTARY_PROFILE to notarize}"
    ENT="$ROOT/scripts/entitlements.plist"

    step "Codesigning freeflo.app (hardened runtime)"
    # Sign nested code (dylibs, the whisper-cli binary, frameworks) inside-out,
    # then the app itself. --deep is deprecated but reliable for py2app bundles.
    codesign --force --deep --options runtime --timestamp \
        --entitlements "$ENT" --sign "$FREEFLO_SIGN_IDENTITY" dist/freeflo.app
    codesign --verify --strict --verbose=2 dist/freeflo.app

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
    echo "  (unsigned build — set FREEFLO_NOTARIZE=1 to codesign + notarize)"
fi

step "Packaging freeflo.zip"
( cd dist && ditto -c -k --sequesterRsrc --keepParent freeflo.app freeflo.zip )
BYTES="$(stat -f%z dist/freeflo.zip)"
SIZE_MB="$(( BYTES / 1000000 ))"
echo "  freeflo.zip = ${SIZE_MB} MB"

# --- 5. stamp the landing page ---
step "Updating landing page ($PAGE)"
sed -i '' "s/<b class=\"verno\">v[0-9.]*<\/b>/<b class=\"verno\">v$VERSION<\/b>/" "$PAGE"
sed -i '' "s/\.zip · [0-9]* MB/.zip · ${SIZE_MB} MB/g" "$PAGE"

# --- 6. commit, tag, push ---
step "Committing, tagging, and pushing"
git add setup.py CHANGELOG.md "$PAGE"
git commit -m "Release $TAG"
git tag -a "$TAG" -m "freeflo $TAG"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git push origin "$BRANCH"
git push origin "$TAG"

# --- 7. GitHub release ---
step "Publishing GitHub Release with freeflo.zip"
gh release create "$TAG" dist/freeflo.zip \
  --title "freeflo $TAG" \
  --notes "$NOTES"

echo
echo "✓ Released freeflo $TAG"
echo "  Release : https://github.com/rachitgupta6720/freeflo/releases/tag/$TAG"
echo "  Download: https://github.com/rachitgupta6720/freeflo/releases/latest/download/freeflo.zip"
echo "  Site    : https://rachitgupta6720.github.io/freeflo/  (Pages rebuilds in ~1-2 min)"
