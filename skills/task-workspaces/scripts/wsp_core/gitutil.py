"""Thin Git wrappers. Every call passes an explicit -C path; nothing depends on cwd."""

import os
import re
import shutil
import tempfile
import time

from . import util
from .errors import WspError

INTERNAL_IDENTITY = {
    "GIT_AUTHOR_NAME": "wsp",
    "GIT_AUTHOR_EMAIL": "wsp@localhost",
    "GIT_COMMITTER_NAME": "wsp",
    "GIT_COMMITTER_EMAIL": "wsp@localhost",
}


def git(path, *args, check=True, env=None, input=None, timeout=None):
    proc = util.run(["git", "-C", path, *args], env=env, check=check, input=input, timeout=timeout)
    return proc


def out(path, *args, **kw):
    return git(path, *args, **kw).stdout.strip()


def version():
    text = util.run(["git", "--version"]).stdout.strip()
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(x or 0) for x in match.groups()) if match else (0, 0, 0)


def is_repo(path):
    if not os.path.isdir(path):
        return False
    return git(path, "rev-parse", "--git-dir", check=False).returncode == 0


def toplevel(path):
    return out(path, "rev-parse", "--show-toplevel")


def absolute_git_dir(path):
    return out(path, "rev-parse", "--absolute-git-dir")


def common_dir(path):
    value = out(path, "rev-parse", "--git-common-dir")
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(path, value))


def origin_url(path, remote="origin"):
    proc = git(path, "remote", "get-url", remote, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def normalize_remote(url):
    """git@github.com:A/B.git, https://github.com/A/B and ssh://git@github.com/A/B are equal."""
    if not url:
        return None
    url = url.strip()
    url = re.sub(r"\.git$", "", url.rstrip("/"))
    m = re.match(r"^[\w.-]+@([\w.-]+):(.+)$", url)
    if m:
        return f"{m.group(1).lower()}/{m.group(2)}"
    m = re.match(r"^(?:https?|ssh|git)://(?:[^@/]+@)?([\w.-]+)(?::\d+)?/(.+)$", url)
    if m:
        return f"{m.group(1).lower()}/{m.group(2)}"
    return os.path.realpath(url) if os.path.exists(url) else url


def same_remote(a, b):
    return normalize_remote(a) == normalize_remote(b)


def rev_parse(path, ref):
    proc = git(path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def default_branch(path, remote="origin"):
    proc = git(path, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().split("/", 1)[1]
    for name in ("main", "master"):
        if rev_parse(path, f"refs/remotes/{remote}/{name}"):
            return name
    return "main"


_LOCK_ERRORS = ("cannot lock ref", ".lock': File exists", "Unable to create", "unable to update local ref",
                "Another git process seems to be running")


def fetch_with_retry(path, remote, refspec, retries=4, backoff=1.5, timeout=300):
    """Fetch into remote-tracking refs only. Retries lock contention with backoff;
    never deletes lock files."""
    attempt = 0
    while True:
        attempt += 1
        proc = git(path, "fetch", "--no-tags", "--quiet", remote, refspec, check=False, timeout=timeout)
        if proc.returncode == 0:
            return {"attempts": attempt}
        transient = any(marker in proc.stderr for marker in _LOCK_ERRORS)
        if not transient or attempt > retries:
            raise WspError("FETCH_FAILED", f"git fetch {remote} {refspec} failed", attempts=attempt,
                           stderr=proc.stderr.strip()[-1500:], transient=transient)
        time.sleep(backoff * attempt)


def local_branch_exists(path, branch):
    return git(path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


def remote_branch_exists(path, branch, remote="origin", timeout=60):
    proc = git(path, "ls-remote", "--heads", remote, branch, check=False, timeout=timeout)
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def worktree_list(path):
    """Parse `git worktree list --porcelain`."""
    text = out(path, "worktree", "list", "--porcelain")
    items, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            cur = {"path": value, "head": None, "branch": None, "detached": False, "bare": False,
                   "locked": False, "lock_reason": None, "prunable": False}
        elif key == "HEAD":
            cur["head"] = value
        elif key == "branch":
            cur["branch"] = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
        elif key == "detached":
            cur["detached"] = True
        elif key == "bare":
            cur["bare"] = True
        elif key == "locked":
            cur["locked"] = True
            cur["lock_reason"] = value or None
        elif key == "prunable":
            cur["prunable"] = True
    return items


def branch_checked_out_at(path, branch):
    for wt in worktree_list(path):
        if wt["branch"] == branch:
            return wt["path"]
    return None


def status_entries(path, ignored=False):
    """Porcelain v1 -z entries: list of (xy, path). Untracked are '??', ignored '!!'."""
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if ignored:
        args.append("--ignored=matching")
    raw = git(path, *args, env={"GIT_OPTIONAL_LOCKS": "0"}).stdout
    entries, parts, i = [], raw.split("\0"), 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if not item:
            continue
        xy, p = item[:2], item[3:]
        if xy[0] in "RC":
            i += 1  # skip the rename source
        entries.append((xy, p))
    return entries


def ignored_paths(path):
    """Ignored files/dirs (dirs collapsed, with trailing '/')."""
    raw = git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z").stdout
    return [p for p in raw.split("\0") if p]


def unpushed_count(path, ref="HEAD"):
    return int(out(path, "rev-list", "--count", ref, "--not", "--remotes"))


def is_ancestor(path, a, b):
    return git(path, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def covered_by_remote(path, head, default_ref):
    """True if every change in `head` already exists on a remote.

    Either head is reachable from a remote-tracking ref, or merging head into
    the default branch changes nothing (squash-merged PRs)."""
    if unpushed_count(path, head) == 0:
        return {"covered": True, "how": "reachable"}
    if default_ref and rev_parse(path, default_ref):
        # merge-tree writes tree objects; send them to a throwaway object dir so the
        # check stays read-only for the repository.
        scratch = tempfile.mkdtemp(prefix="wsp-objects-")
        env = {"GIT_OBJECT_DIRECTORY": scratch,
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(common_dir(path), "objects")}
        try:
            proc = git(path, "merge-tree", "--write-tree", "--no-messages", default_ref, head, check=False, env=env)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if proc.returncode == 0:
            merged_tree = proc.stdout.strip().splitlines()[0]
            default_tree = out(path, "rev-parse", f"{default_ref}^{{tree}}")
            if merged_tree == default_tree:
                return {"covered": True, "how": "merged-content"}
    return {"covered": False, "how": None}
