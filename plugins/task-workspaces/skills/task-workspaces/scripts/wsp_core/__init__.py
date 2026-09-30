"""wsp: task workspaces manager (core). Python 3.9+, standard library only."""

import json as _json
import os as _os


def _plugin_version():
    """The plugin manifest is the single source of the version (skill-only copies fall back)."""
    here = _os.path.dirname(_os.path.abspath(__file__))
    manifest = _os.path.join(here, "..", "..", "..", "..", ".claude-plugin", "plugin.json")
    try:
        with open(manifest) as fh:
            return _json.load(fh)["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


__version__ = _plugin_version()
