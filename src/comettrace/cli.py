"""Command-line entry point for CometTrace v0.38.

Defaults to the v0.38 patched engine (``engine_v038.py``, schema 0.38) while
preserving direct access to the untouched frozen schema 0.37 engine
(``legacy_engine.py``) via ``--engine v037`` / ``--engine legacy``.
"""

from __future__ import annotations


def main() -> None:
    """Run the CometTrace CLI (v0.38 default; v0.37 frozen via --engine v037)."""
    from .engine_v038 import main as v038_main

    v038_main()
