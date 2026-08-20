"""Verify engine integrity and output equivalence across execution entry points.

Usage:
    python tests/test_output_equivalence.py <acquisition> [more ...]

For each supplied acquisition, this test runs the frozen reconstruction engine
through:

1. the backward-compatible legacy launcher; and
2. the packaged ``python -m comettrace`` entry point.

The resulting JSON reports are compared after canonicalizing randomized
temporary extraction-directory paths. ZIP inputs are extracted into a
temporary directory whose generated name can appear in path-bearing report
fields. This path is an execution-environment artifact rather than a
reconstruction difference.

No report content other than the recognized temporary extraction-directory
prefix is canonicalized.

This test establishes same-environment equivalence between the two execution
entry points. It does not:

- compare generated reports against archived paper-associated outputs;
- establish cross-platform output equivalence;
- establish equivalence across different parser dependency revisions; or
- independently reproduce the numerical results reported in the paper.
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


# The frozen engine currently creates temporary extraction directories using
# the prefix "comet_reconstruct_". The earlier package-oriented
# "comettrace_" prefix is retained for compatibility.
#
# The patterns cover:
#
# 1. POSIX paths;
# 2. JSON-escaped Windows paths; and
# 3. ordinary Windows path strings.
#
# No other path or report field is modified.

_EPHEMERAL_PATH_PATTERNS = [
    re.compile(
        r"/[^\"']*?/(?:comettrace|comet_reconstruct)_[A-Za-z0-9_]+"
    ),
    re.compile(
        r"[A-Za-z]:\\\\(?:[^\"\\\\]+\\\\)*?"
        r"(?:comettrace|comet_reconstruct)_[A-Za-z0-9_]+"
    ),
    re.compile(
        r"[A-Za-z]:\\(?:[^\"\\]+\\)*?"
        r"(?:comettrace|comet_reconstruct)_[A-Za-z0-9_]+"
    ),
]


def canonical_sha256(path: Path) -> str:
    """Return a SHA-256 digest after normalizing temporary extraction paths."""

    report_text = path.read_text(encoding="utf-8")

    for pattern in _EPHEMERAL_PATH_PATTERNS:
        report_text = pattern.sub("<EXTRACT_DIR>", report_text)

    return hashlib.sha256(report_text.encode("utf-8")).hexdigest()


def run_entry_point(command: list[str]) -> None:
    """Run one CometTrace execution entry point from the repository root."""

    environment = dict(os.environ)

    environment["PYTHONPATH"] = (
        str(ROOT / "src")
        + os.pathsep
        + environment.get("PYTHONPATH", "")
    )

    subprocess.run(
        command,
        check=True,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
    )


def main() -> int:
    """Verify the engine digest and compare the two execution entry points."""

    if len(sys.argv) < 2:
        print(
            "Usage: python tests/test_output_equivalence.py "
            "<acquisition> [more ...]",
            file=sys.stderr,
        )
        return 2

    sys.path.insert(0, str(ROOT / "src"))

    from comettrace.provenance import verify

    if not verify():
        print(
            "FAIL: engine digest mismatch. "
            "Do not use this engine to produce citable results."
        )
        return 1

    failures = 0

    for argument in sys.argv[1:]:
        acquisition = Path(argument).expanduser().resolve()

        if not acquisition.exists():
            print(f"{acquisition}: NOT FOUND")
            failures += 1
            continue

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)

            legacy_output = temporary_root / "legacy.json"
            packaged_output = temporary_root / "packaged.json"

            run_entry_point(
                [
                    sys.executable,
                    "comet_browser_reconstruct.py",
                    "--input",
                    str(acquisition),
                    "--output",
                    str(legacy_output),
                ]
            )

            run_entry_point(
                [
                    sys.executable,
                    "-m",
                    "comettrace",
                    "--input",
                    str(acquisition),
                    "--output",
                    str(packaged_output),
                ]
            )

            legacy_hash = canonical_sha256(legacy_output)
            packaged_hash = canonical_sha256(packaged_output)

            equivalent = legacy_hash == packaged_hash

            status = "EQUIVALENT" if equivalent else "DIFFERENT"

            print(
                f"{acquisition.name}: {status} "
                f"canonical-sha256 "
                f"legacy={legacy_hash[:16]} "
                f"packaged={packaged_hash[:16]}"
            )

            if not equivalent:
                failures += 1

    if failures == 0:
        print("PASS")
        return 0

    print(f"FAIL ({failures})")
    return 1


if __name__ == "__main__":
    sys.exit(main())