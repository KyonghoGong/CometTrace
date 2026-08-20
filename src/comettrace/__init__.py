"""CometTrace package metadata.

CometTrace is the open-source forensic reconstruction tool accompanying the
paper "Reconstructing Agentic AI Behavior from Browser Artifacts: A Forensic
Case Study of Perplexity Comet".

The analytical reconstruction engine is isolated in
``src/comettrace/legacy_engine.py`` and is release-frozen.

The SHA-256 digest recorded in this module verifies engine-file integrity. It
does not independently establish:

- byte-identical reproduction of archived paper-associated reports;
- equivalence across different parser dependency revisions;
- equivalence across different Python versions or operating systems; or
- independent reproduction of the paper's numerical results.

Packaging, documentation, provenance, and entry-point verification layers are
maintained separately from the frozen analytical engine.
"""


__version__ = "0.37.0.post1"

ENGINE_SCHEMA_VERSION = "0.37"

# SHA-256 of src/comettrace/legacy_engine.py as pinned by this source release.
#
# The historical file docs/refactor_manifest.historical.json records a
# different digest beginning with 6d9c702d for an earlier engine revision
# identified as schema 0.27. That historical digest must not be used to verify
# the current schema 0.37 engine.
#
# This digest verifies engine-file integrity only. Complete report
# reproducibility additionally depends on the parser dependency commits,
# Python version, operating system, acquisition, and invocation parameters.

ENGINE_SHA256 = (
    "f58b3c3683c4f05e5be2cb7e75080e8e"
    "26ef37b5e7e973030d5b921ca5cdaf80"
)


__all__ = [
    "__version__",
    "ENGINE_SCHEMA_VERSION",
    "ENGINE_SHA256",
]