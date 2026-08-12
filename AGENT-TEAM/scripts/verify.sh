#!/usr/bin/env bash
# Run the complete local equivalent of the two CI jobs from the repository root.

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pip-audit
uv run --locked pytest tests/ -q --cov=agent --cov=corpus \
  --cov-report=term-missing:skip-covered --cov-fail-under=75

npm audit --audit-level=moderate
npm test

verification_tmp="$(mktemp -d "${TMPDIR:-/tmp}/rwbookclub-verify.XXXXXX")"
cleanup() {
  rm -rf -- "$verification_tmp"
}
trap cleanup EXIT

OLIVER_DB_PATH="$verification_tmp/oliver-site-build.db" \
OLIVER_CORPUS_DIR="$verification_tmp/corpus" \
  uv run --locked --no-dev python -m agent.script.build_fixture_corpus
OLIVER_CORPUS_DIR="$verification_tmp/corpus" npm run build
