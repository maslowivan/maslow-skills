"""Small helpers shared by all modules: subprocess, time, files, hashing."""

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import tempfile

from .errors import WspError

GIB = 1024 ** 3


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value):
    if not value:
        return None
    return _dt.datetime.fromisoformat(value)


def age_hours(value):
    ts = parse_iso(value)
    if ts is None:
        return None
    return (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() / 3600


def run(cmd, cwd=None, env=None, check=True, input=None, timeout=None, code="GIT_FAILED"):
    """Run a command and return CompletedProcess with text output."""
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=merged,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise WspError("UNSUPPORTED", f"command not found: {cmd[0]}", command=cmd) from exc
    except subprocess.TimeoutExpired as exc:
        raise WspError(code, f"command timed out: {' '.join(cmd)}", command=cmd) from exc
    if check and proc.returncode != 0:
        raise WspError(
            code,
            f"command failed ({proc.returncode}): {' '.join(cmd)}",
            command=cmd,
            cwd=cwd,
            stderr=proc.stderr.strip()[-2000:],
        )
    return proc


def sha256_file(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def expand(path):
    if path is None:
        return None
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


def human_bytes(value):
    if value is None:
        return "?"
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text, max_len=48):
    slug = _SLUG_RE.sub("-", str(text).lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug


TASK_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_task_id(task_id):
    if not task_id or not TASK_ID_RE.match(task_id) or "--" in task_id:
        raise WspError(
            "INVALID_ID",
            "task id must be an ASCII slug [a-z0-9-], 1-48 chars, no leading/trailing or double dashes",
            task_id=task_id,
            suggestion=slugify(task_id or ""),
        )
    return task_id


def validate_repo_name(name):
    if not name or not REPO_NAME_RE.match(name) or name in (".", ".."):
        raise WspError("INVALID_ID", "invalid repository name", repo=name)
    return name
