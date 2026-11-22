"""Read Chromium download metadata from the selected stage in read-only mode."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import legacy_engine as leg


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
        conn = sqlite3.connect(history_path.resolve().as_uri() + "?mode=ro", uri=True)
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
            rows_out.append(
                {
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
                }
            )
    except (sqlite3.Error, OSError, TypeError, ValueError, OverflowError) as exc:
        message = f"{history_path.name}: {type(exc).__name__}: {exc}"
        if diagnostics is not None:
            diagnostics.append({"code": "history_read_failed", "message": message})
        else:
            print(f"[history_read_failed] {message}", file=sys.stderr)
        return []
    return rows_out


def corroborate_downloads(
    report: dict[str, Any], history_path: Path | None, *, read_chromium_history_downloads
) -> None:
    diagnostics = report.setdefault("diagnostics", [])
    downloads = read_chromium_history_downloads(history_path, diagnostics)
    report.setdefault("source", {})["history_path"] = str(history_path) if history_path else None
    report["source"]["history_downloads_count"] = len(downloads)
    report["source"]["history_read_status"] = (
        "read_failed"
        if any(d["code"] == "history_read_failed" for d in diagnostics)
        else "not_present"
        if history_path is None or not history_path.is_file()
        else "rows_read"
        if downloads
        else "read_empty"
    )
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

        matched = [
            row
            for row in matched
            if row.get("start_time_utc") and utc(start) <= utc(row["start_time_utc"]) <= utc(end)
        ]
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
            m
            for m in (outcome.get("missing_corroboration") or [])
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
