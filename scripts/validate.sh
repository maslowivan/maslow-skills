#!/bin/sh
# Validate both catalogs (Codex and Claude Code) and every plugin in them:
# same plugins in both, manifests present with matching name and version,
# skills folders valid. Runs `claude plugin validate` when the CLI is present.
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

python3 "$root/scripts/validate_catalogs.py"

if command -v claude >/dev/null 2>&1; then
  claude plugin validate .
  for dir in plugins/*/; do
    claude plugin validate "$dir"
  done
else
  echo "claude CLI not found: skipped claude plugin validate"
fi
