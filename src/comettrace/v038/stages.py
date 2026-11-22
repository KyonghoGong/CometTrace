"""Locate acquisition stages and preserve file metadata within each stage."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import legacy_engine as leg


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
        raise ValueError(
            f"Ambiguous --stage {selector!r}; matched {[s.stage_id for s in sub]}. Available: {available}"
        )
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
                        in_target = entry_name.startswith(prefix) and ("/" not in entry_name[len(prefix) :])
                    elif single_stage_archive:
                        in_target = (leg.TARGET_LEVELDB_NAME + "/") in entry_name
                    else:
                        in_target = False
                    if not in_target:
                        continue
                    raw_time = leg._v34_zipinfo_local_iso(info)
                    add_file(
                        {
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
                        }
                    )
        else:
            for file_path in leveldb_path.iterdir():
                if not file_path.is_file():
                    continue
                base = file_path.name
                if not leg._v32_is_real_source_file(base):
                    continue
                st = file_path.stat()
                dt_utc = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
                add_file(
                    {
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
                    }
                )
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
        "collection_method": "zip_entry_metadata"
        if leg.is_zip_path(original_input)
        else "filesystem_stat_mtime",
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
