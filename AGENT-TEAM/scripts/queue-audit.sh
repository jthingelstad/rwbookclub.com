#!/usr/bin/env bash

set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated"; exit 1; }

repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
objectives=("objective:run" "objective:club" "objective:agent")

echo "Objective queue — $repo"
for objective in "${objectives[@]}"; do
  printf '\n==> %s\n' "$objective"
  gh issue list --state open --limit 100 --label "$objective" \
    --json number,title,labels,updatedAt \
    --jq '.[] | "  #\(.number)  [\([.labels[].name] | join(","))]  \(.title)  (\(.updatedAt[0:10]))"'
done

printf '\n==> Decisions waiting on Jamie\n'
gh issue list --state open --limit 100 --label decision

printf '\n==> Missing or conflicting objective ownership\n'
issues="$(gh issue list --state open --limit 100 --json number,title,labels)"
bad="$(printf '%s' "$issues" | jq -r '
  .[]
  | ([.labels[].name | select(startswith("objective:"))] | length) as $owners
  | select($owners != 1)
  | "  #\(.number)  owners=\($owners)  \(.title)"
')"
if [ -n "$bad" ]; then
  printf '%s\n' "$bad"
  exit 1
fi
