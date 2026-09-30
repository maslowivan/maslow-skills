"""Sparse-checkout profiles: SC-PROFILES.md in the repository root.

A profile names a subproject, the folders a task tree needs for it (always whole
folders — Git cone mode; files in the repository root and in every parent of an
included folder come automatically) and tags that help an agent pick the right
profile (name, package name, domains, anything the user adds).

`wsp sparse scan` builds the file with a deliberately generous heuristic: any
relative reference that leaves the subproject (`../web/x.ts`, `"../../shared"`,
`node ../../scripts/build.js`) includes the referenced folder whole, and
included folders are scanned in turn. A re-scan keeps everything the user added.
"""

import json
import os
import re

from . import gitutil, util
from .errors import WspError

FILE_NAME = "SC-PROFILES.md"
FORMAT_MARKER = "<!-- wsp:sc-profiles v1 -->"

MANIFESTS = {"package.json", "pyproject.toml", "setup.py", "go.mod", "Cargo.toml", "Gemfile", "composer.json",
             "deno.json", "wrangler.toml", "wrangler.json", "wrangler.jsonc"}
LOCKFILES = {"yarn.lock", "pnpm-lock.yaml", "package-lock.json", "npm-shrinkwrap.json", "bun.lock", "bun.lockb",
             "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock"}
SKIP_SEGMENTS = {"node_modules", "vendor", "dist", "build", ".next", ".astro", ".nuxt", "coverage", "fixtures",
                 "__fixtures__", "__mocks__", "testdata", "test-fixtures", ".git"}
SCAN_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".astro",
                   ".json", ".jsonc", ".json5", ".toml", ".yaml", ".yml", ".css", ".scss", ".sass", ".less",
                   ".html", ".py", ".sh", ".bash", ".zsh", ".rb", ".php", ".go", ".rs", ".sql", ".graphql"}
SCAN_NAMES = {"Makefile", "Dockerfile", "Procfile", ".yarnrc.yml", ".npmrc", ".env.example", ".dev.vars.example"}
MAX_SCAN_BYTES = 512 * 1024
ALWAYS_IF_PRESENT = (".husky", ".yarn")

_REL_REF = re.compile(r"(?<![\w.$@/-])((?:\.\./)+[\w@.+~\-]+(?:/[\w@.+~\-\[\]]+)*)")
_IMPORT_REF = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+|@import\s+(?:url\()?\s*)["'`]((?:\.\./)+[^"'`\s]+)["'`]""")
_TEST_FILE = re.compile(r"(^|/)(__tests__|tests?|e2e|fixtures|__fixtures__|__mocks__|mocks|stories)(/|$)|"
                        r"\.(test|spec|stories|e2e)\.[a-z0-9]+$", re.I)
_URL_HOST = re.compile(r"https?://([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)", re.I)
_HOSTNAME = re.compile(r"(?<![\w@.-])(\*\.)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?![\w-])", re.I)
# only keys that describe where a service is served (routes / custom domains), not vars pointing elsewhere
_DOMAIN_KEYS = re.compile(r"\bpattern\b|zone_name|custom_domain|\broutes?\b|\"domain\"|\bdomain\s*=|"
                          r"\bsite\s*[:=]|\bhostname\s*[:=]", re.I)
NOISE_HOSTS = (
    "github.com", "githubusercontent.com", "gitlab.com", "bitbucket.org", "npmjs.com", "npmjs.org", "yarnpkg.com",
    "localhost", "example.com", "example.org", "example.net", "cloudflare.com", "w3.org", "schema.org",
    "json-schema.org", "schemastore.org", "shields.io", "mozilla.org", "stackoverflow.com", "vitejs.dev",
    "vitest.dev", "astro.build", "nodejs.org", "typescriptlang.org", "react.dev", "reactjs.org", "nextjs.org",
    "vercel.com", "googleapis.com", "gstatic.com", "google.com", "unpkg.com", "jsdelivr.net", "cdnjs.cloudflare.com",
    "python.org", "pypi.org", "golang.org", "go.dev", "rust-lang.org", "crates.io", "docker.com", "wikipedia.org",
    "opensource.org", "semver.org", "keepachangelog.com", "conventionalcommits.org", "anthropic.com", "openai.com",
    "claude.com", "code.claude.com", "microsoft.com", "apple.com", "tailwindcss.com", "eslint.org", "prettier.io",
    "playwright.dev", "jestjs.io", "storybook.js.org", "webpack.js.org", "babeljs.io", "sentry.io", "npm.im",
)
DOMAIN_FILES = ("wrangler.toml", "wrangler.json", "wrangler.jsonc", "netlify.toml", "vercel.json", "fly.toml",
                "CNAME", "app.yaml", "render.yaml", "railway.json")
DOC_FILES = ("README.md", "AGENTS.md", "CLAUDE.md", "readme.md", "Readme.md")
MAX_DOMAINS = 15


# --- file format -------------------------------------------------------------------
def profiles_path(repo_root):
    return os.path.join(repo_root, FILE_NAME)


def parse(text):
    """Parse SC-PROFILES.md into {name: {"path", "folders", "tags", "notes"}} (ordered)."""
    profiles, current = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            name = line[3:].strip().strip("`")
            current = profiles.setdefault(name, {"path": None, "folders": [], "tags": [], "notes": []})
            continue
        if current is None or not line.startswith("- "):
            continue
        key, sep, value = line[2:].partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        items = [v.strip().strip("`").strip() for v in value.split(",")]
        items = [v for v in items if v]
        if key == "path":
            current["path"] = items[0].rstrip("/") if items else None
        elif key == "folders":
            current["folders"].extend(_norm_folder(v) for v in items if _norm_folder(v))
        elif key == "tags":
            current["tags"].extend(items)
        elif key == "notes":
            current["notes"].append(value.strip())
    for prof in profiles.values():
        prof["folders"] = _dedupe(prof["folders"])
        prof["tags"] = _dedupe(prof["tags"])
    return profiles


def render(profiles):
    lines = [
        "# Sparse-checkout profiles",
        "",
        FORMAT_MARKER,
        "",
        "Subprojects of this repository for sparse task worktrees (task-workspaces:",
        "`wsp ensure --task <id> --repo <repo> --profile <name>`). A task tree then contains only",
        "the listed folders, always whole folders; files in the repository root and in every",
        "parent folder of an included folder are always present.",
        "",
        "Generated by `wsp sparse scan`. Edit freely: add folders a subproject needs and tags",
        "(team names, product names, domains, services) that help an agent choose the right",
        "profile. A new scan keeps your additions.",
        "",
    ]
    for name, prof in profiles.items():
        lines.append(f"## {name}")
        lines.append("")
        if prof.get("path"):
            lines.append(f"- path: `{prof['path']}`")
        lines.append("- folders: " + ", ".join(f"`{f}`" for f in prof["folders"]))
        lines.append("- tags: " + ", ".join(prof["tags"]))
        for note in prof.get("notes") or []:
            lines.append(f"- notes: {note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load(repo_root, text=None):
    path = profiles_path(repo_root)
    if text is None:
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    return parse(text)


def load_at(repo_path, rev=None):
    """Profiles as committed at `rev` (the task's base), else the working copy."""
    if rev:
        proc = gitutil.git(repo_path, "show", f"{rev}:{FILE_NAME}", check=False)
        if proc.returncode == 0:
            return parse(proc.stdout), f"{rev}:{FILE_NAME}"
    profiles = load(repo_path)
    return profiles, (profiles_path(repo_path) if profiles is not None else None)


def resolve(profiles, names, source=None):
    """Union of the folders of the named profiles; unknown names raise with suggestions."""
    if profiles is None:
        raise WspError("SPARSE_PROFILE_UNKNOWN", f"{FILE_NAME} not found; run `wsp sparse scan` in the repository "
                       "(or /task-workspaces:setup-sparse-checkout)", source=source)
    folders = []
    for name in names:
        if name not in profiles:
            raise WspError("SPARSE_PROFILE_UNKNOWN", f"profile '{name}' is not in {FILE_NAME}",
                           known=sorted(profiles), suggestions=match(profiles, name)[:5], source=source)
        folders.extend(profiles[name]["folders"])
    return _dedupe(folders)


def match(profiles, query):
    """Rank profiles by how many query words appear in name, path, tags and folders."""
    words = [w for w in re.split(r"[\s,;/]+", query.lower()) if len(w) > 1]
    scored = []
    for name, prof in profiles.items():
        hay = " ".join([name, prof.get("path") or ""] + prof["tags"] + prof["folders"]).lower()
        score = sum(3 if w == name.lower() else 1 for w in words if w in hay)
        if score:
            scored.append((score, name))
    return [n for _, n in sorted(scored, key=lambda x: (-x[0], x[1]))]


def consumers(profiles, folder):
    """Profiles whose folders include `folder` (or one of its parents/children)."""
    folder = _norm_folder(folder)
    out = []
    for name, prof in (profiles or {}).items():
        for f in prof["folders"]:
            if f == folder or folder.startswith(f + "/") or f.startswith(folder + "/"):
                out.append(name)
                break
    return out


def _norm_folder(value):
    value = value.strip().strip("`").strip().strip("/")
    if not value or value in (".", "..") or value.startswith("../") or "/../" in f"/{value}/":
        return None
    return value


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --- scanning --------------------------------------------------------------------------
def _tracked(repo_root):
    raw = util.run(["git", "-C", repo_root, "ls-files", "-z"]).stdout
    return [p for p in raw.split("\0") if p]


def _skip(path):
    return any(seg in SKIP_SEGMENTS for seg in path.split("/"))


def find_subprojects(repo_root, files=None):
    files = files if files is not None else _tracked(repo_root)
    candidates, locks = set(), set()
    for rel in files:
        if _skip(rel):
            continue
        base = os.path.basename(rel)
        d = os.path.dirname(rel)
        if base in MANIFESTS and d:
            candidates.add(d)
        if base in LOCKFILES and d:
            locks.add(d)
    lock_dirs = candidates & locks
    subprojects = []
    for d in sorted(candidates):
        inside_lock_dir = any(d != l and d.startswith(l + "/") for l in lock_dirs)
        if not inside_lock_dir:
            subprojects.append(d)
    return subprojects


def _is_under(path, folder):
    return path == folder or path.startswith(folder + "/")


def _folder_for(target, unit, subprojects, containers):
    """Folder to include for a reference from `unit` to `target` (both repo-relative)."""
    owners = [s for s in subprojects if _is_under(target, s)]
    if owners:
        return max(owners, key=len)
    unit_parts = unit.split("/") if unit else []
    target_parts = target.split("/")
    common = 0
    while common < min(len(unit_parts), len(target_parts)) and unit_parts[common] == target_parts[common]:
        common += 1
    if common >= len(target_parts) - 1:
        return None  # a file directly in a parent folder: cone mode includes it already
    depth = common + 1
    folder = "/".join(target_parts[:depth])
    # descend through container folders (they hold other subprojects) so that
    # `../../cloudflare/common/x` includes `cloudflare/common`, not all of `cloudflare`
    while folder in containers and depth < len(target_parts) - 1:
        depth += 1
        folder = "/".join(target_parts[:depth])
    return folder


def _references(repo_root, files_in_folder, strict=False):
    """Yield repo-relative paths referenced with ../ from files in a folder.

    Non-strict (the subproject itself): any ../ path in code and config files.
    Strict (folders pulled in by a reference): only import/require/@import
    statements in non-test files, so that shared code does not drag in every
    consumer its tests or scripts mention."""
    pattern = _IMPORT_REF if strict else _REL_REF
    for rel in files_in_folder:
        if strict and _TEST_FILE.search(rel):
            continue
        base = os.path.basename(rel)
        ext = os.path.splitext(base)[1].lower()
        if ext not in SCAN_EXTENSIONS and base not in SCAN_NAMES:
            continue
        full = os.path.join(repo_root, rel)
        try:
            if os.path.getsize(full) > MAX_SCAN_BYTES:
                continue
            with open(full, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for m in pattern.finditer(text):
            target = os.path.normpath(os.path.join(os.path.dirname(rel), m.group(1)))
            if target.startswith("..") or os.path.isabs(target) or _skip(target):
                continue
            yield target


def _package_name_deps(repo_root, folder, names_to_dirs):
    """Workspace dependencies by package name (e.g. "@org/utils": "workspace:*")."""
    pkg = util.read_json(os.path.join(repo_root, folder, "package.json")) or {}
    out = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for dep in (pkg.get(key) or {}):
            if dep in names_to_dirs and names_to_dirs[dep] != folder:
                out.append(names_to_dirs[dep])
    return out


def folders_for(repo_root, unit, subprojects, files, max_folders=200):
    by_folder = {}
    containers = set()
    for s in subprojects:
        parts = s.split("/")
        for i in range(1, len(parts)):
            containers.add("/".join(parts[:i]))
    names_to_dirs = {}
    for s in subprojects:
        pkg = util.read_json(os.path.join(repo_root, s, "package.json")) or {}
        if pkg.get("name"):
            names_to_dirs[pkg["name"]] = s

    def files_under(folder):
        if folder not in by_folder:
            by_folder[folder] = [f for f in files if _is_under(f, folder) and not _skip(f)]
        return by_folder[folder]

    included = [unit]
    queue = [unit]
    reasons = {}
    while queue and len(included) < max_folders:
        folder = queue.pop(0)
        found = []
        for target in _references(repo_root, files_under(folder), strict=folder != unit):
            if _is_under(target, folder):
                continue
            inc = _folder_for(target, folder, subprojects, containers)
            if inc:
                found.append((inc, target))
        for dep_dir in (_package_name_deps(repo_root, folder, names_to_dirs) if folder == unit or folder in subprojects
                        else []):
            found.append((dep_dir, f"{dep_dir}/package.json"))
        for inc, target in found:
            if any(_is_under(inc, f) for f in included):
                continue
            # a broader folder replaces narrower ones already included
            included = [f for f in included if not _is_under(f, inc)] + [inc]
            reasons.setdefault(inc, target)
            queue.append(inc)
    for extra in ALWAYS_IF_PRESENT:
        if os.path.isdir(os.path.join(repo_root, extra)) and any(f.startswith(extra + "/") for f in files):
            if extra not in included:
                included.append(extra)
                reasons.setdefault(extra, "always included when present")
    ordered = [unit] + sorted(f for f in included if f != unit)
    return ordered, reasons


def _clean_host(host):
    host = host.lower().strip(".").lstrip("*.")
    if not host or "." not in host or host.replace(".", "").isdigit():
        return None
    if host.endswith((".js", ".ts", ".json", ".md", ".css", ".html", ".png", ".svg", ".toml", ".yaml", ".yml",
                      ".jsonc", ".mjs", ".cjs", ".tsx", ".jsx", ".lock", ".txt", ".sh", ".py", ".xml", ".csv",
                      ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".map", ".wasm", ".env", ".local", ".vars")):
        return None
    for noise in NOISE_HOSTS:
        if host == noise or host.endswith("." + noise):
            return None
    return host


def _registrable(host):
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def served_domains(repo_root, unit, files):
    """Hosts a subproject is served on, from route / custom-domain configs."""
    hosts = []
    for rel in files:
        if not _is_under(rel, unit) or _skip(rel):
            continue
        base = os.path.basename(rel)
        is_cfg = bool(re.match(r"^(astro|next|nuxt|svelte|remix)\.config\.", base)) and os.path.dirname(rel) == unit
        if base not in DOMAIN_FILES and not is_cfg:
            continue
        try:
            with open(os.path.join(repo_root, rel), encoding="utf-8", errors="ignore") as fh:
                text = fh.read(MAX_SCAN_BYTES)
        except OSError:
            continue
        if base == "CNAME":
            hosts.extend(t for t in text.split() if t)
            continue
        for line in text.splitlines():
            if _DOMAIN_KEYS.search(line):
                hosts.extend(m.group(2) for m in _HOSTNAME.finditer(line))
                hosts.extend(m.group(1) for m in _URL_HOST.finditer(line))
    return _dedupe(c for c in (_clean_host(h) for h in hosts) if c)


def doc_domains(repo_root, unit, own_registrable):
    """Hosts mentioned in the subproject's README/AGENTS/CLAUDE that belong to the repo's own domains."""
    hosts = []
    for name in DOC_FILES:
        path = os.path.join(repo_root, unit, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read(MAX_SCAN_BYTES)
        hosts.extend(m.group(1) for m in _URL_HOST.finditer(text))
        hosts.extend(m.group(2) for m in _HOSTNAME.finditer(text))
    out = []
    for h in hosts:
        c = _clean_host(h)
        if not c:
            continue
        if own_registrable and _registrable(c) not in own_registrable:
            continue
        if not own_registrable and not re.search(r"^https?://", h) and "." not in c:
            continue
        out.append(c)
    return _dedupe(out)


def domains_for(repo_root, unit, files, own_registrable=None):
    served = served_domains(repo_root, unit, files)
    docs = doc_domains(repo_root, unit, own_registrable or set())
    return _dedupe(served + docs)[:MAX_DOMAINS]


def tags_for(repo_root, unit, name, domains):
    tags = [name]
    base = os.path.basename(unit)
    if base != name:
        tags.append(base)
    pkg = util.read_json(os.path.join(repo_root, unit, "package.json")) or {}
    if pkg.get("name"):
        tags.append(pkg["name"])
    for cfg in ("wrangler.jsonc", "wrangler.json"):
        path = os.path.join(repo_root, unit, cfg)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    m = re.search(r'"name"\s*:\s*"([^"]+)"', fh.read())
                if m:
                    tags.append(m.group(1))
            except OSError:
                pass
    toml = os.path.join(repo_root, unit, "wrangler.toml")
    if os.path.isfile(toml):
        with open(toml, encoding="utf-8", errors="ignore") as fh:
            m = re.search(r'^\s*name\s*=\s*"([^"]+)"', fh.read(), re.M)
        if m:
            tags.append(m.group(1))
    tags.extend(domains)
    return _dedupe(tags)


def _profile_names(subprojects):
    names = {}
    counts = {}
    for s in subprojects:
        base = util.slugify(os.path.basename(s), 64) or "root"
        counts[base] = counts.get(base, 0) + 1
    for s in subprojects:
        base = util.slugify(os.path.basename(s), 64) or "root"
        names[s] = base if counts[base] == 1 else util.slugify(s.replace("/", "-"), 64)
    return names


def scan(path):
    """Detect subprojects and build profiles for the repository containing `path`."""
    if not gitutil.is_repo(path):
        raise WspError("SOURCE_INVALID", "not a Git repository", path=path)
    repo_root = gitutil.toplevel(path)
    files = _tracked(repo_root)
    subprojects = find_subprojects(repo_root, files)
    names = _profile_names(subprojects)
    detected = {}
    details = {}
    own = set()
    for unit in subprojects:
        own.update(_registrable(h) for h in served_domains(repo_root, unit, files))
    own.discard("workers.dev")
    for unit in subprojects:
        folders, reasons = folders_for(repo_root, unit, subprojects, files)
        domains = domains_for(repo_root, unit, files, own)
        name = names[unit]
        detected[name] = {"path": unit, "folders": folders, "tags": tags_for(repo_root, unit, name, domains),
                          "notes": []}
        details[name] = {"reasons": reasons, "domains": domains}
    return repo_root, detected, details


def merge(existing, detected):
    """Keep user edits: union of folders and tags; profiles the scan no longer finds stay."""
    existing = existing or {}
    merged = {}
    report = {"added": [], "updated": [], "kept_manual": []}
    for name, prof in detected.items():
        old = existing.get(name)
        if old:
            new = {"path": old.get("path") or prof["path"],
                   "folders": _dedupe(prof["folders"] + old["folders"]),
                   "tags": _dedupe(prof["tags"] + old["tags"]),
                   "notes": [n for n in old.get("notes") or [] if "not found by the last scan" not in n]}
            if new["folders"] != old["folders"] or new["tags"] != old["tags"]:
                report["updated"].append(name)
            merged[name] = new
        else:
            merged[name] = prof
            report["added"].append(name)
    for name, old in existing.items():
        if name not in merged:
            notes = [n for n in old.get("notes") or [] if "not found by the last scan" not in n]
            if old.get("path"):
                notes.append("not found by the last scan; check the path or remove this profile")
            merged[name] = {**old, "notes": notes}
            report["kept_manual"].append(name)
    ordered = dict(sorted(merged.items(), key=lambda kv: (kv[1].get("path") or "~", kv[0])))
    return ordered, report


def scan_and_write(path, write=False):
    repo_root, detected, details = scan(path)
    existing = load(repo_root)
    merged, report = merge(existing, detected)
    text = render(merged)
    target = profiles_path(repo_root)
    changed = True
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as fh:
            changed = fh.read() != text
    if write and changed:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
    return {"repo_root": repo_root, "file": target, "written": bool(write and changed), "changed": changed,
            "profiles": merged, "report": report, "details": details,
            "preview": text if not write else None}


# --- applying to a tree ------------------------------------------------------------------
def worktree_config_enabled(repo_path):
    proc = gitutil.git(repo_path, "config", "--get", "extensions.worktreeConfig", check=False)
    return proc.stdout.strip().lower() == "true"


def apply(tree_path, folders):
    """Restrict a worktree created with --no-checkout to the given folders (cone mode)."""
    if not folders:
        raise WspError("USAGE", "no folders to check out")
    gitutil.git(tree_path, "sparse-checkout", "set", "--cone", *folders)
    # the worktree was added with --no-checkout: fill index and files, honouring the cone
    gitutil.git(tree_path, "read-tree", "-mu", "HEAD")


def add(tree_path, folders):
    gitutil.git(tree_path, "sparse-checkout", "add", *folders)


def current(tree_path):
    proc = gitutil.git(tree_path, "sparse-checkout", "list", check=False)
    if proc.returncode != 0:
        return None
    return [l for l in proc.stdout.splitlines() if l.strip()]


def to_json(value):
    return json.dumps(value) if value is not None else None
