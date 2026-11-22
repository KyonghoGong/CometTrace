"""Attribute structured action fields and partition confirmed and candidate payloads."""

from __future__ import annotations

import re

from .. import legacy_engine as leg

_ORIG_PARTITION = leg._v28_partition_typed_payloads
_ORIG_BUILD_ACTIONS = leg.build_structured_actions
_ORIG_MAKE_PAYLOAD = leg._v18_make_payload
_ORIG_PROMOTE_INPUT = leg._v27_promote_form_input_payloads


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
    titles = {
        p.get("value")
        for p in typed
        if p.get("field") == "event_title" and p.get("payload_source") == "prompt"
    }
    if not titles:
        return
    # The legacy short-input heuristic labels unbound clock values as event titles.
    # Preserve those events as observed inputs, without guessing start versus end.
    explicit_titles = {
        p.get("value")
        for p in (thread.get("artifact_buckets") or {}).get("form_inputs", [])
        if p.get("field") in {"title", "event_title"} and p.get("payload_source") != "observed_form_input"
    }
    thread["typed_payloads"] = [
        p
        for p in typed
        if not (
            p.get("field") == "event_title"
            and p.get("payload_source") == "observed_form_input"
            and p.get("value") not in titles | explicit_titles
            and re.fullmatch(r"\d{1,2}:\d{2}(?:\s*[AP]M)?", str(p.get("value", "")).strip(), re.IGNORECASE)
        )
    ]


def _requested_recipient_only(payload):
    return (
        payload.get("field") == "recipient"
        and payload.get("payload_source") == "prompt"
        and not leg.EMAIL_RE.fullmatch(str(payload.get("value") or "").strip())
    )


def _build_structured_actions(thread):
    # A prompt-only label is not a typing event. Exclude it before action provenance is flattened.
    payloads = thread.get("typed_payloads") or []
    if any(isinstance(p, dict) and _requested_recipient_only(p) for p in payloads):
        thread = dict(
            thread,
            typed_payloads=[
                p for p in payloads if not (isinstance(p, dict) and _requested_recipient_only(p))
            ],
        )
    return _ORIG_BUILD_ACTIONS(thread)


def _partition(thread):
    if (thread.get("task_outcome") or {}).get("task_type") == "email_draft_send":
        kept = []
        for p in thread.get("typed_payloads") or []:
            if _requested_recipient_only(p):
                thread.setdefault("candidate_payloads", []).append(
                    dict(
                        p,
                        payload_role="requested_payload",
                        claim_strength_v28="candidate",
                        claim_reason_v28="The prompt names a recipient without an observed address.",
                    )
                )
            else:
                kept.append(p)
        thread["typed_payloads"] = kept
    _ORIG_PARTITION(thread)
    if (thread.get("task_outcome") or {}).get("task_type") != "email_draft_send":
        return
    for name in ("candidate_payloads", "residual_payloads"):
        unique = {}
        for payload in thread.get(name, []):
            unique.setdefault(leg._v27_payload_key(payload), payload)
        thread[name] = list(unique.values())
    thread["confirmed_payloads"] = list(thread.get("typed_payloads") or [])
    audit = thread.setdefault("payload_audit", {})
    audit["confirmed_payload_count"] = len(thread["confirmed_payloads"])
    audit["candidate_payload_count"] = len(thread["candidate_payloads"])
    leg._v24_sync_thread_views(thread)
