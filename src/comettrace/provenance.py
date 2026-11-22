"""Verify the integrity of CometTrace reconstruction engines.

Checks both:
1. ``src/comettrace/legacy_engine.py`` (frozen schema 0.37 baseline) against
   ``ENGINE_SHA256``; and
2. ``src/comettrace/engine_v038.py`` (patched schema 0.38 layer) against
   ``PATCHED_ENGINE_SHA256`` when pinned.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from . import ENGINE_SHA256, PATCHED_ENGINE_SHA256


def engine_path() -> Path:
    """Return the path to the frozen legacy reconstruction engine (schema 0.37)."""
    return Path(__file__).with_name("legacy_engine.py")


def patched_engine_path() -> Path:
    """Return the path to the v0.38 patch layer (schema 0.38)."""
    return Path(__file__).with_name("engine_v038.py")


def compute_engine_sha256() -> str:
    """Compute the SHA-256 digest of the frozen legacy engine file."""
    return hashlib.sha256(engine_path().read_bytes()).hexdigest()


def compute_patched_engine_sha256() -> str:
    """Compute the SHA-256 digest of the v0.38 patch layer file."""
    return hashlib.sha256(patched_engine_path().read_bytes()).hexdigest()


def verify(quiet: bool = False) -> bool:
    """Verify frozen legacy engine integrity (and pinned v0.38 digest when set)."""
    legacy_p = engine_path()
    legacy_actual = compute_engine_sha256()
    legacy_ok = legacy_actual == ENGINE_SHA256

    v38_p = patched_engine_path()
    v38_actual = compute_patched_engine_sha256() if v38_p.exists() else None
    v38_ok = True if PATCHED_ENGINE_SHA256 is None else (v38_actual == PATCHED_ENGINE_SHA256)

    verified = legacy_ok and v38_ok

    if not quiet:
        print(f"legacy engine file  : {legacy_p}")
        print(f"legacy expected     : {ENGINE_SHA256}")
        print(f"legacy actual       : {legacy_actual}")
        print(f"legacy status       : {'VERIFIED' if legacy_ok else 'MISMATCH'}")
        print(f"v0.38 engine file   : {v38_p}")
        print(f"v0.38 expected      : {PATCHED_ENGINE_SHA256 or '(unpinned)'}")
        print(f"v0.38 actual        : {v38_actual}")
        print(f"v0.38 status        : {'VERIFIED' if v38_ok else 'MISMATCH'}")
        print("scope               : engine-file integrity only")

    return verified


def main() -> int:
    return 0 if verify() else 1


if __name__ == "__main__":
    sys.exit(main())
