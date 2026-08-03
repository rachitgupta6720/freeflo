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
