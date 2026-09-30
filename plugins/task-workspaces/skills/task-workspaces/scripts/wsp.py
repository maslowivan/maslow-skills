#!/usr/bin/env python3
"""Entry point: python3 <skill>/scripts/wsp.py <command> ..."""

import os
import sys

if sys.version_info < (3, 9):
    sys.stderr.write("wsp requires Python 3.9 or newer (macOS: /usr/bin/python3 from the Command Line Tools)\n")
    sys.exit(71)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wsp_core.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
