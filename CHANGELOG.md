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

_Changes landing here will roll into the next release. The app binary is
unchanged since 1.0.0; the entries below are website/repository changes._

### Added
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

[Unreleased]: https://github.com/rachitgupta6720/freeflo/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/rachitgupta6720/freeflo/releases/tag/v1.0.0
