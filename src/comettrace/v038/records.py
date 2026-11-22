"""Group physical records and isolate sidebar entries by their owning turn."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from .. import legacy_engine as leg
from .modes import classify_group_v038

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
    for m in re.finditer(
        r"/computer/tasks/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", text
    ):
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
                    scoped = (
                        record
                        if len(by_turn) == 1
                        else replace(record, value=[entries.get(i) for i in range(len(record.value))])
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

    def add_entry(
        thread_id: str, entry: dict[str, Any], source_rec: leg.ForensicRecord, source_kind: str
    ) -> None:
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
    ordered_records = sorted(records, key=lambda r: r.ldb_seq_no if isinstance(r.ldb_seq_no, int) else -1)

    for record in ordered_records:
        if record.record_kind == "asi_thread_list" or (
            "/rest/thread/list_ask_threads" in record.key and "asi" in record.text(4000).lower()
        ):
            for entry in leg.iter_asi_entry_dicts_v3(record.value, assume_asi_context=True):
                tid = str(
                    entry.get("uuid")
                    or entry.get("thread_uuid")
                    or entry.get("slug")
                    or entry.get("context_uuid")
                    or ""
                ).strip()
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
                    fa.update(
                        text=preview,
                        available=True,
                        raw_value_path="answer_preview",
                        relative_order=rec.ldb_seq_no,
                        evidence=leg.record_evidence(rec),
                    )
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
    for name, keys in (
        ("prompt", ("query_str", "title", "user_query")),
        ("final_answer", ("answer_preview", "first_answer", "preview")),
    ):
        field = promoted.get(name) or {}
        value = field.get("text")
        if not value:
            continue
        matches = [
            (rec, key)
            for rec in records
            for key in keys
            if isinstance(rec.value.get(key), str) and rec.value[key].strip() == value.strip()
        ]
        if matches:
            rec, key = matches[0] if name == "prompt" else matches[-1]
            field.update(
                text=rec.value[key], evidence=leg.record_evidence(rec), relative_order=rec.ldb_seq_no
            )
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


_ORIG_INFER_RECORD_KIND = leg.infer_record_kind
_ORIG_COLLECT_ASI = leg.collect_asi_thread_list_candidates_v3
_ORIG_BUILD_PROMOTED = leg.build_promoted_computer_thread
_ORIG_V07_RECORDS_FOR_THREAD = leg._v07_records_for_thread
