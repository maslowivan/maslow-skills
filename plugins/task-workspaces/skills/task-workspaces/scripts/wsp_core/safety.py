"""Path safety: every destructive filesystem operation is confined to the
manager-owned root, never follows symlinks, and re-checks identity right before acting."""

import os
import shutil

from .errors import WspError


def check_root(configured):
    """The configured root itself must not be a symlink (a swapped root would redirect
    removals). System-level symlinks in its ancestry (/var -> /private/var) are resolved."""
    configured = os.path.abspath(configured)
    if os.path.islink(configured):
        raise WspError("PATH_UNSAFE", "configured root is a symlink", root=configured)
    return os.path.realpath(configured)


def inside(root, path):
    """Return the absolute path if it is strictly inside root without symlink hops; raise otherwise."""
    root = os.path.realpath(root)
    path = os.path.abspath(path)
    rel = os.path.relpath(path, root)
    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        raise WspError("PATH_UNSAFE", "path is outside the manager-owned root", root=root, path=path)
    probe = root
    for part in rel.split(os.sep):
        if part in ("", ".", ".."):
            raise WspError("PATH_UNSAFE", "path contains relative components", path=path)
        probe = os.path.join(probe, part)
        if os.path.islink(probe):
            raise WspError("PATH_UNSAFE", "path traverses a symlink", path=path, symlink=probe)
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if not (real == real_root or real.startswith(real_root + os.sep)) or real == real_root:
        raise WspError("PATH_UNSAFE", "resolved path escapes the root", root=real_root, path=real)
    return path


def remove_tree(root, path, expect_inode=None):
    """Remove a directory tree that lies inside root. Refuses symlinks and swapped paths."""
    path = inside(root, path)
    if not os.path.lexists(path):
        return False
    if os.path.islink(path):
        raise WspError("PATH_UNSAFE", "refusing to remove a symlink", path=path)
    if expect_inode is not None and os.stat(path).st_ino != expect_inode:
        raise WspError("PATH_UNSAFE", "directory changed identity between check and removal", path=path)
    shutil.rmtree(path)
    return True


def remove_empty_dir(root, path):
    path = inside(root, path)
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False
