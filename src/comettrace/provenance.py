"""Verify the frozen engine, the schema 0.38 entry point and its evidence modules."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from . import ENGINE_SHA256, PATCHED_ENGINE_SHA256, MODULES_SHA256


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


def module_file_hashes(package_dir: Path) -> dict[str, str]:
    """Hash every Python source under v038, using portable package-relative paths."""
    return {
        file.relative_to(package_dir).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted((package_dir / "v038").rglob("*.py"))
    }


def modules_digest(hashes: dict[str, str]) -> str:
    """Hash sorted UTF-8 lines containing a hex digest, two spaces, path and LF."""
    lines = "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def verify(quiet: bool = False) -> bool:
    """Verify frozen legacy engine integrity (and pinned v0.38 digest when set)."""
    legacy_p = engine_path()
    legacy_actual = compute_engine_sha256()
    legacy_ok = legacy_actual == ENGINE_SHA256

    v38_p = patched_engine_path()
    v38_actual = compute_patched_engine_sha256() if v38_p.exists() else None
    v38_ok = True if PATCHED_ENGINE_SHA256 is None else (v38_actual == PATCHED_ENGINE_SHA256)

    modules_actual = modules_digest(module_file_hashes(v38_p.parent))
    verified = legacy_ok and v38_ok and modules_actual == MODULES_SHA256

    if not quiet:
        print(f"legacy engine file  : {legacy_p}")
        print(f"legacy expected     : {ENGINE_SHA256}")
        print(f"legacy actual       : {legacy_actual}")
        print(f"legacy status       : {'VERIFIED' if legacy_ok else 'MISMATCH'}")
        print(f"v0.38 engine file   : {v38_p}")
        print(f"v0.38 expected      : {PATCHED_ENGINE_SHA256 or '(unpinned)'}")
        print(f"v0.38 actual        : {v38_actual}")
        print(f"v0.38 status        : {'VERIFIED' if v38_ok else 'MISMATCH'}")
        print(f"v0.38 modules actual: {modules_actual}")
        print("module integrity    :", "VERIFIED" if modules_actual == MODULES_SHA256 else "MISMATCH")

    return verified


def main() -> int:
    return 0 if verify() else 1


if __name__ == "__main__":
    sys.exit(main())
