#!/usr/bin/env bash
#
# install.sh — one-command installer for freeflo.
#
#   curl -fsSL https://rachitgupta6720.github.io/freeflo/install.sh | bash
#
# freeflo is an unsigned / non-notarized macOS app (no Apple Developer account),
# so downloading the .zip in a browser makes macOS flag it as "damaged" via the
# quarantine attribute. This script downloads the latest release, installs it,
# and clears that flag so the app opens normally — no scary Gatekeeper dialog.
#
set -euo pipefail

REPO="rachitgupta6720/freeflo"
ZIP_URL="https://github.com/${REPO}/releases/latest/download/freeflo.zip"
APP_NAME="freeflo.app"

say(){ printf '\033[1;36m▸\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. sanity checks ---
[[ "$(uname)" == "Darwin" ]] || die "freeflo is macOS-only."
command -v curl  >/dev/null || die "curl is required but not found."
command -v ditto >/dev/null || die "ditto is required but not found."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- 1. download ---
say "Downloading freeflo (this is a ~650 MB, fully-offline build)…"
curl -fL# "$ZIP_URL" -o "$TMP/freeflo.zip" \
  || die "Download failed. Check your connection and try again."

# --- 2. unpack ---
say "Unpacking…"
ditto -x -k "$TMP/freeflo.zip" "$TMP/unpacked" || die "Could not unzip the download."
APP_SRC="$(find "$TMP/unpacked" -maxdepth 2 -name "$APP_NAME" -type d | head -1)"
[[ -n "$APP_SRC" ]] || die "$APP_NAME not found inside the download."

# --- 3. choose install location (no password prompt if /Applications is writable) ---
DEST_DIR="/Applications"
SUDO=""
if [[ ! -w "$DEST_DIR" ]]; then
  if sudo -v >/dev/null 2>&1; then
    SUDO="sudo"
  else
    DEST_DIR="$HOME/Applications"
    mkdir -p "$DEST_DIR"
    say "Installing to $DEST_DIR (no admin rights needed)."
  fi
fi
DEST="$DEST_DIR/$APP_NAME"

# --- 4. install ---
say "Installing to $DEST…"
$SUDO rm -rf "$DEST"
$SUDO ditto "$APP_SRC" "$DEST" || die "Could not copy freeflo into $DEST_DIR."

# --- 5. clear quarantine + ad-hoc sign so it launches on any Mac ---
say "Clearing the quarantine flag (fixes the \"app is damaged\" error)…"
$SUDO xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
# Ad-hoc re-sign as a safety net (esp. Apple Silicon) — harmless if it fails.
$SUDO codesign --force --deep --sign - "$DEST" >/dev/null 2>&1 || true

# --- 6. launch ---
say "Launching freeflo…"
open "$DEST" || true

cat <<EOF

✓ freeflo is installed at $DEST

  • Look for the 🎙 icon in your menu bar.
  • On first launch, macOS will ask for Microphone and Accessibility
    permissions — grant both (System Settings → Privacy & Security).
  • Then put your cursor anywhere, hold Left Option (⌥), speak, and release.

Enjoy hands-free typing!
EOF
