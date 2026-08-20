"""Verify the integrity of the frozen CometTrace reconstruction engine.

This module checks whether ``src/comettrace/legacy_engine.py`` is
byte-identical to the engine file pinned by the current source release.

Run:

    python -m comettrace.provenance

A successful result verifies engine-file integrity only.

It does not independently verify:

- byte-identical reproduction of archived paper-associated reports;
- equivalence across different parser dependency revisions;
- equivalence across different Python versions or operating systems;
- the numerical values reported in the accompanying paper; or
- external ground-truth observations.

Complete report reproducibility additionally depends on the parser dependency
commits, Python version, operating system, acquisition, and invocation
parameters.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from . import ENGINE_SHA256


def engine_path() -> Path:
    """Return the path to the frozen reconstruction engine."""

    return Path(__file__).with_name("legacy_engine.py")


def compute_engine_sha256() -> str:
    """Compute the SHA-256 digest of the frozen engine file."""

    return hashlib.sha256(
        engine_path().read_bytes()
    ).hexdigest()


def verify(quiet: bool = False) -> bool:
    """Verify that the engine file matches the pinned SHA-256 digest.

    Parameters
    ----------
    quiet:
        When ``True``, return the verification result without printing the
        detailed integrity report.

    Returns
    -------
    bool
        ``True`` when the engine file matches the pinned digest; otherwise,
        ``False``.
    """

    path = engine_path()
    actual_sha256 = compute_engine_sha256()
    verified = actual_sha256 == ENGINE_SHA256

    if not quiet:
        print(f"engine file : {path}")
        print(f"expected    : {ENGINE_SHA256}")
        print(f"actual      : {actual_sha256}")

        if verified:
            print("status      : VERIFIED")
        else:
            print("status      : MISMATCH")

        print("scope       : engine-file integrity only")

    return verified


def main() -> int:
    """Run engine-integrity verification and return a process exit code."""

    return 0 if verify() else 1


if __name__ == "__main__":
    sys.exit(main())