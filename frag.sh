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
