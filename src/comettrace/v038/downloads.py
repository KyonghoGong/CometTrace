"""Interpret thread-local download claims, filenames and completion evidence."""

from __future__ import annotations

import re
from typing import Any

from .. import legacy_engine as leg


PDF_FILENAME_RE_V38 = re.compile(r"(?<![\w.-])[\w-][\w.-]*?(?:\.pdf)+(?![\w-]|\.\w)", re.IGNORECASE)


_PDF_END = r"(?:\.pdf)+(?![\w-]|\.\w)"


_PDF_LABEL = (
    r"\b(?:final\s+downloaded\s+filename|downloaded\s+filename|final\s+filename|file\s*name|"
    r"downloaded\s+files?(?:\s*\(\s*(?:exactly\s+)?(?:one|[1-9]\d*)\s*\))?)"
    r"\s*(?:is\s+|[:：]\s*|\s+-\s+)"
)


_PDF_LABELED = re.compile(
    _PDF_LABEL + r'[*\x60"\x27]*([^<>:"/\\|?*\x60\r\n]+?' + _PDF_END + r")", re.IGNORECASE
)


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


def _join_wrapped_pdf_extension(text):
    return re.sub(
        r"[ \t]*\r?\n[ \t]*(?=\.[ \t]*pdf\b)|(?<=\.)[ \t]*\r?\n[ \t]*(?=pdf\b)", "", text, flags=re.I
    )


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
    if _has_any_regex(
        text, _DOWNLOAD_FAILURE_PATTERNS_V38 + [r"\b(?:saving|download|save) (?:was |is )?blocked\b"]
    ):
        return "download_failed"
    if _has_any_regex(text, _DOWNLOAD_INTERRUPTED_PATTERNS_V38):
        return "download_interrupted"
    if _has_any_regex(
        text,
        [
            r"\b(?:waiting|awaiting) (?:for )?(?:your )?(?:approval|confirmation)\b",
            r"\b(?:need to confirm|please confirm|confirmation required|may I proceed)\b",
        ],
    ):
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
        [
            "wait_for_download",
            "browser_downloads",
            "다운로드 확인",
            "download",
            "ctrl+j",
            ".pdf",
            "save the pdf",
        ],
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
        result.update(
            status=negative,
            side_effect_completed=False,
            confidence="high",
            reported_downloaded_filename=None,
            downloaded_filename_candidates=[],
        )
    elif _has_positive_download_completion_v038(final):
        result.update(
            status="completed_download",
            side_effect_completed=True,
            confidence="high",
            downloaded_filename_candidates=filenames,
        )
    elif result.get("side_effect_completed") or result.get("status") == "completed_download":
        result.update(
            status="source_discovery_or_partial_download",
            side_effect_completed=False,
            confidence="medium",
            reported_downloaded_filename=None,
            downloaded_filename_candidates=[],
        )
    result["classifier_version"] = "v0.38_guarded_download_classifier"
    return result


_ORIG_V21_SIGNALS = leg._v21_current_thread_signals
_ORIG_V23_UPDATE_DOWNLOAD = leg._v23_update_download_outcome
_ORIG_CLASSIFY_TASK_OUTCOME = leg.classify_task_outcome
