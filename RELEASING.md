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
4. Rebuilds `freeflo.app` with py2app and repackages `freeflo.zip`.
5. Stamps the new version number and download size into `docs/index.html`.
6. Commits, creates the `v1.1.0` git tag, and pushes both.
7. Publishes a GitHub Release with `freeflo.zip` attached, using the CHANGELOG
   notes as the release body.

Because the landing page's download button points at
`releases/latest/download/freeflo.zip`, it starts serving the new build the
moment the release is published — no HTML edit needed.

**Before releasing:** jot what changed under `## [Unreleased]` in
`CHANGELOG.md` as you work. At release time those notes become the version's
notes automatically.

## Prerequisites for `release.sh`

- The Python venv with py2app: `.venv` (the script falls back to `python3`).
- whisper.cpp built at `~/whisper.cpp` with the GGML models (see the README) —
  the build embeds `whisper-cli` and the models into the `.app`.
- `gh` authenticated (`gh auth status`).

## Working with Claude Code

You can also just ask Claude in a session:

- *"push my changes"* → runs the everyday flow (`publish.sh`).
- *"cut a release, version 1.1.0"* → runs the release flow (`release.sh`).

Claude will read `CHANGELOG.md` for context on what shipped when.

## Why not fully automatic (CI)?

freeflo's download is a **native macOS build** that bundles a locally compiled
`whisper-cli` and ~600 MB of GGML models. Building that on GitHub's runners
would mean compiling whisper.cpp and downloading the models on every release —
slow and fragile. So the build stays on your Mac (where those already exist),
and `release.sh` automates everything around it. The *website* half already
deploys automatically via GitHub Pages on every push.

If you later want tag-triggered CI releases, that's possible with a macOS
runner — open an issue / ask Claude and we can add a workflow.

## History

See [CHANGELOG.md](CHANGELOG.md) for the full version-by-version history.
