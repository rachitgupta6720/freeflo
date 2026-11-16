# Changelog

All notable changes to **freeflo** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** — incompatible changes (e.g. a rewrite, dropping macOS versions).
- **MINOR** — new features, backwards-compatible (e.g. a new language, a setting).
- **PATCH** — bug fixes and small tweaks.

> How releases are cut and how the version numbers flow into the app, the
> download, and the landing page is documented in [RELEASING.md](RELEASING.md).

## [Unreleased]

### Changed
- **New speech engine: the recognition built into macOS.** freeflo now
  transcribes with Apple's on-device speech recogniser instead of a bundled
  Whisper model. It listens *while* you speak, so the text is ready a fraction
  of a second after you release the key however long you talked (a 70-second
  dictation that took 3.4 s to transcribe now takes about 0.2 s), and the
  download shrinks from roughly 700 MB to about 55 MB because no model ships
  inside the app.
- **freeflo now requires macOS 26 or later.** The new recogniser does not exist
  on older systems. On an older Mac the app refuses to open with the standard
  "needs a newer macOS" message rather than launching half-working.
- **Languages come from your Mac.** The language list is every language macOS
  can transcribe on-device (with regional variants, e.g. English (India)), and
  defaults to your system language. Your previous choice is carried over:
  "Auto-detect" becomes your system language, "English"/"Hindi"/… become the
  matching variant for your region. A language that is not installed yet can
  be downloaded with one click (choosing it starts the download, and it is
  used as soon as it lands) — macOS stores it, not freeflo. If you had chosen
  Auto-detect (or a language the recogniser does not have), freeflo tells you
  what it switched to instead of changing it silently; for an English speaker
  in India it switches to English + Hindi (below), which follows mixed speech
  the way Auto-detect did.
- Using the dictation key as an ordinary modifier (⌥←, ⌥-click) is now noticed
  the moment the second key or the click happens, instead of when you let go,
  so the microphone closes immediately. This only applies to the first half
  second of a hold: once you are dictating, clicking into the text field or
  brushing a key no longer throws the dictation away. Letting go of the
  previous letter or of Shift just after pressing the dictation key no longer
  cancels the dictation.
- **Downloads are named `freeflo-macos26.zip`**, and the in-app updater only
  offers releases this Mac can run. Versions up to 1.4.x keep finding their
  own line under `releases/latest` and are never handed this build on a Mac
  that cannot open it (see RELEASING.md).
- Pasting no longer needs the "control System Events" Automation permission —
  only Accessibility — and restores everything that was on your clipboard
  (images, files, rich text), not just plain text. Clipboard managers no longer
  record each dictation.
- Google Drive backup, history, saved prompts, Turbo mode and updates work as
  before. Turbo now also comes back on by itself after you relaunch freeflo.

### Added
- **Two languages at once — Hinglish works.** freeflo can listen for two
  languages together, for example English (India) + Hindi: both recognisers
  hear the same audio and each sentence is taken from the one that understood
  it better, so English sentences keep their punctuation and Hindi or mixed
  sentences come out right ("Okay, team, quick update on the release. Kal raat
  ko server down ho gaya tha isliye deployment ruk gaya. We will retry tonight
  after the database migration."). A sentence with Hindi words in it goes to
  the Hindi recogniser even when the English one sounded sure of itself, a
  sentence both heard the same way keeps the English punctuation, and Hindi
  words no longer pick up stray capitals ("ho gaya Tha"). Hindi is written in
  English letters, like your earlier dictations. No extra wait: the text still
  arrives about 0.2 s after you let go (0.15–0.5 s measured, depending on
  length). If your Mac is set to India and already has the Indian
  language pack, Hindi is switched on for you; otherwise Home and Settings
  offer "Add Hindi" (a one-time ~620 MB download shared by all Indian
  languages). The menu-bar Language menu switches between "English (India) +
  Hindi", either one alone, and your other downloaded languages in one click.
- **Setup in one window, once.** The setup window lists only what is
  actually missing (after an update, usually just Accessibility), asks macOS
  for one thing at a time and only when you click, and ticks each row the
  moment it is granted. No restart: the dictation keys start working as soon
  as Accessibility is switched on. It no longer opens again for someone who is
  already set up, and "Check Permissions…" in the menu reopens it. Once the
  keys work you can try them right there: hold the dictation key with the
  setup window in front and your words appear on its "Try it" line instead of
  being typed into nowhere.
  `scripts/dev_sign.sh` signs builds with a stable identity when one is
  configured, so permissions survive updates (see RELEASING.md).
- **A simpler window with a real glass look.** Three places — Home, History,
  Settings — in the style of macOS settings. Home shows one status line, your
  languages and your last five dictations with a copy button. Settings is one
  page (shortcuts, languages, vocabulary, appearance, sounds, backup, privacy,
  and an Advanced section). Appearance is System / Light / Dark plus a Glass
  switch that puts the system's Liquid Glass behind the window (paused while
  Reduce Transparency or Increase Contrast is on), follows your accent colour,
  and applies at once to every window and to the recording pill.
- **A recording pill.** A small floating waveform appears while freeflo is
  listening and settles into a "working" animation until the text lands. It
  never takes keyboard focus. It also tells you, briefly, when a dictation
  did not go to plan: nothing was heard, the microphone is muted or
  disconnected, or the text was copied because it could not be pasted
  ("Copied — press ⌘V").
- **Vocabulary** (Settings). Add names and terms that should always be written
  a certain way ("Kiwi", "UPI"), and "heard → written" replacements for words
  the recogniser keeps getting wrong or for short phrases that should expand
  into a snippet.
- **Esc cancels a recording** — handy in hands-free (toggle) mode.
- **Paste Last Transcript** in the menu-bar menu types your last dictation
  again.
- Consecutive dictations into the same place are joined with a space.
- Start a second dictation while the first is still being written out: it no
  longer answers "Busy".
- Optional **start/stop sounds** and a **live text preview** in the pill
  (Settings → While you dictate; both off by default).
- A very short tap of the push-to-talk key is ignored instead of reporting
  "nothing heard".
- Troubleshooting → **Restart Speech Engine**, and speech-engine details in
  Copy Diagnostics.

### Fixed
- **"Audio stalled — restart freeflo" is gone.** Closing the microphone could
  deadlock deep inside macOS's audio system; earlier versions could only detect
  that and ask you to restart. The microphone and the recogniser now run in a
  small separate helper process. If it ever hangs or crashes, freeflo replaces
  it in the background — at worst one dictation reports "try again", and the
  next one works without you doing anything.
- The orange microphone indicator turns off as soon as you stop speaking. (The
  previous engine kept the microphone open for a moment between dictations to
  avoid the deadlock above; there is nothing to work around any more.)
- A push-to-talk recording can no longer keep running if macOS swallows the
  key-up (for example when a password field takes over the keyboard): freeflo
  notices the key is no longer held and finishes the dictation.
- The hotkey recovers when macOS temporarily switches off keyboard monitoring
  "by user input", not only after a timeout.
- Pasting works on keyboard layouts that move the V key (Dvorak, Turkish F,
  Workman…): the paste shortcut is sent with the key that types "v" on the
  layout in use.
- If the microphone stops delivering sound in the middle of a dictation
  (Bluetooth dropout), what you had already said is still written out.
- A hands-free recording that reaches the 5-minute limit says so, and the tap
  you then make to "stop" no longer starts a new recording unnoticed.
- The recording pill (and the start sound) appear when the microphone is
  actually live, not a fixed moment after the key press.
- A microphone test left running in the freeflo window ends when the window
  closes, and no longer blocks the dictation keys.
- A slow or unresponsive app that owns the clipboard can no longer delay a
  dictation: after a quarter of a second freeflo pastes without preserving the
  previous clipboard.
- Switching off Accessibility while freeflo is running removes its keyboard
  monitor at once, says so once, and brings it back the moment access is
  switched on again.
- Setup no longer fires the Microphone and Accessibility prompts at the same
  moment at launch, never asks twice in one run, and "Allow" for
  Accessibility opens System Settings instead of prompting again.
- Stopping the hotkey no longer leaves a disabled keyboard monitor behind, and
  starting then stopping it quickly can no longer leave one running.
- "Update available" is announced once per version instead of every 6 hours,
  and the launch no longer checks twice.
- Two settings changes at the same moment could make one of them fail with
  "No such file" (both used the same temporary file).
- The Language section of Settings notices a language pack that was added or
  removed in System Settings while the window was open.

### Removed
- The bundled `whisper-cli` and Whisper models, and the PortAudio / NumPy /
  SciPy dependencies. Building from source no longer needs whisper.cpp — only
  the Xcode Command Line Tools (see the README).

## [1.4.2] - 2026-08-03

### Fixed
- **Dictation could freeze permanently on the ⏳ icon.** Stopping a recording
  occasionally deadlocked deep inside macOS's audio system, which killed the
  hotkey and left freeflo stuck until you quit and relaunched it — with nothing
  in the logs to show why. The microphone now stays open between dictations and
  is only ever handled on its own dedicated thread, so stopping a recording can
  no longer stall the hotkey or the menu bar. The same freeze could also be
  triggered by the mic test in the window or during onboarding.
- A dictation that gets stuck for any other reason now recovers on its own
  instead of leaving the icon spinning forever, and if the audio system does
  become unresponsive freeflo says so and offers a restart rather than silently
  ignoring the hotkey.
- Non-English dictation no longer fails with an encoding error — transcripts in
  Hindi and other non-Latin scripts were being discarded entirely.
- Long dictations are no longer cut off by a fixed 30-second transcription limit;
  the limit now scales with how long you spoke.
- The microphone indicator is released ~30 seconds after your last dictation, and
  immediately when you disable dictation or quit.
- When your Google sign-in expires, freeflo now tells you once and offers a
  Reconnect option, instead of silently retrying every two minutes forever.
  Failed syncs also back off gradually rather than retrying at a fixed interval.

## [1.4.1] - 2026-08-02

### Added
- Usage-analytics and crash-report consent checkboxes now appear directly on the
  final onboarding screen, so you can choose what to share before you finish
  setup — both are opt-out and never include your dictated text or audio.

### Changed
- New Dock and Finder app icon — an on-brand monochrome "F" monogram that
  replaces the old placeholder and stays legible down to 16px.

### Fixed
- Settings are now written atomically, and a config file that somehow gets
  corrupted falls back to defaults instead of stopping the app from launching.
- Checking Turbo status no longer briefly stalls the menu bar.
- A dictation finishing right after you disable freeflo is no longer typed out.
- A recording that fails to save no longer leaves a stray temporary file behind.

## [1.4.0] - 2026-08-01

### Added
- **Saved Prompts** — a new tab to bookmark dictations and typed prompts so you
  can keep and reuse the ones you rely on.

### Changed
- **Turbo mode** is simpler: it now runs a single lightweight on-device model and
  lets you pick a writing style — **Natural**, **Casual**, **Professional**, or
  **Formal** — instead of choosing between model tiers. Natural keeps your own
  voice with a light cleanup; the others restructure what you said into the
  register you want without dropping any of the meaning. Upgrading reclaims up to
  ~6.7 GB by removing the larger Turbo models that are no longer needed.

## [1.3.0] - 2026-07-30

### Added
- A guided 3-step onboarding flow shown on first launch — walks through the
  microphone and accessibility permissions and the push-to-talk hotkey so
  dictation works before you leave the setup.

### Changed
- Unified all preferences into a single **Settings** window instead of scattered
  menu items, and moved feedback into an in-app form so you can send a report
  without leaving the app.
- Landing page and design language reworked into Apple's marketing style, with a
  clearer download-and-open (quarantine bypass) walkthrough.

### Fixed
- Microphone no longer stays open after a dictation finishes — the yellow
  in-use indicator now clears instead of persisting.
- Processing indicator no longer gets stuck when a paste is slow; the paste step
  now times out so the app recovers on its own.

## [1.2.0] - 2026-07-23

### Changed
- Internal improvements and dependency updates.

## [1.1.0] - 2026-07-14

### Added
- Optional Google Drive backup of dictation history (off by default): syncs to
  a hidden, app-private folder in the user's own Drive so history survives a
  reinstall and follows them to another Mac. On-device dictation is unchanged
  and audio is never uploaded.
- Product landing page (`docs/`), served via GitHub Pages, with a live typing
  demo, "how it works", features, a privacy section, and a comparison vs Wispr Flow.
- "Free forever" messaging and a `$0` vs paid-alternative price comparison.
- `CHANGELOG.md`, `RELEASING.md`, and release automation scripts (`scripts/`).

### Changed
- Landing-page download button now points at `releases/latest/` so it always
  serves the newest published build automatically.

### Removed
- Decorative faux macOS menu-bar strip at the top of the landing page.
- The discontinued `brucke.tech` custom domain (site now lives on `github.io`).

## [1.0.0] - 2026-07-09

First public, open-source release.

### Added
- Offline macOS dictation powered by whisper.cpp — audio is transcribed
  entirely on-device; nothing is uploaded.
- Push-to-talk (hold **Left ⌥**) and toggle (tap **Right ⌥**) dictation modes.
- Types transcribed text at the cursor in any app, then restores the clipboard.
- Multilingual support: English, Hindi, Hinglish, Spanish, French, German,
  Chinese, Japanese, Arabic, Portuguese, and auto-detect.
- Local, searchable transcription history (SQLite), which can be disabled.
- Menu-bar app (rumps) with language and permission controls.
- Packaged, self-contained `.app` bundle (py2app) with `whisper-cli` and the
  GGML models embedded — published as `freeflo.zip`.
- Open-source project files: MIT `LICENSE`, `README.md`, `requirements.txt`,
  `.gitignore`.

[Unreleased]: https://github.com/rachitgupta6720/freeflo/compare/v1.4.1...HEAD
[1.4.1]: https://github.com/rachitgupta6720/freeflo/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/rachitgupta6720/freeflo/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/rachitgupta6720/freeflo/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/rachitgupta6720/freeflo/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/rachitgupta6720/freeflo/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/rachitgupta6720/freeflo/releases/tag/v1.0.0
