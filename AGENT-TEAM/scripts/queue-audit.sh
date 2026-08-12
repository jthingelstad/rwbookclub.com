#!/usr/bin/env bash

set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated"; exit 1; }

repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
objectives=("objective:run" "objective:club" "objective:agent")
retired_controls='["proposal","approved","ready","needs-design","wip","needs-deploy","meta","needs-eval","needs-culture","approved-for-oliver","needs-approval","in-progress","oliver-autonomous"]'

echo "Objective queue — $repo"
for objective in "${objectives[@]}"; do
  printf '\n==> %s\n' "$objective"
  gh issue list --state open --limit 1000 --label "$objective" \
    --json number,title,labels,updatedAt \
    --jq '.[] | "  #\(.number)  [\([.labels[].name] | join(","))]  \(.title)  (\(.updatedAt[0:10]))"'
done

printf '\n==> Decisions waiting on Jamie\n'
gh issue list --state open --limit 1000 --label decision

printf '\n==> Missing or conflicting objective ownership\n'
issues="$(gh issue list --state open --limit 1000 --json number,title,labels)"
bad="$(printf '%s' "$issues" | jq -r '
  .[]
  | ([.labels[].name | select(startswith("objective:"))] | length) as $owners
  | select($owners != 1)
  | "  #\(.number)  owners=\($owners)  \(.title)"
')"
verdict=0
if [ -n "$bad" ]; then
  printf '%s\n' "$bad"
  verdict=1
fi

printf '\n==> Retired workflow controls on open issues\n'
retired="$(printf '%s' "$issues" | jq -r --argjson retired "$retired_controls" '
  .[]
  | ([.labels[].name | select(
      (. as $name | $retired | index($name)) != null
      or startswith("dispatch:")
      or startswith("legacy:")
    )]) as $controls
  | select($controls | length > 0)
  | "  #\(.number)  [\($controls | join(","))]  \(.title)"
')"
if [ -n "$retired" ]; then
  printf '%s\n' "$retired"
  verdict=1
fi

if [ "$verdict" -eq 0 ]; then
  echo "  ✓ every open issue has one objective owner and no retired workflow controls"
fi
exit "$verdict"
