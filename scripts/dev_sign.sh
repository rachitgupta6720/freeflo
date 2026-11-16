#!/bin/bash
# Sign a built freeflo.app so that it runs - and, with a real signing
# identity, so that macOS keeps its Microphone and Accessibility permissions
# from one build to the next.
#
#   scripts/dev_sign.sh                       signs dist/freeflo.app
#   scripts/dev_sign.sh path/to/freeflo.app   signs that bundle
#   scripts/dev_sign.sh --status [bundle]     only reports how it is signed
#
# Identity: $FREEFLO_SIGN_IDENTITY (a certificate name or its SHA-1 hash) when
# that identity is in one of your keychains; otherwise ad-hoc, with a warning.
#
# Why it matters: macOS remembers a privacy permission together with the
# app's "designated requirement". For an ad-hoc signature that requirement is
# the hash of this exact build, so every rebuild is a stranger: Microphone is
# asked again and Accessibility silently stops working while System Settings
# still shows it switched on. With a certificate the requirement names the
# certificate instead, and permissions survive updates. See RELEASING.md,
# "Keeping permissions across updates".
#
# This script only READS the keychain (security find-identity) and signs with
# what is already there. It never creates a certificate, never imports one
# and never changes a keychain or its settings. The first time codesign uses
# a key, macOS itself may ask you to allow that: answer "Always Allow".
#
# Order: the speech helper first (its own identifier; its entitlements and
# its embedded Info.plist stay as they are), then the app around it. Not
# --deep: that would re-sign the helper as part of the app.
#
# Exits non-zero if anything fails.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPER_ID="com.freeflo.helper"

say()  { echo "dev_sign: $*"; }
warn() { echo "dev_sign: WARNING - $*" >&2; }
fail() { echo "dev_sign: $*" >&2; exit 1; }

STATUS_ONLY=0
if [ "${1:-}" = "--status" ]; then STATUS_ONLY=1; shift; fi
APP="${1:-$ROOT/dist/freeflo.app}"
APP="${APP%/}"
HELPER="$APP/Contents/Resources/freeflo-helper"

[ -d "$APP/Contents/MacOS" ] || fail "no app bundle at $APP (build it first: python setup.py py2app)"
[ -f "$HELPER" ] || fail "the bundle has no freeflo-helper ($HELPER)"
command -v codesign >/dev/null 2>&1 || fail "codesign not found; install the Xcode Command Line Tools"

# What `codesign -d -r-` says the designated requirement of $1 is.
requirement_of() {
    codesign -d -r- "$1" 2>&1 | sed -n 's/^# designated => //p; s/^designated => //p' | head -1
}

# Print how the bundle is signed; returns 0 when the identity is stable
# (the requirement does not pin a build hash).
report() {
    local req
    req="$(requirement_of "$APP")"
    if [ -z "$req" ]; then
        say "$APP is not signed"
        return 1
    fi
    say "designated requirement: $req"
    case "$req" in
        *cdhash*)
            say "identity: THIS BUILD ONLY (ad-hoc) - permissions are asked again after every rebuild"
            return 1 ;;
        *)
            say "identity: stable - permissions carry over to the next build signed the same way"
            return 0 ;;
    esac
}

if [ "$STATUS_ONLY" = "1" ]; then
    report && exit 0 || exit 1
fi

# SHA-1 of the code-signing identity called (or hashed) $1, or nothing.
# Deliberately without -v: a self-signed certificate is "not trusted" and -v
# would hide it, yet it signs perfectly well.
find_identity() {
    security find-identity -p codesigning 2>/dev/null | awk -v want="$1" '
        /^[[:space:]]*[0-9]+\)[[:space:]]+[0-9A-Fa-f]+[[:space:]]+"/ {
            hash = $2
            name = $0
            sub(/^[^"]*"/, "", name)
            sub(/".*$/, "", name)
            if (name == want || toupper(hash) == toupper(want)) { print hash; exit }
        }'
}

WANTED="${FREEFLO_SIGN_IDENTITY:-}"
SIGN="-"
LABEL="ad-hoc"
if [ -n "$WANTED" ] && [ "$WANTED" != "-" ]; then
    FOUND="$(find_identity "$WANTED" || true)"
    if [ -n "$FOUND" ]; then
        SIGN="$FOUND"
        LABEL="$WANTED ($FOUND)"
    else
        warn "FREEFLO_SIGN_IDENTITY=\"$WANTED\" is not a code-signing identity in your keychains."
        warn "(list them with: security find-identity -p codesigning)"
        warn "Falling back to an ad-hoc signature."
    fi
fi
if [ "$SIGN" = "-" ]; then
    warn "ad-hoc signature: macOS treats every build as a new app, so Microphone and"
    warn "Accessibility have to be granted again after each rebuild or update."
    warn "Set FREEFLO_SIGN_IDENTITY to keep them (RELEASING.md, \"Keeping permissions across updates\")."
fi

LOG="$(mktemp -t freeflo-dev-sign)"
trap 'rm -f "$LOG"' EXIT

# Signing with a key codesign has not used before makes macOS put up a
# "codesign wants to sign using key..." dialog, and codesign waits for it with
# no timeout - an unattended build would hang forever. So every codesign call
# runs under a watchdog (perl's alarm: no coreutils needed); 142 = SIGALRM.
SIGN_TIMEOUT="${FREEFLO_SIGN_TIMEOUT:-120}"
with_watchdog() {
    perl -e 'alarm shift @ARGV; exec @ARGV or exit 127' "$SIGN_TIMEOUT" "$@"
}

sign() {    # sign <what it is> <codesign arguments...>
    local what="$1"; shift
    local rc=0
    with_watchdog codesign --force --timestamp=none --sign "$SIGN" "$@" >"$LOG" 2>&1 || rc=$?
    if [ "$rc" = "142" ]; then
        fail "codesign gave up after ${SIGN_TIMEOUT}s signing $what. macOS is probably waiting for you to allow codesign to use the key: answer the dialog (Always Allow) and run this again."
    fi
    if [ "$rc" != "0" ]; then
        cat "$LOG" >&2
        fail "could not sign $what"
    fi
}

say "signing $APP"
say "identity: $LABEL"

# 1. The helper. --preserve-metadata keeps whatever entitlements it was built
#    with (none for an ad-hoc build, microphone-only for a Developer ID one).
#    Its Info.plist (NSMicrophoneUsageDescription) is a section of the binary
#    itself, so signing cannot lose it; that is checked below all the same.
sign "the speech helper" --identifier "$HELPER_ID" --preserve-metadata=entitlements "$HELPER"
HELPER_INFO="$(codesign -dv "$HELPER" 2>&1)"
echo "$HELPER_INFO" | grep -q "^Identifier=$HELPER_ID\$" \
    || fail "the helper lost its identifier ($HELPER_ID)"
echo "$HELPER_INFO" | grep -q "^Info.plist entries=" \
    || fail "the helper has no embedded Info.plist (rebuild it: scripts/build_helper.sh)"

# 2. The app, sealing everything inside it (the helper included).
sign "the app" "$APP"

# 3. Check the result the way macOS will.
with_watchdog codesign --verify --deep --strict "$APP" 2>"$LOG" || { cat "$LOG" >&2; fail "the signed bundle does not verify"; }
say "verified (codesign --verify --deep --strict)"
report || true
