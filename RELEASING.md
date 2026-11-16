# Releasing freeflo

This document explains how freeflo is versioned and how a change on your Mac
becomes a live update on GitHub and the landing page.

## Versioning

freeflo uses [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

| Part  | Bump when…                                          | Example         |
|-------|-----------------------------------------------------|-----------------|
| MAJOR | Incompatible change, rewrite, dropping macOS support | `1.4.2 → 2.0.0` |
| MINOR | New feature, backwards-compatible                    | `1.4.2 → 1.5.0` |
| PATCH | Bug fix or small tweak                               | `1.4.2 → 1.4.3` |

The version lives in **one place** — `setup.py` (`CFBundleVersion` /
`CFBundleShortVersionString`) — and the release script keeps everything else
(the CHANGELOG, the download, the landing-page label) in sync with it.

## Two kinds of change

### 1. Everyday changes — no new download

Editing the landing page, docs, or source without shipping a new app build.

```bash
./scripts/publish.sh "Explain the accessibility permission better"
```

This commits and pushes. If you touched `docs/`, GitHub Pages rebuilds the
site automatically (~1–2 min). No version bump, no new download.

### 2. A new app version — new downloadable build

When the change should reach users as a new download:

```bash
./scripts/release.sh 1.1.0
# or with a headline note:
./scripts/release.sh 1.1.0 "Adds Korean support and fixes the toggle race"
```

One command does all of this:

1. Validates the version and checks the tag/release don't already exist.
2. Bumps the version in `setup.py`.
3. Rolls the `## [Unreleased]` section of `CHANGELOG.md` into a dated
   `## [1.1.0] - YYYY-MM-DD` section.
4. Builds the Swift speech helper and `freeflo.app` (py2app), runs a scripted
   dictation through the built app (self-test, no microphone involved), signs
   it, and packages `freeflo-macos26.zip`. Signing: with `FREEFLO_NOTARIZE=1`
   the Developer ID + notarization path; otherwise `scripts/dev_sign.sh`,
   which uses the certificate named by `FREEFLO_SIGN_IDENTITY` or, without
   one, signs ad hoc and warns (see "Keeping permissions across updates"
   below).
5. Stamps the new version number, download link and download size into
   `docs/index.html`.
6. Commits, creates the `v1.1.0` git tag, and pushes both.
7. Publishes a GitHub Release with `freeflo-macos26.zip` attached, using the
   CHANGELOG notes as the release body (prefixed with the macOS requirement).

### Two release lines — do not publish this build as `freeflo.zip`

Builds up to **1.4.x** (the Whisper engine) run on any macOS and on Intel.
Their in-app updater polls `releases/latest` and offers whatever is there as
`freeflo.zip`, with no check of what the build needs. This build needs
**macOS 26+ on Apple silicon**. If it were published as the latest
`freeflo.zip`, a 1.4.x user on macOS 14/15 would download it, drag it over
their working app, and be left with an app that cannot open.

So `release.sh`:

- names the download **`freeflo-macos26.zip`** (`ASSET` in the script,
  `_ASSET_NAME` in `engine/updater.py` — keep them in sync). A 1.4.x client
  cannot download it; this build's updater only offers releases that carry it.
- publishes the release **without the "latest" flag** by default, so
  `releases/latest` stays on v1.4.3 and 1.4.x clients are not told about a
  build most of them may not be able to run. This build's updater reads the
  release list, so it finds new releases regardless. The landing page links
  the release by tag.
- starts the notes with the requirement and ends them with
  `<!-- freeflo-requires: macos=26.0 arch=arm64 -->`, which the updater uses
  to skip releases the running Mac cannot use.

`FREEFLO_RELEASE_LATEST=1 ./scripts/release.sh X.Y.Z` flags the release as
latest anyway: 1.4.x clients then show "Update available", and their
"Download update" opens the release page (there is no `freeflo.zip`). Users
who cannot upgrade keep getting that notification every 6 hours — which is
why it is opt-in. The clean way to tell 1.4.x users is a final 1.4.x bridge
release (from the 1.4 line) whose updater checks the macOS version first.

**Before the first release of this line, launch the built bundle once on a
macOS 26.x Mac** — every test so far ran on macOS 27.

**Before releasing:** jot what changed under `## [Unreleased]` in
`CHANGELOG.md` as you work. At release time those notes become the version's
notes automatically.

## Prerequisites for `release.sh`

- The Python venv with py2app: `.venv` (the script falls back to `python3`).
- Xcode Command Line Tools with the macOS 26 SDK — the script compiles the
  Swift speech helper (`scripts/build_helper.sh`) into the `.app`.
- A static `llama-server` for Turbo mode: `$FREEFLO_LLAMA_SERVER`,
  `vendor/llama-server`, or `~/llama.cpp/build-static/bin/llama-server`.
- `gh` authenticated (`gh auth status`).
- `scripts/dev_sign.sh` executable (the script checks). Optional but strongly
  recommended: `FREEFLO_SIGN_IDENTITY` set to a local code-signing
  certificate, so users keep their permissions across the update (next
  section). Without it the build is signed ad hoc.
- For the notarized path only: `FREEFLO_NOTARIZE=1`, a Developer ID
  Application identity in `FREEFLO_SIGN_IDENTITY`, and a notarytool keychain
  profile in `FREEFLO_NOTARY_PROFILE` (see step 4b in `release.sh`).

## Keeping permissions across updates (code signing)

### Why every update asks again today

macOS stores each privacy grant (Microphone, Accessibility) together with the
app's *designated requirement* - the rule that says "this is the same app".
For an ad-hoc signature (`codesign --sign -`), that rule is the hash of this
exact build:

```
$ codesign -d -r- /Applications/freeflo.app
# designated => cdhash H"290ef3a5..." or cdhash H"3ed5f494..."
```

So every rebuild or update is a stranger to macOS: the Microphone goes back to
"not asked yet", and Accessibility silently stops working while System
Settings still shows freeflo switched on. (The app copes with that in one
setup window, with no restart, but the user still has to do it.)

With a signing certificate the requirement names the certificate instead
(`identifier "com.freeflo.app" and certificate leaf = H"..."`), and grants
carry over to every build signed with the same certificate.

### How to check a build

```bash
scripts/dev_sign.sh --status /Applications/freeflo.app   # or dist/freeflo.app
```

It prints the designated requirement and exits 0 when the identity is stable,
1 when it is pinned to a build hash (ad hoc) or the bundle is not signed.

### Option A - a local self-signed certificate (free; done once, by hand)

1. Open **Keychain Access** > menu **Keychain Access** > **Certificate
   Assistant** > **Create a Certificate…**
2. Name: `freeflo Local Signing`. Identity Type: **Self-Signed Root**.
   Certificate Type: **Code Signing**. Tick **Let me override defaults**.
3. Continue, set **Validity Period (days)** to `3650`, accept the remaining
   defaults, and choose the **login** keychain as the location.
4. In the shell you release from:

   ```bash
   export FREEFLO_SIGN_IDENTITY="freeflo Local Signing"
   security find-identity -p codesigning    # it is listed
   ```

   `CSSMERR_TP_NOT_TRUSTED` / "not trusted" next to it is expected for a
   self-signed certificate; it still signs (`dev_sign.sh` deliberately looks
   it up without `-v`).
5. The first time `codesign` uses the key, macOS asks whether it may use the
   key in your keychain: click **Always Allow** (otherwise every build asks,
   and a non-interactive build blocks on that dialog).

Keep this certificate (and back up the login keychain). A new certificate is a
new identity: users would have to grant permissions once more. Gatekeeper's
first-open warning is unchanged - a self-signed certificate is not a
Developer ID and the app is not notarized.

### Option B - Developer ID + notarization

`FREEFLO_NOTARIZE=1` with a "Developer ID Application" identity (the existing
step 4b of `release.sh`). This also removes the Gatekeeper warning. Needs a
paid Apple Developer account.

### What to expect

- The **first** update from an ad-hoc build to a signed one still needs one
  last grant (the old grants were tied to the old hash). After that, updates
  signed with the same certificate keep them.
- `release.sh` calls `scripts/dev_sign.sh` whenever `FREEFLO_NOTARIZE` is not
  `1`; for local builds run it yourself after `python setup.py py2app`.
- No script creates a certificate, imports one, or changes a keychain:
  `dev_sign.sh` only reads (`security find-identity`) and signs with what is
  already there. If `FREEFLO_SIGN_IDENTITY` is unset or not found it signs ad
  hoc and prints a warning.
- Option A was written from Keychain Access's documented flow; these steps
  were not exercised while writing this section (no certificate was created
  on the build Mac), and the signed path of `dev_sign.sh` has not been run.

## Working with Claude Code

You can also just ask Claude in a session:

- *"push my changes"* → runs the everyday flow (`publish.sh`).
- *"cut a release, version 1.1.0"* → runs the release flow (`release.sh`).

Claude will read `CHANGELOG.md` for context on what shipped when.

## Why not fully automatic (CI)?

freeflo's download is a **native macOS build**: a Swift helper compiled
against the macOS 26 SDK plus a py2app bundle, smoke-tested by running a real
dictation through the built app on the build machine. That needs a recent
macOS runner with the on-device speech assets installed, so for now the build
stays on your Mac and `release.sh` automates everything around it. The
*website* half already deploys automatically via GitHub Pages on every push.

If you later want tag-triggered CI releases, that's possible with a macOS
runner — open an issue / ask Claude and we can add a workflow.

## History

See [CHANGELOG.md](CHANGELOG.md) for the full version-by-version history.
