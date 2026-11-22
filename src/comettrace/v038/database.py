"""Inspect IndexedDB databases and stores for Perplexity query records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ccl_chromium_reader import ccl_chromium_indexeddb

from .. import legacy_engine as leg
from .records import SINGLE_THREAD_KEY_MARKERS


def discover_query_databases(
    leveldb_path: Path,
    blob_path: Path | None = None,
    object_store_name: str = leg.DEFAULT_STORE,
) -> list[dict[str, Any]]:
    """Inspect available IndexedDB databases and count records in target store."""
    wrapper = ccl_chromium_indexeddb.WrappedIndexDB(
        str(leveldb_path),
        str(blob_path) if blob_path is not None else None,
    )
    discovered: list[dict[str, Any]] = []
    try:
        for db_id in wrapper.database_ids:
            db = wrapper[db_id.dbid_no]
            stores = list(db.object_store_names)
            has_target_store = object_store_name in stores
            rec_count = 0
            pplx_key_count = 0
            known_keys = 0
            decode_failures = 0

            def count_failure(key, value):
                nonlocal decode_failures
                decode_failures += 1

            if has_target_store:
                store = db[object_store_name]
                for rec in store.iterate_records(
                    live_only=False,
                    errors_to_stdout=False,
                    bad_deserializer_data_handler=count_failure,
                ):
                    rec_count += 1
                    ks = str(rec.key)
                    raw_key = rec.key.value
                    try:
                        tokens = json.loads(raw_key) if isinstance(raw_key, str) else raw_key
                    except ValueError:
                        tokens = None
                    tokens = (
                        [k for k in tokens if isinstance(k, str)] if isinstance(tokens, (list, tuple)) else []
                    )
                    if any(
                        k in SINGLE_THREAD_KEY_MARKERS
                        or k
                        in {
                            "graphql-sidebar-v2",
                            "sidebar-snapshot",
                            "/rest/thread/list_ask_threads",
                            "/rest/thread/list_recent",
                            "/rest/thread/list_pinned_ask_threads",
                        }
                        for k in tokens
                    ):
                        known_keys += 1
                    if (
                        "pplx-query-cache" in ks
                        or "all_results" in ks
                        or "thread_metadata" in ks
                        or "/rest/thread/" in ks
                    ):
                        pplx_key_count += 1
            discovered.append(
                {
                    "name": db.name,
                    "stores": stores,
                    "has_target_store": has_target_store,
                    "record_count": rec_count,
                    "perplexity_cache_record_count": pplx_key_count,
                    "recognized_thread_keys": known_keys,
                    "unreadable_records": decode_failures,
                    "is_pplx_query_cache": db.name.startswith("pplx-query-cache-"),
                    "is_legacy_keyval_store": db.name == leg.DEFAULT_DATABASE,
                }
            )
    finally:
        close = getattr(wrapper, "close", None)
        if callable(close):
            close()
    return discovered


def select_database(
    leveldb_path: Path,
    blob_path: Path | None,
    requested_database: str | None,
    object_store_name: str = leg.DEFAULT_STORE,
    *,
    discover_query_databases,
) -> tuple[str, dict[str, Any]]:
    """Resolve database name automatically or validate an explicit user choice."""
    try:
        discovered = discover_query_databases(leveldb_path, blob_path, object_store_name)
    except Exception as exc:
        raise RuntimeError(
            f"[database_discovery_failed] Cannot inspect {leveldb_path}: {type(exc).__name__}: {exc}"
        ) from exc
    by_name = {d["name"]: d for d in discovered}
    req = (requested_database or "auto").strip()

    if req.lower() != "auto":
        if req not in by_name:
            avail = ", ".join(sorted(by_name)) or "(none)"
            raise KeyError(
                f"[database_not_found] Requested IndexedDB database {req!r} not found in {leveldb_path}. "
                f"Available databases: {avail}"
            )
        if not by_name[req]["has_target_store"]:
            raise ValueError(f"[store_not_found] Database {req!r} has no object store {object_store_name!r}.")
        return req, {
            "requested_database": req,
            "selected_database": req,
            "selection_mode": "explicit",
            "selection_reason": "User explicitly specified --database.",
            "available_databases": discovered,
        }

    # Auto selection priority:
    # 1. Any database with target store containing Perplexity cache records, preferring:
    #    - pplx-query-cache-* with perplexity_cache_record_count > 0 (highest count wins)
    #    - keyval-store with perplexity_cache_record_count > 0
    #    - any other DB with perplexity_cache_record_count > 0
    # 2. Any pplx-query-cache-* with record_count > 0
    # 3. keyval-store with target store (legacy default fallback)
    candidates = [d for d in discovered if d["has_target_store"]]
    if not candidates:
        raise ValueError(
            f"[database_auto_no_store] No IndexedDB database containing object store {object_store_name!r} found in {leveldb_path}."
        )

    def rank_key(d: dict[str, Any]) -> tuple[int, int, int, int, str]:
        has_pplx = 1 if d["perplexity_cache_record_count"] > 0 else 0
        is_new_cache = 1 if d["is_pplx_query_cache"] else 0
        is_legacy = 1 if d["is_legacy_keyval_store"] else 0
        return (
            has_pplx,
            d["perplexity_cache_record_count"],
            is_new_cache if has_pplx else is_legacy,
            d["record_count"],
            d["name"],
        )

    candidates.sort(key=rank_key, reverse=True)
    chosen = candidates[0]
    if chosen["is_pplx_query_cache"] and chosen["perplexity_cache_record_count"] > 0:
        reason = (
            "Auto-selected account-scoped pplx-query-cache-* database with active Perplexity cache records."
        )
    elif chosen["is_legacy_keyval_store"] and chosen["perplexity_cache_record_count"] > 0:
        reason = "Auto-selected legacy keyval-store database with Perplexity cache records."
    elif chosen["perplexity_cache_record_count"] > 0:
        reason = "Auto-selected database containing Perplexity cache records."
    else:
        reason = "Fallback to database with target object store (no Perplexity cache keys detected)."

    return chosen["name"], {
        "requested_database": "auto",
        "selected_database": chosen["name"],
        "selection_mode": "auto",
        "selection_reason": reason,
        "available_databases": discovered,
    }
