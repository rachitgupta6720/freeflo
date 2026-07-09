#!/usr/bin/env bash
#
# publish.sh — push everyday code/docs changes to GitHub (no new release).
#
# Use this for changes that DON'T need a new downloadable build: editing the
# landing page, docs, source cleanups, etc. GitHub Pages redeploys the site
# automatically after the push.
#
# For a new downloadable app version, use ./scripts/release.sh instead.
#
# Usage:
#   ./scripts/publish.sh "your commit message"
#   ./scripts/publish.sh            # uses a default message
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MSG="${1:-Update project}"

if git diff --quiet && git diff --cached --quiet; then
  echo "Nothing to publish — working tree is clean."
  exit 0
fi

git add -A
git commit -m "$MSG"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git push origin "$BRANCH"

echo "✓ Pushed to origin/$BRANCH"
echo "  If you changed docs/, the site rebuilds at https://rachitgupta6720.github.io/freeflo/ in ~1-2 min."
