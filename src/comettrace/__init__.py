"""CometTrace package metadata (v0.38.4).

The original paper-associated analytical engine remains release-frozen and
byte-identical in ``src/comettrace/legacy_engine.py`` (schema 0.37,
SHA-256 ``f58b3c3683c4f05e5be2cb7e75080e8e26ef37b5e7e973030d5b921ca5cdaf80``).

Phase 2 fixes for multi-stage ZIP discovery, account-scoped IndexedDB database
discovery, multi-thread list-record attribution, download task classification,
and repeated-extension PDF filename preservation are isolated in
``src/comettrace/engine_v038.py`` (schema 0.38).
"""


__version__ = "0.38.4"

ENGINE_SCHEMA_VERSION = "0.38"
LEGACY_ENGINE_SCHEMA_VERSION = "0.37"

# Frozen SHA-256 of src/comettrace/legacy_engine.py (schema 0.37 baseline).
ENGINE_SHA256 = (
    "f58b3c3683c4f05e5be2cb7e75080e8e"
    "26ef37b5e7e973030d5b921ca5cdaf80"
)

# Pinned SHA-256 of src/comettrace/engine_v038.py (schema 0.38 patched engine).
PATCHED_ENGINE_SHA256 = (
    "86f86ea1b857c879ab93e5999ebe8a08"
    "2735edc34cb917046097396badc26c39"
)


__all__ = [
    "__version__",
    "ENGINE_SCHEMA_VERSION",
    "LEGACY_ENGINE_SCHEMA_VERSION",
    "ENGINE_SHA256",
    "PATCHED_ENGINE_SHA256",
]
