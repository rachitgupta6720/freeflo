# freeflo

A lightweight, **fully offline** dictation app for macOS. Hold a key, speak, and
your words are typed into whatever app has focus — transcribed on your Mac by
the speech recognition built into macOS. No cloud, no API keys, no data ever
leaves your machine.

freeflo lives in your menu bar (🎙) and stays out of the way until you need it.

## Features

- **Push-to-talk** — hold a key (default: Left ⌥) to record, release to transcribe.
- **Toggle mode** — tap a key (default: Right ⌥) to start/stop hands-free.
- **A recording pill** — a small floating waveform shows that freeflo is
  listening, and tells you when something needs your attention (nothing heard,
  microphone muted, text copied instead of pasted). It never takes focus.
- **Types anywhere** — pastes at the cursor in any app, then restores your
  clipboard (every item and type, not just plain text).
- **Fast** — the text appears a fraction of a second after you stop talking,
  however long you spoke: it is transcribed while you speak.
- **Languages** — every language macOS can transcribe on-device on your Mac
  (English in its regional variants, Hindi, Spanish, French, German, Chinese,
  Japanese and many more). Ones that are not installed yet are a one-click
  download from Settings; macOS manages the files.
- **Two languages at once** — e.g. English (India) + Hindi: each sentence is
  taken from whichever recogniser understood it, so English, Hindi and
  Hinglish all come out right (Hindi is written in English letters). Turned
  on by itself for a Mac set to India that has the Indian language pack.
- **Vocabulary** — teach it the spelling of names and terms, and add
  "heard → written" replacements for words it keeps getting wrong.
- **Never gets stuck** — the microphone and transcriber run in a separate
  helper process. If it ever hangs or crashes, freeflo replaces it silently;
  you never have to restart the app.
- **Esc cancels** a recording; **Paste Last Transcript** in the menu repeats
  the last one.
- **Local history** — transcriptions are saved to a searchable SQLite database
  on your machine (can be disabled).
- **Turbo mode (optional)** — polish each dictation with a small local LLM
  (llama.cpp). Also fully on-device.
- **100% offline & private** — audio is transcribed in memory and never saved;
  dictation itself never touches the network.
- **Optional Google Drive backup** — off by default. If you turn it on, your
  dictation history syncs to a hidden folder in *your own* Google Drive, so
  it survives a reinstall and follows you to another Mac. See below.

## Requirements

- **macOS 26 or later** on Apple Silicon (the on-device transcriber —
  `SpeechAnalyzer` in Apple's Speech framework — does not exist on older
  systems; the app refuses to launch there with a clear message)
- A microphone, and macOS **Accessibility** permission (for the global hotkey
  and for pasting)

To run from source or build it yourself you also need:

- Python 3.9+ (the shipped app is built with the system Python 3.9)
- Xcode Command Line Tools (`xcode-select --install`) with the macOS 26 SDK —
  for the Swift helper. Full Xcode is not required.

## Setup (from source)

### 1. Get the code and the Python dependencies

```bash
git clone https://github.com/rachitgupta6720/freeflo ~/freeflo
cd ~/freeflo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Build the speech helper

```bash
scripts/build_helper.sh        # -> build/freeflo-helper (about 400 KB)
```

This compiles `helper/` (Swift) with `swiftc`; it takes a few seconds. There
is no speech model to download — macOS provides it.

### 3. Run

```bash
python app.py
```

A 🎙 icon appears in the menu bar, and on first launch a single setup window
lists the two permissions freeflo needs: **Microphone** and **Accessibility**.
Click **Allow** on each, in order — the window asks for one at a time, so two
macOS dialogs never stack — and switch freeflo on in System Settings →
Privacy & Security → Accessibility when asked. Each row ticks itself the
moment macOS grants it, and dictation works straight away: **no restart**.
You can try it right there: hold the dictation key with the setup window in
front and your words appear on its "Try it" line (anywhere else they are
typed at the cursor). Speech Recognition permission is *not* needed.

After an update of a build that is signed ad hoc, macOS treats the new build
as a new app and asks again, once: the same window opens by itself, showing
only what is missing, and closes itself when it is done
(see [RELEASING.md](RELEASING.md), "Keeping permissions across updates").
The menu's **Check Permissions…** opens it at any time.

## Usage

- **Hold Left ⌥**, speak, release → text is typed at your cursor.
- **Tap Right ⌥** to start a hands-free recording; tap again to stop.
- **Esc** while recording throws the recording away.
- Using the dictation key as an ordinary modifier (⌥←, ⌥-click) is detected and
  ignored — nothing is recorded or typed.
- Click the menu-bar icon to switch languages (English + Hindi, either alone,
  …), paste the last transcript again, view history, or disable dictation.

Hotkeys, languages, vocabulary, appearance (System / Light / Dark, Glass) and
the pill's options (live text preview, start/stop sounds) are in
**Open freeflo… → Settings**; settings persist in
`~/Library/Application Support/freeflo/` (`config.json`, `vocabulary.json`).

## Google Drive backup (optional)

Off by default. From the **Backup** tab in the freeflo window you can connect
your own Google account; freeflo then keeps a copy of your dictation history
in a hidden, app-private folder in your Drive (`appDataFolder` — it never
shows up in your regular Drive, and freeflo can't see anything else in your
Drive either). Sign in with the same account on another Mac and its history
merges with what's already backed up.

- Nothing is uploaded until you explicitly connect and approve access.
- The refresh token is stored in the macOS Keychain, never in a plain file.
- "Disconnect" revokes access on that Mac but leaves the Drive backup alone,
  so reconnecting (there or elsewhere) picks up where you left off. "Delete
  backup from Drive" removes it for good.

**Running from source:** this feature needs a Google OAuth "Desktop app"
client, which you register once in
[Google Cloud Console](https://console.cloud.google.com/apis/credentials)
(APIs & Services → Credentials → Create Credentials → OAuth client ID →
Desktop app; enable the Google Drive API on the project first). Then export
its client ID/secret before running freeflo:

```bash
export FREEFLO_GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export FREEFLO_GOOGLE_CLIENT_SECRET="your-client-secret"
```

Without these set, the Backup tab shows "Google Backup isn't available in
this build" and everything else works exactly as before.

## Building a standalone .app (optional)

To package a double-clickable `.app` bundle with everything embedded:

```bash
pip install py2app
scripts/build_helper.sh          # build/freeflo-helper
python setup.py py2app           # dist/freeflo.app (about 55 MB)
scripts/dev_sign.sh              # signs the helper, then the app; verifies
```

`setup.py` changes the bundle after py2app has sealed it, so the bundle does
not verify (`codesign --verify`) until it is signed again. `scripts/dev_sign.sh` signs with the certificate named
by `FREEFLO_SIGN_IDENTITY` when there is one in your keychain, otherwise ad hoc
(with a warning: an ad-hoc build is a new app to macOS, so Microphone and
Accessibility are asked again after every rebuild).
`scripts/dev_sign.sh --status [bundle]` only reports how a bundle is signed.

`build/` is also py2app's scratch directory: clean with
`rm -rf build/bdist* dist`, not `rm -rf build`, or build the helper again
afterwards.

`setup.py` embeds `build/freeflo-helper`. For **Turbo mode** it also embeds a
static `llama-server` (llama.cpp) if it finds one — in `$FREEFLO_LLAMA_SERVER`,
`vendor/llama-server`, or `~/llama.cpp/build-static/bin/llama-server`, in that
order. Without one the build still succeeds and Turbo is shown as unavailable.

A scripted end-to-end check of the built app, with no microphone involved (the
helper plays a WAV file instead, and nothing is pasted or saved to your data):

```bash
say -o /tmp/clip.wav --data-format=LEI16@16000 "Testing freeflo."
FREEFLO_DATA_DIR=/tmp/freeflo-data FREEFLO_SELFTEST=/tmp/result.json \
FREEFLO_FAKE_MIC=/tmp/clip.wav dist/freeflo.app/Contents/MacOS/freeflo
cat /tmp/result.json             # {"ok": true, "text": "Testing freeflo.", ...}
```

Environment switches for testing a build without touching your real setup:

- `FREEFLO_DATA_DIR=<dir>` — use this folder instead of
  `~/Library/Application Support/freeflo` (settings, history, vocabulary).
  The log then goes to `<dir>/logs/freeflo.log` instead of
  `~/Library/Logs/freeflo` (`FREEFLO_LOG_DIR=<dir>` picks another folder).
- `FREEFLO_FAKE_MIC=<file.wav>` — the helper plays this 16 kHz WAV instead of
  opening the microphone.
- `FREEFLO_FAKE_AX=<file>` — simulate the Accessibility permission: the app
  treats it as granted while the file contains `1` and as missing otherwise
  (`echo 1 > file` mid-run simulates the grant). The hotkeys are inert in
  this mode (a key press never records or pastes) and nothing is asked of
  macOS — prompts and System Settings links are only logged. Use it to test
  the setup window.
- `FREEFLO_SELFTEST=<result.json>` — run one scripted dictation and quit.

## Troubleshooting

### Accessibility is on but freeflo says it is off

System Settings may still show freeflo switched on from an earlier build that
macOS no longer recognises (this happens after an update of an ad-hoc signed
build). In System Settings → Privacy & Security → Accessibility:

1. Switch freeflo **off and on again**. The setup window ticks the row within
   a second when it works — no restart needed.
2. If it still does not tick, select freeflo, remove it with **−**, then add
   it again with **+** (pick `/Applications/freeflo.app`) and switch it on.
3. Last resort, by hand in Terminal:
   `tccutil reset Accessibility com.freeflo.app`, then quit and relaunch
   freeflo and allow it again. This clears freeflo's Accessibility entry for
   every copy of the app on this Mac.

## Tests

```bash
python -m unittest discover -s tests          # the Python side (fake helper)
python helper/tests/smoke_test.py             # the real helper, no microphone
```

## How it works

```
hotkey (CGEventTap) ──► app.py ──► SpeechEngine ══ JSON lines ══► freeflo-helper
                          │        (supervisor)     stdin/stdout   (Swift child process)
                          │                                         AVAudioEngine  → microphone
                          │   ◄── level / partial / final ────────  SpeechAnalyzer → transcript
                          ▼
              vocabulary → (Turbo) → injector (clipboard paste, then restore)
```

The microphone and the transcriber live in a **separate process** on purpose.
Audio code occasionally hangs deep inside the system, and a hung thread cannot
be killed — a hung process can. `engine/speech.py` gives every operation a
deadline; if the helper misses one, crashes, or talks nonsense it is killed
and respawned, the dictation in flight reports an error, and the next one just
works.

- `app.py` — menu-bar app, dictation bookkeeping, and wiring (rumps).
- `hotkey.py` — global push-to-talk / toggle / Esc listener via a Quartz event tap.
- `helper/` — `freeflo-helper` (Swift): microphone capture + on-device
  transcription, speaking a small JSON-lines protocol (`helper/README.md`).
- `engine/speech.py` — spawns and supervises the helper; never blocks its caller.
- `engine/locales.py` — the language list (from the helper) under friendly names.
- `engine/vocabulary.py` — custom words and "heard → written" replacements.
- `engine/injector.py` — pastes text at the cursor and restores the clipboard.
- `engine/refiner.py` — Turbo mode (local llama-server).
- `engine/history.py` — local SQLite transcription history.
- `engine/gauth.py` — Google sign-in (OAuth loopback flow, Keychain storage).
- `engine/backup.py` — merges and syncs history with Google Drive's
  `appDataFolder`.
- `ui/hud.py` — the floating recording pill (Core Animation, never takes focus).
- `ui/` — the settings/history window and onboarding (WebKit).

## License

[MIT](LICENSE) — see the LICENSE file.

Speech recognition is provided by macOS (Apple's Speech framework); no speech
model is bundled. Turbo mode uses llama.cpp (MIT-licensed).
