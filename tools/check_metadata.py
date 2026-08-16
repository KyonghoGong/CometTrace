"""Print package metadata and confirm the console entry point is registered.

Run from VS Code's Run and Debug panel ("2. Print package metadata"), or:
    python tools/check_metadata.py
No forensic dependency (ccl) is required.
"""

from __future__ import annotations

import comettrace as c


def main() -> None:
    print("version              :", c.__version__)
    print("engine schema version:", c.ENGINE_SCHEMA_VERSION)
    print("engine sha256 (head) :", c.ENGINE_SHA256[:16], "...")

    # This line only reports True after `pip install -e .`. When running the
    # verification checks straight from the Run and Debug panel (no install),
    # False is expected and not a problem — the metadata above is what matters.
    try:
        from importlib.metadata import entry_points

        names = {e.name for e in entry_points(group="console_scripts")}
        registered = "comettrace" in names
        print("CLI registered (only after pip install):", registered)
    except Exception as exc:  # pragma: no cover
        print("entry-point check skipped:", exc)


if __name__ == "__main__":
    main()
