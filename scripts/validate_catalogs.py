"""Check that the Codex and Claude Code catalogs list the same plugins and that
every plugin folder has both manifests with the same name and version."""

import json
import os
import sys


def local_path(entry):
    src = entry["source"]
    if isinstance(src, str):
        return src
    return src.get("path") if src.get("source") == "local" else None


def main():
    errors = []
    claude = json.load(open(".claude-plugin/marketplace.json"))
    codex = json.load(open(".agents/plugins/marketplace.json"))
    catalogs = {"claude": claude["plugins"], "codex": codex["plugins"]}
    paths = {}
    for kind, entries in catalogs.items():
        names = [e["name"] for e in entries]
        if len(names) != len(set(names)):
            errors.append(f"{kind} catalog: duplicate plugin names")
        paths[kind] = {e["name"]: local_path(e) for e in entries}
    if set(paths["claude"]) != set(paths["codex"]):
        errors.append("catalogs differ: claude-only %s, codex-only %s" % (
            sorted(set(paths["claude"]) - set(paths["codex"])),
            sorted(set(paths["codex"]) - set(paths["claude"]))))

    for name in sorted(set(paths["claude"]) | set(paths["codex"])):
        a, b = paths["claude"].get(name), paths["codex"].get(name)
        if a and b and os.path.normpath(a) != os.path.normpath(b):
            errors.append(f"{name}: catalogs point to different folders ({a} vs {b})")
        path = a or b
        if not path:
            continue  # external source: nothing local to check
        versions = {}
        for kind in (".claude-plugin", ".codex-plugin"):
            manifest = os.path.join(path, kind, "plugin.json")
            if not os.path.isfile(manifest):
                errors.append(f"{name}: missing {manifest}")
                continue
            data = json.load(open(manifest))
            if data.get("name") != name:
                errors.append(f"{name}: {manifest} has name {data.get('name')!r}")
            versions[kind] = data.get("version")
            if kind == ".codex-plugin":
                skills = data.get("skills")
                if not skills or not os.path.isdir(os.path.join(path, skills)):
                    errors.append(f"{name}: {manifest} has no valid skills path")
        if None in versions.values() or len(set(versions.values())) > 1:
            errors.append(f"{name}: manifest versions differ or are missing: {versions}")
        skills_dir = os.path.join(path, "skills")
        if os.path.isdir(skills_dir):
            for skill in sorted(os.listdir(skills_dir)):
                if not os.path.isfile(os.path.join(skills_dir, skill, "SKILL.md")):
                    errors.append(f"{name}: skills/{skill} has no SKILL.md")

    listed = {os.path.normpath(p) for kind in paths.values() for p in kind.values() if p}
    for folder in sorted(os.listdir("plugins")):
        if os.path.normpath(os.path.join("plugins", folder)) not in listed:
            errors.append(f"plugins/{folder} is not listed in the catalogs")

    if errors:
        print("\n".join(errors))
        return 1
    print(f"catalogs ok (codex + claude): {', '.join(sorted(paths['claude']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
