"""Recover prompts, metadata and completed answers with their physical evidence."""

from __future__ import annotations

from .. import legacy_engine as leg

_ORIG_METADATA = leg.extract_metadata
_ORIG_PROMPT = leg.extract_prompt
_ORIG_CLEAN_THREAD = leg._v27_clean_thread_text_and_urls


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
    """Yield each task-owned step with its path and inherited completion state."""
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
            if isinstance(child, dict) and all(
                child.get(k) in (None, owner) for k in ("backend_uuid", "thread_uuid")
            ):
                yield from _workflow_steps(
                    child,
                    f"{location}.items[{j}].payload.workflow_payload",
                    owner,
                    True,
                    done and child.get("status") == "WORKFLOW_COMPLETED",
                )


def _extract_prompt(records):
    for rec, entry, path in sorted(_core_entries(records), key=lambda row: row[0].ldb_seq_no or -1):
        value = entry.get("query_str")
        if isinstance(value, str) and value.strip() and entry.get("backend_uuid"):
            return {
                "text": value,
                "field": "all_results.query_str",
                "raw_value_path": path + ".query_str",
                "reference_codes": leg.extract_reference_codes(value),
                "evidence": leg.record_evidence(rec),
            }
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
    """Select a completed outer answer, or a unique completed child answer for this turn."""
    candidates = []
    nested_candidates = []
    unknown_variants = set()
    for rec, entry, path in _core_entries(records):
        for b, block in _dict_items(entry.get("blocks")):
            base = f"{path}.blocks[{b}]"
            markdown = block.get("markdown_block")
            md = markdown.get("answer") if isinstance(markdown, dict) else None
            completed = (
                entry.get("status") == "COMPLETED"
                or entry.get("final") is True
                or entry.get("text_completed") is True
            )
            if (
                isinstance(md, str)
                and md.strip()
                and completed
                and entry.get("final") is not False
                and entry.get("text_completed") is not False
                and not markdown.get("is_streaming")
                and str(block.get("intended_usage", "")).startswith("ask_text")
            ):
                candidates.append((rec, md, base + ".markdown_block.answer"))
            workflow = block.get("workflow_block") or {}
            if not isinstance(workflow, dict):
                continue
            for payload, location, nested, complete in _workflow_text_items(
                workflow, base + ".workflow_block", entry.get("backend_uuid") or entry.get("thread_uuid")
            ):
                variant = payload.get("variant")
                if isinstance(variant, str) and variant not in ("answer", "thought"):
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
        rec, value, path = max(candidates, key=lambda x: (x[0].ldb_seq_no or -1, len(x[1])))
        return {
            "text": value,
            "available": True,
            "source_kind": rec.record_kind,
            "raw_value_path": path,
            "relative_order": rec.ldb_seq_no,
            "evidence": leg.record_evidence(rec),
        }
    return {
        "text": None,
        "available": False,
        "relative_order": None,
        "evidence": [],
        "unsupported_variants": sorted(unknown_variants),
        "reason": "No unambiguous completed answer was identified in a supported field of this entry.",
    }


def _clean_thread(thread):
    # The legacy presentation routine also cleans URLs. Preserve verified answer text.
    exact = thread.get("final_answer") or {}
    _ORIG_CLEAN_THREAD(thread)
    if exact.get("raw_value_path"):
        thread["final_answer"] = exact
