#!/usr/bin/env bash

set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated"; exit 1; }

upsert() {
  local name="$1" color="$2" description="$3"
  if ! gh label edit "$name" --color "$color" --description "$description" >/dev/null 2>&1; then
    gh label create "$name" --color "$color" --description "$description"
  fi
}

remove() {
  gh label delete "$1" --yes >/dev/null 2>&1 || true
}

label_inventory="$(gh label list --limit 300 --json name)"

has_label() {
  jq -e --arg name "$1" 'any(.[]; .name == $name)' <<<"$label_inventory" >/dev/null
}

retire() {
  local old="$1" new="legacy:$1"
  local description="Legacy workflow label; do not use on open issues"

  if has_label "$old" && has_label "$new"; then
    echo "both $old and $new exist; resolve the duplicate before continuing" >&2
    exit 1
  elif has_label "$old"; then
    gh label edit "$old" --name "$new" --description "$description" >/dev/null
  elif has_label "$new"; then
    gh label edit "$new" --description "$description" >/dev/null
  fi
}

upsert "objective:run"   "D93F0B" "Owner: Run Oliver"
upsert "objective:club"  "D4A5FF" "Owner: Understand the Club"
upsert "objective:agent" "5319E7" "Owner: Improve Oliver"
upsert "decision"        "FBCA04" "Jamie must decide before the objective can continue"
upsert "blocked"         "B60205" "Waiting on an external dependency"
upsert "generated"       "FEF2C0" "Filed by an automated objective owner"

# Descriptive labels do not choose a worker.
upsert "bug"           "D73A4A" "Reproducible defect"
upsert "regression"    "B60205" "Worked before, now broken"
upsert "enhancement"   "A2EEEF" "New feature or capability"
upsert "eval"          "5319E7" "Behavioral measurement or evaluation"
upsert "operations"    "D93F0B" "Runtime, deploy, reliability, or observability"
upsert "culture"       "D4A5FF" "Club culture, history, member taste, or selection context"
upsert "documentation" "0075CA" "Documentation change"

# Preserve historical issue associations while removing obsolete control semantics
# from the live label names.
for label in approved-for-oliver needs-approval in-progress oliver-autonomous; do
  retire "$label"
done

for label in \
  proposal approved ready needs-design wip needs-deploy meta \
  dispatch:build dispatch:operations dispatch:evaluator dispatch:culture \
  dispatch:product dispatch:manager needs-eval needs-culture \
  "good first issue" "help wanted"; do
  remove "$label"
done

# A swallowed "label not found" is expected during cleanup; a surviving retired
# control is not. Verify the resulting catalog before claiming success.
label_inventory="$(gh label list --limit 300 --json name)"
for label in objective:run objective:club objective:agent decision blocked generated; do
  if ! has_label "$label"; then
    echo "required label missing after setup: $label" >&2
    exit 1
  fi
done
for label in \
  proposal approved ready needs-design wip needs-deploy meta \
  dispatch:build dispatch:operations dispatch:evaluator dispatch:culture \
  dispatch:product dispatch:manager needs-eval needs-culture \
  approved-for-oliver needs-approval in-progress oliver-autonomous; do
  if has_label "$label"; then
    echo "retired workflow label remains after setup: $label" >&2
    exit 1
  fi
done

echo "Objective labels are current."
