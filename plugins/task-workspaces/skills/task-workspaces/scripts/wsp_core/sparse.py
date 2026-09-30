"""Sparse-checkout profiles: .wsp/SC-PROFILES.md in the repository.

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
WSP_DIR = ".wsp"                                   # wsp's folder in a repository (also .wsp/profile.json)
FILE_PATH = f"{WSP_DIR}/{FILE_NAME}"               # .wsp/SC-PROFILES.md
LEGACY_PATH = FILE_NAME                            # repository root, before 0.3
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
# agent instructions and skills in the repository root belong to every profile
AGENT_DIRS = (".claude", ".agents", ".codex", ".cursor", ".gemini", ".windsurf", ".github/instructions")
ACTIVITY_DAYS = 28

_AUTO = re.compile(r"<!--\s*wsp:auto\s+(.*?)\s*-->")
_REL_REF = re.compile(r"(?<![\w.$@/-])((?:\.\./)+[\w@.+~\-]+(?:/[\w@.+~\-\[\]]+)*)")
_IMPORT_REF = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+|@import\s+(?:url\()?\s*)["'`]((?:\.\./)+[^"'`\s]+)["'`]""")
_TEST_FILE = re.compile(r"(^|/)(__tests__|tests?|e2e|fixtures|__fixtures__|__mocks__|mocks|stories)(/|$)|"
                        r"\.(test|spec|stories|e2e)\.[a-z0-9]+$", re.I)
_URL_HOST = re.compile(r"https?://([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)", re.I)
_HOSTNAME = re.compile(r"(?<![\w@.-])(\*\.)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?![\w-])", re.I)
# only keys that describe where a service is served (routes / custom domains), not vars pointing elsewhere
_DOMAIN_KEYS = re.compile(r"\bpattern\b|custom_domain|\broutes?\b|\"domain\"|\bdomain\s*=", re.I)
_SITE_KEY = re.compile(r"\bsite\s*[:=]", re.I)
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
DOMAIN_FILES = ("netlify.toml", "vercel.json", "fly.toml", "CNAME", "app.yaml", "render.yaml", "railway.json")
_WRANGLER = re.compile(r"^wrangler(\.[\w-]+)?\.(toml|json|jsonc)$")
_FRAMEWORK_CFG = re.compile(r"^(astro|next|nuxt|svelte|remix)\.config\.")
_ZONE_FIELD = re.compile(r"""["']?zone_(?:name|id)["']?\s*[:=]\s*["'][^"']*["']""")
# hosts of infrastructure providers are origins, not where a subproject is served
INFRA_SUFFIXES = ("amazonaws.com", "cloudfront.net", "elasticbeanstalk.com", "azurewebsites.net", "herokuapp.com",
                  "workers.dev", "pages.dev", "vercel.app", "netlify.app", "fly.dev", "run.app", "appspot.com",
                  "googleusercontent.com", "firebaseapp.com", "web.app", "cloudflareaccess.com", "internal", "local")
_VARIANT = re.compile(r"(^|[.-])(dev|development|staging|stage|stg|preview|pre-release|prerelease|cftest|test|qa|"
                      r"sandbox|uat|canary|beta)([.-]|$)")
_EMBEDDED = re.compile(r"^(?:www\.)?([a-z0-9-]+\.[a-z]{2,6})\.(?:[a-z0-9-]+\.)*"
                       r"(?:pre-release|prerelease|preview|staging|stage|dev|cftest|test)\.")
DOC_FILES = ("README.md", "AGENTS.md", "CLAUDE.md", "readme.md", "Readme.md")
MAX_DOMAINS = 15


# --- file format -------------------------------------------------------------------
def profiles_path(repo_root):
    return os.path.join(repo_root, FILE_PATH)


def legacy_path(repo_root):
    return os.path.join(repo_root, LEGACY_PATH)


def parse(text):
    """Parse SC-PROFILES.md into {name: {"path", "folders", "tags", "notes"}} (ordered)."""
    profiles, current = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        auto = _AUTO.search(line)
        if auto and current is not None:
            current["auto"] = json.loads(auto.group(1))
            continue
        if line.startswith("## "):
            name = line[3:].strip().strip("`")
            current = profiles.setdefault(name, {"path": None, "about": None, "folders": [], "tags": [],
                                                 "notes": []})
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
        elif key == "about":
            current["about"] = value.strip() or None
        elif key == "folders":
            current["folders"].extend(_norm_folder(v) for v in items if _norm_folder(v))
        elif key == "tags":
            current["tags"].extend(items)
        elif key == "notes":
            current["notes"].append(value.strip())
    for prof in profiles.values():
        prof["folders"] = _dedupe(prof["folders"])
        prof["tags"] = dedupe_tags(prof["tags"])
    return profiles


def _tag_key(tag):
    return re.sub(r"[^a-z0-9.@]+", " ", tag.lower()).strip()


def dedupe_tags(tags, drop=()):
    """Case/punctuation-insensitive dedupe ('ugc-indexer' == 'UGC Indexer'); the readable
    spelling wins. Tags equal to a name in `drop` (the profile name) are removed."""
    dropped = {_tag_key(d) for d in drop}
    chosen, order = {}, []
    for tag in tags:
        tag = tag.strip()
        key = _tag_key(tag)
        if not key or key in dropped:
            continue
        if key not in chosen:
            chosen[key] = tag
            order.append(key)
        elif (" " in tag or tag != tag.lower()) and not (" " in chosen[key] or chosen[key] != chosen[key].lower()):
            chosen[key] = tag
    return [chosen[k] for k in order]


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
        "Generated by `wsp sparse scan`; sorted by recent commit activity. Edit freely: add",
        "folders a subproject needs, tags (team, product, service names, domains) and a short",
        "`about` that help an agent choose the right profile. A new scan keeps your additions",
        "(the hidden `wsp:auto` comment records what the scanner added; leave it in place).",
        "",
    ]
    for name, prof in profiles.items():
        lines.append(f"## {name}")
        lines.append("")
        if prof.get("path"):
            lines.append(f"- path: `{prof['path']}`")
        if prof.get("about"):
            lines.append(f"- about: {prof['about']}")
        lines.append("- folders: " + ", ".join(f"`{f}`" for f in prof["folders"]))
        lines.append("- tags: " + ", ".join(prof["tags"]))
        for note in prof.get("notes") or []:
            lines.append(f"- notes: {note}")
        if prof.get("auto"):
            lines.append(f"<!-- wsp:auto {json.dumps(prof['auto'], ensure_ascii=False, separators=(',', ':'))} -->")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load(repo_root, text=None):
    path = profiles_path(repo_root)
    if text is None:
        if not os.path.isfile(path):
            path = legacy_path(repo_root)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    return parse(text)


def load_at(repo_path, rev=None):
    """Profiles as committed at `rev` (the task's base), else the working copy."""
    if rev:
        for rel in (FILE_PATH, LEGACY_PATH):
            proc = gitutil.git(repo_path, "show", f"{rev}:{rel}", check=False)
            if proc.returncode == 0:
                return parse(proc.stdout), f"{rev}:{rel}"
    profiles = load(repo_path)
    if profiles is None:
        return None, None
    path = profiles_path(repo_path)
    return profiles, (path if os.path.isfile(path) else legacy_path(repo_path))


def resolve(profiles, names, source=None):
    """Union of the folders of the named profiles; unknown names raise with suggestions."""
    if profiles is None:
        raise WspError("SPARSE_PROFILE_UNKNOWN", f"{FILE_PATH} not found; run `wsp sparse scan` in the repository "
                       "(or /task-workspaces:setup-sparse-checkout)", source=source)
    folders = []
    for name in names:
        if name not in profiles:
            raise WspError("SPARSE_PROFILE_UNKNOWN", f"profile '{name}' is not in {FILE_PATH}",
                           known=sorted(profiles), suggestions=match(profiles, name)[:5], source=source)
        folders.extend(profiles[name]["folders"])
    return _dedupe(folders + [WSP_DIR])  # wsp's own folder is part of every sparse tree


def _tokens(text):
    """Whole words; domains and package names are kept whole as well as split."""
    text = text.lower()
    out = set(t for t in re.split(r"[\s,;/()]+", text) if t)
    for t in list(out):
        out.update(p for p in re.split(r"[^a-z0-9]+", t) if p)
    return out


def match(profiles, query):
    """Rank profiles by whole-word matches: profile name and tags weigh more than paths."""
    words = [w for w in re.split(r"[\s,;/]+", query.lower()) if len(w) > 1]
    scored = []
    for name, prof in profiles.items():
        name_tokens = _tokens(name)
        tag_tokens = _tokens(" ".join(prof["tags"]))
        path_tokens = _tokens(" ".join([prof.get("path") or ""] + prof["folders"]))
        score = 0
        for w in words:
            if w == name.lower():
                score += 6
            elif w in name_tokens:
                score += 4
            elif w in tag_tokens:
                score += 3
            elif w in path_tokens:
                score += 1
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


def _hidden(path):
    return any(seg.startswith(".") for seg in path.split("/"))


def find_subprojects(repo_root, files=None):
    files = files if files is not None else _tracked(repo_root)
    candidates, locks = set(), set()
    for rel in files:
        if _skip(rel):
            continue
        base = os.path.basename(rel)
        d = os.path.dirname(rel)
        if _hidden(d):
            continue  # .claude/, .agents/, .github/ ... hold tooling, not subprojects
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
    for extra in ALWAYS_IF_PRESENT + AGENT_DIRS:
        if os.path.isdir(os.path.join(repo_root, extra)) and any(f.startswith(extra + "/") for f in files):
            if not any(_is_under(extra, f) for f in included):
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


def _host_ok(host):
    if not host:
        return False
    return not any(host == sfx or host.endswith("." + sfx) for sfx in INFRA_SUFFIXES)


def _prefer_public(hosts):
    """Production hosts first; dev/staging/preview variants only when nothing else is known.
    Brand domains embedded in preview hosts (www.shop.com.pre-release.example.org) count as public."""
    public, variants = [], []
    for h in hosts:
        h = h[4:] if h.startswith("www.") else h  # www.x and x are the same site
        m = _EMBEDDED.match(h)
        if m:
            public.append(m.group(1))
        labels = h.rsplit(".", 1)[0]  # the TLD itself (".dev") is not a dev variant
        (variants if _VARIANT.search(labels) else public).append(h)
    public = [h for h in _dedupe(public) if _host_ok(h)]
    return public if public else [h for h in _dedupe(variants) if _host_ok(h)]


def served_domains(repo_root, unit, files):
    """Hosts a subproject is served on, from route / custom-domain configs."""
    hosts = []
    for rel in files:
        if not _is_under(rel, unit) or _skip(rel):
            continue
        base = os.path.basename(rel)
        is_wrangler = bool(_WRANGLER.match(base))
        is_framework = bool(_FRAMEWORK_CFG.match(base)) and os.path.dirname(rel) == unit
        if base not in DOMAIN_FILES and not is_wrangler and not is_framework:
            continue
        try:
            with open(os.path.join(repo_root, rel), encoding="utf-8", errors="ignore") as fh:
                text = fh.read(MAX_SCAN_BYTES)
        except OSError:
            continue
        if base == "CNAME":
            hosts.extend(t for t in text.split() if t)
            continue
        keys = _SITE_KEY if is_framework else _DOMAIN_KEYS
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("#", "//", "/*", "*")):
                continue  # commented-out routes are not served
            if keys.search(line):
                line = _ZONE_FIELD.sub("", line)  # the zone is not a served host
                hosts.extend(m.group(2) for m in _HOSTNAME.finditer(line))
                hosts.extend(m.group(1) for m in _URL_HOST.finditer(line))
    return _prefer_public([c for c in (_clean_host(h) for h in hosts) if c])


def doc_domains(repo_root, unit, own_registrable):
    """Hosts mentioned in the subproject's README/AGENTS/CLAUDE that belong to the repo's own domains."""
    url_hosts, bare_hosts = [], []
    for name in DOC_FILES:
        path = os.path.join(repo_root, unit, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read(MAX_SCAN_BYTES)
        url_hosts.extend(m.group(1) for m in _URL_HOST.finditer(text))
        bare_hosts.extend(m.group(2) for m in _HOSTNAME.finditer(text))
    out = []
    # links (https://host) count when they belong to the repo's own domains, or when the repo
    # declares none; bare names only when they belong to the repo's own domains (otherwise
    # file names such as lib.rs or schema.sql would look like hosts)
    # documentation mentions brand sites and zones of the whole company all the time; only
    # specific service hosts (sub.domain.tld) are taken, never a bare domain or www.
    def specific(c):
        return c.count(".") >= 2 and not c.startswith("www.")
    for h in url_hosts:
        c = _clean_host(h)
        if c and specific(c) and (not own_registrable or _registrable(c) in own_registrable):
            out.append(c)
    for h in bare_hosts:
        c = _clean_host(h)
        if c and specific(c) and own_registrable and _registrable(c) in own_registrable:
            out.append(c)
    return _dedupe(out)


def domains_for(repo_root, unit, files, own_registrable=None):
    """Served hosts from configs; documentation hosts only for subprojects without any
    (docs mention other services' domains too often)."""
    served = served_domains(repo_root, unit, files)
    if served:
        return served[:MAX_DOMAINS]
    return _prefer_public(doc_domains(repo_root, unit, own_registrable or set()))[:MAX_DOMAINS]


def about_for(repo_root, unit):
    """Short description from the package manifest; agents refine it (setup-sparse-checkout)."""
    pkg = util.read_json(os.path.join(repo_root, unit, "package.json")) or {}
    desc = str(pkg.get("description") or "").strip().rstrip(".")
    words = desc.split()
    if 3 <= len(words) <= 20:
        return desc
    return None


def tags_for(repo_root, unit, name, domains):
    tags = []
    base = os.path.basename(unit)
    if base != name:
        tags.append(base)
    pkg = util.read_json(os.path.join(repo_root, unit, "package.json")) or {}
    if pkg.get("name"):
        tags.append(pkg["name"])
    unit_dir = os.path.join(repo_root, unit)
    for cfg in sorted(os.listdir(unit_dir)) if os.path.isdir(unit_dir) else []:
        if not _WRANGLER.match(cfg):
            continue
        try:
            with open(os.path.join(unit_dir, cfg), encoding="utf-8", errors="ignore") as fh:
                text = fh.read(MAX_SCAN_BYTES)
        except OSError:
            continue
        m = (re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.M) if cfg.endswith(".toml")
             else re.search(r'"name"\s*:\s*"([^"]+)"', text))
        if m:
            tags.append(m.group(1))
    tags.extend(domains)
    return dedupe_tags(tags, drop=[name])


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
        detected[name] = {"path": unit, "about": about_for(repo_root, unit), "folders": folders,
                          "tags": tags_for(repo_root, unit, name, domains), "notes": []}
        details[name] = {"reasons": reasons, "domains": domains}
    return repo_root, detected, details


def activity(repo_root, subprojects, days=ACTIVITY_DAYS):
    """Commits in the last `days` touching each subproject (a commit counts once per subproject)."""
    proc = util.run(["git", "-C", repo_root, "log", f"--since={days} days ago", "--no-merges",
                     "--format=--%H", "--name-only"], check=False)
    counts = {s: 0 for s in subprojects}
    ordered = sorted(subprojects, key=len, reverse=True)
    touched = set()
    for line in proc.stdout.splitlines():
        if line.startswith("--"):
            for s in touched:
                counts[s] += 1
            touched = set()
            continue
        if not line:
            continue
        owner = next((s for s in ordered if _is_under(line, s)), None)
        if owner:
            touched.add(owner)
    for s in touched:
        counts[s] += 1
    return counts


def _auto_of(prof):
    return {"folders": list(prof["folders"]), "tags": list(prof["tags"]), "about": prof.get("about")}


def merge(existing, detected, order=None, rebuild=False):
    """Merge a scan into the existing profiles.

    What the scanner added last time is recorded in a hidden `wsp:auto` marker; items the
    user added (not in the marker) are kept, previous scanner items that are no longer
    detected are dropped, and a user-written `about` wins. With `rebuild`, existing
    profiles without a marker are treated as fully generated (clean regeneration)."""
    existing = existing or {}
    merged = {}
    report = {"added": [], "updated": [], "kept_manual": []}
    for name, prof in detected.items():
        auto = _auto_of(prof)
        old = existing.get(name)
        if old:
            old_auto = old.get("auto") or (_auto_of(old) if rebuild else {"folders": [], "tags": [], "about": None})
            user_folders = [f for f in old["folders"] if f not in old_auto["folders"]]
            auto_keys = {_tag_key(x) for x in old_auto["tags"]}
            user_tags = [t for t in old["tags"] if _tag_key(t) not in auto_keys]
            # a generated tag re-spelled by a person ("pegasus-web-interface" -> "Pegasus web interface")
            respelled = {_tag_key(t): t for t in old["tags"] if _tag_key(t) in auto_keys and t not in old_auto["tags"]}
            user_about = old.get("about") if old.get("about") and old.get("about") != old_auto.get("about") else None
            new = {"path": old.get("path") or prof["path"],
                   "about": user_about or prof.get("about"),
                   "folders": _dedupe(prof["folders"] + user_folders),
                   "tags": [respelled.get(_tag_key(t), t)
                            for t in dedupe_tags(prof["tags"] + user_tags, drop=[name])],
                   "notes": [n for n in old.get("notes") or [] if "not found by the last scan" not in n],
                   "auto": auto}
            if new["folders"] != old["folders"] or new["tags"] != old["tags"] or new["about"] != old.get("about"):
                report["updated"].append(name)
            merged[name] = new
        else:
            merged[name] = {**prof, "auto": auto}
            report["added"].append(name)
    for name, old in existing.items():
        if name in merged:
            continue
        if rebuild and not old.get("auto") and old.get("path"):
            continue  # generated by an older scan and no longer detected
        notes = [n for n in old.get("notes") or [] if "not found by the last scan" not in n]
        if old.get("path"):
            notes.append("not found by the last scan; check the path or remove this profile")
        merged[name] = {**old, "notes": notes}
        report["kept_manual"].append(name)
    order = order or {}
    ordered = dict(sorted(merged.items(), key=lambda kv: (0 if kv[0] in order else 1, -order.get(kv[0], 0),
                                                          kv[1].get("path") or "~", kv[0])))
    return ordered, report


def scan_and_write(path, write=False, activity_days=ACTIVITY_DAYS, rebuild=False):
    repo_root, detected, details = scan(path)
    existing = load(repo_root)
    counts = activity(repo_root, [p["path"] for p in detected.values()], activity_days)
    order = {name: counts.get(p["path"], 0) for name, p in detected.items()}
    for name in details:
        details[name]["commits_last_days"] = order.get(name, 0)
    merged, report = merge(existing, detected, order, rebuild=rebuild)
    text = render(merged)
    target = profiles_path(repo_root)
    legacy = legacy_path(repo_root)
    moved_from = None
    changed = True
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as fh:
            changed = fh.read() != text
    if os.path.isfile(legacy):
        with open(legacy, encoding="utf-8") as fh:
            legacy_is_ours = FORMAT_MARKER in fh.read()
        if legacy_is_ours:
            changed = True
            moved_from = LEGACY_PATH
    if write and changed:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
        if moved_from:
            os.unlink(legacy)  # moved to .wsp/SC-PROFILES.md
    return {"repo_root": repo_root, "file": target, "written": bool(write and changed), "changed": changed,
            "moved_from": moved_from,
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
