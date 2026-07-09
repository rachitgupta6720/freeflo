# freeflo

A lightweight, **fully offline** dictation app for macOS. Hold a key, speak, and
your words are typed into whatever app has focus — powered by
[whisper.cpp](https://github.com/ggerganov/whisper.cpp) running locally on your
Mac. No cloud, no API keys, no data ever leaves your machine.

freeflo lives in your menu bar (🎙) and stays out of the way until you need it.

## Features

- **Push-to-talk** — hold a key (default: Left ⌥) to record, release to transcribe.
- **Toggle mode** — tap a key (default: Right ⌥) to start/stop hands-free.
- **Types anywhere** — pastes at the cursor in any app, then restores your clipboard.
- **Multilingual** — English, Hindi, Hinglish, Spanish, French, German, Chinese,
  Japanese, Arabic, Portuguese, or auto-detect.
- **Local history** — transcriptions are saved to a searchable SQLite database
  on your machine (can be disabled).
- **100% offline & private** — audio is transcribed on-device by whisper.cpp.

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.10+
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) built locally
- A microphone, and macOS **Accessibility** permission (for the global hotkey)

## Setup

### 1. Build whisper.cpp and download models

freeflo shells out to the `whisper-cli` binary and expects it (plus the GGML
models) in `~/whisper.cpp` by default:

```bash
git clone https://github.com/ggerganov/whisper.cpp ~/whisper.cpp
cd ~/whisper.cpp
cmake -B build-static
cmake --build build-static --config Release

# Models: base.en for English (fast), small for other languages
sh ./models/download-ggml-model.sh base.en
sh ./models/download-ggml-model.sh small
```

> Paths are configurable in `config.py` (`get_whisper_cli`, `get_model_path`) if
> you keep whisper.cpp somewhere else.

### 2. Install freeflo

```bash
git clone https://github.com/<your-username>/freeflo ~/freeflo
cd ~/freeflo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Run

```bash
python app.py
```

A 🎙 icon appears in the menu bar. On first launch macOS will ask for
**Microphone** and **Accessibility** permissions — grant both
(System Settings → Privacy & Security → Accessibility), then restart the app.

## Usage

- **Hold Left ⌥**, speak, release → text is typed at your cursor.
- **Tap Right ⌥** to start a hands-free recording; tap again to stop.
- Click the menu-bar icon to change language, view history, or disable dictation.

Hotkeys and language are configurable from the menu; settings persist in
`~/Library/Application Support/freeflo/config.json`.

## Building a standalone .app (optional)

To package a double-clickable `.app` bundle with everything embedded:

```bash
pip install py2app
python setup.py py2app
```

The bundle is written to `dist/freeflo.app`. `setup.py` embeds the `whisper-cli`
binary and the GGML models from `~/whisper.cpp`, so build those first.

## How it works

```
hotkey (CGEventTap) → recorder (sounddevice → WAV)
                    → transcriber (whisper-cli subprocess)
                    → injector (clipboard paste, then restore)
```

- `app.py` — menu-bar app, state machine, and wiring (rumps).
- `hotkey.py` — global push-to-talk / toggle key listener via a Quartz event tap.
- `engine/recorder.py` — records mic audio to a temp 16 kHz WAV.
- `engine/transcriber.py` — runs `whisper-cli` and cleans up its output.
- `engine/injector.py` — pastes text at the cursor and restores the clipboard.
- `engine/history.py` — local SQLite transcription history.
- `ui/` — the settings/history window (WebKit).

## License

[MIT](LICENSE) — see the LICENSE file.

Bundled/dependency licenses: whisper.cpp is MIT-licensed; the Whisper models are
released by OpenAI under the MIT license.
