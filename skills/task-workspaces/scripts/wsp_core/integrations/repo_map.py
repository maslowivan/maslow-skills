"""Repository map integration: import repositories from a Markdown table.

Enabled with `integration: repo-map` and `integration_options.repo_map_path`
(or `wsp init --repo-map FILE`). Any Markdown table row that contains an
absolute local path of a Git checkout is imported; a cell that looks like a Git
URL is used as its origin. The file is only read, never written.
"""

import os
import re

from .. import util

_GIT_URL = re.compile(r"^(?:[\w.-]+@[\w.-]+:[\w./-]+|(?:https?|ssh|git)://\S+)$")


def parse_repo_map(path):
    repos, seen = [], set()
    with open(util.expand(path), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("|") or set(line) <= set("|-: "):
                continue
            cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
            local = next((c for c in cells if c.startswith(("/", "~/"))), None)
            if not local:
                continue
            local = util.expand(local)
            if local in seen:
                continue
            origin = next((c for c in cells if _GIT_URL.match(c)), None)
            seen.add(local)
            repos.append({"name": os.path.basename(local.rstrip("/")), "path": local, "origin": origin})
    return repos
