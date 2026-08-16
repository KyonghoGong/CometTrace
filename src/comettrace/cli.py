"""Command-line entry point.

The import of ``legacy_engine`` is delayed until ``main`` runs, so package
metadata can be inspected without the forensic parsing dependencies
installed. The engine's own argument parser defines the CLI surface; this
wrapper adds nothing and removes nothing.
"""

from __future__ import annotations


def main() -> None:
    """Run the frozen engine CLI (report schema 0.37)."""
    from .legacy_engine import main as legacy_main

    legacy_main()
