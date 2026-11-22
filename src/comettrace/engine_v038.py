#!/usr/bin/env python3
"""CometTrace v0.38 reconstruction engine patch layer.

This module leaves ``src/comettrace/legacy_engine.py`` (schema 0.37, frozen
SHA-256 ``f58b3c3683c4f05e5be2cb7e75080e8e26ef37b5e7e973030d5b921ca5cdaf80``)
byte-for-byte untouched so original paper-associated provenance remains intact.

v0.38 resolves five Phase-1 verified defects without altering S09 core
extraction or deletion-state semantics:

A. Multi-stage ZIP snapshot discovery, explicit/all-stage isolated execution,
   and stage-scoped ``source_file_temporal_context`` (no cross-stage metadata
   leakage).
B. Automatic and explicit IndexedDB database discovery supporting both legacy
   ``keyval-store`` and new account-scoped ``pplx-query-cache-*`` databases.
C. Structural multi-thread list-record isolation (``graphql-sidebar-v2``,
   ``/rest/thread/list_recent``, ``/rest/thread/list_ask_threads``) with
   per-entry UUID/context attribution, eliminating cross-thread preview
   contamination.
D. Comprehensive download task classification (prompt + workflow +
   non-negated final-answer completion + optional stage-scoped Chromium
   ``History`` downloads corroboration) without blind score inflation.
E. Exact PDF filename extraction preserving repeated extensions such as
   ``.pdf.pdf`` without arbitrary normalization.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ccl_chromium_reader import ccl_chromium_indexeddb

from . import legacy_engine as leg

SCHEMA_VERSION_V038 = "0.38"
ENGINE_REVISION_V038 = "v0.38.4"

MULTI_THREAD_LIST_KINDS = {
    "thread_list_recent",
    "thread_list_sidebar",
    "thread_list_pinned",
    "asi_thread_list",
    "thread_list_ask",
    "top_most_urls",
}

SINGLE_THREAD_KEY_MARKERS = [
    "all_results",
    "thread_metadata",
    "thread_first_entry",
    "thread_latest_entry",
    "thread_pagination_state",
]

# Unquoted prose has no reliable left boundary for a multi-word basename.
PDF_FILENAME_RE_V38 = re.compile(r"(?<![\w.-])[\w-][\w.-]*?(?:\.pdf)+(?![\w-]|\.\w)", re.IGNORECASE)
_PDF_END = r"(?:\.pdf)+(?![\w-]|\.\w)"
_PDF_LABEL = (r"\b(?:final\s+downloaded\s+filename|downloaded\s+filename|final\s+filename|file\s*name|"
              r"downloaded\s+files?(?:\s*\(\s*(?:exactly\s+)?(?:one|[1-9]\d*)\s*\))?)"
              r"\s*(?:is\s+|[:：]\s*|\s+-\s+)")
_PDF_LABELED = re.compile(_PDF_LABEL + r'[*\x60"\x27]*([^<>:"/\\|?*\x60\r\n]+?' + _PDF_END + r")", re.IGNORECASE)
_PDF_QUOTED = re.compile(r'([\x60"\x27])([^<>:"/\\|?*\x60\r\n]+?' + _PDF_END + r")\1", re.IGNORECASE)

_DOWNLOAD_NEGATION_OR_FAILURE_PATTERNS = [
    r"\bdo\s+not\s+download\b",
    r"\bdid(?:n't|\s+not)\s+download\b",
    r"\bno\s+download(?:s|ed)?\b",
    r"\bfailed\s+to\s+download\b",
    r"\bcould(?:n't|\s+not)\s+download\b",
    r"\bunable\s+to\s+download\b",
    r"\bdownload\s+failed\b",
    r"\bdownload\s+(?:was\s+)?(?:cancelled|canceled|interrupted|aborted)\b",
    r"\bneed\s+to\s+confirm\b",
    r"\bplease\s+confirm\b",
    r"\bbefore\s+proceeding\b",
    r"\bconfirmation\s+required\b",
    r"\bawaiting\s+confirmation\b",
]

_DOWNLOAD_COMPLETION_PATTERNS_V38 = [
    # A quantified past-tense download report is a completion statement.
    r"\bdownloaded\s+files?\s*\(\s*(?:exactly\s+)?(?:one|[1-9]\d*)\s*\)\s*:",
    r"\bdownload\s+(?:is\s+)?complete(?:d)?\b",
    r"\bdownload\s+has\s+completed\b",
    r"\bsuccessfully\s+downloaded\b",
    r"\bdownloaded\s+successfully\b",
    r"\bi\s+downloaded\s+(?:one|1|a|the|exactly\s+one)\s+(?:pdf|file|document)\b",
    r"\bdownloaded\s+(?:one|1|exactly\s+one)\s+(?:pdf|file)\b",
    r"\bsaved\s+the\s+(?:pdf|file)\b",
    r"다운로드(?:가|를|을)?\s*완료",
]

_DOWNLOAD_FAILURE_PATTERNS_V38 = [
    r"\bfailed\s+to\s+download\b",
    r"\bcould(?:n't|\s+not)\s+download\b",
    r"\bunable\s+to\s+download\b",
    r"\bdownload\s+failed\b",
    r"\bdownload\s+error\b",
    r"다운로드\s*실패",
]

_DOWNLOAD_INTERRUPTED_PATTERNS_V38 = [
    r"\bdownload\s+(?:was\s+)?(?:cancelled|canceled|interrupted|aborted)\b",
    r"다운로드\s*(?:중단|취소)",
]


# ---------------------------------------------------------------------------
# A. Multi-stage ZIP discovery & stage-scoped temporal context
# ---------------------------------------------------------------------------

@dataclass
class DiscoveredStage:
    stage_id: str
    stage_role: str
    leveldb_path: Path
    blob_path: Path | None
    history_path: Path | None
    rel_leveldb_posix: str
    max_file_number: int

    def to_metadata(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "stage_role": self.stage_role,
            "rel_leveldb_path": self.rel_leveldb_posix,
            "has_blob": self.blob_path is not None,
            "has_history": self.history_path is not None,
            "max_leveldb_file_number": self.max_file_number,
        }


def _max_ldb_log_number(leveldb_path: Path) -> int:
    best = -1
    try:
        for p in leveldb_path.iterdir():
            m = re.fullmatch(r"(\d+)\.(?:ldb|log)", p.name, re.IGNORECASE)
            if m:
                best = max(best, int(m.group(1)))
    except Exception:
        pass
    return best


def _infer_stage_id_and_role(rel_leveldb: Path, total_stages: int) -> tuple[str, str]:
    parts = list(rel_leveldb.parts)
    # Strip trailing .../IndexedDB/https_www.perplexity.ai_0.indexeddb.leveldb
    if parts and parts[-1] == leg.TARGET_LEVELDB_NAME:
        parts = parts[:-1]
    if parts and parts[-1].lower() == "indexeddb":
        parts = parts[:-1]
    if parts and parts[-1].lower() in {"default", "profile 1", "profile 2"}:
        parts = parts[:-1]

    if total_stages <= 1:
        stage_id = parts[-1] if parts else "default"
        return stage_id, "single_snapshot"

    # Collection roles are hints from stage directory names.
    stage_id = parts[-1] if parts else "stage"
    low = stage_id.lower()
    if low in {"u", "undeleted", "before", "pre", "before_delete"} or "undelet" in low or "before" in low:
        role = "pre_deletion"
    elif low in {"d", "deleted", "after_delete"} or ("delete" in low and "undelet" not in low):
        role = "post_deletion"
    elif low in {"r", "reopen", "reopened", "restart"} or "restart" in low or "reopen" in low:
        role = "post_restart"
    elif "private" in low:
        role = "post_private"
    elif "detail" in low:
        role = "detail_view"
    elif "after" in low or "list" in low:
        role = "initial_collection"
    else:
        role = "stage_snapshot"
    return stage_id, role


def _find_sibling_history(leveldb_path: Path, root_path: Path) -> Path | None:
    """Locate Chromium History SQLite DB strictly inside the same stage profile."""
    # Typical layout: <stage>/Default/IndexedDB/https_www.perplexity.ai_0.indexeddb.leveldb
    # Sibling History: <stage>/Default/History
    # Alt layout: <stage>/IndexedDB/https_www.perplexity.ai_0.indexeddb.leveldb -> <stage>/History
    candidates = []
    if leveldb_path.parent.name.lower() == "indexeddb":
        profile_dir = leveldb_path.parent.parent
        candidates.append(profile_dir / "History")
    candidates.append(leveldb_path.parent / "History")
    for cand in candidates:
        try:
            cand.resolve().relative_to(root_path.resolve())
        except Exception:
            continue
        if cand.is_file():
            return cand
    return None


def discover_leveldb_stages(root_path: Path) -> list[DiscoveredStage]:
    root_path = Path(root_path).resolve()
    if root_path.is_dir() and root_path.name == leg.TARGET_LEVELDB_NAME:
        matches = [root_path]
    else:
        matches = [p for p in root_path.rglob(leg.TARGET_LEVELDB_NAME) if p.is_dir()]
    if not matches:
        raise FileNotFoundError(
            f"[leveldb_not_found] Could not find {leg.TARGET_LEVELDB_NAME} under {root_path}. "
            "Pass a LevelDB directory, profile folder, or ZIP containing IndexedDB."
        )

    raw_stages: list[DiscoveredStage] = []
    used_ids: dict[str, int] = {}
    for ldb in matches:
        try:
            rel = ldb.relative_to(root_path)
            rel_posix = rel.as_posix()
        except Exception:
            rel = Path(ldb.name)
            rel_posix = ldb.name
        stage_id, stage_role = _infer_stage_id_and_role(rel, len(matches))
        # Ensure unique stage_id even if duplicate leaf names exist
        if stage_id in used_ids:
            used_ids[stage_id] += 1
            stage_id = f"{stage_id}_{used_ids[stage_id]}"
        else:
            used_ids[stage_id] = 1
        blob = ldb.parent / leg.TARGET_BLOB_NAME
        hist = _find_sibling_history(ldb, root_path)
        raw_stages.append(
            DiscoveredStage(
                stage_id=stage_id,
                stage_role=stage_role,
                leveldb_path=ldb,
                blob_path=blob if blob.exists() else None,
                history_path=hist,
                rel_leveldb_posix=rel_posix,
                max_file_number=_max_ldb_log_number(ldb),
            )
        )

    # Order stages chronologically by LevelDB log/ldb file number (not alphabetically!)
    raw_stages.sort(key=lambda s: (s.max_file_number, len(s.leveldb_path.parts), s.rel_leveldb_posix))
    return raw_stages


def select_stage(stages: list[DiscoveredStage], selector: str) -> DiscoveredStage:
    sel = selector.strip()
    sel_low = sel.lower()
    # 1. Exact stage_id match
    exact = [s for s in stages if s.stage_id.lower() == sel_low]
    if len(exact) == 1:
        return exact[0]
    # 2. Exact stage_role match
    by_role = [s for s in stages if s.stage_role.lower() == sel_low]
    if len(by_role) == 1:
        return by_role[0]
    # 3. Substring match on stage_id or rel_leveldb_posix
    sub = [s for s in stages if sel_low in s.stage_id.lower() or sel_low in s.rel_leveldb_posix.lower()]
    if len(sub) == 1:
        return sub[0]
    available = ", ".join(f"{s.stage_id} ({s.stage_role})" for s in stages)
    if len(sub) > 1:
        raise ValueError(f"Ambiguous --stage {selector!r}; matched {[s.stage_id for s in sub]}. Available: {available}")
    raise ValueError(f"Stage {selector!r} not found. Available stages: {available}")


def build_scoped_source_file_temporal_context(
    original_input: Path,
    root_path: Path,
    leveldb_path: Path,
    single_stage_archive: bool = False,
) -> dict[str, Any]:
    """Strictly stage-scoped replacement for legacy _v34_build_source_file_temporal_context.

    Fixes Bug 4: never matches sibling stages in a multi-stage ZIP via the broad
    ``(TARGET_LEVELDB_NAME + "/") in entry_name`` fallback.
    """
    files: list[dict[str, Any]] = []
    original_input = Path(original_input)
    leveldb_path = Path(leveldb_path)
    root_path = Path(root_path)

    def add_file(item: dict[str, Any]) -> None:
        sf = item.get("source_file")
        if not leg._v32_is_real_source_file(sf):
            return
        item.setdefault("source_file_type", leg._v34_file_type_from_name(sf))
        files.append(item)

    try:
        if leg.is_zip_path(original_input):
            try:
                rel_leveldb = leveldb_path.relative_to(root_path).as_posix().rstrip("/")
            except Exception:
                rel_leveldb = ""
            prefix = (rel_leveldb + "/") if rel_leveldb else ""
            with zipfile.ZipFile(original_input, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    entry_name = info.filename.replace("\\", "/").lstrip("./")
                    base = Path(entry_name).name
                    if not leg._v32_is_real_source_file(base):
                        continue
                    if prefix:
                        # Strict prefix match inside the selected stage directory ONLY.
                        in_target = entry_name.startswith(prefix) and ("/" not in entry_name[len(prefix):])
                    elif single_stage_archive:
                        in_target = (leg.TARGET_LEVELDB_NAME + "/") in entry_name
                    else:
                        in_target = False
                    if not in_target:
                        continue
                    raw_time = leg._v34_zipinfo_local_iso(info)
                    add_file({
                        "source_file": base,
                        "zip_entry_path": entry_name,
                        "modified_time_raw": raw_time,
                        "modified_time_interpretation": {
                            "raw": raw_time,
                            "interpreted_utc": None,
                            "timezone_basis": "zip_entry_local_or_unspecified",
                            "confidence": "medium_for_within_archive_order_low_for_utc_conversion",
                        },
                        "size_bytes": int(info.file_size),
                        "compressed_size_bytes": int(info.compress_size),
                        "crc32": f"{info.CRC:08x}",
                        "mtime_source": "zip_entry_metadata",
                    })
        else:
            for file_path in leveldb_path.iterdir():
                if not file_path.is_file():
                    continue
                base = file_path.name
                if not leg._v32_is_real_source_file(base):
                    continue
                st = file_path.stat()
                dt_utc = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
                add_file({
                    "source_file": base,
                    "source_path": str(file_path),
                    "modified_time_raw": dt_utc,
                    "modified_time_interpretation": {
                        "raw": dt_utc,
                        "interpreted_utc": dt_utc,
                        "timezone_basis": "filesystem_stat_epoch_utc",
                        "confidence": "medium_file_context_not_per_record_creation_time",
                    },
                    "size_bytes": int(st.st_size),
                    "mtime_source": "filesystem_stat_mtime",
                })
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "input": str(original_input),
            "leveldb_path": str(leveldb_path),
            "stage_scoped": True,
        }

    files.sort(
        key=lambda x: (
            leg._v34_source_time_sort_key(x),
            str(x.get("source_file") or ""),
            str(x.get("zip_entry_path") or x.get("source_path") or ""),
        )
    )
    by_source: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        by_source.setdefault(str(item.get("source_file")), []).append(item)

    return {
        "available": True,
        "input": str(original_input),
        "input_kind": "zip" if leg.is_zip_path(original_input) else "folder_or_leveldb",
        "leveldb_path": str(leveldb_path),
        "stage_scoped": True,
        "collection_method": "zip_entry_metadata" if leg.is_zip_path(original_input) else "filesystem_stat_mtime",
        "source_file_count": len(files),
        "files": files,
        "by_source_file": by_source,
        "earliest_source_file_time": files[0].get("modified_time_raw") if files else None,
        "latest_source_file_time": files[-1].get("modified_time_raw") if files else None,
        "timezone_policy": (
            "ZIP entry times are local/unspecified and are not converted to UTC; "
            "filesystem stat times are epoch-derived UTC. Scoped strictly to the selected stage LevelDB directory."
        ),
        "caution": (
            "Source-file modified times show archive/filesystem context for this stage only. "
            "They corroborate, not replace, application timestamps and LevelDB sequence order."
        ),
    }


# ---------------------------------------------------------------------------
# B. Automatic & explicit IndexedDB database discovery
# ---------------------------------------------------------------------------

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
                    tokens = [k for k in tokens if isinstance(k,str)] if isinstance(tokens,(list,tuple)) else []
                    if any(k in SINGLE_THREAD_KEY_MARKERS or k in {"graphql-sidebar-v2","sidebar-snapshot","/rest/thread/list_ask_threads","/rest/thread/list_recent","/rest/thread/list_pinned_ask_threads"} for k in tokens):
                        known_keys += 1
                    if "pplx-query-cache" in ks or "all_results" in ks or "thread_metadata" in ks or "/rest/thread/" in ks:
                        pplx_key_count += 1
            discovered.append({
                "name": db.name,
                "stores": stores,
                "has_target_store": has_target_store,
                "record_count": rec_count,
                "perplexity_cache_record_count": pplx_key_count,
                "recognized_thread_keys": known_keys,
                "unreadable_records": decode_failures,
                "is_pplx_query_cache": db.name.startswith("pplx-query-cache-"),
                "is_legacy_keyval_store": db.name == leg.DEFAULT_DATABASE,
            })
    finally:
        close = getattr(wrapper, "close", None)
        if callable(close):
            close()
    return discovered


def resolve_database_selection(
    leveldb_path: Path,
    blob_path: Path | None,
    requested_database: str | None,
    object_store_name: str = leg.DEFAULT_STORE,
) -> tuple[str, dict[str, Any]]:
    """Resolve database name automatically or validate an explicit user choice."""
    try:
        discovered = discover_query_databases(leveldb_path, blob_path, object_store_name)
    except Exception as exc:
        raise RuntimeError(f"[database_discovery_failed] Cannot inspect {leveldb_path}: {type(exc).__name__}: {exc}") from exc
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
        reason = "Auto-selected account-scoped pplx-query-cache-* database with active Perplexity cache records."
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


# ---------------------------------------------------------------------------
# C. Structural multi-thread list isolation & per-entry attribution
# ---------------------------------------------------------------------------

def infer_record_kind_v038(record: leg.ForensicRecord) -> str:
    """Identify record kind using key structure before scanning value text.

    Fixes Bug 3 (part 1): prevents multi-thread sidebar/list caches such as
    ``graphql-sidebar-v2`` from being mislabeled as single ``computer_task``
    records merely because their values contain ``/computer/tasks/<uuid>``.
    """
    tokens = record.key_tokens
    key_low = (record.key or "").lower()

    # 1. Explicit multi-thread list/cache keys FIRST
    if "/rest/thread/list_recent" in key_low:
        return "thread_list_recent"
    if "/rest/thread/list_pinned_ask_threads" in key_low:
        return "thread_list_pinned"
    if (
        "graphql-sidebar-v2" in key_low
        or "sidebar-snapshot" in key_low
        or "initial-pages-with-teammates" in key_low
    ):
        return "thread_list_sidebar"
    if "/rest/thread/list_ask_threads" in key_low:
        text_low = record.text(4000).lower()
        if "thread_type_filter" in text_low or '"mode": "asi"' in text_low or '"mode":"asi"' in text_low:
            return "asi_thread_list"
        return "thread_list_ask"
    if "topmosturls" in key_low:
        return "top_most_urls"

    # 2. Explicit single-thread core & helper keys
    if "all_results" in tokens:
        return "all_results"
    if "thread_metadata" in tokens:
        return "thread_metadata"
    if any(m in tokens for m in ("thread_first_entry", "thread_latest_entry", "thread_pagination_state")):
        return "thread_entry_cache"

    # 3. Fallback to legacy inference on value text, guarded against multi-task lists
    text = record.text(4000)
    if "/rest/thread/list_recent" in text:
        return "thread_list_recent"
    if "/rest/thread/list_ask_threads" in text and (
        "thread_type_filter" in text or '"mode": "asi"' in text.lower() or '"mode":"asi"' in text.lower()
    ):
        return "asi_thread_list"
    if "topMostUrls" in text:
        return "top_most_urls"
    if "all_results" in text:
        return "all_results"
    if "thread_metadata" in text:
        return "thread_metadata"
    if "/computer/tasks/" in text:
        # Only label as single computer_task if it does not list multiple distinct tasks
        task_ids = set(re.findall(r"/computer/tasks/([0-9a-fA-F\-]{36})", record.text(40000)))
        if len(task_ids) > 1:
            return "thread_list_sidebar"
        return "computer_task"
    if "COMET_AGENT_TOOL_INPUT" in text or "COMET_AGENT_TOOL_OUTPUT" in text:
        return "tool_io"
    return "unknown"


def primary_thread_id_from_key_v038(record: leg.ForensicRecord) -> str | None:
    tokens = record.key_tokens
    for marker in SINGLE_THREAD_KEY_MARKERS:
        if marker in tokens:
            idx = tokens.index(marker)
            if idx + 1 < len(tokens):
                candidate = tokens[idx + 1]
                if leg.UUID_RE.fullmatch(candidate):
                    return candidate
                uuids = leg.extract_uuids_from_text(candidate)
                if uuids:
                    return uuids[0]
    for marker in ("all_results", "thread_metadata"):
        if marker in record.key:
            uuids = leg.extract_uuids_from_text(record.key)
            if uuids:
                return uuids[-1]
    return None


def extract_unique_computer_task_ids(record: leg.ForensicRecord) -> list[str]:
    text = record.text(40000)
    seen: list[str] = []
    for m in re.finditer(r"/computer/tasks/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", text):
        uid = m.group(1)
        if uid not in seen:
            seen.append(uid)
    return seen


def group_records_v038(
    records: list[leg.ForensicRecord],
) -> tuple[dict[str, list[leg.ForensicRecord]], list[leg.ForensicRecord]]:
    """Structurally group records without dumping multi-thread lists into one thread.

    Fixes Bug 3 (part 2):
    1. Multi-thread list/global records ALWAYS go to ``global_records``.
    2. Single-thread keyed records (``all_results``, ``thread_metadata``, etc.)
       are grouped by their own key UUID BEFORE checking ``/computer/tasks/``.
    3. ``/computer/tasks/<uuid>`` grouping applies ONLY when a non-list record
       references exactly ONE distinct task UUID.
    """
    groups: dict[str, list[leg.ForensicRecord]] = {}
    global_records: list[leg.ForensicRecord] = []

    for record in records:
        if record.record_kind in MULTI_THREAD_LIST_KINDS:
            global_records.append(record)
            continue

        thread_id = primary_thread_id_from_key_v038(record)
        if thread_id and record.record_kind == "all_results" and isinstance(record.value, list):
            # A paginated conversation may contain turns from multiple backend UUIDs.
            # Keep the physical key and evidence while assigning each entry to its author.
            by_turn = {}
            for index, entry in enumerate(record.value):
                owner = entry.get("backend_uuid") if isinstance(entry, dict) else None
                group = by_turn.setdefault(owner if isinstance(owner, str) and owner else thread_id, {})
                group[index] = entry
            if by_turn:
                for owner, entries in by_turn.items():
                    # Keep original array indexes for provenance paths, including paginated turns.
                    scoped = record if len(by_turn) == 1 else replace(
                        record, value=[entries.get(i) for i in range(len(record.value))]
                    )
                    groups.setdefault(owner, []).append(scoped)
                continue
        if thread_id:
            groups.setdefault(thread_id, []).append(record)
            continue

        task_ids = extract_unique_computer_task_ids(record)
        if len(task_ids) == 1:
            groups.setdefault(f"computer:{task_ids[0]}", []).append(record)
            continue
        if len(task_ids) > 1:
            global_records.append(record)
            continue

        if record.record_kind == "unknown":
            global_records.append(record)
            continue

        value_candidates = leg.get_value_uuid_candidates(record)
        if len(value_candidates) == 1:
            groups.setdefault(next(iter(value_candidates)), []).append(record)
        else:
            global_records.append(record)

    return groups, global_records


def _make_scoped_entry_record(source_record: leg.ForensicRecord, entry: dict[str, Any]) -> leg.ForensicRecord:
    """Create an isolated ForensicRecord containing ONLY one thread's list entry."""
    fr = leg.ForensicRecord(
        key=source_record.key,
        value=entry,
        ldb_seq_no=source_record.ldb_seq_no,
        is_live=source_record.is_live,
        source_types=list(source_record.source_types),
        evidence=list(source_record.evidence),
    )
    fr.key_tokens = list(source_record.key_tokens)
    fr.record_kind = "thread_list_entry_scoped"
    return fr


def _iter_sidebar_or_recent_task_entries(obj: Any) -> list[dict[str, Any]]:
    """Extract individual thread entries from graphql-sidebar-v2 / list_recent."""
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        value = leg.parse_maybe_json(value)
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if not isinstance(value, dict):
            return
        uid = value.get("uuid") or value.get("thread_uuid")
        link = str(value.get("link") or "")
        is_container = any(k in value for k in ("threads", "pages", "edges", "nodes"))
        if (
            isinstance(uid, str)
            and leg.UUID_RE.fullmatch(uid.strip())
            and not is_container
            and (
                "/computer/tasks/" in link
                or str(value.get("mode") or "").lower() == "asi"
                or str(value.get("search_mode") or "").lower() == "asi"
                or str(value.get("mode_type") or "") == "4"
            )
        ):
            found.append(leg.to_jsonable(value))
            return  # Do not descend into child metadata of a leaf thread entry
        for child in value.values():
            walk(child)

    walk(obj)
    return found


def collect_asi_thread_list_candidates_v038(records: list[leg.ForensicRecord]) -> dict[str, dict[str, Any]]:
    """Collect per-thread candidates from ASI list caches & sidebar snapshots with strict entry isolation."""
    candidates: dict[str, dict[str, Any]] = {}

    def add_entry(thread_id: str, entry: dict[str, Any], source_rec: leg.ForensicRecord, source_kind: str) -> None:
        tid = thread_id.strip()
        if not tid:
            return
        entry_copy = dict(entry)
        entry_copy.setdefault("_v38_list_source_kind", source_kind)
        bucket = candidates.setdefault(tid, {"entries": [], "records": [], "source_kinds": set()})
        bucket["entries"].append(entry_copy)
        bucket["records"].append(_make_scoped_entry_record(source_rec, entry_copy))
        bucket["source_kinds"].add(source_kind)

    # Sort by sequence ascending so latest entries appear last in bucket["entries"]
    ordered_records = sorted(records, key=lambda r: (r.ldb_seq_no if isinstance(r.ldb_seq_no, int) else -1))

    for record in ordered_records:
        if record.record_kind == "asi_thread_list" or (
            "/rest/thread/list_ask_threads" in record.key and "asi" in record.text(4000).lower()
        ):
            for entry in leg.iter_asi_entry_dicts_v3(record.value, assume_asi_context=True):
                tid = str(entry.get("uuid") or entry.get("thread_uuid") or entry.get("slug") or entry.get("context_uuid") or "").strip()
                add_entry(tid, entry, record, "asi_thread_list")
        elif record.record_kind in {"thread_list_sidebar", "thread_list_recent"}:
            for entry in _iter_sidebar_or_recent_task_entries(record.value):
                tid = str(entry.get("uuid") or entry.get("thread_uuid") or "").strip()
                add_entry(tid, entry, record, record.record_kind)

    return candidates


def build_promoted_computer_thread_v038(
    thread_id: str,
    candidate: dict[str, Any],
    global_context_urls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build promoted thread using strictly entry-scoped records and preview labeling."""
    # Because candidate["records"] already holds scoped single-entry ForensicRecords,
    # legacy build_promoted_computer_thread cannot read any other thread's fields.
    source_kinds = set(candidate.get("source_kinds") or [])
    entries: list[dict[str, Any]] = candidate.get("entries") or []
    records: list[leg.ForensicRecord] = candidate.get("records") or []

    promoted = _ORIG_BUILD_PROMOTED(thread_id, candidate, global_context_urls)

    # Ensure sidebar/list previews carry explicit preview-only provenance and
    # preserve reference codes seen in early pending titles if no full prompt exists.
    if not (promoted.get("prompt") or {}).get("text"):
        ref_title = None
        for ent in entries:
            t = str(ent.get("query_str") or ent.get("title") or "").strip()
            if t and leg.extract_reference_codes(t):
                ref_title = t
                break
        if ref_title:
            latest_rec = max(records, key=lambda r: r.ldb_seq_no or -1) if records else None
            promoted["prompt"] = {
                "text": ref_title,
                "field": "sidebar_title_with_reference",
                "reference_codes": leg.extract_reference_codes(ref_title),
                "evidence": leg.record_evidence(latest_rec) if latest_rec else [],
                "note": "Recovered from truncated sidebar/list title containing an explicit reference code; full prompt requires all_results.",
            }

    fa = promoted.get("final_answer") or {}
    if source_kinds <= {"thread_list_sidebar", "thread_list_recent"}:
        if not fa.get("available"):
            for rec in reversed(records):
                preview = rec.value.get("answer_preview")
                if isinstance(preview, str) and preview.strip():
                    fa.update(text=preview, available=True, raw_value_path="answer_preview",
                              relative_order=rec.ldb_seq_no, evidence=leg.record_evidence(rec))
                    fa.pop("reason", None)
                    break
        fa["answer_scope"] = "list_preview_only"
        if fa.get("available"):
            fa["source_kind"] = "sidebar_list_answer_preview"
            fa["caution"] = (
                "Recovered from per-entry sidebar/list answer_preview only (not full all_results body). "
                "Scoped strictly to this thread's own UUID/context_uuid entry."
            )
        promoted["final_answer"] = fa
        meta = promoted.get("metadata") or {}
        meta["source_kind"] = "sidebar_or_recent_list_entry_promoted"
        # Preserve context_uuid from any entry carrying it
        if not meta.get("context_uuid"):
            for ent in reversed(entries):
                if ent.get("context_uuid"):
                    meta["context_uuid"] = ent.get("context_uuid")
                    break
        promoted["metadata"] = meta
        promoted["promotion_note"] = (
            "Promoted from per-entry graphql-sidebar-v2 / list_recent cache (preview-only; "
            "no all_results core record present in this snapshot)."
        )
    else:
        fa.setdefault("answer_scope", "asi_list_cache_first_answer")
        promoted["final_answer"] = fa

    # The winning string and its physical evidence must come from the same entry.
    for name, keys in (("prompt", ("query_str", "title", "user_query")),
                       ("final_answer", ("answer_preview", "first_answer", "preview"))):
        field = promoted.get(name) or {}
        value = field.get("text")
        if not value:
            continue
        matches = [(rec, key) for rec in records for key in keys
                   if isinstance(rec.value.get(key), str) and rec.value[key].strip() == value.strip()]
        if matches:
            rec, key = matches[0] if name == "prompt" else matches[-1]
            field.update(text=rec.value[key], evidence=leg.record_evidence(rec), relative_order=rec.ldb_seq_no)
            if name == "final_answer":
                field["raw_value_path"] = key
            else:
                field["field"] = key
    cls = classify_group_v038(records, browser_only=False)
    cls["reconstruction_status"] = promoted["classification"]["reconstruction_status"]
    promoted["classification"] = cls
    return promoted


def _v07_records_for_thread_v038(
    thread: dict[str, Any],
    group_map: dict[str, list[leg.ForensicRecord]],
    relevant_records: list[leg.ForensicRecord],
) -> list[leg.ForensicRecord]:
    """Never pull multi-thread list records back into a single thread during v0.7 repair."""
    tid = str(thread.get("thread_id") or "")
    if tid in group_map:
        return group_map[tid]
    if tid.startswith("computer:") and tid.split(":", 1)[1] in group_map:
        return group_map[tid.split(":", 1)[1]]
    non_list = [r for r in relevant_records if r.record_kind not in MULTI_THREAD_LIST_KINDS]
    return _ORIG_V07_RECORDS_FOR_THREAD(thread, group_map, non_list)


# ---------------------------------------------------------------------------
# E. Exact PDF filename extraction (preserving repeated extensions like .pdf.pdf)
# ---------------------------------------------------------------------------

def _join_wrapped_pdf_extension(text):
    return re.sub(r"[ \t]*\r?\n[ \t]*(?=\.[ \t]*pdf\b)|(?<=\.)[ \t]*\r?\n[ \t]*(?=pdf\b)", "", text, flags=re.I)


def _v38_cleanup_pdf_filename(raw: str) -> str | None:
    value = _join_wrapped_pdf_extension(str(raw or "")).strip().strip("*\x60\"'").strip()
    if re.fullmatch(r'[^<>:"/\\|?*\r\n]+(?:\.pdf)+', value, re.I):
        return value
    return None


def _named_pdfs(text):
    matches = [(m.start(1), m.end(1), m.group(1)) for m in _PDF_LABELED.finditer(text)]
    matches.extend((m.start(2), m.end(2), m.group(2)) for m in _PDF_QUOTED.finditer(text))
    return matches


def extract_pdf_filenames_v038(text: str) -> list[str]:
    source = _join_wrapped_pdf_extension(str(text or ""))
    named = _named_pdfs(source)
    values = {v for _, _, v in named}
    for m in PDF_FILENAME_RE_V38.finditer(source):
        if not any(start <= m.start() and m.end() <= end for start, end, _ in named):
            values.add(m.group())
    return sorted(values, key=lambda x: (-len(x), x.casefold(), x))


def _has_any_regex(text: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if re.search(pat, text, flags=re.IGNORECASE):
            return True
    return False


def _has_positive_download_completion_v038(final_text: str) -> bool:
    norm = " ".join(final_text.split())
    if _download_negative_status(norm) or _has_any_regex(norm, _DOWNLOAD_NEGATION_OR_FAILURE_PATTERNS):
        return False
    return _has_any_regex(norm, _DOWNLOAD_COMPLETION_PATTERNS_V38)


def _download_negative_status(text: str) -> str | None:
    if _has_any_regex(text, _DOWNLOAD_FAILURE_PATTERNS_V38 + [r"\b(?:saving|download|save) (?:was |is )?blocked\b"]):
        return "download_failed"
    if _has_any_regex(text, _DOWNLOAD_INTERRUPTED_PATTERNS_V38):
        return "download_interrupted"
    if _has_any_regex(text, [r"\b(?:waiting|awaiting) (?:for )?(?:your )?(?:approval|confirmation)\b",
                             r"\b(?:need to confirm|please confirm|confirmation required|may I proceed)\b"]):
        return "download_confirmation_pending"
    return None


def _v23_reported_downloaded_filename_v038(thread: dict[str, Any]) -> str | None:
    final = leg._v23_final_text(thread)
    if not _has_positive_download_completion_v038(final):
        return None
    source = _join_wrapped_pdf_extension(final.replace("**", ""))
    names = {m.group(1) for m in _PDF_LABELED.finditer(source)}
    if names:
        return next(iter(names)) if len(names) == 1 else None
    pdfs = extract_pdf_filenames_v038(source)
    return pdfs[0] if len(pdfs) == 1 else None


# ---------------------------------------------------------------------------
# D. Download classification & optional stage-scoped Chromium History corroboration
# ---------------------------------------------------------------------------

def _v21_current_thread_signals_v038(thread: dict[str, Any]) -> dict[str, Any]:
    signals = _ORIG_V21_SIGNALS(thread)
    parts = leg._v21_thread_local_parts(thread)
    prompt = parts["prompt_low"]
    final_raw = parts["final"]
    evidence = parts["evidence_low"]
    all_low = parts["all_low"]
    url_low = leg._v21_url_text(thread).lower()

    positive_download_prompt = leg._v21_has_any(
        prompt, ["download", "downloaded filename", "save the pdf", "pdf file", "official pdf"]
    )
    positive_download_final = _has_positive_download_completion_v038(final_raw)
    positive_download_action = leg._v21_has_any(
        evidence + "\n" + url_low,
        ["wait_for_download", "browser_downloads", "다운로드 확인", "download", "ctrl+j", ".pdf", "save the pdf"],
    )
    filename_candidates = extract_pdf_filenames_v038(parts["all"])

    download_anchor = bool(
        (positive_download_prompt or positive_download_final or positive_download_action)
        and (filename_candidates or ".pdf" in all_low or positive_download_final or positive_download_action)
    )
    if leg.has_negated_term(parts["prompt"], ["download", "file", "pdf"]) and not (
        positive_download_final or positive_download_action
    ):
        download_anchor = False

    # Same principled weight formula as v0.21 (3 + 4 + 2 + 1) — NO blind score inflation!
    download_score = (
        (3 if positive_download_prompt else 0)
        + (4 if positive_download_final else 0)
        + (2 if positive_download_action else 0)
        + (1 if filename_candidates else 0)
    )

    scores = dict(signals.get("scores") or {})
    anchors = dict(signals.get("anchors") or {})
    prompt_markers = dict(signals.get("prompt_markers") or {})
    trace_markers = dict(signals.get("trace_markers") or {})

    scores["file_download"] = download_score
    anchors["file_download"] = download_anchor
    prompt_markers["download"] = positive_download_prompt
    trace_markers["download"] = positive_download_final or positive_download_action
    trace_markers["download_completion_in_final"] = positive_download_final
    trace_markers["pdf_filename_candidates"] = filename_candidates

    signals = dict(signals)
    signals["scores"] = scores
    signals["anchors"] = anchors
    signals["prompt_markers"] = prompt_markers
    signals["trace_markers"] = trace_markers
    return signals


def _v23_update_download_outcome_v038(thread: dict[str, Any], reported_pdf: str | None) -> dict[str, Any]:
    outcome = _ORIG_V23_UPDATE_DOWNLOAD(thread, reported_pdf)
    if outcome.get("task_type") != "file_download":
        return outcome

    final = leg._v23_final_text(thread)
    if reported_pdf:
        all_pdfs = extract_pdf_filenames_v038(leg._v23_thread_text(thread))
        nearby = [p for p in all_pdfs if p.lower() != reported_pdf.lower()]
        outcome["reported_downloaded_filename"] = reported_pdf
        outcome["downloaded_filename_candidates"] = [reported_pdf]
        outcome["nearby_pdf_candidates"] = nearby
        if _has_positive_download_completion_v038(final):
            outcome["status"] = "completed_download"
            outcome["side_effect_completed"] = True
            outcome["confidence"] = "high"
    return outcome


def classify_task_outcome_v038(thread: dict[str, Any]) -> dict[str, Any]:
    result = _ORIG_CLASSIFY_TASK_OUTCOME(thread)
    if result.get("task_type") not in {"file_download", "download"}:
        return result

    final = str((thread.get("final_answer") or {}).get("text") or "")
    outcome_text = leg._v33_download_outcome_text(thread)
    reported_pdf = _v23_reported_downloaded_filename_v038(thread)
    filenames = leg._v29_unique_keep_order(
        ([reported_pdf] if reported_pdf else [])
        + extract_pdf_filenames_v038(outcome_text)
        + leg._v29_as_list(result.get("downloaded_filename_candidates")),
        20,
    )
    if reported_pdf:
        result["reported_downloaded_filename"] = reported_pdf
    negative = _download_negative_status(" ".join(final.split()))
    if not negative and not _has_positive_download_completion_v038(final):
        if leg._v33_has_any(leg._v33_norm_text(outcome_text), leg._v33_download_confirmation_markers()):
            negative = "download_confirmation_pending"
    if negative:
        result.update(status=negative, side_effect_completed=False, confidence="high",
                      reported_downloaded_filename=None, downloaded_filename_candidates=[])
    elif _has_positive_download_completion_v038(final):
        result.update(status="completed_download", side_effect_completed=True, confidence="high",
                      downloaded_filename_candidates=filenames)
    elif result.get("side_effect_completed") or result.get("status") == "completed_download":
        result.update(status="source_discovery_or_partial_download", side_effect_completed=False,
                      confidence="medium", reported_downloaded_filename=None, downloaded_filename_candidates=[])
    result["classifier_version"] = "v0.38_guarded_download_classifier"
    return result


def _chrome_time_to_utc_iso(us: int | None) -> str | None:
    if not us:
        return None
    try:
        dt = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(us))
        return dt.isoformat()
    except Exception:
        return None


def read_chromium_history_downloads(history_path: Path | None, diagnostics=None) -> list[dict[str, Any]]:
    if history_path is None or not history_path.is_file():
        return []
    state_labels = {0: "IN_PROGRESS", 1: "COMPLETE", 2: "CANCELLED", 3: "INTERRUPTED"}
    rows_out: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(history_path.resolve().as_uri()+"?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT id, guid, current_path, target_path, start_time, received_bytes, total_bytes, "
                "state, danger_type, interrupt_reason, end_time, tab_url, mime_type FROM downloads"
            ).fetchall()
            chains: dict[int, list[str]] = {}
            for did, _cidx, url in cur.execute(
                "SELECT id, chain_index, url FROM downloads_url_chains ORDER BY id, chain_index"
            ).fetchall():
                chains.setdefault(int(did), []).append(str(url))
        finally:
            conn.close()
        for r in rows:
            target_path = str(r[3] or r[2] or "")
            filename = Path(target_path.replace("\\", "/")).name if target_path else ""
            st_code = int(r[7]) if r[7] is not None else -1
            rows_out.append({
                "id": int(r[0]),
                "guid": str(r[1] or ""),
                "current_path": str(r[2] or ""),
                "target_path": target_path,
                "filename": filename,
                "start_time_utc": _chrome_time_to_utc_iso(r[4]),
                "end_time_utc": _chrome_time_to_utc_iso(r[10]),
                "received_bytes": int(r[5]) if r[5] is not None else None,
                "total_bytes": int(r[6]) if r[6] is not None else None,
                "state": st_code,
                "state_label": state_labels.get(st_code, f"UNKNOWN({st_code})"),
                "danger_type": int(r[8]) if r[8] is not None else None,
                "interrupt_reason": int(r[9]) if r[9] is not None else None,
                "tab_url": str(r[11] or ""),
                "mime_type": str(r[12] or ""),
                "url_chain": chains.get(int(r[0]), []),
            })
    except (sqlite3.Error, OSError, TypeError, ValueError, OverflowError) as exc:
        message = f"{history_path.name}: {type(exc).__name__}: {exc}"
        if diagnostics is not None:
            diagnostics.append({"code":"history_read_failed","message":message})
        else:
            print(f"[history_read_failed] {message}", file=sys.stderr)
        return []
    return rows_out


def attach_history_download_corroboration(report: dict[str, Any], history_path: Path | None) -> None:
    diagnostics = report.setdefault("diagnostics", [])
    downloads = read_chromium_history_downloads(history_path, diagnostics)
    report.setdefault("source", {})["history_path"] = str(history_path) if history_path else None
    report["source"]["history_downloads_count"] = len(downloads)
    report["source"]["history_read_status"] = (
        "read_failed" if any(d["code"] == "history_read_failed" for d in diagnostics) else
        "not_present" if history_path is None or not history_path.is_file() else
        "rows_read" if downloads else "read_empty")
    if not downloads:
        return

    for thread in report.get("threads") or []:
        if not isinstance(thread, dict):
            continue
        outcome = thread.get("task_outcome")
        if not isinstance(outcome, dict) or outcome.get("task_type") != "file_download":
            continue
        cands = [str(c) for c in (outcome.get("downloaded_filename_candidates") or []) if c]
        rep = outcome.get("reported_downloaded_filename")
        if rep and rep not in cands:
            cands.insert(0, str(rep))
        if not cands:
            continue
        cand_low = {c.lower() for c in cands}
        matched = [d for d in downloads if d["filename"].lower() in cand_low]
        if not matched:
            continue
        times = thread.get("all_time_related_values_v34") or {}
        start = times.get("earliest_interpreted_utc")
        end = times.get("latest_interpreted_utc")
        if not start or not end or not outcome.get("side_effect_completed"):
            continue
        def utc(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        matched = [row for row in matched if row.get("start_time_utc") and
                   utc(start) <= utc(row["start_time_utc"]) <= utc(end)]
        if len(matched) != 1:
            continue
        best = matched[0]
        outcome["history_download_corroboration"] = {
            "available": True,
            "matched": True,
            "match_basis": "unique_filename_and_thread_time_window_in_same_stage_history",
            "history_path": str(history_path),
            "download_record": best,
            "os_file_binary_verified": False,
            "caution": (
                "Chromium History corroborates browser download completion metadata only "
                "(no thread UUID is stored in History, and OS disk PDF binary integrity was not verified)."
            ),
        }
        # Adjust missing corroboration honestly: Chromium Downloads DB is corroborated,
        # while OS Downloads folder/file hash remains unverified.
        missing = [
            m for m in (outcome.get("missing_corroboration") or [])
            if "chromium downloads db" not in str(m).lower()
        ]
        if "OS Downloads folder/file hash" not in missing:
            missing.append("OS Downloads folder/file hash")
        outcome["missing_corroboration"] = missing
        # Reflect interrupted/cancelled History state if IndexedDB did not prove completion
        if best["state"] == 1 and best["interrupt_reason"] == 0:
            outcome["history_corroborated_complete"] = True
        elif best["state"] in (2, 3) or (best["interrupt_reason"] not in (None, 0)):
            outcome["history_corroborated_complete"] = False
        leg._v23_sync_thread_views(thread)


# ---------------------------------------------------------------------------
# Runtime patch context & public entry points
# ---------------------------------------------------------------------------

_ORIG_INFER_RECORD_KIND = leg.infer_record_kind
_ORIG_GROUP_RECORDS = leg.group_records
_ORIG_COLLECT_ASI = leg.collect_asi_thread_list_candidates_v3
_ORIG_BUILD_PROMOTED = leg.build_promoted_computer_thread
_ORIG_V07_RECORDS_FOR_THREAD = leg._v07_records_for_thread
_ORIG_EXTRACT_PDF = leg.extract_pdf_filenames
_ORIG_V21_SIGNALS = leg._v21_current_thread_signals
_ORIG_V23_REPORTED_PDF = leg._v23_reported_downloaded_filename
_ORIG_V23_UPDATE_DOWNLOAD = leg._v23_update_download_outcome
_ORIG_CLASSIFY_TASK_OUTCOME = leg.classify_task_outcome


_ORIG_METADATA = leg.extract_metadata
_ORIG_PROMPT = leg.extract_prompt
_ORIG_IS_PROMPT = leg.looks_like_user_prompt_text
_ORIG_CLEAN_THREAD = leg._v27_clean_thread_text_and_urls
_ORIG_PARTITION = leg._v28_partition_typed_payloads
_ORIG_BUILD_ACTIONS = leg.build_structured_actions
_ORIG_MAKE_PAYLOAD = leg._v18_make_payload
_ORIG_PROMOTE_INPUT = leg._v27_promote_form_input_payloads
_ORIG_IDENTITY = leg.thread_identity_key


def _core_entries(records):
    for rec in records:
        if rec.record_kind != "all_results":
            continue
        entries = rec.value if isinstance(rec.value, list) else [rec.value]
        for i, entry in enumerate(entries):
            if isinstance(entry, dict):
                yield rec, entry, f"[{i}]" if isinstance(rec.value, list) else "$"


def _dict_items(items):
    if isinstance(items, list):
        yield from ((i, item) for i, item in enumerate(items) if isinstance(item, dict))


def _workflow_steps(workflow, path, owner, nested=False, completed=True):
    if not isinstance(workflow, dict):
        return
    completed = completed and workflow.get("status", "WORKFLOW_COMPLETED") == "WORKFLOW_COMPLETED"
    for i, step in _dict_items(workflow.get("steps")):
        location = f"{path}.steps[{i}]"
        done = completed and step.get("status") == "WORKFLOW_COMPLETED"
        yield step, location, nested, done
        for j, item in _dict_items(step.get("items")):
            payload = item.get("payload")
            child = payload.get("workflow_payload") if isinstance(payload, dict) else None
            if isinstance(child, dict) and all(child.get(k) in (None, owner) for k in ("backend_uuid", "thread_uuid")):
                yield from _workflow_steps(child, f"{location}.items[{j}].payload.workflow_payload", owner,
                                           True, done and child.get("status") == "WORKFLOW_COMPLETED")


def classify_group_v038(records, metadata=None, browser_only=True):
    """Classify the retained backend and actions without inferring the selected UI mode."""
    entries = list(_core_entries(records))
    if not entries:
        entries = [(r, r.value, "$") for r in records
                   if r.record_kind in {"thread_entry_cache", "thread_list_entry_scoped"}
                   and isinstance(r.value, dict)]
    entries.sort(key=lambda item: item[0].ldb_seq_no or 0, reverse=True)
    backend = {}
    tools = {}
    skill = False
    legacy_event = False
    list_task = False
    for rec, entry, path in entries:
        if not backend and any(isinstance(entry.get(k), str) and entry[k] for k in ("mode", "search_mode", "display_model")):
            backend = {k:entry.get(k) for k in ("mode", "search_mode", "display_model")}
            backend.update(sequence=rec.ldb_seq_no, entry_path=path, evidence=leg.record_evidence(rec))
        prompt = entry.get("query_str")
        if isinstance(prompt, str) and re.match(
            r"^\s*\[[^\]\n]+\]\(https://www\.perplexity\.ai/computer/skills\?skillId=control-local-browser\)", prompt
        ):
            skill = True
        if rec.record_kind == "thread_list_entry_scoped":
            list_task |= str(entry.get("link") or "").startswith("/computer/tasks/")
        for i, block in _dict_items(entry.get("blocks")):
            legacy_event |= str(block.get("intended_usage") or "").upper() in {"COMET_AGENT_TOOL_INPUT", "COMET_AGENT_TOOL_OUTPUT"}
            for step, loc, _, _ in _workflow_steps(block.get("workflow_block"), f"{path}.blocks[{i}].workflow_block", entry.get("backend_uuid")):
                name = step.get("tool_name")
                if isinstance(name, str) and name and name not in tools:
                    tools[name] = {"sequence":rec.ldb_seq_no, "path":loc+".tool_name", "evidence":leg.record_evidence(rec)}

    mode = str(backend.get("mode") or "").upper()
    search = str(backend.get("search_mode") or "").upper()
    model = str(backend.get("display_model") or "").lower()
    browser_tools = sorted(name for name in tools if name.startswith("browser_"))
    labels = [f"entry.{k}={backend[k]}" for k in ("mode", "search_mode", "display_model") if backend.get(k)]
    if skill:
        labels.append("query_prefix:control-local-browser")
    if legacy_event:
        labels.append("legacy_browser_tool_event")
    labels.extend("tool:"+name for name in browser_tools)
    if search == "BROWSER_AGENT" or (not search and (legacy_event or model.startswith("comet_browser_agent"))):
        execution, interaction, confidence = "browser_control", "agentic", "high"
    elif search in ("SEARCH", "STUDY") and mode in ("", "COPILOT"):
        execution, interaction, confidence = "non_browser_agent", "conversational_or_search", "high"
    elif search in ("ASI","WIDE_RESEARCH") or (not search and (mode=="ASI" or model.startswith("pplx_asi") or list_task)):
        execution, interaction, confidence = "computer_mode", "agentic", "medium"
    else:
        execution, interaction, confidence = "unknown", "agentic" if tools else "unknown", "low"
    status = "partial" if execution == "unknown" else "reconstructed"
    if execution == "computer_mode":
        status = "detected_but_not_expanded" if browser_only else "partial_reconstruction"
    return {"execution_mode":execution, "interaction_type":interaction, "confidence":confidence,
            "reconstruction_status":status, "classification_evidence":labels,
            "observed_backend":backend, "observed_tool_events":tools, "observed_browser_tools":browser_tools,
            "browser_control_skill_in_query_prefix":skill, "legacy_browser_tool_event":legacy_event,
            "ui_selection":{"value":None,"status":"not_established"}}


def _extract_prompt(records):
    for rec, entry, path in sorted(_core_entries(records), key=lambda row: row[0].ldb_seq_no or -1):
        value = entry.get("query_str")
        if isinstance(value, str) and value.strip() and entry.get("backend_uuid"):
            return {"text":value, "field":"all_results.query_str", "raw_value_path":path+".query_str",
                    "reference_codes":leg.extract_reference_codes(value), "evidence":leg.record_evidence(rec)}
    return _ORIG_PROMPT(records)


def _extract_metadata(records):
    metadata = _ORIG_METADATA(records)
    owners = {}
    for rec, entry, _ in _core_entries(records):
        owner = entry.get("backend_uuid")
        if isinstance(owner, str) and owner:
            if owner not in owners or (rec.ldb_seq_no or 0) > (owners[owner].ldb_seq_no or 0):
                owners[owner] = rec
    # Recursive metadata collection also visits ancillary objects such as upsell_information.
    if len(owners) == 1 and metadata.get("backend_uuid") not in owners:
        owner, record = next(iter(owners.items()))
        metadata["backend_uuid"] = owner
        metadata["evidence"]["backend_uuid"] = leg.record_evidence(record, max_items=2)
    return metadata


def _workflow_text_items(workflow, path, owner):
    """Walk only workflow items belonging to this entry, retaining the physical path."""
    for step, loc, nested, done in _workflow_steps(workflow, path, owner):
        for j, item in _dict_items(step.get("items")):
            payload = item.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("text_payload"), dict):
                yield payload["text_payload"], f"{loc}.items[{j}].payload.text_payload", nested, done


def _exact_final(records, metadata=None, prompt_text=None):
    candidates = []
    nested_candidates = []
    unknown_variants = set()
    for rec, entry, path in _core_entries(records):
        for b, block in _dict_items(entry.get("blocks")):
            base = f"{path}.blocks[{b}]"
            markdown = block.get("markdown_block")
            md = markdown.get("answer") if isinstance(markdown, dict) else None
            completed = (entry.get("status") == "COMPLETED" or entry.get("final") is True or entry.get("text_completed") is True)
            if (isinstance(md, str) and md.strip() and completed and entry.get("final") is not False
                    and entry.get("text_completed") is not False and not markdown.get("is_streaming")
                    and str(block.get("intended_usage","")).startswith("ask_text")):
                candidates.append((rec, md, base+".markdown_block.answer"))
            workflow = block.get("workflow_block") or {}
            if not isinstance(workflow, dict):
                continue
            for payload, location, nested, complete in _workflow_text_items(
                workflow, base + ".workflow_block", entry.get("backend_uuid") or entry.get("thread_uuid")
            ):
                variant = payload.get("variant")
                if isinstance(variant, str) and variant not in ("answer","thought"):
                    unknown_variants.add(variant)
                if variant != "answer" or payload.get("is_streaming") or not complete:
                    continue
                value = payload.get("text")
                key = "text"
                chunks = payload.get("chunks")
                if not value and isinstance(chunks, list) and all(isinstance(x, str) for x in chunks):
                    value = "".join(chunks)
                    key = "chunks"
                if isinstance(value, str) and value.strip():
                    bucket = nested_candidates if nested else candidates
                    bucket.append((rec, value, f"{location}.{key}"))
    # Prefer the published outer answer. Multiple distinct child results alone cannot identify it.
    if not candidates and len({value for _, value, _ in nested_candidates}) == 1:
        candidates = nested_candidates
    if candidates:
        rec, value, path = max(candidates, key=lambda x:(x[0].ldb_seq_no or -1,len(x[1])))
        return {"text":value,"available":True,"source_kind":rec.record_kind,"raw_value_path":path,
                "relative_order":rec.ldb_seq_no,"evidence":leg.record_evidence(rec)}
    return {"text":None,"available":False,"relative_order":None,"evidence":[],
            "unsupported_variants":sorted(unknown_variants),
            "reason":"No unambiguous completed answer was identified in a supported field of this entry."}


def _clean_thread(thread):
    # The legacy presentation routine also cleans URLs. Preserve verified answer text.
    exact = thread.get("final_answer") or {}
    _ORIG_CLEAN_THREAD(thread)
    if exact.get("raw_value_path"):
        thread["final_answer"] = exact


def _make_payload(field, value, source, evidence, order=None, confidence="medium"):
    # A blank line ends the recipient paragraph. Instructions after it can contain other addresses.
    if source == "prompt" and field.strip().lower() in {"recipient", "to"}:
        value = re.split(r"\r?\n[ \t]*\r?\n", value.strip(), maxsplit=1)[0]
    return _ORIG_MAKE_PAYLOAD(field, value, source, evidence, order, confidence)


def _promote_input(thread):
    _ORIG_PROMOTE_INPUT(thread)
    if (thread.get("task_outcome") or {}).get("task_type") != "calendar_create":
        return
    typed = thread.get("typed_payloads") or []
    titles = {p.get("value") for p in typed if p.get("field") == "event_title" and p.get("payload_source") == "prompt"}
    if not titles:
        return
    # The legacy short-input heuristic labels unbound clock values as event titles.
    # Preserve those events as observed inputs, without guessing start versus end.
    explicit_titles = {p.get("value") for p in (thread.get("artifact_buckets") or {}).get("form_inputs", [])
                       if p.get("field") in {"title", "event_title"} and p.get("payload_source") != "observed_form_input"}
    thread["typed_payloads"] = [p for p in typed if not (
        p.get("field") == "event_title" and p.get("payload_source") == "observed_form_input"
        and p.get("value") not in titles | explicit_titles
        and re.fullmatch(r"\d{1,2}:\d{2}(?:\s*[AP]M)?", str(p.get("value", "")).strip(), re.IGNORECASE)
    )]


def _requested_recipient_only(payload):
    return (payload.get("field") == "recipient" and payload.get("payload_source") == "prompt"
            and not leg.EMAIL_RE.fullmatch(str(payload.get("value") or "").strip()))


def _build_structured_actions(thread):
    # A prompt-only label is not a typing event. Exclude it before action provenance is flattened.
    payloads = thread.get("typed_payloads") or []
    if any(isinstance(p, dict) and _requested_recipient_only(p) for p in payloads):
        thread = dict(thread, typed_payloads=[p for p in payloads
                      if not (isinstance(p, dict) and _requested_recipient_only(p))])
    return _ORIG_BUILD_ACTIONS(thread)


def _partition(thread):
    if (thread.get("task_outcome") or {}).get("task_type") == "email_draft_send":
        kept = []
        for p in thread.get("typed_payloads") or []:
            if _requested_recipient_only(p):
                thread.setdefault("candidate_payloads", []).append(dict(
                    p, payload_role="requested_payload", claim_strength_v28="candidate",
                    claim_reason_v28="The prompt names a recipient without an observed address."))
            else:
                kept.append(p)
        thread["typed_payloads"] = kept
    _ORIG_PARTITION(thread)
    if (thread.get("task_outcome") or {}).get("task_type") != "email_draft_send":
        return
    for name in ("candidate_payloads","residual_payloads"):
        unique = {}
        for payload in thread.get(name, []):
            unique.setdefault(leg._v27_payload_key(payload), payload)
        thread[name] = list(unique.values())
    thread["confirmed_payloads"] = list(thread.get("typed_payloads") or [])
    audit = thread.setdefault("payload_audit", {})
    audit["confirmed_payload_count"] = len(thread["confirmed_payloads"])
    audit["candidate_payload_count"] = len(thread["candidate_payloads"])
    leg._v24_sync_thread_views(thread)


@contextlib.contextmanager
def _v038_runtime_patches() -> Iterator[None]:
    """Bind v0.38 fixes cleanly for the duration of a call, restoring legacy_engine afterward."""
    overrides = {
        "extract_prompt": _extract_prompt,
        "extract_metadata": _extract_metadata,
        "classify_group": classify_group_v038,
        "looks_like_user_prompt_text": lambda text, field=None: bool(text) if field == "all_results.query_str" else _ORIG_IS_PROMPT(text, field),
        "extract_final_answer": _exact_final,
        "_v27_clean_thread_text_and_urls": _clean_thread,
        "_v18_make_payload": _make_payload,
        "build_structured_actions": _build_structured_actions,
        "_v27_promote_form_input_payloads": _promote_input,
        "_v28_partition_typed_payloads": _partition,
        "thread_identity_key": lambda t: "thread:" + str(t["thread_id"]) if t.get("thread_id") else _ORIG_IDENTITY(t),
        "infer_record_kind": infer_record_kind_v038,
        "group_records": group_records_v038,
        "collect_asi_thread_list_candidates_v3": collect_asi_thread_list_candidates_v038,
        "build_promoted_computer_thread": build_promoted_computer_thread_v038,
        "_v07_records_for_thread": _v07_records_for_thread_v038,
        "PDF_FILENAME_RE_V08": PDF_FILENAME_RE_V38,
        "extract_pdf_filenames": extract_pdf_filenames_v038,
        "_v23_pdf_filenames": extract_pdf_filenames_v038,
        "_v24_cleanup_pdf_filename": _v38_cleanup_pdf_filename,
        "_v24_pdf_filenames": extract_pdf_filenames_v038,
        "_v21_current_thread_signals": _v21_current_thread_signals_v038,
        "_v23_reported_downloaded_filename": _v23_reported_downloaded_filename_v038,
        "_v24_reported_downloaded_filename": _v23_reported_downloaded_filename_v038,
        "_v23_update_download_outcome": _v23_update_download_outcome_v038,
        "classify_task_outcome": classify_task_outcome_v038,
    }
    saved = {k: getattr(leg, k) for k in overrides}
    try:
        for k, fn in overrides.items():
            setattr(leg, k, fn)
        yield
    finally:
        for k, fn in saved.items():
            setattr(leg, k, fn)


def compute_v038_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def run_stage_v038(
    original_input: Path,
    root_path: Path,
    stage: DiscoveredStage,
    all_stages: list[DiscoveredStage],
    explicit_blob_input: Path | None = None,
    database: str = "auto",
    store: str = leg.DEFAULT_STORE,
    include_computer: bool = True,
    target_reference: str | None = None,
    dump_records_dir: Path | None = None,
) -> dict[str, Any]:
    blob_path = explicit_blob_input.expanduser().resolve() if explicit_blob_input else stage.blob_path
    if blob_path is not None and not blob_path.exists():
        raise FileNotFoundError(f"Blob input path does not exist: {blob_path}")

    selected_db, db_selection = resolve_database_selection(
        leveldb_path=stage.leveldb_path,
        blob_path=blob_path,
        requested_database=database,
        object_store_name=store,
    )

    with _v038_runtime_patches():
        source_file_temporal_context = build_scoped_source_file_temporal_context(
            original_input=original_input,
            root_path=root_path,
            leveldb_path=stage.leveldb_path,
            single_stage_archive=(len(all_stages) == 1),
        )
        extracted = leg.extract_records_from_leveldb(
            leveldb_path=stage.leveldb_path,
            blob_path=blob_path,
            database_name=selected_db,
            object_store_name=store,
        )
        extracted["source_file_temporal_context"] = source_file_temporal_context

        if dump_records_dir is not None:
            dump_records_dir.mkdir(parents=True, exist_ok=True)
            leg.write_json(dump_records_dir / "perplexity_live_records.json", {"records": extracted["live_records"]})
            leg.write_json(dump_records_dir / "perplexity_dead_records.json", {"records": extracted["dead_records"]})
            leg.write_json(dump_records_dir / "perplexity_ldb_records.json", {"records": extracted["ldb_records"]})
            leg.write_json(dump_records_dir / "perplexity_log_records.json", {"records": extracted["log_records"]})
            leg.write_json(dump_records_dir / "perplexity_unmatched_records.json", {"records": extracted["unmatched_records"]})
            leg.write_json(dump_records_dir / "perplexity_source_file_temporal_context_v38.json", source_file_temporal_context)

        report = leg.reconstruct_browser_threads(
            extracted=extracted,
            input_label=str(original_input),
            browser_only=not include_computer,
        )
        before_reference = len(report.get("threads", []))
        if target_reference:
            report = leg.filter_report_by_target_reference(report, target_reference)

        summary = report.get("payload_attribution_summary_v24") or {}
        for field in ("confirmed_payload", "candidate_payload"):
            summary[field + "_count"] = sum(len(t.get(field + "s") or []) for t in report.get("threads", []))

        # Attach optional same-stage Chromium History downloads corroboration
        attach_history_download_corroboration(report, stage.history_path)

    # Stamp explicit v0.38 schema & provenance distinct from frozen v0.37
    report["schema_version"] = SCHEMA_VERSION_V038
    report["engine_provenance"] = {
        "engine_revision": ENGINE_REVISION_V038,
        "schema_version": SCHEMA_VERSION_V038,
        "base_legacy_engine_schema": "0.37",
        "base_legacy_engine_sha256": hashlib.sha256(Path(leg.__file__).read_bytes()).hexdigest(),
        "patched_engine_file": "src/comettrace/engine_v038.py",
        "patched_engine_sha256": compute_v038_sha256(),
    }
    src = report.setdefault("source", {})
    src["database_selection"] = db_selection
    diagnostics = report.setdefault("diagnostics", [])
    def note(code, message):
        diagnostics.append({"code":code, "message":message})
    stores = [d for d in db_selection["available_databases"] if d["has_target_store"]]
    selected = next(d for d in stores if d["name"] == selected_db)
    if db_selection["selection_mode"] == "auto" and len(stores) > 1:
        note("multiple_databases", f"Selected {selected_db!r}. Other databases with store {store!r}: " +
             ", ".join(d["name"] for d in stores if d["name"] != selected_db) + ". Use --database to inspect each.")
    if selected.get("unreadable_records"):
        note("records_unreadable", f"{selected['unreadable_records']} record value(s) could not be decoded.")
    if not selected["record_count"] and not selected.get("unreadable_records"):
        note("empty_store", f"Object store {store!r} contains no readable records and reported no decoding failures.")
    if selected["record_count"] and selected.get("recognized_thread_keys") == 0:
        note("unrecognized_thread_keys", "Records exist but none has a recognized thread or sidebar key. Coverage is uncertain.")
    if not include_computer:
        omitted_browser = [g["group_id"] for g in report.get("skipped", []) if any(
            label.startswith("tool:browser_") or label == "legacy_browser_tool_event" for label in g.get("markers", []))]
        note("browser_only_scope", f"Legacy scope omits Computer/ASI expansion, including ASI browser activity. "
             f"{len(report.get('skipped', []))} grouped task(s) skipped, {len(omitted_browser)} with observed browser tools; "
             "list-only promotion disabled. Omit --browser-only for full scope.")
        diagnostics[-1]["omitted_browser_tool_groups"] = omitted_browser
    if not report.get("threads"):
        if target_reference and before_reference:
            note("reference_no_match", f"No reconstructed thread matches reference {target_reference!r}.")
        else:
            note("no_reconstructed_threads", "The selected store and analysis scope yielded no reconstructable thread.")
    if any((t.get("final_answer") or {}).get("unsupported_variants") for t in report.get("threads", [])):
        note("unsupported_answer_variant", "Some entries have an unfamiliar workflow text variant and no supported completed answer.")
    src["stage_selection"] = {
        "selected_stage": stage.to_metadata(),
        "total_stages_discovered": len(all_stages),
        "all_discovered_stages": [s.to_metadata() for s in all_stages],
        "stages_merged": False,
    }
    return report


def run_input_v038(
    input_path: Path,
    blob_input: Path | None = None,
    database: str = "auto",
    store: str = leg.DEFAULT_STORE,
    include_computer: bool = True,
    stage_selector: str | None = None,
    all_stages: bool = False,
    target_reference: str | None = None,
    dump_records_dir: Path | None = None,
) -> tuple[dict[str, Any], list[tuple[DiscoveredStage, dict[str, Any]]]]:
    """Run v0.38 reconstruction on a single-stage or multi-stage acquisition.

    Returns ``(primary_report, stage_reports)``.
    - For single-stage inputs or explicit ``stage_selector``: ``primary_report``
      is the standard single-stage report, and ``stage_reports`` has 1 item.
    - For multi-stage inputs with ``all_stages=True`` (or default when no
      ``stage_selector`` is supplied): each stage is reconstructed independently
      in chronological order (NEVER merged), and ``primary_report`` is a
      ``multi_stage_bundle`` index summarizing all stages.
    """
    input_path = Path(input_path).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    root_path = input_path
    try:
        if leg.is_zip_path(input_path):
            temp_dir = tempfile.TemporaryDirectory(prefix="comet_reconstruct_")
            root_path = Path(temp_dir.name)
            with zipfile.ZipFile(input_path, "r") as zf:
                zf.extractall(root_path)

        stages = discover_leveldb_stages(root_path)

        if stage_selector:
            chosen = select_stage(stages, stage_selector)
            rep = run_stage_v038(
                original_input=input_path,
                root_path=root_path,
                stage=chosen,
                all_stages=stages,
                explicit_blob_input=blob_input,
                database=database,
                store=store,
                include_computer=include_computer,
                target_reference=target_reference,
                dump_records_dir=dump_records_dir,
            )
            return rep, [(chosen, rep)]

        if len(stages) == 1 and not all_stages:
            chosen = stages[0]
            rep = run_stage_v038(
                original_input=input_path,
                root_path=root_path,
                stage=chosen,
                all_stages=stages,
                explicit_blob_input=blob_input,
                database=database,
                store=store,
                include_computer=include_computer,
                target_reference=target_reference,
                dump_records_dir=dump_records_dir,
            )
            return rep, [(chosen, rep)]

        # Multi-stage archive without explicit --stage (or explicit --all-stages):
        # Process every stage independently — NEVER merge stages!
        stage_reports: list[tuple[DiscoveredStage, dict[str, Any]]] = []
        stage_summaries: list[dict[str, Any]] = []
        for st in stages:
            st_dump = (dump_records_dir / st.stage_id) if dump_records_dir else None
            rep = run_stage_v038(
                original_input=input_path,
                root_path=root_path,
                stage=st,
                all_stages=stages,
                explicit_blob_input=None,
                database=database,
                store=store,
                include_computer=include_computer,
                target_reference=target_reference,
                dump_records_dir=st_dump,
            )
            stage_reports.append((st, rep))
            stage_summaries.append({
                "stage_id": st.stage_id,
                "stage_role": st.stage_role,
                "rel_leveldb_path": st.rel_leveldb_posix,
                "database": (rep.get("source") or {}).get("database"),
                "extraction_summary": rep.get("extraction_summary"),
                "summary": rep.get("summary"),
                "source_file_temporal_context": (rep.get("source") or {}).get("source_file_temporal_context"),
            })

        bundle = {
            "tool": "comet-browser-reconstructor",
            "report_kind": "multi_stage_acquisition_bundle",
            "schema_version": SCHEMA_VERSION_V038,
            "engine_provenance": {
                "engine_revision": ENGINE_REVISION_V038,
                "schema_version": SCHEMA_VERSION_V038,
                "base_legacy_engine_schema": "0.37",
                "base_legacy_engine_sha256": hashlib.sha256(Path(leg.__file__).read_bytes()).hexdigest(),
                "patched_engine_file": "src/comettrace/engine_v038.py",
                "patched_engine_sha256": compute_v038_sha256(),
            },
            "source": {
                "input": str(input_path),
                "total_stages": len(stages),
                "stages_merged": False,
                "note": "Each discovered stage was reconstructed independently; stages are never merged.",
            },
            "stages": stage_summaries,
        }
        return bundle, stage_reports
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def build_parser_v038() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CometTrace v0.38 — Reconstruct Perplexity Comet Browser/Computer behavior from IndexedDB LevelDB artifacts."
    )
    parser.add_argument("--engine", choices=["v038", "v037", "legacy"], default="v038", help="Select v0.38 patched engine (default) or frozen v0.37 legacy engine.")
    parser.add_argument("--input", type=Path, default=None, help="LevelDB folder, profile/scenario folder, or ZIP containing Perplexity IndexedDB.")
    parser.add_argument("--blob-input", type=Path, default=None, help="Optional matching IndexedDB blob folder.")
    parser.add_argument("--before", type=Path, default=None, help="Before snapshot zip/folder for deletion comparison.")
    parser.add_argument("--after", type=Path, default=None, help="After snapshot zip/folder for deletion comparison.")
    parser.add_argument("--output", type=Path, default=Path("reconstruction.json"), help="JSON report output path.")
    parser.add_argument("--html-output", type=Path, default=None, help="Optional HTML report output path.")
    parser.add_argument("--database", default="auto", help="IndexedDB database name, or 'auto' to detect keyval-store / pplx-query-cache-* automatically.")
    parser.add_argument("--store", default=leg.DEFAULT_STORE, help="IndexedDB object store name (default: keyval).")
    parser.add_argument("--stage", default=None, help="Select a stage identifier reported by --list-stages.")
    parser.add_argument("--before-stage", help="Select the stage used on the before side.")
    parser.add_argument("--after-stage", help="Select the stage used on the after side.")
    parser.add_argument("--all-stages", action="store_true", help="Independently reconstruct all stages in a multi-stage ZIP/folder.")
    parser.add_argument("--list-stages", action="store_true", help="List all discovered LevelDB stages in --input and exit.")
    parser.set_defaults(include_computer=True)
    parser.add_argument("--include-computer", dest="include_computer", action="store_true", help="Include detected Computer/ASI tasks (default).")
    parser.add_argument("--browser-only", dest="include_computer", action="store_false",
                        help="Legacy scope: skip Computer/ASI expansion, including ASI browser tools. Search records may remain.")
    parser.add_argument("--dump-records-dir", type=Path, default=None, help="Optional directory to write raw parsed record dumps.")
    parser.add_argument("--target-reference", default=None, help="Optional reference code/keyword for case-study filtering.")
    return parser


def _print_diagnostics(stages):
    for stage, report in stages:
        for item in report.get("diagnostics", []):
            print(f"[{item['code']}] {stage.stage_id}: {item['message']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser_v038()
    args = parser.parse_args(argv)

    if args.engine in {"v037", "legacy"}:
        # Delegate directly to untouched frozen legacy_engine CLI
        forwarded: list[str] = []
        raw_argv = argv if argv is not None else __import__("sys").argv[1:]
        skip_next = False
        for i, tok in enumerate(raw_argv):
            if skip_next:
                skip_next = False
                continue
            if tok == "--engine":
                skip_next = True
                continue
            if tok.startswith("--engine="):
                continue
            forwarded.append(tok)
        import sys
        old_argv = sys.argv
        try:
            sys.argv = [old_argv[0]] + forwarded
            leg.main()
        finally:
            sys.argv = old_argv
        return

    if args.list_stages:
        if not args.input:
            raise SystemExit("--list-stages requires --input.")
        inp = args.input.expanduser().resolve()
        temp_dir = None
        root = inp
        try:
            if leg.is_zip_path(inp):
                temp_dir = tempfile.TemporaryDirectory(prefix="comet_reconstruct_")
                root = Path(temp_dir.name)
                with zipfile.ZipFile(inp, "r") as zf:
                    zf.extractall(root)
            stages = discover_leveldb_stages(root)
            print(json.dumps([s.to_metadata() for s in stages], indent=2, ensure_ascii=False))
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()
        return

    if args.before or args.after:
        if not args.before or not args.after:
            raise SystemExit("Both --before and --after are required for snapshot comparison mode.")
        def snapshot(path, selector, side):
            if not selector and path.is_dir() and len(discover_leveldb_stages(path)) > 1:
                parser.error(f"Multiple stages in {side} input. Select --{side}-stage.")
            report, stages = run_input_v038(input_path=path, database=args.database, store=args.store,
                                       include_computer=args.include_computer, stage_selector=selector,
                                       target_reference=args.target_reference)
            _print_diagnostics(stages)
            if "threads" not in report:
                parser.error(f"Multiple stages in {side} input. Select --{side}-stage.")
            return report
        before = snapshot(args.before, args.before_stage or args.stage, "before")
        after = snapshot(args.after, args.after_stage or args.stage, "after")
        with _v038_runtime_patches():
            comparison = leg.compare_reconstructions(before, after)
        comp = {"tool":"comet-browser-reconstructor", "schema_version":SCHEMA_VERSION_V038,
                "mode":"before_after_comparison", "before_reconstruction":before,
                "after_reconstruction":after, "snapshot_comparison":comparison}
        leg.write_json(args.output, comp)
        if args.html_output:
            leg.render_html_report(comp, args.html_output)
        print(f"Wrote comparison report to {args.output}")
        return

    if not args.input:
        raise SystemExit("Provide --input <path> (or --before and --after).")

    primary, stage_reports = run_input_v038(
        input_path=args.input,
        blob_input=args.blob_input,
        database=args.database,
        store=args.store,
        include_computer=args.include_computer,
        stage_selector=args.stage,
        all_stages=args.all_stages,
        target_reference=args.target_reference,
        dump_records_dir=args.dump_records_dir,
    )

    _print_diagnostics(stage_reports)
    if primary.get("report_kind") == "multi_stage_acquisition_bundle":
        out_path = args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        for idx, (st, rep) in enumerate(stage_reports):
            safe_id = re.sub(r"[^A-Za-z0-9._\-]+", "_", st.stage_id)
            st_json = out_path.with_name(f"{out_path.stem}__{safe_id}{out_path.suffix}")
            leg.write_json(st_json, rep)
            primary["stages"][idx]["output_json"] = str(st_json)
            if args.html_output:
                st_html = args.html_output.with_name(f"{args.html_output.stem}__{safe_id}{args.html_output.suffix}")
                leg.render_html_report(rep, st_html)
                primary["stages"][idx]["output_html"] = str(st_html)
        leg.write_json(out_path, primary)
        print(f"Wrote multi-stage bundle ({len(stage_reports)} independent stages) to {out_path}")
    else:
        leg.write_json(args.output, primary)
        if args.html_output:
            leg.render_html_report(primary, args.html_output)
        print(f"Wrote v0.38 report to {args.output}")
