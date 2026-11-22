"""Verify both tracked source files against the release manifest and installed package."""
import hashlib
import json
from pathlib import Path

import comettrace


def verify(root):
    manifest = json.loads((root / "release_manifest.json").read_text(encoding="utf-8"))
    expected = {
        "package": "comettrace",
        "package_version": comettrace.__version__,
        "engine_schema_version": comettrace.LEGACY_ENGINE_SCHEMA_VERSION,
        "patched_engine_schema_version": comettrace.ENGINE_SCHEMA_VERSION,
        "engine_frozen": True,
        "logic_changes_relative_to_engine": False,
    }
    for key, value in expected.items():
        if manifest[key] != value:
            raise SystemExit(f"Release manifest mismatch for {key}")
    for name, filename, pinned in (
        ("engine", "legacy_engine.py", comettrace.ENGINE_SHA256),
        ("patched_engine", "engine_v038.py", comettrace.PATCHED_ENGINE_SHA256),
    ):
        path = Path("src", "comettrace", filename)
        actual = hashlib.sha256((root / path).read_bytes()).hexdigest()
        if (manifest[name + "_file"] != path.as_posix()
                or actual != pinned or manifest[name + "_sha256"] != actual):
            raise SystemExit(f"{name} manifest/source checksum mismatch ({actual})")
        print(f"{path.as_posix()}  {actual}  VERIFIED")


if __name__ == "__main__":
    verify(Path(__file__).resolve().parents[1])
