"""Verify (1) engine freeze and (2) output equivalence across entry points.

Usage:
    python tests/test_output_equivalence.py <acquisition.zip> [more ...]

For each acquisition, runs the frozen engine once through the legacy
launcher and once through the packaged module entry point, then compares
the JSON outputs after canonicalizing the one known-nondeterministic
element: when the input is a zip, the engine extracts it to a
tempfile-created directory whose randomized path is embedded in
path-bearing report fields. The canonicalization replaces that ephemeral
prefix (POSIX and Windows forms) with a fixed token; no other content is
altered. Any remaining difference is a packaging error, since both entry
points execute the same frozen engine.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Ephemeral extraction-directory prefixes (POSIX tmp, Windows temp, and
# JSON-escaped Windows form).
_EPHEMERAL = [
    re.compile(r"/[^\"']*?/comettrace_[A-Za-z0-9_]+"),
    re.compile(r"[A-Za-z]:\\\\(?:[^\"\\\\]+\\\\)*?comettrace_[A-Za-z0-9_]+"),
    re.compile(r"[A-Za-z]:\\(?:[^\"\\]+\\)*?comettrace_[A-Za-z0-9_]+"),
]


def canonical_sha256(p: Path) -> str:
    s = p.read_text(encoding="utf-8")
    for rx in _EPHEMERAL:
        s = rx.sub("<EXTRACT_DIR>", s)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def run(cmd: list[str]) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run(cmd, check=True, cwd=ROOT, env=env, stdout=subprocess.DEVNULL)


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from comettrace.provenance import verify

    if not verify():
        print("FAIL: engine digest mismatch - do not proceed to analysis.")
        return 1

    failures = 0
    for arg in sys.argv[1:]:
        acq = Path(arg).resolve()
        with tempfile.TemporaryDirectory() as td:
            out_a = Path(td) / "a.json"
            out_b = Path(td) / "b.json"
            run([sys.executable, "comet_browser_reconstruct.py",
                 "--input", str(acq), "--output", str(out_a)])
            run([sys.executable, "-m", "comettrace",
                 "--input", str(acq), "--output", str(out_b)])
            ha, hb = canonical_sha256(out_a), canonical_sha256(out_b)
            same = ha == hb
            print(f"{acq.name}: {'IDENTICAL' if same else 'DIFFERENT'} "
                  f"canon-sha256 launcher={ha[:16]} module={hb[:16]}")
            failures += 0 if same else 1
    print("PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    main()
