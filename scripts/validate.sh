#!/bin/sh
# Validate the marketplace catalog and every plugin in it.
# Checks that each catalog entry points at an existing plugin folder with a
# manifest, and runs `claude plugin validate` when the Claude Code CLI is present.
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

python3 - <<'EOF'
import json, os, sys
catalog = json.load(open(".claude-plugin/marketplace.json"))
names, errors = set(), []
for entry in catalog["plugins"]:
    name, source = entry["name"], entry["source"]
    if name in names:
        errors.append(f"duplicate plugin name {name}")
    names.add(name)
    if not isinstance(source, str):
        continue  # external source (github/url): nothing local to check
    manifest = os.path.join(source, ".claude-plugin", "plugin.json")
    if not os.path.isfile(manifest):
        errors.append(f"{name}: missing {manifest}")
        continue
    data = json.load(open(manifest))
    if data.get("name") != name:
        errors.append(f"{name}: plugin.json name is {data.get('name')!r}")
    if not data.get("version"):
        errors.append(f"{name}: plugin.json has no version")
listed = {os.path.normpath(e["source"]) for e in catalog["plugins"] if isinstance(e["source"], str)}
for folder in sorted(os.listdir("plugins")):
    if os.path.normpath(os.path.join("plugins", folder)) not in listed:
        errors.append(f"plugins/{folder} is not listed in marketplace.json")
if errors:
    print("\n".join(errors))
    sys.exit(1)
print(f"catalog ok: {', '.join(sorted(names))}")
EOF

if command -v claude >/dev/null 2>&1; then
  claude plugin validate .
  for dir in plugins/*/; do
    claude plugin validate "$dir"
  done
else
  echo "claude CLI not found: skipped claude plugin validate"
fi
