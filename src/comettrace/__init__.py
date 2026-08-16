"""Comet Browser/Computer artifact reconstruction package.

The reconstruction engine is intentionally isolated in ``legacy_engine`` and
kept frozen: its behavior is the behavior validated against the controlled
experiments reported in the accompanying paper, and the engine file's SHA-256
digest is pinned below and re-checkable via ``comettrace.provenance``.
All packaging, documentation, and verification layers live outside the engine
and make no logic changes.
"""

__version__ = "0.37.0.post1"
ENGINE_SCHEMA_VERSION = "0.37"
# SHA-256 of src/comettrace/legacy_engine.py as shipped in this
# release. NOTE: the historical refactor_manifest.json pinned a different
# digest (6d9c702d...) that corresponds to an earlier engine revision; the
# value below is the digest of the engine actually released and validated.
ENGINE_SHA256 = "f58b3c3683c4f05e5be2cb7e75080e8e26ef37b5e7e973030d5b921ca5cdaf80"

__all__ = ["__version__", "ENGINE_SCHEMA_VERSION", "ENGINE_SHA256"]
