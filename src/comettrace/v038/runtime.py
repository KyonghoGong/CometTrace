"""Bind evidence handlers while the frozen reconstruction pipeline is active."""

from __future__ import annotations

import contextlib
from typing import Iterator

from .. import legacy_engine as leg
from .downloads import (
    PDF_FILENAME_RE_V38,
    _v21_current_thread_signals_v038,
    _v23_reported_downloaded_filename_v038,
    _v23_update_download_outcome_v038,
    _v38_cleanup_pdf_filename,
    classify_task_outcome_v038,
    extract_pdf_filenames_v038,
)
from .modes import classify_group_v038
from .payloads import (
    _build_structured_actions,
    _make_payload,
    _partition,
    _promote_input,
)
from .records import (
    _v07_records_for_thread_v038,
    build_promoted_computer_thread_v038,
    collect_asi_thread_list_candidates_v038,
    group_records_v038,
    infer_record_kind_v038,
)
from .workflow import (
    _clean_thread,
    _exact_final,
    _extract_metadata,
    _extract_prompt,
)

_ORIG_IS_PROMPT = leg.looks_like_user_prompt_text
_ORIG_IDENTITY = leg.thread_identity_key


def _is_prompt(text, field=None):
    if field == "all_results.query_str":
        return bool(text)
    return _ORIG_IS_PROMPT(text, field)


def _thread_identity(thread):
    if thread.get("thread_id"):
        return "thread:" + str(thread["thread_id"])
    return _ORIG_IDENTITY(thread)


def _bindings() -> dict:
    """Return each legacy lookup and its evidence-aware replacement."""
    return {
        "extract_prompt": _extract_prompt,
        "extract_metadata": _extract_metadata,
        "classify_group": classify_group_v038,
        "looks_like_user_prompt_text": _is_prompt,
        "extract_final_answer": _exact_final,
        "_v27_clean_thread_text_and_urls": _clean_thread,
        "_v18_make_payload": _make_payload,
        "build_structured_actions": _build_structured_actions,
        "_v27_promote_form_input_payloads": _promote_input,
        "_v28_partition_typed_payloads": _partition,
        "thread_identity_key": _thread_identity,
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


@contextlib.contextmanager
def _v038_runtime_patches() -> Iterator[None]:
    """Restore the active bindings on every exit, including nested calls."""
    overrides = _bindings()
    saved = {k: getattr(leg, k) for k in overrides}
    try:
        for k, fn in overrides.items():
            setattr(leg, k, fn)
        yield
    finally:
        for k, fn in saved.items():
            setattr(leg, k, fn)
