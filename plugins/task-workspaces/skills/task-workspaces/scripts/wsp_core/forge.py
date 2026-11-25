"""Pull request state of a branch, read from GitHub with the `gh` CLI.

Used only by the cleanup policy, as a second opinion when the content check
(`gitutil.merged_into`) cannot prove a merge: after a squash merge the default
branch often changes the same lines again, and from then on merging the task
branch into it conflicts forever. Read-only; every failure means "unknown".
"""

import json
import os
import re
import shutil
import subprocess

from . import gitutil

_SLUG = re.compile(r"^[\w.-]+/[\w.-]+$")


def github_slug(repo_cfg, origin=None):
    """owner/name of the GitHub repository, or None. `repos.<name>.github` in the config
    wins (SSH host aliases, GitHub Enterprise); otherwise it is read from a github.com origin."""
    explicit = (repo_cfg or {}).get("github")
    if explicit:
        return explicit if _SLUG.match(explicit) else None
    norm = gitutil.normalize_remote(origin or (repo_cfg or {}).get("origin")) or ""
    if norm.startswith("github.com/"):
        slug = norm[len("github.com/"):]
        return slug if _SLUG.match(slug) else None
    return None


def gh_binary():
    if os.environ.get("WSP_NO_GH"):
        return None
    return shutil.which("gh")


def pull_requests(slug, branch, timeout=30):
    """Pull requests whose head branch is `branch`: [{number, state, head, base}] with state
    OPEN | MERGED | CLOSED. None when it cannot be found out (no gh, no auth, network)."""
    gh = gh_binary()
    if not gh or not slug or not branch:
        return None
    try:
        proc = subprocess.run(
            [gh, "pr", "list", "--repo", slug, "--head", branch, "--state", "all", "--limit", "30",
             "--json", "number,state,headRefOid,baseRefName"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
            env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    return [{"number": r.get("number"), "state": str(r.get("state") or "").upper(), "head": r.get("headRefOid"),
             "base": r.get("baseRefName")}
            for r in rows if isinstance(r, dict)]
