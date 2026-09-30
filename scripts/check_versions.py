"""Every plugin change must come with a version bump and a changelog entry.

Compares the working tree with a base revision (argv[1], e.g. the PR base or the
previous commit on main). For every plugin folder with changes:
  - `version` in .claude-plugin/plugin.json and .codex-plugin/plugin.json must be
    equal to each other and differ from the version at the base;
  - CHANGELOG.md must have a `## <version>` section.
Prints `<plugin> <version>` for released versions (used to tag releases).
"""

import json
import os
import re
import subprocess
import sys


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def version_at(rev, path):
    proc = git("show", f"{rev}:{path}")
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout).get("version")


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1"
    if not base or set(base) == {"0"} or git("cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        print(f"base {base!r} not available; skipping version check")
        return 0
    errors, released = [], []
    for plugin in sorted(os.listdir("plugins")):
        root = f"plugins/{plugin}"
        changed = git("diff", "--name-only", base, "--", root).stdout.split()
        if not changed:
            continue
        versions = {}
        for kind in (".claude-plugin", ".codex-plugin"):
            path = f"{root}/{kind}/plugin.json"
            if os.path.isfile(path):
                with open(path) as fh:
                    versions[kind] = json.load(fh).get("version")
        current = set(versions.values())
        if len(current) != 1:
            errors.append(f"{plugin}: manifests disagree on version {versions}")
            continue
        version = current.pop()
        before = version_at(base, f"{root}/.claude-plugin/plugin.json")
        if before == version:
            errors.append(f"{plugin}: files changed ({len(changed)}) but version is still {version}; "
                          f"bump it in both manifests and add a CHANGELOG.md section")
            continue
        changelog = f"{root}/CHANGELOG.md"
        text = open(changelog).read() if os.path.isfile(changelog) else ""
        if not re.search(rf"^## {re.escape(version)}\b", text, re.M):
            errors.append(f"{plugin}: {changelog} has no '## {version}' section")
            continue
        released.append(f"{plugin} {version}")
    if errors:
        print("\n".join(errors))
        return 1
    for line in released:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
