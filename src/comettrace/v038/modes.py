"""Classify backend and tool evidence independently of the UI selection."""

from __future__ import annotations

import re

from .. import legacy_engine as leg
from .workflow import (
    _core_entries,
    _dict_items,
    _workflow_steps,
)


def classify_group_v038(records, metadata=None, browser_only=True):
    """Classify the retained backend and actions without inferring the selected UI mode."""
    entries = list(_core_entries(records))
    if not entries:
        entries = [
            (r, r.value, "$")
            for r in records
            if r.record_kind in {"thread_entry_cache", "thread_list_entry_scoped"}
            and isinstance(r.value, dict)
        ]
    entries.sort(key=lambda item: item[0].ldb_seq_no or 0, reverse=True)
    backend = {}
    tools = {}
    skill = False
    legacy_event = False
    list_task = False
    for rec, entry, path in entries:
        if not backend and any(
            isinstance(entry.get(k), str) and entry[k] for k in ("mode", "search_mode", "display_model")
        ):
            backend = {k: entry.get(k) for k in ("mode", "search_mode", "display_model")}
            backend.update(sequence=rec.ldb_seq_no, entry_path=path, evidence=leg.record_evidence(rec))
        prompt = entry.get("query_str")
        if isinstance(prompt, str) and re.match(
            r"^\s*\[[^\]\n]+\]\(https://www\.perplexity\.ai/computer/skills\?skillId=control-local-browser\)",
            prompt,
        ):
            skill = True
        if rec.record_kind == "thread_list_entry_scoped":
            list_task |= str(entry.get("link") or "").startswith("/computer/tasks/")
        for i, block in _dict_items(entry.get("blocks")):
            legacy_event |= str(block.get("intended_usage") or "").upper() in {
                "COMET_AGENT_TOOL_INPUT",
                "COMET_AGENT_TOOL_OUTPUT",
            }
            for step, loc, _, _ in _workflow_steps(
                block.get("workflow_block"), f"{path}.blocks[{i}].workflow_block", entry.get("backend_uuid")
            ):
                name = step.get("tool_name")
                if isinstance(name, str) and name and name not in tools:
                    tools[name] = {
                        "sequence": rec.ldb_seq_no,
                        "path": loc + ".tool_name",
                        "evidence": leg.record_evidence(rec),
                    }

    mode = str(backend.get("mode") or "").upper()
    search = str(backend.get("search_mode") or "").upper()
    model = str(backend.get("display_model") or "").lower()
    browser_tools = sorted(name for name in tools if name.startswith("browser_"))
    labels = [f"entry.{k}={backend[k]}" for k in ("mode", "search_mode", "display_model") if backend.get(k)]
    if skill:
        labels.append("query_prefix:control-local-browser")
    if legacy_event:
        labels.append("legacy_browser_tool_event")
    labels.extend("tool:" + name for name in browser_tools)
    if search == "BROWSER_AGENT" or (
        not search and (legacy_event or model.startswith("comet_browser_agent"))
    ):
        execution, interaction, confidence = "browser_control", "agentic", "high"
    elif search in ("SEARCH", "STUDY") and mode in ("", "COPILOT"):
        execution, interaction, confidence = "non_browser_agent", "conversational_or_search", "high"
    elif search in ("ASI", "WIDE_RESEARCH") or (
        not search and (mode == "ASI" or model.startswith("pplx_asi") or list_task)
    ):
        execution, interaction, confidence = "computer_mode", "agentic", "medium"
    else:
        execution, interaction, confidence = "unknown", "agentic" if tools else "unknown", "low"
    status = "partial" if execution == "unknown" else "reconstructed"
    if execution == "computer_mode":
        status = "detected_but_not_expanded" if browser_only else "partial_reconstruction"
    return {
        "execution_mode": execution,
        "interaction_type": interaction,
        "confidence": confidence,
        "reconstruction_status": status,
        "classification_evidence": labels,
        "observed_backend": backend,
        "observed_tool_events": tools,
        "observed_browser_tools": browser_tools,
        "browser_control_skill_in_query_prefix": skill,
        "legacy_browser_tool_event": legacy_event,
        "ui_selection": {"value": None, "status": "not_established"},
    }
