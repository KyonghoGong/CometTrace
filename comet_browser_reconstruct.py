#!/usr/bin/env python3
"""Backward-compatible launcher (legacy filename).

The tool was previously invoked as `comet_browser_reconstruct.py`. That
invocation style remains valid and is equivalent to `comettrace.py`:
    python comet_browser_reconstruct.py --input <path> --output out.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from comettrace.cli import main

if __name__ == "__main__":
    main()
