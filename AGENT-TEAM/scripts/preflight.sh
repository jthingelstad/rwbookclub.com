#!/usr/bin/env bash
#
# Git preflight for AGENT-TEAM objective owners. Run from the repo root at the start
# of every run. Prints the working-tree state and a verdict; exits non-zero when
# the tree is in a state an automated run should NOT act on (fetch failure /
# dirty / detached / no upstream / ahead / behind / diverged). An automated
# owner should stop and report on a non-zero exit rather than pull/merge/rebase/stash.

set -euo pipefail

command -v git >/dev/null 2>&1 || { echo "git not found"; exit 2; }

if ! git fetch origin --prune >/dev/null 2>&1; then
  echo "  ✗ git fetch failed — remain read-only; upstream state is unknown."
  exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
echo "==> Preflight on branch: $branch"
git status --short --branch | sed 's/^/  /'

verdict=0

# Detached HEAD is never a safe base for an automated edit or push.
if [ "$branch" = "HEAD" ]; then
  echo "  ✗ DETACHED HEAD — remain read-only."
  verdict=1
fi

if [ "$branch" != "HEAD" ] && [ "$branch" != "main" ]; then
  echo "  ✗ automated publication is allowed only from main — remain read-only."
  verdict=1
fi

# Dirty worktree?
if [ -n "$(git status --porcelain)" ]; then
  echo "  ✗ worktree is DIRTY — stop and report (do not act on unexpected local changes)."
  verdict=1
fi

# Compare to upstream if one is set.
if upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
  ahead="$(git rev-list --count '@{u}..HEAD')"
  behind="$(git rev-list --count 'HEAD..@{u}')"
  if [ "$behind" -gt 0 ] && [ "$ahead" -gt 0 ]; then
    echo "  ✗ DIVERGED from $upstream ($ahead ahead, $behind behind) — stop and report."
    verdict=1
  elif [ "$behind" -gt 0 ]; then
    echo "  ✗ BEHIND $upstream by $behind — stop and report (do not pull from an automated run)."
    verdict=1
  elif [ "$ahead" -gt 0 ]; then
    echo "  ✗ AHEAD of $upstream by $ahead — remain read-only; never push pre-existing commits."
    verdict=1
  fi
else
  echo "  ✗ no upstream configured for $branch — remain read-only."
  verdict=1
fi

if [ "$verdict" -eq 0 ]; then
  echo "  ✓ clean and in sync — safe to work."
fi
exit "$verdict"
