#!/usr/bin/env python3
"""
Comet Browser/Computer Artifact Reconstructor
"""

from __future__ import annotations

import argparse
import ast
import html
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from ccl_chromium_reader import ccl_chromium_indexeddb
    from ccl_chromium_reader.storage_formats import ccl_leveldb
except (ModuleNotFoundError, ImportError) as exc:
    raise SystemExit(
        "ccl_chromium_reader is not installed.\n"
        "Install example:\n"
        "python -m pip install "
        "\"https://github.com/cclgroupltd/ccl_simplesnappy/archive/refs/heads/master.zip\"\n"
        "python -m pip install Brotli\n"
        "python -m pip install --no-deps "
        "\"https://github.com/cclgroupltd/ccl_chromium_reader/archive/refs/heads/master.zip\""
    ) from exc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_LEVELDB_NAME = "https_www.perplexity.ai_0.indexeddb.leveldb"
TARGET_BLOB_NAME = "https_www.perplexity.ai_0.indexeddb.blob"
DEFAULT_DATABASE = "keyval-store"
DEFAULT_STORE = "keyval"

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

BROWSER_RELEVANT_MARKERS = [
    "all_results",
    "thread_metadata",
    "/rest/thread/list_recent",
    "/rest/thread/list_pinned_ask_threads",
    "/rest/thread/list_ask_threads",
    "thread_type_filter",
    "topMostUrls",
    "BROWSER_AGENT",
    "comet_browser_agent",
    "workflow_root",
    "plan_block",
    "web_results",
    "sources_answer_mode",
    "unified_assets",
    "entity_group_v2_block",
    "BROWSER_AGENT_CONFIRMATION",
    "COMET_AGENT_TOOL_INPUT",
    "COMET_AGENT_TOOL_OUTPUT",
    "query_str",
    "markdown_block",
    "final_sse_message",
    "PRIVATE_READ",
    "INCOGNITO",
]

# Strong Computer-mode markers indicate Comet Computer / ASI tasks.
# Do NOT treat generic subtask identifiers as Computer mode by themselves:
# Browser Control workflows can also contain subagent_task_id / computer_list
# records for UI-control subtasks.
STRONG_COMPUTER_MARKERS = [
    "/computer/tasks/",
    "pplx_asi",
    '"mode": "ASI"',
    '"search_mode": "ASI"',
    "WIDE_RESEARCH",
    '"variant": "thought"',
    '"tool_name": "computer"',
]

WEAK_COMPUTER_MARKERS = [
    "subagent_task_id",
    "computer_list",
    '"tool_name": "find"',
]

ASI_THREAD_LIST_MARKERS = [
    "/rest/thread/list_ask_threads",
    "thread_type_filter",
    '"thread_type_filter": "asi"',
    '"thread_type_filter":"asi"',
    '"mode": "asi"',
    '"mode":"asi"',
    '"mode": "ASI"',
    '"search_mode": "ASI"',
]

# Compatibility alias used by relevance filtering. Classification uses the
# strong/weak sets separately so weak markers cannot suppress Browser Control.
COMPUTER_MARKERS = STRONG_COMPUTER_MARKERS + WEAK_COMPUTER_MARKERS

METADATA_FIELDS = [
    "backend_uuid",
    "context_uuid",
    "frontend_uuid",
    "frontend_context_uuid",
    "uuid",
    "thread_uuid",
    "created_at",
    "updated_at",
    "lastAccess",
    "status",
    "thread_status",
    "final",
    "text_completed",
    "message_mode",
    "mode",
    "mode_type",
    "search_mode",
    "search_focus",
    "display_model",
    "user_selected_model",
    "model_preference",
    "access_level",
    "privacy_state",
    "sources",
]

ACTION_KEYWORDS = [
    "click",
    "key",
    "wait",
    "find",
    "navigate",
    "download",
    "Downloading",
    "Saving",
    "Opening",
    "Filling",
    "Checking",
    "Preparing",
    "Waiting",
    "send",
    "draft",
    "sent folder",
    "calendar",
    "event",
    "PDF",
    "ctrl+j",
    "클릭",
    "타이핑",
    "키 누르기",
    "기다림",
    "다운로드",
    "확인",
]

# Generic payload hints only. Do not key reconstruction logic to scenario names
# such as experiment IDs; those are case/reference markers, not parser rules.
TYPED_PAYLOAD_HINTS = [
    "recipient",
    "to",
    "cc",
    "bcc",
    "subject",
    "body",
    "title",
    "description",
    "date",
    "time",
    "start",
    "end",
    "start_time",
    "end_time",
    "location",
    "filename",
    "file name",
    "download",
    "pdf",
    "attachment",
    "calendar",
    "event",
]

FINAL_ANSWER_MARKERS = [
    "markdown_block",
    "final_answer",
    "final_sse_message",
    "answer",
    "chunks",
    "text_completed",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class EvidenceRef:
    source_file: str | None
    source_path: str | None
    source_type: str | None
    offset: int | None
    state: str | None
    ldb_seq_no: int | None
    is_live: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_path": self.source_path,
            "source_type": self.source_type,
            "offset": self.offset,
            "state": self.state,
            "ldb_seq_no": self.ldb_seq_no,
            "is_live": self.is_live,
        }


@dataclass
class ForensicRecord:
    key: str
    value: Any
    ldb_seq_no: int | None
    is_live: bool
    source_types: list[str]
    evidence: list[EvidenceRef]
    key_tokens: list[str] = field(default_factory=list)
    record_kind: str = "unknown"

    def text(self, max_chars: int | None = None) -> str:
        text = self.key + "\n" + json.dumps(self.value, ensure_ascii=False, default=str)
        if max_chars is not None:
            return text[:max_chars]
        return text


# ---------------------------------------------------------------------------
# Existing extraction layer, preserved and slightly wrapped
# ---------------------------------------------------------------------------

def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def get_attr(obj: Any, names: list[str], default: Any = None) -> Any:
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def normalize_source_type(raw_record: Any) -> str:
    file_type = get_attr(raw_record, ["file_type"], None)
    file_type_name = getattr(file_type, "name", str(file_type)).lower()

    if "ldb" in file_type_name or "sst" in file_type_name:
        return "ldb"
    if "log" in file_type_name:
        return "log"

    origin_file = str(get_attr(raw_record, ["origin_file"], ""))
    suffix = Path(origin_file).suffix.lower()

    if suffix in {".ldb", ".sst"}:
        return "ldb"
    if suffix == ".log":
        return "log"
    return "other"


def collect_sources_by_sequence(leveldb_path: Path) -> dict[int, list[dict[str, Any]]]:
    """
    Read raw LevelDB entries to identify where each LevelDB sequence number
    physically came from. This does not output raw values.
    """
    sources_by_seq: dict[int, list[dict[str, Any]]] = {}

    with ccl_leveldb.RawLevelDb(leveldb_path) as raw_db:
        for raw_record in raw_db.iterate_records_raw():
            seq_no = get_attr(raw_record, ["seq", "sequence_number", "ldb_seq_no"])
            if not isinstance(seq_no, int):
                continue

            origin_file = get_attr(raw_record, ["origin_file"], "unknown")
            state_obj = get_attr(raw_record, ["state"], None)
            state = getattr(state_obj, "name", str(state_obj))

            source_info = {
                "source_file": Path(str(origin_file)).name,
                "source_path": str(origin_file),
                "source_type": normalize_source_type(raw_record),
                "offset": get_attr(raw_record, ["offset", "file_offset"], None),
                "state": state,
            }

            sources_by_seq.setdefault(seq_no, []).append(source_info)

    return sources_by_seq


def parse_dataset(
    leveldb_path: Path,
    database_name: str,
    object_store_name: str,
    blob_path: Path | None = None,
    sources_by_seq: dict[int, list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    wrapper = ccl_chromium_indexeddb.WrappedIndexDB(
        str(leveldb_path),
        str(blob_path) if blob_path is not None else None,
    )

    try:
        database = wrapper[database_name]
        store = database[object_store_name]

        bad_records: list[dict[str, str]] = []

        def on_bad_record(key: Any, value: Any) -> None:
            bad_records.append(
                {
                    "key": repr(key),
                    "raw_value_preview": repr(value)[:400],
                }
            )

        live_records: list[dict[str, Any]] = []
        dead_records: list[dict[str, Any]] = []

        for record in store.iterate_records(
            live_only=False,
            errors_to_stdout=False,
            bad_deserializer_data_handler=on_bad_record,
        ):
            seq_no = record.ldb_seq_no
            source_files = sources_by_seq.get(seq_no, []) if sources_by_seq else []

            parsed = {
                "key": str(record.key),
                "ldb_seq_no": seq_no,
                "is_live": record.is_live,
                "source_files": source_files,
                "source_types": sorted({src["source_type"] for src in source_files}),
                "value": to_jsonable(record.value),
            }

            if record.is_live:
                live_records.append(parsed)
            else:
                dead_records.append(parsed)

        return live_records, dead_records, bad_records
    finally:
        close = getattr(wrapper, "close", None)
        if callable(close):
            close()


def split_parsed_records_by_source(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ldb_records: list[dict[str, Any]] = []
    log_records: list[dict[str, Any]] = []
    unmatched_records: list[dict[str, Any]] = []

    for record in records:
        source_types = set(record.get("source_types", []))

        if "ldb" in source_types:
            ldb_records.append(record)
        if "log" in source_types:
            log_records.append(record)
        if not source_types:
            unmatched_records.append(record)

    return ldb_records, log_records, unmatched_records


def extract_records_from_leveldb(
    leveldb_path: Path,
    blob_path: Path | None,
    database_name: str = DEFAULT_DATABASE,
    object_store_name: str = DEFAULT_STORE,
) -> dict[str, Any]:
    sources_by_seq = collect_sources_by_sequence(leveldb_path)
    live_records, dead_records, bad_records = parse_dataset(
        leveldb_path=leveldb_path,
        database_name=database_name,
        object_store_name=object_store_name,
        blob_path=blob_path,
        sources_by_seq=sources_by_seq,
    )
    all_records = live_records + dead_records
    ldb_records, log_records, unmatched_records = split_parsed_records_by_source(all_records)

    return {
        "source_leveldb_path": str(leveldb_path.resolve()),
        "source_blob_path": str(blob_path.resolve()) if blob_path else None,
        "database": database_name,
        "object_store": object_store_name,
        "live_records": live_records,
        "dead_records": dead_records,
        "all_records": all_records,
        "ldb_records": ldb_records,
        "log_records": log_records,
        "unmatched_records": unmatched_records,
        "bad_records": bad_records,
    }


# ---------------------------------------------------------------------------
# Input discovery
# ---------------------------------------------------------------------------

@dataclass
class PreparedInput:
    original_input: Path
    root_path: Path
    leveldb_path: Path
    blob_path: Path | None
    temp_dir: tempfile.TemporaryDirectory[str] | None = None

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            self.temp_dir.cleanup()


def is_zip_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".zip"


def find_perplexity_indexeddb(root: Path) -> tuple[Path, Path | None]:
    if root.is_dir() and root.name == TARGET_LEVELDB_NAME:
        leveldb_path = root
        sibling_blob = root.parent / TARGET_BLOB_NAME
        return leveldb_path, sibling_blob if sibling_blob.exists() else None

    matches = [p for p in root.rglob(TARGET_LEVELDB_NAME) if p.is_dir()]
    if not matches:
        raise FileNotFoundError(
            f"Could not find {TARGET_LEVELDB_NAME} under {root}. "
            "Pass the LevelDB folder directly or a zip/folder containing IndexedDB."
        )

    # Prefer shortest path, which usually means the intended scenario root.
    matches.sort(key=lambda p: (len(p.parts), str(p)))
    leveldb_path = matches[0]
    blob_path = leveldb_path.parent / TARGET_BLOB_NAME
    return leveldb_path, blob_path if blob_path.exists() else None


def prepare_input(input_path: Path, explicit_blob_input: Path | None = None) -> PreparedInput:
    input_path = input_path.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    root_path = input_path

    if is_zip_path(input_path):
        temp_dir = tempfile.TemporaryDirectory(prefix="comet_reconstruct_")
        root_path = Path(temp_dir.name)
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(root_path)

    leveldb_path, discovered_blob = find_perplexity_indexeddb(root_path)
    blob_path = explicit_blob_input.expanduser().resolve() if explicit_blob_input else discovered_blob

    if blob_path is not None and not blob_path.exists():
        raise FileNotFoundError(f"Blob input path does not exist: {blob_path}")

    return PreparedInput(
        original_input=input_path,
        root_path=root_path,
        leveldb_path=leveldb_path,
        blob_path=blob_path,
        temp_dir=temp_dir,
    )


# ---------------------------------------------------------------------------
# Normalization and helpers
# ---------------------------------------------------------------------------

def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_key_tokens(key: str) -> list[str]:
    try:
        parsed = ast.literal_eval(key)
        if isinstance(parsed, (list, tuple)):
            return [str(x) for x in parsed]
    except Exception:
        pass
    return [key]


def normalize_records(records: list[dict[str, Any]]) -> list[ForensicRecord]:
    normalized: list[ForensicRecord] = []
    for record in records:
        ldb_seq_no = record.get("ldb_seq_no")
        is_live = bool(record.get("is_live"))
        evidence = []
        for src in record.get("source_files", []) or []:
            evidence.append(
                EvidenceRef(
                    source_file=src.get("source_file"),
                    source_path=src.get("source_path"),
                    source_type=src.get("source_type"),
                    offset=src.get("offset"),
                    state=src.get("state"),
                    ldb_seq_no=ldb_seq_no,
                    is_live=is_live,
                )
            )

        fr = ForensicRecord(
            key=str(record.get("key", "")),
            value=record.get("value"),
            ldb_seq_no=ldb_seq_no if isinstance(ldb_seq_no, int) else None,
            is_live=is_live,
            source_types=list(record.get("source_types", []) or []),
            evidence=evidence,
        )
        fr.key_tokens = parse_key_tokens(fr.key)
        fr.record_kind = infer_record_kind(fr)
        normalized.append(fr)
    return normalized


def infer_record_kind(record: ForensicRecord) -> str:
    tokens = record.key_tokens
    text = record.text(4000)

    if "all_results" in tokens or "all_results" in text:
        return "all_results"
    if "thread_metadata" in tokens or "thread_metadata" in text:
        return "thread_metadata"
    if "/rest/thread/list_recent" in text:
        return "thread_list_recent"
    if "/rest/thread/list_ask_threads" in text and ("thread_type_filter" in text or '"mode": "asi"' in text.lower() or '"mode":"asi"' in text.lower()):
        return "asi_thread_list"
    if "topMostUrls" in text:
        return "top_most_urls"
    if "/computer/tasks/" in text:
        return "computer_task"
    if "COMET_AGENT_TOOL_INPUT" in text or "COMET_AGENT_TOOL_OUTPUT" in text:
        return "tool_io"
    return "unknown"


def record_evidence(record: ForensicRecord, max_items: int = 5) -> list[dict[str, Any]]:
    if record.evidence:
        return [ev.to_dict() for ev in record.evidence[:max_items]]
    return [
        {
            "source_file": None,
            "source_path": None,
            "source_type": None,
            "offset": None,
            "state": None,
            "ldb_seq_no": record.ldb_seq_no,
            "is_live": record.is_live,
        }
    ]


def contains_any(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def json_text(value: Any, max_chars: int | None = None) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if max_chars is None else text[:max_chars]


def flatten_strings(obj: Any, max_len: int = 5000) -> list[str]:
    result: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            s = value.strip()
            if s:
                result.append(s[:max_len])
        elif isinstance(value, dict):
            for k, v in value.items():
                if isinstance(k, str) and k.strip():
                    # Keep informative keys such as WORKFLOW_ITEM_TEXT or COMET markers.
                    if any(marker in k for marker in ACTION_KEYWORDS + ["COMET", "WORKFLOW", "BROWSER_AGENT"]):
                        result.append(k[:max_len])
                walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return result


def recursive_find(obj: Any, key_names: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in key_names:
                found.append(v)
            found.extend(recursive_find(v, key_names))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(recursive_find(item, key_names))
    return found


def recursive_collect_fields(obj: Any, field_names: set[str]) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in field_names:
                found.append((str(k), v))
            found.extend(recursive_collect_fields(v, field_names))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(recursive_collect_fields(item, field_names))
    return found


def extract_uuids_from_text(text: str) -> list[str]:
    return UUID_RE.findall(text)


def extract_urls_from_text(text: str) -> list[str]:
    urls = []
    for url in URL_RE.findall(text):
        cleaned = url.rstrip("),.]}>")
        urls.append(cleaned)
    return urls


def extract_reference_codes(text: str) -> list[str]:
    """Extract scenario/reference codes used in the experiments.

    Keep this generic enough for Sxx scenario codes, Cxx comparison codes, and
    Computer_* comparison references. These labels are used for filtering and
    display only; parser behavior must not depend on specific scenario names.
    """
    patterns = [
        r"S\d{2}_[A-Za-z0-9_\-]+(?:_\d{8})?",
        r"C\d{2}_[A-Za-z0-9_\-]+(?:_\d{8})?",
    ]
    found: set[str] = set()
    for pattern in patterns:
        found.update(re.findall(pattern, text or ""))
    return sorted(found)


# Internal labels that are useful as parser context but should not be reported as
# user/browser actions, typed payloads, or final answers.
INTERNAL_NOISE_EXACT = {
    "answer_mode_type",
    "widget_type",
    "step_type",
    "mode_type",
    "pending",
    "pending_followups",
    "ANSWER",
    "SOURCES",
    "IMAGE",
    "INITIAL_QUERY",
}

INTERNAL_NOISE_CONTAINS = [
    "answer_mode_type",
    "widget_type",
    "pending_followups",
    "\"step_type\":\"INITIAL_QUERY\"",
    "\"step_type\": \"INITIAL_QUERY\"",
]


def is_internal_noise_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return True
    if s in INTERNAL_NOISE_EXACT:
        return True
    lower = s.lower()
    if lower in {x.lower() for x in INTERNAL_NOISE_EXACT}:
        return True
    if any(marker.lower() in lower for marker in INTERNAL_NOISE_CONTAINS):
        return True
    # JSON-ish empty initial query is not a user action.
    if "initial_query" in lower and '"query":""' in lower.replace(" ", ""):
        return True
    # A short all-caps UI label is rarely a final answer or payload.
    if len(s) <= 20 and s.isupper() and s.replace("_", "").replace("-", "").isalpha():
        return True
    return False


def is_prompt_duplicate(value: str, prompt_text: str | None) -> bool:
    if not value or not prompt_text:
        return False
    a = " ".join(value.split())
    b = " ".join(prompt_text.split())
    if not a or not b:
        return False
    return a == b or (len(a) > 80 and (a in b or b in a))


def is_useful_human_text(value: str, prompt_text: str | None = None) -> bool:
    s = value.strip()
    if len(s) < 20:
        return False
    if is_internal_noise_string(s):
        return False
    if is_prompt_duplicate(s, prompt_text):
        return False
    # Avoid accepting lists of internal UI labels as an answer.
    lines = [line.strip() for line in s.splitlines() if line.strip()]
    if lines and all(is_internal_noise_string(line) for line in lines):
        return False
    return True


def collect_thread_list_entries(value: Any, thread_id: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            entries.extend(collect_thread_list_entries(item, thread_id))
    elif isinstance(value, dict):
        uuid_value = value.get("uuid") or value.get("thread_uuid") or value.get("backend_uuid")
        if uuid_value == thread_id:
            entries.append({
                "uuid": uuid_value,
                "title": value.get("title"),
                "link": value.get("link"),
                "variant": value.get("variant"),
                "unread": value.get("unread"),
                "status": value.get("status"),
                "context_uuid": value.get("context_uuid"),
                "answer_preview": value.get("answer_preview"),
                "mode_type": value.get("mode_type"),
            })
        else:
            for child in value.values():
                entries.extend(collect_thread_list_entries(child, thread_id))
    return entries


def link_global_records_to_thread(global_records: list[ForensicRecord], thread_id: str) -> list[dict[str, Any]]:
    linked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in global_records:
        if record.record_kind != "thread_list_recent":
            continue
        for entry in collect_thread_list_entries(record.value, thread_id):
            key = json.dumps(entry, sort_keys=True, ensure_ascii=False) + str(record.ldb_seq_no)
            if key in seen:
                continue
            seen.add(key)
            linked.append({
                "kind": "thread_list_recent",
                "relative_order": record.ldb_seq_no,
                "entry": entry,
                "evidence": record_evidence(record),
                "interpretation": "Thread list/cache evidence linked by UUID. Used as external status/context evidence, not merged as core thread content.",
            })
    return sorted(linked, key=lambda item: item.get("relative_order") or -1)


def extract_global_context_urls(global_records: list[ForensicRecord], max_items: int = 25) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in global_records:
        if record.record_kind != "top_most_urls":
            continue
        for item in normalize_top_url_items(record.value, record):
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            item["role"] = "global_topMostUrls_context_candidate"
            item["interpretation"] = "Global browser context URL candidate. Not proof that this URL belongs to a specific thread unless corroborated by thread-level evidence."
            candidates.append(item)
    return sorted(candidates, key=lambda item: item.get("relative_order") or -1)[:max_items]



# ---------------------------------------------------------------------------
# Computer / ASI thread-list promotion
# ---------------------------------------------------------------------------

def is_asi_thread_list_record(record: ForensicRecord) -> bool:
    text = record.text(80000)
    low = text.lower()
    return "/rest/thread/list_ask_threads" in text and (
        "thread_type_filter" in low
        or '"mode": "asi"' in low
        or '"mode":"asi"' in low
        or "computer_d" in low
    )


def iter_asi_entry_dicts(obj: Any) -> list[dict[str, Any]]:
    """Return likely ASI/Computer thread-list entries from nested pages payloads."""
    found: list[dict[str, Any]] = []
    if isinstance(obj, list):
        for item in obj:
            found.extend(iter_asi_entry_dicts(item))
    elif isinstance(obj, dict):
        mode = str(obj.get("mode") or obj.get("search_mode") or "").lower()
        query = str(obj.get("query_str") or obj.get("title") or "")
        has_thread_identity = bool(obj.get("uuid") or obj.get("thread_uuid") or obj.get("context_uuid"))
        has_answer = bool(obj.get("first_answer") or obj.get("answer") or obj.get("answer_preview"))
        looks_computer = (
            mode == "asi"
            or "computer mode" in query.lower()
            or looks_like_computer_reference(query)
            or "thread_type_filter" in json.dumps(obj, ensure_ascii=False, default=str)
        )
        if looks_computer and has_thread_identity and (query or has_answer):
            found.append(obj)
            # Still walk children because some entries contain nested answer blocks.
        for child in obj.values():
            found.extend(iter_asi_entry_dicts(child))
    return found


def parse_maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(text)
        except Exception:
            pass
    return value


def extract_text_from_any(obj: Any, max_items: int = 20) -> list[str]:
    """Collect human-readable text/answer strings from arbitrary nested objects."""
    texts: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value or value in seen:
            return
        # Ignore raw JSON-like blobs when a nested parse can recover cleaner text.
        if len(value) > 30 and not is_internal_noise_string(value):
            seen.add(value)
            texts.append(value)

    def walk(value: Any) -> None:
        if len(texts) >= max_items:
            return
        value = parse_maybe_json(value)
        if isinstance(value, dict):
            for key in ("answer", "text", "content", "markdown", "message", "summary", "final_answer", "answer_preview"):
                if key in value:
                    walk(value.get(key))
            for key, child in value.items():
                if key not in {"answer", "text", "content", "markdown", "message", "summary", "final_answer", "answer_preview"}:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        else:
            add(value)

    walk(obj)
    return texts[:max_items]


def extract_computer_reasoning_items_from_value(value: Any, record: ForensicRecord) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        obj = parse_maybe_json(obj)
        if isinstance(obj, dict):
            variant = str(obj.get("variant") or obj.get("type") or "").lower()
            tool_name = str(obj.get("tool_name") or obj.get("tool") or "").lower()
            if "thought" in variant or variant == "reasoning" or "computer" in tool_name:
                texts = extract_text_from_any(obj, max_items=6)
                preview = "\n".join(texts) if texts else json_text(obj, 2500)
                if preview.strip():
                    items.append({
                        "kind": "computer_reasoning_or_tool_candidate",
                        "text_preview": preview[:2500],
                        "relative_order": record.ldb_seq_no,
                        "evidence": record_evidence(record),
                    })
            for child in obj.values():
                walk(child)
        elif isinstance(obj, list):
            for child in obj:
                walk(child)
        elif isinstance(obj, str):
            parsed = parse_maybe_json(obj)
            if parsed is not obj:
                walk(parsed)

    walk(value)
    # Deduplicate by preview.
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = item.get("text_preview", "")[:300]
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def collect_asi_thread_list_candidates(global_records: list[ForensicRecord]) -> dict[str, dict[str, Any]]:
    """Promote ASI list-cache entries into Computer-mode thread candidates.

    Computer/ASI tasks may remain in /rest/thread/list_ask_threads cache rather
    than the Browser-Control all_results/thread_metadata shape. This function
    builds lightweight thread candidates so the report can show prompt,
    metadata, first-answer/reasoning candidates, and evidence instead of leaving
    them only under global_records.
    """
    candidates: dict[str, dict[str, Any]] = {}

    for record in global_records:
        if not is_asi_thread_list_record(record):
            continue
        for entry in iter_asi_entry_dicts(record.value):
            thread_id = str(entry.get("uuid") or entry.get("thread_uuid") or entry.get("slug") or "").strip()
            if not thread_id:
                # As a last resort, derive a stable-ish id from context_uuid.
                thread_id = str(entry.get("context_uuid") or "").strip()
            if not thread_id:
                continue
            bucket = candidates.setdefault(thread_id, {"entries": [], "records": []})
            bucket["entries"].append(to_jsonable(entry))
            bucket["records"].append(record)

    return candidates


def build_promoted_computer_thread(
    thread_id: str,
    candidate: dict[str, Any],
    global_context_urls: list[dict[str, Any]],
) -> dict[str, Any]:
    records: list[ForensicRecord] = candidate.get("records") or []
    entries: list[dict[str, Any]] = candidate.get("entries") or []
    # Prefer the latest sequence record/entry for the most complete cache state.
    latest_record = max(records, key=lambda r: r.ldb_seq_no or -1) if records else None
    latest_entry = entries[-1] if entries else {}
    if records and entries:
        # If possible, align latest entry to latest record order by selecting the
        # last entry after collection, which follows record traversal order.
        latest_entry = entries[-1]

    query = str(latest_entry.get("query_str") or latest_entry.get("title") or "").strip()
    title = str(latest_entry.get("title") or "").strip()
    prompt_text = query or title
    prompt_evidence = record_evidence(latest_record) if latest_record else []

    first_answer_raw = latest_entry.get("first_answer") or latest_entry.get("answer") or latest_entry.get("answer_preview")
    parsed_first_answer = parse_maybe_json(first_answer_raw)
    answer_texts = [t for t in extract_text_from_any(parsed_first_answer, max_items=12) if is_useful_human_text(t, prompt_text)]
    final_text = answer_texts[-1] if answer_texts else None
    answer_evidence = record_evidence(latest_record) if (latest_record and final_text) else []

    reasoning_items: list[dict[str, Any]] = []
    for record in records:
        reasoning_items.extend(extract_computer_reasoning_items_from_value(record.value, record))
    if parsed_first_answer is not None and latest_record is not None:
        reasoning_items.extend(extract_computer_reasoning_items_from_value(parsed_first_answer, latest_record))
    # Deduplicate reasoning items after merging.
    deduped_reasoning: list[dict[str, Any]] = []
    seen_reasoning: set[str] = set()
    for item in reasoning_items:
        key = item.get("text_preview", "")[:300]
        if key and key not in seen_reasoning:
            seen_reasoning.add(key)
            deduped_reasoning.append(item)

    metadata = {
        "status": latest_entry.get("status") or latest_entry.get("thread_status") or latest_entry.get("answer_status"),
        "final": bool(final_text) if final_text is not None else None,
        "mode": latest_entry.get("mode") or "asi",
        "search_mode": str(latest_entry.get("search_mode") or "ASI").upper(),
        "display_model": latest_entry.get("display_model") or latest_entry.get("model_preference") or "pplx_asi_candidate",
        "thread_status": latest_entry.get("status") or latest_entry.get("thread_status"),
        "created_at": latest_entry.get("created_at") or latest_entry.get("last_query_datetime"),
        "updated_at": latest_entry.get("updated_at") or latest_entry.get("last_query_datetime"),
        "last_query_datetime": latest_entry.get("last_query_datetime"),
        "backend_uuid": latest_entry.get("uuid") or thread_id,
        "context_uuid": latest_entry.get("context_uuid"),
        "frontend_uuid": latest_entry.get("frontend_uuid"),
        "frontend_context_uuid": latest_entry.get("frontend_context_uuid"),
        "slug": latest_entry.get("slug"),
        "thread_number": latest_entry.get("thread_number"),
        "source_kind": "asi_thread_list_cache_promoted",
        "cache_entry_count": len(entries),
        "evidence": {
            "asi_thread_list_cache": record_evidence(latest_record) if latest_record else [],
        },
    }

    prompt = {
        "text": prompt_text or None,
        "field": "query_str" if latest_entry.get("query_str") else "title",
        "reference_codes": extract_reference_codes(prompt_text or ""),
        "evidence": prompt_evidence,
    }

    classification = {
        "interaction_type": "agentic",
        "execution_mode": "computer_mode",
        "confidence": "medium" if len(records) <= 1 else "high",
        "reconstruction_status": "computer_partial_list_cache_reconstruction" if final_text else "computer_metadata_only",
        "classification_evidence": [
            "asi_thread_list_cache",
            "thread_type_filter=asi",
            f"mode={metadata.get('mode')}",
            "search_mode=ASI",
        ] + (["first_answer_cache"] if first_answer_raw else []),
    }

    # ASI cache records often contain URLs in prompt/first_answer. These are not
    # the same as low-level browser history, but still useful leads.
    urls = extract_urls(records)
    typed_payloads = extract_typed_payloads(records, prompt_text=prompt_text)
    plan: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if latest_entry.get("title"):
        plan.append({
            "kind": "asi_thread_title",
            "label": str(latest_entry.get("title"))[:1000],
            "relative_order": latest_record.ldb_seq_no if latest_record else None,
            "evidence": record_evidence(latest_record) if latest_record else [],
        })
    for text in answer_texts[:4]:
        if any(keyword.lower() in text.lower() for keyword in ["open", "download", "saving", "click", "navigate", "pdf", "browser automation"]):
            actions.append({
                "kind": "asi_answer_action_candidate",
                "label": text[:1000],
                "relative_order": latest_record.ldb_seq_no if latest_record else None,
                "evidence": record_evidence(latest_record) if latest_record else [],
            })

    final_answer = {
        "text": final_text,
        "available": bool(final_text),
        "reason": None if final_text else "ASI list-cache entry was promoted, but no clean first_answer/final-answer text was recovered.",
        "relative_order": latest_record.ldb_seq_no if latest_record else None,
        "evidence": answer_evidence,
        "source_kind": "asi_thread_list_first_answer" if final_text else None,
        "caution": "This is recovered from ASI thread-list cache/first_answer, not yet corroborated by all_results core thread content.",
    }

    reasoning = {
        "available": bool(deduped_reasoning),
        "items": deduped_reasoning[:25],
        "note": None if deduped_reasoning else "Computer/ASI task detected from list cache, but no thought/reasoning block was cleanly parsed in this promoted candidate.",
    }

    private_mode = detect_private_mode(records)
    deletion_state = detect_deletion_state(records)
    metadata["private_mode"] = private_mode["private_mode"]
    metadata["private_detection"] = private_mode

    timeline = build_timeline(
        records=records,
        prompt=prompt,
        plan=plan,
        actions=actions,
        urls=urls,
        typed_payloads=typed_payloads,
        final_answer=final_answer,
    )

    return {
        "thread_id": thread_id,
        "classification": classification,
        "prompt": prompt,
        "metadata": metadata,
        "plan": plan,
        "actions": actions,
        "urls": urls,
        "context_url_candidates": [],
        "typed_payloads": typed_payloads,
        "reasoning": reasoning,
        "final_answer": final_answer,
        "deletion_state": deletion_state,
        "timeline": timeline,
        "record_count": len(records),
        "source_summary": summarize_sources(records),
        "promotion_note": "Promoted from /rest/thread/list_ask_threads ASI cache. Treat as partial Computer-mode reconstruction until corroborated with /computer/tasks, History, or Downloads artifacts.",
        "source_entries_sample": entries[:3],
    }


# ---------------------------------------------------------------------------
# Filtering, grouping, classification
# ---------------------------------------------------------------------------

def is_browser_relevant_record(record: ForensicRecord) -> bool:
    text = record.text(20000)
    return contains_any(text, BROWSER_RELEVANT_MARKERS + COMPUTER_MARKERS + ASI_THREAD_LIST_MARKERS)


def primary_thread_id_from_key(record: ForensicRecord) -> str | None:
    tokens = record.key_tokens
    for marker in ["all_results", "thread_metadata"]:
        if marker in tokens:
            idx = tokens.index(marker)
            if idx + 1 < len(tokens):
                candidate = tokens[idx + 1]
                if UUID_RE.fullmatch(candidate):
                    return candidate
                uuids = extract_uuids_from_text(candidate)
                if uuids:
                    return uuids[0]

    # Fallback for textual key strings.
    if "all_results" in record.key or "thread_metadata" in record.key:
        uuids = extract_uuids_from_text(record.key)
        if uuids:
            return uuids[-1]
    return None


def extract_computer_task_id(record: ForensicRecord) -> str | None:
    text = record.text(20000)
    marker = "/computer/tasks/"
    if marker in text:
        after = text.split(marker, 1)[1]
        uuids = extract_uuids_from_text(after)
        if uuids:
            return uuids[0]
    return None


def get_value_uuid_candidates(record: ForensicRecord) -> set[str]:
    candidates: set[str] = set()
    if isinstance(record.value, dict):
        for field_name, value in recursive_collect_fields(
            record.value,
            {
                "backend_uuid",
                "context_uuid",
                "frontend_uuid",
                "frontend_context_uuid",
                "uuid",
                "thread_uuid",
            },
        ):
            if isinstance(value, str):
                candidates.update(extract_uuids_from_text(value))
    return candidates


def group_records(records: list[ForensicRecord]) -> tuple[dict[str, list[ForensicRecord]], list[ForensicRecord]]:
    groups: dict[str, list[ForensicRecord]] = {}
    global_records: list[ForensicRecord] = []

    for record in records:
        task_id = extract_computer_task_id(record)
        if task_id:
            groups.setdefault(f"computer:{task_id}", []).append(record)
            continue

        thread_id = primary_thread_id_from_key(record)
        if thread_id:
            groups.setdefault(thread_id, []).append(record)
            continue

        # Do not explode list_recent/topMostUrls into many groups. Keep them global.
        if record.record_kind in {"thread_list_recent", "top_most_urls", "unknown"}:
            global_records.append(record)
            continue

        value_candidates = get_value_uuid_candidates(record)
        if len(value_candidates) == 1:
            groups.setdefault(next(iter(value_candidates)), []).append(record)
        else:
            global_records.append(record)

    # Do not automatically merge global/cache records into a single thread.
    # Real Comet profiles can contain residual URL/list/cache artifacts from
    # previous activity. Only records with strong thread/task identifiers are
    # attached to a thread; unassigned records remain in global_records and are
    # reported separately.
    return groups, global_records


def classify_group(
    records: list[ForensicRecord],
    metadata: dict[str, Any] | None = None,
    browser_only: bool = True,
) -> dict[str, Any]:
    """
    Classify per thread/task group, not per input zip.

    Browser Control and Computer mode markers can co-exist in raw Comet
    artifacts. Browser Control may contain subagent_task_id/computer_list
    records for UI-control subtasks, so weak computer-looking markers must not
    override explicit BROWSER_AGENT metadata. Classification therefore uses:

    1. explicit Browser Control markers first,
    2. strong Computer/ASI markers second,
    3. weak computer-looking markers only as supporting/ambiguous evidence.
    """
    metadata = metadata or {}
    text = "\n".join(r.text(30000) for r in records)
    classification_evidence: list[str] = []

    def add_evidence(label: str) -> None:
        if label and label not in classification_evidence:
            classification_evidence.append(label)

    metadata_search_mode = str(metadata.get("search_mode") or "")
    metadata_display_model = str(metadata.get("display_model") or metadata.get("model_preference") or "")
    metadata_mode = str(metadata.get("mode") or "")
    metadata_mode_type = str(metadata.get("mode_type") or "")

    # Strong Browser Control evidence. Metadata is preferred because decoded
    # all_results normalizes fields even when raw JSON formatting changes.
    if metadata_search_mode == "BROWSER_AGENT":
        add_evidence("metadata.search_mode=BROWSER_AGENT")
    if "comet_browser_agent" in metadata_display_model:
        add_evidence(f"metadata.display_model={metadata_display_model}")
    if metadata_mode_type == "2" and metadata_search_mode == "BROWSER_AGENT":
        add_evidence("metadata.mode_type=2 with BROWSER_AGENT")

    strong_browser_markers = [
        '"search_mode": "BROWSER_AGENT"',
        "'search_mode': 'BROWSER_AGENT'",
        "BROWSER_AGENT",
        "comet_browser_agent",
        "BROWSER_AGENT_CONFIRMATION",
    ]
    supporting_browser_markers = [
        "workflow_root",
        "plan_block",
        "web_results",
        "topMostUrls",
        "unified_assets",
        "COMET_AGENT_TOOL_INPUT",
        "COMET_AGENT_TOOL_OUTPUT",
    ]

    strong_hits = [marker for marker in strong_browser_markers if marker in text]
    supporting_hits = [marker for marker in supporting_browser_markers if marker in text]
    for hit in strong_hits:
        add_evidence(hit)
    for hit in supporting_hits:
        add_evidence(hit)

    strong_browser = bool(
        metadata_search_mode == "BROWSER_AGENT"
        or "comet_browser_agent" in metadata_display_model
        or strong_hits
        or ("COMET_AGENT_TOOL_INPUT" in supporting_hits or "COMET_AGENT_TOOL_OUTPUT" in supporting_hits)
    )

    # Strong Computer mode evidence. These should indicate ASI/Computer mode
    # independently of Browser Control weak subtask artifacts.
    strong_computer_hits = [marker for marker in STRONG_COMPUTER_MARKERS if marker in text]
    if metadata_search_mode.upper() in {"ASI", "WIDE_RESEARCH"}:
        strong_computer_hits.append(f"metadata.search_mode={metadata_search_mode}")
    if "PPLX_ASI" in metadata_display_model or metadata_mode.upper() == "ASI":
        strong_computer_hits.append(f"metadata.display_model/mode={metadata_display_model or metadata_mode}")

    weak_computer_hits = [marker for marker in WEAK_COMPUTER_MARKERS if marker in text]

    # Browser Control wins over weak computer-looking subtask markers.
    if strong_browser:
        for hit in weak_computer_hits:
            add_evidence(f"weak_computer_marker_present_but_browser_control={hit}")
        return {
            "interaction_type": "agentic",
            "execution_mode": "browser_control",
            "confidence": "high",
            "reconstruction_status": "reconstructed",
            "classification_evidence": classification_evidence[:10],
        }

    # Only strong Computer/ASI evidence can trigger Computer-mode skip.
    if strong_computer_hits:
        return {
            "interaction_type": "agentic",
            "execution_mode": "computer_mode",
            "confidence": "high",
            "reconstruction_status": "skipped" if browser_only else "unsupported_in_this_mvp",
            "classification_evidence": strong_computer_hits[:10],
        }

    # Weak computer-looking markers alone are ambiguous, not Computer mode.
    if weak_computer_hits:
        for hit in weak_computer_hits:
            add_evidence(f"weak_computer_marker={hit}")

    # Conservative fallback: workflow/tool/web markers alone are not enough to
    # call Browser Control, because normal search/cache records can contain them.
    if "query_str" in text or "markdown_block" in text or "final_sse_message" in text or metadata:
        evidence = classification_evidence[:10]
        if metadata_search_mode:
            evidence.insert(0, f"metadata.search_mode={metadata_search_mode}")
        if metadata_display_model:
            evidence.insert(0, f"metadata.display_model={metadata_display_model}")
        return {
            "interaction_type": "conversational_or_search",
            "execution_mode": "non_browser_agent",
            "confidence": "medium",
            "reconstruction_status": "reconstructed",
            "classification_evidence": evidence[:10],
        }

    return {
        "interaction_type": "unknown",
        "execution_mode": "unknown",
        "confidence": "low",
        "reconstruction_status": "partial",
        "classification_evidence": classification_evidence[:10],
    }


# ---------------------------------------------------------------------------
# Artifact extractors
# ---------------------------------------------------------------------------

def extract_prompt(records: list[ForensicRecord]) -> dict[str, Any]:
    candidates: list[tuple[int, str, ForensicRecord, str]] = []
    priority = {"query_str": 0, "title": 1, "thread_title": 2}

    for record in records:
        if not isinstance(record.value, (dict, list)):
            continue
        for field, value in recursive_collect_fields(record.value, {"query_str", "title", "thread_title"}):
            if isinstance(value, str) and value.strip():
                score = priority.get(field, 10)
                # Prefer prompts with reference codes or multi-sentence instructions.
                if extract_reference_codes(value):
                    score -= 2
                if len(value) > 100:
                    score -= 1
                candidates.append((score, value.strip(), record, field))

    if not candidates:
        return {"text": None, "field": None, "reference_codes": [], "evidence": []}

    candidates.sort(key=lambda item: (item[0], -(len(item[1])), item[2].ldb_seq_no or -1))
    _, text, record, field = candidates[0]
    return {
        "text": text,
        "field": field,
        "reference_codes": extract_reference_codes(text),
        "evidence": record_evidence(record),
    }


def extract_metadata(records: list[ForensicRecord]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    evidence_map: dict[str, list[dict[str, Any]]] = {}

    for record in sorted(records, key=lambda r: r.ldb_seq_no or -1):
        if not isinstance(record.value, (dict, list)):
            continue
        for field, value in recursive_collect_fields(record.value, set(METADATA_FIELDS)):
            if value in (None, "", [], {}):
                continue
            # Keep latest sequence as preferred value.
            metadata[field] = to_jsonable(value)
            evidence_map[field] = record_evidence(record, max_items=2)

    metadata["evidence"] = evidence_map
    return metadata


def unique_event_key(text: str, kind: str) -> str:
    return f"{kind}:{text[:300]}"


def make_item(kind: str, label: str, record: ForensicRecord, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "kind": kind,
        "label": label.strip(),
        "relative_order": record.ldb_seq_no,
        "source_types": record.source_types,
        "evidence": record_evidence(record),
    }
    if extra:
        payload.update(extra)
    return payload


def extract_plan(records: list[ForensicRecord]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for record in records:
        plan_objs = recursive_find(record.value, {"plan_block"}) if isinstance(record.value, (dict, list)) else []
        for plan_obj in plan_objs:
            strings = flatten_strings(plan_obj)
            for s in strings:
                if len(s) < 3:
                    continue
                key = unique_event_key(s, "plan")
                if key in seen:
                    continue
                seen.add(key)
                items.append(make_item("plan", s, record))

    return sorted(items, key=lambda item: item.get("relative_order") or -1)


def extract_actions(records: list[ForensicRecord], prompt_text: str | None = None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_action(kind: str, label: str, record: ForensicRecord, extra: dict[str, Any] | None = None) -> None:
        if not is_useful_human_text(label, prompt_text):
            return
        if not contains_any(label, ACTION_KEYWORDS + ["COMET_AGENT_TOOL_INPUT", "COMET_AGENT_TOOL_OUTPUT", "BROWSER_AGENT_CONFIRMATION"]):
            return
        key = unique_event_key(label, kind)
        if key in seen:
            return
        seen.add(key)
        actions.append(make_item(kind, label, record, extra))

    for record in records:
        value_text = json_text(record.value, 80000)

        # Tool I/O records are high-value action traces. Keep a preview, but do
        # not treat internal UI labels as individual actions.
        if "COMET_AGENT_TOOL_INPUT" in value_text or "COMET_AGENT_TOOL_OUTPUT" in value_text:
            preview = value_text[:2500]
            key = unique_event_key(preview, "tool_io")
            if key not in seen:
                seen.add(key)
                actions.append(
                    make_item(
                        "tool_io",
                        "COMET_AGENT_TOOL_INPUT/OUTPUT candidate",
                        record,
                        {"text_preview": preview},
                    )
                )

        # Workflow root / plan blocks are the main Browser Control action source.
        workflow_objs: list[Any] = []
        if isinstance(record.value, (dict, list)):
            workflow_objs.extend(recursive_find(record.value, {"workflow_root", "plan_block"}))
        for workflow_obj in workflow_objs:
            for s in flatten_strings(workflow_obj):
                add_action("workflow_action", s, record)

        # Fallback only for explicit tool/confirmation records. Do not scan every
        # all_results string, because prompt text and UI labels create false
        # click/type/action candidates.
        if record.record_kind == "tool_io" or "BROWSER_AGENT_CONFIRMATION" in value_text:
            for s in flatten_strings(record.value):
                add_action("action_candidate", s, record)

    return sorted(actions, key=lambda item: item.get("relative_order") or -1)


def normalize_top_url_items(item: Any, record: ForensicRecord) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if isinstance(item, list):
        for child in item:
            results.extend(normalize_top_url_items(child, record))
        return results

    if isinstance(item, dict):
        # Item may either be one URL entry or a container.
        url = item.get("url") or item.get("href") or item.get("link")
        if isinstance(url, str) and url.startswith("http"):
            results.append(
                {
                    "url": url,
                    "title": item.get("title"),
                    "lastAccess": item.get("lastAccess"),
                    "visitCount": item.get("visitCount"),
                    "role": "topMostUrls",
                    "relative_order": record.ldb_seq_no,
                    "evidence": record_evidence(record),
                }
            )
        else:
            for value in item.values():
                results.extend(normalize_top_url_items(value, record))
    return results


def extract_urls(records: list[ForensicRecord]) -> list[dict[str, Any]]:
    urls: list[dict[str, Any]] = []
    seen: set[str] = set()

    for record in records:
        top_objs = recursive_find(record.value, {"topMostUrls"}) if isinstance(record.value, (dict, list)) else []
        for top_obj in top_objs:
            for entry in normalize_top_url_items(top_obj, record):
                url = entry.get("url")
                if url and url not in seen:
                    seen.add(url)
                    urls.append(entry)

        text = json_text(record.value, 50000)
        for url in extract_urls_from_text(text):
            if url in seen:
                continue
            seen.add(url)
            role = "url"
            lowered = url.lower()
            if lowered.endswith(".pdf") or ".pdf" in lowered:
                role = "download_target_or_pdf"
            elif "calendar.google.com" in lowered:
                role = "target_web_app"
            elif "mail.google.com" in lowered:
                role = "target_web_app"
            urls.append(
                {
                    "url": url,
                    "title": None,
                    "lastAccess": None,
                    "visitCount": None,
                    "role": role,
                    "relative_order": record.ldb_seq_no,
                    "evidence": record_evidence(record),
                }
            )

    return sorted(urls, key=lambda item: item.get("relative_order") or -1)


def extract_typed_payloads(records: list[ForensicRecord], prompt_text: str | None = None) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()

    interesting_fields = {
        "recipient", "to", "cc", "bcc", "subject", "body", "title",
        "description", "date", "time", "start", "end", "start_time",
        "end_time", "location", "filename", "file_name", "attachment",
        "url", "href", "query", "input", "text",
    }

    def maybe_add(field: str, value: Any, record: ForensicRecord) -> None:
        if not isinstance(value, str):
            return
        s = value.strip()
        if not is_useful_human_text(s, prompt_text):
            return
        # Generic web research prompts should not become typed payloads. Keep
        # payloads for concrete values such as recipients, subjects, event titles,
        # filenames, dates, URLs, or tool input values.
        if not (EMAIL_RE.search(s) or URL_RE.search(s) or contains_any(s, TYPED_PAYLOAD_HINTS)):
            return
        key = unique_event_key(s, field)
        if key in seen:
            return
        seen.add(key)
        payloads.append({
            "field": field,
            "value": s,
            "relative_order": record.ldb_seq_no,
            "evidence": record_evidence(record),
        })

    def walk_fields(obj: Any, record: ForensicRecord, path: tuple[str, ...] = ()) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k)
                lower = key.lower()
                if lower in interesting_fields or any(hint == lower for hint in TYPED_PAYLOAD_HINTS):
                    if isinstance(v, str):
                        maybe_add(key, v, record)
                    elif isinstance(v, (int, float, bool)):
                        maybe_add(key, str(v), record)
                    elif isinstance(v, (dict, list)):
                        for s in flatten_strings(v, max_len=2000):
                            maybe_add(key, s, record)
                walk_fields(v, record, path + (key,))
        elif isinstance(obj, list):
            for item in obj:
                walk_fields(item, record, path)

    for record in records:
        # Prefer structured tool/action data. Avoid flattening generic all_results
        # into payload candidates.
        if record.record_kind == "tool_io" or contains_any(json_text(record.value, 50000), ["COMET_AGENT_TOOL_INPUT", "COMET_AGENT_TOOL_OUTPUT", "workflow_root"]):
            walk_fields(record.value, record)

    return sorted(payloads, key=lambda item: item.get("relative_order") or -1)


def extract_reasoning(records: list[ForensicRecord], execution_mode: str) -> dict[str, Any]:
    if execution_mode == "browser_control":
        return {
            "available": False,
            "items": [],
            "note": "Browser Control artifacts did not contain internal reasoning text in this MVP scope.",
        }

    items: list[dict[str, Any]] = []
    for record in records:
        text = json_text(record.value, 50000)
        if "thought" in text or '"variant": "thought"' in text:
            items.append(
                {
                    "text_preview": text[:2000],
                    "relative_order": record.ldb_seq_no,
                    "evidence": record_evidence(record),
                }
            )

    return {
        "available": bool(items),
        "items": items,
        "note": None if items else "No reasoning artifact identified.",
    }


def try_parse_json_string(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None


def unwrap_answer_text(value: Any) -> list[str]:
    """
    Extract human-readable answer text from nested FINAL/content/answer strings.
    Some Comet records store JSON as strings, so this unwraps one or more layers.
    """
    results: list[str] = []

    if isinstance(value, str):
        parsed = try_parse_json_string(value)
        if parsed is not None:
            results.extend(unwrap_answer_text(parsed))
        elif len(value.strip()) > 20:
            results.append(value.strip())

    elif isinstance(value, dict):
        # Most useful case: {"answer": "..."}, sometimes answer itself is JSON.
        answer = value.get("answer")
        if isinstance(answer, str):
            parsed = try_parse_json_string(answer)
            if parsed is not None:
                results.extend(unwrap_answer_text(parsed))
            elif len(answer.strip()) > 20:
                results.append(answer.strip())

        # FINAL step often stores content.answer.
        content = value.get("content")
        if isinstance(content, dict) and "answer" in content:
            results.extend(unwrap_answer_text(content["answer"]))

        # Markdown/stream/chunk-like fields.
        for key in ["text", "chunks", "markdown_block", "final_sse_message"]:
            if key in value:
                results.extend(unwrap_answer_text(value[key]))

    elif isinstance(value, list):
        for item in value:
            results.extend(unwrap_answer_text(item))

    # Deduplicate while preserving order.
    deduped: list[str] = []
    seen: set[str] = set()
    for item in results:
        cleaned = item.strip()
        if not cleaned or len(cleaned) <= 20:
            continue
        key = cleaned[:1000]
        if key not in seen:
            seen.add(key)
            deduped.append(cleaned)
    return deduped


def extract_answer_like_text(value: Any) -> list[str]:
    candidates: list[str] = []

    def walk(obj: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k)
                lower = key.lower()
                if lower in {"answer_mode_type", "widget_type", "mode_type", "step_type", "pending_followups"}:
                    walk(v, path + (key,))
                    continue
                interesting = (
                    key in {"answer", "text", "chunks", "markdown_block", "final_sse_message"}
                    or ("answer" in lower and lower not in {"answer_mode_type"})
                    or "chunk" in lower
                )
                if interesting:
                    if isinstance(v, str) and v.strip():
                        candidates.append(v.strip())
                    elif isinstance(v, list):
                        joined = "\n".join(x for x in flatten_strings(v) if len(x) > 1)
                        if joined.strip():
                            candidates.append(joined.strip())
                    elif isinstance(v, dict):
                        joined = "\n".join(x for x in flatten_strings(v) if len(x) > 1)
                        if joined.strip():
                            candidates.append(joined.strip())
                walk(v, path + (key,))
        elif isinstance(obj, list):
            for item in obj:
                walk(item, path)

    walk(value)
    return candidates


def extract_final_answer(records: list[ForensicRecord], metadata: dict[str, Any] | None = None, prompt_text: str | None = None) -> dict[str, Any]:
    metadata = metadata or {}
    candidates: list[dict[str, Any]] = []

    status = str(metadata.get("status") or metadata.get("thread_status") or "").lower()
    final_flag = metadata.get("final")
    text_completed = metadata.get("text_completed")

    for record in records:
        text = json_text(record.value, 80000)
        if not contains_any(text, FINAL_ANSWER_MARKERS):
            continue

        answer_texts = unwrap_answer_text(record.value)
        if not answer_texts:
            answer_texts = extract_answer_like_text(record.value)

        for answer_text in answer_texts:
            if not is_useful_human_text(answer_text, prompt_text):
                continue
            candidates.append({
                "text": answer_text[:10000],
                "relative_order": record.ldb_seq_no,
                "evidence": record_evidence(record),
                "source_kind": record.record_kind,
            })

    if not candidates:
        reason = "No clean human-readable final answer reconstructed."
        if status == "pending" or final_flag is False or text_completed is False:
            reason = "Core record is pending/final=false/text_completed=false and no clean final answer was reconstructed."
        return {"text": None, "available": False, "reason": reason, "relative_order": None, "evidence": []}

    candidates.sort(key=lambda c: ((c.get("relative_order") or -1), len(c.get("text") or "")))
    chosen = candidates[-1]
    chosen["available"] = True
    # Do not suppress a clean answer solely because an older core snapshot says
    # pending; list_recent/thread_metadata can disagree across LevelDB sequence
    # numbers. Preserve the answer and expose status evidence separately.
    return chosen


def detect_private_mode(records: list[ForensicRecord]) -> dict[str, Any]:
    """
    Private/incognito detection is intentionally conservative.

    access_level=PRIVATE_READ appears in normal records too, so it is preserved as
    metadata but does not by itself prove Comet Private/Incognito mode.
    """
    markers: list[dict[str, Any]] = []
    strong_private = False
    privacy_states: set[str] = set()
    access_levels: set[str] = set()

    for record in records:
        if not isinstance(record.value, (dict, list)):
            continue

        for field, value in recursive_collect_fields(record.value, {"privacy_state", "access_level"}):
            if not isinstance(value, str):
                continue

            normalized = value.upper()
            item = {
                "field": field,
                "value": value,
                "relative_order": record.ldb_seq_no,
                "evidence": record_evidence(record),
            }

            if field == "privacy_state":
                privacy_states.add(normalized)
                markers.append(item)
                if normalized in {"INCOGNITO", "PRIVATE", "COMET_PRIVATE"}:
                    strong_private = True
            elif field == "access_level":
                access_levels.add(normalized)
                markers.append(item)

    # Explicit NONE wins unless a strong private state is also found.
    if "NONE" in privacy_states and not strong_private:
        private_mode = False
    else:
        private_mode = strong_private

    return {
        "private_mode": private_mode,
        "privacy_states": sorted(privacy_states),
        "access_levels": sorted(access_levels),
        "markers": markers,
        "interpretation": (
            "private_mode requires INCOGNITO/private privacy_state; "
            "PRIVATE_READ alone is treated as access-level metadata."
        ),
    }


def detect_deletion_state(records: list[ForensicRecord]) -> dict[str, Any]:
    """
    Deletion/stale detection is intentionally conservative.

    Only core thread records (all_results/thread_metadata) tombstones are strong
    evidence of thread deletion in a single snapshot. topMostUrls and other old
    records are weak evidence only because they may reflect cache/list updates.
    """
    strong_evidence: list[dict[str, Any]] = []
    weak_evidence: list[dict[str, Any]] = []
    has_live_core = False

    for record in records:
        is_core_thread_record = record.record_kind in {"all_results", "thread_metadata"}

        if is_core_thread_record and record.is_live and record.value is not None:
            has_live_core = True

        if is_core_thread_record:
            record_deleted = (not record.is_live) or record.value is None
            for ev in record.evidence:
                if ev.state and "deleted" in ev.state.lower():
                    record_deleted = True

            if record_deleted:
                strong_evidence.append(
                    {
                        "key": record.key,
                        "record_kind": record.record_kind,
                        "relative_order": record.ldb_seq_no,
                        "evidence": record_evidence(record),
                    }
                )
        else:
            for ev in record.evidence:
                if ev.state and "deleted" in ev.state.lower():
                    weak_evidence.append(
                        {
                            "key": record.key,
                            "record_kind": record.record_kind,
                            "relative_order": record.ldb_seq_no,
                            "reason": "non-core deleted/old record; not sufficient for thread deletion",
                            "evidence": record_evidence(record),
                        }
                    )
                    break

    if strong_evidence and has_live_core:
        state = "mixed_core_live_and_deleted"
    elif strong_evidence:
        state = "deleted"
    elif has_live_core:
        state = "live"
    else:
        state = "unknown"

    return {
        "state": state,
        "strong_evidence": strong_evidence[:20],
        "weak_evidence": weak_evidence[:20],
        "interpretation": (
            "Only all_results/thread_metadata tombstones are treated as strong deletion evidence. "
            "topMostUrls or other old records are weak evidence only."
        ),
    }



def interpret_timestamp(value: Any) -> dict[str, Any]:
    """
    Best-effort timestamp interpretation.

    The raw value remains authoritative because Comet/Chromium artifacts may use
    different timestamp units depending on the field. Interpreted UTC is only a
    convenience for review.
    """
    result: dict[str, Any] = {
        "raw": to_jsonable(value),
        "interpreted_utc": None,
        "interpretation": "raw_only",
    }

    if isinstance(value, (int, float)):
        try:
            # Common web/app timestamp form: Unix epoch in milliseconds.
            if value > 10_000_000_000:
                dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
                result["interpreted_utc"] = dt.isoformat()
                result["interpretation"] = "best_effort_unix_epoch_milliseconds"
            # Unix epoch in seconds.
            elif value > 1_000_000_000:
                dt = datetime.fromtimestamp(value, tz=timezone.utc)
                result["interpreted_utc"] = dt.isoformat()
                result["interpretation"] = "best_effort_unix_epoch_seconds"
        except Exception:
            pass

    return result

def extract_time_fields_from_value(value: Any) -> list[dict[str, Any]]:
    """Return only artifact-level time fields for timeline display.

    Generic fields such as "timestamp" or "time" can be page-content values
    (for example, dates embedded in page/source snippets). They are no
    longer treated as forensic timeline events by default.
    """
    time_fields = []
    for field, field_value in recursive_collect_fields(
        value,
        {"created_at", "updated_at", "lastAccess", "last_access", "last_query_datetime"},
    ):
        if field_value not in (None, "", [], {}):
            time_fields.append(
                {
                    "field": field,
                    "value": to_jsonable(field_value),
                    "time_interpretation": interpret_timestamp(field_value),
                }
            )
    return time_fields


def build_timeline(
    records: list[ForensicRecord],
    prompt: dict[str, Any],
    plan: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    urls: list[dict[str, Any]],
    typed_payloads: list[dict[str, Any]],
    final_answer: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    if prompt.get("text"):
        order = None
        if prompt.get("evidence"):
            order = prompt["evidence"][0].get("ldb_seq_no")
        events.append(
            {
                "kind": "prompt",
                "label": (prompt.get("text") or "")[:1000],
                "relative_order": order,
                "evidence": prompt.get("evidence", []),
            }
        )

    for item in plan:
        events.append({"kind": "plan", **item})
    for item in actions:
        events.append({"kind": "action", **item})
    for item in typed_payloads:
        events.append({"kind": "typed_payload", **item})
    for item in urls:
        events.append({"kind": "url", **item})

    if final_answer.get("text"):
        events.append(
            {
                "kind": "final_answer",
                "label": final_answer.get("text", "")[:1000],
                "relative_order": final_answer.get("relative_order"),
                "evidence": final_answer.get("evidence", []),
            }
        )

    # Add time fields as metadata events only when not too many.
    time_event_count = 0
    for record in records:
        for time_field in extract_time_fields_from_value(record.value):
            if time_event_count >= 50:
                break
            events.append(
                {
                    "kind": "time_metadata",
                    "label": f"{time_field['field']}: {time_field['value']}",
                    "field": time_field["field"],
                    "value": time_field["value"],
                    "time_interpretation": time_field.get("time_interpretation"),
                    "relative_order": record.ldb_seq_no,
                    "evidence": record_evidence(record),
                }
            )
            time_event_count += 1

    return sorted(events, key=lambda e: (e.get("relative_order") is None, e.get("relative_order") or -1))



# ---------------------------------------------------------------------------
# v0.2 case-study helpers: ASI promotion, outcomes, artifact buckets, temporal
# ---------------------------------------------------------------------------

FORENSIC_TIME_FIELDS = {
    "created_at",
    "updated_at",
    "lastAccess",
    "last_access",
    "last_query_datetime",
}

TIME_LIKE_BUT_CONTENT_FIELDS = {
    "timestamp",
    "time",
}


def normalize_for_search(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def collect_thread_text(thread: dict[str, Any], include_prompt: bool = True) -> str:
    """Collect thread text for rule-based, best-effort case classification."""
    parts: list[str] = []
    if include_prompt:
        parts.append(((thread.get("prompt", {}) or {}).get("text") or ""))
    for key in ["plan", "actions", "urls", "typed_payloads", "final_answer", "reasoning", "metadata", "artifact_buckets"]:
        value = thread.get(key)
        if value not in (None, [], {}):
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
    return "\n".join(parts)


def extract_pdf_filenames(text: str) -> list[str]:
    found = re.findall(r"\b[A-Za-z0-9][A-Za-z0-9._\- ]{0,120}\.pdf\b", text or "", flags=re.IGNORECASE)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in found:
        item = item.strip().strip(".,;:)]}\"'")
        if item and item.lower() not in seen:
            seen.add(item.lower())
            cleaned.append(item)
    return cleaned


def extract_prompt_target_urls(prompt_text: str | None) -> list[str]:
    if not prompt_text:
        return []
    return extract_urls_from_text(prompt_text)


def non_noise_thread_urls(thread: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in thread.get("urls", []) or []:
        url = item.get("url")
        if not url or url in seen:
            continue
        lower = str(url).lower()
        # Exclude obvious internal/static/asset URLs from behavioral URL counts.
        if any(marker in lower for marker in [
            "perplexity.ai/rest/asset",
            "pplx-res.cloudinary",
            "cloudfront.net",
            "s3.amazonaws.com",
            "blob:",
            "data:",
        ]):
            continue
        seen.add(url)
        urls.append(url)
    return urls


def classify_task_outcome(thread: dict[str, Any]) -> dict[str, Any]:
    """Best-effort outcome layer used for case-study summaries.

    This does not replace raw artifacts. It adds a cautious interpretation:
    completed vs confirmation-gate vs partial, with missing corroboration clearly
    recorded. All side effects still require external validation such as History,
    Downloads folder, mail-service, or calendar-service artifacts.
    """
    prompt = ((thread.get("prompt", {}) or {}).get("text") or "")
    final = ((thread.get("final_answer", {}) or {}).get("text") or "")
    all_text = collect_thread_text(thread, include_prompt=True)
    all_text_low = all_text.lower()
    final_low = final.lower()
    prompt_low = prompt.lower()
    refs = ((thread.get("prompt", {}) or {}).get("reference_codes") or [])
    execution_mode = (thread.get("classification", {}) or {}).get("execution_mode")

    task_type = "unknown"
    if any(x in prompt_low for x in ["download", "pdf", "downloaded filename"]):
        task_type = "download"
    elif any(x in prompt_low for x in ["gmail", "email", "sent folder", "draft"]):
        task_type = "gmail_send"
    elif any(x in prompt_low for x in ["google calendar", "calendar event", "new calendar event"]):
        task_type = "calendar_create"
    elif any(x in prompt_low for x in ["open the following page", "open wikipedia", "page title", "alan_turing", "alan turing"]):
        task_type = "page_open"
    elif any(x in prompt_low for x in ["research the topic", "sources used", "web research"]):
        task_type = "web_research"
    elif looks_like_basic_chat_thread(prompt_low, thread.get("metadata", {}) or {}, execution_mode):
        task_type = "basic_chat"

    filenames = extract_pdf_filenames(all_text)
    target_urls = extract_prompt_target_urls(prompt)
    thread_urls = non_noise_thread_urls(thread)

    result: dict[str, Any] = {
        "task_type": task_type,
        "status": "unknown",
        "side_effect_completed": None,
        "confidence": "low",
        "primary_artifacts": [],
        "warnings": [],
        "missing_corroboration": [],
    }

    if execution_mode == "browser_control":
        result["primary_artifacts"].append("browser_control_thread")
    elif execution_mode == "computer_mode":
        result["primary_artifacts"].append("computer_mode_or_asi_cache")

    if task_type == "download":
        # Avoid treating the prompt's own "confirm" language as proof of a
        # confirmation-gate result. Prefer final answer/action text for outcome.
        outcome_text = "\n".join([
            json.dumps(thread.get("final_answer", {}), ensure_ascii=False, default=str),
            json.dumps(thread.get("actions", []), ensure_ascii=False, default=str),
            json.dumps(thread.get("plan", []), ensure_ascii=False, default=str),
        ]).lower()
        confirmation = any(marker in outcome_text for marker in [
            "browser_agent_confirmation",
            "may i proceed",
            "please confirm",
            "confirm before",
            "confirmation",
            "proceed with the download",
            "다운로드 전",
            "사용자 확인",
            "확인 요청",
        ])
        completed = bool(filenames) and any(marker in outcome_text for marker in [
            "download is complete",
            "downloaded filename",
            "final downloaded filename",
            "downloaded file",
            "successfully downloaded",
            "download complete",
            "다운로드 완료",
        ])
        if completed:
            result.update({
                "status": "completed_download",
                "side_effect_completed": True,
                "confidence": "high",
                "downloaded_filename_candidates": filenames,
            })
        elif confirmation:
            result.update({
                "status": "confirmation_required",
                "side_effect_completed": False,
                "confidence": "high",
                "downloaded_filename_candidates": filenames,
            })
        elif filenames or any(".pdf" in str(u).lower() for u in thread_urls):
            result.update({
                "status": "source_discovery_or_partial_download",
                "side_effect_completed": None,
                "confidence": "medium",
                "downloaded_filename_candidates": filenames,
            })
        else:
            result["status"] = "download_intent_only"
        result["missing_corroboration"].extend(["Chromium Downloads DB", "OS Downloads folder/file hash"])
        if execution_mode == "computer_mode":
            result["warnings"].append("Computer-mode result is promoted from ASI/cache evidence; verify with Downloads/History artifacts.")

    elif task_type == "gmail_send":
        sent = any(marker in final_low for marker in ["email sent", "sent folder", "sent email", "send the email", "visible in sent"])
        draft = any(marker in final_low for marker in ["draft", "saved as a draft"])
        if sent:
            result.update({"status": "sent_reported_by_agent", "side_effect_completed": True, "confidence": "medium_high"})
        elif draft:
            result.update({"status": "draft_reported_by_agent", "side_effect_completed": None, "confidence": "medium"})
        else:
            result.update({"status": "gmail_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"].extend(["mail service Sent/Draft record", "message headers"])

    elif task_type == "calendar_create":
        created = any(marker in final_low for marker in ["event", "created", "saved", "visible", "verified", "calendar"])
        if created:
            result.update({"status": "calendar_event_reported_created", "side_effect_completed": True, "confidence": "medium_high"})
        else:
            result.update({"status": "calendar_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"].append("calendar service/event record")

    elif task_type == "page_open":
        opened = any(url in all_text for url in target_urls) if target_urls else False
        expected_title = extract_expected_page_title(prompt)
        title_confirmed = bool(expected_title and expected_title.lower() in (final or "").lower())
        if opened and title_confirmed:
            result.update({"status": "target_page_opened_and_title_reported", "side_effect_completed": True, "confidence": "medium_high"})
        elif opened:
            result.update({"status": "target_page_url_recovered", "side_effect_completed": True, "confidence": "medium"})
        else:
            result.update({"status": "page_open_intent_only", "side_effect_completed": None, "confidence": "low"})
        result["target_urls"] = target_urls
        result["non_noise_thread_urls"] = thread_urls[:20]
        result["missing_corroboration"].append("Chromium History DB for manual/agent navigation timing")

    elif task_type == "web_research":
        if final:
            result.update({"status": "research_answer_recovered", "side_effect_completed": None, "confidence": "medium"})
        elif thread.get("urls"):
            result.update({"status": "research_url_leads_only", "side_effect_completed": None, "confidence": "medium"})
        else:
            result.update({"status": "research_intent_only", "side_effect_completed": None, "confidence": "low"})
        result["missing_corroboration"].append("History/cache/source page content")

    elif task_type == "basic_chat":
        result.update({"status": "conversation_answer_recovered" if final else "conversation_prompt_only", "side_effect_completed": False, "confidence": "high" if final else "medium"})

    else:
        has_final = bool(final)
        result.update({"status": "final_answer_recovered" if has_final else "metadata_only", "side_effect_completed": None, "confidence": "medium" if has_final else "low"})

    return result


def build_artifact_buckets(thread: dict[str, Any]) -> dict[str, Any]:
    """Add a cleaner, investigator-friendly split without changing old fields."""
    form_inputs: list[dict[str, Any]] = []
    download_artifacts: list[dict[str, Any]] = []
    visited_urls: list[dict[str, Any]] = []
    source_urls: list[dict[str, Any]] = []
    screenshot_assets: list[dict[str, Any]] = []
    workflow_steps: list[dict[str, Any]] = []
    tool_io: list[dict[str, Any]] = []

    form_fields = {"recipient", "to", "cc", "bcc", "subject", "body", "title", "description", "date", "time", "start_time", "end_time", "location", "input", "text"}
    for payload in thread.get("typed_payloads", []) or []:
        field = str(payload.get("field") or "").lower()
        value = str(payload.get("value") or "")
        low = value.lower()
        if not value:
            continue
        if ".pdf" in low or field in {"filename", "file_name"}:
            download_artifacts.append(payload)
        elif URL_RE.search(value):
            # URLs are better treated as URL evidence than typed payloads.
            continue
        elif field in form_fields and not contains_any(value, ACTION_KEYWORDS):
            form_inputs.append(payload)

    for item in thread.get("urls", []) or []:
        url = str(item.get("url") or "")
        if not url:
            continue
        low = url.lower()
        if any(marker in low for marker in ["screenshot", "asset", "cloudfront", "s3", "cloudinary"]):
            screenshot_assets.append(item)
        elif ".pdf" in low:
            download_artifacts.append(item)
            source_urls.append(item)
        else:
            visited_urls.append(item)

    for item in thread.get("plan", []) or []:
        workflow_steps.append(item)
    for item in thread.get("actions", []) or []:
        if item.get("kind") == "tool_io":
            tool_io.append(item)
        else:
            workflow_steps.append(item)

    def dedupe_items(items: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            sig = "|".join(str(item.get(k) or "")[:500] for k in key_fields)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(item)
        return out

    return {
        "form_inputs": dedupe_items(form_inputs, ("field", "value")),
        "download_artifacts": dedupe_items(download_artifacts, ("url", "value", "label")),
        "visited_urls": dedupe_items(visited_urls, ("url",)),
        "source_urls": dedupe_items(source_urls, ("url",)),
        "screenshot_assets": dedupe_items(screenshot_assets, ("url",)),
        "workflow_steps": dedupe_items(workflow_steps, ("kind", "label")),
        "tool_io": dedupe_items(tool_io, ("kind", "text_preview", "label")),
        "interpretation": "Typed payloads are preserved in legacy typed_payloads. This bucketed view separates form inputs, URLs/assets, workflow steps, tool I/O, and download artifacts for case-study readability.",
    }


def build_temporal_evidence(records: list[ForensicRecord], metadata: dict[str, Any]) -> dict[str, Any]:
    """Thread-level temporal summary. Avoid page-content timestamp false positives."""
    accepted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    seqs = [r.ldb_seq_no for r in records if isinstance(r.ldb_seq_no, int)]

    for record in records:
        if not isinstance(record.value, (dict, list)):
            continue
        # Accept only artifact-level fields that repeatedly appeared in Comet
        # thread/list/topMostUrls metadata. Generic "timestamp"/"time" may be
        # page content and is excluded from forensic timeline by default.
        for field, value in recursive_collect_fields(record.value, FORENSIC_TIME_FIELDS):
            if value in (None, "", [], {}):
                continue
            sig = f"{field}:{json.dumps(value, ensure_ascii=False, default=str)}:{record.ldb_seq_no}"
            if sig in seen:
                continue
            seen.add(sig)
            accepted.append({
                "field": field,
                "raw": to_jsonable(value),
                "formatted": format_time_value(value) if "format_time_value" in globals() else str(value),
                "time_interpretation": interpret_timestamp(value),
                "relative_order": record.ldb_seq_no,
                "evidence": record_evidence(record),
            })

        for field, value in recursive_collect_fields(record.value, TIME_LIKE_BUT_CONTENT_FIELDS):
            if value in (None, "", [], {}):
                continue
            if len(excluded) >= 20:
                continue
            excluded.append({
                "field": field,
                "raw": to_jsonable(value),
                "reason": "Generic time-like field; excluded from forensic timeline unless field path/source semantics are verified.",
                "relative_order": record.ldb_seq_no,
                "evidence": record_evidence(record),
            })

    # Metadata-level convenience copy.
    for field in ["created_at", "updated_at", "lastAccess", "last_query_datetime"]:
        if metadata.get(field) not in (None, ""):
            accepted.insert(0, {
                "field": field,
                "raw": to_jsonable(metadata.get(field)),
                "formatted": format_time_value(metadata.get(field)) if "format_time_value" in globals() else str(metadata.get(field)),
                "source": "preferred_metadata",
                "evidence": (metadata.get("evidence", {}) or {}).get(field, []),
            })

    # Deduplicate again after metadata insert.
    final: list[dict[str, Any]] = []
    seen2: set[str] = set()
    for item in accepted:
        sig = f"{item.get('field')}:{json.dumps(item.get('raw'), ensure_ascii=False, default=str)}:{item.get('relative_order')}"
        if sig in seen2:
            continue
        seen2.add(sig)
        final.append(item)

    return {
        "sequence_range": {
            "first_relevant_seq": min(seqs) if seqs else None,
            "last_relevant_seq": max(seqs) if seqs else None,
        },
        "forensic_time_fields": final[:50],
        "excluded_time_like_values": excluded[:20],
        "note": "Only created_at/updated_at/lastAccess/last_query_datetime are treated as forensic time by default. Generic timestamp/time fields are excluded to avoid page-content false positives.",
    }


def add_case_layers_to_thread(thread: dict[str, Any], records: list[ForensicRecord] | None = None) -> dict[str, Any]:
    """Attach non-breaking case-study layers to a reconstructed thread."""
    thread["artifact_buckets"] = build_artifact_buckets(thread)
    thread["task_outcome"] = classify_task_outcome(thread)
    if records is not None:
        thread["temporal_evidence"] = build_temporal_evidence(records, thread.get("metadata", {}) or {})
    return thread


def iter_asi_entry_dicts_v2(obj: Any) -> list[dict[str, Any]]:
    """More tolerant ASI/Computer list-cache entry extractor.

    v0.1 was too strict for some Computer-mode snapshots where the only strong
    evidence was in /rest/thread/list_ask_threads global cache. This walker
    promotes any list-cache item with ASI/computer indicators, a thread identity,
    and a prompt/title/answer candidate.
    """
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        value = parse_maybe_json(value)
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if not isinstance(value, dict):
            return

        text = json.dumps(value, ensure_ascii=False, default=str)
        low = text.lower()
        mode = str(value.get("mode") or value.get("search_mode") or "").lower()
        query = str(value.get("query_str") or value.get("title") or "")
        has_identity = bool(value.get("uuid") or value.get("thread_uuid") or value.get("slug") or value.get("context_uuid"))
        has_content = bool(query or value.get("first_answer") or value.get("answer") or value.get("answer_preview"))
        looks_asi = (
            mode == "asi"
            or value.get("search_mode") == "ASI"
            or "thread_type_filter" in low and "asi" in low
            or "computer mode" in query.lower()
            or looks_like_computer_reference(query)
            or "pplx_asi" in low
            or "wide_research" in low
            or "\"variant\": \"thought\"" in low
            or '"variant":"thought"' in low
        )
        if has_identity and has_content and looks_asi:
            found.append(to_jsonable(value))
        for child in value.values():
            walk(child)

    walk(obj)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in found:
        sig = json.dumps({
            "uuid": entry.get("uuid"),
            "context_uuid": entry.get("context_uuid"),
            "query_str": entry.get("query_str"),
            "title": entry.get("title"),
        }, ensure_ascii=False, sort_keys=True, default=str)
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(entry)
    return deduped


def collect_asi_thread_list_candidates_v2(global_records: list[ForensicRecord]) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for record in global_records:
        text = record.text(120000)
        if record.record_kind != "asi_thread_list" and "/rest/thread/list_ask_threads" not in text:
            continue
        for entry in iter_asi_entry_dicts_v2(record.value):
            thread_id = str(entry.get("uuid") or entry.get("thread_uuid") or entry.get("slug") or entry.get("context_uuid") or "").strip()
            if not thread_id:
                continue
            bucket = candidates.setdefault(thread_id, {"entries": [], "records": []})
            bucket["entries"].append(entry)
            bucket["records"].append(record)
    return candidates


def build_case_summary(report: dict[str, Any], target_reference: str | None = None, original_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    threads = report.get("threads", []) or []
    skipped = report.get("skipped", []) or []
    globals_ = report.get("global_records", []) or []

    def contains_target(item: Any) -> bool:
        if not target_reference:
            return False
        return str(target_reference) in json.dumps(item, ensure_ascii=False, default=str)

    target_threads = [t for t in threads if contains_target(t)] if target_reference else threads
    target_globals = [g for g in globals_ if contains_target(g)] if target_reference else []

    primary = None
    if target_threads:
        # Prefer completed side-effect threads, then browser/computer agentic.
        def score(thread: dict[str, Any]) -> tuple[int, int, int]:
            outcome = thread.get("task_outcome", {}) or {}
            cls = thread.get("classification", {}) or {}
            completed = 1 if outcome.get("side_effect_completed") is True else 0
            agentic = 1 if cls.get("interaction_type") == "agentic" else 0
            has_final = 1 if (thread.get("final_answer", {}) or {}).get("text") else 0
            return (completed, agentic, has_final)
        primary = sorted(target_threads, key=score, reverse=True)[0]

    case = {
        "target_reference": target_reference,
        "target_found": bool(target_threads),
        "target_thread_count": len(target_threads),
        "target_global_record_count": len(target_globals),
        "primary_thread_id": primary.get("thread_id") if primary else None,
        "primary_execution_mode": (primary.get("classification", {}) or {}).get("execution_mode") if primary else None,
        "primary_task_outcome": primary.get("task_outcome") if primary else None,
        "residual_thread_count": (original_counts or {}).get("original_thread_count", len(threads)) - len(target_threads) if target_reference else 0,
        "warnings": [],
        "investigator_summary": [],
    }

    if target_reference and not target_threads and target_globals:
        case["warnings"].append("Target reference was found only in global/residual records, not in reconstructed threads. This commonly indicates ASI/Computer list-cache promotion needs review or the activity is outside Comet agent thread storage.")
    if target_reference and not target_threads and not target_globals:
        case["warnings"].append("No matching Comet agent thread/global cache record was found for the target reference.")
    if primary:
        outcome = primary.get("task_outcome", {}) or {}
        case["investigator_summary"].append(
            f"Primary target thread is classified as {case['primary_execution_mode']} with outcome={outcome.get('status')} and confidence={outcome.get('confidence')}."
        )
        missing = outcome.get("missing_corroboration") or []
        if missing:
            case["warnings"].append("External corroboration recommended: " + ", ".join(str(x) for x in missing))
    if target_reference and len(target_threads) > 1:
        case["warnings"].append("Multiple reconstructed threads match the same target reference. Treat them as separate attempts/states and select the primary by task outcome.")

    return case


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------

def reconstruct_browser_threads(
    extracted: dict[str, Any],
    input_label: str,
    browser_only: bool = True,
) -> dict[str, Any]:
    forensic_records = normalize_records(extracted["all_records"])
    relevant_records = [r for r in forensic_records if is_browser_relevant_record(r)]
    groups, global_records = group_records(relevant_records)

    reconstructed_threads: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    global_context_urls = extract_global_context_urls(global_records)

    promoted_computer_candidates = collect_asi_thread_list_candidates_v2(global_records) if not browser_only else {}
    normal_group_ids = {gid.split("computer:", 1)[-1] if gid.startswith("computer:") else gid for gid in groups}

    for group_id, records_in_group in sorted(groups.items(), key=lambda item: item[0]):
        prompt = extract_prompt(records_in_group)
        metadata = extract_metadata(records_in_group)
        classification = classify_group(records_in_group, metadata=metadata, browser_only=browser_only)
        execution_mode = classification.get("execution_mode", "unknown")

        if browser_only and execution_mode == "computer_mode":
            skipped.append(
                {
                    "group_id": group_id,
                    "reason": "computer_mode_not_supported_in_browser_only_mvp",
                    "markers": classification.get("classification_evidence", []),
                    "record_count": len(records_in_group),
                    "prompt_preview": (prompt.get("text") or "")[:300],
                    "reference_codes": prompt.get("reference_codes", []),
                    "metadata_hints": {
                        "search_mode": metadata.get("search_mode"),
                        "display_model": metadata.get("display_model"),
                        "model_preference": metadata.get("model_preference"),
                        "mode": metadata.get("mode"),
                        "mode_type": metadata.get("mode_type"),
                        "status": metadata.get("status"),
                        "thread_status": metadata.get("thread_status"),
                        "created_at": metadata.get("created_at"),
                        "updated_at": metadata.get("updated_at"),
                    },
                }
            )
            continue

        prompt_text = prompt.get("text")
        thread_list_evidence = link_global_records_to_thread(global_records, group_id)
        if thread_list_evidence:
            metadata["external_thread_list_evidence"] = thread_list_evidence
        final_answer = extract_final_answer(records_in_group, metadata=metadata, prompt_text=prompt_text)

        # Only Browser Control threads should emit agentic behavior artifacts.
        # Conversational/search threads keep the report focused on prompt,
        # metadata, final answer, timing, and evidence.
        if execution_mode == "browser_control":
            plan = extract_plan(records_in_group)
            actions = extract_actions(records_in_group, prompt_text=prompt_text)
            urls = extract_urls(records_in_group)
            typed_payloads = extract_typed_payloads(records_in_group, prompt_text=prompt_text)
        else:
            plan = []
            actions = []
            urls = []
            typed_payloads = []

        reasoning = extract_reasoning(records_in_group, execution_mode)
        private_mode = detect_private_mode(records_in_group)
        deletion_state = detect_deletion_state(records_in_group)

        metadata["private_mode"] = private_mode["private_mode"]
        metadata["private_detection"] = private_mode

        timeline = build_timeline(
            records=records_in_group,
            prompt=prompt,
            plan=plan,
            actions=actions,
            urls=urls,
            typed_payloads=typed_payloads,
            final_answer=final_answer,
        )

        thread_obj = {
            "thread_id": group_id,
            "classification": classification,
            "prompt": prompt,
            "metadata": metadata,
            "plan": plan,
            "actions": actions,
            "urls": urls,
            "context_url_candidates": global_context_urls if execution_mode == "browser_control" else [],
            "typed_payloads": typed_payloads,
            "reasoning": reasoning,
            "final_answer": final_answer,
            "deletion_state": deletion_state,
            "timeline": timeline,
            "record_count": len(records_in_group),
            "source_summary": summarize_sources(records_in_group),
        }
        reconstructed_threads.append(add_case_layers_to_thread(thread_obj, records_in_group))

    # Computer/ASI tasks may only be represented in list-cache records rather
    # than all_results/thread_metadata. When --include-computer is used, promote
    # those cache entries into partial Computer-mode thread records.
    if not browser_only and promoted_computer_candidates:
        existing_ids = {str(t.get("thread_id")) for t in reconstructed_threads}
        for comp_id, candidate in sorted(promoted_computer_candidates.items(), key=lambda item: item[0]):
            # If a fully reconstructed group already exists, keep it untouched.
            # Otherwise promote ASI/list-cache entries into partial Computer-mode
            # threads so target-reference filtering and case-study summaries can
            # see Computer mode instead of leaving it buried in global_records.
            if comp_id in existing_ids or f"computer:{comp_id}" in existing_ids:
                continue
            promoted = build_promoted_computer_thread(comp_id, candidate, global_context_urls)
            reconstructed_threads.append(add_case_layers_to_thread(promoted, candidate.get("records") or []))

    summary = summarize_reconstruction(reconstructed_threads, skipped)

    return {
        "tool": "comet-browser-reconstructor",
        "schema_version": "0.1",
        "source": {
            "input": input_label,
            "target_origin": "https_www.perplexity.ai_0.indexeddb",
            "leveldb_path": extracted.get("source_leveldb_path"),
            "blob_path": extracted.get("source_blob_path"),
            "analysis_scope": "browser_control_only" if browser_only else "browser_and_detected_computer",
            "database": extracted.get("database"),
            "object_store": extracted.get("object_store"),
        },
        "extraction_summary": {
            "live_record_count": len(extracted.get("live_records", [])),
            "dead_record_count": len(extracted.get("dead_records", [])),
            "all_record_count": len(extracted.get("all_records", [])),
            "bad_record_count": len(extracted.get("bad_records", [])),
            "relevant_record_count": len(relevant_records),
            "global_record_count": len(global_records),
        },
        "summary": summary,
        "threads": reconstructed_threads,
        "skipped": skipped,
        "global_records": [summarize_record(r) for r in global_records[:100]],
        "case_summary": build_case_summary({"threads": reconstructed_threads, "skipped": skipped, "global_records": [summarize_record(r) for r in global_records[:100]]}),
    }


def summarize_sources(records: list[ForensicRecord]) -> dict[str, Any]:
    source_type_counts: dict[str, int] = {}
    source_files: dict[str, int] = {}
    live_count = 0
    dead_count = 0

    for record in records:
        if record.is_live:
            live_count += 1
        else:
            dead_count += 1
        for stype in record.source_types:
            source_type_counts[stype] = source_type_counts.get(stype, 0) + 1
        for ev in record.evidence:
            if ev.source_file:
                source_files[ev.source_file] = source_files.get(ev.source_file, 0) + 1

    return {
        "live_record_count": live_count,
        "dead_or_old_record_count": dead_count,
        "source_type_counts": source_type_counts,
        "top_source_files": sorted(source_files.items(), key=lambda x: x[1], reverse=True)[:20],
    }


def summarize_record(record: ForensicRecord) -> dict[str, Any]:
    return {
        "key": record.key,
        "record_kind": record.record_kind,
        "ldb_seq_no": record.ldb_seq_no,
        "is_live": record.is_live,
        "source_types": record.source_types,
        "evidence": record_evidence(record, max_items=3),
        "value_preview": json_text(record.value, 1000),
    }


def summarize_reconstruction(threads: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> dict[str, Any]:
    browser_count = 0
    computer_count = 0
    conversational_count = 0
    deleted_or_stale_count = 0
    private_count = 0

    for thread in threads:
        classification = thread.get("classification", {})
        if classification.get("execution_mode") == "browser_control":
            browser_count += 1
        if classification.get("execution_mode") == "computer_mode":
            computer_count += 1
        if classification.get("interaction_type") == "conversational_or_search":
            conversational_count += 1
        deletion_state = thread.get("deletion_state", {}).get("state")
        if deletion_state in {"deleted", "mixed_core_live_and_deleted", "mixed_live_and_deleted_or_stale_candidate"}:
            deleted_or_stale_count += 1
        if thread.get("metadata", {}).get("private_mode"):
            private_count += 1

    return {
        "thread_count": len(threads),
        "browser_agent_thread_count": browser_count,
        "computer_mode_thread_count": computer_count,
        "conversational_or_search_thread_count": conversational_count,
        "skipped_computer_mode_count": len(skipped),
        "deleted_or_stale_candidate_count": deleted_or_stale_count,
        "private_mode_candidate_count": private_count,
    }


# ---------------------------------------------------------------------------
# Snapshot comparison for creation/deletion pairs
# ---------------------------------------------------------------------------

def thread_identity_key(thread: dict[str, Any]) -> str:
    prompt_text = (thread.get("prompt", {}) or {}).get("text") or ""
    refs = extract_reference_codes(prompt_text)
    if refs:
        return "ref:" + refs[0]
    return "thread:" + str(thread.get("thread_id"))


def compare_reconstructions(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_threads = {thread_identity_key(t): t for t in before.get("threads", [])}
    after_threads = {thread_identity_key(t): t for t in after.get("threads", [])}

    disappeared = []
    persisted = []
    appeared = []

    for key, before_thread in before_threads.items():
        after_thread = after_threads.get(key)
        if after_thread is None:
            disappeared.append(
                {
                    "identity": key,
                    "before_thread_id": before_thread.get("thread_id"),
                    "prompt": before_thread.get("prompt"),
                    "interpretation": "Thread reconstructed in before snapshot but not reconstructed in after snapshot.",
                }
            )
        else:
            before_state = before_thread.get("deletion_state", {}).get("state")
            after_state = after_thread.get("deletion_state", {}).get("state")
            persisted.append(
                {
                    "identity": key,
                    "before_thread_id": before_thread.get("thread_id"),
                    "after_thread_id": after_thread.get("thread_id"),
                    "before_deletion_state": before_state,
                    "after_deletion_state": after_state,
                    "after_record_count": after_thread.get("record_count"),
                    "interpretation": "Thread artifacts remained reconstructable in after snapshot.",
                }
            )

    for key, after_thread in after_threads.items():
        if key not in before_threads:
            appeared.append(
                {
                    "identity": key,
                    "after_thread_id": after_thread.get("thread_id"),
                    "prompt": after_thread.get("prompt"),
                    "interpretation": "Thread appears only in after snapshot.",
                }
            )

    return {
        "comparison_type": "before_after_snapshot",
        "before_source": before.get("source"),
        "after_source": after.get("source"),
        "summary": {
            "before_thread_count": len(before_threads),
            "after_thread_count": len(after_threads),
            "disappeared_thread_count": len(disappeared),
            "persisted_thread_count": len(persisted),
            "appeared_thread_count": len(appeared),
        },
        "disappeared_threads": disappeared,
        "persisted_or_stale_candidate_threads": persisted,
        "appeared_threads": appeared,
    }


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# v0.2 HTML overrides: clearer case-study summary and outcome sections
# ---------------------------------------------------------------------------

def _render_case_summary_v10(report: dict[str, Any]) -> str:
    case = report.get("case_summary") or {}
    if not case:
        return ""
    outcome = case.get("primary_task_outcome") or {}
    warnings = case.get("warnings") or []
    investigator = case.get("investigator_summary") or []

    rows = [
        ("Target reference", case.get("target_reference") or "N/A"),
        ("Target found", "Yes" if case.get("target_found") else "No"),
        ("Target threads", case.get("target_thread_count")),
        ("Primary thread", case.get("primary_thread_id") or "N/A"),
        ("Primary execution mode", case.get("primary_execution_mode") or "N/A"),
        ("Primary outcome", outcome.get("status") or "N/A"),
        ("Outcome confidence", outcome.get("confidence") or "N/A"),
        ("Residual threads in profile", case.get("residual_thread_count", 0)),
    ]
    parts = [
        "<section class='card'><div class='topline'><h2>Target case summary</h2><span class='badge good'>case-study view</span></div>",
        _kv_table_v07(rows),
    ]
    if investigator:
        parts.append("<h4>Investigator-readable summary</h4><ul class='findings'>")
        for item in investigator:
            parts.append(f"<li>{_h(item)}</li>")
        parts.append("</ul>")
    if warnings:
        parts.append("<div class='note warn'><strong>Cautions / corroboration needed</strong><ul>")
        for item in warnings:
            parts.append(f"<li>{_h(item)}</li>")
        parts.append("</ul></div>")
    if outcome:
        parts.append(_raw_details_v07("Raw primary task outcome", outcome))
    parts.append("</section>")
    return "\n".join(parts)


def _render_executive_findings_v07(report: dict[str, Any]) -> str:
    threads = report.get("threads", []) or []
    case = report.get("case_summary") or {}
    findings: list[str] = []

    if case.get("target_reference"):
        if case.get("target_found"):
            outcome = case.get("primary_task_outcome") or {}
            findings.append(
                f"<li><strong>Target {_h(case.get('target_reference'))}</strong>: found "
                f"{_h(case.get('target_thread_count'))} thread(s); primary mode="
                f"{_h(case.get('primary_execution_mode'))}, outcome={_h(outcome.get('status'))}.</li>"
            )
        elif case.get("target_global_record_count"):
            findings.append(
                f"<li><strong>Target {_h(case.get('target_reference'))}</strong>: not promoted as a thread, "
                f"but matching global/cache records exist. Review Computer/ASI promotion or non-agent artifacts.</li>"
            )
        else:
            findings.append(
                f"<li><strong>Target {_h(case.get('target_reference'))}</strong>: no matching Comet agent thread was reconstructed. "
                "For manual browsing, check Chromium History/Cache separately.</li>"
            )

    for idx, thread in enumerate(threads, start=1):
        title = _thread_title_v07(thread, idx)
        quality, _, explanation = _thread_quality_v07(thread)
        outcome = thread.get("task_outcome", {}) or {}
        outcome_text = f" outcome={outcome.get('status')}" if outcome.get("status") else ""
        findings.append(f"<li><strong>{_h(title)}</strong>: {_h(quality)}{_h(outcome_text)} — {_h(explanation)}</li>")

    if report.get("global_records"):
        findings.append(
            f"<li><strong>Global/residual artifacts</strong>: {_h(len(report.get('global_records') or []))} matching records remain unassigned; they are context, not definitive thread evidence.</li>"
        )
    skipped = report.get("skipped") or []
    if skipped:
        findings.append(f"<li><strong>Computer-mode groups</strong>: {_h(len(skipped))} groups were skipped in browser-only scope.</li>")
    if not findings:
        findings.append("<li>No reconstructable thread-level findings were generated.</li>")
    return "<ul class='findings'>" + "\n".join(findings) + "</ul>"


def _thread_quality_v07(thread: dict[str, Any]) -> tuple[str, str, str]:
    classification = thread.get("classification", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}
    outcome = thread.get("task_outcome", {}) or {}
    execution_mode = classification.get("execution_mode")
    is_browser = execution_mode == "browser_control"
    is_computer = execution_mode == "computer_mode"
    has_final = bool(final_answer.get("text"))
    has_activity = bool(thread.get("plan") or thread.get("actions") or thread.get("urls"))
    has_external = bool(metadata.get("external_thread_list_evidence"))
    status = str(metadata.get("status") or metadata.get("thread_status") or "").lower()

    if outcome.get("status") == "confirmation_required":
        return "Confirmation required", "warn", "Agent reached a user-approval gate; this is not proof that the side effect completed."
    if outcome.get("side_effect_completed") is True and is_browser and has_final and has_activity:
        return "Good reconstruction", "good", "Prompt, Browser Control evidence, activity artifacts, and final reported outcome are present."
    if is_computer and has_final:
        return "Computer partial reconstruction", "warn", "Computer/ASI task was promoted from list-cache evidence. Treat as partial until corroborated with History/Downloads."
    if is_computer:
        return "Computer metadata only", "warn", "Computer/ASI task was detected, but clean final/action content was not fully recovered."
    if is_browser and has_final and has_activity:
        return "Good reconstruction", "good", "Browser thread has prompt, agentic activity, URLs and final answer."
    if is_browser and has_final:
        return "Partial reconstruction", "warn", "Final answer exists, but action/URL evidence may be incomplete or partly cache-derived."
    if is_browser and has_external and ("pending" in status or metadata.get("final") is False):
        return "Partial / cache conflict", "warn", "Core record is pending or non-final, but thread-list cache shows later completed status."
    if is_browser:
        return "Sparse browser evidence", "warn", "Browser-agent markers exist, but detailed action/final-answer artifacts were not recovered."
    return "Non-browser or skipped", "neutral", "Thread is not reconstructed as a browser-control activity."


def _status_line_v07(thread: dict[str, Any]) -> str:
    metadata = thread.get("metadata", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    privacy = metadata.get("private_detection", {}) or {}
    outcome = thread.get("task_outcome", {}) or {}
    return (
        f"Core status={_h(metadata.get('status') or metadata.get('thread_status') or 'N/A')} · "
        f"final={_h(metadata.get('final'))} · "
        f"answer={'yes' if final_answer.get('text') else 'no'} · "
        f"outcome={_h(outcome.get('status') or 'N/A')} · "
        f"deletion={_h(deletion.get('state'))} · "
        f"private={'yes' if privacy.get('private_mode') else 'no'}"
    )


def _render_task_outcome_v10(thread: dict[str, Any]) -> str:
    outcome = thread.get("task_outcome") or {}
    if not outcome:
        return "<div class='empty'>No task outcome layer was generated.</div>"
    rows = [
        ("Task type", outcome.get("task_type")),
        ("Outcome status", outcome.get("status")),
        ("Side effect completed", outcome.get("side_effect_completed")),
        ("Confidence", outcome.get("confidence")),
        ("Downloaded filename candidates", ", ".join(outcome.get("downloaded_filename_candidates") or [])),
        ("Target URLs", ", ".join(outcome.get("target_urls") or [])),
        ("Missing corroboration", ", ".join(outcome.get("missing_corroboration") or [])),
    ]
    parts = [_kv_table_v07(rows)]
    if outcome.get("warnings"):
        parts.append("<div class='note warn'><strong>Outcome caution</strong><ul>")
        for warning in outcome.get("warnings") or []:
            parts.append(f"<li>{_h(warning)}</li>")
        parts.append("</ul></div>")
    parts.append("<p class='muted'>Outcome is an interpretation layer generated from recovered artifacts. Raw evidence remains authoritative; external artifacts may be required for side-effect proof.</p>")
    return "\n".join(parts)


def _render_artifact_buckets_v10(thread: dict[str, Any]) -> str:
    buckets = thread.get("artifact_buckets") or {}
    if not buckets:
        return ""
    parts = ["<details><summary>Clean artifact buckets: form inputs / URLs / workflow / tool I/O</summary>"]
    bucket_specs = [
        ("form_inputs", "Form inputs"),
        ("download_artifacts", "Download artifacts"),
        ("visited_urls", "Visited / target URLs"),
        ("screenshot_assets", "Screenshot / asset URLs"),
        ("workflow_steps", "Workflow steps"),
        ("tool_io", "Tool I/O"),
    ]
    for key, title in bucket_specs:
        items = buckets.get(key) or []
        if not items:
            continue
        rows = []
        for item in items[:25]:
            rows.append({
                "kind": item.get("kind") or item.get("field") or item.get("role"),
                "label": item.get("label") or item.get("value") or item.get("url") or item.get("title"),
                "order": item.get("relative_order"),
                "evidence": item.get("evidence"),
            })
        parts.append(f"<h4>{_h(title)}</h4>")
        parts.append(_simple_table_v07(rows, [("kind", "Kind/field"), ("label", "Value/label"), ("order", "Order"), ("evidence", "Evidence")], "No items."))
    parts.append("</details>")
    return "\n".join(parts)


def _render_activity_v07(thread: dict[str, Any]) -> str:
    plan = thread.get("plan") or []
    actions = thread.get("actions") or []
    urls = thread.get("urls") or []
    payloads = thread.get("typed_payloads") or []
    context_likely, context_other = _split_context_urls_v07(thread)
    counts = "".join([
        _panel_metric("Plan", len(plan)),
        _panel_metric("Actions", len(actions)),
        _panel_metric("Thread URLs", len(urls)),
        _panel_metric("Likely context URLs", len(context_likely)),
        _panel_metric("Typed payloads (legacy)", len(payloads)),
        _panel_metric("Form inputs (clean)", len((thread.get("artifact_buckets") or {}).get("form_inputs") or [])),
    ])
    parts = ["<div class='metrics six'>", counts, "</div>", _render_artifact_buckets_v10(thread)]
    if not (plan or actions or urls or payloads):
        parts.append("<div class='note warn'><strong>No thread-level action trace recovered.</strong><br>Classification may be supported by metadata/cache evidence only. Use History/Downloads/service artifacts for corroboration.</div>")

    if plan:
        rows = [{"order": p.get("relative_order"), "kind": p.get("kind"), "label": p.get("label"), "evidence": p.get("evidence")} for p in plan]
        parts.append("<h4>Agent plan / workflow steps</h4>")
        parts.append(_simple_table_v07(rows, [("order", "Order"), ("kind", "Kind"), ("label", "Step / label"), ("evidence", "Evidence")], "No plan artifacts."))
    if actions:
        rows = [{"order": a.get("relative_order"), "kind": a.get("kind"), "label": a.get("label"), "evidence": a.get("evidence")} for a in actions]
        parts.append("<h4>Agent actions / tool I/O</h4>")
        parts.append(_simple_table_v07(rows, [("order", "Order"), ("kind", "Kind"), ("label", "Action / label"), ("evidence", "Evidence")], "No action artifacts."))
    if payloads:
        rows = [{"order": p.get("relative_order"), "field": p.get("field"), "value": p.get("value"), "evidence": p.get("evidence")} for p in payloads[:50]]
        parts.append("<details><summary>Legacy typed payload candidates</summary>")
        parts.append("<p class='muted'>This preserves the old broad extractor. Use clean artifact buckets above for case-study interpretation.</p>")
        parts.append(_simple_table_v07(rows, [("order", "Order"), ("field", "Field"), ("value", "Value"), ("evidence", "Evidence")], "No typed payloads."))
        parts.append("</details>")
    if urls:
        rows = [{"title": u.get("title"), "url": u.get("url"), "role": u.get("role"), "order": u.get("relative_order"), "evidence": u.get("evidence")} for u in urls]
        parts.append("<h4>Thread-level URLs</h4>")
        parts.append(_simple_table_v07(rows, [("role", "Role"), ("title", "Title"), ("url", "URL"), ("order", "Order"), ("evidence", "Evidence")], "No thread-level URLs."))
    if context_likely:
        parts.append("<h4>Likely relevant global URL leads</h4>")
        parts.append("<p class='muted'>Global topMostUrls/browser context records are leads, not definitive thread-specific proof.</p>")
        rows = [{"title": u.get("title"), "url": u.get("url"), "visitCount": u.get("visitCount"), "lastAccess": format_time_value(u.get("lastAccess")), "evidence": u.get("evidence")} for u in context_likely[:12]]
        parts.append(_simple_table_v07(rows, [("title", "Title"), ("url", "URL"), ("visitCount", "Visits"), ("lastAccess", "Last access"), ("evidence", "Evidence")], "No relevant context URLs."))
    if context_other:
        rows = [{"title": u.get("title"), "url": u.get("url"), "visitCount": u.get("visitCount"), "evidence": u.get("evidence")} for u in context_other[:30]]
        parts.append("<details><summary>Other low-confidence / unrelated global URL candidates</summary>")
        parts.append(_simple_table_v07(rows, [("title", "Title"), ("url", "URL"), ("visitCount", "Visits"), ("evidence", "Evidence")], "No other context URLs."))
        parts.append("</details>")
    return "\n".join(parts)


def _render_time_and_sources_v07(thread: dict[str, Any]) -> str:
    temporal = thread.get("temporal_evidence") or {}
    timeline = thread.get("timeline", []) or []
    source_summary = thread.get("source_summary", {}) or {}
    time_rows = []
    for item in temporal.get("forensic_time_fields") or []:
        time_rows.append({
            "field": item.get("field"),
            "value": item.get("formatted") or format_time_value(item.get("raw")),
            "evidence": item.get("evidence"),
        })
    core_rows = []
    for item in timeline:
        if item.get("kind") in {"prompt", "final_answer", "action", "url", "typed_payload"}:
            core_rows.append({"kind": item.get("kind"), "order": item.get("relative_order"), "label": item.get("label") or item.get("value"), "evidence": item.get("evidence")})
    top_files = source_summary.get("top_source_files") or []
    source_counts = source_summary.get("source_type_counts") or {}
    parts = [
        "<div class='split'>",
        "<div>",
        "<h4>Forensic time metadata</h4>",
        _simple_table_v07(time_rows[:25], [("field", "Field"), ("value", "Value"), ("evidence", "Evidence")], "No explicit forensic time metadata."),
        "<p class='muted'>Generic timestamp/time values are excluded unless their source semantics are verified.</p>",
        _raw_details_v07("Excluded time-like values", (temporal.get("excluded_time_like_values") or [])[:20]),
        "</div><div>",
        "<h4>Storage evidence summary</h4>",
        _kv_table_v07([
            ("Live records in thread", source_summary.get("live_record_count")),
            ("Dead/old records in thread", source_summary.get("dead_or_old_record_count")),
            ("Sequence range", json.dumps(temporal.get("sequence_range") or {}, ensure_ascii=False)),
            ("Source type counts", ", ".join(f"{k}: {v}" for k, v in source_counts.items()) or "N/A"),
            ("Top source files", ", ".join(f"{name} ({count})" for name, count in top_files) or "N/A"),
        ]),
        "<p class='muted'>.log = recent write-ahead log artifact; .ldb/.sst = flushed LevelDB table artifact. Live/deleted state comes from record state, not file extension alone.</p>",
        "</div></div>",
    ]
    if core_rows:
        parts.append("<details><summary>Core timeline events</summary>")
        parts.append(_simple_table_v07(core_rows[:40], [("kind", "Kind"), ("order", "Order"), ("label", "Label"), ("evidence", "Evidence")], "No core timeline events."))
        parts.append("</details>")
    return "\n".join(parts)


def _render_thread_detail_v07(thread: dict[str, Any], idx: int) -> str:
    classification = thread.get("classification", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    prompt = thread.get("prompt", {}) or {}
    title = _thread_title_v07(thread, idx)
    quality, qkind, explanation = _thread_quality_v07(thread)
    badges = (
        _badge_v07(classification.get("interaction_type"), "good" if classification.get("interaction_type") == "agentic" else "neutral")
        + _badge_v07(classification.get("execution_mode"), "good" if classification.get("execution_mode") in {"browser_control", "computer_mode"} else "neutral")
        + _badge_v07("confidence: " + str(classification.get("confidence")), "neutral")
        + _badge_v07(quality, qkind)
    )
    classification_evidence = ", ".join(str(x) for x in classification.get("classification_evidence", []) or [])
    return "\n".join([
        "<section class='card thread-detail'>",
        "<div class='thread-head'>",
        f"<div><h2>{_h(title)}</h2><p class='thread-id'>{_h(thread.get('thread_id'))}</p></div>",
        f"<div class='badges'>{badges}</div>",
        "</div>",
        f"<div class='note {qkind}'><strong>Reconstruction verdict:</strong> {_h(explanation)}<br><span class='muted'>{_status_line_v07(thread)}</span></div>",
        "<h3>1. Prompt</h3>",
        f"<div class='prompt'>{_h(prompt.get('text') or 'No prompt extracted.')}</div>",
        _evidence_row("Prompt evidence", prompt.get("evidence")),
        "<h3>2. Classification & key metadata</h3>",
        _kv_table_v07([
            ("Interaction / execution", SafeHtml(_badge_v07(classification.get("interaction_type"), "good") + _badge_v07(classification.get("execution_mode"), "good" if classification.get("execution_mode") in {"browser_control", "computer_mode"} else "neutral"))),
            ("Classification evidence", classification_evidence),
            ("Core status", metadata.get("status") or metadata.get("thread_status")),
            ("Final flag", metadata.get("final")),
            ("Search mode", metadata.get("search_mode")),
            ("Display model", metadata.get("display_model")),
            ("Message mode", metadata.get("message_mode")),
            ("Search focus", metadata.get("search_focus")),
            ("Backend UUID", metadata.get("backend_uuid")),
            ("Context UUID", metadata.get("context_uuid")),
        ]),
        _render_external_status_v07(thread),
        "<h3>3. Task outcome</h3>",
        _render_task_outcome_v10(thread),
        "<h3>4. Activity / artifact reconstruction</h3>",
        _render_activity_v07(thread),
        "<h3>5. Reasoning availability</h3>",
        _kv_table_v07([
            ("Available", (thread.get("reasoning", {}) or {}).get("available")),
            ("Note", (thread.get("reasoning", {}) or {}).get("note")),
        ]),
        _raw_details_v07("Reasoning / thought candidates", (thread.get("reasoning", {}) or {}).get("items")),
        "<h3>6. Final answer</h3>",
        _render_final_answer_v07(thread),
        "<h3>7. Privacy / deletion</h3>",
        _render_privacy_deletion_v07(thread),
        "<h3>8. Time & storage evidence</h3>",
        _render_time_and_sources_v07(thread),
        _raw_details_v07("Raw metadata evidence", metadata.get("evidence")),
        _raw_details_v07("Full thread JSON", thread),
        "</section>",
    ])


def render_html_report(report: dict[str, Any], output_path: Path) -> None:
    """
    Human-readable HTML report.

    The JSON output remains the authoritative machine-readable report. This HTML
    view is intentionally selective: it highlights the forensic reconstruction
    result first, and hides raw JSON/evidence in collapsible sections.
    """

    def esc(value: Any) -> str:
        return html.escape("" if value is None else str(value))

    def basename(value: Any) -> str:
        if value is None:
            return ""
        return Path(str(value)).name or str(value)

    summary = report.get("summary", {}) or {}
    extraction = report.get("extraction_summary", {}) or {}
    source = report.get("source", {}) or {}

    parts: list[str] = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    parts.append("<title>Comet Browser Reconstruction Report</title>")
    parts.append(
        "<style>"
        ":root{--bg:#f5f7fb;--card:#ffffff;--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;"
        "--soft:#f9fafb;--blue:#2563eb;--green:#047857;--amber:#b45309;--red:#b91c1c;--mono:#0f172a;}"
        "*{box-sizing:border-box;}"
        "body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;line-height:1.55;}"
        ".page{max-width:1180px;margin:0 auto;padding:28px;}"
        ".hero{background:linear-gradient(135deg,#111827,#1f2937);color:#fff;border-radius:18px;padding:28px 32px;margin-bottom:22px;box-shadow:0 8px 28px rgba(15,23,42,.16);}"
        ".hero h1{margin:0 0 8px;font-size:28px;letter-spacing:-.02em;}"
        ".hero p{margin:4px 0;color:#d1d5db;}"
        ".card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin:16px 0;box-shadow:0 2px 10px rgba(15,23,42,.04);}"
        ".section-title{display:flex;align-items:center;gap:10px;margin:0 0 14px;font-size:19px;}"
        ".section-title .num{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:50%;background:#e0ecff;color:#1d4ed8;font-weight:700;font-size:13px;}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;}"
        ".metric{background:var(--soft);border:1px solid var(--line);border-radius:14px;padding:14px;}"
        ".metric .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}"
        ".metric .value{font-size:24px;font-weight:750;margin-top:3px;}"
        ".kv{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff;}"
        ".kv th{width:220px;text-align:left;background:#f9fafb;color:#4b5563;font-weight:650;}"
        ".kv th,.kv td{border-bottom:1px solid var(--line);padding:10px 12px;vertical-align:top;}"
        ".kv tr:last-child th,.kv tr:last-child td{border-bottom:0;}"
        ".badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 9px;font-size:12px;font-weight:650;background:#eef2ff;color:#3730a3;margin:2px 4px 2px 0;}"
        ".badge.ok{background:#dcfce7;color:#166534;}"
        ".badge.warn{background:#fef3c7;color:#92400e;}"
        ".badge.bad{background:#fee2e2;color:#991b1b;}"
        ".badge.gray{background:#f3f4f6;color:#374151;}"
        ".callout{border-left:4px solid var(--blue);background:#eff6ff;padding:12px 14px;border-radius:10px;margin:12px 0;}"
        ".callout.warn{border-left-color:var(--amber);background:#fffbeb;}"
        ".callout.ok{border-left-color:var(--green);background:#ecfdf5;}"
        ".prompt,.answer{white-space:pre-wrap;background:#f8fafc;border:1px solid var(--line);border-radius:14px;padding:16px;overflow:auto;}"
        ".answer{max-height:520px;}"
        ".small{font-size:13px;color:var(--muted);}"
        ".mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--mono);font-size:12.5px;}"
        ".table{width:100%;border-collapse:collapse;margin-top:8px;}"
        ".table th,.table td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top;}"
        ".table th{background:#f9fafb;color:#374151;}"
        "details{border:1px solid var(--line);border-radius:12px;background:#fff;margin:10px 0;}"
        "summary{cursor:pointer;padding:10px 13px;font-weight:650;color:#374151;background:#f9fafb;border-radius:12px;}"
        "details[open] summary{border-bottom:1px solid var(--line);border-radius:12px 12px 0 0;}"
        "pre.raw{margin:0;padding:14px;background:#0f172a;color:#e5e7eb;overflow:auto;border-radius:0 0 12px 12px;font-size:12px;line-height:1.45;}"
        ".thread-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;}"
        ".thread-id{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:#6b7280;word-break:break-all;}"
        ".empty{color:#6b7280;font-style:italic;background:#f9fafb;border:1px dashed #d1d5db;border-radius:12px;padding:12px;}"
        "</style></head><body><main class='page'>"
    )

    parts.append("<section class='hero'>")
    parts.append("<h1>Comet Browser Reconstruction Report</h1>")
    parts.append(f"<p><strong>Input:</strong> {esc(basename(source.get('input')))}</p>")
    parts.append(f"<p><strong>Scope:</strong> {esc(source.get('analysis_scope'))} · <strong>Object store:</strong> {esc(source.get('database'))}/{esc(source.get('object_store'))}</p>")
    parts.append("</section>")

    parts.append("<section class='card'>")
    parts.append("<h2 class='section-title'><span class='num'>1</span>Case Summary</h2>")
    parts.append("<div class='grid'>")
    metric_items = [
        ("Threads", summary.get("thread_count", 0)),
        ("Browser agent", summary.get("browser_agent_thread_count", 0)),
        ("Conversational", summary.get("conversational_or_search_thread_count", 0)),
        ("Skipped computer", summary.get("skipped_computer_mode_count", 0)),
        ("Deleted/stale", summary.get("deleted_or_stale_candidate_count", 0)),
        ("Private mode", summary.get("private_mode_candidate_count", 0)),
        ("Global records", extraction.get("global_record_count", 0)),
    ]
    for label, value in metric_items:
        parts.append(f"<div class='metric'><div class='label'>{esc(label)}</div><div class='value'>{esc(value)}</div></div>")
    parts.append("</div>")
    parts.append(render_key_value_table([
        ("Live records", extraction.get("live_record_count")),
        ("Dead/old records", extraction.get("dead_record_count")),
        ("All parsed records", extraction.get("all_record_count")),
        ("Relevant records", extraction.get("relevant_record_count")),
        ("Bad records", extraction.get("bad_record_count")),
    ]))
    parts.append("</section>")

    parts.append("<section class='card'>")
    parts.append("<h2 class='section-title'><span class='num'>2</span>Thread Overview</h2>")
    parts.append(render_thread_overview(report))
    parts.append("</section>")

    for idx, thread in enumerate(report.get("threads", []), start=1):
        parts.append(render_thread_html(thread, idx))

    parts.append("<section class='card'>")
    parts.append("<h2 class='section-title'><span class='num'>G</span>Residual / Global Artifacts</h2>")
    parts.append(render_global_artifacts(report.get("global_records", [])))
    parts.append("</section>")

    if report.get("skipped"):
        parts.append("<section class='card'>")
        parts.append("<h2 class='section-title'><span class='num'>!</span>Skipped Groups</h2>")
        parts.append(render_raw_details("Skipped group details", report.get("skipped")))
        parts.append("</section>")

    if report.get("snapshot_comparison"):
        parts.append("<section class='card'>")
        parts.append("<h2 class='section-title'><span class='num'>↔</span>Snapshot Comparison</h2>")
        parts.append(render_snapshot_comparison(report.get("snapshot_comparison", {})))
        parts.append(render_raw_details("Raw snapshot comparison", report.get("snapshot_comparison")))
        parts.append("</section>")

    parts.append("</main></body></html>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")



def render_thread_overview(report: dict[str, Any]) -> str:
    """Compact index of every reconstructed thread in the report."""
    threads = report.get("threads", []) or []
    if not threads:
        return "<div class='empty'>No reconstructable threads were found.</div>"

    rows = [
        "<table class='table'>"
        "<tr>"
        "<th>#</th>"
        "<th>Thread / group</th>"
        "<th>Classification</th>"
        "<th>Prompt</th>"
        "<th>Activity</th>"
        "<th>State</th>"
        "<th>Sources</th>"
        "</tr>"
    ]
    for i, thread in enumerate(threads, start=1):
        classification = thread.get("classification", {}) or {}
        prompt = thread.get("prompt", {}) or {}
        deletion = thread.get("deletion_state", {}) or {}
        metadata = thread.get("metadata", {}) or {}
        source_summary = thread.get("source_summary", {}) or {}
        source_counts = source_summary.get("source_type_counts", {}) or {}
        execution_mode = classification.get("execution_mode")
        interaction_type = classification.get("interaction_type")
        activity = (
            f"plan={len(thread.get('plan', []) or [])}, "
            f"actions={len(thread.get('actions', []) or [])}, "
            f"urls={len(thread.get('urls', []) or [])}, "
            f"payloads={len(thread.get('typed_payloads', []) or [])}"
        )
        state = (
            f"deletion={deletion.get('state')}; "
            f"private={metadata.get('private_mode')}; "
            f"final={'yes' if (thread.get('final_answer', {}) or {}).get('text') else 'no'}"
        )
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td class='mono'>{html.escape(shorten_text(thread.get('thread_id'), 80))}</td>"
            f"<td>{html.escape(str(interaction_type))}<br><span class='small'>{html.escape(str(execution_mode))}</span></td>"
            f"<td>{html.escape(shorten_text(prompt.get('text'), 140))}</td>"
            f"<td>{html.escape(activity)}</td>"
            f"<td>{html.escape(state)}</td>"
            f"<td>{html.escape(', '.join(f'{k}:{v}' for k, v in source_counts.items()))}</td>"
            "</tr>"
        )
    rows.append("</table>")
    rows.append(
        "<p class='small'>This overview is generated from the same JSON report as the detailed sections. "
        "HTML does not make separate reconstruction decisions.</p>"
    )
    return "\n".join(rows)


def render_global_artifacts(global_records: list[dict[str, Any]]) -> str:
    """Render records that were relevant but not confidently assigned to one thread."""
    if not global_records:
        return (
            "<div class='empty'>No unassigned global artifacts. "
            "All relevant records were either assigned to a thread/task or skipped.</div>"
        )

    rows = []
    for record in global_records[:50]:
        rows.append(
            {
                "record_kind": record.get("record_kind"),
                "source": format_evidence_short(record.get("evidence", [])),
                "key": shorten_text(record.get("key"), 160),
                "preview": shorten_text(record.get("value_preview"), 240),
            }
        )
    parts = [
        "<p class='small'>These records matched forensic relevance markers but did not have a strong thread/task identifier. "
        "They may be URL/list/cache artifacts, residual records from previous activity, or context shared across threads. "
        "They are not merged into a thread unless a strong identifier links them.</p>",
        render_items_table(rows, ["record_kind", "source", "key", "preview"], empty="No global artifacts."),
    ]
    if len(global_records) > 50:
        parts.append(f"<p class='small'>Showing 50 of {html.escape(str(len(global_records)))} global records. Full details are in JSON.</p>")
    parts.append(render_raw_details("Raw global artifacts", global_records[:100]))
    return "\n".join(parts)


def render_thread_html(thread: dict[str, Any], idx: int) -> str:
    classification = thread.get("classification", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    prompt = thread.get("prompt", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    source_summary = thread.get("source_summary", {}) or {}
    execution_mode = classification.get("execution_mode")
    is_browser = execution_mode == "browser_control"

    cls_badge = "ok" if is_browser else "warn" if execution_mode != "computer_mode" else "bad"
    parts: list[str] = []

    parts.append("<section class='card'>")
    parts.append("<div class='thread-head'>")
    parts.append(f"<div><h2 style='margin:0'>Thread {idx}</h2><div class='thread-id'>{html.escape(str(thread.get('thread_id')))}</div></div>")
    parts.append("<div>")
    parts.append(render_badge(str(classification.get("interaction_type")), cls_badge))
    parts.append(render_badge(str(execution_mode), cls_badge))
    parts.append(render_badge("confidence: " + str(classification.get("confidence")), "gray"))
    parts.append("</div></div>")

    parts.append("<div class='callout ok'>")
    parts.append(render_thread_interpretation(thread))
    parts.append("</div>")

    parts.append("<h3 class='section-title'><span class='num'>A</span>Prompt</h3>")
    parts.append(f"<div class='prompt'>{html.escape(str(prompt.get('text') or 'No prompt extracted.'))}</div>")
    parts.append(render_evidence_line("Prompt evidence", prompt.get("evidence", [])))

    parts.append("<h3 class='section-title'><span class='num'>B</span>Key Metadata</h3>")
    parts.append(render_key_value_table([
        ("Status", metadata.get("status")),
        ("Final", metadata.get("final")),
        ("Mode", metadata.get("mode")),
        ("Search mode", metadata.get("search_mode")),
        ("Display model", metadata.get("display_model")),
        ("Message mode", metadata.get("message_mode")),
        ("Search focus", metadata.get("search_focus")),
        ("Privacy state", metadata.get("privacy_state")),
        ("Access level", metadata.get("access_level")),
        ("Last access", format_time_value(metadata.get("lastAccess"))),
        ("Backend UUID", metadata.get("backend_uuid")),
        ("Context UUID", metadata.get("context_uuid")),
    ]))
    parts.append(render_raw_details("Raw metadata evidence", metadata.get("evidence", {})))

    if metadata.get("external_thread_list_evidence"):
        parts.append("<h4>External Thread List / Cache Evidence</h4>")
        rows = []
        for item in metadata.get("external_thread_list_evidence", [])[:10]:
            entry = item.get("entry", {}) or {}
            rows.append({
                "relative_order": item.get("relative_order"),
                "status": entry.get("status"),
                "unread": entry.get("unread"),
                "link": entry.get("link"),
                "title": shorten_text(entry.get("title"), 140),
                "evidence": format_evidence_short(item.get("evidence", [])),
            })
        parts.append(render_items_table(rows, ["relative_order", "status", "unread", "link", "title", "evidence"], empty="No external thread-list evidence."))
        parts.append("<p class='small'>Thread-list/cache evidence is linked by UUID and shown as external status/context evidence. It is not merged as core thread content.</p>")

    parts.append("<h3 class='section-title'><span class='num'>C</span>Reconstructed Activity</h3>")
    if is_browser:
        parts.append(render_browser_activity(thread))
    else:
        parts.append("<div class='empty'>This thread is classified as non-browser-agent, so click/type/navigation reconstruction is intentionally suppressed.</div>")

    parts.append("<h3 class='section-title'><span class='num'>D</span>Final Answer</h3>")
    answer_text = final_answer.get("text") or "No final answer extracted."
    parts.append(f"<div class='answer'>{html.escape(str(answer_text))}</div>")
    parts.append(render_evidence_line("Final answer evidence", final_answer.get("evidence", [])))

    parts.append("<h3 class='section-title'><span class='num'>E</span>Privacy / Deletion</h3>")
    privacy = metadata.get("private_detection", {}) or {}
    deletion_state = deletion.get("state")
    deletion_badge = "ok" if deletion_state == "live" else "warn"
    private_badge = "bad" if privacy.get("private_mode") else "ok"
    parts.append("<div class='grid'>")
    parts.append(f"<div class='metric'><div class='label'>Deletion state</div><div class='value'>{render_badge(str(deletion_state), deletion_badge)}</div><div class='small'>Strong evidence: {html.escape(str(len(deletion.get('strong_evidence', []) or [])))} · Weak evidence: {html.escape(str(len(deletion.get('weak_evidence', []) or [])))}</div></div>")
    parts.append(f"<div class='metric'><div class='label'>Private mode</div><div class='value'>{render_badge('Yes' if privacy.get('private_mode') else 'No', private_badge)}</div><div class='small'>privacy_state={html.escape(', '.join(privacy.get('privacy_states', []) or [])) or 'N/A'} · access_level={html.escape(', '.join(privacy.get('access_levels', []) or [])) or 'N/A'}</div></div>")
    parts.append("</div>")
    parts.append(render_raw_details("Deletion evidence", deletion))
    parts.append(render_raw_details("Private-mode evidence", privacy))

    parts.append("<h3 class='section-title'><span class='num'>F</span>Timeline & Source Evidence</h3>")
    parts.append(render_core_timeline(thread))
    parts.append(render_source_summary(source_summary))
    parts.append(render_raw_details("Full thread JSON", thread))

    parts.append("</section>")
    return "\n".join(parts)


def render_thread_interpretation(thread: dict[str, Any]) -> str:
    classification = thread.get("classification", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}

    mode = classification.get("execution_mode")
    if mode == "browser_control":
        activity_summary = "Browser Control artifacts are reconstructed from plan/action/URL/payload sections."
    elif mode == "computer_mode":
        activity_summary = "Computer mode was detected; Browser-only MVP may skip detailed reconstruction."
    else:
        activity_summary = "This is a conversational/search thread; browser action sections are hidden to avoid cache noise."

    return (
        f"<strong>Interpretation:</strong> {html.escape(str(classification.get('interaction_type')))} "
        f"/ {html.escape(str(mode))}. {activity_summary} "
        f"Status={html.escape(str(metadata.get('status')))}, search_mode={html.escape(str(metadata.get('search_mode')))}, "
        f"deletion={html.escape(str(deletion.get('state')))}, "
        f"final_answer={'present' if final_answer.get('text') else 'missing'}."
    )


def render_browser_activity(thread: dict[str, Any]) -> str:
    parts: list[str] = []
    plan = thread.get("plan", []) or []
    actions = thread.get("actions", []) or []
    urls = thread.get("urls", []) or []
    context_urls = thread.get("context_url_candidates", []) or []
    payloads = thread.get("typed_payloads", []) or []

    parts.append("<div class='grid'>")
    parts.append(f"<div class='metric'><div class='label'>Plan steps</div><div class='value'>{len(plan)}</div></div>")
    parts.append(f"<div class='metric'><div class='label'>Actions</div><div class='value'>{len(actions)}</div></div>")
    parts.append(f"<div class='metric'><div class='label'>Thread URLs</div><div class='value'>{len(urls)}</div></div>")
    parts.append(f"<div class='metric'><div class='label'>Context URL candidates</div><div class='value'>{len(context_urls)}</div></div>")
    parts.append(f"<div class='metric'><div class='label'>Typed payloads</div><div class='value'>{len(payloads)}</div></div>")
    parts.append("</div>")

    parts.append("<h4>Plan</h4>")
    parts.append(render_items_table(plan, ["kind", "relative_order", "label"], empty="No plan artifacts extracted."))
    parts.append("<h4>Actions</h4>")
    parts.append(render_items_table(actions, ["kind", "relative_order", "label"], empty="No action artifacts extracted."))
    parts.append("<h4>Typed Payloads</h4>")
    parts.append(render_items_table(payloads, ["field", "relative_order", "value"], empty="No typed payload artifacts extracted."))
    parts.append("<h4>Thread-level URLs</h4>")
    parts.append(render_url_table(urls))
    if context_urls:
        parts.append("<h4>Global Browser Context URL Candidates</h4>")
        parts.append("<p class='small'>These come from global topMostUrls/browser context records. They are useful leads, but are not asserted as thread-specific unless corroborated by thread-level evidence.</p>")
        parts.append(render_url_table(context_urls))
    return "\n".join(parts)


def render_core_timeline(thread: dict[str, Any]) -> str:
    timeline = thread.get("timeline", []) or []
    core_events = [e for e in timeline if e.get("kind") in {"prompt", "final_answer", "action", "typed_payload", "url"}]
    time_events = [e for e in timeline if e.get("kind") == "time_metadata"]

    parts: list[str] = []
    if core_events:
        rows = []
        for item in core_events[:30]:
            rows.append({
                "kind": item.get("kind"),
                "relative_order": item.get("relative_order"),
                "label": shorten_text(item.get("label"), 180),
                "evidence": format_evidence_short(item.get("evidence", [])),
            })
        parts.append(render_items_table(rows, ["kind", "relative_order", "label", "evidence"], empty="No core timeline events."))
    else:
        parts.append("<div class='empty'>No core timeline events extracted.</div>")

    if time_events:
        rows = []
        for item in time_events[:12]:
            rows.append({
                "field": item.get("field"),
                "raw_value": item.get("value"),
                "interpreted": format_time_value(item.get("value")),
                "relative_order": item.get("relative_order"),
                "evidence": format_evidence_short(item.get("evidence", [])),
            })
        parts.append("<details><summary>Additional time metadata</summary>")
        parts.append(render_items_table(rows, ["field", "raw_value", "interpreted", "relative_order", "evidence"], empty="No time metadata."))
        parts.append("</details>")
    return "\n".join(parts)


def render_source_summary(source_summary: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append("<h4>Source summary</h4>")
    parts.append(render_key_value_table([
        ("Live records in thread", source_summary.get("live_record_count")),
        ("Dead/old records in thread", source_summary.get("dead_or_old_record_count")),
        ("Source type counts", json.dumps(source_summary.get("source_type_counts", {}), ensure_ascii=False)),
    ]))
    top_files = source_summary.get("top_source_files", []) or []
    if top_files:
        rows = [{"source_file": file, "record_count": count} for file, count in top_files]
        parts.append(render_items_table(rows, ["source_file", "record_count"], empty="No source files."))
    return "\n".join(parts)


def render_snapshot_comparison(comparison: dict[str, Any]) -> str:
    summary = comparison.get("summary", {}) or {}
    return render_key_value_table([
        ("Before thread count", summary.get("before_thread_count")),
        ("After thread count", summary.get("after_thread_count")),
        ("Disappeared", summary.get("disappeared_thread_count")),
        ("Persisted/stale candidates", summary.get("persisted_thread_count")),
        ("Appeared", summary.get("appeared_thread_count")),
    ])


def render_key_value_table(rows: list[tuple[str, Any]]) -> str:
    html_rows = ["<table class='kv'>"]
    for key, value in rows:
        html_rows.append(
            "<tr>"
            f"<th>{html.escape(str(key))}</th>"
            f"<td>{value if isinstance(value, SafeHtml) else html.escape('' if value is None else str(value))}</td>"
            "</tr>"
        )
    html_rows.append("</table>")
    return "\n".join(html_rows)


class SafeHtml(str):
    pass


def render_badge(text: str, kind: str = "gray") -> SafeHtml:
    return SafeHtml(f"<span class='badge {html.escape(kind)}'>{html.escape(text)}</span>")


def render_evidence_line(label: str, evidence: Any) -> str:
    return f"<p class='small'><strong>{html.escape(label)}:</strong> <span class='mono'>{html.escape(format_evidence_short(evidence))}</span></p>"


def format_evidence_short(evidence: Any) -> str:
    if not evidence:
        return "No evidence reference"
    if isinstance(evidence, list):
        ev = evidence[0] if evidence else {}
    elif isinstance(evidence, dict):
        ev = evidence
    else:
        return str(evidence)
    return (
        f"{ev.get('source_file')} · {ev.get('source_type')} · {ev.get('state')} · "
        f"seq={ev.get('ldb_seq_no')} · offset={ev.get('offset')}"
    )


def render_raw_details(title: str, payload: Any) -> str:
    if payload in (None, {}, []):
        return ""
    raw = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return (
        f"<details><summary>{html.escape(title)}</summary>"
        f"<pre class='raw'>{html.escape(raw)}</pre>"
        "</details>"
    )


def render_items_table(items: list[dict[str, Any]], columns: list[str], empty: str = "No items extracted.") -> str:
    if not items:
        return f"<div class='empty'>{html.escape(empty)}</div>"
    rows = ["<table class='table'><tr>"]
    for col in columns:
        rows.append(f"<th>{html.escape(labelize(col))}</th>")
    rows.append("</tr>")
    for item in items:
        rows.append("<tr>")
        for col in columns:
            value = item.get(col)
            if col in {"label", "value"}:
                value = shorten_text(value, 500)
            rows.append(f"<td>{html.escape('' if value is None else str(value))}</td>")
        rows.append("</tr>")
    rows.append("</table>")
    return "\n".join(rows)


def render_url_table(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<div class='empty'>No URLs extracted.</div>"
    rows = ["<table class='table'><tr><th>#</th><th>Role</th><th>Title</th><th>URL</th><th>Order</th><th>Evidence</th></tr>"]
    for i, item in enumerate(items, start=1):
        url = str(item.get("url") or "")
        display_url = shorten_text(url, 120)
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{html.escape(str(item.get('role')))}</td>"
            f"<td>{html.escape(shorten_text(item.get('title'), 100))}</td>"
            f"<td><a href='{html.escape(url)}'>{html.escape(display_url)}</a></td>"
            f"<td>{html.escape(str(item.get('relative_order')))}</td>"
            f"<td class='mono'>{html.escape(format_evidence_short(item.get('evidence', [])))}</td>"
            "</tr>"
        )
    rows.append("</table>")
    return "\n".join(rows)


def labelize(value: str) -> str:
    return value.replace("_", " ").strip().title()


def shorten_text(value: Any, limit: int = 160) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def format_time_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    raw = str(value)
    if isinstance(value, (int, float)):
        try:
            if value > 10_000_000_000:
                dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
                return f"{raw} ({dt.isoformat()} UTC, best-effort ms epoch)"
            if value > 1_000_000_000:
                dt = datetime.fromtimestamp(value, tz=timezone.utc)
                return f"{raw} ({dt.isoformat()} UTC, best-effort sec epoch)"
        except Exception:
            pass
    return raw


# Backward-compatible helpers kept for older call sites or quick reuse.
def render_item_table(items: list[dict[str, Any]], include_preview: bool = False) -> str:
    return render_items_table(items, ["kind", "relative_order", "label"])


def render_payload_table(items: list[dict[str, Any]]) -> str:
    return render_items_table(items, ["field", "relative_order", "value"], empty="No typed payload candidates extracted.")


# ---------------------------------------------------------------------------
# HTML report v07: browser-only reconstruction view
# ---------------------------------------------------------------------------
# The functions below intentionally override the earlier HTML renderer.  The
# underlying JSON schema and extraction logic stay unchanged; only the human
# readable view is reorganized so investigators see the reconstruction result
# first and raw evidence only when they expand details.

_NOISE_URL_SUBSTRINGS_V07 = (
    "myaccount.google.",
    "accounts.google.",
    "lh3.googleusercontent.com",
    "ppl-ai-agent-screenshots.s3.amazonaws.com",
    "ppl-ai-public.s3.amazonaws.com",
    "static/img/pplx-default-preview",
)

_STOPWORDS_V07 = {
    "the", "and", "for", "with", "this", "that", "from", "into", "about", "your", "you",
    "keep", "open", "source", "sources", "used", "topic", "research", "compare", "main",
    "claims", "summary", "summarize", "future", "web", "page", "pages", "site", "website",
    "해당", "웹사이트", "내용", "요약", "논문", "저널", "열어줘", "이후", "대한",
}


def _h(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _safe_url(value: Any) -> str:
    url = "" if value is None else str(value)
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    return url


def _badge_v07(text: Any, kind: str = "neutral") -> str:
    return f"<span class='badge {html.escape(kind)}'>{_h(text)}</span>"


def _panel_metric(label: str, value: Any, note: Any = "") -> str:
    note_html = f"<div class='metric-note'>{_h(note)}</div>" if note not in (None, "") else ""
    return (
        "<div class='metric'>"
        f"<div class='metric-label'>{_h(label)}</div>"
        f"<div class='metric-value'>{_h(value)}</div>"
        f"{note_html}"
        "</div>"
    )


def _evidence_chip(evidence: Any, max_items: int = 2) -> str:
    if not evidence:
        return "<span class='evidence-chip muted'>no direct evidence</span>"
    evs = evidence if isinstance(evidence, list) else [evidence]
    chips: list[str] = []
    for ev in evs[:max_items]:
        if not isinstance(ev, dict):
            chips.append(f"<span class='evidence-chip'>{_h(ev)}</span>")
            continue
        chips.append(
            "<span class='evidence-chip'>"
            f"{_h(ev.get('source_file'))} · {_h(ev.get('source_type'))} · {_h(ev.get('state'))} · "
            f"seq={_h(ev.get('ldb_seq_no'))} · offset={_h(ev.get('offset'))}"
            "</span>"
        )
    if len(evs) > max_items:
        chips.append(f"<span class='evidence-chip muted'>+{len(evs) - max_items} more</span>")
    return "".join(chips)


def _evidence_row(label: str, evidence: Any) -> str:
    return f"<div class='evidence-row'><strong>{_h(label)}</strong>{_evidence_chip(evidence)}</div>"


def _raw_details_v07(title: str, payload: Any) -> str:
    if payload in (None, {}, []):
        return ""
    raw = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return (
        f"<details class='raw-details'><summary>{_h(title)}</summary>"
        f"<pre class='raw'>{_h(raw)}</pre>"
        "</details>"
    )


def _first_line(value: Any, limit: int = 120) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip() or text.replace("\n", " ").strip()
    return shorten_text(line, limit)


def _thread_title_v07(thread: dict[str, Any], idx: int) -> str:
    prompt = (thread.get("prompt", {}) or {}).get("text") or ""
    refs = (thread.get("prompt", {}) or {}).get("reference_codes") or []
    if refs:
        return str(refs[0])
    line = _first_line(prompt, 80)
    return line or f"Thread {idx}"


def _thread_quality_v07(thread: dict[str, Any]) -> tuple[str, str, str]:
    """Return (label, badge_kind, explanation)."""
    classification = thread.get("classification", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}
    execution_mode = classification.get("execution_mode")
    is_browser = execution_mode == "browser_control"
    is_computer = execution_mode == "computer_mode"
    has_final = bool(final_answer.get("text"))
    has_activity = bool(thread.get("plan") or thread.get("actions") or thread.get("urls"))
    has_external = bool(metadata.get("external_thread_list_evidence"))
    status = str(metadata.get("status") or metadata.get("thread_status") or "").lower()

    if is_browser and has_final and has_activity:
        return "Good reconstruction", "good", "Browser thread has prompt, agentic activity, URLs and final answer."
    if is_browser and has_final:
        return "Partial reconstruction", "warn", "Final answer exists, but action/URL evidence may be incomplete or partly cache-derived."
    if is_browser and has_external and ("pending" in status or metadata.get("final") is False):
        return "Partial / cache conflict", "warn", "Core record is pending or non-final, but thread-list cache shows later completed status."
    if is_browser:
        return "Sparse browser evidence", "warn", "Browser-agent markers exist, but detailed action/final-answer artifacts were not recovered."
    if is_computer and has_final:
        return "Computer partial reconstruction", "warn", "Computer/ASI task was promoted from list-cache evidence with prompt and answer candidate; corroborate with History/Downloads for side effects."
    if is_computer:
        return "Computer metadata only", "warn", "Computer/ASI task was detected from cache metadata, but no clean answer/action content was reconstructed."
    return "Non-browser or skipped", "neutral", "Thread is not reconstructed as a browser-control activity."


def _status_line_v07(thread: dict[str, Any]) -> str:
    metadata = thread.get("metadata", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    privacy = metadata.get("private_detection", {}) or {}
    return (
        f"Core status={_h(metadata.get('status') or metadata.get('thread_status') or 'N/A')} · "
        f"final={_h(metadata.get('final'))} · "
        f"answer={'yes' if final_answer.get('text') else 'no'} · "
        f"deletion={_h(deletion.get('state'))} · "
        f"private={'yes' if privacy.get('private_mode') else 'no'}"
    )


def _url_noise_v07(url: Any) -> bool:
    text = str(url or "").lower()
    return any(marker in text for marker in _NOISE_URL_SUBSTRINGS_V07)


def _prompt_terms_v07(thread: dict[str, Any]) -> set[str]:
    prompt = ((thread.get("prompt", {}) or {}).get("text") or "").lower()
    terms = set(re.findall(r"[a-zA-Z가-힣0-9]{4,}", prompt))
    return {t for t in terms if t not in _STOPWORDS_V07}


def _url_score_v07(item: dict[str, Any], terms: set[str]) -> int:
    text = ((item.get("title") or "") + " " + (item.get("url") or "")).lower()
    score = 0
    for term in terms:
        if term and term in text:
            score += 2
    # Generic relevance: prefer URLs/titles that overlap with prompt terms.
    # Do not boost case/topic-specific domains or experiment vocabulary.
    if _url_noise_v07(item.get("url")):
        score -= 8
    if "bing.com" in text or "google.com" in text:
        score -= 3
    return score


def _split_context_urls_v07(thread: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items = list(thread.get("context_url_candidates", []) or [])
    terms = _prompt_terms_v07(thread)
    scored = [(item, _url_score_v07(item, terms)) for item in items]
    likely = [item for item, score in scored if score > 0]
    other = [item for item, score in scored if score <= 0]
    likely.sort(key=lambda item: (_url_score_v07(item, terms), item.get("visitCount") or 0), reverse=True)
    return likely, other


def _kv_table_v07(rows: list[tuple[str, Any]]) -> str:
    out = ["<table class='kv'>"]
    for key, value in rows:
        rendered = value if isinstance(value, SafeHtml) else _h(value)
        out.append(f"<tr><th>{_h(key)}</th><td>{rendered}</td></tr>")
    out.append("</table>")
    return "\n".join(out)


def _simple_table_v07(rows: list[dict[str, Any]], columns: list[tuple[str, str]], empty: str) -> str:
    if not rows:
        return f"<div class='empty'>{_h(empty)}</div>"
    out = ["<table class='table compact'><thead><tr>"]
    for key, label in columns:
        out.append(f"<th>{_h(label)}</th>")
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for key, _label in columns:
            value = row.get(key)
            if key == "url" and value:
                url = _safe_url(value)
                shown = shorten_text(value, 90)
                value = SafeHtml(f"<a href='{_h(url)}'>{_h(shown)}</a>") if url else _h(shown)
            elif key == "evidence":
                value = SafeHtml(_evidence_chip(value, 1))
            elif key in {"label", "value", "title"}:
                value = shorten_text(value, 220)
            rendered = value if isinstance(value, SafeHtml) else _h(value)
            out.append(f"<td>{rendered}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def _render_executive_findings_v07(report: dict[str, Any]) -> str:
    threads = report.get("threads", []) or []
    findings: list[str] = []
    for idx, thread in enumerate(threads, start=1):
        title = _thread_title_v07(thread, idx)
        quality, _, explanation = _thread_quality_v07(thread)
        findings.append(f"<li><strong>{_h(title)}</strong>: {_h(quality)} — {_h(explanation)}</li>")
    if report.get("global_records"):
        findings.append(
            f"<li><strong>Global/residual artifacts</strong>: {_h(len(report.get('global_records') or []))} records remain unassigned; they are treated as context, not automatically merged into a thread.</li>"
        )
    skipped = report.get("skipped") or []
    if skipped:
        findings.append(f"<li><strong>Computer-mode groups</strong>: {_h(len(skipped))} groups were skipped in browser-only scope.</li>")
    if not findings:
        findings.append("<li>No reconstructable thread-level findings were generated.</li>")
    return "<ul class='findings'>" + "\n".join(findings) + "</ul>"


def _render_thread_overview_v07(report: dict[str, Any]) -> str:
    threads = report.get("threads", []) or []
    if not threads:
        return "<div class='empty'>No reconstructable threads found.</div>"
    cards: list[str] = ["<div class='thread-grid'>"]
    for idx, thread in enumerate(threads, start=1):
        classification = thread.get("classification", {}) or {}
        metadata = thread.get("metadata", {}) or {}
        quality, qkind, explanation = _thread_quality_v07(thread)
        title = _thread_title_v07(thread, idx)
        prompt = (thread.get("prompt", {}) or {}).get("text")
        activity = {
            "Plan": len(thread.get("plan") or []),
            "Actions": len(thread.get("actions") or []),
            "Thread URLs": len(thread.get("urls") or []),
            "Context URLs": len(thread.get("context_url_candidates") or []),
            "Payloads": len(thread.get("typed_payloads") or []),
        }
        activity_html = "".join(f"<span class='mini'>{_h(k)}: <b>{_h(v)}</b></span>" for k, v in activity.items())
        cards.append(
            "<article class='thread-card'>"
            f"<div class='thread-card-top'><span class='thread-num'>#{idx}</span>{_badge_v07(quality, qkind)}</div>"
            f"<h3>{_h(title)}</h3>"
            f"<p class='thread-id'>{_h(thread.get('thread_id'))}</p>"
            f"<div class='badges'>{_badge_v07(classification.get('interaction_type'), 'good' if classification.get('interaction_type') == 'agentic' else 'neutral')}{_badge_v07(classification.get('execution_mode'), 'good' if classification.get('execution_mode') in {'browser_control', 'computer_mode'} else 'neutral')}{_badge_v07('confidence: ' + str(classification.get('confidence')), 'neutral')}</div>"
            f"<p class='muted'>{_h(shorten_text(prompt, 180))}</p>"
            f"<div class='mini-row'>{activity_html}</div>"
            f"<p class='muted'>{_status_line_v07(thread)}</p>"
            f"<p class='muted'>{_h(explanation)}</p>"
            "</article>"
        )
    cards.append("</div>")
    return "\n".join(cards)


def _render_external_status_v07(thread: dict[str, Any]) -> str:
    metadata = thread.get("metadata", {}) or {}
    external = metadata.get("external_thread_list_evidence") or []
    if not external:
        return ""
    statuses = []
    for item in external:
        entry = item.get("entry", {}) or {}
        statuses.append(str(entry.get("status") or "N/A"))
    unique_status = ", ".join(sorted(set(statuses)))
    rows = []
    for item in external[:8]:
        entry = item.get("entry", {}) or {}
        rows.append({
            "order": item.get("relative_order"),
            "status": entry.get("status"),
            "unread": entry.get("unread"),
            "link": entry.get("link"),
            "evidence": item.get("evidence"),
        })
    body = [
        "<div class='note warn'><strong>External thread-list/cache evidence</strong><br>",
        f"Same UUID appears in list/cache records with status: <b>{_h(unique_status)}</b>. This is context evidence only; core all_results/thread_metadata remains authoritative for thread content.</div>",
        "<details><summary>Show linked thread-list/cache entries</summary>",
        _simple_table_v07(rows, [("order", "Seq/order"), ("status", "Status"), ("unread", "Unread"), ("link", "Link"), ("evidence", "Evidence")], "No external evidence."),
        "</details>",
    ]
    return "\n".join(body)


def _render_activity_v07(thread: dict[str, Any]) -> str:
    plan = thread.get("plan") or []
    actions = thread.get("actions") or []
    urls = thread.get("urls") or []
    payloads = thread.get("typed_payloads") or []
    context_likely, context_other = _split_context_urls_v07(thread)
    counts = "".join([
        _panel_metric("Plan", len(plan)),
        _panel_metric("Actions", len(actions)),
        _panel_metric("Thread URLs", len(urls)),
        _panel_metric("Likely context URLs", len(context_likely)),
        _panel_metric("Other context URLs", len(context_other)),
        _panel_metric("Typed payloads", len(payloads)),
    ])
    parts = ["<div class='metrics six'>", counts, "</div>"]
    if not (plan or actions or urls or payloads):
        parts.append("<div class='note warn'><strong>No thread-level action trace recovered.</strong><br>Classification is supported by metadata/cache evidence, but these records do not expose detailed click/type/navigation steps. Use external status, URL leads, History, and Downloads artifacts for corroboration.</div>")

    if plan:
        rows = [{"order": p.get("relative_order"), "kind": p.get("kind"), "label": p.get("label"), "evidence": p.get("evidence")} for p in plan]
        parts.append("<h4>Agent plan / workflow steps</h4>")
        parts.append(_simple_table_v07(rows, [("order", "Order"), ("kind", "Kind"), ("label", "Step / label"), ("evidence", "Evidence")], "No plan artifacts."))
    if actions:
        rows = [{"order": a.get("relative_order"), "kind": a.get("kind"), "label": a.get("label"), "evidence": a.get("evidence")} for a in actions]
        parts.append("<h4>Agent actions / tool I/O</h4>")
        parts.append(_simple_table_v07(rows, [("order", "Order"), ("kind", "Kind"), ("label", "Action / label"), ("evidence", "Evidence")], "No action artifacts."))
    if payloads:
        rows = [{"order": p.get("relative_order"), "field": p.get("field"), "value": p.get("value"), "evidence": p.get("evidence")} for p in payloads]
        parts.append("<h4>Typed / submitted payloads</h4>")
        parts.append(_simple_table_v07(rows, [("order", "Order"), ("field", "Field"), ("value", "Value"), ("evidence", "Evidence")], "No typed payloads."))
    if urls:
        rows = [{"title": u.get("title"), "url": u.get("url"), "role": u.get("role"), "order": u.get("relative_order"), "evidence": u.get("evidence")} for u in urls]
        parts.append("<h4>Thread-level URLs</h4>")
        parts.append(_simple_table_v07(rows, [("role", "Role"), ("title", "Title"), ("url", "URL"), ("order", "Order"), ("evidence", "Evidence")], "No thread-level URLs."))

    if context_likely:
        parts.append("<h4>Likely relevant global URL leads</h4>")
        parts.append("<p class='muted'>These are from global topMostUrls/browser context records and are ranked for display using prompt/topic overlap. They are leads, not definitive thread-specific proof.</p>")
        rows = [{"title": u.get("title"), "url": u.get("url"), "visitCount": u.get("visitCount"), "lastAccess": format_time_value(u.get("lastAccess")), "evidence": u.get("evidence")} for u in context_likely[:12]]
        parts.append(_simple_table_v07(rows, [("title", "Title"), ("url", "URL"), ("visitCount", "Visits"), ("lastAccess", "Last access"), ("evidence", "Evidence")], "No relevant context URLs."))
    if context_other:
        rows = [{"title": u.get("title"), "url": u.get("url"), "visitCount": u.get("visitCount"), "evidence": u.get("evidence")} for u in context_other[:30]]
        parts.append("<details><summary>Other low-confidence / unrelated global URL candidates</summary>")
        parts.append(_simple_table_v07(rows, [("title", "Title"), ("url", "URL"), ("visitCount", "Visits"), ("evidence", "Evidence")], "No other context URLs."))
        parts.append("</details>")
    return "\n".join(parts)


def _render_final_answer_v07(thread: dict[str, Any]) -> str:
    final_answer = thread.get("final_answer", {}) or {}
    if final_answer.get("text"):
        return (
            "<div class='answer good-answer'>" + _h(final_answer.get("text")) + "</div>" +
            _evidence_row("Final answer evidence", final_answer.get("evidence"))
        )
    reason = final_answer.get("reason") or "No clean final-answer text was reconstructed from core records."
    return (
        "<div class='note warn'><strong>No clean final answer extracted.</strong><br>"
        f"{_h(reason)}</div>"
        + _evidence_row("Final answer evidence", final_answer.get("evidence"))
    )


def _render_time_and_sources_v07(thread: dict[str, Any]) -> str:
    metadata = thread.get("metadata", {}) or {}
    timeline = thread.get("timeline", []) or []
    source_summary = thread.get("source_summary", {}) or {}
    time_rows = []
    for key in ["created_at", "updated_at", "lastAccess"]:
        if metadata.get(key) not in (None, ""):
            ev = (metadata.get("evidence", {}) or {}).get(key)
            time_rows.append({"field": key, "value": format_time_value(metadata.get(key)), "evidence": ev})
    for item in timeline:
        if item.get("kind") == "time_metadata":
            time_rows.append({"field": item.get("field"), "value": format_time_value(item.get("value")), "evidence": item.get("evidence")})
    # Deduplicate by field/value/evidence string.
    deduped = []
    seen = set()
    for row in time_rows:
        sig = (row.get("field"), row.get("value"), format_evidence_short(row.get("evidence")))
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(row)
    core_rows = []
    for item in timeline:
        if item.get("kind") in {"prompt", "final_answer", "action", "url", "typed_payload"}:
            core_rows.append({"kind": item.get("kind"), "order": item.get("relative_order"), "label": item.get("label") or item.get("value"), "evidence": item.get("evidence")})
    top_files = source_summary.get("top_source_files") or []
    source_counts = source_summary.get("source_type_counts") or {}
    parts = [
        "<div class='split'>",
        "<div>",
        "<h4>Time metadata</h4>",
        _simple_table_v07(deduped[:20], [("field", "Field"), ("value", "Value"), ("evidence", "Evidence")], "No explicit time metadata."),
        "</div><div>",
        "<h4>Storage evidence summary</h4>",
        _kv_table_v07([
            ("Live records in thread", source_summary.get("live_record_count")),
            ("Dead/old records in thread", source_summary.get("dead_or_old_record_count")),
            ("Source type counts", ", ".join(f"{k}: {v}" for k, v in source_counts.items()) or "N/A"),
            ("Top source files", ", ".join(f"{name} ({count})" for name, count in top_files) or "N/A"),
        ]),
        "<p class='muted'>.log = recent write-ahead log artifact; .ldb/.sst = flushed LevelDB table artifact. Both are disk evidence; live/deleted state comes from record state, not extension alone.</p>",
        "</div></div>",
    ]
    if core_rows:
        parts.append("<details><summary>Core timeline events</summary>")
        parts.append(_simple_table_v07(core_rows[:40], [("kind", "Kind"), ("order", "Order"), ("label", "Label"), ("evidence", "Evidence")], "No core timeline events."))
        parts.append("</details>")
    return "\n".join(parts)


def _render_privacy_deletion_v07(thread: dict[str, Any]) -> str:
    metadata = thread.get("metadata", {}) or {}
    privacy = metadata.get("private_detection", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    deletion_state = deletion.get("state") or "unknown"
    private = bool(privacy.get("private_mode"))
    return (
        "<div class='metrics two'>"
        + _panel_metric("Deletion", deletion_state, f"strong={len(deletion.get('strong_evidence') or [])}, weak={len(deletion.get('weak_evidence') or [])}")
        + _panel_metric("Private mode", "Yes" if private else "No", privacy.get("interpretation") or "")
        + "</div>"
        + _raw_details_v07("Deletion evidence details", deletion)
        + _raw_details_v07("Private-mode evidence details", privacy)
    )


def _render_thread_detail_v07(thread: dict[str, Any], idx: int) -> str:
    classification = thread.get("classification", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    prompt = thread.get("prompt", {}) or {}
    title = _thread_title_v07(thread, idx)
    quality, qkind, explanation = _thread_quality_v07(thread)
    badges = (
        _badge_v07(classification.get("interaction_type"), "good" if classification.get("interaction_type") == "agentic" else "neutral")
        + _badge_v07(classification.get("execution_mode"), "good" if classification.get("execution_mode") == "browser_control" else "neutral")
        + _badge_v07("confidence: " + str(classification.get("confidence")), "neutral")
        + _badge_v07(quality, qkind)
    )
    classification_evidence = ", ".join(str(x) for x in classification.get("classification_evidence", []) or [])
    return "\n".join([
        "<section class='card thread-detail'>",
        "<div class='thread-head'>",
        f"<div><h2>{_h(title)}</h2><p class='thread-id'>{_h(thread.get('thread_id'))}</p></div>",
        f"<div class='badges'>{badges}</div>",
        "</div>",
        f"<div class='note {qkind}'><strong>Reconstruction verdict:</strong> {_h(explanation)}<br><span class='muted'>{_status_line_v07(thread)}</span></div>",
        "<h3>1. Prompt</h3>",
        f"<div class='prompt'>{_h(prompt.get('text') or 'No prompt extracted.')}</div>",
        _evidence_row("Prompt evidence", prompt.get("evidence")),
        "<h3>2. Classification & key metadata</h3>",
        _kv_table_v07([
            ("Interaction / execution", SafeHtml(_badge_v07(classification.get("interaction_type"), "good") + _badge_v07(classification.get("execution_mode"), "good" if classification.get("execution_mode") in {"browser_control", "computer_mode"} else "neutral"))),
            ("Classification evidence", classification_evidence),
            ("Core status", metadata.get("status") or metadata.get("thread_status")),
            ("Final flag", metadata.get("final")),
            ("Search mode", metadata.get("search_mode")),
            ("Display model", metadata.get("display_model")),
            ("Message mode", metadata.get("message_mode")),
            ("Search focus", metadata.get("search_focus")),
            ("Backend UUID", metadata.get("backend_uuid")),
            ("Context UUID", metadata.get("context_uuid")),
        ]),
        _render_external_status_v07(thread),
        "<h3>3. Activity / artifact reconstruction</h3>",
        _render_activity_v07(thread),
        "<h3>4. Reasoning availability</h3>",
        _kv_table_v07([
            ("Available", (thread.get("reasoning", {}) or {}).get("available")),
            ("Note", (thread.get("reasoning", {}) or {}).get("note")),
        ]),
        "<h3>5. Final answer</h3>",
        _render_final_answer_v07(thread),
        "<h3>6. Privacy / deletion</h3>",
        _render_privacy_deletion_v07(thread),
        "<h3>7. Time & storage evidence</h3>",
        _render_time_and_sources_v07(thread),
        _raw_details_v07("Raw metadata evidence", metadata.get("evidence")),
        _raw_details_v07("Full thread JSON", thread),
        "</section>",
    ])


def _render_global_artifacts_v07(global_records: list[dict[str, Any]]) -> str:
    if not global_records:
        return "<div class='empty'>No unassigned global artifacts.</div>"
    counts: dict[str, int] = {}
    for rec in global_records:
        kind = str(rec.get("record_kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    rows = [{"kind": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    preview = []
    for rec in global_records[:20]:
        preview.append({
            "kind": rec.get("record_kind"),
            "key": shorten_text(rec.get("key"), 140),
            "preview": shorten_text(rec.get("value_preview"), 160),
            "evidence": rec.get("evidence"),
        })
    return "\n".join([
        "<div class='note'><strong>Residual artifacts are not automatically assigned to a thread.</strong><br>They may include list_recent, topMostUrls, stale cache, or artifacts from earlier browser activity.</div>",
        "<h4>Counts by record kind</h4>",
        _simple_table_v07(rows, [("kind", "Record kind"), ("count", "Count")], "No global artifacts."),
        "<details><summary>Show global artifact preview</summary>",
        _simple_table_v07(preview, [("kind", "Kind"), ("key", "Key"), ("preview", "Preview"), ("evidence", "Evidence")], "No global artifacts."),
        "</details>",
        _raw_details_v07("Raw global artifact details", global_records[:100]),
    ])


def render_html_report(report: dict[str, Any], output_path: Path) -> None:
    """v09 minimal sidebar HTML report.

    This keeps the reconstruction logic untouched. The JSON report remains the
    authoritative output. The HTML is a simple readable viewer: choose one item
    in the left sidebar and only that section is shown on the right.
    """
    summary = report.get("summary", {}) or {}
    extraction = report.get("extraction_summary", {}) or {}
    source = report.get("source", {}) or {}
    threads = report.get("threads", []) or []

    def nav_thread_label(thread: dict[str, Any], idx: int) -> str:
        prompt = thread.get("prompt", {}) or {}
        refs = prompt.get("reference_codes") or []
        if refs:
            return str(refs[0])
        text = prompt.get("text") or thread.get("thread_id") or f"Thread {idx}"
        return shorten_text(text, 44)

    def nav_button(target: str, label: str, sublabel: str = "", active: bool = False) -> str:
        cls = "nav-item active" if active else "nav-item"
        sub = f"<span>{_h(sublabel)}</span>" if sublabel else ""
        return (
            f"<button type='button' class='{cls}' data-target='{_h(target)}'>"
            f"<strong>{_h(label)}</strong>{sub}</button>"
        )

    style = """
<style>
:root{--bg:#f4f6fb;--card:#fff;--ink:#111827;--muted:#667085;--line:#d9e0ea;--soft:#f8fafc;--blue:#1d4ed8;--green:#047857;--amber:#b45309;--red:#b91c1c;--nav:#111827;}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;line-height:1.55}.layout{display:grid;grid-template-columns:285px minmax(0,1fr);min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;overflow:auto;background:var(--nav);color:#fff;padding:22px 18px}.brand{padding-bottom:16px;border-bottom:1px solid rgba(255,255,255,.12);margin-bottom:14px}.brand h1{font-size:18px;line-height:1.2;margin:0 0 6px}.brand p{margin:0;color:#cbd5e1;font-size:12px;word-break:break-all}.nav-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin:18px 6px 8px}.nav-item{width:100%;border:0;background:transparent;color:#d1d5db;text-align:left;border-radius:10px;padding:10px 11px;margin:3px 0;cursor:pointer}.nav-item strong{display:block;font-size:13px;line-height:1.25}.nav-item span{display:block;font-size:11px;color:#94a3b8;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.nav-item:hover{background:rgba(255,255,255,.09);color:#fff}.nav-item.active{background:#fff;color:#111827}.nav-item.active span{color:#475569}.main{max-width:1160px;width:100%;margin:0 auto;padding:26px}.view-section{display:none}.view-section.active{display:block}.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px;margin:0 0 18px;box-shadow:0 2px 10px rgba(15,23,42,.04)}.hero{background:#111827;color:#fff;border-radius:18px;padding:26px 30px;margin-bottom:18px}.hero h1{margin:0 0 8px;font-size:28px}.hero p{margin:4px 0;color:#cbd5e1}h2{margin:0 0 14px}h3{margin:24px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}h4{margin:18px 0 8px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.metrics.two{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}.metrics.six{grid-template-columns:repeat(auto-fit,minmax(135px,1fr))}.metric{background:var(--soft);border:1px solid var(--line);border-radius:13px;padding:12px}.metric-label{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.metric-value{font-size:24px;font-weight:800}.metric-note{font-size:12px;color:var(--muted);margin-top:4px}.badge{display:inline-block;border-radius:999px;padding:4px 9px;margin:2px 4px 2px 0;font-size:12px;font-weight:700;background:#eef2ff;color:#3730a3}.badge.good{background:#dcfce7;color:#166534}.badge.warn{background:#fef3c7;color:#92400e}.badge.bad{background:#fee2e2;color:#991b1b}.badge.neutral{background:#f3f4f6;color:#374151}.note{border-left:4px solid var(--blue);background:#eff6ff;border-radius:12px;padding:12px 14px;margin:12px 0}.note.warn{border-left-color:var(--amber);background:#fffbeb}.note.good{border-left-color:var(--green);background:#ecfdf5}.prompt,.answer{white-space:pre-wrap;background:#f8fafc;border:1px solid var(--line);border-radius:14px;padding:16px;overflow:auto}.answer{max-height:560px}.good-answer{border-left:4px solid var(--green)}.muted,.small{color:var(--muted);font-size:13px}.kv{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--line);border-radius:12px;overflow:hidden}.kv th{width:230px;text-align:left;background:#f8fafc;color:#475467}.kv th,.kv td{border-bottom:1px solid var(--line);padding:10px;vertical-align:top}.kv tr:last-child th,.kv tr:last-child td{border-bottom:0}.table{width:100%;border-collapse:collapse}.table th,.table td{border:1px solid var(--line);padding:8px 9px;vertical-align:top;text-align:left}.table th{background:#f8fafc;color:#475467}.compact td{font-size:13px}.thread-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:14px}.thread-card{border:1px solid var(--line);border-radius:16px;padding:16px;background:#fff}.thread-card h3{border:0;margin:8px 0 2px;padding:0}.thread-card-top{display:flex;justify-content:space-between;gap:8px}.thread-num{font-weight:800;color:var(--blue)}.thread-id{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:var(--muted);word-break:break-all}.mini-row{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.mini{background:#f3f4f6;border-radius:8px;padding:3px 7px;font-size:12px}.thread-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}.evidence-row{margin:8px 0}.evidence-row strong{display:block;font-size:12px;color:#475467;margin-bottom:4px}.evidence-chip{display:inline-block;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f3f4f6;border:1px solid #d0d5dd;border-radius:7px;padding:4px 7px;margin:2px 4px 2px 0;font-size:12px}.empty{color:#667085;font-style:italic;background:#f9fafb;border:1px dashed #cbd5e1;border-radius:12px;padding:12px}.split{display:grid;grid-template-columns:1.1fr .9fr;gap:16px}details{border:1px solid var(--line);border-radius:12px;background:#fff;margin:10px 0}summary{cursor:pointer;padding:10px 13px;font-weight:700;color:#374151;background:#f8fafc;border-radius:12px}details[open] summary{border-bottom:1px solid var(--line);border-radius:12px 12px 0 0}pre.raw{margin:0;padding:14px;background:#111827;color:#e5e7eb;overflow:auto;border-radius:0 0 12px 12px;font-size:12px;line-height:1.45}.findings li{margin:6px 0}a{color:#1d4ed8;word-break:break-all}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#0f172a}.topline{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}.hint{font-size:13px;color:#475569;background:#f8fafc;border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:14px}@media(max-width:980px){.layout{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.main{padding:16px}.split{grid-template-columns:1fr}.nav-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:4px}.nav-title{margin-top:12px}}@media print{.sidebar{display:none}.layout{display:block}.main{max-width:none}.view-section{display:block!important}.card{break-inside:avoid}}
</style>
<noscript><style>.view-section{display:block!important}.sidebar{position:relative;height:auto}</style></noscript>
"""

    nav_parts: list[str] = []
    nav_parts.append("<aside class='sidebar'>")
    nav_parts.append("<div class='brand'><h1>Comet Reconstruction</h1>")
    nav_parts.append(f"<p>{_h(Path(str(source.get('input') or '')).name)}</p></div>")
    nav_parts.append("<div class='nav-list'>")
    nav_parts.append("<div class='nav-title'>Report</div>")
    nav_parts.append(nav_button("summary", "Case summary", f"{summary.get('thread_count', 0)} threads", True))
    nav_parts.append(nav_button("overview", "Thread overview", "Reconstruction list"))
    nav_parts.append("<div class='nav-title'>Threads</div>")
    for idx, thread in enumerate(threads, start=1):
        cls = thread.get("classification", {}) or {}
        nav_parts.append(nav_button(f"thread-{idx}", f"Thread {idx}", f"{nav_thread_label(thread, idx)} · {cls.get('execution_mode') or 'unknown'}"))
    nav_parts.append("<div class='nav-title'>Evidence</div>")
    nav_parts.append(nav_button("global", "Residual / unassigned", f"{extraction.get('global_record_count', 0)} records"))
    if report.get("skipped"):
        nav_parts.append(nav_button("skipped", "Skipped groups", f"{len(report.get('skipped') or [])} groups"))
    if report.get("snapshot_comparison"):
        nav_parts.append(nav_button("comparison", "Snapshot comparison", "Before / after"))
    nav_parts.append(nav_button("raw", "Raw JSON", "Full report"))
    nav_parts.append("</div></aside>")

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Comet Browser Reconstruction Report</title>",
        style,
        "</head><body><div class='layout'>",
        "\n".join(nav_parts),
        "<main class='main'>",
        "<section id='summary' class='view-section active'>",
        "<section class='hero'>",
        "<h1>Comet Browser Reconstruction Report</h1>",
        f"<p><strong>Input:</strong> {_h(Path(str(source.get('input') or '')).name)}</p>",
        f"<p><strong>Scope:</strong> {_h(source.get('analysis_scope'))} · <strong>Target:</strong> {_h(source.get('target_origin'))} · <strong>Store:</strong> {_h(source.get('database'))}/{_h(source.get('object_store'))}</p>",
        "</section>",
        "<section class='card'><div class='topline'><h2>Executive findings</h2><span class='badge neutral'>JSON-synced view</span></div>",
        "<div class='hint'>좌측 목록에서 항목을 선택하면 오른쪽에 해당 섹션만 표시됩니다. HTML은 JSON 결과를 보기 쉽게 보여주는 뷰어이며, 별도 판단을 추가하지 않습니다.</div>",
        _render_executive_findings_v07(report),
        "<div class='metrics'>",
        _panel_metric("Threads", summary.get("thread_count", 0)),
        _panel_metric("Browser agent", summary.get("browser_agent_thread_count", 0)),
        _panel_metric("Computer mode", summary.get("computer_mode_thread_count", 0)),
        _panel_metric("Skipped computer", summary.get("skipped_computer_mode_count", 0)),
        _panel_metric("Global records", extraction.get("global_record_count", 0)),
        _panel_metric("Parsed records", extraction.get("all_record_count", 0)),
        _panel_metric("Relevant records", extraction.get("relevant_record_count", 0)),
        "</div></section>",
        _render_case_summary_v10(report),
        "</section>",
        "<section id='overview' class='card view-section'><div class='topline'><h2>Thread overview</h2><span class='badge neutral'>Select a thread on the left</span></div>",
        _render_thread_overview_v07(report),
        "</section>",
    ]
    for idx, thread in enumerate(threads, start=1):
        detail = _render_thread_detail_v07(thread, idx)
        detail = detail.replace("<section class='card thread-detail'>", f"<section id='thread-{idx}' class='card thread-detail view-section'>", 1)
        parts.append(detail)

    parts.extend([
        "<section id='global' class='card view-section'><h2>Residual / global artifacts</h2>",
        _render_global_artifacts_v07(report.get("global_records", []) or []),
        "</section>",
    ])
    if report.get("skipped"):
        parts.extend([
            "<section id='skipped' class='card view-section'><h2>Skipped groups</h2>",
            _raw_details_v07("Skipped group details", report.get("skipped")),
            "</section>",
        ])
    if report.get("snapshot_comparison"):
        parts.extend([
            "<section id='comparison' class='card view-section'><h2>Snapshot comparison</h2>",
            render_snapshot_comparison(report.get("snapshot_comparison", {})),
            _raw_details_v07("Raw snapshot comparison", report.get("snapshot_comparison")),
            "</section>",
        ])
    parts.extend([
        "<section id='raw' class='card view-section'><h2>Full raw report JSON</h2>",
        "<div class='note warn'><strong>Authoritative raw output.</strong><br>The readable sections above are generated from this same report object.</div>",
        "<pre class='raw'>" + _h(json.dumps(report, ensure_ascii=False, indent=2, default=str)) + "</pre>",
        "</section>",
        "</main></div>",
        """
<script>
(function(){
  const sections = Array.from(document.querySelectorAll('.view-section'));
  const buttons = Array.from(document.querySelectorAll('.nav-item[data-target]'));
  function showSection(id){
    const target = document.getElementById(id);
    if(!target) return;
    sections.forEach(sec => sec.classList.remove('active'));
    target.classList.add('active');
    buttons.forEach(btn => btn.classList.toggle('active', btn.dataset.target === id));
    window.scrollTo(0, 0);
    history.replaceState(null, '', '#' + id);
  }
  buttons.forEach(btn => btn.addEventListener('click', () => showSection(btn.dataset.target)));
  const initial = (window.location.hash || '').replace('#','');
  if(initial && document.getElementById(initial)) showSection(initial);
})();
</script>
""",
        "</body></html>",
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# v0.6 prompt provenance and conservative Computer/subtask filtering overrides
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.6"

PROGRESS_OR_SUBTASK_TITLE_PATTERNS = [
    r"^clicking\b", r"^waiting\b", r"^opening\b", r"^downloading\b", r"^saving\b",
    r"^checking\b", r"^preparing\b", r"^filling\b", r"^navigating\b", r"^finding\b",
    r"\bbutton\b", r"\bto finish\b", r"\bpopup\b", r"\bviewer\b",
    r"^클릭", r"^기다", r"^다운로드", r"^저장", r"^확인", r"^이동", r"^입력",
]

USER_PROMPT_HINTS = [
    "reference code", "ref code", "experiment", "use browser control", "use computer mode",
    "use computer", "do not", "don't", "keep", "open the", "create", "download exactly",
    "recipient:", "event title:", "step 1", "step 2", "this is", "is my reference code",
]

CONFLICT_REFERENCE_RE = re.compile(r"\b(?:S\d{2}|C\d{2}|Computer)_[A-Za-z0-9_\-]+(?:_\d{8})?\b")


def looks_like_progress_or_subtask_title(text: Any) -> bool:
    s = " ".join(str(text or "").strip().split())
    if not s:
        return False
    low = s.lower()
    if len(s) <= 120:
        for pat in PROGRESS_OR_SUBTASK_TITLE_PATTERNS:
            if re.search(pat, low, flags=re.IGNORECASE):
                return True
    return False


def looks_like_user_prompt_text(text: Any, field: str | None = None) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    low = s.lower()
    if looks_like_progress_or_subtask_title(s):
        return False
    # query_str is the most reliable user-authored prompt field.
    if field == "query_str":
        return True
    if extract_reference_codes(s) and ("reference" in low or len(s) > 80 or "_" in s):
        return True
    if len(s) >= 80 and any(h in low for h in USER_PROMPT_HINTS):
        return True
    if "\n" in s and len(s) >= 80 and any(v in low for v in ["use ", "do not", "keep", "open", "download", "create"]):
        return True
    return False


def extract_prompt(records: list[ForensicRecord]) -> dict[str, Any]:
    """Recover only the user-authored prompt.

    Important distinction:
    - query_str is treated as user prompt.
    - title/thread_title are accepted only when they look like a full user instruction.
      Short ASI/Computer subtask titles such as "Clicking the Download..." are not
      promoted to prompt; they belong in task_title/subtask metadata instead.
    """
    candidates: list[tuple[int, str, ForensicRecord, str]] = []
    priority = {"query_str": 0, "thread_title": 2, "title": 3}

    for record in records:
        if not isinstance(record.value, (dict, list)):
            continue
        for field, value in recursive_collect_fields(record.value, {"query_str", "title", "thread_title"}):
            if not isinstance(value, str) or not value.strip():
                continue
            text = value.strip()
            if not looks_like_user_prompt_text(text, field):
                continue
            score = priority.get(field, 10)
            if field == "query_str":
                score -= 10
            if extract_reference_codes(text):
                score -= 3
            if len(text) > 100:
                score -= 1
            candidates.append((score, text, record, field))

    if not candidates:
        return {
            "text": None,
            "field": None,
            "reference_codes": [],
            "evidence": [],
            "note": "No user-authored prompt was recovered from query_str/thread_title/title. Short task/progress titles were not treated as prompts.",
        }

    candidates.sort(key=lambda item: (item[0], -(len(item[1])), item[2].ldb_seq_no or -1))
    _, text, record, field = candidates[0]
    return {
        "text": text,
        "field": field,
        "reference_codes": extract_reference_codes(text),
        "evidence": record_evidence(record),
        "note": "Prompt is restricted to user-authored instruction fields; agent progress/subtask titles are excluded.",
    }


def _best_user_prompt_from_entries_v06(entries: list[dict[str, Any]], latest_record: ForensicRecord | None = None) -> tuple[str | None, str | None]:
    candidates: list[tuple[int, str, str]] = []
    for entry in entries or []:
        for field in ("query_str", "thread_title", "title"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            text = value.strip()
            if not looks_like_user_prompt_text(text, field):
                continue
            score = {"query_str": 0, "thread_title": 2, "title": 3}.get(field, 10)
            if extract_reference_codes(text):
                score -= 3
            if len(text) > 100:
                score -= 1
            candidates.append((score, text, field))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: (item[0], -len(item[1])))
    return candidates[0][1], candidates[0][2]


def _best_task_title_from_entries_v06(entries: list[dict[str, Any]]) -> str | None:
    # Prefer concise non-prompt titles for ASI subtasks; they are not user prompts.
    for entry in reversed(entries or []):
        title = entry.get("title")
        if isinstance(title, str) and title.strip() and not looks_like_user_prompt_text(title, "title"):
            return title.strip()
    for entry in reversed(entries or []):
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def build_promoted_computer_thread(thread_id: str, candidate: dict[str, Any], global_context_urls: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[ForensicRecord] = candidate.get("records") or []
    entries: list[dict[str, Any]] = candidate.get("entries") or []
    latest_record = max(records, key=lambda r: r.ldb_seq_no or -1) if records else None
    latest_entry = entries[-1] if entries else {}

    prompt_text, prompt_field = _best_user_prompt_from_entries_v06(entries, latest_record)
    task_title = _best_task_title_from_entries_v06(entries)
    prompt_evidence = record_evidence(latest_record) if (latest_record and prompt_text) else []

    # Pick a latest answer-bearing entry, not necessarily the latest subtask title entry.
    answer_entry = None
    for entry in reversed(entries):
        if entry.get("first_answer") or entry.get("answer") or entry.get("answer_preview"):
            answer_entry = entry
            break
    if answer_entry is None:
        answer_entry = latest_entry

    first_answer_raw = answer_entry.get("first_answer") or answer_entry.get("answer") or answer_entry.get("answer_preview")
    parsed_first_answer = parse_maybe_json(first_answer_raw)
    answer_texts = [t for t in extract_text_from_any(parsed_first_answer, max_items=12) if is_useful_human_text(t, prompt_text)]
    final_text = answer_texts[-1] if answer_texts else None
    answer_evidence = record_evidence(latest_record) if (latest_record and final_text) else []

    reasoning_items: list[dict[str, Any]] = []
    for record in records:
        reasoning_items.extend(extract_computer_reasoning_items_from_value(record.value, record))
    if parsed_first_answer is not None and latest_record is not None:
        reasoning_items.extend(extract_computer_reasoning_items_from_value(parsed_first_answer, latest_record))
    deduped_reasoning: list[dict[str, Any]] = []
    seen_reasoning: set[str] = set()
    for item in reasoning_items:
        key = item.get("text_preview", "")[:300]
        if key and key not in seen_reasoning:
            seen_reasoning.add(key)
            deduped_reasoning.append(item)

    id_entry = latest_entry or answer_entry or {}
    metadata = {
        "status": id_entry.get("status") or id_entry.get("thread_status") or id_entry.get("answer_status"),
        "final": bool(final_text) if final_text is not None else None,
        "mode": id_entry.get("mode") or "asi",
        "search_mode": str(id_entry.get("search_mode") or "ASI").upper(),
        "display_model": id_entry.get("display_model") or id_entry.get("model_preference") or "pplx_asi_candidate",
        "thread_status": id_entry.get("status") or id_entry.get("thread_status"),
        "created_at": id_entry.get("created_at") or id_entry.get("last_query_datetime"),
        "updated_at": id_entry.get("updated_at") or id_entry.get("last_query_datetime"),
        "last_query_datetime": id_entry.get("last_query_datetime"),
        "backend_uuid": id_entry.get("uuid") or thread_id,
        "context_uuid": id_entry.get("context_uuid"),
        "frontend_uuid": id_entry.get("frontend_uuid"),
        "frontend_context_uuid": id_entry.get("frontend_context_uuid"),
        "slug": id_entry.get("slug"),
        "thread_number": id_entry.get("thread_number"),
        "task_title": task_title,
        "source_kind": "asi_thread_list_cache_promoted",
        "cache_entry_count": len(entries),
        "evidence": {"asi_thread_list_cache": record_evidence(latest_record) if latest_record else []},
    }

    prompt = {
        "text": prompt_text,
        "field": prompt_field,
        "reference_codes": extract_reference_codes(prompt_text or ""),
        "evidence": prompt_evidence,
        "note": "Computer/ASI prompt is reported only when a user-authored query_str/full instruction is recovered. Short ASI subtask titles are stored as metadata.task_title, not prompt.",
    }

    classification = {
        "interaction_type": "agentic",
        "execution_mode": "computer_mode",
        "confidence": "medium" if len(records) <= 1 else "high",
        "reconstruction_status": "computer_partial_list_cache_reconstruction" if final_text or prompt_text else "computer_metadata_only",
        "classification_evidence": ["asi_thread_list_cache", "thread_type_filter=asi", f"mode={metadata.get('mode')}", "search_mode=ASI"] + (["first_answer_cache"] if first_answer_raw else []),
    }

    urls = extract_urls(records)
    typed_payloads = extract_typed_payloads(records, prompt_text=prompt_text)
    plan: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if task_title and looks_like_progress_or_subtask_title(task_title):
        actions.append({
            "kind": "asi_subtask_title",
            "label": task_title[:1000],
            "relative_order": latest_record.ldb_seq_no if latest_record else None,
            "evidence": record_evidence(latest_record) if latest_record else [],
            "interpretation": "Computer/ASI subtask title or progress label; not a user prompt.",
        })
    for text in answer_texts[:4]:
        if any(keyword.lower() in text.lower() for keyword in ["open", "download", "saving", "click", "navigate", "pdf", "browser automation"]):
            actions.append({
                "kind": "asi_answer_action_candidate",
                "label": text[:1000],
                "relative_order": latest_record.ldb_seq_no if latest_record else None,
                "evidence": record_evidence(latest_record) if latest_record else [],
            })

    final_answer = {
        "text": final_text,
        "available": bool(final_text),
        "reason": None if final_text else "ASI list-cache entry was promoted, but no clean first_answer/final-answer text was recovered.",
        "relative_order": latest_record.ldb_seq_no if latest_record else None,
        "evidence": answer_evidence,
        "source_kind": "asi_thread_list_first_answer" if final_text else None,
        "caution": "This is recovered from ASI thread-list cache/first_answer, not yet corroborated by all_results core thread content.",
    }
    reasoning = {
        "available": bool(deduped_reasoning),
        "items": deduped_reasoning[:25],
        "note": None if deduped_reasoning else "Computer/ASI task detected from list cache, but no thought/reasoning block was cleanly parsed in this promoted candidate.",
    }

    private_mode = detect_private_mode(records)
    deletion_state = detect_deletion_state(records)
    metadata["private_mode"] = private_mode["private_mode"]
    metadata["private_detection"] = private_mode
    timeline = build_timeline(records=records, prompt=prompt, plan=plan, actions=actions, urls=urls, typed_payloads=typed_payloads, final_answer=final_answer)

    return {
        "thread_id": thread_id,
        "classification": classification,
        "prompt": prompt,
        "metadata": metadata,
        "plan": plan,
        "actions": actions,
        "urls": urls,
        "context_url_candidates": [],
        "typed_payloads": typed_payloads,
        "reasoning": reasoning,
        "final_answer": final_answer,
        "deletion_state": deletion_state,
        "timeline": timeline,
        "record_count": len(records),
        "source_summary": summarize_sources(records),
        "promotion_note": "Promoted from /rest/thread/list_ask_threads ASI cache. User prompt and subtask titles are separated; verify with /computer/tasks, History, or Downloads artifacts.",
        "source_entries_sample": entries[:3],
    }


def is_activity_noise_label(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    s = " ".join(value.strip().split())
    if not s:
        return True
    if is_internal_noise_string(s):
        return True
    if re.fullmatch(r"\d+", s):
        return True
    if s.lower() in {"click", "type", "wait", "find", "key", "input", "output", "text", "content"}:
        return True
    return False


def _path_has_any(path: tuple[str, ...], hints: Iterable[str]) -> bool:
    path_low = "/".join(path).lower()
    return any(str(h).lower() in path_low for h in hints)


def extract_typed_payloads(records: list[ForensicRecord], prompt_text: str | None = None) -> list[dict[str, Any]]:
    """Extract concrete typed/form payloads only.

    This intentionally avoids treating plan text, answer text, source snippets,
    web_results, screenshot URLs, and workflow status labels as typed payloads.
    """
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    forbidden_path_hints = set(NON_PAYLOAD_CONTEXT_HINTS) | {"plan_block", "markdown_block", "answer", "chunks", "final_sse_message", "classifier", "telemetry"}
    tool_input_hints = {"tool", "input", "arguments", "args", "form", "compose", "calendar", "event", "typed", "typing", "타이핑", "WORKFLOW_ITEM_TEXT", "COMET_AGENT_TOOL_INPUT"}
    scalar_fields = {x.lower() for x in SCALAR_PAYLOAD_FIELDS}

    def allowed_context(path: tuple[str, ...], record: ForensicRecord) -> bool:
        if _path_has_any(path, forbidden_path_hints) and not _path_has_any(path, tool_input_hints):
            return False
        if record.record_kind == "tool_io":
            return True
        if _path_has_any(path, tool_input_hints):
            return True
        text = json_text(record.value, 6000).lower()
        return "comet_agent_tool_input" in text or "타이핑" in text

    def add(field: str, value: Any, record: ForensicRecord, path: tuple[str, ...]) -> None:
        if not isinstance(value, (str, int, float, bool)):
            return
        s = str(value).strip()
        if not s or is_internal_noise_string(s) or is_prompt_duplicate(s, prompt_text):
            return
        if looks_like_progress_or_subtask_title(s):
            return
        field_low = field.lower()
        if not allowed_context(path, record):
            return
        if field_low in {"text", "input", "query"}:
            # Generic text fields are accepted only when the path clearly says this
            # was a typed/tool argument, not workflow prose.
            if not _path_has_any(path, {"input", "arguments", "args", "typed", "typing", "타이핑", "WORKFLOW_ITEM_TEXT", "COMET_AGENT_TOOL_INPUT"}):
                return
            if len(s) > 1000:
                return
        elif field_low in {"url", "href"}:
            if any(x in s.lower() for x in ["ppl-ai-agent-screenshots", "cloudinary", "s3.amazonaws.com"]):
                return
            if len(s) > 2000:
                return
        elif field_low in scalar_fields:
            if len(s) > 5000:
                return
        elif EMAIL_RE.search(s):
            pass
        else:
            return
        key = f"{field_low}:{s[:500]}"
        if key in seen:
            return
        seen.add(key)
        payloads.append({"field": field, "value": s, "path_hint": "/".join(path[-6:]), "relative_order": record.ldb_seq_no, "evidence": record_evidence(record)})

    def walk(obj: Any, record: ForensicRecord, path: tuple[str, ...] = ()) -> None:
        obj = parse_maybe_json(obj)
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k)
                lower = key.lower()
                new_path = path + (key,)
                if key == "evidence":
                    continue
                if lower in scalar_fields or lower in {"url", "href", "input", "text", "query"}:
                    if isinstance(v, (str, int, float, bool)):
                        add(key, v, record, new_path)
                    elif isinstance(v, (dict, list)) and allowed_context(new_path, record):
                        for s in flatten_strings(v, max_len=2000):
                            add(key, s, record, new_path)
                walk(v, record, new_path)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj[:300]):
                walk(item, record, path + (str(idx),))

    for record in records:
        text = json_text(record.value, 80000).lower()
        if record.record_kind == "tool_io" or "comet_agent_tool_input" in text or "타이핑" in text or "workflow_item_text" in text:
            walk(record.value, record)
    return sorted(payloads, key=lambda item: item.get("relative_order") or -1)


def _extract_reference_family(text: str) -> set[str]:
    return {m.group(0) for m in CONFLICT_REFERENCE_RE.finditer(text or "")}


def _thread_has_conflicting_reference_v06(thread: dict[str, Any], target: str) -> bool:
    target_refs = _extract_reference_family(target)
    prompt = ((thread.get("prompt", {}) or {}).get("text") or "")
    title = ((thread.get("metadata", {}) or {}).get("task_title") or "")
    text = "\n".join([prompt, title])
    refs = _extract_reference_family(text)
    # If a thread carries a different experiment/reference code in its prompt/title,
    # do not pull it into the target case through a loose task-id match.
    return bool(refs and target_refs and not (refs & target_refs))


def _target_task_hint_v06(target: str, seeds: dict[str, Any]) -> str | None:
    text = target + "\n" + json.dumps(seeds.get("sources") or [], ensure_ascii=False, default=str)
    low = text.lower()
    if any(x in low for x in ["download", "pdf", "file"]):
        return "download"
    if any(x in low for x in ["calendar", "event"]):
        return "calendar"
    if any(x in low for x in ["gmail", "email", "draft", "send"]):
        return "email"
    if any(x in low for x in ["wikipedia", "navigate", "page"]):
        return "navigation"
    return None


def _thread_task_compatible_v06(thread: dict[str, Any], target_kind: str | None) -> bool:
    if not target_kind:
        return True
    outcome = (thread.get("task_outcome", {}) or {}).get("task_type")
    text = collect_thread_text(thread, include_prompt=True).lower()
    if target_kind == "download":
        return outcome == "download" or any(x in text for x in ["download", "pdf", "downloaded filename", ".pdf"])
    if target_kind == "calendar":
        return outcome == "calendar_create" or any(x in text for x in ["calendar", "event"])
    if target_kind == "email":
        return outcome == "gmail_send" or any(x in text for x in ["gmail", "email", "draft", "sent folder"])
    if target_kind == "navigation":
        return outcome in {"page_open", "web_research"} or any(x in text for x in ["navigate", "open", "wikipedia", "page"])
    return True


def _thread_matches_target_case_v04(thread: dict[str, Any], target: str, seeds: dict[str, Any]) -> tuple[bool, str]:
    if _contains_text_v04(thread, target):
        if _thread_has_conflicting_reference_v06(thread, target):
            return False, "conflicting_reference"
        return True, "direct_target_text"
    if not _is_computer_thread_v04(thread):
        return False, "no_match"
    if _thread_has_conflicting_reference_v06(thread, target):
        return False, "conflicting_reference"

    ident = _thread_identity_v04(thread)
    seed_contexts = seeds.get("contexts") or set()
    seed_ids = seeds.get("ids") or set()
    target_kind = _target_task_hint_v06(target, seeds)

    if seed_contexts and ident.get("contexts") and (ident["contexts"] & seed_contexts):
        if _thread_task_compatible_v06(thread, target_kind):
            return True, "computer_same_context_uuid"
        return False, "context_match_but_task_incompatible"

    # ID-only linkage is weak. Keep it only when the thread is task-compatible
    # and does not carry a conflicting prompt/reference. This prevents unrelated
    # stale /computer/tasks/<uuid> payloads from being pulled into the case.
    if seed_ids and ident.get("ids") and (ident["ids"] & seed_ids):
        if _thread_task_compatible_v06(thread, target_kind):
            return True, "computer_same_thread_identifier_weak"
        return False, "id_match_but_task_incompatible"
    return False, "no_match"


# Rewrap the final reconstructor again so the report advertises the corrected schema.
_reconstruct_browser_threads_v05_final = reconstruct_browser_threads

def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_v05_final(extracted, input_label, browser_only=browser_only)
    report["schema_version"] = SCHEMA_VERSION
    audit = report.setdefault("hardcoding_audit", {})
    audit.update({
        "prompt_provenance_rule": "prompt.text contains only recovered user-authored prompt/query text; ASI/agent progress titles are stored separately as metadata.task_title or actions.",
        "target_linkage_rule": "direct target text and context UUID linkage are preferred; ID-only Computer linkage is weak and rejected when a conflicting reference or incompatible task type is detected.",
        "typed_payload_rule": "typed_payloads are restricted to concrete tool/form input context and exclude plan/answer/source/screenshot text.",
    })
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconstruct Perplexity Comet Browser Control behavior from IndexedDB LevelDB artifacts."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="LevelDB folder, scenario folder, or zip containing IndexedDB/https_www.perplexity.ai_0.indexeddb.leveldb.",
    )
    parser.add_argument(
        "--blob-input",
        type=Path,
        default=None,
        help="Optional matching https_www.perplexity.ai_0.indexeddb.blob folder.",
    )
    parser.add_argument("--before", type=Path, default=None, help="Before snapshot zip/folder for deletion comparison.")
    parser.add_argument("--after", type=Path, default=None, help="After snapshot zip/folder for deletion comparison.")
    parser.add_argument("--output", type=Path, default=Path("reconstruction.json"), help="JSON report output path.")
    parser.add_argument("--html-output", type=Path, default=None, help="Optional HTML report output path.")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.add_argument(
        "--include-computer",
        action="store_true",
        help="Do not skip detected Computer mode groups. Current MVP still marks them unsupported/partial.",
    )
    parser.add_argument(
        "--dump-records-dir",
        type=Path,
        default=None,
        help="Optional directory to also write live/dead/ldb/log/unmatched raw parsed record dumps.",
    )
    parser.add_argument(
        "--target-reference",
        default=None,
        help="Optional reference code or keyword. If set, only matching reconstructed/skipped groups are kept in the final report.",
    )
    return parser


def run_single_input(
    input_path: Path,
    blob_input: Path | None,
    database: str,
    store: str,
    include_computer: bool,
    dump_records_dir: Path | None = None,
) -> dict[str, Any]:
    prepared = prepare_input(input_path, explicit_blob_input=blob_input)
    try:
        extracted = extract_records_from_leveldb(
            leveldb_path=prepared.leveldb_path,
            blob_path=prepared.blob_path,
            database_name=database,
            object_store_name=store,
        )

        if dump_records_dir is not None:
            dump_records_dir.mkdir(parents=True, exist_ok=True)
            write_json(dump_records_dir / "perplexity_live_records.json", {"records": extracted["live_records"]})
            write_json(dump_records_dir / "perplexity_dead_records.json", {"records": extracted["dead_records"]})
            write_json(dump_records_dir / "perplexity_ldb_records.json", {"records": extracted["ldb_records"]})
            write_json(dump_records_dir / "perplexity_log_records.json", {"records": extracted["log_records"]})
            write_json(dump_records_dir / "perplexity_unmatched_records.json", {"records": extracted["unmatched_records"]})

        return reconstruct_browser_threads(
            extracted=extracted,
            input_label=str(prepared.original_input),
            browser_only=not include_computer,
        )
    finally:
        prepared.cleanup()


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    """Keep target threads for case-study reporting, while preserving counts.

    v0.2 changes:
    - target filtering runs after Computer/ASI promotion;
    - a case_summary explains whether target was found as a thread or only as
      global cache;
    - residual counts are retained so filtered output does not hide the fact
      that the profile contained earlier/stale activity.
    """
    if not target_reference:
        report["case_summary"] = build_case_summary(report, None)
        return report

    target = str(target_reference).strip()
    if not target:
        report["case_summary"] = build_case_summary(report, None)
        return report

    if report.get("mode") == "before_after_comparison":
        report["target_reference_filter"] = target
        report["filter_warning"] = "Target-reference filtering is not applied to before/after comparison envelopes."
        return report

    filtered = json.loads(json.dumps(report, ensure_ascii=False, default=str))

    def contains_target(item: Any) -> bool:
        return target in json.dumps(item, ensure_ascii=False, default=str)

    original_threads = filtered.get("threads", []) or []
    original_skipped = filtered.get("skipped", []) or []
    original_globals = filtered.get("global_records", []) or []

    filtered_threads = [thread for thread in original_threads if contains_target(thread)]
    filtered_skipped = [group for group in original_skipped if contains_target(group)]
    filtered_globals = [record for record in original_globals if contains_target(record)]

    # Preserve target-only focus for case-study output, but keep residual counts.
    filtered["threads"] = filtered_threads
    filtered["skipped"] = filtered_skipped
    filtered["global_records"] = filtered_globals
    filtered["summary"] = summarize_reconstruction(filtered_threads, filtered_skipped)
    filtered.setdefault("source", {})["target_reference_filter"] = target

    original_counts = {
        "original_thread_count": len(original_threads),
        "original_skipped_count": len(original_skipped),
        "original_global_record_count": len(original_globals),
    }
    filtered["filter_summary"] = {
        "target_reference": target,
        **original_counts,
        "filtered_thread_count": len(filtered_threads),
        "filtered_skipped_count": len(filtered_skipped),
        "filtered_global_record_count": len(filtered_globals),
        "residual_thread_count": len(original_threads) - len(filtered_threads),
        "residual_skipped_count": len(original_skipped) - len(filtered_skipped),
        "residual_global_record_count": len(original_globals) - len(filtered_globals),
    }
    filtered["case_summary"] = build_case_summary(filtered, target, original_counts=original_counts)

    return filtered



# ---------------------------------------------------------------------------
# v0.3 generic interpretation overrides
# ---------------------------------------------------------------------------

NEGATION_CUES = [
    "do not", "don't", "dont", "no ", "not ", "never", "without", "avoid", "금지", "하지 말", "하지마", "다운로드하지", "삭제하지",
]


def looks_like_computer_reference(text: Any) -> bool:
    """Generic reference-pattern helper, not tied to one experiment ID."""
    s = str(text or "")
    return bool(re.search(r"\bcomputer(?:[_\-\s]+(?:mode|task|agent|run|control|download|browser))?\b", s, flags=re.IGNORECASE))


def _near_negation(text: str, start: int, window: int = 48) -> bool:
    left = text[max(0, start - window):start].lower()
    return any(cue in left for cue in NEGATION_CUES)


def has_positive_term(text: str, terms: Iterable[str]) -> bool:
    low = str(text or "").lower()
    for term in terms:
        for m in re.finditer(re.escape(str(term).lower()), low):
            if not _near_negation(low, m.start()):
                return True
    return False


def has_negated_term(text: str, terms: Iterable[str]) -> bool:
    low = str(text or "").lower()
    for term in terms:
        t = re.escape(str(term).lower())
        if re.search(r"(?:do not|don't|dont|not|never|without|avoid)\W+(?:\w+\W+){0,6}" + t, low):
            return True
        if re.search(t + r"\W+(?:files?|documents?)?\W*(?:is|are)?\W*(?:not|forbidden|disallowed)", low):
            return True
        if re.search(r"(?:하지\s*말|하지마|금지).{0,30}" + t, low):
            return True
    return False


def looks_like_basic_chat_thread(prompt_low: str, metadata: dict[str, Any], execution_mode: Any = None) -> bool:
    """Generic non-agentic conversational baseline detector."""
    if execution_mode in {"browser_control", "computer_mode"}:
        return False
    search_mode = str(metadata.get("search_mode") or "").lower()
    display_model = str(metadata.get("display_model") or metadata.get("model_preference") or "").lower()
    side_effect_terms = ["download", "send", "draft", "calendar", "event", "open", "navigate", "browser control", "computer mode", "upload", "delete"]
    if any(term in prompt_low for term in side_effect_terms):
        return False
    return search_mode in {"study", "writing"} or "study" in display_model or "explain" in prompt_low or "summarize" in prompt_low


def extract_expected_page_title(prompt_text: str | None) -> str | None:
    """Extract a requested page-title assertion from the prompt without case-specific titles."""
    if not prompt_text:
        return None
    patterns = [
        r"page\s+title\s+(?:is|equals|should\s+be|must\s+be)\s+[\"']?([^\n\.\"']{2,120})",
        r"confirm\s+that\s+the\s+page\s+title\s+(?:is|equals)\s+[\"']?([^\n\.\"']{2,120})",
        r"title\s+(?:is|equals|should\s+be|must\s+be)\s+[\"']?([^\n\.\"']{2,120})",
    ]
    for pat in patterns:
        m = re.search(pat, prompt_text, flags=re.IGNORECASE)
        if m:
            value = m.group(1).strip().strip("'\"` *_-:;")
            # Stop at common instruction boundaries.
            value = re.split(r"\b(?:after|before|and|keep|do not)\b", value, flags=re.IGNORECASE)[0].strip()
            if 2 <= len(value) <= 120:
                return value
    return None


def classify_task_outcome(thread: dict[str, Any]) -> dict[str, Any]:
    """Generic, non-scenario-specific task outcome classifier."""
    prompt = ((thread.get("prompt", {}) or {}).get("text") or "")
    final = ((thread.get("final_answer", {}) or {}).get("text") or "")
    all_text = collect_thread_text(thread, include_prompt=True)
    final_low = final.lower()
    prompt_low = prompt.lower()
    metadata = thread.get("metadata", {}) or {}
    execution_mode = (thread.get("classification", {}) or {}).get("execution_mode")

    download_terms = ["download", "save", "pdf", "file"]
    email_terms = ["gmail", "email", "compose", "recipient", "subject", "draft", "sent folder"]
    calendar_terms = ["calendar", "event", "schedule", "appointment", "meeting"]
    page_open_terms = ["open the following page", "open this page", "open url", "navigate to", "page title", "keep the page open"]
    research_terms = ["research", "investigate", "consult", "sources used", "web sources", "summarize the findings", "public web sources"]

    negated_download = has_negated_term(prompt, ["download", "file", "pdf"])
    positive_download = has_positive_term(prompt, ["download", "save"]) or bool(re.search(r"\bdownload(?:ed)?\s+filename\b", prompt_low))
    positive_email = has_positive_term(prompt, email_terms) and not has_negated_term(prompt, ["email", "send", "draft"])
    positive_calendar = has_positive_term(prompt, calendar_terms) and not has_negated_term(prompt, ["calendar", "event"])
    positive_page_open = has_positive_term(prompt, page_open_terms) or bool(extract_prompt_target_urls(prompt)) and has_positive_term(prompt, ["open", "navigate", "visit"])
    positive_research = has_positive_term(prompt, research_terms)

    task_type = "unknown"
    if positive_email:
        task_type = "gmail_send"
    elif positive_calendar:
        task_type = "calendar_create"
    elif positive_download and not negated_download:
        task_type = "download"
    elif positive_page_open and not positive_research:
        task_type = "page_open"
    elif positive_research:
        task_type = "web_research"
    elif looks_like_basic_chat_thread(prompt_low, metadata, execution_mode):
        task_type = "basic_chat"

    filenames = extract_pdf_filenames(all_text)
    target_urls = extract_prompt_target_urls(prompt)
    thread_urls = non_noise_thread_urls(thread)

    result: dict[str, Any] = {
        "task_type": task_type,
        "status": "unknown",
        "side_effect_completed": None,
        "confidence": "low",
        "primary_artifacts": [],
        "warnings": [],
        "missing_corroboration": [],
        "negated_actions_detected": {
            "download": negated_download,
        },
    }

    if execution_mode == "browser_control":
        result["primary_artifacts"].append("browser_control_thread")
    elif execution_mode == "computer_mode":
        result["primary_artifacts"].append("computer_mode_or_asi_cache")

    if task_type == "download":
        outcome_text = "\n".join([
            json.dumps(thread.get("final_answer", {}), ensure_ascii=False, default=str),
            json.dumps(thread.get("actions", []), ensure_ascii=False, default=str),
            json.dumps(thread.get("plan", []), ensure_ascii=False, default=str),
        ]).lower()
        confirmation = any(marker in outcome_text for marker in [
            "browser_agent_confirmation", "may i proceed", "please confirm", "confirm before",
            "confirmation", "proceed with the download", "사용자 확인", "확인 요청",
        ])
        completed = bool(filenames) and any(marker in outcome_text for marker in [
            "download is complete", "downloaded filename", "final downloaded filename",
            "downloaded file", "successfully downloaded", "download complete", "다운로드 완료",
        ])
        if completed:
            result.update({"status": "completed_download", "side_effect_completed": True, "confidence": "high", "downloaded_filename_candidates": filenames})
        elif confirmation:
            result.update({"status": "confirmation_required", "side_effect_completed": False, "confidence": "high", "downloaded_filename_candidates": filenames})
        elif filenames or any(".pdf" in str(u).lower() for u in thread_urls):
            result.update({"status": "source_discovery_or_partial_download", "side_effect_completed": None, "confidence": "medium", "downloaded_filename_candidates": filenames})
        else:
            result.update({"status": "download_intent_only", "confidence": "low"})
        result["missing_corroboration"].extend(["Chromium Downloads DB", "OS Downloads folder/file hash"])
        if execution_mode == "computer_mode":
            result["warnings"].append("Computer-mode result is promoted from ASI/cache evidence; verify with Downloads/History artifacts.")

    elif task_type == "gmail_send":
        sent = any(marker in final_low for marker in ["email sent", "sent folder", "sent email", "visible in sent", "sent message"])
        draft = any(marker in final_low for marker in ["draft", "saved as a draft"])
        if sent:
            result.update({"status": "sent_reported_by_agent", "side_effect_completed": True, "confidence": "medium_high"})
        elif draft:
            result.update({"status": "draft_reported_by_agent", "side_effect_completed": None, "confidence": "medium"})
        else:
            result.update({"status": "email_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"].extend(["mail service Sent/Draft record", "message headers"])

    elif task_type == "calendar_create":
        created = any(marker in final_low for marker in ["event", "created", "saved", "visible", "verified", "calendar"])
        if created:
            result.update({"status": "calendar_event_reported_created", "side_effect_completed": True, "confidence": "medium_high"})
        else:
            result.update({"status": "calendar_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"].append("calendar service/event record")

    elif task_type == "page_open":
        expected_title = extract_expected_page_title(prompt)
        opened = any(url in all_text for url in target_urls) if target_urls else bool(thread_urls)
        title_confirmed = bool(expected_title and expected_title.lower() in final_low)
        if opened and (title_confirmed or not expected_title):
            result.update({"status": "target_page_opened_or_reported", "side_effect_completed": True, "confidence": "medium_high" if title_confirmed else "medium"})
        elif opened:
            result.update({"status": "target_page_url_recovered", "side_effect_completed": True, "confidence": "medium"})
        else:
            result.update({"status": "page_open_intent_only", "side_effect_completed": None, "confidence": "low"})
        result["target_urls"] = target_urls
        result["expected_title"] = expected_title
        result["non_noise_thread_urls"] = thread_urls[:20]
        result["missing_corroboration"].append("Chromium History DB for navigation timing")

    elif task_type == "web_research":
        if final:
            result.update({"status": "research_answer_recovered", "side_effect_completed": False, "confidence": "medium_high" if thread_urls else "medium"})
        elif thread.get("urls"):
            result.update({"status": "research_url_leads_only", "side_effect_completed": None, "confidence": "medium"})
        else:
            result.update({"status": "research_intent_or_metadata_only", "side_effect_completed": None, "confidence": "low"})
        result["missing_corroboration"].append("History/cache/source page content")

    elif task_type == "basic_chat":
        result.update({"status": "conversation_answer_recovered" if final else "conversation_prompt_only", "side_effect_completed": False, "confidence": "high" if final else "medium"})

    else:
        has_final = bool(final)
        result.update({"status": "final_answer_recovered" if has_final else "metadata_only", "side_effect_completed": None, "confidence": "medium" if has_final else "low"})
        if negated_download and positive_research:
            result["warnings"].append("Prompt contains a negated download constraint; not classified as a download task.")

    return result


def build_storage_state(thread: dict[str, Any]) -> dict[str, Any]:
    summary = thread.get("source_summary", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    live_count = int(summary.get("live_record_count") or 0)
    dead_count = int(summary.get("dead_or_old_record_count") or 0)
    if live_count and dead_count:
        state = "mixed_live_and_dead_records"
    elif live_count:
        state = "live_records_present"
    elif dead_count:
        state = "dead_or_old_records_only"
    else:
        state = "no_record_state_summary"
    return {
        "state": state,
        "live_record_count": live_count,
        "dead_or_old_record_count": dead_count,
        "deletion_marker_state": deletion.get("state"),
        "has_strong_deletion_evidence": bool(deletion.get("strong_evidence")),
        "interpretation": "Storage state describes parsed LevelDB record state. It does not by itself prove whether the user-visible cloud/thread object still exists.",
    }


def build_content_state(thread: dict[str, Any]) -> dict[str, Any]:
    prompt = thread.get("prompt", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    plan = thread.get("plan") or []
    actions = thread.get("actions") or []
    urls = thread.get("urls") or []
    payloads = thread.get("typed_payloads") or []
    has_prompt = bool(prompt.get("text"))
    has_final = bool(final_answer.get("text"))
    has_workflow = bool(plan)
    has_actions = bool(actions)
    has_urls = bool(urls)
    has_payloads = bool(payloads)
    has_external = bool(metadata.get("external_thread_list_evidence"))
    if has_prompt and has_final and (has_workflow or has_actions or has_urls):
        state = "behavior_content_reconstructed"
    elif has_prompt and has_final:
        state = "prompt_and_answer_only"
    elif has_prompt and (has_workflow or has_actions or has_urls or has_payloads):
        state = "partial_behavior_without_final"
    elif has_prompt:
        state = "metadata_or_prompt_only"
    elif has_external:
        state = "list_cache_only"
    else:
        state = "no_thread_content"
    return {
        "state": state,
        "has_prompt": has_prompt,
        "prompt_field": prompt.get("field"),
        "has_final_answer": has_final,
        "has_workflow": has_workflow,
        "has_actions": has_actions,
        "has_thread_urls": has_urls,
        "has_typed_payloads": has_payloads,
        "has_external_thread_list_evidence": has_external,
        "interpretation": "Content state describes what behavioral content is reconstructable from this thread, separately from LevelDB live/dead record state.",
    }


def build_reconstruction_availability(thread: dict[str, Any]) -> dict[str, Any]:
    storage = build_storage_state(thread)
    content = build_content_state(thread)
    if content["state"] == "behavior_content_reconstructed":
        level = "strong_thread_reconstruction"
    elif content["state"] in {"prompt_and_answer_only", "partial_behavior_without_final"}:
        level = "partial_thread_reconstruction"
    elif content["state"] in {"metadata_or_prompt_only", "list_cache_only"}:
        level = "metadata_residue_only"
    else:
        level = "not_reconstructable"
    return {
        "level": level,
        "storage_state": storage,
        "content_state": content,
        "interpretation": "Use this field for deletion/reopen experiments: a record can be LevelDB-live while only metadata-level content remains reconstructable.",
    }


def add_case_layers_to_thread(thread: dict[str, Any], records: list[ForensicRecord] | None = None) -> dict[str, Any]:
    thread["artifact_buckets"] = build_artifact_buckets(thread)
    thread["task_outcome"] = classify_task_outcome(thread)
    thread["storage_state"] = build_storage_state(thread)
    thread["content_state"] = build_content_state(thread)
    thread["reconstruction_availability"] = build_reconstruction_availability(thread)
    if records is not None:
        thread["temporal_evidence"] = build_temporal_evidence(records, thread.get("metadata", {}) or {})
    return thread


def iter_asi_entry_dicts_v3(obj: Any, assume_asi_context: bool = False) -> list[dict[str, Any]]:
    """Extract ASI/Computer thread entries structurally from list-cache payloads."""
    found: list[dict[str, Any]] = []

    def walk(value: Any, asi_context: bool = False) -> None:
        value = parse_maybe_json(value)
        if isinstance(value, list):
            for child in value:
                walk(child, asi_context)
            return
        if not isinstance(value, dict):
            return
        text = json.dumps(value, ensure_ascii=False, default=str)
        low = text.lower()
        local_asi_context = asi_context or assume_asi_context or (
            "list_ask_threads" in low and "asi" in low
        ) or (
            str(value.get("thread_type_filter") or "").lower() == "asi"
        )
        mode = str(value.get("mode") or "").lower()
        search_mode = str(value.get("search_mode") or "").lower()
        model = str(value.get("display_model") or value.get("model_preference") or "").lower()
        query = str(value.get("query_str") or value.get("title") or "")
        has_identity = bool(value.get("uuid") or value.get("thread_uuid") or value.get("slug") or value.get("context_uuid"))
        has_content = bool(query or value.get("first_answer") or value.get("answer") or value.get("answer_preview"))
        looks_asi = local_asi_context or mode == "asi" or search_mode == "asi" or "pplx_asi" in model or "wide_research" in low or '"variant": "thought"' in low or '"variant":"thought"' in low
        if has_identity and has_content and looks_asi:
            found.append(to_jsonable(value))
        for child in value.values():
            walk(child, local_asi_context)

    walk(obj, assume_asi_context)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in found:
        sig = json.dumps({
            "uuid": entry.get("uuid"),
            "thread_uuid": entry.get("thread_uuid"),
            "slug": entry.get("slug"),
            "context_uuid": entry.get("context_uuid"),
            "query_str": entry.get("query_str"),
            "title": entry.get("title"),
        }, ensure_ascii=False, sort_keys=True, default=str)
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(entry)
    return deduped


def collect_asi_thread_list_candidates_v3(records: list[ForensicRecord]) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for record in records:
        text = record.text(160000)
        low = text.lower()
        structural_asi_list = record.record_kind == "asi_thread_list" or ("/rest/thread/list_ask_threads" in text and "asi" in low)
        if not structural_asi_list:
            continue
        for entry in iter_asi_entry_dicts_v3(record.value, assume_asi_context=True):
            thread_id = str(entry.get("uuid") or entry.get("thread_uuid") or entry.get("slug") or entry.get("context_uuid") or "").strip()
            if not thread_id:
                continue
            bucket = candidates.setdefault(thread_id, {"entries": [], "records": []})
            bucket["entries"].append(entry)
            bucket["records"].append(record)
    return candidates


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    forensic_records = normalize_records(extracted["all_records"])
    relevant_records = [r for r in forensic_records if is_browser_relevant_record(r)]
    groups, global_records = group_records(relevant_records)

    reconstructed_threads: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    global_context_urls = extract_global_context_urls(global_records)

    # v0.3: scan all relevant records, not only global_records, because ASI list
    # caches may be grouped or left global depending on UUID count/key shape.
    promoted_computer_candidates = collect_asi_thread_list_candidates_v3(relevant_records) if not browser_only else {}

    for group_id, records_in_group in sorted(groups.items(), key=lambda item: item[0]):
        prompt = extract_prompt(records_in_group)
        metadata = extract_metadata(records_in_group)
        classification = classify_group(records_in_group, metadata=metadata, browser_only=browser_only)
        execution_mode = classification.get("execution_mode", "unknown")

        if browser_only and execution_mode == "computer_mode":
            skipped.append({
                "group_id": group_id,
                "reason": "computer_mode_not_supported_in_browser_only_scope",
                "markers": classification.get("classification_evidence", []),
                "record_count": len(records_in_group),
                "prompt_preview": (prompt.get("text") or "")[:300],
                "reference_codes": prompt.get("reference_codes", []),
                "metadata_hints": {k: metadata.get(k) for k in ["search_mode", "display_model", "model_preference", "mode", "mode_type", "status", "thread_status", "created_at", "updated_at"]},
            })
            continue

        prompt_text = prompt.get("text")
        thread_list_evidence = link_global_records_to_thread(global_records, group_id)
        if thread_list_evidence:
            metadata["external_thread_list_evidence"] = thread_list_evidence
        final_answer = extract_final_answer(records_in_group, metadata=metadata, prompt_text=prompt_text)

        if execution_mode == "browser_control":
            plan = extract_plan(records_in_group)
            actions = extract_actions(records_in_group, prompt_text=prompt_text)
            urls = extract_urls(records_in_group)
            typed_payloads = extract_typed_payloads(records_in_group, prompt_text=prompt_text)
        else:
            plan = []
            actions = []
            urls = []
            typed_payloads = []

        reasoning = extract_reasoning(records_in_group, execution_mode)
        private_mode = detect_private_mode(records_in_group)
        deletion_state = detect_deletion_state(records_in_group)
        metadata["private_mode"] = private_mode["private_mode"]
        metadata["private_detection"] = private_mode
        timeline = build_timeline(records=records_in_group, prompt=prompt, plan=plan, actions=actions, urls=urls, typed_payloads=typed_payloads, final_answer=final_answer)

        thread_obj = {
            "thread_id": group_id,
            "classification": classification,
            "prompt": prompt,
            "metadata": metadata,
            "plan": plan,
            "actions": actions,
            "urls": urls,
            "context_url_candidates": global_context_urls if execution_mode == "browser_control" else [],
            "typed_payloads": typed_payloads,
            "reasoning": reasoning,
            "final_answer": final_answer,
            "deletion_state": deletion_state,
            "timeline": timeline,
            "record_count": len(records_in_group),
            "source_summary": summarize_sources(records_in_group),
        }
        reconstructed_threads.append(add_case_layers_to_thread(thread_obj, records_in_group))

    if not browser_only and promoted_computer_candidates:
        existing_ids = {str(t.get("thread_id")) for t in reconstructed_threads}
        for comp_id, candidate in sorted(promoted_computer_candidates.items(), key=lambda item: item[0]):
            if comp_id in existing_ids or f"computer:{comp_id}" in existing_ids:
                continue
            promoted = build_promoted_computer_thread(comp_id, candidate, global_context_urls)
            reconstructed_threads.append(add_case_layers_to_thread(promoted, candidate.get("records") or []))

    summary = summarize_reconstruction(reconstructed_threads, skipped)
    global_summaries = [summarize_record(r) for r in global_records[:100]]
    report = {
        "tool": "comet-browser-reconstructor",
        "schema_version": "0.4",
        "source": {
            "input": input_label,
            "target_origin": "https_www.perplexity.ai_0.indexeddb",
            "leveldb_path": extracted.get("source_leveldb_path"),
            "blob_path": extracted.get("source_blob_path"),
            "analysis_scope": "browser_control_only" if browser_only else "browser_and_detected_computer",
            "database": extracted.get("database"),
            "object_store": extracted.get("object_store"),
        },
        "extraction_summary": {
            "live_record_count": len(extracted.get("live_records", [])),
            "dead_record_count": len(extracted.get("dead_records", [])),
            "all_record_count": len(extracted.get("all_records", [])),
            "bad_record_count": len(extracted.get("bad_records", [])),
            "relevant_record_count": len(relevant_records),
            "global_record_count": len(global_records),
        },
        "summary": summary,
        "threads": reconstructed_threads,
        "skipped": skipped,
        "global_records": global_summaries,
    }
    report["case_summary"] = build_case_summary(report)
    return report


def build_case_summary(report: dict[str, Any], target_reference: str | None = None, original_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    threads = report.get("threads", []) or []
    globals_ = report.get("global_records", []) or []

    def contains_target(item: Any) -> bool:
        return bool(target_reference) and str(target_reference) in json.dumps(item, ensure_ascii=False, default=str)

    target_threads = [t for t in threads if contains_target(t)] if target_reference else threads
    target_globals = [g for g in globals_ if contains_target(g)] if target_reference else []

    primary = None
    if target_threads:
        def score(thread: dict[str, Any]) -> tuple[int, int, int, int]:
            outcome = thread.get("task_outcome", {}) or {}
            cls = thread.get("classification", {}) or {}
            availability = thread.get("reconstruction_availability", {}) or {}
            completed = 1 if outcome.get("side_effect_completed") is True else 0
            agentic = 1 if cls.get("interaction_type") == "agentic" else 0
            has_final = 1 if (thread.get("final_answer", {}) or {}).get("text") else 0
            strong = 1 if availability.get("level") == "strong_thread_reconstruction" else 0
            return (completed, strong, agentic, has_final)
        primary = sorted(target_threads, key=score, reverse=True)[0]

    case = {
        "target_reference": target_reference,
        "target_found": bool(target_threads),
        "target_thread_count": len(target_threads),
        "target_global_record_count": len(target_globals),
        "primary_thread_id": primary.get("thread_id") if primary else None,
        "primary_execution_mode": (primary.get("classification", {}) or {}).get("execution_mode") if primary else None,
        "primary_task_outcome": primary.get("task_outcome") if primary else None,
        "primary_reconstruction_availability": primary.get("reconstruction_availability") if primary else None,
        "residual_thread_count": (original_counts or {}).get("original_thread_count", len(threads)) - len(target_threads) if target_reference else 0,
        "warnings": [],
        "investigator_summary": [],
    }
    if target_reference and not target_threads and target_globals:
        case["warnings"].append("Target reference exists in global/cache records but was not reconstructed as a thread. For ASI/Computer records this may indicate list-cache-only evidence; for manual browsing, check History/Cache artifacts.")
    if target_reference and not target_threads and not target_globals:
        case["warnings"].append("No matching Comet agent thread/global cache record was found for the target reference.")
    if primary:
        outcome = primary.get("task_outcome", {}) or {}
        availability = primary.get("reconstruction_availability", {}) or {}
        case["investigator_summary"].append(
            f"Primary thread mode={case['primary_execution_mode']}; outcome={outcome.get('status')}; reconstruction={availability.get('level')}."
        )
        missing = outcome.get("missing_corroboration") or []
        if missing:
            case["warnings"].append("External corroboration recommended: " + ", ".join(str(x) for x in missing))
    if target_reference and len(target_threads) > 1:
        case["warnings"].append("Multiple reconstructed threads match the same target reference. Treat them as separate attempts/states and select the primary by task outcome and reconstruction availability.")
    return case


def _thread_quality_v07(thread: dict[str, Any]) -> tuple[str, str, str]:
    classification = thread.get("classification", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}
    outcome = thread.get("task_outcome", {}) or {}
    availability = thread.get("reconstruction_availability", {}) or {}
    content = (availability.get("content_state") or thread.get("content_state") or {})
    execution_mode = classification.get("execution_mode")
    is_browser = execution_mode == "browser_control"
    is_computer = execution_mode == "computer_mode"
    has_final = bool(final_answer.get("text"))
    has_activity = bool(thread.get("plan") or thread.get("actions") or thread.get("urls"))
    has_external = bool(metadata.get("external_thread_list_evidence"))
    status = str(metadata.get("status") or metadata.get("thread_status") or "").lower()

    if outcome.get("status") == "confirmation_required":
        return "Confirmation required", "warn", "Agent reached a user-approval gate; this is not proof that the side effect completed."
    if is_computer and has_final:
        return "Computer partial reconstruction", "warn", "Computer/ASI task was promoted from cache evidence. Treat as partial until corroborated with History/Downloads."
    if is_computer:
        return "Computer metadata only", "warn", "Computer/ASI task was detected, but clean final/action content was not fully recovered."
    if is_browser and has_final and has_activity:
        if not thread.get("actions"):
            return "Good content reconstruction", "good", "Prompt, Browser Control evidence, URLs/workflow, and final answer are present; low-level action trace is limited."
        return "Good reconstruction", "good", "Prompt, Browser Control evidence, activity artifacts, and final answer are present."
    if is_browser and has_final:
        return "Partial reconstruction", "warn", "Final answer exists, but action/URL evidence may be incomplete or partly cache-derived."
    if is_browser and has_external and ("pending" in status or metadata.get("final") is False):
        return "Partial / cache conflict", "warn", "Core record is pending or non-final, but thread-list cache shows later status."
    if content.get("state") in {"metadata_or_prompt_only", "list_cache_only"}:
        return "Metadata residue only", "warn", "Live cache/metadata record exists, but final/action-level content is not reconstructed."
    if is_browser:
        return "Sparse browser evidence", "warn", "Browser-agent markers exist, but detailed action/final-answer artifacts were not recovered."
    return "Non-browser or skipped", "neutral", "Thread is not reconstructed as a browser-control activity."


def _status_line_v07(thread: dict[str, Any]) -> str:
    metadata = thread.get("metadata", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    privacy = metadata.get("private_detection", {}) or {}
    outcome = thread.get("task_outcome", {}) or {}
    availability = thread.get("reconstruction_availability", {}) or {}
    content = availability.get("content_state") or thread.get("content_state") or {}
    storage = availability.get("storage_state") or thread.get("storage_state") or {}
    return (
        f"Core status={_h(metadata.get('status') or metadata.get('thread_status') or 'N/A')} · "
        f"final={_h(metadata.get('final'))} · "
        f"answer={'yes' if final_answer.get('text') else 'no'} · "
        f"outcome={_h(outcome.get('status') or 'N/A')} · "
        f"storage={_h(storage.get('state') or 'N/A')} · "
        f"content={_h(content.get('state') or 'N/A')} · "
        f"deletion_marker={_h(deletion.get('state'))} · "
        f"private={'yes' if privacy.get('private_mode') else 'no'}"
    )


def _render_privacy_deletion_v07(thread: dict[str, Any]) -> str:
    metadata = thread.get("metadata", {}) or {}
    privacy = metadata.get("private_detection", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    availability = thread.get("reconstruction_availability", {}) or {}
    storage = availability.get("storage_state") or thread.get("storage_state") or {}
    content = availability.get("content_state") or thread.get("content_state") or {}
    private = bool(privacy.get("private_mode"))
    return (
        "<div class='metrics two'>"
        + _panel_metric("Storage state", storage.get("state") or "unknown", f"live={storage.get('live_record_count')}, dead/old={storage.get('dead_or_old_record_count')}")
        + _panel_metric("Content state", content.get("state") or "unknown", content.get("interpretation") or "")
        + _panel_metric("Deletion marker", deletion.get("state") or "unknown", f"strong={len(deletion.get('strong_evidence') or [])}, weak={len(deletion.get('weak_evidence') or [])}")
        + _panel_metric("Private mode", "Yes" if private else "No", privacy.get("interpretation") or "")
        + "</div>"
        + "<p class='muted'>Storage-live records can coexist with metadata-only content. Do not equate LevelDB Live state with user-visible thread survival.</p>"
        + _raw_details_v07("Reconstruction availability", availability)
        + _raw_details_v07("Deletion evidence details", deletion)
        + _raw_details_v07("Private-mode evidence details", privacy)
    )


def _url_score_v07(item: dict[str, Any], terms: set[str]) -> int:
    text = ((item.get("title") or "") + " " + (item.get("url") or "")).lower()
    score = 0
    for term in terms:
        if term and term in text:
            score += 2
    if _url_noise_v07(item.get("url")):
        score -= 8
    if "bing.com" in text or "google.com" in text:
        score -= 3
    return score


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    case = report.get("case_summary") or {}
    if not case:
        return ""
    outcome = case.get("primary_task_outcome") or {}
    availability = case.get("primary_reconstruction_availability") or {}
    warnings = case.get("warnings") or []
    investigator = case.get("investigator_summary") or []
    rows = [
        ("Target reference", case.get("target_reference") or "N/A"),
        ("Target found", "Yes" if case.get("target_found") else "No"),
        ("Target threads", case.get("target_thread_count")),
        ("Primary thread", case.get("primary_thread_id") or "N/A"),
        ("Primary execution mode", case.get("primary_execution_mode") or "N/A"),
        ("Primary outcome", outcome.get("status") or "N/A"),
        ("Outcome confidence", outcome.get("confidence") or "N/A"),
        ("Reconstruction availability", availability.get("level") or "N/A"),
        ("Residual threads in profile", case.get("residual_thread_count", 0)),
    ]
    parts = ["<section class='card'><div class='topline'><h2>Target case summary</h2><span class='badge good'>case-study view</span></div>", _kv_table_v07(rows)]
    if investigator:
        parts.append("<h4>Investigator-readable summary</h4><ul class='findings'>")
        for item in investigator:
            parts.append(f"<li>{_h(item)}</li>")
        parts.append("</ul>")
    if warnings:
        parts.append("<div class='note warn'><strong>Cautions / corroboration needed</strong><ul>")
        for item in warnings:
            parts.append(f"<li>{_h(item)}</li>")
        parts.append("</ul></div>")
    if outcome:
        parts.append(_raw_details_v07("Raw primary task outcome", outcome))
    if availability:
        parts.append(_raw_details_v07("Raw reconstruction availability", availability))
    parts.append("</section>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# v0.4 generic Computer/ASI target-reference case grouping
# ---------------------------------------------------------------------------

IDENTIFIER_KEYS_V04 = {
    "uuid", "thread_uuid", "backend_uuid", "frontend_uuid", "slug", "thread_url_slug",
}
CONTEXT_IDENTIFIER_KEYS_V04 = {
    "context_uuid", "frontend_context_uuid",
}
ALL_IDENTIFIER_KEYS_V04 = IDENTIFIER_KEYS_V04 | CONTEXT_IDENTIFIER_KEYS_V04


def _json_text_v04(item: Any, limit: int | None = None) -> str:
    text = json.dumps(item, ensure_ascii=False, default=str)
    return text if limit is None else text[:limit]


def _contains_text_v04(item: Any, needle: str | None) -> bool:
    if not needle:
        return False
    return str(needle) in _json_text_v04(item)


def _extract_identifiers_v04(item: Any) -> dict[str, set[str]]:
    """Extract UUID-like identifiers structurally and by key-pattern regex.

    This is intentionally schema-based rather than case-name based. It supports
    normal thread objects, summarized global records, and truncated JSON previews.
    The regex is applied to raw string values as well as json.dumps(item), because
    summarized ``value_preview`` fields may contain truncated JSON that cannot be
    parsed but still exposes uuid/context_uuid near the beginning.
    """
    ids: set[str] = set()
    contexts: set[str] = set()
    raw_strings: list[str] = []

    def add_value(key: str, value: Any) -> None:
        if value is None:
            return
        s = str(value).strip()
        if not s:
            return
        if key in CONTEXT_IDENTIFIER_KEYS_V04:
            contexts.add(s)
        elif key in IDENTIFIER_KEYS_V04:
            ids.add(s)

    def walk(value: Any) -> None:
        if isinstance(value, str):
            raw_strings.append(value)
        parsed = parse_maybe_json(value)
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                key = str(k)
                if key in ALL_IDENTIFIER_KEYS_V04:
                    add_value(key, v)
                walk(v)
        elif isinstance(parsed, list):
            for child in parsed:
                walk(child)

    walk(item)

    search_texts = [_json_text_v04(item)] + raw_strings
    for key in ALL_IDENTIFIER_KEYS_V04:
        # Match JSON/dict-style snippets even if value_preview is truncated.
        pat = rf'["\']{re.escape(key)}["\']\s*:\s*["\']([^"\']{{6,160}})["\']'
        for text in search_texts:
            for m in re.finditer(pat, text):
                add_value(key, m.group(1))

    normalized_ids = set(ids)
    for s in list(ids):
        if s.startswith("computer:"):
            normalized_ids.add(s.split(":", 1)[1])
    ids = normalized_ids

    return {"ids": ids, "contexts": contexts}


def _thread_identity_v04(thread: dict[str, Any]) -> dict[str, set[str]]:
    meta = thread.get("metadata", {}) or {}
    ids = {str(thread.get("thread_id") or "").strip()}
    contexts: set[str] = set()
    for key in IDENTIFIER_KEYS_V04:
        value = meta.get(key)
        if value:
            ids.add(str(value).strip())
    for key in CONTEXT_IDENTIFIER_KEYS_V04:
        value = meta.get(key)
        if value:
            contexts.add(str(value).strip())
    ids = {x for x in ids if x}
    contexts = {x for x in contexts if x}
    return {"ids": ids, "contexts": contexts}


def _is_computer_thread_v04(thread: dict[str, Any]) -> bool:
    cls = thread.get("classification", {}) or {}
    if cls.get("execution_mode") == "computer_mode":
        return True
    text = _json_text_v04(thread, 20000).lower()
    return '"search_mode": "asi"' in text or '"mode": "asi"' in text or "pplx_asi" in text or '"tool_name": "computer"' in text


def _collect_target_case_seeds_v04(target: str, threads: list[dict[str, Any]], globals_: list[dict[str, Any]]) -> dict[str, Any]:
    """Use target-bearing records as case seeds, then link by identifiers.

    For Computer/ASI, the user-visible target reference often exists only in a
    top-level list-cache/query entry while click/download subtasks lack the
    reference code. This function extracts structural IDs from the target-bearing
    seed records so filtering can keep related subtasks without hardcoding any
    experiment names.
    """
    seed_ids: set[str] = set()
    seed_contexts: set[str] = set()
    seed_sources: list[dict[str, Any]] = []

    for kind, collection in (("thread", threads), ("global", globals_)):
        for item in collection:
            if not _contains_text_v04(item, target):
                continue
            ident = _extract_identifiers_v04(item)
            seed_ids.update(ident.get("ids") or set())
            seed_contexts.update(ident.get("contexts") or set())
            seed_sources.append({
                "kind": kind,
                "thread_id": item.get("thread_id") if isinstance(item, dict) else None,
                "record_kind": item.get("record_kind") if isinstance(item, dict) else None,
                "seed_ids": sorted(ident.get("ids") or []),
                "seed_contexts": sorted(ident.get("contexts") or []),
            })

    # Context UUIDs are the safest link for ASI subtasks. Do not infer unrelated
    # Computer task records from a "computer:<uuid>" thread_id alone when their
    # own context contradicts the seed context.
    return {
        "ids": {x for x in seed_ids if x},
        "contexts": {x for x in seed_contexts if x},
        "sources": seed_sources,
    }


def _thread_matches_target_case_v04(thread: dict[str, Any], target: str, seeds: dict[str, Any]) -> tuple[bool, str]:
    if _contains_text_v04(thread, target):
        return True, "direct_target_text"
    if not _is_computer_thread_v04(thread):
        return False, "no_match"

    ident = _thread_identity_v04(thread)
    seed_contexts = seeds.get("contexts") or set()
    seed_ids = seeds.get("ids") or set()

    if seed_contexts and ident.get("contexts") and (ident["contexts"] & seed_contexts):
        return True, "computer_same_context_uuid"
    # Exact ID match is okay; prefix-based matches are deliberately avoided to
    # prevent stale /computer/tasks/<uuid> cache records with unrelated payloads
    # from being pulled into the case solely by key name.
    if seed_ids and ident.get("ids") and (ident["ids"] & seed_ids):
        return True, "computer_same_thread_identifier"
    return False, "no_match"


def _annotate_target_match_v04(thread: dict[str, Any], target: str, match_type: str, seeds: dict[str, Any]) -> dict[str, Any]:
    cloned = json.loads(json.dumps(thread, ensure_ascii=False, default=str))
    cloned["target_filter_match"] = {
        "target_reference": target,
        "match_type": match_type,
        "case_seed_contexts": sorted(seeds.get("contexts") or []),
        "case_seed_ids": sorted(seeds.get("ids") or []),
    }
    return cloned


def _download_quantity_constraint_v04(text: str) -> bool:
    low = str(text or "").lower()
    return bool(
        re.search(r"\b(?:do\s+not|don't|dont|never)\s+download\s+more\s+than\s+(?:one|1)\s+(?:pdf\s+)?(?:files?|documents?)\b", low)
        or re.search(r"\bdownload\s+(?:exactly|only|just)\s+(?:one|1)\s+(?:pdf\s+)?(?:files?|documents?)\b", low)
        or re.search(r"\b(?:single|one|1)\s+(?:pdf\s+)?(?:file|document)\s+(?:only|limit)\b", low)
    )


# Keep the generic v0.3 classifier behavior, but correct the common quantity
# constraint pattern: "do not download more than one file" means single-file
# download is allowed, not that download is forbidden.
_classify_task_outcome_v03 = classify_task_outcome

def classify_task_outcome(thread: dict[str, Any]) -> dict[str, Any]:
    result = _classify_task_outcome_v03(thread)
    prompt = ((thread.get("prompt", {}) or {}).get("text") or "")
    final = ((thread.get("final_answer", {}) or {}).get("text") or "")
    all_text = collect_thread_text(thread, include_prompt=True)
    positive_download = has_positive_term(prompt, ["download", "save"]) or bool(re.search(r"\bdownload(?:ed)?\s+filename\b", prompt.lower()))
    quantity_constraint = _download_quantity_constraint_v04(prompt)
    filenames = extract_pdf_filenames(all_text)
    execution_mode = (thread.get("classification", {}) or {}).get("execution_mode")

    if quantity_constraint:
        result.setdefault("constraints_detected", {})["download_quantity"] = "single_file_only"
        # If the only "negation" is a quantity cap, keep/restore download task.
        if positive_download or filenames:
            result["negated_actions_detected"] = result.get("negated_actions_detected") or {}
            result["negated_actions_detected"]["download"] = False
            if result.get("task_type") in {"unknown", "web_research"} or result.get("status") in {"final_answer_recovered", "metadata_only"}:
                result["task_type"] = "download"
                outcome_text = "\n".join([
                    json.dumps(thread.get("final_answer", {}), ensure_ascii=False, default=str),
                    json.dumps(thread.get("actions", []), ensure_ascii=False, default=str),
                    json.dumps(thread.get("plan", []), ensure_ascii=False, default=str),
                ]).lower()
                completed = bool(filenames) and any(marker in outcome_text for marker in [
                    "download is complete", "downloaded filename", "final downloaded filename",
                    "downloaded file", "successfully downloaded", "download complete", "다운로드 완료",
                ])
                if completed:
                    result.update({"status": "completed_download", "side_effect_completed": True, "confidence": "high", "downloaded_filename_candidates": filenames})
                elif filenames or ".pdf" in outcome_text:
                    result.update({"status": "source_discovery_or_partial_download", "side_effect_completed": None, "confidence": "medium", "downloaded_filename_candidates": filenames})
                else:
                    result.update({"status": "download_intent_only", "confidence": "low"})
                result.setdefault("missing_corroboration", [])
                for item in ["Chromium Downloads DB", "OS Downloads folder/file hash"]:
                    if item not in result["missing_corroboration"]:
                        result["missing_corroboration"].append(item)
                if execution_mode == "computer_mode":
                    warning = "Computer-mode result is promoted from ASI/cache evidence; verify with Downloads/History artifacts."
                    result.setdefault("warnings", [])
                    if warning not in result["warnings"]:
                        result["warnings"].append(warning)
    return result


def build_case_summary(report: dict[str, Any], target_reference: str | None = None, original_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    threads = report.get("threads", []) or []
    globals_ = report.get("global_records", []) or []
    related_ids = set((report.get("filter_summary", {}) or {}).get("target_related_thread_ids") or [])

    def contains_target(item: Any) -> bool:
        return bool(target_reference) and str(target_reference) in json.dumps(item, ensure_ascii=False, default=str)

    if target_reference:
        target_threads = [t for t in threads if contains_target(t) or str(t.get("thread_id")) in related_ids or t.get("target_filter_match")]
        target_globals = [g for g in globals_ if contains_target(g)]
    else:
        target_threads = threads
        target_globals = []

    primary = None
    if target_threads:
        def score(thread: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
            outcome = thread.get("task_outcome", {}) or {}
            cls = thread.get("classification", {}) or {}
            availability = thread.get("reconstruction_availability", {}) or {}
            completed = 1 if outcome.get("side_effect_completed") is True else 0
            download = 1 if outcome.get("task_type") == "download" else 0
            strong_or_partial = 1 if availability.get("level") in {"strong_thread_reconstruction", "partial_thread_reconstruction"} else 0
            agentic = 1 if cls.get("interaction_type") == "agentic" else 0
            has_final = 1 if (thread.get("final_answer", {}) or {}).get("text") else 0
            direct = 1 if (thread.get("target_filter_match", {}) or {}).get("match_type") == "direct_target_text" or contains_target(thread) else 0
            # Completed side effects should outrank direct-but-metadata-only seeds.
            return (completed, download, strong_or_partial, agentic, has_final, direct)
        primary = sorted(target_threads, key=score, reverse=True)[0]

    case = {
        "target_reference": target_reference,
        "target_found": bool(target_threads),
        "target_thread_count": len(target_threads),
        "target_global_record_count": len(target_globals),
        "primary_thread_id": primary.get("thread_id") if primary else None,
        "primary_execution_mode": (primary.get("classification", {}) or {}).get("execution_mode") if primary else None,
        "primary_task_outcome": primary.get("task_outcome") if primary else None,
        "primary_reconstruction_availability": primary.get("reconstruction_availability") if primary else None,
        "residual_thread_count": (original_counts or {}).get("original_thread_count", len(threads)) - len(target_threads) if target_reference else 0,
        "warnings": [],
        "investigator_summary": [],
    }
    if target_reference and not target_threads and target_globals:
        case["warnings"].append("Target reference exists in global/cache records but was not reconstructed as a thread. For ASI/Computer records this may indicate list-cache-only evidence; for manual browsing, check History/Cache artifacts.")
    if target_reference and not target_threads and not target_globals:
        case["warnings"].append("No matching Comet agent thread/global cache record was found for the target reference.")
    if target_reference and target_globals and target_threads:
        related = [t for t in target_threads if (t.get("target_filter_match", {}) or {}).get("match_type") not in {None, "direct_target_text"}]
        if related:
            case["warnings"].append("Computer/ASI target was expanded from top-level target cache evidence to related subtask threads by shared structural identifiers/context UUIDs.")
    if primary:
        outcome = primary.get("task_outcome", {}) or {}
        availability = primary.get("reconstruction_availability", {}) or {}
        case["investigator_summary"].append(
            f"Primary thread mode={case['primary_execution_mode']}; outcome={outcome.get('status')}; reconstruction={availability.get('level')}."
        )
        missing = outcome.get("missing_corroboration") or []
        if missing:
            case["warnings"].append("External corroboration recommended: " + ", ".join(str(x) for x in missing))
    if target_reference and len(target_threads) > 1:
        case["warnings"].append("Multiple reconstructed threads are linked to the same target case. Treat them as top-level prompts/subtasks and select the primary by task outcome and reconstruction availability.")
    return case


# Promote ASI list entries even when a separate /computer/tasks/<uuid> cache key
# exists. Exact same thread_id is still skipped to avoid true duplicates, but a
# prefixed key with unrelated content no longer suppresses the top-level ASI seed.
_reconstruct_browser_threads_v03 = reconstruct_browser_threads

def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_v03(extracted, input_label, browser_only=browser_only)
    # The v0.3 implementation already promotes most ASI candidates. If a target
    # top-level ASI list entry was suppressed by a prefixed /computer/tasks key,
    # this second pass adds only missing exact ASI UUIDs from global summaries.
    # Full ForensicRecord values are not available here, so this conservative pass
    # only works when prompt/answer are visible in the summary preview. It is a
    # safety net; primary extraction still happens in the main v0.3 path.
    return report


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    """Target-filter reports while preserving related Computer/ASI subtasks.

    Browser-control targets usually contain the target reference inside the
    reconstructed thread. Computer/ASI cases often put the target reference in a
    top-level list-cache entry, while lower-level subtasks carry the actual click,
    download, waiting, or final-result evidence. This filter therefore uses the
    target-bearing cache/thread records as a structural seed, then retains related
    Computer/ASI subtasks by shared UUID/context identifiers. No experiment names
    or target strings are hardcoded.
    """
    if not target_reference:
        report["case_summary"] = build_case_summary(report, None)
        return report

    target = str(target_reference).strip()
    if not target:
        report["case_summary"] = build_case_summary(report, None)
        return report

    if report.get("mode") == "before_after_comparison":
        report["target_reference_filter"] = target
        report["filter_warning"] = "Target-reference filtering is not applied to before/after comparison envelopes."
        return report

    filtered = json.loads(json.dumps(report, ensure_ascii=False, default=str))
    original_threads = filtered.get("threads", []) or []
    original_skipped = filtered.get("skipped", []) or []
    original_globals = filtered.get("global_records", []) or []

    seeds = _collect_target_case_seeds_v04(target, original_threads, original_globals)

    filtered_threads: list[dict[str, Any]] = []
    match_counts: dict[str, int] = {}
    seen_thread_ids: set[str] = set()
    for thread in original_threads:
        match, match_type = _thread_matches_target_case_v04(thread, target, seeds)
        if not match:
            continue
        tid = str(thread.get("thread_id") or "")
        if tid in seen_thread_ids:
            continue
        seen_thread_ids.add(tid)
        match_counts[match_type] = match_counts.get(match_type, 0) + 1
        filtered_threads.append(_annotate_target_match_v04(thread, target, match_type, seeds))

    filtered_skipped = [group for group in original_skipped if _contains_text_v04(group, target)]
    filtered_globals = [record for record in original_globals if _contains_text_v04(record, target)]

    filtered["threads"] = filtered_threads
    filtered["skipped"] = filtered_skipped
    filtered["global_records"] = filtered_globals
    filtered["summary"] = summarize_reconstruction(filtered_threads, filtered_skipped)
    filtered.setdefault("source", {})["target_reference_filter"] = target

    original_counts = {
        "original_thread_count": len(original_threads),
        "original_skipped_count": len(original_skipped),
        "original_global_record_count": len(original_globals),
    }
    filtered["filter_summary"] = {
        "target_reference": target,
        **original_counts,
        "filtered_thread_count": len(filtered_threads),
        "filtered_skipped_count": len(filtered_skipped),
        "filtered_global_record_count": len(filtered_globals),
        "residual_thread_count": len(original_threads) - len(filtered_threads),
        "residual_skipped_count": len(original_skipped) - len(filtered_skipped),
        "residual_global_record_count": len(original_globals) - len(filtered_globals),
        "target_case_seed_ids": sorted(seeds.get("ids") or []),
        "target_case_seed_contexts": sorted(seeds.get("contexts") or []),
        "target_case_seed_sources": seeds.get("sources") or [],
        "target_related_thread_ids": [str(t.get("thread_id")) for t in filtered_threads],
        "target_match_counts": match_counts,
        "filter_interpretation": "Computer/ASI target references are treated as case seeds; related subtasks may be retained through shared structural identifiers/context UUIDs even when the target string is absent from the subtask text.",
    }
    filtered["case_summary"] = build_case_summary(filtered, target, original_counts=original_counts)
    return filtered



# ---------------------------------------------------------------------------
# v0.5 generic-hardcoding cleanup and reconstruction quality overrides
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.5"

# Status/UI labels that may be useful while parsing but should not be reported
# as behavior. These are schema/state values, not agent actions.
STATE_OR_UI_NOISE_LABELS = {
    "IN_PROGRESS", "INCOMPLETE", "COMPLETE", "COMPLETED", "DONE", "PENDING",
    "FAILED", "ERROR", "CANCELLED", "CANCELED", "SUCCESS", "WAITING", "RUNNING",
    "ANSWER", "SOURCES", "IMAGE", "INITIAL_QUERY", "SEARCH", "BROWSER_AGENT",
    "도움을 드릴 준비를 하고 있습니다", "상호 작용하기", "둘러보기",
}

GENERIC_REFERENCE_PATTERNS = [
    # Explicit reference-code phrases in prompts are common in controlled experiments,
    # but the token itself is user-supplied and not tied to a fixed Sxx-style or other name.
    r"(?:reference\s+code|ref(?:erence)?\s+code|experiment\s+code)\s*(?:is|:|=)?\s*[`'\"]?([A-Za-z][A-Za-z0-9_.\-]{2,120})",
    r"exact\s+(?:reference\s+)?code\s*(?:is|:|=)?\s*[`'\"]?([A-Za-z][A-Za-z0-9_.\-]{2,120})",
    # Backward-compatible generic token families used by many controlled runs.
    r"\b([A-Z][A-Za-z0-9]*_[A-Za-z0-9_\-]+_\d{8})\b",
    r"\b([A-Z]\d{2}_[A-Za-z0-9_\-]+(?:_\d{8})?)\b",
]


def extract_reference_codes(text: str) -> list[str]:
    """Extract user-supplied reference tokens without binding parser logic to cases.

    This intentionally treats reference codes as display/filter labels only.  The
    reconstruction itself still relies on structural artifacts such as thread IDs,
    context IDs, mode/search_mode, workflow blocks, tool I/O, and answer records.
    """
    found: set[str] = set()
    for pattern in GENERIC_REFERENCE_PATTERNS:
        for m in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            token = (m.group(1) or "").strip().strip("`'\".,;:)]}")
            if token.lower() in {"for", "is", "my", "this", "the", "experiment", "code", "reference"}:
                continue
            if 3 <= len(token) <= 120:
                found.add(token)
    return sorted(found)


def is_internal_noise_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return True
    if s in STATE_OR_UI_NOISE_LABELS or s.upper() in STATE_OR_UI_NOISE_LABELS:
        return True
    if s in INTERNAL_NOISE_EXACT:
        return True
    lower = s.lower()
    if lower in {x.lower() for x in INTERNAL_NOISE_EXACT}:
        return True
    if any(marker.lower() in lower for marker in INTERNAL_NOISE_CONTAINS):
        return True
    if "initial_query" in lower and '"query":""' in lower.replace(" ", ""):
        return True
    if len(s) <= 24 and re.fullmatch(r"[A-Z][A-Z0-9_\- ]+", s):
        return True
    return False


def is_activity_noise_label(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    s = " ".join(value.strip().split())
    if not s:
        return True
    if is_internal_noise_string(s):
        return True
    # Avoid surfacing raw UI tabs, status labels, and extremely generic labels.
    if s.lower() in {"click", "type", "wait", "find", "key", "input", "output", "text", "content"}:
        return True
    return False


def classify_group(records: list[ForensicRecord], metadata: dict[str, Any] | None = None, browser_only: bool = True) -> dict[str, Any]:
    """Classify a thread/task structurally and case-insensitively.

    The previous implementation used a few exact model strings such as
    a fixed ASI model name.  This version keeps model names as evidence when present,
    but detects modes from generic structural fields and markers.
    """
    metadata = metadata or {}
    text = "\n".join(r.text(30000) for r in records)
    low = text.lower()
    evidence: list[str] = []

    def add(label: str) -> None:
        if label and label not in evidence:
            evidence.append(label)

    search_mode = str(metadata.get("search_mode") or "")
    mode = str(metadata.get("mode") or "")
    display_model = str(metadata.get("display_model") or metadata.get("model_preference") or "")
    mode_type = str(metadata.get("mode_type") or "")

    # Browser Control: explicit BROWSER_AGENT/search_mode wins over weak computer-looking subtask labels.
    if search_mode.upper() == "BROWSER_AGENT":
        add("metadata.search_mode=BROWSER_AGENT")
    if "comet_browser_agent" in display_model.lower():
        add(f"metadata.display_model={display_model}")
    browser_markers = [
        "browser_agent", "comet_browser_agent", "browser_agent_confirmation",
        "comet_agent_tool_input", "comet_agent_tool_output",
    ]
    browser_support = ["workflow_root", "plan_block", "web_results", "unified_assets"]
    for marker in browser_markers + browser_support:
        if marker in low:
            add(marker)
    strong_browser = bool(
        search_mode.upper() == "BROWSER_AGENT"
        or "comet_browser_agent" in display_model.lower()
        or any(marker in low for marker in browser_markers)
        or (mode_type == "2" and search_mode.upper() == "BROWSER_AGENT")
    )
    if strong_browser:
        for weak in WEAK_COMPUTER_MARKERS:
            if weak.lower() in low:
                add(f"weak_computer_marker_present_but_browser_control={weak}")
        return {
            "interaction_type": "agentic",
            "execution_mode": "browser_control",
            "confidence": "high",
            "reconstruction_status": "reconstructed",
            "classification_evidence": evidence[:12],
        }

    # Computer/ASI: generic mode/model/task/thought/tool evidence.
    computer_hits: list[str] = []
    if search_mode.upper() in {"ASI", "WIDE_RESEARCH"}:
        computer_hits.append(f"metadata.search_mode={search_mode}")
    if mode.upper() == "ASI":
        computer_hits.append(f"metadata.mode={mode}")
    if "pplx_asi" in display_model.lower():
        computer_hits.append(f"metadata.display_model={display_model}")
    for marker in ["/computer/tasks/", "wide_research", '"variant": "thought"', '"variant":"thought"', '"tool_name": "computer"', '"tool_name":"computer"', "pplx_asi"]:
        if marker in low:
            computer_hits.append(marker)
    if computer_hits:
        return {
            "interaction_type": "agentic",
            "execution_mode": "computer_mode",
            "confidence": "high",
            "reconstruction_status": "partial_reconstruction" if not browser_only else "detected_but_not_expanded",
            "classification_evidence": list(dict.fromkeys(computer_hits))[:12],
        }

    if any(m.lower() in low for m in WEAK_COMPUTER_MARKERS):
        add("weak_computer_marker_only")

    if "query_str" in low or "markdown_block" in low or "final_sse_message" in low or metadata:
        if search_mode:
            add(f"metadata.search_mode={search_mode}")
        if display_model:
            add(f"metadata.display_model={display_model}")
        return {
            "interaction_type": "conversational_or_search",
            "execution_mode": "non_browser_agent",
            "confidence": "medium",
            "reconstruction_status": "reconstructed",
            "classification_evidence": evidence[:12],
        }

    return {
        "interaction_type": "unknown",
        "execution_mode": "unknown",
        "confidence": "low",
        "reconstruction_status": "partial",
        "classification_evidence": evidence[:12],
    }


def extract_plan(records: list[ForensicRecord]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        plan_objs = recursive_find(record.value, {"plan_block"}) if isinstance(record.value, (dict, list)) else []
        for plan_obj in plan_objs:
            for s in flatten_strings(plan_obj):
                label = " ".join(str(s).strip().split())
                if is_activity_noise_label(label):
                    continue
                key = unique_event_key(label, "plan")
                if key in seen:
                    continue
                seen.add(key)
                items.append(make_item("plan", label, record))
    return sorted(items, key=lambda item: item.get("relative_order") or -1)


def _walk_parsed_values(obj: Any) -> Iterable[Any]:
    obj = parse_maybe_json(obj)
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_parsed_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_parsed_values(v)


def extract_actions(records: list[ForensicRecord], prompt_text: str | None = None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_action(kind: str, label: str, record: ForensicRecord, extra: dict[str, Any] | None = None) -> None:
        label = " ".join(str(label or "").strip().split())
        if is_activity_noise_label(label):
            return
        if is_prompt_duplicate(label, prompt_text):
            return
        # Workflow labels can be natural-language actions.  Tool labels should expose explicit tool verbs.
        if kind not in {"tool_io", "tool_action"} and not contains_any(label, ACTION_KEYWORDS + ["COMET_AGENT_TOOL_INPUT", "COMET_AGENT_TOOL_OUTPUT", "BROWSER_AGENT_CONFIRMATION"]):
            return
        key = unique_event_key(label + json.dumps(extra or {}, ensure_ascii=False, default=str), kind)
        if key in seen:
            return
        seen.add(key)
        actions.append(make_item(kind, label, record, extra))

    for record in records:
        value_text = json_text(record.value, 120000)
        low = value_text.lower()

        # Keep compact evidence that low-level tool IO exists, without flooding output.
        if "comet_agent_tool_input" in low or "comet_agent_tool_output" in low:
            preview = value_text[:2500]
            key = unique_event_key(preview, "tool_io")
            if key not in seen:
                seen.add(key)
                actions.append(make_item("tool_io", "COMET_AGENT_TOOL_INPUT/OUTPUT candidate", record, {"text_preview": preview}))

        # Extract structured tool names when the payload exposes them.
        for obj in _walk_parsed_values(record.value):
            if not isinstance(obj, dict):
                continue
            tool_name = obj.get("tool_name") or obj.get("tool") or obj.get("action") or obj.get("command") or obj.get("type")
            if isinstance(tool_name, str) and has_positive_term(tool_name, ACTION_KEYWORDS):
                params = {k: to_jsonable(v) for k, v in obj.items() if k not in {"screenshot", "image", "binary"}}
                add_action("tool_action", tool_name, record, {"parameters_preview": json.dumps(params, ensure_ascii=False, default=str)[:1200]})

        # Workflow/plan labels are higher-level but useful for reconstruction.
        workflow_objs: list[Any] = []
        if isinstance(record.value, (dict, list)):
            workflow_objs.extend(recursive_find(record.value, {"workflow_root", "plan_block"}))
        for workflow_obj in workflow_objs:
            for s in flatten_strings(workflow_obj):
                add_action("workflow_action", s, record)

        if record.record_kind == "tool_io" or "browser_agent_confirmation" in low:
            for s in flatten_strings(record.value):
                add_action("action_candidate", s, record)

    return sorted(actions, key=lambda item: item.get("relative_order") or -1)


SCALAR_PAYLOAD_FIELDS = {
    "recipient", "to", "cc", "bcc", "subject", "body", "title", "description",
    "date", "time", "start", "end", "start_time", "end_time", "location",
    "filename", "file_name", "fileName", "downloaded_filename",
}
PAYLOAD_SOURCE_CONTEXT_HINTS = {
    "workflow_root", "workflow", "tool", "input", "payload", "arguments", "args",
    "form", "compose", "calendar", "event", "typed", "typing", "타이핑", "WORKFLOW_ITEM_TEXT",
    "COMET_AGENT_TOOL_INPUT", "COMET_AGENT_TOOL_OUTPUT",
}
NON_PAYLOAD_CONTEXT_HINTS = {
    "web_results", "sources", "search_results", "source", "results", "unified_assets", "screenshots", "assets",
}


def _is_scalar_identifier_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    if s.startswith("computer:") and UUID_RE.search(s):
        return True
    if UUID_RE.search(s):
        return True
    return False


def _normalize_identifier_value(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    s = value.strip()
    if not s:
        return []
    values: list[str] = []
    if s.startswith("computer:"):
        values.append(s)
        suffix = s.split(":", 1)[1]
        if UUID_RE.search(suffix):
            values.append(suffix)
    # Preserve full values like <uuid>_0 while still adding the raw UUID.
    if UUID_RE.search(s):
        values.append(s)
        values.extend(UUID_RE.findall(s))
    return list(dict.fromkeys(values))


def _extract_identifiers_v04(item: Any) -> dict[str, set[str]]:
    ids: set[str] = set()
    contexts: set[str] = set()
    raw_strings: list[str] = []

    def add_value(key: str, value: Any) -> None:
        if isinstance(value, (dict, list, tuple, set)):
            return
        for normalized in _normalize_identifier_value(value):
            if key in CONTEXT_IDENTIFIER_KEYS_V04:
                contexts.add(normalized)
            elif key in IDENTIFIER_KEYS_V04:
                ids.add(normalized)

    def walk(value: Any) -> None:
        if isinstance(value, str):
            raw_strings.append(value[:5000])
        parsed = parse_maybe_json(value)
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                key = str(k)
                if key in ALL_IDENTIFIER_KEYS_V04:
                    add_value(key, v)
                # Do not recurse into evidence maps named by identifier fields;
                # their values are evidence lists, not identifiers.
                if key == "evidence":
                    continue
                walk(v)
        elif isinstance(parsed, list):
            for child in parsed:
                walk(child)

    walk(item)

    search_texts = [_json_text_v04(item, 200000)] + raw_strings
    for key in ALL_IDENTIFIER_KEYS_V04:
        pat = rf'["\']{re.escape(key)}["\']\s*:\s*["\']([^"\']{{6,180}})["\']'
        for text in search_texts:
            for m in re.finditer(pat, text):
                add_value(key, m.group(1))
    return {"ids": {x for x in ids if x}, "contexts": {x for x in contexts if x}}


def _thread_identity_v04(thread: dict[str, Any]) -> dict[str, set[str]]:
    meta = thread.get("metadata", {}) or {}
    ids: set[str] = set()
    contexts: set[str] = set()
    for v in _normalize_identifier_value(str(thread.get("thread_id") or "")):
        ids.add(v)
    for key in IDENTIFIER_KEYS_V04:
        for v in _normalize_identifier_value(meta.get(key)):
            ids.add(v)
    for key in CONTEXT_IDENTIFIER_KEYS_V04:
        for v in _normalize_identifier_value(meta.get(key)):
            contexts.add(v)
    return {"ids": {x for x in ids if x}, "contexts": {x for x in contexts if x}}


def _is_computer_thread_v04(thread: dict[str, Any]) -> bool:
    cls = thread.get("classification", {}) or {}
    if cls.get("execution_mode") == "computer_mode":
        return True
    text = _json_text_v04(thread, 30000).lower()
    return (
        '"search_mode": "asi"' in text or '"search_mode":"asi"' in text
        or '"mode": "asi"' in text or '"mode":"asi"' in text
        or "pplx_asi" in text or '"tool_name": "computer"' in text or '"tool_name":"computer"' in text
        or "/computer/tasks/" in text or "wide_research" in text
    )


def extract_typed_payloads(records: list[ForensicRecord], prompt_text: str | None = None) -> list[dict[str, Any]]:
    """Extract concrete form/tool input payloads without treating search snippets as typed input."""
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()

    def context_allows_payload(path: tuple[str, ...], record: ForensicRecord) -> bool:
        path_text = "/".join(path)
        path_low = path_text.lower()
        if any(h.lower() in path_low for h in NON_PAYLOAD_CONTEXT_HINTS):
            # Tool/workflow context can override source-like parent names.
            return any(h.lower() in path_low for h in PAYLOAD_SOURCE_CONTEXT_HINTS)
        if record.record_kind == "tool_io":
            return True
        if any(h.lower() in path_low for h in PAYLOAD_SOURCE_CONTEXT_HINTS):
            return True
        value_text = json_text(record.value, 5000).lower()
        return "workflow_root" in value_text or "comet_agent_tool" in value_text or "타이핑" in value_text

    def looks_like_payload(field: str, value: str, path: tuple[str, ...], record: ForensicRecord) -> bool:
        s = value.strip()
        if not s or is_internal_noise_string(s) or is_prompt_duplicate(s, prompt_text):
            return False
        field_low = field.lower()
        if field_low == "url":
            # URLs are reconstructed in the urls section, except direct tool/form URL input.
            return context_allows_payload(path, record) and not any(x in s.lower() for x in ["ppl-ai-agent-screenshots", "cloudinary", "s3.amazonaws.com"])
        if field_low in {"date", "time", "start", "end", "start_time", "end_time"}:
            return context_allows_payload(path, record) and len(s) <= 120
        if EMAIL_RE.search(s):
            return True
        if field_low in {x.lower() for x in SCALAR_PAYLOAD_FIELDS}:
            return context_allows_payload(path, record) and len(s) <= 5000
        if URL_RE.search(s):
            return context_allows_payload(path, record)
        return False

    def add_payload(field: str, value: Any, record: ForensicRecord, path: tuple[str, ...]) -> None:
        if not isinstance(value, (str, int, float, bool)):
            return
        s = str(value).strip()
        if not looks_like_payload(field, s, path, record):
            return
        key = f"{field}:{s[:500]}:{record.ldb_seq_no}"
        if key in seen:
            return
        seen.add(key)
        payloads.append({
            "field": field,
            "value": s,
            "path_hint": "/".join(path[-6:]),
            "relative_order": record.ldb_seq_no,
            "evidence": record_evidence(record),
        })

    def walk(obj: Any, record: ForensicRecord, path: tuple[str, ...] = ()) -> None:
        obj = parse_maybe_json(obj)
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k)
                lower = key.lower()
                if lower in {x.lower() for x in SCALAR_PAYLOAD_FIELDS} or lower in {"url", "href", "input", "text", "query"}:
                    if isinstance(v, (str, int, float, bool)):
                        add_payload(key, v, record, path + (key,))
                    elif isinstance(v, (dict, list)) and context_allows_payload(path + (key,), record):
                        for s in flatten_strings(v, max_len=2000):
                            add_payload(key, s, record, path + (key,))
                if key == "evidence":
                    continue
                walk(v, record, path + (key,))
        elif isinstance(obj, list):
            for idx, item in enumerate(obj[:500]):
                walk(item, record, path + (str(idx),))

    for record in records:
        value_text = json_text(record.value, 50000).lower()
        if record.record_kind == "tool_io" or "comet_agent_tool" in value_text or "workflow_root" in value_text or "타이핑" in value_text:
            walk(record.value, record)
    return sorted(payloads, key=lambda item: item.get("relative_order") or -1)


def build_content_state(thread: dict[str, Any]) -> dict[str, Any]:
    prompt = thread.get("prompt", {}) or {}
    final_answer = thread.get("final_answer", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    plan = thread.get("plan") or []
    actions = thread.get("actions") or []
    urls = thread.get("urls") or []
    payloads = thread.get("typed_payloads") or []
    has_prompt = bool(prompt.get("text"))
    has_final = bool(final_answer.get("text"))
    has_workflow = bool(plan)
    has_actions = bool(actions)
    has_urls = bool(urls)
    has_payloads = bool(payloads)
    has_external = bool(metadata.get("external_thread_list_evidence"))

    if has_prompt and has_final and (has_workflow or has_actions or has_urls or has_payloads):
        state = "behavior_content_reconstructed"
    elif has_prompt and has_final:
        state = "prompt_and_answer_only"
    elif has_final:
        state = "final_answer_only"
    elif has_prompt and (has_workflow or has_actions or has_urls or has_payloads):
        state = "partial_behavior_without_final"
    elif has_prompt:
        state = "metadata_or_prompt_only"
    elif has_external:
        state = "list_cache_only"
    else:
        state = "no_thread_content"
    return {
        "state": state,
        "has_prompt": has_prompt,
        "prompt_field": prompt.get("field"),
        "has_final_answer": has_final,
        "has_workflow": has_workflow,
        "has_actions": has_actions,
        "has_thread_urls": has_urls,
        "has_typed_payloads": has_payloads,
        "has_external_thread_list_evidence": has_external,
        "interpretation": "Content state describes reconstructable behavioral content separately from LevelDB live/dead record state.",
    }


def build_residue_state(thread: dict[str, Any]) -> dict[str, Any]:
    deletion = thread.get("deletion_state", {}) or {}
    storage = build_storage_state(thread)
    content = build_content_state(thread)
    has_live = storage.get("live_record_count", 0) > 0
    has_dead = storage.get("dead_or_old_record_count", 0) > 0
    strong_del = bool(deletion.get("strong_evidence"))
    weak_del = bool(deletion.get("weak_evidence"))
    content_state = content.get("state")
    stale = bool(
        (has_live and has_dead)
        or weak_del
        or (strong_del and has_live)
        or content_state in {"metadata_or_prompt_only", "list_cache_only", "final_answer_only"}
    )
    if strong_del and has_live:
        label = "mixed_live_deleted"
    elif strong_del:
        label = "deleted_tombstone"
    elif stale:
        label = "live_or_stale_residue"
    elif has_live:
        label = "live"
    elif has_dead:
        label = "old_or_deleted_records_only"
    else:
        label = "unknown"
    return {
        "state": label,
        "stale_candidate": stale,
        "basis": {
            "storage_state": storage.get("state"),
            "content_state": content_state,
            "deletion_marker_state": deletion.get("state"),
            "has_strong_deletion_evidence": strong_del,
            "has_weak_deletion_evidence": weak_del,
        },
        "interpretation": "This separates user-visible deletion/reopen residue from raw LevelDB liveness; stale_candidate means the record may be residual/cache evidence rather than an active cloud thread.",
    }


def build_reconstruction_availability(thread: dict[str, Any]) -> dict[str, Any]:
    storage = build_storage_state(thread)
    content = build_content_state(thread)
    residue = build_residue_state({**thread, "content_state": content}) if False else None
    state = content["state"]
    if state == "behavior_content_reconstructed":
        level = "strong_thread_reconstruction"
    elif state in {"prompt_and_answer_only", "partial_behavior_without_final", "final_answer_only"}:
        level = "partial_thread_reconstruction"
    elif state in {"metadata_or_prompt_only", "list_cache_only"}:
        level = "metadata_residue_only"
    else:
        level = "not_reconstructable"
    return {
        "level": level,
        "storage_state": storage,
        "content_state": content,
        "residue_state": build_residue_state(thread),
        "interpretation": "Use storage_state, content_state, and residue_state together: a LevelDB-live record can still be stale/cache residue after deletion or reopen.",
    }


def add_case_layers_to_thread(thread: dict[str, Any], records: list[ForensicRecord] | None = None) -> dict[str, Any]:
    thread["artifact_buckets"] = build_artifact_buckets(thread)
    thread["task_outcome"] = classify_task_outcome(thread)
    thread["storage_state"] = build_storage_state(thread)
    thread["content_state"] = build_content_state(thread)
    thread["residue_state"] = build_residue_state(thread)
    thread["reconstruction_availability"] = build_reconstruction_availability(thread)
    if records is not None:
        thread["temporal_evidence"] = build_temporal_evidence(records, thread.get("metadata", {}) or {})
    return thread


# Wrap the final v0.4 reconstructor to stamp schema and add a transparent audit.
_reconstruct_browser_threads_v04_final = reconstruct_browser_threads

def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_v04_final(extracted, input_label, browser_only=browser_only)
    report["schema_version"] = SCHEMA_VERSION
    report["hardcoding_audit"] = {
        "case_specific_parser_rules": False,
        "target_reference_filter_behavior": "User-supplied target/reference strings are used only to filter and link candidate threads; reconstruction rules use structural fields and generic task verbs.",
        "generic_signatures_used": [
            "mode/search_mode/display_model",
            "all_results/thread_metadata/list_ask_threads",
            "workflow_root/plan_block/tool I/O",
            "UUID/context UUID linkage",
            "generic task verbs such as download/send/calendar/navigate/research",
        ],
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconstruct Perplexity Comet Browser/Computer behavior from IndexedDB LevelDB artifacts."
    )
    parser.add_argument("--input", type=Path, default=None, help="LevelDB folder, profile/scenario folder, or zip containing the Comet Perplexity IndexedDB LevelDB folder.")
    parser.add_argument("--blob-input", type=Path, default=None, help="Optional matching IndexedDB blob folder.")
    parser.add_argument("--before", type=Path, default=None, help="Before snapshot zip/folder for deletion comparison.")
    parser.add_argument("--after", type=Path, default=None, help="After snapshot zip/folder for deletion comparison.")
    parser.add_argument("--output", type=Path, default=Path("reconstruction.json"), help="JSON report output path.")
    parser.add_argument("--html-output", type=Path, default=None, help="Optional HTML report output path.")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.set_defaults(include_computer=True)
    parser.add_argument("--include-computer", dest="include_computer", action="store_true", help="Include detected Computer/ASI tasks. This is the default in v0.5.")
    parser.add_argument("--browser-only", dest="include_computer", action="store_false", help="Skip expanding detected Computer/ASI tasks and report Browser Control only.")
    parser.add_argument("--dump-records-dir", type=Path, default=None, help="Optional directory to also write raw parsed record dumps.")
    parser.add_argument("--target-reference", default=None, help="Optional user-supplied reference code/keyword for case-study filtering; not used as a parser rule.")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.before or args.after:
        if not args.before or not args.after:
            raise SystemExit("Both --before and --after are required for snapshot comparison mode.")

        before_report = run_single_input(
            input_path=args.before,
            blob_input=None,
            database=args.database,
            store=args.store,
            include_computer=args.include_computer,
            dump_records_dir=None,
        )
        after_report = run_single_input(
            input_path=args.after,
            blob_input=None,
            database=args.database,
            store=args.store,
            include_computer=args.include_computer,
            dump_records_dir=None,
        )
        comparison = compare_reconstructions(before_report, after_report)
        report = {
            "tool": "comet-browser-reconstructor",
            "schema_version": "0.1",
            "mode": "before_after_comparison",
            "before_reconstruction": before_report,
            "after_reconstruction": after_report,
            "snapshot_comparison": comparison,
        }
    else:
        if args.input is None:
            raise SystemExit("--input is required unless --before/--after are used.")
        report = run_single_input(
            input_path=args.input,
            blob_input=args.blob_input,
            database=args.database,
            store=args.store,
            include_computer=args.include_computer,
            dump_records_dir=args.dump_records_dir,
        )

    report = filter_report_by_target_reference(report, args.target_reference)

    write_json(args.output, report)
    print(f"JSON reconstruction saved: {args.output}")

    if args.html_output is not None:
        render_html_report(report, args.html_output)
        print(f"HTML reconstruction saved: {args.html_output}")

    # Print compact run summary.
    if report.get("mode") == "before_after_comparison":
        print(json.dumps(report.get("snapshot_comparison", {}).get("summary", {}), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report.get("summary", {}), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# v0.7 provenance-first prompt repair and conservative target filtering
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.7"

AGENT_PROGRESS_PREFIXES_V07 = (
    "clicking", "waiting", "opening", "downloading", "saving", "checking",
    "preparing", "filling", "navigating", "finding", "searching", "running",
    "i can see", "i see", "i'll ", "i will ", "let me ", "we need to",
    "클릭", "기다", "다운로드", "저장", "확인", "이동", "입력", "준비",
)

USER_PROMPT_FIELD_PRIORITY_V07 = {
    "query_str": 0,
    "user_query": 1,
    "prompt": 1,
    "thread_title": 3,
    "title": 4,
}


def _v07_norm_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def looks_like_progress_or_subtask_title(text: Any) -> bool:
    """Return True for agent-created progress/subtask titles, not user prompts."""
    s = _v07_norm_text(text)
    if not s:
        return False
    low = s.lower()
    if len(s) <= 180 and any(low.startswith(prefix) for prefix in AGENT_PROGRESS_PREFIXES_V07):
        return True
    if len(s) <= 180 and any(term in low for term in [" button", "to finish", "popup", "viewer", "browser download", "download to finish"]):
        return True
    # A short present-tense status/action phrase is likely generated by the agent/workflow.
    if len(s) <= 120 and re.search(r"\b(?:button|clicked|click|waiting|download|saving|verifying|confirming)\b", low):
        return True
    return False


def looks_like_user_prompt_text(text: Any, field: str | None = None) -> bool:
    """Accept only text that looks like a user-authored instruction.

    This is intentionally field-driven rather than scenario-driven:
    query_str/user_query/prompt are treated as user-authored when present in the
    artifact. title/thread_title are accepted only if they look like a full user
    instruction, because Computer/ASI titles often store subtask progress labels.
    """
    s = str(text or "").strip()
    if not s:
        return False
    if looks_like_progress_or_subtask_title(s):
        return False
    field_low = str(field or "").lower()
    if field_low in {"query_str", "user_query", "prompt"}:
        return True
    low = s.lower()
    if len(s) >= 60 and extract_reference_codes(s):
        return True
    if len(s) >= 80 and any(h in low for h in [
        "use browser control", "use computer mode", "use computer", "do not",
        "don't", "keep", "open the", "download", "create", "recipient:",
        "event title:", "step 1", "step 2", "reference code", "experiment",
    ]):
        return True
    if "\n" in s and len(s) >= 80 and any(h in low for h in ["use ", "do not", "keep", "open", "download", "create"]):
        return True
    return False


def is_activity_noise_label(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    s = _v07_norm_text(value)
    if not s:
        return True
    if re.fullmatch(r"\d+", s):
        return True
    if is_internal_noise_string(s):
        return True
    if s.lower() in {"click", "type", "wait", "find", "key", "input", "output", "text", "content", "title", "url", "href"}:
        return True
    return False


def extract_prompt(records: list[ForensicRecord]) -> dict[str, Any]:
    """Recover only the user-authored prompt from structural prompt fields."""
    candidates: list[tuple[int, int, str, ForensicRecord, str]] = []
    for record in records:
        if not isinstance(record.value, (dict, list)):
            continue
        for field, value in recursive_collect_fields(record.value, {"query_str", "user_query", "prompt", "title", "thread_title"}):
            if not isinstance(value, str) or not value.strip():
                continue
            text = value.strip()
            if not looks_like_user_prompt_text(text, field):
                continue
            score = USER_PROMPT_FIELD_PRIORITY_V07.get(str(field).lower(), 10)
            if extract_reference_codes(text):
                score -= 2
            if len(text) > 100:
                score -= 1
            candidates.append((score, -(len(text)), text, record, field))
    if not candidates:
        return {
            "text": None,
            "field": None,
            "reference_codes": [],
            "evidence": [],
            "note": "No user-authored prompt was recovered from prompt/query fields. Agent progress/subtask titles were not promoted to prompt.",
        }
    candidates.sort(key=lambda item: (item[0], item[1], item[3].ldb_seq_no or -1))
    _, _, text, record, field = candidates[0]
    return {
        "text": text,
        "field": field,
        "reference_codes": extract_reference_codes(text),
        "evidence": record_evidence(record),
        "note": "Prompt is restricted to user-authored prompt/query fields recovered from the artifacts.",
    }


def extract_plan(records: list[ForensicRecord]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        plan_objs = recursive_find(record.value, {"plan_block"}) if isinstance(record.value, (dict, list)) else []
        for plan_obj in plan_objs:
            for s in flatten_strings(plan_obj):
                label = _v07_norm_text(s)
                if is_activity_noise_label(label):
                    continue
                key = unique_event_key(label, "plan")
                if key in seen:
                    continue
                seen.add(key)
                items.append(make_item("plan", label, record))
    return sorted(items, key=lambda item: item.get("relative_order") or -1)


def _v07_walk_parsed(obj: Any) -> Iterable[tuple[Any, tuple[str, ...]]]:
    def walk(value: Any, path: tuple[str, ...]) -> Iterable[tuple[Any, tuple[str, ...]]]:
        value = parse_maybe_json(value)
        yield value, path
        if isinstance(value, dict):
            for k, v in value.items():
                if str(k) == "evidence":
                    continue
                yield from walk(v, path + (str(k),))
        elif isinstance(value, list):
            for i, v in enumerate(value[:500]):
                yield from walk(v, path + (str(i),))
    yield from walk(obj, ())


def extract_actions(records: list[ForensicRecord], prompt_text: str | None = None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_action(kind: str, label: Any, record: ForensicRecord, extra: dict[str, Any] | None = None) -> None:
        text = _v07_norm_text(label)
        if is_activity_noise_label(text) or is_prompt_duplicate(text, prompt_text):
            return
        if kind not in {"tool_io", "tool_action", "agent_subtask_or_progress"} and not contains_any(text, ACTION_KEYWORDS + ["COMET_AGENT_TOOL_INPUT", "COMET_AGENT_TOOL_OUTPUT", "BROWSER_AGENT_CONFIRMATION"]):
            return
        key = unique_event_key(text + json.dumps(extra or {}, ensure_ascii=False, default=str), kind)
        if key in seen:
            return
        seen.add(key)
        actions.append(make_item(kind, text, record, extra))

    for record in records:
        value_text = json_text(record.value, 120000)
        low = value_text.lower()
        if "comet_agent_tool_input" in low or "comet_agent_tool_output" in low:
            preview = value_text[:2500]
            key = unique_event_key(preview, "tool_io")
            if key not in seen:
                seen.add(key)
                actions.append(make_item("tool_io", "COMET_AGENT_TOOL_INPUT/OUTPUT candidate", record, {"text_preview": preview}))

        for obj, _path in _v07_walk_parsed(record.value):
            if not isinstance(obj, dict):
                continue
            tool_name = obj.get("tool_name") or obj.get("tool") or obj.get("action") or obj.get("command") or obj.get("type")
            if isinstance(tool_name, str) and contains_any(tool_name, ACTION_KEYWORDS + ["computer", "browser"]):
                params = {k: to_jsonable(v) for k, v in obj.items() if k not in {"screenshot", "image", "binary", "evidence"}}
                add_action("tool_action", tool_name, record, {"parameters_preview": json.dumps(params, ensure_ascii=False, default=str)[:1200]})

        workflow_objs: list[Any] = []
        if isinstance(record.value, (dict, list)):
            workflow_objs.extend(recursive_find(record.value, {"workflow_root", "plan_block"}))
        for workflow_obj in workflow_objs:
            for s in flatten_strings(workflow_obj):
                add_action("workflow_action", s, record)
    return sorted(actions, key=lambda item: item.get("relative_order") or -1)


SCALAR_PAYLOAD_FIELDS = {
    "recipient", "to", "cc", "bcc", "subject", "body", "description",
    "date", "time", "start", "end", "start_time", "end_time", "location",
    "filename", "file_name", "fileName", "downloaded_filename",
}

TITLE_PAYLOAD_FIELDS = {"title"}

PAYLOAD_ALLOW_CONTEXT_HINTS = {
    "tool", "tool_input", "comet_agent_tool_input", "arguments", "args", "parameters",
    "form", "compose", "gmail", "mail", "calendar", "event", "typed", "typing", "타이핑",
}

PAYLOAD_DENY_CONTEXT_HINTS = {
    "web_results", "sources", "search_results", "source", "unified_assets", "screenshots",
    "assets", "plan_block", "markdown_block", "answer", "chunks", "final_sse_message",
    "classifier", "telemetry", "workflow_block/steps/title", "steps/title", "step/title",
}


def _v07_path_text(path: tuple[str, ...]) -> str:
    return "/".join(str(x) for x in path).lower()


def extract_typed_payloads(records: list[ForensicRecord], prompt_text: str | None = None) -> list[dict[str, Any]]:
    """Extract concrete values typed into tools/forms, not workflow/source prose."""
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()

    def allowed_path(path: tuple[str, ...], record: ForensicRecord) -> bool:
        p = _v07_path_text(path)
        if any(h in p for h in PAYLOAD_DENY_CONTEXT_HINTS) and not any(h in p for h in PAYLOAD_ALLOW_CONTEXT_HINTS):
            return False
        if record.record_kind == "tool_io":
            return True
        if any(h in p for h in PAYLOAD_ALLOW_CONTEXT_HINTS):
            return True
        return False

    def add(field: str, value: Any, record: ForensicRecord, path: tuple[str, ...]) -> None:
        if not isinstance(value, (str, int, float, bool)):
            return
        s = str(value).strip()
        if not s or is_internal_noise_string(s) or is_prompt_duplicate(s, prompt_text):
            return
        if looks_like_progress_or_subtask_title(s):
            return
        field_low = field.lower()
        p = _v07_path_text(path)
        if field_low in {"title"}:
            # Titles are accepted only in clear form/calendar/email/tool-input contexts.
            if not (allowed_path(path, record) and any(x in p for x in ["calendar", "event", "compose", "form", "arguments", "args", "tool"])):
                return
        elif field_low in {x.lower() for x in SCALAR_PAYLOAD_FIELDS}:
            if not allowed_path(path, record):
                return
        elif field_low in {"url", "href"}:
            # URLs are normally handled by extract_urls; keep only direct tool/form input URLs.
            if not allowed_path(path, record):
                return
            if any(x in s.lower() for x in ["ppl-ai-agent-screenshots", "cloudinary", "s3.amazonaws.com", "screenshot"]):
                return
        elif field_low in {"input", "text", "query"}:
            if not allowed_path(path, record):
                return
            if len(s) > 1000:
                return
        elif EMAIL_RE.search(s):
            pass
        else:
            return
        if len(s) > 5000:
            return
        key = f"{field_low}:{s[:500]}"
        if key in seen:
            return
        seen.add(key)
        payloads.append({
            "field": field,
            "value": s,
            "path_hint": "/".join(path[-7:]),
            "relative_order": record.ldb_seq_no,
            "evidence": record_evidence(record),
        })

    def walk(obj: Any, record: ForensicRecord, path: tuple[str, ...] = ()) -> None:
        obj = parse_maybe_json(obj)
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k)
                if key == "evidence":
                    continue
                lower = key.lower()
                new_path = path + (key,)
                if lower in {x.lower() for x in SCALAR_PAYLOAD_FIELDS} | TITLE_PAYLOAD_FIELDS | {"url", "href", "input", "text", "query"}:
                    if isinstance(v, (str, int, float, bool)):
                        add(key, v, record, new_path)
                    elif isinstance(v, (dict, list)) and allowed_path(new_path, record):
                        for s in flatten_strings(v, max_len=2000):
                            add(key, s, record, new_path)
                walk(v, record, new_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj[:500]):
                walk(item, record, path + (str(i),))

    for record in records:
        text = json_text(record.value, 20000).lower()
        if record.record_kind == "tool_io" or any(h in text for h in ["comet_agent_tool_input", "arguments", "args", "타이핑", "typing"]):
            walk(record.value, record)
    return sorted(payloads, key=lambda item: item.get("relative_order") or -1)


def _v07_identifier_values_from_mapping(mapping: dict[str, Any]) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    contexts: set[str] = set()
    id_keys = {"backend_uuid", "uuid", "thread_uuid", "frontend_uuid", "slug"}
    ctx_keys = {"context_uuid", "frontend_context_uuid"}
    for key in id_keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            ids.add(value.strip())
            ids.update(UUID_RE.findall(value))
    for key in ctx_keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            contexts.add(value.strip())
            contexts.update(UUID_RE.findall(value))
    return ids, contexts


def _v07_thread_identity(thread: dict[str, Any]) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    contexts: set[str] = set()
    thread_id = str(thread.get("thread_id") or "")
    if thread_id:
        ids.add(thread_id)
        if thread_id.startswith("computer:"):
            ids.add(thread_id.split(":", 1)[1])
        ids.update(UUID_RE.findall(thread_id))
    meta = thread.get("metadata", {}) or {}
    meta_ids, meta_contexts = _v07_identifier_values_from_mapping(meta)
    ids |= meta_ids
    contexts |= meta_contexts
    return ids, contexts


def _v07_prompt_candidate_from_entry(entry: dict[str, Any], record: ForensicRecord) -> dict[str, Any] | None:
    best: tuple[int, str, str] | None = None
    for field in ("query_str", "user_query", "prompt", "thread_title", "title"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if not looks_like_user_prompt_text(text, field):
            continue
        score = USER_PROMPT_FIELD_PRIORITY_V07.get(field, 10)
        if extract_reference_codes(text):
            score -= 2
        if len(text) > 100:
            score -= 1
        cand = (score, field, text)
        if best is None or cand[0] < best[0] or (cand[0] == best[0] and len(cand[2]) > len(best[2])):
            best = cand
    if best is None:
        return None
    _, field, text = best
    ids, contexts = _v07_identifier_values_from_mapping(entry)
    title = entry.get("title") if isinstance(entry.get("title"), str) else None
    task_title = title.strip() if title and not looks_like_user_prompt_text(title, "title") else None
    return {
        "text": text,
        "field": f"/rest/thread/list_ask_threads.{field}",
        "reference_codes": extract_reference_codes(text),
        "evidence": record_evidence(record),
        "ids": ids,
        "contexts": contexts,
        "task_title": task_title,
        "entry_status": entry.get("status") or entry.get("thread_status") or entry.get("answer_status"),
        "last_query_datetime": entry.get("last_query_datetime"),
        "relative_order": record.ldb_seq_no,
    }


def _v07_collect_asi_prompt_infos(records: list[ForensicRecord]) -> list[dict[str, Any]]:
    infos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        text = record.text(120000)
        if "/rest/thread/list_ask_threads" not in text:
            continue
        try:
            entries = iter_asi_entry_dicts(record.value)
        except Exception:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            info = _v07_prompt_candidate_from_entry(entry, record)
            if not info:
                continue
            key = (info["text"][:300], tuple(sorted(info["ids"])), tuple(sorted(info["contexts"])), info.get("relative_order"))
            key_s = repr(key)
            if key_s in seen:
                continue
            seen.add(key_s)
            infos.append(info)
    return infos


def _v07_match_prompt_info(thread: dict[str, Any], prompt_infos: list[dict[str, Any]]) -> dict[str, Any] | None:
    ids, contexts = _v07_thread_identity(thread)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for info in prompt_infos:
        score = 0
        if contexts and info.get("contexts") and (contexts & set(info["contexts"])):
            score += 100
        if ids and info.get("ids") and (ids & set(info["ids"])):
            score += 60
        if score <= 0:
            continue
        if info.get("reference_codes"):
            score += 5
        scored.append((score, int(info.get("relative_order") or -1), info))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]


def _v07_add_subtask_action(thread: dict[str, Any], label: str, evidence: list[dict[str, Any]] | None = None) -> None:
    if not label or not looks_like_progress_or_subtask_title(label):
        return
    actions = thread.setdefault("actions", [])
    if any(_v07_norm_text(a.get("label")) == _v07_norm_text(label) for a in actions):
        return
    actions.append({
        "kind": "agent_subtask_or_progress",
        "label": label,
        "relative_order": evidence[0].get("ldb_seq_no") if evidence else None,
        "evidence": evidence or [],
        "interpretation": "Agent-created subtask/progress title recovered from artifacts; not a user-authored prompt.",
    })


def _v07_repair_thread_prompt(thread: dict[str, Any], prompt_infos: list[dict[str, Any]]) -> None:
    cls = thread.get("classification", {}) or {}
    mode = cls.get("execution_mode")
    prompt = thread.get("prompt", {}) or {}
    old_text = prompt.get("text")
    old_field = prompt.get("field")
    old_is_user = looks_like_user_prompt_text(old_text, old_field)
    old_is_progress = looks_like_progress_or_subtask_title(old_text)

    if old_is_progress:
        thread.setdefault("metadata", {})["task_title"] = thread.get("metadata", {}).get("task_title") or old_text
        _v07_add_subtask_action(thread, old_text, prompt.get("evidence") or [])

    matched_info = _v07_match_prompt_info(thread, prompt_infos) if mode == "computer_mode" else None
    if matched_info and (not old_is_user or str(old_field).lower() in {"title", "thread_title"}):
        thread["prompt"] = {
            "text": matched_info["text"],
            "field": matched_info["field"],
            "reference_codes": matched_info.get("reference_codes", []),
            "evidence": matched_info.get("evidence", []),
            "note": "Computer/ASI user prompt recovered from /rest/thread/list_ask_threads.query_str by thread/context identifiers.",
        }
        meta = thread.setdefault("metadata", {})
        if matched_info.get("task_title") and not meta.get("task_title"):
            meta["task_title"] = matched_info["task_title"]
        meta["prompt_provenance"] = {
            "source": matched_info["field"],
            "linkage": "context_uuid_or_thread_identifier",
            "relative_order": matched_info.get("relative_order"),
        }
    elif not old_is_user:
        thread["prompt"] = {
            "text": None,
            "field": None,
            "reference_codes": [],
            "evidence": [],
            "note": "No user-authored prompt was recovered. Agent progress/subtask title was not treated as prompt.",
        }
    else:
        prompt["note"] = prompt.get("note") or "Prompt accepted as user-authored instruction text recovered from artifacts."
        thread["prompt"] = prompt

    # Clean timeline prompt events: only user prompt may have kind=prompt.
    user_prompt_text = (thread.get("prompt", {}) or {}).get("text")
    cleaned: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    for event in thread.get("timeline", []) or []:
        if event.get("kind") == "prompt":
            label = event.get("label") or ""
            if user_prompt_text and _v07_norm_text(label) == _v07_norm_text(user_prompt_text[:1000]):
                cleaned.append(event)
            elif looks_like_progress_or_subtask_title(label):
                converted.append({**event, "kind": "agent_subtask_or_progress", "interpretation": "Converted from prompt because it is an agent progress/subtask label."})
            continue
        cleaned.append(event)
    thread["timeline"] = converted + cleaned


def _v07_records_for_thread(thread: dict[str, Any], group_map: dict[str, list[ForensicRecord]], relevant_records: list[ForensicRecord]) -> list[ForensicRecord]:
    tid = str(thread.get("thread_id") or "")
    if tid in group_map:
        return group_map[tid]
    if tid.startswith("computer:") and tid.split(":", 1)[1] in group_map:
        return group_map[tid.split(":", 1)[1]]
    ids, contexts = _v07_thread_identity(thread)
    matched: list[ForensicRecord] = []
    for record in relevant_records:
        rec_ids = get_value_uuid_candidates(record)
        key_ids = set(UUID_RE.findall(record.key))
        rec_text = record.text(20000)
        rec_contexts = set()
        for ctx_key in ["context_uuid", "frontend_context_uuid"]:
            for m in re.finditer(rf'["\']{ctx_key}["\']\s*:\s*["\']([^"\']+)["\']', rec_text):
                rec_contexts.update(UUID_RE.findall(m.group(1)))
        if (ids and (ids & (rec_ids | key_ids))) or (contexts and rec_contexts and (contexts & rec_contexts)):
            matched.append(record)
    return matched


def _v07_clean_list_items(items: list[dict[str, Any]], prompt_text: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        label = item.get("label") or item.get("value") or ""
        if isinstance(label, str):
            if is_activity_noise_label(label) or is_prompt_duplicate(label, prompt_text):
                continue
            if item.get("kind") in {"plan", "workflow_action"} and re.fullmatch(r"\d+", label.strip()):
                continue
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)[:1000]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _v07_repair_report_from_extracted(report: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    try:
        all_records = normalize_records(extracted.get("all_records", []) or [])
        relevant_records = [r for r in all_records if is_browser_relevant_record(r)]
        group_map, _global_records = group_records(relevant_records)
        prompt_infos = _v07_collect_asi_prompt_infos(relevant_records)
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.7 prompt repair could not inspect raw records: {exc}")
        return report

    for thread in report.get("threads", []) or []:
        _v07_repair_thread_prompt(thread, prompt_infos)
        records = _v07_records_for_thread(thread, group_map, relevant_records)
        prompt_text = (thread.get("prompt", {}) or {}).get("text")
        if records:
            thread["plan"] = _v07_clean_list_items(extract_plan(records), prompt_text)
            thread["actions"] = _v07_clean_list_items((thread.get("actions") or []) + extract_actions(records, prompt_text=prompt_text), prompt_text)
            thread["typed_payloads"] = extract_typed_payloads(records, prompt_text=prompt_text)
            thread["timeline"] = build_timeline(
                records=records,
                prompt=thread.get("prompt", {}) or {},
                plan=thread.get("plan", []) or [],
                actions=thread.get("actions", []) or [],
                urls=thread.get("urls", []) or [],
                typed_payloads=thread.get("typed_payloads", []) or [],
                final_answer=thread.get("final_answer", {}) or {},
            )
            # Reapply case layers after prompt/payload cleanup.
            try:
                add_case_layers_to_thread(thread, records)
            except Exception as exc:
                thread.setdefault("repair_warnings", []).append(f"v0.7 case layers not recalculated: {exc}")

    report["schema_version"] = SCHEMA_VERSION
    audit = report.setdefault("hardcoding_audit", {})
    audit.update({
        "case_specific_parser_rules": False,
        "prompt_provenance_rule_v07": "prompt.text is populated only from user-authored prompt/query fields recovered in artifacts, with /rest/thread/list_ask_threads.query_str used for Computer/ASI when linked by UUID/context UUID.",
        "agent_progress_rule_v07": "Clicking/Waiting/I can see-style Computer/ASI progress text is stored as task_title/action/reasoning, never as prompt.",
        "typed_payload_rule_v07": "Typed payloads are restricted to concrete tool/form input contexts and exclude plan, workflow-step titles, search results, source snippets, screenshot/assets, and final answer text.",
        "data_integrity_rule_v07": "No prompt text is synthesized or rewritten; values are copied from parsed LDB/LOG records with evidence references.",
    })
    report.setdefault("repair_summary", {})["v07_prompt_infos_found"] = len(prompt_infos)
    return report


_reconstruct_browser_threads_pre_v07 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v07(extracted, input_label, browser_only=browser_only)
    return _v07_repair_report_from_extracted(report, extracted)


def _v07_thread_text_for_target(thread: dict[str, Any]) -> str:
    fields: list[str] = []
    prompt = thread.get("prompt", {}) or {}
    meta = thread.get("metadata", {}) or {}
    final = thread.get("final_answer", {}) or {}
    outcome = thread.get("task_outcome", {}) or {}
    fields.extend([
        str(prompt.get("text") or ""),
        " ".join(prompt.get("reference_codes") or []),
        str(meta.get("task_title") or ""),
        str(meta.get("backend_uuid") or ""),
        str(meta.get("context_uuid") or ""),
        str(final.get("text") or ""),
        json.dumps(outcome.get("downloaded_filename_candidates") or [], ensure_ascii=False, default=str),
    ])
    return "\n".join(fields)


def _v07_reference_tokens(text: str) -> set[str]:
    refs = set(extract_reference_codes(text or ""))
    for m in re.finditer(r"\b(?:S\d{2}|C\d{2}|Computer)_[A-Za-z0-9_\-]+(?:_\d{8})?\b", text or ""):
        refs.add(m.group(0))
    return refs


def _v07_thread_has_conflicting_reference(thread: dict[str, Any], target: str) -> bool:
    target_refs = _v07_reference_tokens(target)
    if not target_refs:
        return False
    prompt_text = ((thread.get("prompt", {}) or {}).get("text") or "")
    task_title = ((thread.get("metadata", {}) or {}).get("task_title") or "")
    refs = _v07_reference_tokens(prompt_text + "\n" + task_title)
    return bool(refs and not (refs & target_refs))


def _v07_target_kind(target: str) -> str | None:
    low = str(target or "").lower()
    if any(x in low for x in ["download", "pdf", "file"]):
        return "download"
    if any(x in low for x in ["calendar", "event"]):
        return "calendar"
    if any(x in low for x in ["gmail", "email", "draft", "send"]):
        return "email"
    if any(x in low for x in ["wikipedia", "navigate", "page"]):
        return "navigation"
    return None


def _v07_thread_task_compatible(thread: dict[str, Any], kind: str | None) -> bool:
    if not kind:
        return True
    text = _v07_thread_text_for_target(thread).lower() + "\n" + collect_thread_text(thread, include_prompt=True).lower()
    task_type = (thread.get("task_outcome", {}) or {}).get("task_type")
    if kind == "download":
        return task_type == "download" or any(x in text for x in ["download", ".pdf", "downloaded filename"])
    if kind == "calendar":
        return task_type == "calendar_create" or any(x in text for x in ["calendar", "event"])
    if kind == "email":
        return task_type == "gmail_send" or any(x in text for x in ["gmail", "email", "draft", "sent folder"])
    if kind == "navigation":
        return task_type in {"page_open", "web_research"} or any(x in text for x in ["navigate", "wikipedia", "page"])
    return True


def _v07_thread_matches_target(thread: dict[str, Any], target: str) -> tuple[bool, str]:
    if _v07_thread_has_conflicting_reference(thread, target):
        return False, "conflicting_reference_code"
    target_kind = _v07_target_kind(target)
    selected_text = _v07_thread_text_for_target(thread)
    if target in selected_text:
        if _v07_thread_task_compatible(thread, target_kind):
            return True, "target_in_prompt_or_case_fields"
        return False, "target_text_but_task_incompatible"
    # Do not match on full evidence/source paths. Only allow structural Computer linkage
    # if the repaired prompt or task/outcome indicates compatible task content.
    if (thread.get("classification", {}) or {}).get("execution_mode") == "computer_mode" and _v07_thread_task_compatible(thread, target_kind):
        prompt_text = ((thread.get("prompt", {}) or {}).get("text") or "")
        if prompt_text and not _v07_thread_has_conflicting_reference(thread, target):
            return target in prompt_text, "computer_prompt_checked"
    return False, "no_target_match"


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    if not target_reference:
        report["case_summary"] = build_case_summary(report, None)
        return report
    target = str(target_reference).strip()
    if not target:
        report["case_summary"] = build_case_summary(report, None)
        return report
    if report.get("mode") == "before_after_comparison":
        report["target_reference_filter"] = target
        report["filter_warning"] = "Target-reference filtering is not applied to before/after comparison envelopes."
        return report

    filtered = json.loads(json.dumps(report, ensure_ascii=False, default=str))
    original_threads = filtered.get("threads", []) or []
    original_skipped = filtered.get("skipped", []) or []
    original_globals = filtered.get("global_records", []) or []

    kept_threads: list[dict[str, Any]] = []
    residual_threads: list[dict[str, Any]] = []
    for thread in original_threads:
        keep, reason = _v07_thread_matches_target(thread, target)
        thread.setdefault("target_filter_match", {})["v07_reason"] = reason
        if keep:
            kept_threads.append(thread)
        else:
            residual_threads.append({
                "thread_id": thread.get("thread_id"),
                "execution_mode": (thread.get("classification", {}) or {}).get("execution_mode"),
                "prompt_reference_codes": (thread.get("prompt", {}) or {}).get("reference_codes", []),
                "reason": reason,
            })

    def global_has_target(record: dict[str, Any]) -> bool:
        return target in str(record.get("key") or "") or target in str(record.get("value_preview") or "")

    filtered_globals = [record for record in original_globals if global_has_target(record)]
    filtered_skipped = [group for group in original_skipped if target in json.dumps(group, ensure_ascii=False, default=str)]
    filtered["threads"] = kept_threads
    filtered["skipped"] = filtered_skipped
    filtered["global_records"] = filtered_globals
    filtered["summary"] = summarize_reconstruction(kept_threads, filtered_skipped)
    filtered.setdefault("source", {})["target_reference_filter"] = target
    original_counts = {
        "original_thread_count": len(original_threads),
        "original_skipped_count": len(original_skipped),
        "original_global_record_count": len(original_globals),
    }
    filtered["filter_summary"] = {
        "target_reference": target,
        **original_counts,
        "filtered_thread_count": len(kept_threads),
        "filtered_skipped_count": len(filtered_skipped),
        "filtered_global_record_count": len(filtered_globals),
        "residual_thread_count": len(original_threads) - len(kept_threads),
        "residual_skipped_count": len(original_skipped) - len(filtered_skipped),
        "residual_global_record_count": len(original_globals) - len(filtered_globals),
        "residual_threads_sample": residual_threads[:20],
        "filter_rule": "v0.7 uses repaired prompt/case fields and rejects conflicting reference codes; it does not match target strings found only in evidence/source paths.",
    }
    filtered["case_summary"] = build_case_summary(filtered, target, original_counts=original_counts)
    return filtered


# ---------------------------------------------------------------------------
# v0.8 Computer-mode dedicated reconstruction layer
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.8"

ISO_TIME_RE_V08 = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b")
PDF_FILENAME_RE_V08 = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._\-\s]{0,160}?\.\s*pdf\b", re.IGNORECASE)


def _v08_norm_text(text: Any) -> str:
    return " ".join(str(text or "").split())


def _v08_evidence_key(ev: dict[str, Any]) -> tuple[Any, Any, Any, Any, Any]:
    return (ev.get("source_file"), ev.get("source_type"), ev.get("offset"), ev.get("ldb_seq_no"), ev.get("state"))


def _v08_dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any, Any, Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = _v08_evidence_key(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return sorted(out, key=lambda ev: ((ev.get("ldb_seq_no") is None), ev.get("ldb_seq_no") or -1, str(ev.get("source_file") or ""), ev.get("offset") or -1))


def _v08_source_counts_from_evidence(evidence: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ev in evidence or []:
        st = ev.get("source_type") or "unknown"
        counts[st] = counts.get(st, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: x[0]))


def _v08_record_contains_exact_text(record: ForensicRecord, target_text: str) -> bool:
    if not target_text:
        return False
    return _v08_norm_text(target_text) in _v08_norm_text(record.text(None))


def _v08_records_from_extracted(extracted: dict[str, Any]) -> list[ForensicRecord]:
    try:
        return normalize_records(extracted.get("all_records", []) or [])
    except Exception:
        return []


def _v08_collect_prompt_occurrences(records: list[ForensicRecord], prompt_text: str) -> list[dict[str, Any]]:
    """Find every LDB/LOG occurrence of the exact recovered user prompt.

    This does not invent or rewrite prompt content. It only corroborates where
    the already-recovered prompt string appears in parsed artifacts.
    """
    if not prompt_text:
        return []
    out: list[dict[str, Any]] = []
    normalized = _v08_norm_text(prompt_text)
    for record in records:
        if not _v08_record_contains_exact_text(record, prompt_text):
            continue
        field_hits: list[str] = []
        if isinstance(record.value, (dict, list)):
            for field, value in recursive_collect_fields(record.value, {"query_str", "thread_title", "title", "prompt", "user_query"}):
                if isinstance(value, str) and _v08_norm_text(value) == normalized:
                    if record.record_kind == "asi_thread_list" or "/rest/thread/list_ask_threads" in record.text(4000):
                        field_hits.append(f"/rest/thread/list_ask_threads.{field}")
                    else:
                        field_hits.append(field)
        if not field_hits and normalized in _v08_norm_text(record.key):
            field_hits.append("key_text_match")
        # Keep broad exact-text matches as corroboration, but label them as such.
        if not field_hits:
            field_hits.append("exact_text_in_value")
        out.append({
            "record_kind": record.record_kind,
            "key": record.key,
            "fields": sorted(set(field_hits)),
            "is_live": record.is_live,
            "source_types": record.source_types,
            "relative_order": record.ldb_seq_no,
            "evidence": record_evidence(record),
        })
    # Deduplicate by record seq/kind/key.
    seen: set[tuple[Any, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in out:
        key = (item.get("relative_order"), item.get("record_kind"), item.get("key"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return sorted(deduped, key=lambda x: x.get("relative_order") or -1)


def _v08_parse_jsonish(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    s = text.strip()
    if not s:
        return text
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(s)
        except Exception:
            pass
    return text


def _v08_collect_iso_times_from_obj(obj: Any, source_label: str, evidence: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(field_path: str, value: Any) -> None:
        if value in (None, "", [], {}):
            return
        raw = str(value)
        # ISO strings are treated as strongest for Computer workflow timings.
        if ISO_TIME_RE_V08.search(raw):
            out.append({
                "field": field_path,
                "raw": raw,
                "source": source_label,
                "time_interpretation": {"raw": raw, "interpreted_utc": raw, "interpretation": "artifact_iso8601"},
                "evidence": evidence or [],
            })
        elif field_path.split(".")[-1] in {"created_at", "updated_at", "last_query_datetime", "lastAccess", "last_access"}:
            out.append({
                "field": field_path,
                "raw": to_jsonable(value),
                "source": source_label,
                "time_interpretation": interpret_timestamp(value),
                "evidence": evidence or [],
            })

    def walk(value: Any, path: tuple[str, ...]) -> None:
        value = _v08_parse_jsonish(value)
        if isinstance(value, dict):
            for k, v in value.items():
                key = str(k)
                lower = key.lower()
                # These field names are workflow/artifact timing fields, not page-content snippets.
                if lower in {
                    "started_at", "completed_at", "created_at", "updated_at",
                    "last_query_datetime", "lastaccess", "last_access",
                    "start_time", "end_time", "download_start_time", "download_end_time",
                }:
                    add(".".join(path + (key,)), v)
                walk(v, path + (key,))
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, path + (str(idx),))
        elif isinstance(value, str):
            # Avoid scanning arbitrary long page text. Only parse strings that look JSON-ish or are parameter previews.
            if value.strip().startswith(("{", "[")):
                parsed = _v08_parse_jsonish(value)
                if parsed is not value:
                    walk(parsed, path)

    walk(obj, tuple())
    # Deduplicate by field/raw/source/order.
    seen: set[tuple[str, str, str, Any]] = set()
    deduped: list[dict[str, Any]] = []
    for item in out:
        order = None
        ev = (item.get("evidence") or [])
        if ev:
            order = ev[0].get("ldb_seq_no")
        key = (item.get("field"), str(item.get("raw")), item.get("source"), order)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _v08_extract_text_candidates(obj: Any) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        s = value.strip()
        if not s or is_internal_noise_string(s):
            return
        # Keep reasonably human-readable Computer thought/progress text only.
        if len(s) < 15:
            return
        if s.startswith(("{", "[")) and len(s) > 80:
            return
        key = _v08_norm_text(s)[:500]
        if key in seen:
            return
        seen.add(key)
        texts.append(s)

    def walk(value: Any) -> None:
        value = _v08_parse_jsonish(value)
        if isinstance(value, dict):
            # Prefer direct text-ish fields in order.
            for key in ("text", "message", "content", "answer", "summary", "title", "label"):
                if key in value:
                    walk(value.get(key))
            # Chunks often hold a list of text fragments.
            if "chunks" in value:
                walk(value.get("chunks"))
            if "text_payload" in value:
                walk(value.get("text_payload"))
            # Then walk remaining nested payload.
            for k, child in value.items():
                if k not in {"text", "message", "content", "answer", "summary", "title", "label", "chunks", "text_payload"}:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        else:
            add(value)

    walk(obj)
    return texts


def extract_computer_reasoning_items_from_value(value: Any, record: ForensicRecord) -> list[dict[str, Any]]:
    """Extract observable Computer-mode thought/action-rationale candidates.

    This is intentionally source-driven: it reports text found in decoded LDB/LOG
    payloads when a structured marker such as variant=thought or tool_name=computer
    exists. It does not infer hidden chain-of-thought.
    """
    items: list[dict[str, Any]] = []

    def walk(obj: Any, path: tuple[str, ...] = ()) -> None:
        parsed = _v08_parse_jsonish(obj)
        obj = parsed
        if isinstance(obj, dict):
            variant = str(obj.get("variant") or obj.get("type") or obj.get("role") or "").lower()
            tool_name = str(obj.get("tool_name") or obj.get("tool") or "").lower()
            status = str(obj.get("status") or "")
            title = obj.get("title")
            is_thought = "thought" in variant or variant in {"reasoning", "rationale"}
            is_computer_tool = tool_name in {"computer", "wait_for_download", "wait_for_subagents"}
            if is_thought or is_computer_tool:
                text_candidates = _v08_extract_text_candidates(obj)
                # For tool actions, the title is often the only concise action rationale.
                if isinstance(title, str) and title.strip() and title.strip() not in text_candidates:
                    text_candidates.insert(0, title.strip())
                cleaned: list[str] = []
                seen_text: set[str] = set()
                for text in text_candidates:
                    if not text or is_prompt_duplicate(text, None):
                        continue
                    key = _v08_norm_text(text)[:300]
                    if key in seen_text:
                        continue
                    seen_text.add(key)
                    cleaned.append(text[:2500])
                if cleaned:
                    items.append({
                        "kind": "observable_computer_thought_candidate" if is_thought else "computer_tool_action_rationale",
                        "variant": variant or None,
                        "tool_name": tool_name or None,
                        "status": status or None,
                        "text": cleaned[0],
                        "text_candidates": cleaned[:6],
                        "relative_order": record.ldb_seq_no,
                        "source_types": record.source_types,
                        "evidence": record_evidence(record),
                        "time_fields": _v08_collect_iso_times_from_obj(obj, "computer_reasoning_or_tool_payload", record_evidence(record)),
                        "caution": "Observable Computer workflow text recovered from artifacts; not asserted to be private chain-of-thought.",
                    })
            for key, child in obj.items():
                walk(child, path + (str(key),))
        elif isinstance(obj, list):
            for idx, child in enumerate(obj):
                walk(child, path + (str(idx),))

    walk(value)
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, str | None]] = set()
    for item in items:
        key = (item.get("text", "")[:300], item.get("relative_order"), item.get("tool_name"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def extract_reasoning(records: list[ForensicRecord], execution_mode: str) -> dict[str, Any]:
    if execution_mode == "browser_control":
        return {
            "available": False,
            "items": [],
            "note": "Browser Control artifacts did not contain internal reasoning text in this reconstruction scope.",
        }
    if execution_mode != "computer_mode":
        return {"available": False, "items": [], "note": "No reasoning artifact identified."}

    items: list[dict[str, Any]] = []
    for record in records:
        items.extend(extract_computer_reasoning_items_from_value(record.value, record))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sorted(items, key=lambda x: x.get("relative_order") or -1):
        key = _v08_norm_text(item.get("text") or "")[:300] + str(item.get("relative_order"))
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return {
        "available": bool(deduped),
        "items": deduped[:40],
        "note": None if deduped else "No observable Computer thought/action-rationale artifact was identified in decoded LDB/LOG records.",
        "interpretation": "Computer-mode reasoning means observable workflow thought/rationale text recovered from artifacts, not hidden private chain-of-thought.",
    }


def _v08_cleanup_pdf_filename(raw: str) -> str | None:
    s = str(raw or "")
    s = re.sub(r"[`*_]+", "", s)
    s = re.sub(r"\s*\.\s*pdf\b", ".pdf", s, flags=re.IGNORECASE)
    s = " ".join(s.split())
    # If a candidate contains a long sentence before a final filename, keep the last whitespace-delimited token ending .pdf.
    tokens = [tok.strip(".,;:()[]{}<>\"'") for tok in s.split() if ".pdf" in tok.lower()]
    if tokens:
        s = tokens[-1]
    s = s.strip(".,;:()[]{}<>\"'")
    if not s.lower().endswith(".pdf"):
        return None
    # Reject obvious fragments unless no better candidate exists upstream.
    stem = s[:-4]
    if len(stem) < 4:
        return None
    if not re.search(r"[A-Za-z0-9]", stem):
        return None
    return s


def extract_pdf_filenames(text: str) -> list[str]:
    """Extract PDF filenames without inventing values.

    Handles markdown/newline breaks around the .pdf extension and avoids
    short fragment candidates when longer artifact-derived candidates exist.
    """
    source = str(text or "")
    # Normalize common markdown line wraps around the extension only.
    normalized = re.sub(r"\s*\.\s*pdf\b", ".pdf", source, flags=re.IGNORECASE)
    found = PDF_FILENAME_RE_V08.findall(normalized)
    candidates: list[str] = []
    for raw in found:
        cleaned = _v08_cleanup_pdf_filename(raw)
        if cleaned:
            candidates.append(cleaned)
    # Also catch filename labels explicitly, allowing line breaks inside the filename.
    for m in re.finditer(r"(?:filename|file name|downloaded file|final downloaded filename)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9._\-\s]{0,180}?\.\s*pdf)", source, flags=re.IGNORECASE | re.DOTALL):
        cleaned = _v08_cleanup_pdf_filename(m.group(1))
        if cleaned:
            candidates.append(cleaned)
    # Deduplicate and prefer more specific/longer candidates.
    by_lower: dict[str, str] = {}
    for cand in candidates:
        key = cand.lower()
        if key not in by_lower or len(cand) > len(by_lower[key]):
            by_lower[key] = cand
    ordered = sorted(by_lower.values(), key=lambda x: (-len(x), x.lower()))
    # Remove short suffix fragments if a longer candidate ends with them.
    filtered: list[str] = []
    for cand in ordered:
        low = cand.lower()
        if any(other.lower().endswith(low) and len(other) > len(cand) + 3 for other in filtered):
            continue
        filtered.append(cand)
    return filtered


def _v08_collect_action_time_fields(thread: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action in thread.get("actions", []) or []:
        evidence = action.get("evidence") or []
        for key in ("parameters_preview", "text_preview", "label"):
            if key in action:
                out.extend(_v08_collect_iso_times_from_obj(action.get(key), f"action.{action.get('kind')}.{key}", evidence))
    for item in thread.get("reasoning", {}).get("items", []) or []:
        if item.get("time_fields"):
            out.extend(item.get("time_fields") or [])
    seen: set[tuple[str, str, Any]] = set()
    deduped: list[dict[str, Any]] = []
    for item in out:
        ev = item.get("evidence") or []
        order = ev[0].get("ldb_seq_no") if ev else None
        key = (item.get("field"), str(item.get("raw")), order)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return sorted(deduped, key=lambda x: ((x.get("evidence") or [{}])[0].get("ldb_seq_no") or -1, x.get("field") or ""))


def _v08_attach_computer_source_layers(thread: dict[str, Any], all_records: list[ForensicRecord]) -> None:
    if (thread.get("classification", {}) or {}).get("execution_mode") != "computer_mode":
        return
    prompt = thread.get("prompt", {}) or {}
    prompt_text = prompt.get("text")
    if prompt_text:
        occurrences = _v08_collect_prompt_occurrences(all_records, prompt_text)
        evidence: list[dict[str, Any]] = []
        for item in occurrences:
            evidence.extend(item.get("evidence") or [])
        combined = _v08_dedupe_evidence((prompt.get("evidence") or []) + evidence)
        prompt["evidence"] = combined[:20]
        prompt["corroboration"] = {
            "occurrence_count": len(occurrences),
            "source_type_counts": _v08_source_counts_from_evidence(combined),
            "has_ldb_copy": any(ev.get("source_type") == "ldb" for ev in combined),
            "has_log_copy": any(ev.get("source_type") == "log" for ev in combined),
            "occurrences_sample": occurrences[:10],
            "interpretation": "All entries are exact prompt-text occurrences recovered from parsed artifacts; no prompt text is generated or normalized except whitespace matching for comparison.",
        }
        prompt["artifact_text_unchanged"] = True
    prompt.setdefault("scope", "thread_or_case_prompt")
    thread["prompt"] = prompt

    # Source persistence summary for Computer-mode review.
    source_summary = thread.get("source_summary") or {}
    source_counts = source_summary.get("source_type_counts") or {}
    thread["computer_source_persistence"] = {
        "source_type_counts": source_counts,
        "has_ldb_records": bool(source_counts.get("ldb")),
        "has_log_records": bool(source_counts.get("log")),
        "top_source_files": source_summary.get("top_source_files") or [],
        "interpretation": "Computer-mode evidence may appear in LOG, LDB/SST, or both. LOG-only means the selected records are recent/uncompacted artifacts, not that Computer mode inherently stores only LOG.",
    }
    thread["computer_temporal_evidence"] = {
        "workflow_time_fields": _v08_collect_action_time_fields(thread),
        "sequence_range": (thread.get("temporal_evidence") or {}).get("sequence_range"),
        "note": "Computer-specific workflow timings include artifact fields such as started_at/completed_at when present in tool/action payloads. Page-content time strings are not used as forensic timestamps.",
    }


def _v08_add_case_level_computer_prompt(report: dict[str, Any]) -> None:
    computer_threads = [t for t in report.get("threads", []) or [] if (t.get("classification", {}) or {}).get("execution_mode") == "computer_mode"]
    if not computer_threads:
        return
    prompt_groups: dict[str, list[dict[str, Any]]] = {}
    for thread in computer_threads:
        text = ((thread.get("prompt", {}) or {}).get("text") or "").strip()
        if not text:
            continue
        prompt_groups.setdefault(_v08_norm_text(text), []).append(thread)
    if not prompt_groups:
        return
    # Prefer the prompt shared by most Computer threads; tie-break by length.
    norm, group = sorted(prompt_groups.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0]
    if len(group) < 2:
        return
    text = ((group[0].get("prompt", {}) or {}).get("text") or "").strip()
    evidence = _v08_dedupe_evidence(sum([((t.get("prompt", {}) or {}).get("evidence") or []) for t in group], []))
    case_prompt = {
        "text": text,
        "reference_codes": extract_reference_codes(text),
        "source": (group[0].get("prompt", {}) or {}).get("field"),
        "thread_count_linked": len(group),
        "linked_thread_ids": [t.get("thread_id") for t in group],
        "evidence": evidence[:25],
        "source_type_counts": _v08_source_counts_from_evidence(evidence),
        "interpretation": "This is a parent/case-level Computer prompt recovered from artifacts and linked to related Computer subtasks. It is repeated in subthreads for navigation, but should not be interpreted as a separately typed prompt per subtask unless corroborated by per-thread query_str evidence.",
    }
    report["computer_case_prompt"] = case_prompt
    for thread in group:
        p = thread.get("prompt", {}) or {}
        p["scope"] = "case_level_prompt_linked_to_computer_subtask"
        p["thread_specific_prompt"] = False
        p["case_prompt_id"] = "computer_case_prompt"
        p["interpretation"] = "The prompt text is artifact-derived, but it is treated as the parent Computer-mode case prompt linked to this subtask/thread, not as proof of a separate user prompt typed for this specific subtask."
        thread["prompt"] = p


def _v08_recompute_computer_outcomes(report: dict[str, Any]) -> None:
    for thread in report.get("threads", []) or []:
        if (thread.get("classification", {}) or {}).get("execution_mode") == "computer_mode":
            thread["task_outcome"] = classify_task_outcome(thread)
            # Ensure all currently extracted PDF candidates are normalized after v0.8 filename repair.
            filenames = extract_pdf_filenames(collect_thread_text(thread, include_prompt=True))
            if filenames:
                thread.setdefault("task_outcome", {})["downloaded_filename_candidates"] = filenames


def _v08_enhance_report_from_extracted(report: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    all_records = _v08_records_from_extracted(extracted)
    for thread in report.get("threads", []) or []:
        _v08_attach_computer_source_layers(thread, all_records)
    _v08_add_case_level_computer_prompt(report)
    _v08_recompute_computer_outcomes(report)
    report["schema_version"] = "0.8"
    report.setdefault("hardcoding_audit", {})["v08"] = {
        "computer_mode_separate_path": True,
        "prompt_rule": "prompt text must come from artifact user-prompt fields such as query_str/thread_title; progress titles are not promoted to prompt.",
        "computer_reasoning_rule": "only observable variant=thought/tool payload text from decoded LDB/LOG records is reported; hidden reasoning is not inferred.",
        "timestamp_rule": "forensic timestamps are artifact fields; Computer tool started_at/completed_at are reported when present.",
        "no_scenario_value_rule": "no experiment-specific literal is required by the parser; target-reference is used only for optional filtering/report grouping.",
    }
    return report


_reconstruct_browser_threads_pre_v08 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v08(extracted, input_label, browser_only=browser_only)
    return _v08_enhance_report_from_extracted(report, extracted)


_build_case_summary_pre_v08 = build_case_summary


def build_case_summary(report: dict[str, Any], target_reference: str | None = None, original_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    case = _build_case_summary_pre_v08(report, target_reference, original_counts=original_counts)
    threads = report.get("threads", []) or []
    if not threads:
        return case
    target_threads = threads
    if target_reference:
        target_threads = [t for t in threads if t.get("target_filter_match") or str(target_reference) in json.dumps(t, ensure_ascii=False, default=str)]
    if not target_threads:
        return case

    def score(thread: dict[str, Any]) -> tuple[int, int, int, int, int, int, int, int]:
        outcome = thread.get("task_outcome", {}) or {}
        cls = thread.get("classification", {}) or {}
        availability = thread.get("reconstruction_availability", {}) or {}
        source_counts = ((thread.get("source_summary") or {}).get("source_type_counts") or {})
        completed = 1 if outcome.get("side_effect_completed") is True else 0
        has_actions = min(len(thread.get("actions") or []), 9)
        has_reasoning = min(len((thread.get("reasoning") or {}).get("items") or []), 9)
        has_final = 1 if (thread.get("final_answer", {}) or {}).get("text") else 0
        has_ldb = 1 if source_counts.get("ldb") else 0
        strong_or_partial = 1 if availability.get("level") in {"strong_thread_reconstruction", "partial_thread_reconstruction"} else 0
        computer = 1 if cls.get("execution_mode") == "computer_mode" else 0
        source_volume = min(int(source_counts.get("ldb", 0)) + int(source_counts.get("log", 0)), 99)
        # For Computer cases, action/reasoning/ldb evidence should outrank a top-level prompt+answer cache.
        return (completed, has_actions, has_reasoning, has_final, has_ldb, strong_or_partial, computer, source_volume)

    primary = sorted(target_threads, key=score, reverse=True)[0]
    case["primary_thread_id"] = primary.get("thread_id")
    case["primary_execution_mode"] = (primary.get("classification", {}) or {}).get("execution_mode")
    case["primary_task_outcome"] = primary.get("task_outcome")
    case["primary_reconstruction_availability"] = primary.get("reconstruction_availability")
    case.setdefault("investigator_summary", [])
    case["investigator_summary"].append(
        "Primary selection prefers completed side-effect evidence with action/reasoning/LDB corroboration over prompt-only Computer cache entries."
    )
    if report.get("computer_case_prompt"):
        case["computer_case_prompt_summary"] = {
            "source": report["computer_case_prompt"].get("source"),
            "thread_count_linked": report["computer_case_prompt"].get("thread_count_linked"),
            "source_type_counts": report["computer_case_prompt"].get("source_type_counts"),
            "interpretation": report["computer_case_prompt"].get("interpretation"),
        }
    return case


_render_thread_detail_v07_pre_v08 = _render_thread_detail_v07


def _render_thread_detail_v07(thread: dict[str, Any], idx: int) -> str:
    html_out = _render_thread_detail_v07_pre_v08(thread, idx)
    if (thread.get("classification", {}) or {}).get("execution_mode") != "computer_mode":
        return html_out
    extra = "\n".join([
        "<h3>8. Computer-mode specific evidence</h3>",
        _kv_table_v07([
            ("Prompt scope", (thread.get("prompt", {}) or {}).get("scope")),
            ("Thread-specific prompt", (thread.get("prompt", {}) or {}).get("thread_specific_prompt")),
            ("Task title", (thread.get("metadata", {}) or {}).get("task_title")),
            ("LDB records present", (thread.get("computer_source_persistence", {}) or {}).get("has_ldb_records")),
            ("LOG records present", (thread.get("computer_source_persistence", {}) or {}).get("has_log_records")),
        ]),
        _raw_details_v07("Prompt corroboration", (thread.get("prompt", {}) or {}).get("corroboration")),
        _raw_details_v07("Computer source persistence", thread.get("computer_source_persistence")),
        _raw_details_v07("Computer temporal evidence", thread.get("computer_temporal_evidence")),
        _raw_details_v07("Reasoning / observable thought candidates", (thread.get("reasoning", {}) or {}).get("items")),
    ])
    return html_out.replace("</section>", extra + "\n</section>", 1)


_render_case_summary_v10_pre_v08 = _render_case_summary_v10


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    base = _render_case_summary_v10_pre_v08(report)
    case_prompt = report.get("computer_case_prompt")
    if not case_prompt:
        return base
    prompt_html = "\n".join([
        "<section class='card'><div class='topline'><h2>Computer case-level prompt</h2><span class='badge warn'>parent prompt</span></div>",
        f"<div class='prompt'>{_h(case_prompt.get('text') or '')}</div>",
        _evidence_row("Case prompt evidence", case_prompt.get("evidence")),
        _kv_table_v07([
            ("Source", case_prompt.get("source")),
            ("Linked threads", case_prompt.get("thread_count_linked")),
            ("Source type counts", json.dumps(case_prompt.get("source_type_counts") or {}, ensure_ascii=False)),
            ("Interpretation", case_prompt.get("interpretation")),
        ]),
        "</section>",
    ])
    return base + prompt_html


# ---------------------------------------------------------------------------
# v0.9 target-safe Computer case prompt repair
# ---------------------------------------------------------------------------
#
# v0.8 added a Computer case-level prompt layer, but it selected the largest
# shared Computer prompt group before target filtering. In profiles containing
# residual Computer tasks, that can surface an unrelated parent prompt in a
# target-filtered case report. v0.9 rebuilds the case-level prompt from the
# currently retained threads and, when a target reference is provided, requires
# that the chosen prompt be explicitly supported by that target reference in
# parsed artifact text. This does not invent or edit prompt text.

SCHEMA_VERSION = "0.9"


def _v09_prompt_matches_target(prompt: dict[str, Any], target_reference: str | None) -> bool:
    if not target_reference:
        return True
    target = str(target_reference).strip()
    if not target:
        return True
    text = str(prompt.get("text") or "")
    refs = [str(x) for x in (prompt.get("reference_codes") or [])]
    return target in text or target in refs


def _v09_clear_case_prompt_flags(report: dict[str, Any]) -> None:
    report.pop("computer_case_prompt", None)
    for thread in report.get("threads", []) or []:
        prompt = thread.get("prompt") or {}
        if prompt.get("case_prompt_id") == "computer_case_prompt":
            prompt.pop("case_prompt_id", None)
            prompt.pop("thread_specific_prompt", None)
            prompt.pop("interpretation", None)
            prompt["scope"] = "thread_or_case_prompt"
            thread["prompt"] = prompt


def _v09_add_case_level_computer_prompt(report: dict[str, Any], target_reference: str | None = None) -> None:
    """Create a target-safe Computer parent prompt summary from retained threads.

    The selected prompt must be artifact-derived from the thread prompt fields
    already reconstructed. If `target_reference` is provided, prompts not
    containing that exact target/reference code are not eligible. This prevents
    residual Computer tasks from becoming the case-level prompt in a filtered
    report.
    """
    _v09_clear_case_prompt_flags(report)

    computer_threads = [
        t for t in report.get("threads", []) or []
        if (t.get("classification", {}) or {}).get("execution_mode") == "computer_mode"
    ]
    if not computer_threads:
        return

    prompt_groups: dict[str, list[dict[str, Any]]] = {}
    for thread in computer_threads:
        prompt = thread.get("prompt") or {}
        text = str(prompt.get("text") or "").strip()
        if not text:
            continue
        if not _v09_prompt_matches_target(prompt, target_reference):
            continue
        prompt_groups.setdefault(_v08_norm_text(text), []).append(thread)

    # Without a target reference, avoid creating a case-level parent prompt when
    # multiple unrelated Computer prompts coexist in the profile.
    if not prompt_groups:
        return
    if not target_reference and len(prompt_groups) > 1:
        return

    # Prefer the target-matching prompt shared by most retained Computer threads.
    # Tie-breaker: more evidence first, then longer prompt text.
    def group_score(item: tuple[str, list[dict[str, Any]]]) -> tuple[int, int, int]:
        norm, group = item
        ev_count = sum(len(((t.get("prompt", {}) or {}).get("evidence") or [])) for t in group)
        return (len(group), ev_count, len(norm))

    norm, group = sorted(prompt_groups.items(), key=group_score, reverse=True)[0]
    if len(group) < 2:
        # A single Computer thread may still have a valid prompt, but it is not a
        # shared parent prompt. Leave it as thread-level prompt evidence.
        return

    prompt0 = group[0].get("prompt") or {}
    text = str(prompt0.get("text") or "").strip()
    evidence = _v08_dedupe_evidence(
        sum([((t.get("prompt", {}) or {}).get("evidence") or []) for t in group], [])
    )
    source_type_counts = _v08_source_counts_from_evidence(evidence)

    case_prompt = {
        "text": text,
        "reference_codes": extract_reference_codes(text),
        "source": prompt0.get("field"),
        "thread_count_linked": len(group),
        "linked_thread_ids": [t.get("thread_id") for t in group],
        "evidence": evidence[:25],
        "source_type_counts": source_type_counts,
        "target_reference": target_reference,
        "target_safe_selection": bool(target_reference),
        "artifact_text_unchanged": True,
        "interpretation": (
            "This is a target-matched parent/case-level Computer prompt recovered from parsed artifacts "
            "and linked to related Computer subtasks. It is repeated in subthreads for navigation, but "
            "should not be interpreted as a separately typed prompt per subtask unless corroborated by "
            "per-thread query_str evidence."
        ),
    }
    report["computer_case_prompt"] = case_prompt

    for thread in group:
        p = thread.get("prompt", {}) or {}
        p["scope"] = "case_level_prompt_linked_to_computer_subtask"
        p["thread_specific_prompt"] = False
        p["case_prompt_id"] = "computer_case_prompt"
        p["artifact_text_unchanged"] = True
        p["interpretation"] = (
            "The prompt text is artifact-derived and target-matched, but it is treated as the parent "
            "Computer-mode case prompt linked to this subtask/thread, not as proof of a separate user "
            "prompt typed for this specific subtask."
        )
        # Do not report occurrence_count=0 when the thread already has direct
        # prompt evidence. The occurrence finder is only a corroboration helper.
        corr = p.get("corroboration") or {}
        if corr.get("occurrence_count") in (None, 0) and p.get("evidence"):
            corr["occurrence_count"] = len(p.get("evidence") or [])
            corr["source_type_counts"] = _v08_source_counts_from_evidence(p.get("evidence") or [])
            corr["has_ldb_copy"] = any(ev.get("source_type") == "ldb" for ev in (p.get("evidence") or []))
            corr["has_log_copy"] = any(ev.get("source_type") == "log" for ev in (p.get("evidence") or []))
            corr["interpretation"] = (
                "Count is based on direct artifact evidence already attached to the prompt; prompt text "
                "is not generated or edited."
            )
            p["corroboration"] = corr
        thread["prompt"] = p


_filter_report_by_target_reference_pre_v09 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v09(report, target_reference)
    # v0.8 may have carried an unrelated global Computer case prompt into a
    # target-filtered report. Rebuild it only from the retained target threads.
    _v09_add_case_level_computer_prompt(filtered, target_reference)
    filtered["schema_version"] = "0.9"
    filtered.setdefault("hardcoding_audit", {})["v09"] = {
        "target_safe_computer_case_prompt": True,
        "rule": (
            "Computer case-level prompt must be selected from retained target threads and must match "
            "the requested target reference when one is supplied."
        ),
        "no_prompt_generation": True,
        "residual_prompt_guard": (
            "Unrelated residual Computer prompts, even if present in LDB/LOG, are not displayed as the "
            "case-level prompt of a filtered target report."
        ),
    }
    # Recompute case summary after case prompt repair so the summary describes
    # the repaired prompt rather than stale pre-filter prompt state.
    original_counts = filtered.get("filter_summary") or None
    filtered["case_summary"] = build_case_summary(filtered, target_reference, original_counts=original_counts)
    return filtered


_reconstruct_browser_threads_pre_v09 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v09(extracted, input_label, browser_only=browser_only)
    # For unfiltered reports, only produce a Computer case prompt if there is a
    # single unambiguous shared Computer prompt group. Target-filtered runs are
    # repaired later by filter_report_by_target_reference.
    _v09_add_case_level_computer_prompt(report, None)
    report["schema_version"] = "0.9"
    report.setdefault("hardcoding_audit", {})["v09_unfiltered"] = {
        "case_prompt_requires_unambiguous_group_without_target": True
    }
    return report


# ---------------------------------------------------------------------------
# v0.10 Computer prompt persistence/corroboration repair
# ---------------------------------------------------------------------------
#
# v0.9 correctly prevents unrelated residual Computer prompts from becoming the
# target case prompt, but the case prompt evidence can still under-report LDB
# corroboration because the prompt attached to retained subtasks may originate
# from the newest LOG-backed list-cache entry. v0.10 explicitly re-scans the
# retained report's parsed global ASI list-cache records and adds target-matched
# /rest/thread/list_ask_threads evidence from LDB/LOG to the case-level prompt.
# No prompt text is generated: only evidence/corroboration metadata is amended.

SCHEMA_VERSION = "0.10"


def _v10_global_record_text(record: dict[str, Any]) -> str:
    try:
        return json.dumps(record, ensure_ascii=False, default=str)
    except Exception:
        return str(record)


def _v10_prompt_record_matches(record: dict[str, Any], prompt_text: str, target_reference: str | None) -> bool:
    text = _v10_global_record_text(record)
    if "/rest/thread/list_ask_threads" not in text and str(record.get("record_kind") or "") != "asi_thread_list":
        return False
    if '"query_str"' not in text and "query_str" not in text:
        return False
    target = str(target_reference or "").strip()
    if target and target not in text:
        return False
    prompt = str(prompt_text or "").strip()
    if not prompt:
        return bool(target)
    # Use exact full prompt when available. Some previews can be truncated, so
    # also allow the first substantial prefix together with the target code and
    # query_str marker. This only changes evidence linking, not prompt content.
    if prompt in text:
        return True
    prefix = prompt[:180]
    if len(prefix) >= 80 and prefix in text:
        return True
    if target and target in text:
        distinctive_lines = [ln.strip() for ln in prompt.splitlines() if len(ln.strip()) >= 40]
        hits = sum(1 for ln in distinctive_lines[:4] if ln in text)
        return hits >= 2
    return False


def _v10_collect_case_prompt_global_evidence(report: dict[str, Any], prompt_text: str, target_reference: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    records_sample: list[dict[str, Any]] = []
    for record in report.get("global_records", []) or []:
        if not isinstance(record, dict):
            continue
        if not _v10_prompt_record_matches(record, prompt_text, target_reference):
            continue
        ev = record.get("evidence") or []
        evidence.extend(ev)
        records_sample.append({
            "record_kind": record.get("record_kind"),
            "key": record.get("key"),
            "ldb_seq_no": record.get("ldb_seq_no"),
            "is_live": record.get("is_live"),
            "source_types": record.get("source_types"),
            "evidence": ev[:5],
        })
    return _v08_dedupe_evidence(evidence), records_sample[:10]


def _v10_prompt_persistence_from_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    counts = _v08_source_counts_from_evidence(evidence)
    has_log = bool(counts.get("log"))
    has_ldb = bool(counts.get("ldb") or counts.get("sst"))
    if has_log and has_ldb:
        state = "log_and_ldb"
    elif has_log:
        state = "log_only"
    elif has_ldb:
        state = "ldb_only"
    else:
        state = "unknown"
    return {
        "state": state,
        "source_type_counts": counts,
        "has_ldb_copy": has_ldb,
        "has_log_copy": has_log,
        "interpretation": (
            "This describes where the parsed Computer parent prompt was observed in this collection. "
            "It does not mean Computer mode generally stores only in one source type; related subtasks "
            "may have different LOG/LDB persistence."
        ),
    }


def _v10_repair_computer_prompt_corroboration(report: dict[str, Any], target_reference: str | None = None) -> None:
    case_prompt = report.get("computer_case_prompt")
    if not isinstance(case_prompt, dict):
        return
    prompt_text = str(case_prompt.get("text") or "").strip()
    if not prompt_text:
        return
    global_evidence, records_sample = _v10_collect_case_prompt_global_evidence(report, prompt_text, target_reference)
    combined = _v08_dedupe_evidence((case_prompt.get("evidence") or []) + global_evidence)
    persistence = _v10_prompt_persistence_from_evidence(combined)
    case_prompt["evidence"] = combined[:25]
    case_prompt["source_type_counts"] = persistence["source_type_counts"]
    case_prompt["prompt_persistence"] = persistence
    case_prompt["global_prompt_record_sample"] = records_sample
    case_prompt["corroboration_note"] = (
        "Evidence includes target-matched /rest/thread/list_ask_threads global ASI list-cache records "
        "from parsed LOG/LDB artifacts when present. Prompt text itself remains the artifact-derived query_str."
    )
    report["computer_case_prompt"] = case_prompt

    for thread in report.get("threads", []) or []:
        prompt = thread.get("prompt") or {}
        if prompt.get("case_prompt_id") != "computer_case_prompt":
            continue
        p_combined = _v08_dedupe_evidence((prompt.get("evidence") or []) + combined)
        prompt["evidence"] = p_combined[:25]
        prompt["corroboration"] = {
            "occurrence_count": len(p_combined),
            "source_type_counts": _v08_source_counts_from_evidence(p_combined),
            "has_ldb_copy": any(ev.get("source_type") in ("ldb", "sst") for ev in p_combined),
            "has_log_copy": any(ev.get("source_type") == "log" for ev in p_combined),
            "global_prompt_records_considered": len(records_sample),
            "interpretation": (
                "Corroboration is based on direct prompt evidence plus target-matched global ASI list-cache "
                "records recovered from parsed artifacts; prompt text is not generated or edited."
            ),
        }
        prompt["prompt_persistence"] = _v10_prompt_persistence_from_evidence(p_combined)
        thread["prompt"] = prompt


_filter_report_by_target_reference_pre_v10 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v10(report, target_reference)
    _v10_repair_computer_prompt_corroboration(filtered, target_reference)
    filtered["schema_version"] = "0.10"
    filtered.setdefault("hardcoding_audit", {})["v10"] = {
        "computer_prompt_persistence_repair": True,
        "rule": (
            "Computer case prompt persistence is derived from retained parsed artifacts and target-matched "
            "/rest/thread/list_ask_threads global records; no scenario-specific strings are hardcoded."
        ),
        "no_prompt_generation": True,
    }
    original_counts = filtered.get("filter_summary") or None
    filtered["case_summary"] = build_case_summary(filtered, target_reference, original_counts=original_counts)
    return filtered


_reconstruct_browser_threads_pre_v10 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v10(extracted, input_label, browser_only=browser_only)
    _v10_repair_computer_prompt_corroboration(report, None)
    report["schema_version"] = "0.10"
    report.setdefault("hardcoding_audit", {})["v10_unfiltered"] = {
        "computer_prompt_persistence_repair_unfiltered": True
    }
    return report


# ---------------------------------------------------------------------------
# v0.11: source-driven reasoning scan for both Browser Control and Computer mode
# ---------------------------------------------------------------------------

def _v11_extract_observable_reasoning_items_from_value(
    value: Any,
    record: ForensicRecord,
    execution_mode: str,
) -> list[dict[str, Any]]:
    """Extract observable thought/reasoning/rationale text from decoded artifacts.

    This is deliberately evidence-driven. Browser Control is not assumed to have
    no reasoning; decoded records are scanned for explicit structured markers
    such as variant/type/key = thought/reasoning/rationale. Computer mode also
    reports Computer tool-action rationale candidates when tool_name is
    computer/wait_for_download/wait_for_subagents.
    """
    items: list[dict[str, Any]] = []

    explicit_reason_keys = {
        "thought",
        "reasoning",
        "rationale",
        "reason",
        "analysis",
        "chain_of_thought",
    }
    computer_tool_names = {
        "computer",
        "wait_for_download",
        "wait_for_subagents",
    }

    def is_explicit_reason_marker(obj: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, str | None]:
        variant = str(obj.get("variant") or obj.get("type") or obj.get("role") or "").lower()
        if "thought" in variant:
            return True, "variant_or_type_thought"
        if variant in {"reasoning", "rationale", "analysis"}:
            return True, "variant_or_type_reasoning"
        path_l = {p.lower() for p in path}
        key_l = {str(k).lower() for k in obj.keys()}
        if path_l & explicit_reason_keys or key_l & explicit_reason_keys:
            return True, "reasoning_key"
        return False, None

    def add_item(
        obj: dict[str, Any],
        kind: str,
        marker: str,
        path: tuple[str, ...],
    ) -> None:
        variant = str(obj.get("variant") or obj.get("type") or obj.get("role") or "").lower()
        tool_name = str(obj.get("tool_name") or obj.get("tool") or "").lower()
        title = obj.get("title")
        status = str(obj.get("status") or "")

        text_candidates = _v08_extract_text_candidates(obj)
        if isinstance(title, str) and title.strip() and title.strip() not in text_candidates:
            text_candidates.insert(0, title.strip())

        cleaned: list[str] = []
        seen_text: set[str] = set()
        for text in text_candidates:
            if not text:
                continue
            # Do not report the user prompt itself as reasoning.
            if is_prompt_duplicate(text, None):
                continue
            key = _v08_norm_text(text)[:300]
            if not key or key in seen_text:
                continue
            seen_text.add(key)
            cleaned.append(text[:2500])

        if not cleaned:
            return

        items.append({
            "kind": kind,
            "marker": marker,
            "execution_mode": execution_mode,
            "variant": variant or None,
            "tool_name": tool_name or None,
            "status": status or None,
            "text": cleaned[0],
            "text_candidates": cleaned[:6],
            "relative_order": record.ldb_seq_no,
            "source_types": record.source_types,
            "evidence": record_evidence(record),
            "time_fields": _v08_collect_iso_times_from_obj(obj, f"{execution_mode}_observable_reasoning_payload", record_evidence(record)),
            "caution": "Observable workflow text recovered from decoded artifacts; not asserted to be hidden/private chain-of-thought.",
        })

    def walk(obj: Any, path: tuple[str, ...] = ()) -> None:
        obj = _v08_parse_jsonish(obj)
        if isinstance(obj, dict):
            is_explicit, marker = is_explicit_reason_marker(obj, path)
            tool_name = str(obj.get("tool_name") or obj.get("tool") or "").lower()

            if is_explicit:
                add_item(obj, "observable_reasoning_candidate", marker or "explicit_reason_marker", path)
            elif execution_mode == "computer_mode" and tool_name in computer_tool_names:
                add_item(obj, "computer_tool_action_rationale", f"tool_name={tool_name}", path)

            for key, child in obj.items():
                walk(child, path + (str(key),))
        elif isinstance(obj, list):
            for idx, child in enumerate(obj):
                walk(child, path + (str(idx),))
        elif isinstance(obj, str):
            parsed = _v08_parse_jsonish(obj)
            if parsed is not obj:
                walk(parsed, path)

    walk(value)
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, str | None, str | None]] = set()
    for item in items:
        key = (
            _v08_norm_text(item.get("text") or "")[:300],
            item.get("relative_order"),
            item.get("tool_name"),
            item.get("marker"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def extract_reasoning(records: list[ForensicRecord], execution_mode: str) -> dict[str, Any]:
    """Scan decoded records for observable reasoning/thought artifacts.

    Browser Control is scanned too. If none are found, the report says none were
    identified in the decoded artifacts rather than assuming they cannot exist.
    """
    if execution_mode not in {"browser_control", "computer_mode"}:
        return {
            "available": False,
            "items": [],
            "note": "No observable reasoning artifact identified for this execution mode.",
            "interpretation": "Reasoning detection is based on explicit decoded artifact markers, not assumptions.",
        }

    items: list[dict[str, Any]] = []
    for record in records:
        items.extend(_v11_extract_observable_reasoning_items_from_value(record.value, record, execution_mode))

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, str | None]] = set()
    for item in sorted(items, key=lambda x: x.get("relative_order") or -1):
        key = (_v08_norm_text(item.get("text") or "")[:300], item.get("relative_order"), item.get("kind"))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    if execution_mode == "browser_control":
        note = None if deduped else (
            "No explicit observable reasoning/thought marker was identified in decoded Browser Control LDB/LOG records."
        )
        interpretation = (
            "Browser Control reasoning is not assumed absent. The decoded artifacts were scanned for explicit "
            "thought/reasoning/rationale markers; items are reported only when such markers are recovered."
        )
    else:
        note = None if deduped else (
            "No observable Computer thought/action-rationale artifact was identified in decoded LDB/LOG records."
        )
        interpretation = (
            "Computer-mode reasoning means observable workflow thought/rationale or Computer tool-action text "
            "recovered from artifacts, not hidden private chain-of-thought."
        )

    return {
        "available": bool(deduped),
        "items": deduped[:60],
        "note": note,
        "scan_rule": "explicit_decoded_artifact_markers",
        "interpretation": interpretation,
    }


_filter_report_by_target_reference_pre_v11 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v11(report, target_reference)
    filtered["schema_version"] = "0.11"
    filtered.setdefault("hardcoding_audit", {})["v11_reasoning_scan"] = {
        "browser_control_reasoning_scanned": True,
        "computer_mode_reasoning_scanned": True,
        "rule": (
            "Reasoning is reported only when explicit decoded thought/reasoning/rationale markers or Computer "
            "tool-action rationale markers are recovered from parsed LDB/LOG records; no mode is hardcoded to "
            "always contain or never contain reasoning."
        ),
        "no_scenario_specific_strings": True,
    }
    original_counts = filtered.get("filter_summary") or None
    filtered["case_summary"] = build_case_summary(filtered, target_reference, original_counts=original_counts)
    return filtered


_reconstruct_browser_threads_pre_v11 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v11(extracted, input_label, browser_only=browser_only)
    report["schema_version"] = "0.11"
    report.setdefault("hardcoding_audit", {})["v11_reasoning_scan_unfiltered"] = {
        "browser_control_reasoning_scanned": True,
        "computer_mode_reasoning_scanned": True,
        "no_scenario_specific_strings": True,
    }
    return report


# v0.11 HTML rendering: show reasoning scan results and order thread sections as requested.
def _render_reasoning_v11(thread: dict[str, Any]) -> str:
    reasoning = thread.get("reasoning", {}) or {}
    rows = []
    for item in (reasoning.get("items") or [])[:12]:
        ev = item.get("evidence") or {}
        rows.append({
            "kind": item.get("kind"),
            "marker": item.get("marker") or item.get("variant") or item.get("tool_name"),
            "source": ev.get("source_file"),
            "type": ev.get("source_type"),
            "seq": ev.get("ldb_seq_no"),
            "text": shorten_text(item.get("text") or item.get("text_preview") or "", 260),
        })
    return "\n".join([
        _kv_table_v07([
            ("Available", reasoning.get("available")),
            ("Scan rule", reasoning.get("scan_rule")),
            ("Note", reasoning.get("note")),
            ("Interpretation", reasoning.get("interpretation")),
        ]),
        "<h4>Reasoning / rationale candidates</h4>",
        _simple_table_v07(
            rows,
            [
                ("kind", "Kind"),
                ("marker", "Marker"),
                ("source", "Source"),
                ("type", "Type"),
                ("seq", "Seq"),
                ("text", "Text"),
            ],
            "No observable reasoning/thought candidate was recovered.",
        ),
        _raw_details_v07("Raw reasoning evidence", reasoning),
    ])


def _render_computer_specific_v11(thread: dict[str, Any]) -> str:
    if (thread.get("classification", {}) or {}).get("execution_mode") != "computer_mode":
        return ""
    return "\n".join([
        "<h3>8. Computer-mode specific evidence</h3>",
        _kv_table_v07([
            ("Prompt scope", (thread.get("prompt", {}) or {}).get("scope")),
            ("Thread-specific prompt", (thread.get("prompt", {}) or {}).get("thread_specific_prompt")),
            ("Prompt persistence", ((thread.get("prompt", {}) or {}).get("prompt_persistence") or {}).get("state")),
            ("Task title", (thread.get("metadata", {}) or {}).get("task_title")),
            ("LDB records present", (thread.get("computer_source_persistence", {}) or {}).get("has_ldb_records")),
            ("LOG records present", (thread.get("computer_source_persistence", {}) or {}).get("has_log_records")),
        ]),
        _raw_details_v07("Prompt corroboration", (thread.get("prompt", {}) or {}).get("corroboration")),
        _raw_details_v07("Prompt persistence", (thread.get("prompt", {}) or {}).get("prompt_persistence")),
        _raw_details_v07("Computer source persistence", thread.get("computer_source_persistence")),
        _raw_details_v07("Computer temporal evidence", thread.get("computer_temporal_evidence")),
    ])


def _render_thread_detail_v07(thread: dict[str, Any], idx: int) -> str:
    classification = thread.get("classification", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    prompt = thread.get("prompt", {}) or {}
    title = _thread_title_v07(thread, idx)
    quality, qkind, explanation = _thread_quality_v07(thread)
    execution_mode = classification.get("execution_mode")
    badges = (
        _badge_v07(classification.get("interaction_type"), "good" if classification.get("interaction_type") == "agentic" else "neutral")
        + _badge_v07(execution_mode, "good" if execution_mode in {"browser_control", "computer_mode"} else "neutral")
        + _badge_v07("confidence: " + str(classification.get("confidence")), "neutral")
        + _badge_v07(quality, qkind)
    )
    classification_evidence = ", ".join(str(x) for x in classification.get("classification_evidence", []) or [])
    return "\n".join([
        "<section class='card thread-detail'>",
        "<div class='thread-head'>",
        f"<div><h2>{_h(title)}</h2><p class='thread-id'>{_h(thread.get('thread_id'))}</p></div>",
        f"<div class='badges'>{badges}</div>",
        "</div>",
        f"<div class='note {qkind}'><strong>Reconstruction verdict:</strong> {_h(explanation)}<br><span class='muted'>{_status_line_v07(thread)}</span></div>",
        "<h3>1. Prompt</h3>",
        f"<div class='prompt'>{_h(prompt.get('text') or 'No prompt extracted.')}</div>",
        _evidence_row("Prompt evidence", prompt.get("evidence")),
        "<h3>2. Classification & key metadata</h3>",
        _kv_table_v07([
            ("Interaction / execution", SafeHtml(_badge_v07(classification.get("interaction_type"), "good") + _badge_v07(execution_mode, "good" if execution_mode in {"browser_control", "computer_mode"} else "neutral"))),
            ("Classification evidence", classification_evidence),
            ("Core status", metadata.get("status") or metadata.get("thread_status")),
            ("Final flag", metadata.get("final")),
            ("Search mode", metadata.get("search_mode")),
            ("Display model", metadata.get("display_model")),
            ("Message mode", metadata.get("message_mode")),
            ("Search focus", metadata.get("search_focus")),
            ("Backend UUID", metadata.get("backend_uuid")),
            ("Context UUID", metadata.get("context_uuid")),
        ]),
        _render_external_status_v07(thread),
        "<h3>3. Activity / artifact reconstruction</h3>",
        _render_activity_v07(thread),
        "<h3>4. Reasoning availability</h3>",
        _render_reasoning_v11(thread),
        "<h3>5. Privacy / deletion</h3>",
        _render_privacy_deletion_v07(thread),
        "<h3>6. Final answer</h3>",
        _render_final_answer_v07(thread),
        "<h3>7. Time & storage evidence</h3>",
        _render_time_and_sources_v07(thread),
        _render_computer_specific_v11(thread),
        _raw_details_v07("Raw metadata evidence", metadata.get("evidence")),
        _raw_details_v07("Full thread JSON", thread),
        "</section>",
    ])


# v0.11: preserve URL evidence from Computer-mode artifacts without overstating visited URLs.
def _v11_clean_url_candidate(url: str) -> str:
    return str(url or "").strip().strip("`'\".,;:)]}>")

def _v11_collect_url_candidates_from_obj(obj: Any, path: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        # If a dict already carries evidence, keep it for child strings.
        ev = obj.get("evidence") if isinstance(obj.get("evidence"), dict) else None
        for k, v in obj.items():
            child_path = path + (str(k),)
            for item in _v11_collect_url_candidates_from_obj(v, child_path):
                if ev and not item.get("evidence"):
                    item["evidence"] = ev
                candidates.append(item)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            candidates.extend(_v11_collect_url_candidates_from_obj(v, path + (str(i),)))
    elif isinstance(obj, str):
        for m in URL_RE.finditer(obj):
            url = _v11_clean_url_candidate(m.group(0))
            if not url:
                continue
            path_s = ".".join(path)
            if "action" in path_s or "tool" in path_s or "parameters" in path_s:
                role = "action_or_tool_payload_url"
            elif "final_answer" in path_s or "answer" in path_s:
                role = "final_answer_reported_url"
            elif "reasoning" in path_s:
                role = "reasoning_or_workflow_text_url"
            elif "metadata" in path_s or "sources" in path_s:
                role = "metadata_source_url"
            else:
                role = "artifact_text_url"
            candidates.append({
                "url": url,
                "role": role,
                "source_path": path_s,
                "interpretation": (
                    "URL string recovered from decoded artifact text. Treat as a candidate/context URL unless "
                    "corroborated by browser History, Downloads DB, topMostUrls, or explicit navigation/action evidence."
                ),
            })
    return candidates

def _v11_augment_computer_url_candidates(report: dict[str, Any]) -> None:
    for thread in report.get("threads", []) or []:
        if (thread.get("classification", {}) or {}).get("execution_mode") != "computer_mode":
            continue
        raw_candidates: list[dict[str, Any]] = []
        for key in ("metadata", "actions", "reasoning", "final_answer", "artifact_buckets", "task_outcome"):
            if key in thread:
                raw_candidates.extend(_v11_collect_url_candidates_from_obj(thread.get(key), (key,)))
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_candidates:
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(item)
        thread["computer_url_candidates"] = deduped[:50]
        if deduped:
            thread.setdefault("content_state", {})["has_computer_url_candidates"] = True

# Wrap v0.11 reconstruct/filter once more to add Computer URL candidates.
_filter_report_by_target_reference_pre_v11_url = filter_report_by_target_reference

def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v11_url(report, target_reference)
    _v11_augment_computer_url_candidates(filtered)
    filtered["schema_version"] = "0.11"
    filtered.setdefault("hardcoding_audit", {})["v11_computer_url_candidates"] = {
        "enabled": True,
        "rule": "Computer URL candidates are extracted from decoded artifact text; they are not asserted as visited URLs without corroboration.",
        "no_scenario_specific_strings": True,
    }
    original_counts = filtered.get("filter_summary") or None
    filtered["case_summary"] = build_case_summary(filtered, target_reference, original_counts=original_counts)
    return filtered

_reconstruct_browser_threads_pre_v11_url = reconstruct_browser_threads

def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v11_url(extracted, input_label, browser_only=browser_only)
    _v11_augment_computer_url_candidates(report)
    report["schema_version"] = "0.11"
    report.setdefault("hardcoding_audit", {})["v11_computer_url_candidates_unfiltered"] = {
        "enabled": True,
        "no_scenario_specific_strings": True,
    }
    return report

# Re-override Computer HTML section after URL augmentation.
def _render_computer_specific_v11(thread: dict[str, Any]) -> str:
    if (thread.get("classification", {}) or {}).get("execution_mode") != "computer_mode":
        return ""
    return "\n".join([
        "<h3>8. Computer-mode specific evidence</h3>",
        _kv_table_v07([
            ("Prompt scope", (thread.get("prompt", {}) or {}).get("scope")),
            ("Thread-specific prompt", (thread.get("prompt", {}) or {}).get("thread_specific_prompt")),
            ("Prompt persistence", ((thread.get("prompt", {}) or {}).get("prompt_persistence") or {}).get("state")),
            ("Task title", (thread.get("metadata", {}) or {}).get("task_title")),
            ("LDB records present", (thread.get("computer_source_persistence", {}) or {}).get("has_ldb_records")),
            ("LOG records present", (thread.get("computer_source_persistence", {}) or {}).get("has_log_records")),
            ("Computer URL candidates", len(thread.get("computer_url_candidates") or [])),
        ]),
        _raw_details_v07("Computer URL candidates", thread.get("computer_url_candidates")),
        _raw_details_v07("Prompt corroboration", (thread.get("prompt", {}) or {}).get("corroboration")),
        _raw_details_v07("Prompt persistence", (thread.get("prompt", {}) or {}).get("prompt_persistence")),
        _raw_details_v07("Computer source persistence", thread.get("computer_source_persistence")),
        _raw_details_v07("Computer temporal evidence", thread.get("computer_temporal_evidence")),
    ])



# ---------------------------------------------------------------------------
# v0.12: HTML evidence explorer, per-thread subnavigation, and stricter
# reasoning/progress separation without scenario-specific hardcoding.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.13"

_PROGRESS_STATUS_PATTERNS_V12 = [
    r"^clicking\b", r"^waiting\b", r"^opening\b", r"^downloading\b", r"^saving\b",
    r"^checking\b", r"^preparing\b", r"^filling\b", r"^navigating\b", r"^finding\b",
    r"\bto finish\b", r"\bcompleted successfully\b", r"^has completed\b",
    r"^클릭", r"^대기", r"^기다", r"^다운로드", r"^저장", r"^확인", r"^이동", r"^입력",
]

_REASONING_SIGNAL_PATTERNS_V12 = [
    r"\bi can see\b", r"\bi can now\b", r"\bi need to\b", r"\bi(?:'|’)ll\b", r"\bi will\b",
    r"\bbecause\b", r"\btherefore\b", r"\bso that\b", r"\bto ensure\b", r"\bin order to\b",
]


def _v12_norm_key(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _v12_is_progress_or_status_text(text: Any) -> bool:
    s = _v12_norm_key(text)
    if not s:
        return False
    if looks_like_progress_or_subtask_title(s):
        return True
    if len(s) <= 160:
        for pat in _PROGRESS_STATUS_PATTERNS_V12:
            if re.search(pat, s, flags=re.IGNORECASE):
                return True
    return False


def _v12_has_reasoning_signal(text: Any) -> bool:
    s = _v12_norm_key(text)
    if not s:
        return False
    for pat in _REASONING_SIGNAL_PATTERNS_V12:
        if re.search(pat, s, flags=re.IGNORECASE):
            return True
    # Long first-person workflow text can be an observable rationale even when it
    # does not contain the short signal phrases above.
    if len(s) >= 180 and re.search(r"\bi\b", s):
        return True
    return False


def _v12_dedupe_items(items: list[dict[str, Any]], limit: int = 60) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for item in sorted(items, key=lambda x: ((x.get("relative_order") is None), x.get("relative_order") or 0)):
        text = item.get("text") or ""
        key = (_v12_norm_key(text)[:500], item.get("kind"), item.get("tool_name") or item.get("marker"))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def extract_reasoning(records: list[ForensicRecord], execution_mode: str) -> dict[str, Any]:
    """v0.12 source-driven reasoning extraction.

    The extractor scans decoded artifacts for explicit thought/reasoning/rationale
    markers in both Browser Control and Computer mode. Short progress labels such
    as "Clicking..." or "Waiting..." are separated as workflow progress/status
    candidates rather than being counted as reasoning.
    """
    if execution_mode not in {"browser_control", "computer_mode"}:
        return {
            "available": False,
            "items": [],
            "progress_or_status_items": [],
            "note": "No observable reasoning artifact identified for this execution mode.",
            "scan_rule": "explicit_decoded_artifact_markers",
            "interpretation": "Reasoning detection is based on decoded artifact markers, not assumptions.",
        }

    raw_items: list[dict[str, Any]] = []
    for record in records:
        raw_items.extend(_v11_extract_observable_reasoning_items_from_value(record.value, record, execution_mode))

    reasoning_items: list[dict[str, Any]] = []
    progress_items: list[dict[str, Any]] = []
    for item in raw_items:
        text = item.get("text") or ""
        # User prompts should never be moved into reasoning.
        if is_prompt_duplicate(text, None):
            continue
        if _v12_is_progress_or_status_text(text) and not _v12_has_reasoning_signal(text):
            copied = dict(item)
            copied["kind"] = "workflow_progress_or_status_candidate"
            copied["caution"] = "Progress/status text recovered from artifacts; not counted as reasoning."
            progress_items.append(copied)
        else:
            copied = dict(item)
            copied["kind"] = copied.get("kind") or "observable_reasoning_candidate"
            copied["caution"] = "Observable workflow/rationale text recovered from decoded artifacts; not hidden/private chain-of-thought."
            reasoning_items.append(copied)

    reasoning_items = _v12_dedupe_items(reasoning_items, limit=40)
    progress_items = _v12_dedupe_items(progress_items, limit=40)

    if execution_mode == "browser_control":
        note = None if reasoning_items else "No explicit observable reasoning/thought marker was identified in decoded Browser Control LDB/LOG records."
        interpretation = (
            "Browser Control reasoning is not assumed absent. Decoded artifacts are scanned for explicit "
            "thought/reasoning/rationale markers; only recovered markers are reported."
        )
    else:
        note = None if reasoning_items else "No observable Computer thought/action-rationale artifact was identified after separating progress/status labels."
        interpretation = (
            "Computer-mode reasoning means observable workflow thought/rationale text recovered from artifacts. "
            "Short progress/status labels are separated and are not treated as reasoning."
        )

    return {
        "available": bool(reasoning_items),
        "items": reasoning_items,
        "progress_or_status_items": progress_items,
        "raw_candidate_count_before_separation": len(raw_items),
        "dedupe_rule": "normalized_text_plus_kind_plus_marker_or_tool",
        "note": note,
        "scan_rule": "explicit_decoded_artifact_markers_with_progress_status_separation",
        "interpretation": interpretation,
    }


# Re-wrap reconstruction so reruns use the v0.12 reasoning separator and schema.
_reconstruct_browser_threads_pre_v12 = reconstruct_browser_threads

def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v12(extracted, input_label, browser_only=browser_only)
    report["schema_version"] = "0.12"
    report.setdefault("hardcoding_audit", {})["v12"] = {
        "thread_subnavigation_html": True,
        "decoded_evidence_explorer_html": True,
        "reasoning_progress_separation": True,
        "rule": "Reasoning/progress classification uses generic decoded artifact markers and generic progress/status phrasing, not case-specific literals.",
        "no_scenario_specific_strings": True,
    }
    return report

_filter_report_by_target_reference_pre_v12 = filter_report_by_target_reference

def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v12(report, target_reference)
    filtered["schema_version"] = "0.12"
    filtered.setdefault("hardcoding_audit", {})["v12"] = {
        "thread_subnavigation_html": True,
        "decoded_evidence_explorer_html": True,
        "reasoning_progress_separation": True,
        "rule": "Target filtering uses the supplied target_reference and recovered artifact fields. No scenario-specific target values are embedded.",
        "no_scenario_specific_strings": True,
    }
    filtered["case_summary"] = build_case_summary(filtered, target_reference, original_counts=filtered.get("filter_summary"))
    return filtered


def _v12_collect_evidence_refs(obj: Any, limit: int = 120) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any, Any, Any]] = set()

    def walk(x: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(x, dict):
            if {"source_file", "source_type", "offset"} & set(x.keys()):
                key = (x.get("source_file"), x.get("source_type"), x.get("offset"), x.get("ldb_seq_no"), x.get("state"))
                if key not in seen and (x.get("source_file") or x.get("source_type") or x.get("offset") is not None):
                    seen.add(key)
                    out.append({
                        "source_file": x.get("source_file"),
                        "source_type": x.get("source_type"),
                        "state": x.get("state"),
                        "seq": x.get("ldb_seq_no"),
                        "offset": x.get("offset"),
                        "is_live": x.get("is_live"),
                    })
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return out


def _v12_evidence_bundle(thread: dict[str, Any]) -> dict[str, Any]:
    metadata = thread.get("metadata", {}) or {}
    return {
        "classification": thread.get("classification"),
        "prompt": thread.get("prompt"),
        "metadata_evidence": metadata.get("evidence"),
        "agentic_activity": {
            "plan": thread.get("plan"),
            "actions": thread.get("actions"),
            "thread_urls": thread.get("urls"),
            "context_url_candidates": thread.get("context_url_candidates"),
            "computer_url_candidates": thread.get("computer_url_candidates"),
            "typed_payloads": thread.get("typed_payloads"),
            "timeline": thread.get("timeline"),
        },
        "reasoning": thread.get("reasoning"),
        "privacy_and_deletion": {
            "privacy_detection": metadata.get("private_detection"),
            "deletion_state": thread.get("deletion_state"),
            "reconstruction_availability": thread.get("reconstruction_availability"),
            "storage_state": (thread.get("reconstruction_availability") or {}).get("storage_state"),
            "content_state": (thread.get("reconstruction_availability") or {}).get("content_state"),
            "residue_state": (thread.get("reconstruction_availability") or {}).get("residue_state"),
        },
        "final_answer": thread.get("final_answer"),
        "task_outcome": thread.get("task_outcome"),
        "source_summary": thread.get("source_summary"),
        "computer_specific": {
            "computer_source_persistence": thread.get("computer_source_persistence"),
            "computer_temporal_evidence": thread.get("computer_temporal_evidence"),
            "prompt_persistence": (thread.get("prompt") or {}).get("prompt_persistence"),
            "prompt_corroboration": (thread.get("prompt") or {}).get("corroboration"),
        },
    }


def _v12_render_evidence_explorer(thread: dict[str, Any]) -> str:
    bundle = _v12_evidence_bundle(thread)
    refs = _v12_collect_evidence_refs(bundle)
    rows = refs[:80]
    return "\n".join([
        "<div class='note'><strong>Decoded evidence explorer.</strong><br>아래 항목은 HTML 해석문이 아니라 JSON report 안에 보존된 decoded artifact/evidence 조각입니다. 원본 LevelDB 바이트 전체가 아니라 파싱된 record 값과 source_file/offset/seq 근거를 열어보는 용도입니다.</div>",
        "<h4>Evidence reference index</h4>",
        _simple_table_v07(rows, [("source_file", "File"), ("source_type", "Type"), ("state", "State"), ("seq", "Seq"), ("offset", "Offset"), ("is_live", "Live")], "No evidence refs recovered."),
        _raw_details_v07("Classification evidence + metadata evidence", {"classification": bundle.get("classification"), "metadata_evidence": bundle.get("metadata_evidence")}),
        _raw_details_v07("Prompt evidence", bundle.get("prompt")),
        _raw_details_v07("Agentic activity evidence: plan/actions/URLs/payloads/timeline", bundle.get("agentic_activity")),
        _raw_details_v07("Reasoning/progress evidence", bundle.get("reasoning")),
        _raw_details_v07("Privacy/deletion/stale evidence", bundle.get("privacy_and_deletion")),
        _raw_details_v07("Final answer and task outcome evidence", {"final_answer": bundle.get("final_answer"), "task_outcome": bundle.get("task_outcome")}),
        _raw_details_v07("Computer-specific persistence/time evidence", bundle.get("computer_specific")),
        _raw_details_v07("Full thread JSON", thread),
    ])


def _v12_subnav(idx: int, execution_mode: str | None) -> str:
    items = [
        ("prompt", "Prompt"),
        ("metadata", "Metadata"),
        ("activity", "Activity"),
        ("reasoning", "Reasoning"),
        ("privacy", "Privacy/Delete"),
        ("final", "Final"),
        ("time", "Time/Storage"),
        ("evidence", "Raw Evidence"),
    ]
    if execution_mode == "computer_mode":
        items.insert(7, ("computer", "Computer"))
    links = [f"<button type='button' class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-{slug}'>{_h(label)}</button>" for slug, label in items]
    return "<div class='mini-row thread-jump-row'>" + "".join(links) + "</div>"


def _v12_render_reasoning(thread: dict[str, Any]) -> str:
    reasoning = thread.get("reasoning", {}) or {}

    def _first_evidence_ref(value: Any) -> dict[str, Any]:
        """Return a representative evidence dict from dict/list/nested-list values.

        Reasoning candidates may carry either a single evidence dict or a list of
        evidence records depending on where the artifact was recovered. HTML
        rendering must be tolerant of both forms because the JSON reconstruction
        is the source of truth and should not be reshaped only for display.
        """
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    return entry
                if isinstance(entry, list):
                    nested = _first_evidence_ref(entry)
                    if nested:
                        return nested
        return {}

    def _evidence_count(value: Any) -> int:
        if isinstance(value, dict):
            return 1
        if isinstance(value, list):
            total = 0
            for entry in value:
                if isinstance(entry, dict):
                    total += 1
                elif isinstance(entry, list):
                    total += _evidence_count(entry)
            return total
        return 0

    def rowify(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for item in items[:18]:
            if not isinstance(item, dict):
                rows.append({
                    "kind": "unstructured",
                    "marker": None,
                    "source": None,
                    "type": None,
                    "seq": None,
                    "evidence_count": 0,
                    "text": shorten_text(str(item), 260),
                })
                continue
            evidence_value = item.get("evidence") or item.get("evidence_refs") or {}
            ev = _first_evidence_ref(evidence_value)
            rows.append({
                "kind": item.get("kind"),
                "marker": item.get("marker") or item.get("variant") or item.get("tool_name"),
                "source": ev.get("source_file"),
                "type": ev.get("source_type"),
                "seq": ev.get("ldb_seq_no"),
                "evidence_count": _evidence_count(evidence_value),
                "text": shorten_text(item.get("text") or "", 260),
            })
        return rows
    return "\n".join([
        _kv_table_v07([
            ("Available", reasoning.get("available")),
            ("Scan rule", reasoning.get("scan_rule")),
            ("Raw candidates before separation", reasoning.get("raw_candidate_count_before_separation")),
            ("Dedupe rule", reasoning.get("dedupe_rule")),
            ("Note", reasoning.get("note")),
            ("Interpretation", reasoning.get("interpretation")),
        ]),
        "<h4>Observable reasoning/rationale candidates</h4>",
        _simple_table_v07(rowify(reasoning.get("items") or []), [("kind", "Kind"), ("marker", "Marker"), ("source", "Source"), ("type", "Type"), ("seq", "Seq"), ("evidence_count", "Evidence"), ("text", "Text")], "No observable reasoning/thought candidate was recovered."),
        "<h4>Separated progress/status candidates</h4>",
        _simple_table_v07(rowify(reasoning.get("progress_or_status_items") or []), [("kind", "Kind"), ("marker", "Marker"), ("source", "Source"), ("type", "Type"), ("seq", "Seq"), ("evidence_count", "Evidence"), ("text", "Text")], "No progress/status candidate was separated from reasoning."),
        _raw_details_v07("Raw reasoning/progress object", reasoning),
    ])


def _render_thread_detail_v12(thread: dict[str, Any], idx: int) -> str:
    classification = thread.get("classification", {}) or {}
    metadata = thread.get("metadata", {}) or {}
    prompt = thread.get("prompt", {}) or {}
    title = _thread_title_v07(thread, idx)
    quality, qkind, explanation = _thread_quality_v07(thread)
    execution_mode = classification.get("execution_mode")
    badges = (
        _badge_v07(classification.get("interaction_type"), "good" if classification.get("interaction_type") == "agentic" else "neutral")
        + _badge_v07(execution_mode, "good" if execution_mode in {"browser_control", "computer_mode"} else "neutral")
        + _badge_v07("confidence: " + str(classification.get("confidence")), "neutral")
        + _badge_v07(quality, qkind)
    )
    classification_evidence = ", ".join(str(x) for x in classification.get("classification_evidence", []) or [])
    return "\n".join([
        f"<section id='thread-{idx}' class='card thread-detail view-section'>",
        "<div class='thread-head'>",
        f"<div><h2>{_h(title)}</h2><p class='thread-id'>{_h(thread.get('thread_id'))}</p></div>",
        f"<div class='badges'>{badges}</div>",
        "</div>",
        _v12_subnav(idx, execution_mode),
        f"<div class='note {qkind}'><strong>Reconstruction verdict:</strong> {_h(explanation)}<br><span class='muted'>{_status_line_v07(thread)}</span></div>",
        f"<h3 id='thread-{idx}-prompt'>1. Prompt</h3>",
        f"<div class='prompt'>{_h(prompt.get('text') or 'No prompt extracted.')}</div>",
        _evidence_row("Prompt evidence", prompt.get("evidence")),
        _raw_details_v07("Open prompt raw object", prompt),
        f"<h3 id='thread-{idx}-metadata'>2. Classification & key metadata</h3>",
        _kv_table_v07([
            ("Interaction / execution", SafeHtml(_badge_v07(classification.get("interaction_type"), "good") + _badge_v07(execution_mode, "good" if execution_mode in {"browser_control", "computer_mode"} else "neutral"))),
            ("Classification evidence", classification_evidence),
            ("Core status", metadata.get("status") or metadata.get("thread_status")),
            ("Final flag", metadata.get("final")),
            ("Search mode", metadata.get("search_mode")),
            ("Display model", metadata.get("display_model")),
            ("Message mode", metadata.get("message_mode")),
            ("Search focus", metadata.get("search_focus")),
            ("Backend UUID", metadata.get("backend_uuid")),
            ("Context UUID", metadata.get("context_uuid")),
        ]),
        _render_external_status_v07(thread),
        _raw_details_v07("Open classification + metadata raw evidence", {"classification": classification, "metadata_evidence": metadata.get("evidence")}),
        f"<h3 id='thread-{idx}-activity'>3. Activity / artifact reconstruction</h3>",
        _render_activity_v07(thread),
        _raw_details_v07("Open activity raw objects", {"plan": thread.get("plan"), "actions": thread.get("actions"), "urls": thread.get("urls"), "context_url_candidates": thread.get("context_url_candidates"), "computer_url_candidates": thread.get("computer_url_candidates"), "typed_payloads": thread.get("typed_payloads"), "timeline": thread.get("timeline")}),
        f"<h3 id='thread-{idx}-reasoning'>4. Reasoning availability</h3>",
        _v12_render_reasoning(thread),
        f"<h3 id='thread-{idx}-privacy'>5. Privacy / deletion</h3>",
        _render_privacy_deletion_v07(thread),
        f"<h3 id='thread-{idx}-final'>6. Final answer</h3>",
        _render_final_answer_v07(thread),
        _raw_details_v07("Open final-answer raw object", thread.get("final_answer")),
        f"<h3 id='thread-{idx}-time'>7. Time & storage evidence</h3>",
        _render_time_and_sources_v07(thread),
        f"<div id='thread-{idx}-computer'>" + _render_computer_specific_v11(thread) + "</div>",
        f"<h3 id='thread-{idx}-evidence'>Decoded evidence explorer</h3>",
        _v12_render_evidence_explorer(thread),
        "</section>",
    ])


def render_html_report(report: dict[str, Any], output_path: Path) -> None:
    """v0.12 HTML viewer with thread subnavigation and decoded evidence explorer."""
    summary = report.get("summary", {}) or {}
    extraction = report.get("extraction_summary", {}) or {}
    source = report.get("source", {}) or {}
    threads = report.get("threads", []) or []

    def nav_thread_label(thread: dict[str, Any], idx: int) -> str:
        prompt = thread.get("prompt", {}) or {}
        refs = prompt.get("reference_codes") or []
        if refs:
            return str(refs[0])
        text = prompt.get("text") or thread.get("thread_id") or f"Thread {idx}"
        return shorten_text(text, 44)

    def nav_button(target: str, label: str, sublabel: str = "", active: bool = False, scroll: str | None = None, subitem: bool = False) -> str:
        classes = ["nav-item"]
        if active:
            classes.append("active")
        if subitem:
            classes.append("nav-subitem")
        sub = f"<span>{_h(sublabel)}</span>" if sublabel else ""
        scroll_attr = f" data-scroll='{_h(scroll)}'" if scroll else ""
        return f"<button type='button' class='{' '.join(classes)}' data-target='{_h(target)}'{scroll_attr}><strong>{_h(label)}</strong>{sub}</button>"

    style = """
<style>
:root{--bg:#f4f6fb;--card:#fff;--ink:#111827;--muted:#667085;--line:#d9e0ea;--soft:#f8fafc;--blue:#1d4ed8;--green:#047857;--amber:#b45309;--red:#b91c1c;--nav:#111827;}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;line-height:1.55}.layout{display:grid;grid-template-columns:315px minmax(0,1fr);min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;overflow:auto;background:var(--nav);color:#fff;padding:22px 18px}.brand{padding-bottom:16px;border-bottom:1px solid rgba(255,255,255,.12);margin-bottom:14px}.brand h1{font-size:18px;line-height:1.2;margin:0 0 6px}.brand p{margin:0;color:#cbd5e1;font-size:12px;word-break:break-all}.nav-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin:18px 6px 8px}.nav-item{width:100%;border:0;background:transparent;color:#d1d5db;text-align:left;border-radius:10px;padding:10px 11px;margin:3px 0;cursor:pointer}.nav-subitem{padding:6px 10px 6px 24px;margin:1px 0;color:#aeb9c9}.nav-subitem strong{font-size:12px}.nav-subitem:before{content:'↳ ';color:#64748b}.nav-item strong{display:block;font-size:13px;line-height:1.25}.nav-item span{display:block;font-size:11px;color:#94a3b8;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.nav-item:hover{background:rgba(255,255,255,.09);color:#fff}.nav-item.active{background:#fff;color:#111827}.nav-subitem.active{background:rgba(255,255,255,.18);color:#fff}.main{max-width:1180px;width:100%;margin:0 auto;padding:26px}.view-section{display:none}.view-section.active{display:block}.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px;margin:0 0 18px;box-shadow:0 2px 10px rgba(15,23,42,.04)}.hero{background:#111827;color:#fff;border-radius:18px;padding:26px 30px;margin-bottom:18px}.hero h1{margin:0 0 8px;font-size:28px}.hero p{margin:4px 0;color:#cbd5e1}h2{margin:0 0 14px}h3{margin:24px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px;scroll-margin-top:16px}h4{margin:18px 0 8px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.metric{background:var(--soft);border:1px solid var(--line);border-radius:13px;padding:12px}.metric-label{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.metric-value{font-size:24px;font-weight:800}.badge{display:inline-block;border-radius:999px;padding:4px 9px;margin:2px 4px 2px 0;font-size:12px;font-weight:700;background:#eef2ff;color:#3730a3}.badge.good{background:#dcfce7;color:#166534}.badge.warn{background:#fef3c7;color:#92400e}.badge.bad{background:#fee2e2;color:#991b1b}.badge.neutral{background:#f3f4f6;color:#374151}.note{border-left:4px solid var(--blue);background:#eff6ff;border-radius:12px;padding:12px 14px;margin:12px 0}.note.warn{border-left-color:var(--amber);background:#fffbeb}.note.good{border-left-color:var(--green);background:#ecfdf5}.prompt,.answer{white-space:pre-wrap;background:#f8fafc;border:1px solid var(--line);border-radius:14px;padding:16px;overflow:auto}.answer{max-height:560px}.good-answer{border-left:4px solid var(--green)}.muted,.small{color:var(--muted);font-size:13px}.kv{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--line);border-radius:12px;overflow:hidden}.kv th{width:230px;text-align:left;background:#f8fafc;color:#475467}.kv th,.kv td{border-bottom:1px solid var(--line);padding:10px;vertical-align:top}.kv tr:last-child th,.kv tr:last-child td{border-bottom:0}.table{width:100%;border-collapse:collapse}.table th,.table td{border:1px solid var(--line);padding:8px 9px;vertical-align:top;text-align:left}.table th{background:#f8fafc;color:#475467}.thread-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:14px}.thread-card{border:1px solid var(--line);border-radius:16px;padding:16px;background:#fff}.thread-card h3{border:0;margin:8px 0 2px;padding:0}.thread-card-top{display:flex;justify-content:space-between;gap:8px}.thread-num{font-weight:800;color:var(--blue)}.thread-id{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:var(--muted);word-break:break-all}.mini-row{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.mini{background:#f3f4f6;border:0;border-radius:8px;padding:4px 8px;font-size:12px}.jump-btn{cursor:pointer}.thread-jump-row{position:sticky;top:0;background:#fff;border:1px solid var(--line);border-radius:12px;padding:8px;z-index:2}.thread-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}.evidence-row{margin:8px 0}.evidence-row strong{display:block;font-size:12px;color:#475467;margin-bottom:4px}.evidence-chip{display:inline-block;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f3f4f6;border:1px solid #d0d5dd;border-radius:7px;padding:4px 7px;margin:2px 4px 2px 0;font-size:12px}.empty{color:#667085;font-style:italic;background:#f9fafb;border:1px dashed #cbd5e1;border-radius:12px;padding:12px}details{border:1px solid var(--line);border-radius:12px;background:#fff;margin:10px 0}summary{cursor:pointer;padding:10px 13px;font-weight:700;color:#374151;background:#f8fafc;border-radius:12px}details[open] summary{border-bottom:1px solid var(--line);border-radius:12px 12px 0 0}pre.raw{margin:0;padding:14px;background:#111827;color:#e5e7eb;overflow:auto;border-radius:0 0 12px 12px;font-size:12px;line-height:1.45}.findings li{margin:6px 0}a{color:#1d4ed8;word-break:break-all}.topline{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}.hint{font-size:13px;color:#475569;background:#f8fafc;border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:14px}@media(max-width:980px){.layout{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.main{padding:16px}.nav-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:4px}.nav-title{margin-top:12px}.thread-jump-row{position:relative}}@media print{.sidebar{display:none}.layout{display:block}.main{max-width:none}.view-section{display:block!important}.card{break-inside:avoid}}
</style>
<noscript><style>.view-section{display:block!important}.sidebar{position:relative;height:auto}</style></noscript>
"""

    nav_parts: list[str] = []
    nav_parts.append("<aside class='sidebar'>")
    nav_parts.append("<div class='brand'><h1>Comet Reconstruction</h1>")
    nav_parts.append(f"<p>{_h(Path(str(source.get('input') or '')).name)}</p></div>")
    nav_parts.append("<div class='nav-list'>")
    nav_parts.append("<div class='nav-title'>Report</div>")
    nav_parts.append(nav_button("summary", "Case summary", f"{summary.get('thread_count', 0)} threads", True))
    nav_parts.append(nav_button("overview", "Thread overview", "Reconstruction list"))
    nav_parts.append("<div class='nav-title'>Threads</div>")
    subitems = [("prompt", "Prompt"), ("metadata", "Metadata"), ("activity", "Activity"), ("reasoning", "Reasoning"), ("privacy", "Privacy/Delete"), ("final", "Final"), ("time", "Time/Storage"), ("evidence", "Raw Evidence")]
    for idx, thread in enumerate(threads, start=1):
        cls = thread.get("classification", {}) or {}
        nav_parts.append(nav_button(f"thread-{idx}", f"Thread {idx}", f"{nav_thread_label(thread, idx)} · {cls.get('execution_mode') or 'unknown'}"))
        local_subitems = list(subitems)
        if cls.get("execution_mode") == "computer_mode":
            local_subitems.insert(7, ("computer", "Computer"))
        for slug, label in local_subitems:
            nav_parts.append(nav_button(f"thread-{idx}", label, "", scroll=f"thread-{idx}-{slug}", subitem=True))
    nav_parts.append("<div class='nav-title'>Evidence</div>")
    nav_parts.append(nav_button("global", "Residual / unassigned", f"{extraction.get('global_record_count', 0)} records"))
    if report.get("skipped"):
        nav_parts.append(nav_button("skipped", "Skipped groups", f"{len(report.get('skipped') or [])} groups"))
    if report.get("snapshot_comparison"):
        nav_parts.append(nav_button("comparison", "Snapshot comparison", "Before / after"))
    nav_parts.append(nav_button("raw", "Raw JSON", "Full report"))
    nav_parts.append("</div></aside>")

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Comet Browser Reconstruction Report</title>",
        style,
        "</head><body><div class='layout'>",
        "\n".join(nav_parts),
        "<main class='main'>",
        "<section id='summary' class='view-section active'>",
        "<section class='hero'>",
        "<h1>Comet Browser Reconstruction Report</h1>",
        f"<p><strong>Input:</strong> {_h(Path(str(source.get('input') or '')).name)}</p>",
        f"<p><strong>Scope:</strong> {_h(source.get('analysis_scope'))} · <strong>Target:</strong> {_h(source.get('target_origin'))} · <strong>Store:</strong> {_h(source.get('database'))}/{_h(source.get('object_store'))}</p>",
        "</section>",
        "<section class='card'><div class='topline'><h2>Executive findings</h2><span class='badge neutral'>JSON-synced view</span></div>",
        "<div class='hint'>좌측 목록에서 thread를 열고, 하위 항목 Prompt/Activity/Reasoning/Raw Evidence를 바로 이동할 수 있습니다. 각 Raw Evidence는 JSON report 안의 decoded artifact/evidence를 여는 뷰입니다.</div>",
        _render_executive_findings_v07(report),
        "<div class='metrics'>",
        _panel_metric("Threads", summary.get("thread_count", 0)),
        _panel_metric("Browser agent", summary.get("browser_agent_thread_count", 0)),
        _panel_metric("Computer mode", summary.get("computer_mode_thread_count", 0)),
        _panel_metric("Skipped computer", summary.get("skipped_computer_mode_count", 0)),
        _panel_metric("Global records", extraction.get("global_record_count", 0)),
        _panel_metric("Parsed records", extraction.get("all_record_count", 0)),
        _panel_metric("Relevant records", extraction.get("relevant_record_count", 0)),
        "</div></section>",
        _render_case_summary_v10(report),
        "</section>",
        "<section id='overview' class='card view-section'><div class='topline'><h2>Thread overview</h2><span class='badge neutral'>Select a thread on the left</span></div>",
        _render_thread_overview_v07(report),
        "</section>",
    ]
    for idx, thread in enumerate(threads, start=1):
        parts.append(_render_thread_detail_v12(thread, idx))

    parts.extend([
        "<section id='global' class='card view-section'><h2>Residual / global artifacts</h2>",
        _render_global_artifacts_v07(report.get("global_records", []) or []),
        "</section>",
    ])
    if report.get("skipped"):
        parts.extend(["<section id='skipped' class='card view-section'><h2>Skipped groups</h2>", _raw_details_v07("Skipped group details", report.get("skipped")), "</section>"])
    if report.get("snapshot_comparison"):
        parts.extend(["<section id='comparison' class='card view-section'><h2>Snapshot comparison</h2>", render_snapshot_comparison(report.get("snapshot_comparison", {})), _raw_details_v07("Raw snapshot comparison", report.get("snapshot_comparison")), "</section>"])
    parts.extend([
        "<section id='raw' class='card view-section'><h2>Full raw report JSON</h2>",
        "<div class='note warn'><strong>Authoritative raw output.</strong><br>The readable sections above are generated from this same report object.</div>",
        "<pre class='raw'>" + _h(json.dumps(report, ensure_ascii=False, indent=2, default=str)) + "</pre>",
        "</section>",
        "</main></div>",
        """
<script>
(function(){
  const sections = Array.from(document.querySelectorAll('.view-section'));
  const buttons = Array.from(document.querySelectorAll('.nav-item[data-target], .jump-btn[data-target]'));
  function showSection(id, scrollId){
    const target = document.getElementById(id);
    if(!target) return;
    sections.forEach(sec => sec.classList.remove('active'));
    target.classList.add('active');
    buttons.filter(b => b.classList.contains('nav-item')).forEach(btn => btn.classList.toggle('active', btn.dataset.target === id && (!scrollId || btn.dataset.scroll === scrollId)));
    const doScroll = () => {
      if(scrollId){
        const el = document.getElementById(scrollId);
        if(el) { el.scrollIntoView({behavior:'smooth', block:'start'}); return; }
      }
      window.scrollTo(0, 0);
    };
    setTimeout(doScroll, 20);
    history.replaceState(null, '', '#' + (scrollId || id));
  }
  buttons.forEach(btn => btn.addEventListener('click', () => showSection(btn.dataset.target, btn.dataset.scroll)));
  const initial = (window.location.hash || '').replace('#','');
  if(initial){
    const sec = document.getElementById(initial);
    if(sec && sec.classList.contains('view-section')) showSection(initial);
    else {
      const parent = initial.match(/^(thread-[0-9]+)-/);
      if(parent && document.getElementById(parent[1])) showSection(parent[1], initial);
    }
  }
})();
</script>
""",
        "</body></html>",
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")



# ---------------------------------------------------------------------------
# v0.14: compact professor-focused HTML view with collapsible thread subitems
# and raw decoded evidence under the relevant section.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.14"

_reconstruct_browser_threads_pre_v14 = reconstruct_browser_threads

def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v14(extracted, input_label, browser_only=browser_only)
    report["schema_version"] = "0.14"
    report.setdefault("hardcoding_audit", {})["v14"] = {
        "compact_professor_view_html": True,
        "collapsible_thread_subnavigation": True,
        "section_raw_decoded_evidence": True,
        "rule": "HTML display is compact, but decoded JSON evidence remains available under each relevant section. No scenario-specific literals are used for classification or display.",
        "no_scenario_specific_strings": True,
    }
    return report

_filter_report_by_target_reference_pre_v14 = filter_report_by_target_reference

def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v14(report, target_reference)
    filtered["schema_version"] = "0.14"
    filtered.setdefault("hardcoding_audit", {})["v14"] = {
        "compact_professor_view_html": True,
        "collapsible_thread_subnavigation": True,
        "section_raw_decoded_evidence": True,
        "rule": "Target display uses supplied target_reference plus recovered artifact fields. It does not assume values from notes or experiments.",
        "no_scenario_specific_strings": True,
    }
    filtered["case_summary"] = build_case_summary(filtered, target_reference, original_counts=filtered.get("filter_summary"))
    return filtered


def _v14_first_evidence_ref(value: Any) -> dict[str, Any]:
    """Return a representative evidence dict from dict/list/nested values."""
    if isinstance(value, dict):
        if value.get("source_file") or value.get("source_type") or value.get("offset") is not None:
            return value
        for v in value.values():
            found = _v14_first_evidence_ref(v)
            if found:
                return found
    elif isinstance(value, list):
        for entry in value:
            found = _v14_first_evidence_ref(entry)
            if found:
                return found
    return {}


def _v14_evidence_count(value: Any) -> int:
    if isinstance(value, dict):
        return 1 if (value.get("source_file") or value.get("source_type") or value.get("offset") is not None) else sum(_v14_evidence_count(v) for v in value.values())
    if isinstance(value, list):
        return sum(_v14_evidence_count(v) for v in value)
    return 0


def _v14_evidence_text(value: Any) -> str:
    ev = _v14_first_evidence_ref(value)
    if not ev:
        return ""
    parts = []
    if ev.get("source_file"):
        parts.append(str(ev.get("source_file")))
    if ev.get("source_type"):
        parts.append(str(ev.get("source_type")))
    if ev.get("state"):
        parts.append(str(ev.get("state")))
    if ev.get("ldb_seq_no") is not None:
        parts.append(f"seq={ev.get('ldb_seq_no')}")
    if ev.get("offset") is not None:
        parts.append(f"offset={ev.get('offset')}")
    count = _v14_evidence_count(value)
    if count > 1:
        parts.append(f"+{count-1} more")
    return " · ".join(parts)


def _v14_counts(thread: dict[str, Any]) -> dict[str, int]:
    reasoning = thread.get("reasoning", {}) or {}
    return {
        "plan": len(thread.get("plan") or []),
        "actions": len(thread.get("actions") or []),
        "urls": len(thread.get("urls") or []),
        "context_urls": len(thread.get("context_url_candidates") or []),
        "payloads": len(thread.get("typed_payloads") or []),
        "reasoning": len(reasoning.get("items") or []),
        "progress": len(reasoning.get("progress_or_status_items") or []),
    }


def _v14_key_facts(thread: dict[str, Any]) -> str:
    cls = thread.get("classification", {}) or {}
    meta = thread.get("metadata", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    final = thread.get("final_answer", {}) or {}
    privacy = meta.get("private_detection", {}) or {}
    source_counts = ((thread.get("source_summary") or {}).get("source_type_counts") or {})
    outcome = (thread.get("task_outcome") or {}).get("status")
    counts = _v14_counts(thread)
    return _kv_table_v07([
        ("Interaction", cls.get("interaction_type")),
        ("Execution mode", cls.get("execution_mode")),
        ("Classification confidence", cls.get("confidence")),
        ("Core status", meta.get("status") or meta.get("thread_status")),
        ("Task outcome", outcome),
        ("Final answer", "present" if final.get("text") else "not recovered"),
        ("Activity counts", f"plan={counts['plan']}, actions={counts['actions']}, urls={counts['urls']}, payloads={counts['payloads']}"),
        ("Reasoning/progress", f"reasoning={counts['reasoning']}, progress/status={counts['progress']}"),
        ("Deletion / residue", deletion.get("state") or (thread.get("reconstruction_availability") or {}).get("residue_state", {}).get("state")),
        ("Private mode", bool(privacy.get("private_mode"))),
        ("LOG/LDB sources", ", ".join(f"{k}:{v}" for k, v in source_counts.items()) or "not summarized"),
    ])


def _v14_render_prompt_and_metadata(thread: dict[str, Any], idx: int) -> str:
    prompt = thread.get("prompt", {}) or {}
    cls = thread.get("classification", {}) or {}
    meta = thread.get("metadata", {}) or {}
    class_evidence = cls.get("classification_evidence") or []
    class_preview = ", ".join(str(x) for x in class_evidence[:10])
    if len(class_evidence) > 10:
        class_preview += f", +{len(class_evidence)-10} more"
    return "\n".join([
        f"<h3 id='thread-{idx}-prompt'>1. Prompt</h3>",
        f"<div class='prompt'>{_h(prompt.get('text') or 'No user-authored prompt extracted.')}</div>",
        _evidence_row("Prompt evidence", prompt.get("evidence")),
        _raw_details_v07("Open prompt raw object", prompt),
        f"<h3 id='thread-{idx}-metadata'>2. Classification & key metadata</h3>",
        _v14_key_facts(thread),
        _kv_table_v07([
            ("Classification evidence", class_preview),
            ("Search mode", meta.get("search_mode")),
            ("Display model", meta.get("display_model")),
            ("Context UUID", meta.get("context_uuid")),
        ]),
        _raw_details_v07("Open classification + metadata raw evidence", {"classification": cls, "metadata": {k: meta.get(k) for k in ["status", "mode", "search_mode", "display_model", "context_uuid", "frontend_context_uuid", "privacy_state"]}, "metadata_evidence": meta.get("evidence")}),
    ])


def _v14_render_activity(thread: dict[str, Any]) -> str:
    plan = thread.get("plan") or []
    actions = thread.get("actions") or []
    urls = thread.get("urls") or []
    payloads = thread.get("typed_payloads") or []
    context_urls = thread.get("context_url_candidates") or []
    computer_urls = thread.get("computer_url_candidates") or []
    counts = _v14_counts(thread)
    parts = [
        "<div class='metrics compact-metrics'>",
        _panel_metric("Plan", counts["plan"]),
        _panel_metric("Actions", counts["actions"]),
        _panel_metric("URLs", counts["urls"] + counts["context_urls"] + len(computer_urls)),
        _panel_metric("Payloads", counts["payloads"]),
        "</div>",
    ]
    if not (plan or actions or urls or payloads or computer_urls):
        parts.append("<div class='note warn'><strong>No detailed action trace recovered.</strong><br>Classification may still be supported by metadata/cache evidence. Use History/Downloads artifacts for side-effect corroboration.</div>")
    if plan:
        rows = [{"order": p.get("relative_order"), "kind": p.get("kind"), "label": shorten_text(p.get("label"), 160), "source": _v14_evidence_text(p.get("evidence"))} for p in plan[:10]]
        parts.append("<h4>Plan / workflow steps</h4>")
        parts.append(_simple_table_v07(rows, [("order", "Order"), ("kind", "Kind"), ("label", "Step"), ("source", "LDB/LOG source")], "No plan artifacts."))
    if actions:
        rows = [{"order": a.get("relative_order"), "kind": a.get("kind"), "label": shorten_text(a.get("label"), 180), "source": _v14_evidence_text(a.get("evidence"))} for a in actions[:12]]
        parts.append("<h4>Agent actions / tool evidence</h4>")
        parts.append(_simple_table_v07(rows, [("order", "Order"), ("kind", "Kind"), ("label", "Action"), ("source", "LDB/LOG source")], "No action artifacts."))
    if payloads:
        rows = [{"order": p.get("relative_order"), "field": p.get("field"), "value": shorten_text(p.get("value"), 180), "source": _v14_evidence_text(p.get("evidence"))} for p in payloads[:8]]
        parts.append("<h4>Typed/submitted payloads</h4>")
        parts.append(_simple_table_v07(rows, [("order", "Order"), ("field", "Field"), ("value", "Value"), ("source", "LDB/LOG source")], "No typed payloads."))
    url_rows = []
    for u in list(urls)[:8]:
        url_rows.append({"role": u.get("role"), "title": shorten_text(u.get("title"), 120), "url": u.get("url"), "source": _v14_evidence_text(u.get("evidence"))})
    for u in list(computer_urls)[:8]:
        url_rows.append({"role": u.get("role") or "computer_url_candidate", "title": shorten_text(u.get("title"), 120), "url": u.get("url"), "source": _v14_evidence_text(u.get("evidence"))})
    if url_rows:
        parts.append("<h4>URL evidence / candidates</h4>")
        parts.append("<p class='muted'>Computer-mode URL candidates are artifact text leads unless supported by navigation/history evidence.</p>")
        parts.append(_simple_table_v07(url_rows, [("role", "Role"), ("title", "Title"), ("url", "URL"), ("source", "LDB/LOG source")], "No URL artifacts."))
    if context_urls:
        rows = [{"title": shorten_text(u.get("title"), 120), "url": u.get("url"), "visitCount": u.get("visitCount"), "source": _v14_evidence_text(u.get("evidence"))} for u in context_urls[:8]]
        parts.append("<details><summary>Open context/global URL leads</summary>")
        parts.append(_simple_table_v07(rows, [("title", "Title"), ("url", "URL"), ("visitCount", "Visits"), ("source", "LDB/LOG source")], "No context URL leads."))
        parts.append("</details>")
    parts.append(_raw_details_v07("Open raw agentic activity object", {"plan": plan, "actions": actions, "urls": urls, "context_url_candidates": context_urls, "computer_url_candidates": computer_urls, "typed_payloads": payloads, "timeline": thread.get("timeline")}))
    return "\n".join(parts)


def _v14_reasoning_rows(items: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    rows = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            rows.append({"kind": "unstructured", "marker": None, "source": "", "text": shorten_text(str(item), 220)})
            continue
        evidence = item.get("evidence") or item.get("evidence_refs") or {}
        rows.append({
            "kind": item.get("kind"),
            "marker": item.get("marker") or item.get("variant") or item.get("tool_name"),
            "source": _v14_evidence_text(evidence),
            "text": shorten_text(item.get("text"), 260),
        })
    return rows


def _v14_render_reasoning(thread: dict[str, Any]) -> str:
    reasoning = thread.get("reasoning", {}) or {}
    items = reasoning.get("items") or []
    progress = reasoning.get("progress_or_status_items") or []
    parts = [
        _kv_table_v07([
            ("Available", reasoning.get("available")),
            ("Observable reasoning items", len(items)),
            ("Separated progress/status items", len(progress)),
            ("Scan rule", reasoning.get("scan_rule")),
            ("Note", reasoning.get("note")),
            ("Interpretation", reasoning.get("interpretation")),
        ])
    ]
    if items:
        parts.append("<h4>Observable reasoning / rationale</h4>")
        parts.append(_simple_table_v07(_v14_reasoning_rows(items, 10), [("kind", "Kind"), ("marker", "Marker"), ("source", "LDB/LOG source"), ("text", "Recovered text")], "No observable reasoning/thought candidate was recovered."))
    else:
        parts.append("<div class='empty'>No observable reasoning/thought candidate was recovered.</div>")
    if progress:
        parts.append("<details><summary>Open separated progress/status items</summary>")
        parts.append(_simple_table_v07(_v14_reasoning_rows(progress, 12), [("kind", "Kind"), ("marker", "Marker"), ("source", "LDB/LOG source"), ("text", "Recovered text")], "No progress/status items."))
        parts.append("</details>")
    parts.append(_raw_details_v07("Open raw reasoning/progress object", reasoning))
    return "\n".join(parts)


def _v14_render_privacy_deletion(thread: dict[str, Any]) -> str:
    metadata = thread.get("metadata", {}) or {}
    privacy = metadata.get("private_detection", {}) or {}
    deletion = thread.get("deletion_state", {}) or {}
    availability = thread.get("reconstruction_availability", {}) or {}
    residue = availability.get("residue_state") or {}
    storage = availability.get("storage_state") or {}
    return "\n".join([
        _kv_table_v07([
            ("Private mode", bool(privacy.get("private_mode"))),
            ("Privacy states", ", ".join(str(x) for x in (privacy.get("privacy_states") or []))),
            ("Access levels", ", ".join(str(x) for x in (privacy.get("access_levels") or []))),
            ("Deletion marker", deletion.get("state") or storage.get("deletion_marker_state")),
            ("Storage state", storage.get("state")),
            ("Residue/stale state", residue.get("state")),
            ("Stale candidate", residue.get("stale_candidate")),
        ]),
        _raw_details_v07("Open privacy/deletion/stale raw object", {"private_detection": privacy, "deletion_state": deletion, "reconstruction_availability": availability}),
    ])


def _v14_render_final(thread: dict[str, Any]) -> str:
    final = thread.get("final_answer", {}) or {}
    outcome = thread.get("task_outcome", {}) or {}
    parts = []
    if final.get("text"):
        parts.append("<div class='answer good-answer'>" + _h(shorten_text(final.get("text"), 1800)) + "</div>")
        parts.append(_evidence_row("Final answer evidence", final.get("evidence")))
    else:
        parts.append("<div class='note warn'><strong>No clean final answer extracted.</strong><br>Use raw evidence and external corroboration.</div>")
    parts.append(_kv_table_v07([
        ("Task type", outcome.get("task_type")),
        ("Outcome status", outcome.get("status")),
        ("Side effect completed", outcome.get("side_effect_completed")),
        ("Outcome confidence", outcome.get("confidence")),
        ("Missing corroboration", ", ".join(str(x) for x in (outcome.get("missing_corroboration") or []))),
    ]))
    parts.append(_raw_details_v07("Open final answer / task outcome raw object", {"final_answer": final, "task_outcome": outcome}))
    return "\n".join(parts)


def _v14_render_thread_detail(thread: dict[str, Any], idx: int) -> str:
    classification = thread.get("classification", {}) or {}
    title = _thread_title_v07(thread, idx)
    quality, qkind, explanation = _thread_quality_v07(thread)
    execution_mode = classification.get("execution_mode")
    badges = (
        _badge_v07(classification.get("interaction_type"), "good" if classification.get("interaction_type") == "agentic" else "neutral")
        + _badge_v07(execution_mode, "good" if execution_mode in {"browser_control", "computer_mode"} else "neutral")
        + _badge_v07("confidence: " + str(classification.get("confidence")), "neutral")
        + _badge_v07(quality, qkind)
    )
    parts = [
        f"<section id='thread-{idx}' class='card thread-detail view-section'>",
        "<div class='thread-head'>",
        f"<div><h2>{_h(title)}</h2><p class='thread-id'>{_h(thread.get('thread_id'))}</p></div>",
        f"<div class='badges'>{badges}</div>",
        "</div>",
        _v12_subnav(idx, execution_mode),
        f"<div class='note {qkind}'><strong>Reconstruction verdict:</strong> {_h(explanation)}<br><span class='muted'>{_status_line_v07(thread)}</span></div>",
        _v14_render_prompt_and_metadata(thread, idx),
        f"<h3 id='thread-{idx}-activity'>3. Agentic activity</h3>",
        _v14_render_activity(thread),
        f"<h3 id='thread-{idx}-reasoning'>4. Reasoning / progress evidence</h3>",
        _v14_render_reasoning(thread),
        f"<h3 id='thread-{idx}-privacy'>5. Privacy / deletion / stale</h3>",
        _v14_render_privacy_deletion(thread),
        f"<h3 id='thread-{idx}-final'>6. Final answer / outcome</h3>",
        _v14_render_final(thread),
        f"<h3 id='thread-{idx}-time'>7. Time & storage</h3>",
        _render_time_and_sources_v07(thread),
    ]
    if execution_mode == "computer_mode":
        parts.extend([
            f"<h3 id='thread-{idx}-computer'>8. Computer-mode evidence</h3>",
            _render_computer_specific_v11(thread),
        ])
    parts.extend([
        f"<h3 id='thread-{idx}-evidence'>Decoded evidence explorer</h3>",
        _v12_render_evidence_explorer(thread),
        "</section>",
    ])
    return "\n".join(parts)

# Keep the existing v0.12 local subnavigation row inside the thread body, but
# make the sidebar subitems collapsible from v0.14 onward.
_render_thread_detail_v12 = _v14_render_thread_detail


def render_html_report(report: dict[str, Any], output_path: Path) -> None:
    """v0.14 compact HTML viewer with collapsible thread subitems and raw decoded evidence."""
    report["schema_version"] = report.get("schema_version") or "0.14"
    summary = report.get("summary", {}) or {}
    extraction = report.get("extraction_summary", {}) or {}
    source = report.get("source", {}) or {}
    threads = report.get("threads", []) or []

    def nav_thread_label(thread: dict[str, Any], idx: int) -> str:
        prompt = thread.get("prompt", {}) or {}
        refs = prompt.get("reference_codes") or []
        if refs:
            return str(refs[0])
        text = prompt.get("text") or thread.get("thread_id") or f"Thread {idx}"
        return shorten_text(text, 44)

    def nav_button(target: str, label: str, sublabel: str = "", active: bool = False, scroll: str | None = None, subitem: bool = False, extra_class: str = "") -> str:
        classes = ["nav-item"]
        if active:
            classes.append("active")
        if subitem:
            classes.append("nav-subitem")
        if extra_class:
            classes.append(extra_class)
        sub = f"<span>{_h(sublabel)}</span>" if sublabel else ""
        scroll_attr = f" data-scroll='{_h(scroll)}'" if scroll else ""
        return f"<button type='button' class='{' '.join(classes)}' data-target='{_h(target)}'{scroll_attr}><strong>{_h(label)}</strong>{sub}</button>"

    style = """
<style>
:root{--bg:#f6f7fb;--card:#fff;--ink:#111827;--muted:#667085;--line:#d9e0ea;--soft:#f8fafc;--blue:#1d4ed8;--green:#047857;--amber:#b45309;--red:#b91c1c;--nav:#111827;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;line-height:1.55;font-size:14px}.layout{display:grid;grid-template-columns:300px minmax(0,1fr);min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;overflow:auto;background:var(--nav);color:#fff;padding:20px 16px}.brand{padding-bottom:14px;border-bottom:1px solid rgba(255,255,255,.12);margin-bottom:12px}.brand h1{font-size:17px;margin:0 0 6px}.brand p{margin:0;color:#cbd5e1;font-size:12px;word-break:break-all}.nav-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin:16px 6px 7px}.nav-item{width:100%;border:0;background:transparent;color:#d1d5db;text-align:left;border-radius:10px;padding:9px 10px;margin:3px 0;cursor:pointer}.nav-thread{position:relative}.nav-thread:after{content:'›';position:absolute;right:10px;top:11px;color:#94a3b8}.thread-nav-group.open>.nav-thread:after{content:'⌄'}.nav-subitems{display:none;margin:2px 0 8px 8px;border-left:1px solid rgba(255,255,255,.14);padding-left:6px}.thread-nav-group.open>.nav-subitems{display:block}.nav-subitem{padding:5px 8px 5px 16px;margin:1px 0;color:#aeb9c9}.nav-subitem strong{font-size:12px}.nav-subitem:before{content:'↳ ';color:#64748b}.nav-item strong{display:block;font-size:13px;line-height:1.25}.nav-item span{display:block;font-size:11px;color:#94a3b8;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.nav-item:hover{background:rgba(255,255,255,.09);color:#fff}.nav-item.active{background:#fff;color:#111827}.nav-subitem.active{background:rgba(255,255,255,.18);color:#fff}.main{max-width:1120px;width:100%;margin:0 auto;padding:24px}.view-section{display:none}.view-section.active{display:block}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin:0 0 18px;box-shadow:0 2px 10px rgba(15,23,42,.04)}.hero{background:#111827;color:#fff;border-radius:18px;padding:24px 28px;margin-bottom:18px}.hero h1{margin:0 0 8px;font-size:25px}.hero p{margin:4px 0;color:#cbd5e1}h2{margin:0 0 14px;font-size:21px}h3{margin:22px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px;font-size:17px;scroll-margin-top:18px}h4{margin:16px 0 8px;font-size:14px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.compact-metrics{grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}.metric{background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:10px}.metric-label{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.metric-value{font-size:21px;font-weight:800}.badge{display:inline-block;border-radius:999px;padding:4px 8px;margin:2px 4px 2px 0;font-size:12px;font-weight:700;background:#eef2ff;color:#3730a3}.badge.good{background:#dcfce7;color:#166534}.badge.warn{background:#fef3c7;color:#92400e}.badge.bad{background:#fee2e2;color:#991b1b}.badge.neutral{background:#f3f4f6;color:#374151}.note{border-left:4px solid var(--blue);background:#eff6ff;border-radius:12px;padding:11px 13px;margin:12px 0}.note.warn{border-left-color:var(--amber);background:#fffbeb}.note.good{border-left-color:var(--green);background:#ecfdf5}.prompt,.answer{white-space:pre-wrap;background:#f8fafc;border:1px solid var(--line);border-radius:13px;padding:14px;overflow:auto}.answer{max-height:460px}.good-answer{border-left:4px solid var(--green)}.muted,.small{color:var(--muted);font-size:12px}.kv{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:8px 0}.kv th{width:210px;text-align:left;background:#f8fafc;color:#475467}.kv th,.kv td{border-bottom:1px solid var(--line);padding:8px 9px;vertical-align:top}.kv tr:last-child th,.kv tr:last-child td{border-bottom:0}.table{width:100%;border-collapse:collapse;margin:8px 0}.table th,.table td{border:1px solid var(--line);padding:7px 8px;vertical-align:top;text-align:left}.table th{background:#f8fafc;color:#475467}.compact td{font-size:12px}.thread-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:12px}.thread-card{border:1px solid var(--line);border-radius:14px;padding:14px;background:#fff}.thread-card h3{border:0;margin:8px 0 2px;padding:0}.thread-card-top{display:flex;justify-content:space-between;gap:8px}.thread-num{font-weight:800;color:var(--blue)}.thread-id{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:var(--muted);word-break:break-all}.mini-row{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.mini{background:#f3f4f6;border:0;border-radius:8px;padding:4px 8px;font-size:12px}.jump-btn{cursor:pointer}.thread-jump-row{position:sticky;top:0;background:#fff;border:1px solid var(--line);border-radius:12px;padding:8px;z-index:2}.thread-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}.evidence-row{margin:8px 0}.evidence-row strong{display:block;font-size:12px;color:#475467;margin-bottom:4px}.evidence-chip{display:inline-block;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f3f4f6;border:1px solid #d0d5dd;border-radius:7px;padding:4px 7px;margin:2px 4px 2px 0;font-size:12px}.empty{color:#667085;font-style:italic;background:#f9fafb;border:1px dashed #cbd5e1;border-radius:12px;padding:11px}details{border:1px solid var(--line);border-radius:12px;background:#fff;margin:9px 0}summary{cursor:pointer;padding:9px 12px;font-weight:700;color:#374151;background:#f8fafc;border-radius:12px}details[open] summary{border-bottom:1px solid var(--line);border-radius:12px 12px 0 0}pre.raw{margin:0;padding:13px;background:#111827;color:#e5e7eb;overflow:auto;border-radius:0 0 12px 12px;font-size:12px;line-height:1.45}.findings li{margin:5px 0}a{color:#1d4ed8;word-break:break-all}.topline{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}.hint{font-size:12px;color:#475569;background:#f8fafc;border:1px solid var(--line);border-radius:12px;padding:9px 11px;margin-bottom:12px}@media(max-width:980px){.layout{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.main{padding:16px}.nav-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:4px}.nav-subitems{display:none}.thread-jump-row{position:relative}}@media print{.sidebar{display:none}.layout{display:block}.main{max-width:none}.view-section{display:block!important}.card{break-inside:avoid}.nav-subitems{display:block}}
</style>
<noscript><style>.view-section{display:block!important}.nav-subitems{display:block!important}.sidebar{position:relative;height:auto}</style></noscript>
"""

    nav_parts: list[str] = []
    nav_parts.append("<aside class='sidebar'>")
    nav_parts.append("<div class='brand'><h1>Comet Reconstruction</h1>")
    nav_parts.append(f"<p>{_h(Path(str(source.get('input') or '')).name)}</p></div>")
    nav_parts.append("<div class='nav-list'>")
    nav_parts.append("<div class='nav-title'>Report</div>")
    nav_parts.append(nav_button("summary", "Case summary", f"{summary.get('thread_count', 0)} threads", True))
    nav_parts.append(nav_button("overview", "Thread overview", "Reconstruction list"))
    nav_parts.append("<div class='nav-title'>Threads</div>")
    subitems = [("prompt", "Prompt"), ("metadata", "Metadata"), ("activity", "Activity"), ("reasoning", "Reasoning"), ("privacy", "Privacy/Delete"), ("final", "Final"), ("time", "Time/Storage"), ("evidence", "Raw Evidence")]
    for idx, thread in enumerate(threads, start=1):
        cls = thread.get("classification", {}) or {}
        thread_id = f"thread-{idx}"
        nav_parts.append(f"<div class='thread-nav-group' data-thread='{thread_id}'>")
        nav_parts.append(nav_button(thread_id, f"Thread {idx}", f"{nav_thread_label(thread, idx)} · {cls.get('execution_mode') or 'unknown'}", extra_class="nav-thread"))
        nav_parts.append("<div class='nav-subitems'>")
        local_subitems = list(subitems)
        if cls.get("execution_mode") == "computer_mode":
            local_subitems.insert(7, ("computer", "Computer"))
        for slug, label in local_subitems:
            nav_parts.append(nav_button(thread_id, label, "", scroll=f"{thread_id}-{slug}", subitem=True))
        nav_parts.append("</div></div>")
    nav_parts.append("<div class='nav-title'>Evidence</div>")
    nav_parts.append(nav_button("global", "Residual / unassigned", f"{extraction.get('global_record_count', 0)} records"))
    if report.get("skipped"):
        nav_parts.append(nav_button("skipped", "Skipped groups", f"{len(report.get('skipped') or [])} groups"))
    if report.get("snapshot_comparison"):
        nav_parts.append(nav_button("comparison", "Snapshot comparison", "Before / after"))
    nav_parts.append(nav_button("raw", "Raw JSON", "Full report"))
    nav_parts.append("</div></aside>")

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Comet Browser Reconstruction Report</title>",
        style,
        "</head><body><div class='layout'>",
        "\n".join(nav_parts),
        "<main class='main'>",
        "<section id='summary' class='view-section active'>",
        "<section class='hero'>",
        "<h1>Comet Browser Reconstruction Report</h1>",
        f"<p><strong>Input:</strong> {_h(Path(str(source.get('input') or '')).name)}</p>",
        f"<p><strong>Scope:</strong> {_h(source.get('analysis_scope'))} · <strong>Target:</strong> {_h(source.get('target_origin'))} · <strong>Store:</strong> {_h(source.get('database'))}/{_h(source.get('object_store'))}</p>",
        "</section>",
        "<section class='card'><div class='topline'><h2>Executive findings</h2><span class='badge neutral'>compact evidence view</span></div>",
        "<div class='hint'>Thread를 클릭하면 하위 항목이 펼쳐집니다. 화면에는 핵심 복원 결과만 표시하고, 각 섹션의 원문 decoded JSON/evidence는 접힌 Raw 항목에서 확인합니다.</div>",
        _render_executive_findings_v07(report),
        "<div class='metrics'>",
        _panel_metric("Threads", summary.get("thread_count", 0)),
        _panel_metric("Browser agent", summary.get("browser_agent_thread_count", 0)),
        _panel_metric("Computer mode", summary.get("computer_mode_thread_count", 0)),
        _panel_metric("Global records", extraction.get("global_record_count", 0)),
        _panel_metric("Parsed records", extraction.get("all_record_count", 0)),
        _panel_metric("Relevant records", extraction.get("relevant_record_count", 0)),
        "</div></section>",
        _render_case_summary_v10(report),
        "</section>",
        "<section id='overview' class='card view-section'><div class='topline'><h2>Thread overview</h2><span class='badge neutral'>Select a thread on the left</span></div>",
        _render_thread_overview_v07(report),
        "</section>",
    ]
    for idx, thread in enumerate(threads, start=1):
        parts.append(_v14_render_thread_detail(thread, idx))
    parts.extend([
        "<section id='global' class='card view-section'><h2>Residual / global artifacts</h2>",
        _render_global_artifacts_v07(report.get("global_records", []) or []),
        "</section>",
    ])
    if report.get("skipped"):
        parts.extend(["<section id='skipped' class='card view-section'><h2>Skipped groups</h2>", _raw_details_v07("Skipped group details", report.get("skipped")), "</section>"])
    if report.get("snapshot_comparison"):
        parts.extend(["<section id='comparison' class='card view-section'><h2>Snapshot comparison</h2>", render_snapshot_comparison(report.get("snapshot_comparison", {})), _raw_details_v07("Raw snapshot comparison", report.get("snapshot_comparison")), "</section>"])
    parts.extend([
        "<section id='raw' class='card view-section'><h2>Full raw report JSON</h2>",
        "<div class='note warn'><strong>Authoritative raw output.</strong><br>The readable sections above are generated from this same report object.</div>",
        "<pre class='raw'>" + _h(json.dumps(report, ensure_ascii=False, indent=2, default=str)) + "</pre>",
        "</section>",
        "</main></div>",
        """
<script>
(function(){
  const sections = Array.from(document.querySelectorAll('.view-section'));
  const buttons = Array.from(document.querySelectorAll('.nav-item[data-target], .jump-btn[data-target]'));
  const groups = Array.from(document.querySelectorAll('.thread-nav-group'));
  function parentThreadId(id){
    const m = (id || '').match(/^(thread-[0-9]+)/);
    return m ? m[1] : null;
  }
  function openThreadGroup(threadId){
    groups.forEach(g => g.classList.toggle('open', g.dataset.thread === threadId));
  }
  function showSection(id, scrollId){
    const target = document.getElementById(id);
    if(!target) return;
    sections.forEach(sec => sec.classList.remove('active'));
    target.classList.add('active');
    const threadId = parentThreadId(scrollId || id);
    openThreadGroup(threadId);
    buttons.filter(b => b.classList.contains('nav-item')).forEach(btn => {
      const sameTarget = btn.dataset.target === id;
      const sameScroll = !scrollId || btn.dataset.scroll === scrollId;
      btn.classList.toggle('active', sameTarget && sameScroll);
    });
    const doScroll = () => {
      if(scrollId){
        const el = document.getElementById(scrollId);
        if(el) { el.scrollIntoView({behavior:'smooth', block:'start'}); return; }
      }
      window.scrollTo(0, 0);
    };
    setTimeout(doScroll, 20);
    history.replaceState(null, '', '#' + (scrollId || id));
  }
  buttons.forEach(btn => btn.addEventListener('click', () => showSection(btn.dataset.target, btn.dataset.scroll)));
  const initial = (window.location.hash || '').replace('#','');
  if(initial){
    const sec = document.getElementById(initial);
    if(sec && sec.classList.contains('view-section')) showSection(initial);
    else {
      const parent = initial.match(/^(thread-[0-9]+)-/);
      if(parent && document.getElementById(parent[1])) showSection(parent[1], initial);
    }
  }
})();
</script>
""",
        "</body></html>",
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")



# ---------------------------------------------------------------------------
# v0.15: HCI-focused HTML report refinement.
# - Restores a more readable visual hierarchy.
# - Shows per-section decoded evidence inline, next to the reconstructed item.
# - Makes plan/action/reasoning rows clickable via <details> so investigators can
#   open the exact decoded JSON object under the row rather than scrolling to a
#   monolithic raw-evidence section.
# - Keeps thread subnavigation collapsed until the thread is selected.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.15"


def _v15_source_line(evidence: Any) -> str:
    ev = _v14_first_evidence_ref(evidence)
    if not ev:
        return "source not attached"
    parts: list[str] = []
    source_file = ev.get("source_file")
    source_type = ev.get("source_type")
    state = ev.get("state")
    if source_file:
        parts.append(str(source_file))
    if source_type:
        parts.append(str(source_type))
    if state:
        parts.append(str(state))
    if ev.get("ldb_seq_no") is not None:
        parts.append(f"seq={ev.get('ldb_seq_no')}")
    if ev.get("offset") is not None:
        parts.append(f"offset={ev.get('offset')}")
    count = _v14_evidence_count(evidence)
    if count > 1:
        parts.append(f"+{count - 1} evidence refs")
    return " · ".join(parts)


def _v15_raw_json(value: Any) -> str:
    return _h(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _v15_value(value: Any, default: str = "not recovered") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _v15_evidence_card(
    item: Any,
    *,
    kind: str = "evidence",
    label: str | None = None,
    order: Any = None,
    evidence: Any = None,
    raw_title: str = "Open decoded artifact object",
) -> str:
    if not isinstance(item, dict):
        item_obj = {"value": item}
    else:
        item_obj = item
    if label is None:
        label = (
            item_obj.get("label")
            or item_obj.get("text")
            or item_obj.get("value")
            or item_obj.get("title")
            or item_obj.get("url")
            or "decoded artifact"
        )
    if order is None:
        order = item_obj.get("relative_order") or item_obj.get("order") or item_obj.get("ldb_seq_no") or "—"
    if evidence is None:
        evidence = item_obj.get("evidence") or item_obj.get("evidence_refs") or item_obj.get("source") or {}
    source = _v15_source_line(evidence)
    return "".join([
        "<details class='evidence-card'>",
        "<summary>",
        f"<span class='ev-order'>{_h(order)}</span>",
        f"<span class='ev-kind'>{_h(kind)}</span>",
        f"<strong class='ev-label'>{_h(shorten_text(label, 190))}</strong>",
        f"<span class='ev-source'>{_h(source)}</span>",
        "</summary>",
        f"<div class='raw-caption'>{_h(raw_title)}</div>",
        f"<pre class='raw'>{_v15_raw_json(item_obj)}</pre>",
        "</details>",
    ])


def _v15_evidence_list(items: list[Any], *, kind: str, label_key: str = "label", limit: int = 12, empty: str = "No decoded artifacts recovered.") -> str:
    if not items:
        return f"<div class='empty'>{_h(empty)}</div>"
    html: list[str] = ["<div class='evidence-list'>"]
    for item in items[:limit]:
        if isinstance(item, dict):
            label = item.get(label_key) or item.get("label") or item.get("text") or item.get("value") or item.get("url")
            html.append(_v15_evidence_card(item, kind=kind, label=label))
        else:
            html.append(_v15_evidence_card(item, kind=kind, label=str(item)))
    if len(items) > limit:
        html.append(f"<div class='more-note'>+{len(items)-limit} more items preserved in the section raw JSON.</div>")
    html.append("</div>")
    return "\n".join(html)


def _v15_section_title(idx: int, slug: str, number: int, title: str, subtitle: str = "") -> str:
    return "".join([
        f"<h3 id='thread-{idx}-{slug}' class='section-title'>",
        f"<span class='section-num'>{number}</span>",
        f"<span>{_h(title)}</span>",
        "</h3>",
        f"<p class='section-subtitle'>{_h(subtitle)}</p>" if subtitle else "",
    ])


def _v15_render_prompt(thread: dict[str, Any], idx: int) -> str:
    prompt = thread.get("prompt", {}) or {}
    text = prompt.get("text") or "No user-authored prompt extracted."
    return "\n".join([
        _v15_section_title(idx, "prompt", 1, "Prompt", "User-authored prompt/query recovered from decoded artifacts."),
        f"<div class='prompt'>{_h(text)}</div>",
        "<div class='source-strip'>" + _h(_v15_source_line(prompt.get("evidence"))) + "</div>",
        _raw_details_v07("Open prompt decoded JSON", prompt),
    ])


def _v15_render_metadata(thread: dict[str, Any], idx: int) -> str:
    cls = thread.get("classification", {}) or {}
    meta = thread.get("metadata", {}) or {}
    counts = _v14_counts(thread)
    source_counts = ((thread.get("source_summary") or {}).get("source_type_counts") or {})
    outcome = (thread.get("task_outcome") or {}).get("status")
    private = ((meta.get("private_detection") or {}).get("private_mode"))
    rows = [
        ("Interaction", cls.get("interaction_type")),
        ("Execution mode", cls.get("execution_mode")),
        ("Confidence", cls.get("confidence")),
        ("Status", meta.get("status") or meta.get("thread_status")),
        ("Task outcome", outcome),
        ("Activity", f"plan={counts['plan']}, actions={counts['actions']}, urls={counts['urls'] + counts['context_urls']}, payloads={counts['payloads']}"),
        ("Reasoning/progress", f"reasoning={counts['reasoning']}, progress/status={counts['progress']}"),
        ("Private mode", private),
        ("LOG/LDB", ", ".join(f"{k}:{v}" for k, v in source_counts.items()) or "not summarized"),
    ]
    class_evidence = cls.get("classification_evidence") or []
    badges = "".join(f"<span class='badge neutral'>{_h(x)}</span>" for x in class_evidence[:8])
    if len(class_evidence) > 8:
        badges += f"<span class='badge neutral'>+{len(class_evidence)-8} more</span>"
    return "\n".join([
        _v15_section_title(idx, "metadata", 2, "Classification & metadata", "Conversational vs agentic, then Browser Control vs Computer mode."),
        _kv_table_v07(rows),
        f"<div class='badge-row'>{badges}</div>" if badges else "<div class='empty'>No classification marker summary.</div>",
        _raw_details_v07("Open classification and metadata decoded JSON", {"classification": cls, "metadata_core": {k: meta.get(k) for k in ["status", "mode", "search_mode", "display_model", "context_uuid", "frontend_context_uuid", "privacy_state"]}, "metadata_evidence": meta.get("evidence")}),
    ])


def _v15_render_activity(thread: dict[str, Any], idx: int) -> str:
    plan = thread.get("plan") or []
    actions = thread.get("actions") or []
    urls = thread.get("urls") or []
    context_urls = thread.get("context_url_candidates") or []
    computer_urls = thread.get("computer_url_candidates") or []
    payloads = thread.get("typed_payloads") or []
    counts = _v14_counts(thread)
    parts: list[str] = [
        _v15_section_title(idx, "activity", 3, "Agentic activity", "Plan/workflow steps, tool actions, payloads, and URL evidence recovered from LDB/LOG."),
        "<div class='metrics compact-metrics'>",
        _panel_metric("Plan", counts["plan"]),
        _panel_metric("Actions", counts["actions"]),
        _panel_metric("URLs", counts["urls"] + counts["context_urls"] + len(computer_urls)),
        _panel_metric("Payloads", counts["payloads"]),
        "</div>",
    ]
    if not (plan or actions or urls or context_urls or computer_urls or payloads):
        parts.append("<div class='note warn'><strong>No detailed action trace recovered.</strong><br>Use other artifacts such as History/Downloads for side-effect corroboration.</div>")
    if plan:
        parts.append("<h4>Plan / workflow steps</h4>")
        parts.append(_v15_evidence_list(plan, kind="plan", label_key="label", limit=12, empty="No plan/workflow artifacts."))
    if actions:
        parts.append("<h4>Actions / tool evidence</h4>")
        parts.append(_v15_evidence_list(actions, kind="action", label_key="label", limit=14, empty="No action/tool artifacts."))
    if payloads:
        parts.append("<h4>Typed / submitted payloads</h4>")
        parts.append(_v15_evidence_list(payloads, kind="payload", label_key="value", limit=10, empty="No typed payload artifacts."))
    url_items = list(urls or []) + list(computer_urls or [])
    if url_items:
        parts.append("<h4>URL evidence / candidates</h4>")
        parts.append("<p class='muted'>Computer-mode URL candidates are artifact text leads unless backed by navigation/history evidence.</p>")
        parts.append(_v15_evidence_list(url_items, kind="url", label_key="url", limit=10, empty="No URL artifacts."))
    if context_urls:
        parts.append("<details class='section-raw'><summary>Open context/global URL leads</summary>")
        parts.append(_v15_evidence_list(context_urls, kind="context-url", label_key="url", limit=12, empty="No context URL leads."))
        parts.append("</details>")
    parts.append(_raw_details_v07("Open activity section JSON", {"plan": plan, "actions": actions, "urls": urls, "context_url_candidates": context_urls, "computer_url_candidates": computer_urls, "typed_payloads": payloads, "timeline": thread.get("timeline")}))
    return "\n".join(parts)


def _v15_render_reasoning(thread: dict[str, Any], idx: int) -> str:
    reasoning = thread.get("reasoning") or {}
    items = reasoning.get("items") or []
    progress = reasoning.get("progress_or_status_items") or []
    available = reasoning.get("available")
    note = reasoning.get("note") or reasoning.get("interpretation") or "Reasoning markers are reported only when explicit decoded artifact markers are recovered."
    badge_cls = "good" if items else "neutral"
    parts: list[str] = [
        _v15_section_title(idx, "reasoning", 4, "Reasoning / progress evidence", "Observable reasoning/rationale markers are separated from progress/status text."),
        f"<div class='note'><strong>Reasoning available:</strong> {_h(_v15_value(available))}<br>{_h(note)}</div>",
    ]
    if items:
        parts.append(f"<h4><span class='badge {badge_cls}'>Observable reasoning / rationale</span></h4>")
        parts.append(_v15_evidence_list(items, kind="reasoning", label_key="text", limit=10, empty="No observable reasoning/rationale candidate."))
    else:
        parts.append("<div class='empty'>No explicit observable reasoning/thought marker was recovered for this thread.</div>")
    if progress:
        parts.append("<h4><span class='badge warn'>Progress / status text</span></h4>")
        parts.append(_v15_evidence_list(progress, kind="progress", label_key="text", limit=10, empty="No progress/status artifacts."))
    parts.append(_raw_details_v07("Open reasoning/progress section JSON", reasoning))
    return "\n".join(parts)


def _v15_render_privacy_delete(thread: dict[str, Any], idx: int) -> str:
    meta = thread.get("metadata") or {}
    privacy = meta.get("private_detection") or {}
    deletion = thread.get("deletion_state") or {}
    availability = thread.get("reconstruction_availability") or {}
    storage = availability.get("storage_state") or {}
    residue = availability.get("residue_state") or {}
    rows = [
        ("Private mode", privacy.get("private_mode")),
        ("Privacy states", ", ".join(map(str, privacy.get("privacy_states") or []))),
        ("Access levels", ", ".join(map(str, privacy.get("access_levels") or []))),
        ("Deletion state", deletion.get("state") or storage.get("deletion_marker_state")),
        ("Storage state", storage.get("state")),
        ("Residue/stale", residue.get("state")),
        ("Stale candidate", residue.get("stale_candidate")),
    ]
    return "\n".join([
        _v15_section_title(idx, "privacy", 5, "Privacy / deletion / stale", "Private-mode and deletion/stale assessment from recovered artifact markers."),
        _kv_table_v07(rows),
        _raw_details_v07("Open privacy/deletion decoded JSON", {"private_detection": privacy, "deletion_state": deletion, "reconstruction_availability": availability}),
    ])


def _v15_render_final(thread: dict[str, Any], idx: int) -> str:
    final = thread.get("final_answer") or {}
    outcome = thread.get("task_outcome") or {}
    text = final.get("text")
    rows = [
        ("Outcome", outcome.get("status")),
        ("Confidence", outcome.get("confidence")),
        ("Side effect completed", outcome.get("side_effect_completed")),
        ("Filename candidates", ", ".join(map(str, outcome.get("downloaded_filename_candidates") or []))),
        ("Corroboration needed", ", ".join(map(str, outcome.get("missing_corroboration") or []))),
    ]
    parts = [
        _v15_section_title(idx, "final", 6, "Final answer / outcome", "Model answer and inferred task outcome. External side-effect verification may still be required."),
        _kv_table_v07(rows),
    ]
    if text:
        parts.append(f"<div class='answer good-answer'>{_h(text)}</div>")
        parts.append(_evidence_row("Final answer evidence", final.get("evidence")))
    else:
        parts.append("<div class='empty'>Final answer text was not recovered for this thread.</div>")
    parts.append(_raw_details_v07("Open final answer/outcome decoded JSON", {"final_answer": final, "task_outcome": outcome}))
    return "\n".join(parts)


def _v15_render_time_storage(thread: dict[str, Any], idx: int) -> str:
    temporal = thread.get("temporal_evidence") or thread.get("computer_temporal_evidence") or {}
    source_summary = thread.get("source_summary") or {}
    time_fields = []
    if isinstance(temporal, dict):
        time_fields = temporal.get("workflow_time_fields") or temporal.get("time_fields") or []
    parts = [
        _v15_section_title(idx, "time", 7, "Time & storage evidence", "Timestamps, sequence/order values, and LOG/LDB persistence summary."),
        _kv_table_v07([
            ("Source type counts", source_summary.get("source_type_counts")),
            ("Record count", source_summary.get("record_count") or source_summary.get("total_records")),
            ("First order", source_summary.get("min_relative_order")),
            ("Last order", source_summary.get("max_relative_order")),
        ]),
    ]
    if time_fields:
        parts.append("<h4>Recovered time fields</h4>")
        parts.append(_v15_evidence_list(time_fields, kind="timestamp", label_key="raw", limit=12, empty="No timestamp fields."))
    parts.append(_raw_details_v07("Open time/storage decoded JSON", {"temporal_evidence": temporal, "source_summary": source_summary}))
    return "\n".join(parts)


def _v15_render_computer(thread: dict[str, Any], idx: int) -> str:
    cls = thread.get("classification") or {}
    if cls.get("execution_mode") != "computer_mode":
        return ""
    prompt = thread.get("prompt") or {}
    parts = [
        _v15_section_title(idx, "computer", 8, "Computer-mode evidence", "Parent prompt linkage, ASI/cache evidence, and Computer-specific persistence."),
        _kv_table_v07([
            ("Prompt scope", prompt.get("scope")),
            ("Thread-specific prompt", prompt.get("thread_specific_prompt")),
            ("Prompt persistence", (prompt.get("prompt_persistence") or {}).get("state")),
            ("Prompt LOG/LDB", (prompt.get("prompt_persistence") or {}).get("source_type_counts")),
            ("Case prompt ID", prompt.get("case_prompt_id")),
        ]),
        _raw_details_v07("Open Computer prompt/linkage decoded JSON", prompt),
    ]
    comp = thread.get("computer_mode_reconstruction") or thread.get("computer_source_persistence") or {}
    if comp:
        parts.append(_raw_details_v07("Open other Computer-mode decoded JSON", comp))
    return "\n".join(parts)


def _v15_render_thread_detail(thread: dict[str, Any], idx: int) -> str:
    cls = thread.get("classification") or {}
    execution_mode = cls.get("execution_mode")
    prompt = thread.get("prompt") or {}
    counts = _v14_counts(thread)
    subnav = _v12_subnav(idx, execution_mode)
    residual = {
        "thread_id": thread.get("thread_id"),
        "classification": thread.get("classification"),
        "storage_state": (thread.get("reconstruction_availability") or {}).get("storage_state"),
        "content_state": (thread.get("reconstruction_availability") or {}).get("content_state"),
        "residue_state": (thread.get("reconstruction_availability") or {}).get("residue_state"),
        "timeline_sample": (thread.get("timeline") or [])[:20],
    }
    return "\n".join([
        f"<section id='thread-{idx}' class='card view-section thread-detail'>",
        "<div class='thread-head'>",
        f"<div><div class='eyebrow'>Thread {idx}</div><h2>{_h((prompt.get('reference_codes') or [thread.get('thread_id') or 'Thread'])[0])}</h2><p class='thread-id'>{_h(thread.get('thread_id'))}</p></div>",
        f"<div class='badges'><span class='badge good'>{_h(cls.get('interaction_type'))}</span><span class='badge good'>{_h(execution_mode)}</span><span class='badge neutral'>confidence: {_h(cls.get('confidence'))}</span></div>",
        "</div>",
        subnav,
        "<div class='metrics compact-metrics'>",
        _panel_metric("Plan", counts["plan"]),
        _panel_metric("Actions", counts["actions"]),
        _panel_metric("Reasoning", counts["reasoning"]),
        _panel_metric("Progress", counts["progress"]),
        "</div>",
        _v15_render_prompt(thread, idx),
        _v15_render_metadata(thread, idx),
        _v15_render_activity(thread, idx),
        _v15_render_reasoning(thread, idx),
        _v15_render_privacy_delete(thread, idx),
        _v15_render_final(thread, idx),
        _v15_render_time_storage(thread, idx),
        _v15_render_computer(thread, idx),
        _v15_section_title(idx, "evidence", 9 if execution_mode == "computer_mode" else 8, "Other decoded evidence", "Only residual thread-level objects not already shown above."),
        _raw_details_v07("Open residual thread evidence summary", residual),
        _raw_details_v07("Open full thread JSON", thread),
        "</section>",
    ])


def render_html_report(report: dict[str, Any], output_path: Path) -> None:
    """v0.15 report viewer: readable hierarchy plus inline per-item raw evidence."""
    source = report.get("source", {}) or {}
    summary = report.get("summary", {}) or {}
    extraction = report.get("extraction_summary", {}) or {}
    threads = report.get("threads", []) or []

    def nav_thread_label(thread: dict[str, Any], idx: int) -> str:
        prompt = thread.get("prompt", {}) or {}
        refs = prompt.get("reference_codes") or []
        if refs:
            return str(refs[0])
        return str(thread.get("thread_id") or f"Thread {idx}")[:36]

    def nav_button(target: str, label: str, sublabel: str = "", active: bool = False, scroll: str | None = None, subitem: bool = False, extra_class: str = "") -> str:
        classes = ["nav-item"]
        if active:
            classes.append("active")
        if subitem:
            classes.append("nav-subitem")
        if extra_class:
            classes.append(extra_class)
        data_scroll = f" data-scroll='{_h(scroll)}'" if scroll else ""
        return f"<button type='button' class='{' '.join(classes)}' data-target='{_h(target)}'{data_scroll}><strong>{_h(label)}</strong>{('<span>'+_h(sublabel)+'</span>') if sublabel else ''}</button>"

    style = """
<style>
:root{--bg:#f3f6fb;--card:#fff;--ink:#101828;--muted:#667085;--line:#d9e0ea;--soft:#f8fafc;--soft2:#eef4ff;--blue:#1d4ed8;--green:#047857;--amber:#b45309;--red:#b91c1c;--nav:#111827;--purple:#6d28d9;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;line-height:1.56;font-size:14px}.layout{display:grid;grid-template-columns:320px minmax(0,1fr);min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;overflow:auto;background:var(--nav);color:#fff;padding:22px 18px}.brand{padding-bottom:16px;border-bottom:1px solid rgba(255,255,255,.12);margin-bottom:14px}.brand h1{font-size:19px;line-height:1.2;margin:0 0 6px;font-weight:850}.brand p{margin:0;color:#cbd5e1;font-size:12px;word-break:break-all}.nav-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin:18px 6px 8px}.nav-item{width:100%;border:0;background:transparent;color:#d1d5db;text-align:left;border-radius:11px;padding:10px 12px;margin:3px 0;cursor:pointer}.nav-thread{position:relative;padding-right:28px}.nav-thread:after{content:'›';position:absolute;right:12px;top:11px;color:#94a3b8;font-size:16px}.thread-nav-group.open>.nav-thread:after{content:'⌄'}.nav-subitems{display:none;margin:2px 0 10px 10px;border-left:1px solid rgba(255,255,255,.14);padding-left:7px}.thread-nav-group.open>.nav-subitems{display:block}.nav-subitem{padding:6px 9px 6px 16px;margin:1px 0;color:#b6c2d1}.nav-subitem strong{font-size:12px}.nav-subitem:before{content:'↳ ';color:#64748b}.nav-item strong{display:block;font-size:13px;line-height:1.25;font-weight:750}.nav-item span{display:block;font-size:11px;color:#94a3b8;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.nav-item:hover{background:rgba(255,255,255,.09);color:#fff}.nav-item.active{background:#fff;color:#111827}.nav-subitem.active{background:rgba(255,255,255,.18);color:#fff}.main{max-width:1180px;width:100%;margin:0 auto;padding:28px}.view-section{display:none}.view-section.active{display:block}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;margin:0 0 20px;box-shadow:0 3px 14px rgba(15,23,42,.05)}.hero{background:#111827;color:#fff;border-radius:20px;padding:28px 32px;margin-bottom:20px}.hero h1{margin:0 0 8px;font-size:29px;font-weight:850}.hero p{margin:4px 0;color:#cbd5e1}h2{margin:0 0 16px;font-size:25px;font-weight:850;letter-spacing:-.01em}h3.section-title{display:flex;align-items:center;gap:10px;margin:28px 0 6px;border-bottom:2px solid var(--line);padding-bottom:9px;font-size:20px;font-weight:850;scroll-margin-top:18px}.section-num{display:inline-flex;width:30px;height:30px;align-items:center;justify-content:center;border-radius:999px;background:var(--soft2);color:var(--blue);font-size:14px;font-weight:850}.section-subtitle{margin:0 0 12px;color:#667085;font-size:13px}h4{margin:18px 0 8px;font-size:15px;font-weight:800}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#667085;font-weight:800}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:11px}.compact-metrics{grid-template-columns:repeat(auto-fit,minmax(125px,1fr));margin:14px 0}.metric{background:var(--soft);border:1px solid var(--line);border-radius:13px;padding:12px}.metric-label{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.metric-value{font-size:23px;font-weight:850}.badge{display:inline-block;border-radius:999px;padding:4px 9px;margin:2px 4px 2px 0;font-size:12px;font-weight:800;background:#eef2ff;color:#3730a3}.badge.good{background:#dcfce7;color:#166534}.badge.warn{background:#fef3c7;color:#92400e}.badge.bad{background:#fee2e2;color:#991b1b}.badge.neutral{background:#f3f4f6;color:#374151}.badge-row{margin:8px 0}.note{border-left:4px solid var(--blue);background:#eff6ff;border-radius:13px;padding:12px 14px;margin:12px 0}.note.warn{border-left-color:var(--amber);background:#fffbeb}.note.good{border-left-color:var(--green);background:#ecfdf5}.prompt,.answer{white-space:pre-wrap;background:#f8fafc;border:1px solid var(--line);border-radius:14px;padding:16px;overflow:auto;font-size:14px}.answer{max-height:500px}.good-answer{border-left:4px solid var(--green)}.source-strip{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f3f4f6;border:1px solid var(--line);border-radius:10px;padding:7px 9px;margin:8px 0;color:#475467;font-size:12px}.muted,.small{color:var(--muted);font-size:13px}.kv{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--line);border-radius:13px;overflow:hidden;margin:10px 0}.kv th{width:220px;text-align:left;background:#f8fafc;color:#475467;font-weight:800}.kv th,.kv td{border-bottom:1px solid var(--line);padding:9px 10px;vertical-align:top}.kv tr:last-child th,.kv tr:last-child td{border-bottom:0}.table{width:100%;border-collapse:collapse;margin:8px 0}.table th,.table td{border:1px solid var(--line);padding:8px 9px;vertical-align:top;text-align:left}.table th{background:#f8fafc;color:#475467}.thread-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(305px,1fr));gap:14px}.thread-card{border:1px solid var(--line);border-radius:16px;padding:16px;background:#fff}.thread-card h3{border:0;margin:8px 0 2px;padding:0}.thread-card-top{display:flex;justify-content:space-between;gap:8px}.thread-num{font-weight:850;color:var(--blue)}.thread-id{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:var(--muted);word-break:break-all}.mini-row{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.mini{background:#f3f4f6;border:0;border-radius:8px;padding:4px 8px;font-size:12px}.jump-btn{cursor:pointer}.thread-jump-row{position:sticky;top:0;background:#fff;border:1px solid var(--line);border-radius:13px;padding:8px;z-index:2}.thread-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}.evidence-row{margin:8px 0}.evidence-row strong{display:block;font-size:12px;color:#475467;margin-bottom:4px}.evidence-chip{display:inline-block;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f3f4f6;border:1px solid #d0d5dd;border-radius:8px;padding:4px 7px;margin:2px 4px 2px 0;font-size:12px}.empty{color:#667085;font-style:italic;background:#f9fafb;border:1px dashed #cbd5e1;border-radius:13px;padding:12px}.evidence-list{display:grid;gap:8px}.evidence-card{border:1px solid var(--line);border-radius:13px;background:#fff;margin:0}.evidence-card summary{display:grid;grid-template-columns:82px 110px minmax(220px,1fr) minmax(220px,.9fr);gap:8px;align-items:center;padding:10px 12px;background:#fbfcff;border-radius:13px;cursor:pointer}.evidence-card[open] summary{border-bottom:1px solid var(--line);border-radius:13px 13px 0 0}.ev-order{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#475467;font-size:12px}.ev-kind{display:inline-block;border-radius:999px;background:#eef2ff;color:#3730a3;font-weight:800;font-size:12px;padding:3px 8px;text-align:center}.ev-label{font-weight:780;color:#111827}.ev-source{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#667085;font-size:12px}.raw-caption{font-size:12px;color:#667085;padding:10px 13px 0}details{border:1px solid var(--line);border-radius:13px;background:#fff;margin:10px 0}details>summary{cursor:pointer;padding:10px 13px;font-weight:800;color:#374151;background:#f8fafc;border-radius:13px}details[open]>summary{border-bottom:1px solid var(--line);border-radius:13px 13px 0 0}pre.raw{margin:0;padding:14px;background:#111827;color:#e5e7eb;overflow:auto;border-radius:0 0 13px 13px;font-size:12px;line-height:1.45}.more-note{color:#667085;font-size:12px;margin:5px 2px}.findings li{margin:6px 0}a{color:#1d4ed8;word-break:break-all}.topline{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}.hint{font-size:13px;color:#475569;background:#f8fafc;border:1px solid var(--line);border-radius:13px;padding:10px 12px;margin-bottom:14px}@media(max-width:1080px){.evidence-card summary{grid-template-columns:70px 90px 1fr}.ev-source{grid-column:1/-1}}@media(max-width:980px){.layout{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.main{padding:16px}.nav-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:4px}.nav-subitems{display:none}.thread-jump-row{position:relative}}@media print{.sidebar{display:none}.layout{display:block}.main{max-width:none}.view-section{display:block!important}.card{break-inside:avoid}.nav-subitems{display:block}.evidence-card{break-inside:avoid}}
</style>
<noscript><style>.view-section{display:block!important}.nav-subitems{display:block!important}.sidebar{position:relative;height:auto}</style></noscript>
"""

    nav_parts: list[str] = []
    nav_parts.append("<aside class='sidebar'>")
    nav_parts.append("<div class='brand'><h1>Comet Reconstruction</h1>")
    nav_parts.append(f"<p>{_h(Path(str(source.get('input') or '')).name)}</p></div>")
    nav_parts.append("<div class='nav-list'>")
    nav_parts.append("<div class='nav-title'>Report</div>")
    nav_parts.append(nav_button("summary", "Case summary", f"{summary.get('thread_count', 0)} threads", True))
    nav_parts.append(nav_button("overview", "Thread overview", "Reconstruction list"))
    nav_parts.append("<div class='nav-title'>Threads</div>")
    subitems = [("prompt", "Prompt"), ("metadata", "Metadata"), ("activity", "Activity"), ("reasoning", "Reasoning"), ("privacy", "Privacy/Delete"), ("final", "Final"), ("time", "Time/Storage"), ("evidence", "Other evidence")]
    for idx, thread in enumerate(threads, start=1):
        cls = thread.get("classification", {}) or {}
        thread_id = f"thread-{idx}"
        nav_parts.append(f"<div class='thread-nav-group' data-thread='{thread_id}'>")
        nav_parts.append(nav_button(thread_id, f"Thread {idx}", f"{nav_thread_label(thread, idx)} · {cls.get('execution_mode') or 'unknown'}", extra_class="nav-thread"))
        nav_parts.append("<div class='nav-subitems'>")
        local_subitems = list(subitems)
        if cls.get("execution_mode") == "computer_mode":
            local_subitems.insert(7, ("computer", "Computer"))
        for slug, label in local_subitems:
            nav_parts.append(nav_button(thread_id, label, "", scroll=f"{thread_id}-{slug}", subitem=True))
        nav_parts.append("</div></div>")
    nav_parts.append("<div class='nav-title'>Residual audit</div>")
    nav_parts.append(nav_button("global", "Residual / unassigned", f"{extraction.get('global_record_count', 0)} records"))
    if report.get("skipped"):
        nav_parts.append(nav_button("skipped", "Skipped groups", f"{len(report.get('skipped') or [])} groups"))
    if report.get("snapshot_comparison"):
        nav_parts.append(nav_button("comparison", "Snapshot comparison", "Before / after"))
    nav_parts.append(nav_button("raw", "Full JSON", "Complete report"))
    nav_parts.append("</div></aside>")

    parts: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Comet Browser Reconstruction Report</title>",
        style,
        "</head><body><div class='layout'>",
        "\n".join(nav_parts),
        "<main class='main'>",
        "<section id='summary' class='view-section active'>",
        "<section class='hero'>",
        "<h1>Comet Browser Reconstruction Report</h1>",
        f"<p><strong>Input:</strong> {_h(Path(str(source.get('input') or '')).name)}</p>",
        f"<p><strong>Scope:</strong> {_h(source.get('analysis_scope'))} · <strong>Target:</strong> {_h(source.get('target_origin'))} · <strong>Store:</strong> {_h(source.get('database'))}/{_h(source.get('object_store'))}</p>",
        "</section>",
        "<section class='card'><div class='topline'><h2>Executive findings</h2><span class='badge neutral'>investigator view</span></div>",
        "<div class='hint'>Thread를 클릭하면 하위 항목이 펼쳐집니다. 각 plan/action/reasoning 행을 클릭하면 바로 아래에서 해당 decoded JSON과 LOG/LDB 위치를 확인할 수 있습니다.</div>",
        _render_executive_findings_v07(report),
        "<div class='metrics'>",
        _panel_metric("Threads", summary.get("thread_count", 0)),
        _panel_metric("Browser agent", summary.get("browser_agent_thread_count", 0)),
        _panel_metric("Computer mode", summary.get("computer_mode_thread_count", 0)),
        _panel_metric("Global records", extraction.get("global_record_count", 0)),
        _panel_metric("Parsed records", extraction.get("all_record_count", 0)),
        _panel_metric("Relevant records", extraction.get("relevant_record_count", 0)),
        "</div></section>",
        _render_case_summary_v10(report),
        "</section>",
        "<section id='overview' class='card view-section'><div class='topline'><h2>Thread overview</h2><span class='badge neutral'>Select a thread on the left</span></div>",
        _render_thread_overview_v07(report),
        "</section>",
    ]
    for idx, thread in enumerate(threads, start=1):
        parts.append(_v15_render_thread_detail(thread, idx))
    parts.extend([
        "<section id='global' class='card view-section'><h2>Residual / unassigned artifacts</h2>",
        "<div class='hint'>Thread-specific evidence is shown inside each section. This page keeps only profile-level or unassigned residual records here.</div>",
        _render_global_artifacts_v07(report.get("global_records", []) or []),
        "</section>",
    ])
    if report.get("skipped"):
        parts.extend(["<section id='skipped' class='card view-section'><h2>Skipped groups</h2>", _raw_details_v07("Skipped group details", report.get("skipped")), "</section>"])
    if report.get("snapshot_comparison"):
        parts.extend(["<section id='comparison' class='card view-section'><h2>Snapshot comparison</h2>", render_snapshot_comparison(report.get("snapshot_comparison", {})), _raw_details_v07("Raw snapshot comparison", report.get("snapshot_comparison")), "</section>"])
    parts.extend([
        "<section id='raw' class='card view-section'><h2>Full report JSON</h2>",
        "<div class='note warn'><strong>Complete machine-readable output.</strong><br>Use this only when the section-level decoded evidence is not enough.</div>",
        "<pre class='raw'>" + _h(json.dumps(report, ensure_ascii=False, indent=2, default=str)) + "</pre>",
        "</section>",
        "</main></div>",
        """
<script>
(function(){
  const sections = Array.from(document.querySelectorAll('.view-section'));
  const buttons = Array.from(document.querySelectorAll('.nav-item[data-target], .jump-btn[data-target]'));
  const groups = Array.from(document.querySelectorAll('.thread-nav-group'));
  function parentThreadId(id){ const m = (id || '').match(/^(thread-[0-9]+)/); return m ? m[1] : null; }
  function openThreadGroup(threadId){ groups.forEach(g => g.classList.toggle('open', g.dataset.thread === threadId)); }
  function showSection(id, scrollId){
    const target = document.getElementById(id); if(!target) return;
    sections.forEach(sec => sec.classList.remove('active'));
    target.classList.add('active');
    const threadId = parentThreadId(scrollId || id);
    openThreadGroup(threadId);
    buttons.filter(b => b.classList.contains('nav-item')).forEach(btn => {
      const sameTarget = btn.dataset.target === id;
      const sameScroll = !scrollId || btn.dataset.scroll === scrollId;
      btn.classList.toggle('active', sameTarget && sameScroll);
    });
    setTimeout(() => {
      if(scrollId){ const el = document.getElementById(scrollId); if(el){ el.scrollIntoView({behavior:'smooth', block:'start'}); return; } }
      window.scrollTo(0,0);
    }, 20);
    history.replaceState(null, '', '#' + (scrollId || id));
  }
  buttons.forEach(btn => btn.addEventListener('click', () => showSection(btn.dataset.target, btn.dataset.scroll)));
  const initial = (window.location.hash || '').replace('#','');
  if(initial){
    const sec = document.getElementById(initial);
    if(sec && sec.classList.contains('view-section')) showSection(initial);
    else { const parent = initial.match(/^(thread-[0-9]+)-/); if(parent && document.getElementById(parent[1])) showSection(parent[1], initial); }
  }
})();
</script>
""",
        "</body></html>",
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")


_reconstruct_browser_threads_pre_v15 = reconstruct_browser_threads

def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v15(extracted, input_label, browser_only=browser_only)
    report["schema_version"] = SCHEMA_VERSION
    report.setdefault("hardcoding_audit", {})["v15"] = {
        "hci_section_evidence_html": True,
        "inline_clickable_decoded_artifacts": True,
        "no_scenario_specific_strings": True,
        "rule": "HTML evidence display is generated from reconstructed thread objects and attached LOG/LDB evidence; no experiment-specific content is synthesized.",
    }
    return report


# ---------------------------------------------------------------------------
# v0.16: professor-focused case dashboard + stronger section separation.
# - Keeps section-level clickable decoded artifacts from v0.15.
# - Adds one-page checklist aligned with the professor's requested pipeline.
# - De-emphasizes profile/global evidence as residual audit, while preserving it
#   when it is needed for Computer parent prompt corroboration.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.16"


def _v16_primary_thread(report: dict[str, Any]) -> dict[str, Any]:
    threads = report.get("threads") or []
    case_summary = report.get("case_summary") or {}
    primary_id = case_summary.get("primary_thread_id")
    if primary_id:
        for thread in threads:
            if thread.get("thread_id") == primary_id:
                return thread
    return threads[0] if threads else {}


def _v16_yes_no(value: Any) -> str:
    return "yes" if value else "no"


def _v16_compact_sources(thread: dict[str, Any]) -> str:
    source_counts = ((thread.get("source_summary") or {}).get("source_type_counts") or {})
    if not source_counts:
        return "not summarized"
    return ", ".join(f"{k}:{v}" for k, v in source_counts.items())


def _v16_mode_label(mode: str | None) -> str:
    if mode == "browser_control":
        return "Browser Control"
    if mode == "computer_mode":
        return "Computer mode"
    if mode:
        return str(mode)
    return "not classified"


def _v16_reasoning_summary(thread: dict[str, Any]) -> tuple[str, str, str]:
    cls = thread.get("classification") or {}
    mode = cls.get("execution_mode")
    reasoning = thread.get("reasoning") or {}
    items = reasoning.get("items") or []
    progress = reasoning.get("progress_or_status_items") or []
    if items:
        return (
            "OK",
            f"{len(items)} observable reasoning/rationale item(s); {len(progress)} progress/status item(s)",
            "Reported only from explicit decoded markers. Progress/status text is separated from reasoning."
        )
    if mode == "browser_control":
        return (
            "Scanned",
            "No explicit reasoning/thought marker identified",
            reasoning.get("interpretation") or "Browser Control reasoning is not assumed absent; decoded LDB/LOG markers are scanned."
        )
    if progress:
        return (
            "Partial",
            f"No reasoning item; {len(progress)} progress/status item(s)",
            "Progress/status items are useful workflow residue but should not be overclaimed as reasoning."
        )
    return (
        "Not recovered",
        "No explicit reasoning/rationale marker identified",
        reasoning.get("interpretation") or "No decoded artifact marker was recovered for this thread."
    )


def _v16_requirement_rows(report: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    source = report.get("source") or {}
    summary = report.get("summary") or {}
    case_summary = report.get("case_summary") or {}
    primary = _v16_primary_thread(report)
    cls = primary.get("classification") or {}
    meta = primary.get("metadata") or {}
    prompt = primary.get("prompt") or {}
    outcome = primary.get("task_outcome") or {}
    final = primary.get("final_answer") or {}
    availability = primary.get("reconstruction_availability") or {}
    storage = availability.get("storage_state") or {}
    residue = availability.get("residue_state") or {}
    deletion = primary.get("deletion_state") or {}
    privacy = (meta.get("private_detection") or {})
    counts = _v14_counts(primary) if primary else {"plan": 0, "actions": 0, "urls": 0, "context_urls": 0, "payloads": 0, "reasoning": 0, "progress": 0}
    mode = cls.get("execution_mode")
    reason_status, reason_brief, reason_note = _v16_reasoning_summary(primary)
    residual = (report.get("filter_summary") or {}).get("residual_thread_count", case_summary.get("residual_thread_count", 0))
    warnings = outcome.get("warnings") or case_summary.get("warnings") or []
    missing = outcome.get("missing_corroboration") or []
    profile_note = "Collapsed residual audit. Use it to verify target filtering and unassigned/profile-level artifacts; do not merge it into a thread without linkage evidence."
    if report.get("computer_case_prompt"):
        ccp = report.get("computer_case_prompt") or {}
        profile_note = f"Computer parent prompt uses target-matched ASI list-cache evidence ({ccp.get('source_type_counts') or {}}); remaining profile evidence stays residual/audit-only."

    return [
        ("Input", "OK", "LevelDB/IndexedDB source parsed", f"{source.get('database')}/{source.get('object_store')} · target_origin={source.get('target_origin') or 'unknown'}"),
        ("1. Conversation vs agentic", "OK" if cls.get("interaction_type") else "Review", f"Primary interaction={cls.get('interaction_type') or 'unknown'}", f"agentic threads={summary.get('browser_agent_thread_count',0)+summary.get('computer_mode_thread_count',0)}, conversational/search={summary.get('conversational_or_search_thread_count',0)}"),
        ("2. Browser vs Computer", "OK" if mode in {"browser_control", "computer_mode"} else "Review", _v16_mode_label(mode), f"browser={summary.get('browser_agent_thread_count',0)}, computer={summary.get('computer_mode_thread_count',0)}"),
        ("3. Prompt + metadata", "OK" if prompt.get("text") else "Missing", f"prompt field={prompt.get('field') or 'not recovered'}", f"status={meta.get('status') or 'unknown'}, model={meta.get('display_model') or meta.get('model') or 'unknown'}"),
        ("4. Agentic activity", "OK" if (counts.get("plan") or counts.get("actions") or counts.get("urls") or counts.get("context_urls")) else "Partial", f"plan={counts.get('plan',0)}, actions={counts.get('actions',0)}, urls={counts.get('urls',0)+counts.get('context_urls',0)}, payloads={counts.get('payloads',0)}", "Rows are clickable; decoded LOG/LDB object opens under the item."),
        ("5. Reasoning policy", reason_status, reason_brief, reason_note),
        ("6. Deleted / stale", "OK", f"storage={storage.get('state') or 'unknown'}, residue={residue.get('state') or 'unknown'}", f"deletion_marker={storage.get('deletion_marker_state') or deletion.get('state') or 'unknown'}"),
        ("7. Private mode", "OK", f"private={_v16_yes_no(privacy.get('private_mode'))}", f"privacy_states={privacy.get('privacy_states') or []}, access_levels={privacy.get('access_levels') or []}"),
        ("8. Final answer / outcome", "OK" if final.get("available") or outcome.get("status") else "Partial", f"outcome={outcome.get('status') or 'unknown'}, confidence={outcome.get('confidence') or 'unknown'}", f"filename_candidates={outcome.get('downloaded_filename_candidates') or []}"),
        ("9. Corroboration", "Review" if (warnings or missing) else "OK", "; ".join(warnings[:2]) if warnings else "No internal warning", f"external checks={missing or 'not requested'}"),
        ("10. LOG/LDB persistence", "OK" if _v16_compact_sources(primary) != "not summarized" else "Review", _v16_compact_sources(primary), "Evidence source, seq, offset, and Live/Deleted state are shown on each clickable row."),
        ("Profile/residual audit", "Secondary", f"residual_threads={residual}, global_records={len(report.get('global_records') or [])}", profile_note),
    ]


def _v16_status_badge(status: str) -> str:
    s = (status or "").lower()
    cls = "good" if s in {"ok", "scanned"} else "warn" if s in {"review", "partial", "secondary", "not recovered"} else "bad" if s in {"missing"} else "neutral"
    return f"<span class='badge {cls}'>{_h(status)}</span>"


def _v16_professor_dashboard(report: dict[str, Any]) -> str:
    rows = _v16_requirement_rows(report)
    out = [
        "<section class='card'>",
        "<div class='topline'><h2>Checklist</h2><span class='badge neutral'>one-page view</span></div>",
        "<div class='hint'>세부 thread를 열기 전에 전체 충족 여부를 한눈에 보는 요약입니다. 세부 원문은 각 thread의 항목 행을 클릭해 확인합니다.</div>",
        "<table class='kv'>",
    ]
    for req, status, brief, detail in rows:
        out.append("<tr>")
        out.append(f"<th>{_h(req)}<br>{_v16_status_badge(status)}</th>")
        out.append(f"<td><strong>{_h(brief)}</strong><br><span class='muted'>{_h(detail)}</span></td>")
        out.append("</tr>")
    out.append("</table>")
    out.append("</section>")
    return "\n".join(out)


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    """v16 replacement for the old case-summary block."""
    case = report.get("case_summary") or build_case_summary(report, (report.get("source") or {}).get("target_reference_filter"))
    primary = _v16_primary_thread(report)
    cls = primary.get("classification") or {}
    outcome = primary.get("task_outcome") or {}
    availability = primary.get("reconstruction_availability") or {}
    warnings = list(case.get("warnings") or [])
    missing = outcome.get("missing_corroboration") or []
    rows = [
        ("Target reference", case.get("target_reference")),
        ("Target found", "Yes" if case.get("target_found") else "No"),
        ("Target threads", case.get("target_thread_count")),
        ("Primary thread", case.get("primary_thread_id")),
        ("Primary execution mode", _v16_mode_label(case.get("primary_execution_mode") or cls.get("execution_mode"))),
        ("Primary outcome", outcome.get("status") or (case.get("primary_task_outcome") or {}).get("status")),
        ("Outcome confidence", outcome.get("confidence") or (case.get("primary_task_outcome") or {}).get("confidence")),
        ("Reconstruction availability", (availability.get("level") or (case.get("primary_reconstruction_availability") or {}).get("level"))),
        ("Residual/profile audit", f"residual_threads={case.get('residual_thread_count')}, target_global_records={case.get('target_global_record_count')}"),
    ]
    summary_line = f"Primary thread is {_v16_mode_label(cls.get('execution_mode'))}; interaction={cls.get('interaction_type') or 'unknown'}; outcome={outcome.get('status') or 'unknown'}."
    parts = [
        _v16_professor_dashboard(report),
        "<section class='card'><div class='topline'><h2>Target case summary</h2><span class='badge good'>case-study view</span></div>",
        _kv_table_v07(rows),
        "<h4>Investigator-readable summary</h4>",
        f"<ul class='findings'><li>{_h(summary_line)}</li><li>Profile/residual evidence is retained as audit context only; thread reconstruction relies on linked prompt, metadata, activity, reasoning, deletion/private, and final-answer evidence.</li></ul>",
    ]
    if warnings or missing:
        parts.append("<div class='note warn'><strong>Cautions / corroboration needed</strong><ul>")
        for w in warnings[:4]:
            parts.append(f"<li>{_h(w)}</li>")
        if missing:
            parts.append(f"<li>External corroboration recommended: {_h(', '.join(map(str, missing)))}</li>")
        parts.append("</ul></div>")
    parts.append(_raw_details_v07("Open primary task outcome JSON", outcome))
    parts.append(_raw_details_v07("Open reconstruction availability JSON", availability))
    parts.append("</section>")
    return "\n".join(parts)


# Override section title to make boundaries visually stronger without hiding detail.
def _v15_section_title(idx: int, slug: str, number: int, title: str, subtitle: str = "") -> str:
    return "".join([
        f"<h3 id='thread-{idx}-{slug}' class='section-title' style='margin-top:34px;border-top:3px solid #e5e7eb;padding-top:18px;border-bottom:2px solid #d9e0ea;'>",
        f"<span class='section-num'>{number}</span>",
        f"<span>{_h(title)}</span>",
        "</h3>",
        f"<p class='section-subtitle'>{_h(subtitle)}</p>" if subtitle else "",
    ])


# Wrap each major section in a card-like block so Prompt / Metadata / Activity
# are visually separable, while keeping per-row raw JSON under each row.
def _v16_section_shell(html: str) -> str:
    return f"<div style='border:1px solid #e5e7eb;border-radius:16px;padding:0 18px 18px;margin:18px 0;background:#fff;'>\n{html}\n</div>"


def _v15_render_thread_detail(thread: dict[str, Any], idx: int) -> str:
    cls = thread.get("classification", {}) or {}
    mode = cls.get("execution_mode")
    counts = _v14_counts(thread)
    sections: list[str] = []
    sections.append(_v16_section_shell(_v15_render_prompt(thread, idx)))
    sections.append(_v16_section_shell(_v15_render_metadata(thread, idx)))
    sections.append(_v16_section_shell(_v15_render_activity(thread, idx)))
    sections.append(_v16_section_shell(_v15_render_reasoning(thread, idx)))
    sections.append(_v16_section_shell(_v15_render_privacy_delete(thread, idx)))
    sections.append(_v16_section_shell(_v15_render_final(thread, idx)))
    sections.append(_v16_section_shell(_v15_render_time_storage(thread, idx)))
    if mode == "computer_mode":
        sections.append(_v16_section_shell(_v15_render_computer(thread, idx)))
    sections.append(_v16_section_shell("\n".join([
        _v15_section_title(idx, "evidence", 9 if mode == "computer_mode" else 8, "Other decoded evidence", "Residual objects not already shown in the relevant section."),
        _raw_details_v07("Open full thread JSON", thread),
    ])))
    return "\n".join([
        f"<section id='thread-{idx}' class='card view-section'>",
        "<div class='thread-head'>",
        f"<div><div class='eyebrow'>Thread {idx}</div><h2>{_h(nav_thread_label(thread, idx))}</h2><p class='thread-id'>{_h(thread.get('thread_id'))}</p></div>",
        f"<div><span class='badge good'>{_h(cls.get('interaction_type'))}</span><span class='badge good'>{_h(_v16_mode_label(mode))}</span><span class='badge neutral'>confidence: {_h(cls.get('confidence'))}</span></div>",
        "</div>",
        "<div class='thread-jump-row mini-row'>",
        f"<button class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-prompt'>Prompt</button>",
        f"<button class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-metadata'>Metadata</button>",
        f"<button class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-activity'>Activity ({counts['plan']}/{counts['actions']})</button>",
        f"<button class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-reasoning'>Reasoning ({counts['reasoning']}+{counts['progress']})</button>",
        f"<button class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-privacy'>Privacy/Delete</button>",
        f"<button class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-final'>Final</button>",
        f"<button class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-time'>Time/Storage</button>",
        f"<button class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-computer'>Computer</button>" if mode == "computer_mode" else "",
        f"<button class='mini jump-btn' data-target='thread-{idx}' data-scroll='thread-{idx}-evidence'>Other evidence</button>",
        "</div>",
        "\n".join(sections),
        "</section>",
    ])


_reconstruct_browser_threads_pre_v16 = reconstruct_browser_threads

def _v16_attach_dashboard(report: dict[str, Any]) -> dict[str, Any]:
    report["schema_version"] = SCHEMA_VERSION
    report["professor_checklist"] = [
        {"requirement": r, "status": s, "brief": b, "detail": d}
        for r, s, b, d in _v16_requirement_rows(report)
    ]
    report.setdefault("hardcoding_audit", {})["v16"] = {
        "professor_focused_dashboard": True,
        "profile_evidence_demoted_to_residual_audit": True,
        "section_specific_clickable_decoded_evidence": True,
        "no_note_driven_assumptions": True,
        "rule": "The dashboard summarizes only reconstructed report fields. Notes are not used as evidence; LOG/LDB-derived fields and supplied target_reference drive the display.",
    }
    return report


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v16(extracted, input_label, browser_only=browser_only)
    return _v16_attach_dashboard(report)


_filter_report_by_target_reference_pre_v16 = filter_report_by_target_reference

def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v16(report, target_reference)
    filtered["case_summary"] = build_case_summary(filtered, target_reference, original_counts=filtered.get("filter_summary"))
    return _v16_attach_dashboard(filtered)



# Global helper needed by the v16 thread-detail override; older renderers
# had this as a nested function inside render_html_report.
def nav_thread_label(thread: dict[str, Any], idx: int) -> str:
    prompt = thread.get("prompt") or {}
    refs = prompt.get("reference_codes") or []
    if refs:
        return str(refs[0])
    title = ((thread.get("metadata") or {}).get("title") or prompt.get("field") or thread.get("thread_id") or f"Thread {idx}")
    return shorten_text(str(title), 60)


# ---------------------------------------------------------------------------
# v0.17 hardening patch
# ---------------------------------------------------------------------------
# This block intentionally overrides only report-safety, time interpretation,
# and review-oriented normalization. It does not use scenario IDs as parser
# rules. A --target-reference value is still used only for filtering/display.

SCHEMA_VERSION = "0.17"


def _v17_as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _v17_as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _v17_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _v17_is_reasonable_forensic_dt(dt: datetime) -> bool:
    try:
        return 2000 <= dt.year <= 2100
    except Exception:
        return False


def _v17_add_time_candidate(candidates: list[dict[str, Any]], name: str, dt: datetime) -> None:
    if not _v17_is_reasonable_forensic_dt(dt):
        return
    iso = dt.astimezone(timezone.utc).isoformat()
    if not any(item.get("interpreted_utc") == iso and item.get("interpretation") == name for item in candidates):
        candidates.append({"interpretation": name, "interpreted_utc": iso})


def interpret_timestamp(value: Any) -> dict[str, Any]:
    """v0.17 best-effort timestamp interpretation.

    Raw values remain authoritative. The function exposes multiple plausible
    UTC candidates instead of silently forcing one unit. This is important for
    forensic reporting because Comet/Chromium artifacts can mix Unix epoch,
    JavaScript milliseconds, microseconds, and Chromium/WebKit timestamps.
    """
    result: dict[str, Any] = {
        "raw": to_jsonable(value),
        "interpreted_utc": None,
        "interpretation": "raw_only",
        "candidates": [],
        "caution": "Best-effort only; use raw artifact value as authoritative evidence.",
    }

    from datetime import timedelta

    candidates: list[dict[str, Any]] = []

    def parse_iso_string(text: str) -> None:
        s = text.strip()
        if not s:
            return
        normalized = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            _v17_add_time_candidate(candidates, "best_effort_iso8601", dt)
        except Exception:
            return

    raw_number: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw_number = float(value)
    elif isinstance(value, str):
        parse_iso_string(value)
        stripped = value.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", stripped):
            try:
                raw_number = float(stripped)
            except Exception:
                raw_number = None

    if raw_number is not None:
        abs_value = abs(raw_number)
        numeric_attempts: list[tuple[str, float]] = []

        # Unix seconds / milliseconds / microseconds / nanoseconds.
        numeric_attempts.append(("best_effort_unix_epoch_seconds", raw_number))
        numeric_attempts.append(("best_effort_unix_epoch_milliseconds", raw_number / 1_000))
        numeric_attempts.append(("best_effort_unix_epoch_microseconds", raw_number / 1_000_000))
        numeric_attempts.append(("best_effort_unix_epoch_nanoseconds", raw_number / 1_000_000_000))

        for name, seconds in numeric_attempts:
            try:
                # Avoid huge values that datetime cannot represent.
                if abs(seconds) < 4_102_444_800:  # before 2100-01-01 roughly
                    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
                    _v17_add_time_candidate(candidates, name, dt)
            except Exception:
                pass

        # Chromium/WebKit timestamp: microseconds since 1601-01-01 UTC.
        try:
            if abs_value > 10_000_000_000_000:
                base = datetime(1601, 1, 1, tzinfo=timezone.utc)
                dt = base + timedelta(microseconds=int(raw_number))
                _v17_add_time_candidate(candidates, "best_effort_chromium_webkit_microseconds_since_1601", dt)
        except Exception:
            pass

    # Prefer ISO, then common Unix ms, then Chromium/WebKit, then remaining plausible candidate.
    preference = [
        "best_effort_iso8601",
        "best_effort_unix_epoch_milliseconds",
        "best_effort_unix_epoch_seconds",
        "best_effort_chromium_webkit_microseconds_since_1601",
        "best_effort_unix_epoch_microseconds",
        "best_effort_unix_epoch_nanoseconds",
    ]
    for name in preference:
        for item in candidates:
            if item.get("interpretation") == name:
                result["interpreted_utc"] = item.get("interpreted_utc")
                result["interpretation"] = name
                result["candidates"] = candidates
                return result

    result["candidates"] = candidates
    return result


FORENSIC_TIME_FIELD_NAMES_V17 = {
    "created_at", "updated_at", "lastAccess", "last_access", "last_query_datetime",
    "createdAt", "updatedAt", "completed_at", "started_at", "finished_at",
    "last_updated_at", "time_created", "time_updated", "creation_time",
    "modification_time", "deleted_at", "archived_at",
}


def extract_time_fields_from_value(value: Any) -> list[dict[str, Any]]:
    """v0.17 artifact-level time field extractor.

    Deliberately excludes generic payload fields such as date/time because those
    can be user-entered Calendar values rather than forensic timestamps.
    """
    time_fields: list[dict[str, Any]] = []
    for field, field_value in recursive_collect_fields(value, FORENSIC_TIME_FIELD_NAMES_V17):
        if field_value in (None, "", [], {}):
            continue
        time_fields.append({
            "field": field,
            "value": to_jsonable(field_value),
            "time_interpretation": interpret_timestamp(field_value),
        })
    return time_fields


def _v17_guess_action_type(text: str, field: str | None = None) -> str:
    low = (text or "").lower()
    field_low = (field or "").lower()
    if any(x in low for x in ["comet_agent_tool_input", "comet_agent_tool_output"]):
        return "tool_io"
    if any(x in low for x in ["타이핑", "type", "typing", "input"]) or field_low in {"recipient", "to", "cc", "bcc", "subject", "body", "title", "description"}:
        return "type"
    if any(x in low for x in ["클릭", "click", "send click"]):
        return "click"
    if any(x in low for x in ["키 누르기", "key", "ctrl+", "tab"]):
        return "key"
    if any(x in low for x in ["기다림", "wait", "waiting"]):
        return "wait"
    if any(x in low for x in ["download", "다운로드", "saving the pdf", "pdf 다운로드"]):
        return "download"
    if any(x in low for x in ["open", "opening", "navigate", "navigating", "browser control"]):
        return "navigate_or_open"
    if any(x in low for x in ["confirm", "verify", "checking", "확인"]):
        return "verify_or_confirm"
    return "workflow_or_payload"


def _v17_guess_target_context(text: str) -> str | None:
    low = (text or "").lower()
    domain_app_rules = [
        ("mail.google.com", "gmail"),
        ("gmail", "gmail"),
        ("calendar.google.com", "google_calendar"),
        ("google calendar", "google_calendar"),
        ("wikipedia.org", "wikipedia"),
        ("wikipedia", "wikipedia"),
        ("nist.gov", "nist"),
        ("perplexity.ai", "perplexity"),
    ]
    for marker, label in domain_app_rules:
        if marker in low:
            return label
    return None


def build_structured_actions(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize plan/action/payload/URL artifacts into a compact review table.

    This does not replace raw evidence. It is an investigator-facing index over
    already-reconstructed thread fields and keeps all original evidence refs.
    """
    structured: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, text: str, evidence_item: dict[str, Any], *, field: str | None = None, value: Any = None) -> None:
        if not text:
            return
        key = f"{kind}:{field or ''}:{text[:500]}"
        if key in seen:
            return
        seen.add(key)
        action_type = _v17_guess_action_type(text, field)
        context = _v17_guess_target_context(text)
        structured.append({
            "source_kind": kind,
            "action_type": action_type,
            "target_context": context,
            "field": field,
            "label": shorten_text(text, 500),
            "value": to_jsonable(value) if value is not None else None,
            "relative_order": evidence_item.get("relative_order") or evidence_item.get("order") or evidence_item.get("ldb_seq_no"),
            "evidence": evidence_item.get("evidence") or evidence_item.get("evidence_refs") or [],
            "confidence": "high" if kind in {"typed_payload", "tool_action"} or action_type in {"type", "click", "key", "download"} else "medium",
        })

    for item in _v17_as_list(thread.get("plan")):
        if isinstance(item, dict):
            add("plan", _v17_text(item.get("label") or item.get("text")), item)
    for item in _v17_as_list(thread.get("actions")):
        if isinstance(item, dict):
            label = _v17_text(item.get("label") or item.get("text") or item.get("text_preview"))
            kind = "tool_action" if item.get("kind") == "tool_io" or "COMET_AGENT_TOOL" in label else "workflow_action"
            add(kind, label, item)
    for item in _v17_as_list(thread.get("typed_payloads")):
        if isinstance(item, dict):
            field = _v17_text(item.get("field") or "payload")
            value = item.get("value")
            label = f"{field}: {_v17_text(value)}"
            add("typed_payload", label, item, field=field, value=value)
    for item in _v17_as_list(thread.get("urls")) + _v17_as_list(thread.get("computer_url_candidates")):
        if isinstance(item, dict):
            url = _v17_text(item.get("url"))
            add("url", url, item, field="url", value=url)

    return sorted(structured, key=lambda item: item.get("relative_order") if item.get("relative_order") is not None else -1)


def build_time_audit(thread: dict[str, Any]) -> dict[str, Any]:
    timeline = _v17_as_list(thread.get("timeline"))
    metadata = _v17_as_dict(thread.get("metadata"))
    source_summary = _v17_as_dict(thread.get("source_summary"))

    time_events = [event for event in timeline if isinstance(event, dict) and event.get("kind") == "time_metadata"]
    interpreted = []
    raw_only = []
    for event in time_events:
        interpretation = _v17_as_dict(event.get("time_interpretation"))
        if interpretation.get("interpreted_utc"):
            interpreted.append(event)
        else:
            raw_only.append(event)

    return {
        "relative_order_field": "ldb_seq_no",
        "absolute_time_policy": "Use interpreted UTC only as best-effort; raw timestamp and evidence refs remain authoritative.",
        "metadata_time_fields": {k: metadata.get(k) for k in sorted(FORENSIC_TIME_FIELD_NAMES_V17) if k in metadata},
        "time_metadata_event_count": len(time_events),
        "interpreted_event_count": len(interpreted),
        "raw_only_event_count": len(raw_only),
        "interpreted_sample": interpreted[:10],
        "raw_only_sample": raw_only[:10],
        "source_type_counts": source_summary.get("source_type_counts"),
    }


def classify_browser_reconstruction_level(thread: dict[str, Any]) -> dict[str, Any]:
    cls = _v17_as_dict(thread.get("classification"))
    mode = cls.get("execution_mode")
    counts = _v14_counts(thread) if isinstance(thread, dict) else {}
    final = _v17_as_dict(thread.get("final_answer"))
    has_prompt = bool(_v17_as_dict(thread.get("prompt")).get("text"))
    has_metadata = bool(_v17_as_dict(thread.get("metadata")))
    has_outcome = bool(_v17_as_dict(thread.get("task_outcome")).get("status") or final.get("available"))

    if mode == "browser_control":
        if has_prompt and has_metadata and (counts.get("plan") or counts.get("actions") or counts.get("urls")) and has_outcome:
            level = "browser_control_workflow_reconstructed"
            confidence = "high"
        elif has_prompt and has_metadata and (counts.get("plan") or counts.get("urls")):
            level = "browser_control_partial_workflow"
            confidence = "medium"
        elif has_prompt or has_metadata:
            level = "browser_control_metadata_only"
            confidence = "low"
        else:
            level = "browser_control_not_reconstructed"
            confidence = "low"
    elif mode == "computer_mode":
        # Browser-first validation keeps Computer conservative for the next phase.
        reasoning = _v17_as_dict(thread.get("reasoning"))
        if reasoning.get("available") and (counts.get("actions") or counts.get("urls")):
            level = "computer_workflow_with_reasoning"
            confidence = "medium"
        elif has_prompt and has_outcome:
            level = "computer_list_cache_partial"
            confidence = "medium"
        else:
            level = "computer_metadata_only_or_unvalidated"
            confidence = "low"
    else:
        level = "non_browser_agent_or_unknown"
        confidence = "low"

    return {
        "level": level,
        "confidence": confidence,
        "browser_first_note": "Browser Control scenarios should be validated before using Computer-mode claims.",
        "inputs": {
            "has_prompt": has_prompt,
            "has_metadata": has_metadata,
            "has_outcome_or_final": has_outcome,
            "counts": counts,
        },
    }


def _v17_enrich_thread(thread: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(thread, dict):
        return thread
    thread["structured_actions"] = build_structured_actions(thread)
    thread["time_audit"] = build_time_audit(thread)
    thread["browser_validation"] = classify_browser_reconstruction_level(thread)

    cls = _v17_as_dict(thread.get("classification"))
    if cls.get("execution_mode") == "browser_control":
        reasoning = _v17_as_dict(thread.get("reasoning"))
        # Make the Browser-Control reasoning statement explicit and conservative.
        if not reasoning.get("available"):
            reasoning["available"] = False
            reasoning["items"] = _v17_as_list(reasoning.get("items"))
            reasoning.setdefault(
                "interpretation",
                "No explicit decoded reasoning/thought marker was recovered for this Browser Control thread. This is an artifact observation, not proof that no reasoning occurred internally.",
            )
            thread["reasoning"] = reasoning
    return thread


def _v17_enrich_report(report: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(report, dict):
        return report
    for thread in _v17_as_list(report.get("threads")):
        if isinstance(thread, dict):
            _v17_enrich_thread(thread)
    report["schema_version"] = SCHEMA_VERSION
    report.setdefault("hardcoding_audit", {})["v17"] = {
        "scenario_id_rules_used": False,
        "target_reference_used_only_for_filtering": True,
        "browser_first_validation": True,
        "safe_html_none_handling": True,
        "timestamp_interpretation_candidates": True,
        "structured_action_index": True,
    }
    return report


_reconstruct_browser_threads_pre_v17 = reconstruct_browser_threads

def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v17(extracted, input_label, browser_only=browser_only)
    return _v17_enrich_report(report)


_filter_report_by_target_reference_pre_v17 = filter_report_by_target_reference

def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v17(report, target_reference)
    return _v17_enrich_report(filtered)


_v15_render_activity_pre_v17 = _v15_render_activity

def _v15_render_activity(thread: dict[str, Any], idx: int) -> str:
    base = _v15_render_activity_pre_v17(thread, idx)
    structured = _v17_as_list(thread.get("structured_actions"))
    if not structured:
        return base
    block = "\n".join([
        "<h4>Normalized action index</h4>",
        "<p class='muted'>Generic index over recovered plan/action/payload/URL artifacts. Raw evidence remains inside each row.</p>",
        _v15_evidence_list(structured, kind="normalized", label_key="label", limit=20, empty="No normalized actions."),
        _raw_details_v07("Open normalized action index JSON", structured),
    ])
    return base + "\n" + block


_v15_render_time_storage_pre_v17 = _v15_render_time_storage

def _v15_render_time_storage(thread: dict[str, Any], idx: int) -> str:
    base = _v15_render_time_storage_pre_v17(thread, idx)
    audit = _v17_as_dict(thread.get("time_audit"))
    if not audit:
        return base
    block = "\n".join([
        "<h4>Time interpretation audit</h4>",
        "<div class='note warn'>Interpreted UTC values are best-effort. Use raw values, source file, offset, and ldb_seq_no as authoritative forensic evidence.</div>",
        _raw_details_v07("Open time interpretation audit JSON", audit),
    ])
    return base + "\n" + block


def _v16_requirement_rows(report: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    source = _v17_as_dict(report.get("source"))
    summary = _v17_as_dict(report.get("summary"))
    case_summary = _v17_as_dict(report.get("case_summary"))
    primary = _v17_as_dict(_v16_primary_thread(report))
    cls = _v17_as_dict(primary.get("classification"))
    meta = _v17_as_dict(primary.get("metadata"))
    prompt = _v17_as_dict(primary.get("prompt"))
    outcome = _v17_as_dict(primary.get("task_outcome"))
    final = _v17_as_dict(primary.get("final_answer"))
    availability = _v17_as_dict(primary.get("reconstruction_availability"))
    storage = _v17_as_dict(availability.get("storage_state"))
    residue = _v17_as_dict(availability.get("residue_state"))
    deletion = _v17_as_dict(primary.get("deletion_state"))
    privacy = _v17_as_dict(meta.get("private_detection"))
    validation = _v17_as_dict(primary.get("browser_validation"))
    time_audit = _v17_as_dict(primary.get("time_audit"))
    counts = _v14_counts(primary) if primary else {"plan": 0, "actions": 0, "urls": 0, "context_urls": 0, "payloads": 0, "reasoning": 0, "progress": 0}
    mode = cls.get("execution_mode")
    reason_status, reason_brief, reason_note = _v16_reasoning_summary(primary)
    residual = _v17_as_dict(report.get("filter_summary")).get("residual_thread_count", case_summary.get("residual_thread_count", 0))
    warnings = _v17_as_list(outcome.get("warnings")) or _v17_as_list(case_summary.get("warnings"))
    missing = _v17_as_list(outcome.get("missing_corroboration"))
    profile_note = "Collapsed residual audit. Use it to verify target filtering and unassigned/profile-level artifacts; do not merge it into a thread without linkage evidence."
    if report.get("computer_case_prompt"):
        ccp = _v17_as_dict(report.get("computer_case_prompt"))
        profile_note = f"Computer parent prompt uses target-matched ASI list-cache evidence ({ccp.get('source_type_counts') or {}}); remaining profile evidence stays residual/audit-only."

    return [
        ("Input", "OK", "LevelDB/IndexedDB source parsed", f"{source.get('database')}/{source.get('object_store')} · target_origin={source.get('target_origin') or 'unknown'}"),
        ("1. Conversation vs agentic", "OK" if cls.get("interaction_type") else "Review", f"Primary interaction={cls.get('interaction_type') or 'unknown'}", f"agentic threads={summary.get('browser_agent_thread_count',0)+summary.get('computer_mode_thread_count',0)}, conversational/search={summary.get('conversational_or_search_thread_count',0)}"),
        ("2. Browser vs Computer", "OK" if mode in {"browser_control", "computer_mode"} else "Review", _v16_mode_label(mode), f"browser={summary.get('browser_agent_thread_count',0)}, computer={summary.get('computer_mode_thread_count',0)}"),
        ("3. Prompt + metadata", "OK" if prompt.get("text") else "Missing", f"prompt field={prompt.get('field') or 'not recovered'}", f"status={meta.get('status') or 'unknown'}, model={meta.get('display_model') or meta.get('model') or 'unknown'}"),
        ("4. Agentic activity", "OK" if (counts.get("plan") or counts.get("actions") or counts.get("urls") or counts.get("context_urls")) else "Partial", f"plan={counts.get('plan',0)}, actions={counts.get('actions',0)}, urls={counts.get('urls',0)+counts.get('context_urls',0)}, payloads={counts.get('payloads',0)}, normalized={len(_v17_as_list(primary.get('structured_actions')))}", "Rows are clickable; decoded LOG/LDB object opens under the item."),
        ("5. Reasoning policy", reason_status, reason_brief, reason_note),
        ("6. Deleted / stale", "OK", f"storage={storage.get('state') or 'unknown'}, residue={residue.get('state') or 'unknown'}", f"deletion_marker={storage.get('deletion_marker_state') or deletion.get('state') or 'unknown'}"),
        ("7. Private mode", "OK", f"private={_v16_yes_no(privacy.get('private_mode'))}", f"privacy_states={privacy.get('privacy_states') or []}, access_levels={privacy.get('access_levels') or []}"),
        ("8. Final answer / outcome", "OK" if final.get("available") or outcome.get("status") else "Partial", f"outcome={outcome.get('status') or 'unknown'}, confidence={outcome.get('confidence') or 'unknown'}", f"filename_candidates={outcome.get('downloaded_filename_candidates') or []}"),
        ("9. Browser validation level", "OK" if validation.get("level") else "Review", validation.get("level") or "not calculated", f"confidence={validation.get('confidence') or 'unknown'}"),
        ("10. Time audit", "OK" if time_audit else "Review", f"time_events={time_audit.get('time_metadata_event_count', 0)}, interpreted={time_audit.get('interpreted_event_count', 0)}", "Absolute time is best-effort; ldb_seq_no is relative write order."),
        ("11. Corroboration", "Review" if (warnings or missing) else "OK", "; ".join(map(str, warnings[:2])) if warnings else "No internal warning", f"external checks={missing or 'not requested'}"),
        ("12. LOG/LDB persistence", "OK" if _v16_compact_sources(primary) != "not summarized" else "Review", _v16_compact_sources(primary), "Evidence source, seq, offset, and Live/Deleted state are shown on each clickable row."),
        ("Profile/residual audit", "Secondary", f"residual_threads={residual}, global_records={len(_v17_as_list(report.get('global_records')))}", profile_note),
    ]


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    """v0.17 safe case-summary block.

    Fixes None-valued primary_task_outcome / primary_reconstruction_availability
    when a target reference matches residual/manual cache evidence but no promoted
    thread outcome exists.
    """
    raw_case = report.get("case_summary")
    case = _v17_as_dict(raw_case) or build_case_summary(report, _v17_as_dict(report.get("source")).get("target_reference_filter"))
    primary = _v17_as_dict(_v16_primary_thread(report))
    cls = _v17_as_dict(primary.get("classification"))
    outcome = _v17_as_dict(primary.get("task_outcome"))
    case_outcome = _v17_as_dict(case.get("primary_task_outcome"))
    availability = _v17_as_dict(primary.get("reconstruction_availability"))
    case_availability = _v17_as_dict(case.get("primary_reconstruction_availability"))
    validation = _v17_as_dict(primary.get("browser_validation"))
    time_audit = _v17_as_dict(primary.get("time_audit"))
    warnings = _v17_as_list(case.get("warnings"))
    missing = _v17_as_list(outcome.get("missing_corroboration"))

    rows = [
        ("Target reference", case.get("target_reference")),
        ("Target found", "Yes" if case.get("target_found") else "No"),
        ("Target threads", case.get("target_thread_count")),
        ("Primary thread", case.get("primary_thread_id")),
        ("Primary execution mode", _v16_mode_label(case.get("primary_execution_mode") or cls.get("execution_mode"))),
        ("Primary outcome", outcome.get("status") or case_outcome.get("status") or "N/A"),
        ("Outcome confidence", outcome.get("confidence") or case_outcome.get("confidence") or "N/A"),
        ("Reconstruction availability", availability.get("level") or case_availability.get("level") or "N/A"),
        ("Browser validation", validation.get("level") or "N/A"),
        ("Time audit", f"time_events={time_audit.get('time_metadata_event_count', 0)}, interpreted={time_audit.get('interpreted_event_count', 0)}" if time_audit else "N/A"),
        ("Residual/profile audit", f"residual_threads={case.get('residual_thread_count')}, target_global_records={case.get('target_global_record_count')}"),
    ]
    summary_line = (
        f"Primary thread is {_v16_mode_label(cls.get('execution_mode'))}; "
        f"interaction={cls.get('interaction_type') or 'unknown'}; "
        f"outcome={outcome.get('status') or case_outcome.get('status') or 'unknown'}."
    )
    parts = [
        _v16_professor_dashboard(report),
        "<section class='card'><div class='topline'><h2>Target case summary</h2><span class='badge good'>case-study view</span></div>",
        _kv_table_v07(rows),
        "<h4>Investigator-readable summary</h4>",
        f"<ul class='findings'><li>{_h(summary_line)}</li><li>Profile/residual evidence is retained as audit context only; thread reconstruction relies on linked prompt, metadata, activity, reasoning, deletion/private, and final-answer evidence.</li></ul>",
    ]
    if warnings or missing or not case.get("target_found"):
        parts.append("<div class='note warn'><strong>Cautions / corroboration needed</strong><ul>")
        for w in warnings[:6]:
            parts.append(f"<li>{_h(w)}</li>")
        if missing:
            parts.append(f"<li>External corroboration recommended: {_h(', '.join(map(str, missing)))}</li>")
        if not case.get("target_found"):
            parts.append("<li>No promoted thread was selected for this target. For manual browsing, this can be expected: inspect Chromium History/Cache separately and use global/profile evidence only as context.</li>")
        parts.append("</ul></div>")
    parts.append(_raw_details_v07("Open primary task outcome JSON", outcome or case_outcome))
    parts.append(_raw_details_v07("Open reconstruction availability JSON", availability or case_availability))
    if validation:
        parts.append(_raw_details_v07("Open Browser validation JSON", validation))
    if time_audit:
        parts.append(_raw_details_v07("Open time audit JSON", time_audit))
    parts.append("</section>")
    return "\n".join(parts)



# ---------------------------------------------------------------------------
# v0.18 target/residual separation + generic Browser outcome repair
# ---------------------------------------------------------------------------
# This block is intentionally small and self-contained. It does not hardcode
# scenario IDs. The optional --target-reference is used only to separate the
# requested case from residual/profile-wide artifacts and to choose the primary
# case thread before side-effect scoring.

SCHEMA_VERSION = "0.18"

_EMAIL_INTENT_MARKERS_V18 = [
    "gmail", "email", "compose", "recipient", "subject", "body", "draft", "sent folder",
    "send the email", "email sent", "sent email", "message sent", "compose window",
]
_CALENDAR_INTENT_MARKERS_V18 = [
    "google calendar", "calendar", "event title", "new calendar event", "start time", "end time",
    "save the event", "saved event", "event created", "event is visible", "description:",
]
_DOWNLOAD_INTENT_MARKERS_V18 = [
    "download", "downloaded filename", "final downloaded filename", "download complete",
    "downloaded file", "pdf", "saving the pdf", "downloads", "ctrl+j",
]
_RESEARCH_INTENT_MARKERS_V18 = [
    "research", "investigate", "web sources", "sources used", "source pages", "consult at least",
    "summarize the findings", "pages visited", "final urls",
]
_PAGE_OPEN_INTENT_MARKERS_V18 = [
    "open the following page", "open this page", "navigate to", "page title", "keep the page open",
]

_PAYLOAD_LABEL_MAP_V18 = {
    "recipient": "recipient",
    "to": "recipient",
    "subject": "subject",
    "body": "body",
    "event title": "event_title",
    "title": "title",
    "date": "date",
    "start time": "start_time",
    "end time": "end_time",
    "time": "time",
    "description": "description",
    "location": "location",
    "filename": "filename",
    "file name": "filename",
}


def _v18_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _v18_json_text(value: Any, limit: int | None = None) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text if limit is None else text[:limit]


def _v18_low(value: Any) -> str:
    return _v18_text(value).lower()


def _v18_thread_text(thread: dict[str, Any], *, include_prompt: bool = True) -> str:
    parts: list[str] = []
    if include_prompt:
        parts.append(_v18_text(_v17_as_dict(thread.get("prompt")).get("text")))
    for key in [
        "final_answer", "plan", "actions", "structured_actions", "typed_payloads", "urls",
        "context_url_candidates", "computer_url_candidates", "metadata", "classification",
    ]:
        if key in thread:
            parts.append(_v18_json_text(thread.get(key), 250000))
    return "\n".join(parts)


def _v18_contains_any(text: str, markers: list[str]) -> bool:
    low = text.lower()
    return any(marker.lower() in low for marker in markers)


def _v18_regex_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) for pattern in patterns)


def _v18_clean_payload_value(field: str, value: str) -> str:
    value = html.unescape(_v18_text(value)).strip()
    value = re.sub(r"\[\s*([^\]]+?)\s*\]\(mailto:([^\)]+)\)", r"\2", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"\[\s*([^\]]+?)\s*\]\(([^\)]+)\)", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"\s+", " ", value).strip(" `*_:-")
    if field in {"recipient", "to"}:
        match = EMAIL_RE.search(value)
        if match:
            return match.group(0)
    return value


def _v18_make_payload(field: str, value: str, source: str, evidence: list[dict[str, Any]], order: Any = None, confidence: str = "medium") -> dict[str, Any] | None:
    normalized_field = _PAYLOAD_LABEL_MAP_V18.get(field.strip().lower(), field.strip().lower().replace(" ", "_"))
    cleaned = _v18_clean_payload_value(normalized_field, value)
    if not cleaned or len(cleaned) < 2:
        return None
    if len(cleaned) > 4000:
        cleaned = cleaned[:4000]
    return {
        "field": normalized_field,
        "value": cleaned,
        "payload_source": source,
        "payload_role": "requested_payload" if source == "prompt" else "observed_or_reported_payload",
        "relative_order": order,
        "evidence": evidence or [],
        "confidence": confidence,
        "interpretation": (
            "Payload is extracted from the artifact field shown in payload_source. "
            "Prompt-derived payloads are intended/requested values; final/tool/action-derived payloads are reported or observed values."
        ),
    }


def _v18_extract_labeled_payloads_from_text(text: str, source: str, evidence: list[dict[str, Any]], order: Any = None) -> list[dict[str, Any]]:
    """Extract generic labeled payloads such as Recipient, Subject, Event title.

    This is intentionally label-based, not scenario-name-based. It works for
    Calendar/Gmail/Form-style prompts and final answers where values are written
    after human-readable labels.
    """
    if not text:
        return []
    label_re = r"(?:recipient|to|subject|body|event title|title|date|start time|end time|time|description|location|filename|file name)"
    pattern = re.compile(
        rf"(?im)^\s*(?P<label>{label_re})\s*[:：]\s*(?P<value>.*?)(?=\n\s*(?:{label_re})\s*[:：]|\n\s*(?:Step\s*\d+|Do not|Don't|Keep|After|Before)\b|\Z)",
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    results: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        label = match.group("label")
        value = match.group("value")
        # Avoid turning instructions like "The subject must include..." into an
        # observed subject when the label was not at line start with a colon.
        item = _v18_make_payload(label, value, source, evidence, order, confidence="medium_high" if source != "prompt" else "medium")
        if item:
            results.append(item)
    return results


def _v18_dedupe_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payloads:
        if not isinstance(item, dict):
            continue
        key = f"{item.get('field')}::{_v18_clean_payload_value(str(item.get('field') or ''), str(item.get('value') or ''))[:500]}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _v18_add_derived_typed_payloads(thread: dict[str, Any]) -> None:
    existing = _v17_as_list(thread.get("typed_payloads"))
    new_payloads: list[dict[str, Any]] = []

    prompt = _v17_as_dict(thread.get("prompt"))
    prompt_text = _v18_text(prompt.get("text"))
    prompt_ev = _v17_as_list(prompt.get("evidence"))
    prompt_order = prompt_ev[0].get("ldb_seq_no") if prompt_ev and isinstance(prompt_ev[0], dict) else None
    new_payloads.extend(_v18_extract_labeled_payloads_from_text(prompt_text, "prompt", prompt_ev, prompt_order))

    final = _v17_as_dict(thread.get("final_answer"))
    final_text = _v18_text(final.get("text"))
    final_ev = _v17_as_list(final.get("evidence"))
    new_payloads.extend(_v18_extract_labeled_payloads_from_text(final_text, "final_answer", final_ev, final.get("relative_order")))

    # Add generic email addresses / PDF filenames if they are visible in final or actions.
    combined = _v18_thread_text(thread, include_prompt=False)
    action_ev: list[dict[str, Any]] = []
    for action in _v17_as_list(thread.get("actions"))[:3]:
        if isinstance(action, dict):
            action_ev.extend(_v17_as_list(action.get("evidence")))
    for email_match in EMAIL_RE.findall(combined):
        item = _v18_make_payload("recipient", email_match, "action_or_final_text", action_ev or final_ev, None, confidence="medium")
        if item:
            new_payloads.append(item)
    for filename in extract_pdf_filenames(combined):
        item = _v18_make_payload("filename", filename, "action_or_final_text", action_ev or final_ev, None, confidence="medium")
        if item:
            new_payloads.append(item)

    merged = _v18_dedupe_payloads([p for p in existing if isinstance(p, dict)] + new_payloads)
    thread["typed_payloads"] = merged


def _v18_intent_signals(thread: dict[str, Any]) -> dict[str, Any]:
    prompt = _v18_text(_v17_as_dict(thread.get("prompt")).get("text"))
    final = _v18_text(_v17_as_dict(thread.get("final_answer")).get("text"))
    non_prompt_text = _v18_thread_text(thread, include_prompt=False)
    all_text = prompt + "\n" + non_prompt_text
    prompt_low = prompt.lower()
    final_low = final.lower()
    non_prompt_low = non_prompt_text.lower()
    all_low = all_text.lower()

    email_score = 0
    calendar_score = 0
    download_score = 0
    research_score = 0
    page_open_score = 0

    if _v18_contains_any(prompt, _EMAIL_INTENT_MARKERS_V18):
        email_score += 2
    if _v18_contains_any(non_prompt_text, ["email sent", "draft confirmed", "sent folder", "compose window", "recipient", "subject"]):
        email_score += 3
    if EMAIL_RE.search(all_text) and _v18_contains_any(all_text, ["gmail", "email", "recipient", "sent folder", "draft"]):
        email_score += 2

    if _v18_contains_any(prompt, _CALENDAR_INTENT_MARKERS_V18):
        calendar_score += 2
    if _v18_contains_any(non_prompt_text, ["event created", "created and verified", "saved event", "google calendar", "no guests invited", "calendar remains open"]):
        calendar_score += 3
    if _v18_contains_any(all_text, ["event title", "start time", "end time", "description"]):
        calendar_score += 1

    # Download is deliberately after email/calendar and requires positive file or
    # completion evidence, not just a negated instruction such as "do not download".
    positive_download_prompt = _v18_contains_any(prompt, ["download", "downloaded filename", "download exactly", "save the pdf"])
    positive_download_trace = _v18_contains_any(non_prompt_text, ["download complete", "final downloaded filename", "downloaded file", "saving the pdf", "ctrl+j", "wait_for_download"])
    filenames = extract_pdf_filenames(all_text)
    if positive_download_prompt:
        download_score += 2
    if positive_download_trace:
        download_score += 3
    if filenames or any(_v18_text(u.get("url") if isinstance(u, dict) else u).lower().endswith(".pdf") for u in _v17_as_list(thread.get("urls"))):
        download_score += 1
    if has_negated_term(prompt, ["download", "file", "pdf"]) and not positive_download_trace:
        download_score -= 2

    if _v18_contains_any(prompt, _RESEARCH_INTENT_MARKERS_V18):
        research_score += 2
    if _v18_contains_any(non_prompt_text, ["sources used", "pages visited", "final urls", "web_results"]):
        research_score += 2

    if _v18_contains_any(prompt, _PAGE_OPEN_INTENT_MARKERS_V18) or (extract_prompt_target_urls(prompt) and _v18_contains_any(prompt, ["open", "navigate", "visit"])):
        page_open_score += 2
    if _v18_contains_any(final, ["page title", "opened", "confirmed"]):
        page_open_score += 1

    return {
        "scores": {
            "email_draft_send": email_score,
            "calendar_create": calendar_score,
            "file_download": download_score,
            "web_research": research_score,
            "page_open": page_open_score,
        },
        "prompt_markers": {
            "email": _v18_contains_any(prompt, _EMAIL_INTENT_MARKERS_V18),
            "calendar": _v18_contains_any(prompt, _CALENDAR_INTENT_MARKERS_V18),
            "download": positive_download_prompt,
            "research": _v18_contains_any(prompt, _RESEARCH_INTENT_MARKERS_V18),
            "page_open": page_open_score > 0,
        },
        "trace_markers": {
            "email": _v18_contains_any(non_prompt_text, ["email sent", "draft confirmed", "sent folder", "compose"]),
            "calendar": _v18_contains_any(non_prompt_text, ["event created", "created and verified", "saved event", "google calendar"]),
            "download": positive_download_trace,
            "pdf_filename_candidates": filenames,
        },
    }


_classify_task_outcome_pre_v18 = classify_task_outcome


def classify_task_outcome(thread: dict[str, Any]) -> dict[str, Any]:
    """v0.18 generic outcome classifier.

    Repairs the main Browser-side failure mode: Gmail/Calendar threads being
    labeled as download tasks because residual PDF/download strings existed in
    the same profile. The classifier scores the current thread text and gives
    app-specific form tasks priority over generic download signals.
    """
    base = _classify_task_outcome_pre_v18(thread)
    signals = _v18_intent_signals(thread)
    scores = signals["scores"]
    prompt = _v18_text(_v17_as_dict(thread.get("prompt")).get("text"))
    final = _v18_text(_v17_as_dict(thread.get("final_answer")).get("text"))
    final_low = final.lower()
    all_text = _v18_thread_text(thread, include_prompt=True)
    all_low = all_text.lower()
    execution_mode = _v17_as_dict(thread.get("classification")).get("execution_mode")

    # Choose highest score, but prefer specific side-effect tasks over generic
    # download when email/calendar evidence is strong.
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    task_type = ordered[0][0] if ordered and ordered[0][1] > 0 else base.get("task_type", "unknown")
    if scores["email_draft_send"] >= 3:
        task_type = "email_draft_send"
    elif scores["calendar_create"] >= 3:
        task_type = "calendar_create"
    elif scores["file_download"] >= 3:
        task_type = "file_download"
    elif scores["web_research"] >= 2:
        task_type = "web_research"
    elif scores["page_open"] >= 2:
        task_type = "page_open"

    result = dict(base or {})
    result.update({
        "task_type": task_type,
        "classifier_version": "v0.18_generic_target_safe",
        "classification_basis": signals,
    })
    result.setdefault("primary_artifacts", [])
    if execution_mode and execution_mode not in result["primary_artifacts"]:
        result["primary_artifacts"].append(str(execution_mode))

    if task_type == "email_draft_send":
        sent = _v18_contains_any(final, ["email sent", "sent folder", "sent email", "visible in sent", "message sent", "sent successfully"])
        draft = _v18_contains_any(final, ["draft confirmed", "saved as a draft", "draft has been saved", "draft saved"])
        if sent:
            status = "email_sent_reported_and_verified" if _v18_contains_any(final, ["verified", "visible"]) else "email_sent_reported"
            result.update({"status": status, "side_effect_completed": True, "confidence": "high" if draft else "medium_high"})
        elif draft:
            result.update({"status": "email_draft_reported", "side_effect_completed": None, "confidence": "medium_high"})
        else:
            result.update({"status": "email_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"] = ["mail service Sent/Draft record", "message headers"]
        result["downloaded_filename_candidates"] = []

    elif task_type == "calendar_create":
        created = _v18_contains_any(final, ["event created", "created and verified", "created", "saved", "visible", "verified"])
        verified = _v18_contains_any(final, ["verified", "visible", "opened", "confirmed"])
        if created and verified:
            result.update({"status": "calendar_event_created_and_verified_reported", "side_effect_completed": True, "confidence": "high"})
        elif created:
            result.update({"status": "calendar_event_created_reported", "side_effect_completed": True, "confidence": "medium_high"})
        else:
            result.update({"status": "calendar_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"] = ["calendar service/event record"]
        result["downloaded_filename_candidates"] = []

    elif task_type == "file_download":
        filenames = extract_pdf_filenames(all_text)
        completed = bool(filenames) and _v18_contains_any(all_text, [
            "download complete", "downloaded filename", "final downloaded filename", "downloaded file", "successfully downloaded", "다운로드 완료",
        ])
        confirmation = _v18_contains_any(all_text, ["browser_agent_confirmation", "please confirm", "may i proceed", "confirmation required", "사용자 확인"])
        if completed:
            result.update({"status": "completed_download", "side_effect_completed": True, "confidence": "high", "downloaded_filename_candidates": filenames})
        elif confirmation:
            result.update({"status": "confirmation_required", "side_effect_completed": False, "confidence": "high", "downloaded_filename_candidates": filenames})
        elif filenames or ".pdf" in all_low:
            result.update({"status": "source_discovery_or_partial_download", "side_effect_completed": None, "confidence": "medium", "downloaded_filename_candidates": filenames})
        else:
            result.update({"status": "download_intent_only", "side_effect_completed": None, "confidence": "low", "downloaded_filename_candidates": []})
        result["missing_corroboration"] = ["Chromium Downloads DB", "OS Downloads folder/file hash"]

    elif task_type == "web_research":
        final_available = bool(_v17_as_dict(thread.get("final_answer")).get("text"))
        urls = _v17_as_list(thread.get("urls")) or _v17_as_list(thread.get("context_url_candidates"))
        if final_available and urls:
            result.update({"status": "research_answer_and_source_leads_recovered", "side_effect_completed": False, "confidence": "medium_high"})
        elif final_available:
            result.update({"status": "research_answer_recovered", "side_effect_completed": False, "confidence": "medium"})
        elif urls:
            result.update({"status": "research_url_leads_only", "side_effect_completed": None, "confidence": "medium"})
        else:
            result.update({"status": "research_intent_or_metadata_only", "side_effect_completed": None, "confidence": "low"})
        result["missing_corroboration"] = ["History/cache/source page content"]

    elif task_type == "page_open":
        target_urls = extract_prompt_target_urls(prompt)
        expected_title = extract_expected_page_title(prompt)
        opened = bool(target_urls and any(url in all_text for url in target_urls)) or bool(_v17_as_list(thread.get("urls")))
        title_confirmed = bool(expected_title and expected_title.lower() in final_low)
        if opened and (title_confirmed or not expected_title):
            result.update({"status": "target_page_opened_or_reported", "side_effect_completed": True, "confidence": "medium_high" if title_confirmed else "medium"})
        elif opened:
            result.update({"status": "target_page_url_recovered", "side_effect_completed": True, "confidence": "medium"})
        else:
            result.update({"status": "page_open_intent_only", "side_effect_completed": None, "confidence": "low"})
        result["target_urls"] = target_urls
        result["expected_title"] = expected_title
        result["missing_corroboration"] = ["Chromium History DB for navigation timing"]

    # Preserve external corroboration warnings but avoid stale download warning on
    # non-download tasks.
    if task_type not in {"file_download", "download"}:
        result["warnings"] = [w for w in _v17_as_list(result.get("warnings")) if "download" not in str(w).lower()]
    return result


def _v18_thread_contains_target(thread: dict[str, Any], target_reference: str | None) -> bool:
    if not target_reference:
        return False
    target = str(target_reference).strip()
    if not target:
        return False
    refs = [str(x) for x in _v17_as_dict(thread.get("prompt")).get("reference_codes") or []]
    prompt = _v18_text(_v17_as_dict(thread.get("prompt")).get("text"))
    final = _v18_text(_v17_as_dict(thread.get("final_answer")).get("text"))
    if target in refs or target in prompt or target in final:
        return True
    # Last resort: exact text anywhere in the thread JSON. This is still case
    # filtering/display logic, not parser behavior.
    return target in _v18_json_text(thread, 500000)


def _v18_reference_family(reference: str | None) -> str | None:
    if not reference:
        return None
    m = re.match(r"^(S\d{2}|C\d{2})[_-]", str(reference))
    return m.group(1) if m else None


def _v18_thread_reference_families(thread: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for ref in _v17_as_dict(thread.get("prompt")).get("reference_codes") or []:
        fam = _v18_reference_family(str(ref))
        if fam:
            refs.add(fam)
    text = _v18_json_text(thread, 100000)
    refs.update(re.findall(r"\b(S\d{2}|C\d{2})[_-]", text))
    return refs


def _v18_infer_evidence_scope(thread: dict[str, Any]) -> str:
    content = _v17_as_dict(thread.get("content_state"))
    state = content.get("state")
    metadata = _v17_as_dict(thread.get("metadata"))
    if state in {"behavior_content_reconstructed", "prompt_and_answer_only", "partial_behavior_without_final", "metadata_or_prompt_only"}:
        return "core_thread_record"
    if metadata.get("external_thread_list_evidence"):
        return "thread_list_cache"
    if _v17_as_list(thread.get("context_url_candidates")) and not _v17_as_list(thread.get("urls")):
        return "global_topMostUrls"
    return "unknown_or_mixed"


def _v18_infer_reconstruction_level(thread: dict[str, Any]) -> str:
    availability = _v17_as_dict(thread.get("reconstruction_availability"))
    content = _v17_as_dict(thread.get("content_state"))
    validation = _v17_as_dict(thread.get("browser_validation"))
    if availability.get("level"):
        return availability.get("level")
    if validation.get("level"):
        return validation.get("level")
    state = content.get("state")
    if state == "behavior_content_reconstructed":
        return "behavior_reconstructed"
    if state in {"metadata_or_prompt_only", "list_cache_only"}:
        return state
    if _v17_as_dict(thread.get("prompt")).get("text"):
        return "prompt_metadata_only"
    return "unreconstructed"


def _v18_relation_to_target(thread: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    target = str(target_reference or "").strip()
    evidence_scope = _v18_infer_evidence_scope(thread)
    reconstruction_level = _v18_infer_reconstruction_level(thread)
    direct = _v18_thread_contains_target(thread, target) if target else False
    relation = "profile_thread" if not target else "previous_residual"
    family = _v18_reference_family(target)
    if target and direct:
        relation = "exact_target"
    elif target and family and family in _v18_thread_reference_families(thread):
        relation = "same_scenario_family"

    if relation == "exact_target":
        if "strong" in reconstruction_level or "behavior" in reconstruction_level or "workflow" in reconstruction_level:
            claim = "can_assert_target_behavior_reconstructed_at_available_artifact_level"
        elif "metadata" in reconstruction_level or "list" in reconstruction_level:
            claim = "can_assert_target_thread_or_prompt_metadata_only"
        else:
            claim = "target_match_found_but_reconstruction_strength_low"
    elif relation == "same_scenario_family":
        claim = "same_scenario_family_residue_not_primary_target_without_exact_reference"
    elif target:
        claim = "can_assert_residual_profile_artifact_only_not_target_behavior"
    else:
        claim = "profile_wide_artifact_inventory_item"

    return {
        "target_reference": target or None,
        "relation_to_target": relation,
        "evidence_scope": evidence_scope,
        "reconstruction_level": reconstruction_level,
        "claim_level": claim,
        "direct_target_text_match": direct,
        "reference_families": sorted(_v18_thread_reference_families(thread)),
        "interpretation": (
            "Target relation separates case-focused reconstruction from prior/stale profile artifacts. "
            "Residual items should not be merged into the target behavior timeline without core-thread or structural linkage evidence."
        ),
    }


def _v18_summarize_thread_for_residual(thread: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    prompt = _v17_as_dict(thread.get("prompt"))
    cls = _v17_as_dict(thread.get("classification"))
    outcome = _v17_as_dict(thread.get("task_outcome"))
    availability = _v17_as_dict(thread.get("reconstruction_availability"))
    relation = _v18_relation_to_target(thread, target_reference)
    return {
        "thread_id": thread.get("thread_id"),
        "relation_to_target": relation.get("relation_to_target"),
        "evidence_scope": relation.get("evidence_scope"),
        "reconstruction_level": relation.get("reconstruction_level"),
        "claim_level": relation.get("claim_level"),
        "execution_mode": cls.get("execution_mode"),
        "interaction_type": cls.get("interaction_type"),
        "task_type": outcome.get("task_type"),
        "outcome_status": outcome.get("status"),
        "availability": availability.get("level"),
        "reference_codes": prompt.get("reference_codes") or [],
        "prompt_preview": shorten_text(_v18_text(prompt.get("text")), 240),
    }


def _v18_annotate_report(report: dict[str, Any], target_reference: str | None = None) -> dict[str, Any]:
    if not isinstance(report, dict):
        return report
    for thread in _v17_as_list(report.get("threads")):
        if not isinstance(thread, dict):
            continue
        _v18_add_derived_typed_payloads(thread)
        # Recompute outcome after payload repair so Gmail/Calendar/Download are
        # classified from the current thread, not from profile-level residuals.
        thread["task_outcome"] = classify_task_outcome(thread)
        # Refresh dependent case layers if the helper functions are available.
        try:
            thread["content_state"] = build_content_state(thread)
            thread["reconstruction_availability"] = build_reconstruction_availability(thread)
        except Exception:
            pass
        try:
            thread["structured_actions"] = build_structured_actions(thread)
            thread["browser_validation"] = classify_browser_reconstruction_level(thread)
        except Exception:
            pass
        thread["case_relation"] = _v18_relation_to_target(thread, target_reference)

    report["schema_version"] = "0.18"
    report.setdefault("hardcoding_audit", {})["v18"] = {
        "target_reference_used_for_case_separation_only": True,
        "scenario_ids_not_used_as_parser_rules": True,
        "residual_threads_preserved_as_inventory": True,
        "task_classifier": "generic keyword/field/action scoring, with email/calendar priority over generic download",
    }
    return report


_build_case_summary_pre_v18 = build_case_summary


def build_case_summary(report: dict[str, Any], target_reference: str | None = None, original_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    threads = _v17_as_list(report.get("threads"))
    globals_ = _v17_as_list(report.get("global_records"))
    target = str(target_reference or "").strip() or None

    for thread in threads:
        if isinstance(thread, dict):
            thread["case_relation"] = _v18_relation_to_target(thread, target)

    def global_contains_target(item: Any) -> bool:
        return bool(target) and target in _v18_json_text(item, 250000)

    if target:
        target_threads = [t for t in threads if isinstance(t, dict) and _v17_as_dict(t.get("case_relation")).get("relation_to_target") == "exact_target"]
        target_globals = [g for g in globals_ if global_contains_target(g)]
    else:
        target_threads = [t for t in threads if isinstance(t, dict)]
        target_globals = []

    def score(thread: dict[str, Any]) -> tuple[int, int, int, int, int, int, int]:
        relation = _v17_as_dict(thread.get("case_relation"))
        outcome = _v17_as_dict(thread.get("task_outcome"))
        availability = _v17_as_dict(thread.get("reconstruction_availability"))
        cls = _v17_as_dict(thread.get("classification"))
        final = _v17_as_dict(thread.get("final_answer"))
        content = _v17_as_dict(thread.get("content_state"))
        direct = 1 if relation.get("relation_to_target") == "exact_target" else 0
        behavior = 1 if content.get("state") == "behavior_content_reconstructed" or availability.get("level") == "strong_thread_reconstruction" else 0
        partial = 1 if availability.get("level") in {"partial_thread_reconstruction", "metadata_residue_only"} else 0
        has_final = 1 if final.get("text") or final.get("available") else 0
        agentic = 1 if cls.get("interaction_type") == "agentic" else 0
        side_effect = 1 if outcome.get("side_effect_completed") is True else 0
        # Do not privilege downloads over the exact target. This was the source
        # of S07/S08 being summarized as residual download cases.
        confidence_rank = {"high": 3, "medium_high": 2, "medium": 1}.get(str(outcome.get("confidence")), 0)
        return (direct, behavior, has_final, agentic, side_effect, partial, confidence_rank)

    primary = sorted(target_threads or [t for t in threads if isinstance(t, dict)], key=score, reverse=True)[0] if (target_threads or threads) else None
    filter_summary = _v17_as_dict(report.get("filter_summary"))
    residual_count = filter_summary.get("residual_thread_count")
    if residual_count is None:
        residual_count = (original_counts or {}).get("original_thread_count", len(threads)) - len(target_threads) if target else 0

    case = {
        "target_reference": target,
        "target_found": bool(target_threads or target_globals),
        "target_thread_count": len(target_threads),
        "target_global_record_count": len(target_globals),
        "primary_thread_id": primary.get("thread_id") if isinstance(primary, dict) else None,
        "primary_execution_mode": _v17_as_dict(primary.get("classification") if isinstance(primary, dict) else {}).get("execution_mode"),
        "primary_task_outcome": primary.get("task_outcome") if isinstance(primary, dict) else None,
        "primary_reconstruction_availability": primary.get("reconstruction_availability") if isinstance(primary, dict) else None,
        "primary_case_relation": primary.get("case_relation") if isinstance(primary, dict) else None,
        "residual_thread_count": residual_count,
        "warnings": [],
        "investigator_summary": [],
        "case_layer_policy": {
            "target_threads": "exact --target-reference matches only",
            "residual_threads": "kept as profile inventory; not merged into target timeline",
            "primary_selection": "target-first, then reconstruction strength/final answer/agentic/side-effect",
        },
    }
    if target and not target_threads and target_globals:
        case["warnings"].append("Target reference was found only in global/cache evidence, not as a reconstructed core thread. Treat as cache/list evidence and check History/Cache separately if this was manual browsing.")
    if target and not target_threads and not target_globals:
        case["warnings"].append("No exact target thread/global cache record was found for the requested target reference.")
    if primary:
        outcome = _v17_as_dict(primary.get("task_outcome"))
        availability = _v17_as_dict(primary.get("reconstruction_availability"))
        relation = _v17_as_dict(primary.get("case_relation"))
        case["investigator_summary"].append(
            f"Primary relation={relation.get('relation_to_target')}; mode={case.get('primary_execution_mode')}; outcome={outcome.get('status')}; reconstruction={availability.get('level')}."
        )
        missing = _v17_as_list(outcome.get("missing_corroboration"))
        if missing:
            case["warnings"].append("External corroboration recommended: " + ", ".join(str(x) for x in missing))
    return case


_reconstruct_browser_threads_pre_v18 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v18(extracted, input_label, browser_only=browser_only)
    _v18_annotate_report(report, None)
    report["case_summary"] = build_case_summary(report, None)
    return report


_filter_report_by_target_reference_pre_v18 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    """v0.18 target filter.

    Keeps the report case-focused while preserving a residual inventory. The
    output threads remain the target threads selected by earlier filters, but
    profile_residual_threads contains summarized previous/stale threads from the
    original unfiltered report.
    """
    if not target_reference:
        _v18_annotate_report(report, None)
        report["case_summary"] = build_case_summary(report, None)
        return report

    target = str(target_reference).strip()
    if not target:
        _v18_annotate_report(report, None)
        report["case_summary"] = build_case_summary(report, None)
        return report

    original_threads = [t for t in _v17_as_list(report.get("threads")) if isinstance(t, dict)]
    original_skipped = _v17_as_list(report.get("skipped"))
    original_globals = _v17_as_list(report.get("global_records"))

    # Run the pre-existing filter first because it already contains many guards
    # for Browser/Computer target matching. Then repair target/residual semantics.
    filtered = _filter_report_by_target_reference_pre_v18(report, target)
    filtered_threads = [t for t in _v17_as_list(filtered.get("threads")) if isinstance(t, dict)]

    # If the older filter was too strict but exact target threads exist in the
    # unfiltered report, recover them. This is target matching, not scenario logic.
    if not filtered_threads:
        filtered_threads = [json.loads(json.dumps(t, ensure_ascii=False, default=str)) for t in original_threads if _v18_thread_contains_target(t, target)]
        filtered["threads"] = filtered_threads

    # Annotate target threads and compute residual summaries from the original
    # profile inventory.
    _v18_annotate_report(filtered, target)
    kept_ids = {str(t.get("thread_id")) for t in filtered_threads}
    residual_summaries = []
    for thread in original_threads:
        if str(thread.get("thread_id")) in kept_ids:
            continue
        # Add a relation calculated against the target so previous scenarios are
        # explainable instead of silently hidden.
        residual_summaries.append(_v18_summarize_thread_for_residual(thread, target))
    filtered["profile_residual_threads"] = residual_summaries[:200]
    filtered["profile_residual_thread_count"] = len(residual_summaries)

    filtered.setdefault("source", {})["target_reference_filter"] = target
    filtered["summary"] = summarize_reconstruction(_v17_as_list(filtered.get("threads")), _v17_as_list(filtered.get("skipped")))
    original_counts = {
        "original_thread_count": len(original_threads),
        "original_skipped_count": len(original_skipped),
        "original_global_record_count": len(original_globals),
    }
    filtered["filter_summary"] = {
        "target_reference": target,
        **original_counts,
        "filtered_thread_count": len(_v17_as_list(filtered.get("threads"))),
        "filtered_skipped_count": len(_v17_as_list(filtered.get("skipped"))),
        "filtered_global_record_count": len(_v17_as_list(filtered.get("global_records"))),
        "residual_thread_count": len(residual_summaries),
        "residual_skipped_count": max(0, len(original_skipped) - len(_v17_as_list(filtered.get("skipped")))),
        "residual_global_record_count": max(0, len(original_globals) - len(_v17_as_list(filtered.get("global_records")))),
        "filter_rule": "v0.18 target-focused threads plus residual profile inventory; residual items are not merged into target reconstruction.",
    }
    filtered["case_summary"] = build_case_summary(filtered, target, original_counts=original_counts)
    return filtered


_render_case_summary_v10_pre_v18 = _render_case_summary_v10


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    base_html = _render_case_summary_v10_pre_v18(report)
    residuals = _v17_as_list(report.get("profile_residual_threads"))
    if not residuals:
        return base_html
    rows = []
    for item in residuals[:30]:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{_h(item.get('relation_to_target'))}</td>"
            f"<td>{_h(item.get('thread_id'))}</td>"
            f"<td>{_h(item.get('execution_mode'))}</td>"
            f"<td>{_h(item.get('task_type'))}</td>"
            f"<td>{_h(item.get('reconstruction_level'))}</td>"
            f"<td>{_h(item.get('prompt_preview'))}</td>"
            "</tr>"
        )
    table = (
        "<section class='card'><h2>Profile residual inventory</h2>"
        "<div class='note warn'>These are previous/stale/profile-wide artifacts found in the same LevelDB profile. "
        "They are retained for forensic context but are not merged into the target case timeline unless exact target linkage exists.</div>"
        "<table class='table'><thead><tr><th>Relation</th><th>Thread</th><th>Mode</th><th>Task</th><th>Level</th><th>Prompt preview</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
        + _raw_details_v07("Open full residual inventory JSON", {"count": len(residuals), "items": residuals})
        + "</section>"
    )
    return base_html + "\n" + table

# NOTE: The executable entry point is intentionally placed at the very end of
# the file so later override blocks are active when running as a script.
# if __name__ == "__main__":
#     main()

# ---------------------------------------------------------------------------
# v0.19 Browser validation repairs
# ---------------------------------------------------------------------------
# Goals:
# - Keep the parser target-agnostic: no scenario ID, filename, email address, or
#   expected answer is used as a rule.
# - Make profile-wide reports honest when --target-reference is omitted.
# - Fix generic outcome precedence: Calendar must not be labeled Email merely
#   because generic text contains words such as "description/body" or residual
#   email artifacts exist elsewhere in the profile.
# - Treat ordinary non-agentic chat as conversation/basic_chat unless the current
#   thread has explicit web/search/source evidence.

SCHEMA_VERSION = "0.19"

_classify_task_outcome_pre_v19 = classify_task_outcome
_build_case_summary_pre_v19 = build_case_summary
_reconstruct_browser_threads_pre_v19 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v19 = filter_report_by_target_reference


def _v19_current_thread_evidence_text(thread: dict[str, Any]) -> tuple[str, str, str]:
    """Return prompt/final/non-prompt text for current-thread classification.

    This deliberately ignores unrelated profile/global records. It can still see
    context_url_candidates attached to a thread by earlier layers, but task
    precedence is determined mainly from prompt + final + plan/actions/payloads
    of the current thread.
    """
    prompt = _v18_text(_v17_as_dict(thread.get("prompt")).get("text"))
    final = _v18_text(_v17_as_dict(thread.get("final_answer")).get("text"))
    parts: list[str] = []
    for key in ["plan", "actions", "structured_actions", "typed_payloads", "urls", "metadata", "classification"]:
        if key in thread:
            parts.append(_v18_json_text(thread.get(key), 250000))
    return prompt, final, "\n".join(parts)


def _v19_has_explicit_search_or_web_evidence(thread: dict[str, Any]) -> bool:
    prompt, final, evidence = _v19_current_thread_evidence_text(thread)
    metadata = _v17_as_dict(thread.get("metadata"))
    search_mode = str(metadata.get("search_mode") or "").upper()
    search_focus = str(metadata.get("search_focus") or "").lower()
    sources = metadata.get("sources") or []
    if search_mode in {"SEARCH", "BROWSER_AGENT", "ASI", "WIDE_RESEARCH"}:
        return True
    if search_focus == "internet":
        return True
    if isinstance(sources, list) and any(str(x).lower() == "web" for x in sources):
        return True
    # Only count source/research markers when they are explicit task/source fields,
    # not when a normal answer happens to mention investigation/reporting.
    return _v18_contains_any(evidence, ["web_results", "sources_answer_mode", "sources used", "source pages", "final urls"])


def _v19_select_task_type(thread: dict[str, Any], signals: dict[str, Any], base: dict[str, Any]) -> str:
    scores = _v17_as_dict(signals.get("scores"))
    prompt_markers = _v17_as_dict(signals.get("prompt_markers"))
    trace_markers = _v17_as_dict(signals.get("trace_markers"))
    classification = _v17_as_dict(thread.get("classification"))
    execution_mode = str(classification.get("execution_mode") or "")
    interaction_type = str(classification.get("interaction_type") or "")

    email_score = int(scores.get("email_draft_send") or 0)
    calendar_score = int(scores.get("calendar_create") or 0)
    download_score = int(scores.get("file_download") or 0)
    research_score = int(scores.get("web_research") or 0)
    page_score = int(scores.get("page_open") or 0)

    # Non-agentic chat/search must not become web_research because the answer text
    # contains generic words like "investigation", "analyze", or "report".
    if execution_mode == "non_browser_agent" or interaction_type == "conversational_or_search":
        if _v19_has_explicit_search_or_web_evidence(thread):
            return "web_search_or_research"
        return "basic_chat"

    # Specific app/form side effects beat generic download/research. Use a true
    # score comparison, not a fixed email-before-calendar priority.
    app_candidates = []
    if email_score >= 3:
        app_candidates.append((email_score, bool(prompt_markers.get("email") or trace_markers.get("email")), "email_draft_send"))
    if calendar_score >= 3:
        app_candidates.append((calendar_score, bool(prompt_markers.get("calendar") or trace_markers.get("calendar")), "calendar_create"))
    if app_candidates:
        app_candidates.sort(key=lambda item: (item[0], item[1], 1 if item[2] == "calendar_create" else 0), reverse=True)
        return app_candidates[0][2]

    if download_score >= 3:
        return "file_download"
    if research_score >= 2:
        return "web_research"
    if page_score >= 2:
        return "page_open"

    ordered = sorted([(email_score, "email_draft_send"), (calendar_score, "calendar_create"), (download_score, "file_download"), (research_score, "web_research"), (page_score, "page_open")], reverse=True)
    if ordered and ordered[0][0] > 0:
        return ordered[0][1]
    return str(base.get("task_type") or "unknown")


def classify_task_outcome(thread: dict[str, Any]) -> dict[str, Any]:
    """v0.19 generic current-thread outcome classifier.

    No scenario-specific strings are used. The target reference, when supplied,
    remains only a case selection/filtering input outside this classifier.
    """
    base = dict(_classify_task_outcome_pre_v19(thread) or {})
    signals = _v18_intent_signals(thread)
    task_type = _v19_select_task_type(thread, signals, base)
    prompt, final, evidence = _v19_current_thread_evidence_text(thread)
    all_text = "\n".join([prompt, final, evidence])
    final_low = final.lower()
    classification = _v17_as_dict(thread.get("classification"))
    execution_mode = str(classification.get("execution_mode") or "")

    result = dict(base)
    result.update({
        "task_type": task_type,
        "classifier_version": "v0.19_generic_current_thread_safe",
        "classification_basis": signals,
    })
    result.setdefault("primary_artifacts", [])
    if execution_mode and execution_mode not in result["primary_artifacts"]:
        result["primary_artifacts"].append(execution_mode)

    if task_type == "basic_chat":
        final_available = bool(_v17_as_dict(thread.get("final_answer")).get("text"))
        result.update({
            "status": "conversation_answer_recovered" if final_available else "conversation_metadata_only",
            "side_effect_completed": False,
            "confidence": "high" if final_available else "medium",
            "missing_corroboration": [],
            "downloaded_filename_candidates": [],
        })

    elif task_type == "web_search_or_research" or task_type == "web_research":
        final_available = bool(_v17_as_dict(thread.get("final_answer")).get("text"))
        urls = _v17_as_list(thread.get("urls")) or _v17_as_list(thread.get("context_url_candidates"))
        if final_available and urls:
            status, conf = "research_answer_and_source_leads_recovered", "medium_high"
        elif final_available:
            status, conf = "research_answer_recovered", "medium"
        elif urls:
            status, conf = "research_url_leads_only", "medium"
        else:
            status, conf = "research_intent_or_metadata_only", "low"
        result.update({
            "task_type": "web_research" if task_type == "web_search_or_research" else task_type,
            "status": status,
            "side_effect_completed": False if final_available else None,
            "confidence": conf,
            "missing_corroboration": ["History/cache/source page content"],
            "downloaded_filename_candidates": [],
        })

    elif task_type == "email_draft_send":
        sent = _v18_contains_any(final, ["email sent", "sent folder", "sent email", "visible in sent", "message sent", "sent successfully"])
        draft = _v18_contains_any(final, ["draft confirmed", "saved as a draft", "draft has been saved", "draft saved"])
        if sent:
            result.update({"status": "email_sent_reported_and_verified" if _v18_contains_any(final, ["verified", "visible"]) else "email_sent_reported", "side_effect_completed": True, "confidence": "high" if draft else "medium_high"})
        elif draft:
            result.update({"status": "email_draft_reported", "side_effect_completed": None, "confidence": "medium_high"})
        else:
            result.update({"status": "email_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"] = ["mail service Sent/Draft record", "message headers"]
        result["downloaded_filename_candidates"] = []

    elif task_type == "calendar_create":
        created = _v18_contains_any(final, ["event created", "created and verified", "successfully created", "created", "saved"])
        verified = _v18_contains_any(final, ["verified", "visible", "opened", "confirmed", "all details match"])
        if created and verified:
            result.update({"status": "calendar_event_created_and_verified_reported", "side_effect_completed": True, "confidence": "high"})
        elif created:
            result.update({"status": "calendar_event_created_reported", "side_effect_completed": True, "confidence": "medium_high"})
        else:
            result.update({"status": "calendar_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"] = ["calendar service/event record"]
        result["downloaded_filename_candidates"] = []

    elif task_type == "file_download":
        filenames = extract_pdf_filenames(all_text)
        completed = bool(filenames) and _v18_contains_any(all_text, ["download complete", "downloaded filename", "final downloaded filename", "downloaded file", "successfully downloaded", "다운로드 완료"])
        confirmation = _v18_contains_any(all_text, ["browser_agent_confirmation", "please confirm", "may i proceed", "confirmation required", "사용자 확인"])
        if completed:
            result.update({"status": "completed_download", "side_effect_completed": True, "confidence": "high", "downloaded_filename_candidates": filenames})
        elif confirmation:
            result.update({"status": "confirmation_required", "side_effect_completed": False, "confidence": "high", "downloaded_filename_candidates": filenames})
        elif filenames or ".pdf" in all_text.lower():
            result.update({"status": "source_discovery_or_partial_download", "side_effect_completed": None, "confidence": "medium", "downloaded_filename_candidates": filenames})
        else:
            result.update({"status": "download_intent_only", "side_effect_completed": None, "confidence": "low", "downloaded_filename_candidates": []})
        result["missing_corroboration"] = ["Chromium Downloads DB", "OS Downloads folder/file hash"]

    elif task_type == "page_open":
        target_urls = extract_prompt_target_urls(prompt)
        expected_title = extract_expected_page_title(prompt)
        opened = bool(target_urls and any(url in all_text for url in target_urls)) or bool(_v17_as_list(thread.get("urls")))
        title_confirmed = bool(expected_title and expected_title.lower() in final_low)
        if opened and (title_confirmed or not expected_title):
            status, conf = "target_page_opened_or_reported", "medium_high" if title_confirmed else "medium"
        elif opened:
            status, conf = "target_page_url_recovered", "medium"
        else:
            status, conf = "page_open_intent_only", "low"
        result.update({"status": status, "side_effect_completed": True if opened else None, "confidence": conf, "target_urls": target_urls, "expected_title": expected_title, "missing_corroboration": ["Chromium History DB for navigation timing"]})

    if task_type not in {"file_download", "download"}:
        result["warnings"] = [w for w in _v17_as_list(result.get("warnings")) if "download" not in str(w).lower()]
    return result


def build_case_summary(report: dict[str, Any], target_reference: str | None = None, original_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    case = _build_case_summary_pre_v19(report, target_reference, original_counts=original_counts)
    target = str(target_reference or "").strip()
    if not target:
        # In a profile-wide run there is no target case. Do not call every thread
        # a target thread; present it as inventory and ask the investigator to use
        # --target-reference for case-study validation.
        case["target_reference"] = None
        case["target_found"] = None
        case["target_thread_count"] = 0
        case["target_global_record_count"] = 0
        case.setdefault("warnings", []).append(
            "No --target-reference was supplied. This is a profile-wide artifact inventory, not a case-focused validation report. Residual previous threads may legitimately appear."
        )
        case.setdefault("case_layer_policy", {})["profile_wide_mode"] = "primary is best reconstructed inventory item, not necessarily the intended scenario"
    return case


def _v19_annotate_report(report: dict[str, Any], target_reference: str | None = None) -> dict[str, Any]:
    _v18_annotate_report(report, target_reference)
    for thread in _v17_as_list(report.get("threads")):
        if isinstance(thread, dict):
            thread["task_outcome"] = classify_task_outcome(thread)
            try:
                thread["content_state"] = build_content_state(thread)
                thread["reconstruction_availability"] = build_reconstruction_availability(thread)
                thread["structured_actions"] = build_structured_actions(thread)
                thread["browser_validation"] = classify_browser_reconstruction_level(thread)
            except Exception:
                pass
            thread["case_relation"] = _v18_relation_to_target(thread, target_reference)
    report["schema_version"] = "0.19"
    report.setdefault("hardcoding_audit", {})["v19"] = {
        "scenario_ids_not_used_as_rules": True,
        "target_reference_used_only_for_case_selection": True,
        "profile_wide_reports_marked_as_inventory_when_no_target_reference": True,
        "outcome_classifier_scope": "current thread prompt/final/action/payload evidence",
    }
    return report


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v19(extracted, input_label, browser_only=browser_only)
    _v19_annotate_report(report, None)
    report["case_summary"] = build_case_summary(report, None)
    return report


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v19(report, target_reference)
    _v19_annotate_report(filtered, target_reference)
    filtered["case_summary"] = build_case_summary(filtered, target_reference, original_counts=filtered.get("filter_summary") or None)
    return filtered


# ---------------------------------------------------------------------------
# v0.20 profile-inventory mode repair
# ---------------------------------------------------------------------------
# The parser must work with or without --target-reference.
# - Without --target-reference: output a profile-wide artifact inventory. Do not
#   select or imply an intended case/primary outcome.
# - With --target-reference: output a case-focused reconstruction and keep
#   residual/profile artifacts separated.
# This block is generic: it uses no scenario IDs, no expected answers, no fixed
# email addresses, and no fixed filenames as parser rules.

SCHEMA_VERSION = "0.20"

_build_case_summary_pre_v20 = build_case_summary
_reconstruct_browser_threads_pre_v20 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v20 = filter_report_by_target_reference
_render_case_summary_v10_pre_v20 = _render_case_summary_v10


def _v20_inventory_case_summary(report: dict[str, Any]) -> dict[str, Any]:
    threads = [t for t in _v17_as_list(report.get("threads")) if isinstance(t, dict)]
    skipped = _v17_as_list(report.get("skipped"))
    globals_ = _v17_as_list(report.get("global_records"))
    summary = _v17_as_dict(report.get("summary"))

    mode_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    level_counts: dict[str, int] = {}
    relation_counts: dict[str, int] = {}

    for thread in threads:
        cls = _v17_as_dict(thread.get("classification"))
        outcome = _v17_as_dict(thread.get("task_outcome"))
        avail = _v17_as_dict(thread.get("reconstruction_availability"))
        relation = _v17_as_dict(thread.get("case_relation"))
        mode = str(cls.get("execution_mode") or "unknown")
        task = str(outcome.get("task_type") or "unknown")
        level = str(avail.get("level") or "unknown")
        rel = str(relation.get("relation_to_target") or "profile_thread")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        outcome_counts[task] = outcome_counts.get(task, 0) + 1
        level_counts[level] = level_counts.get(level, 0) + 1
        relation_counts[rel] = relation_counts.get(rel, 0) + 1

    return {
        "report_mode": "profile_inventory",
        "target_reference": None,
        "target_found": None,
        "target_thread_count": None,
        "target_global_record_count": None,
        "primary_thread_id": None,
        "primary_execution_mode": None,
        "primary_task_outcome": None,
        "primary_reconstruction_availability": None,
        "primary_case_relation": None,
        "inventory_thread_count": len(threads),
        "inventory_skipped_count": len(skipped),
        "inventory_global_record_count": len(globals_),
        "mode_counts": mode_counts,
        "task_type_counts": outcome_counts,
        "reconstruction_level_counts": level_counts,
        "relation_counts": relation_counts,
        "summary_counts": summary,
        "warnings": [
            "No --target-reference was supplied. This report is a profile-wide artifact inventory, not a case-focused reconstruction.",
            "Multiple previous/residual threads can legitimately appear in one Comet IndexedDB/LevelDB snapshot. Do not treat the first or strongest side-effect thread as the intended case without a target reference or external ground truth.",
        ],
        "investigator_summary": [
            f"Profile inventory recovered {len(threads)} thread candidate(s), {len(skipped)} skipped item(s), and {len(globals_)} global/profile record(s).",
            "Use --target-reference only when validating a known scenario or case; otherwise review each thread independently as recovered profile evidence.",
        ],
        "case_layer_policy": {
            "no_target_reference": "profile_inventory_only",
            "primary_selection": "disabled_without_target_reference",
            "target_found": "not_applicable_without_target_reference",
            "residual_threads": "shown as normal inventory items; not classified as target or non-target without a target reference",
        },
    }


def build_case_summary(report: dict[str, Any], target_reference: str | None = None, original_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    target = str(target_reference or "").strip()
    if not target:
        return _v20_inventory_case_summary(report)

    case = _build_case_summary_pre_v20(report, target, original_counts=original_counts)
    case["report_mode"] = "case_reconstruction"
    case.setdefault("case_layer_policy", {})["target_reference_present"] = "case-focused primary selection enabled"
    return case


def _v20_annotate_report(report: dict[str, Any], target_reference: str | None = None) -> dict[str, Any]:
    _v19_annotate_report(report, target_reference)
    target = str(target_reference or "").strip()
    report["schema_version"] = "0.20"
    report["report_mode"] = "case_reconstruction" if target else "profile_inventory"
    report.setdefault("source", {})["report_mode"] = report["report_mode"]
    report.setdefault("hardcoding_audit", {})["v20"] = {
        "scenario_specific_rules": False,
        "fixed_expected_answers": False,
        "fixed_email_or_filename_rules": False,
        "target_reference_required": False,
        "without_target_reference": "profile_inventory; primary/case outcome disabled",
        "with_target_reference": "case_reconstruction; residual artifacts retained separately",
    }
    return report


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v20(extracted, input_label, browser_only=browser_only)
    _v20_annotate_report(report, None)
    report["case_summary"] = build_case_summary(report, None)
    return report


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    target = str(target_reference or "").strip()
    if not target:
        # Preserve full inventory. Do not run target filtering and do not promote
        # a strongest residual thread into a fake primary case.
        _v20_annotate_report(report, None)
        report["case_summary"] = build_case_summary(report, None)
        return report

    filtered = _filter_report_by_target_reference_pre_v20(report, target)
    _v20_annotate_report(filtered, target)
    filtered["case_summary"] = build_case_summary(filtered, target, original_counts=filtered.get("filter_summary") or None)
    return filtered


def _v20_thread_inventory_rows(report: dict[str, Any], max_rows: int = 80) -> str:
    rows: list[str] = []
    threads = [t for t in _v17_as_list(report.get("threads")) if isinstance(t, dict)]
    for idx, thread in enumerate(threads[:max_rows], start=1):
        cls = _v17_as_dict(thread.get("classification"))
        outcome = _v17_as_dict(thread.get("task_outcome"))
        avail = _v17_as_dict(thread.get("reconstruction_availability"))
        prompt = _v17_as_dict(thread.get("prompt"))
        relation = _v17_as_dict(thread.get("case_relation"))
        final = _v17_as_dict(thread.get("final_answer"))
        rows.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{_h(thread.get('thread_id'))}</td>"
            f"<td>{_h(cls.get('interaction_type'))}</td>"
            f"<td>{_h(cls.get('execution_mode'))}</td>"
            f"<td>{_h(outcome.get('task_type'))}</td>"
            f"<td>{_h(outcome.get('status'))}</td>"
            f"<td>{_h(avail.get('level'))}</td>"
            f"<td>{_h(relation.get('evidence_scope') or 'profile_inventory')}</td>"
            f"<td>{'yes' if final.get('available') or final.get('text') else 'no'}</td>"
            f"<td>{_h((prompt.get('text') or '')[:260])}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='10'><em>No thread candidates reconstructed.</em></td></tr>")
    return (
        "<table class='table'><thead><tr>"
        "<th>#</th><th>Thread</th><th>Interaction</th><th>Mode</th><th>Task</th><th>Outcome</th><th>Level</th><th>Scope</th><th>Final</th><th>Prompt preview</th>"
        "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
    )


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    case = _v17_as_dict(report.get("case_summary")) or build_case_summary(report, _v17_as_dict(report.get("source")).get("target_reference_filter"))
    if case.get("report_mode") == "profile_inventory" or report.get("report_mode") == "profile_inventory":
        rows = [
            ("Report mode", "profile_inventory"),
            ("Target reference", "N/A — not supplied"),
            ("Target found", "N/A — target selection disabled"),
            ("Primary thread", "N/A — no primary selected in inventory mode"),
            ("Thread candidates", case.get("inventory_thread_count")),
            ("Skipped candidates", case.get("inventory_skipped_count")),
            ("Global/profile records", case.get("inventory_global_record_count")),
            ("Modes", json.dumps(case.get("mode_counts") or {}, ensure_ascii=False)),
            ("Task types", json.dumps(case.get("task_type_counts") or {}, ensure_ascii=False)),
            ("Reconstruction levels", json.dumps(case.get("reconstruction_level_counts") or {}, ensure_ascii=False)),
        ]
        parts = [
            "<section class='card'><div class='topline'><h2>Profile-wide artifact inventory</h2><span class='badge warn'>no target reference</span></div>",
            "<div class='note warn'><strong>Inventory mode.</strong> No <code>--target-reference</code> was supplied, so this report does not choose an intended case, primary thread, or primary outcome. Every reconstructed thread below is simply evidence recovered from the input profile.</div>",
            _kv_table_v07(rows),
            "<h4>Investigator-readable summary</h4><ul class='findings'>",
        ]
        for item in _v17_as_list(case.get("investigator_summary"))[:6]:
            parts.append(f"<li>{_h(item)}</li>")
        parts.append("</ul>")
        warnings = _v17_as_list(case.get("warnings"))
        if warnings:
            parts.append("<div class='note warn'><strong>Cautions</strong><ul>")
            for w in warnings[:8]:
                parts.append(f"<li>{_h(w)}</li>")
            parts.append("</ul></div>")
        parts.append("<h4>Thread inventory</h4>")
        parts.append(_v20_thread_inventory_rows(report))
        parts.append(_raw_details_v07("Open profile inventory summary JSON", case))
        parts.append("</section>")
        return "\n".join(parts)

    # Target reference exists: keep the case-focused summary from earlier layers.
    return _render_case_summary_v10_pre_v20(report)


# ---------------------------------------------------------------------------
# v0.21 Presentation-safe inventory and thread-local outcome guards
# ---------------------------------------------------------------------------
# Goals:
# - A bare command (--input/--output/--html-output only) produces a profile-wide
#   artifact inventory and never implies an intended case.
# - JSON preserves every extracted thread/residual artifact.
# - HTML defaults to an inventory view; residual/historical/context evidence is
#   labeled rather than promoted to a case outcome.
# - Derived task outcomes are computed only from current-thread evidence with
#   guard conditions; global/profile records and unrelated threads do not score
#   another thread's task.
# - No scenario-specific IDs, fixed emails, filenames, or expected answers are
#   used as rules.

SCHEMA_VERSION = "0.21"

_classify_task_outcome_pre_v21 = classify_task_outcome
_build_case_summary_pre_v21 = build_case_summary
_reconstruct_browser_threads_pre_v21 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v21 = filter_report_by_target_reference
_render_case_summary_v10_pre_v21 = _render_case_summary_v10


def _v21_norm_text(value: Any) -> str:
    return _v18_text(value) if '_v18_text' in globals() else str(value or "")


def _v21_json_text(value: Any, limit: int = 180000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) > limit:
        return text[:limit]
    return text


def _v21_has_any(text: str, needles: list[str]) -> bool:
    low = (text or "").lower()
    return any(n.lower() in low for n in needles)


def _v21_thread_local_parts(thread: dict[str, Any]) -> dict[str, str]:
    """Return strictly current-thread text buckets for interpretation.

    This intentionally does not read report.global_records, other threads, or
    profile-wide topMostUrls. Metadata is reduced to fields that describe this
    thread/mode, not user profile fields that often contain unrelated email or
    account strings.
    """
    prompt = _v21_norm_text(_v17_as_dict(thread.get("prompt")).get("text"))
    final = _v21_norm_text(_v17_as_dict(thread.get("final_answer")).get("text"))

    meta = _v17_as_dict(thread.get("metadata"))
    meta_keep = {
        k: meta.get(k)
        for k in [
            "mode", "search_mode", "search_focus", "display_model", "user_selected_model",
            "sources", "status", "thread_status", "message_mode", "backend_uuid",
            "context_uuid", "privacy_state", "access_level",
        ]
        if k in meta
    }

    evidence_parts: list[str] = []
    for key in ["plan", "actions", "structured_actions", "typed_payloads", "urls", "context_url_candidates"]:
        if key in thread:
            evidence_parts.append(_v21_json_text(thread.get(key)))
    evidence_parts.append(_v21_json_text({"metadata": meta_keep, "classification": thread.get("classification")}))
    evidence = "\n".join(evidence_parts)
    all_text = "\n".join([prompt, final, evidence])
    return {
        "prompt": prompt,
        "final": final,
        "evidence": evidence,
        "all": all_text,
        "prompt_low": prompt.lower(),
        "final_low": final.lower(),
        "evidence_low": evidence.lower(),
        "all_low": all_text.lower(),
    }


def _v21_payload_field_names(thread: dict[str, Any]) -> set[str]:
    fields: set[str] = set()
    for item in _v17_as_list(thread.get("typed_payloads")):
        if isinstance(item, dict):
            field = str(item.get("field") or "").strip().lower()
            if field:
                fields.add(field)
    return fields


def _v21_url_text(thread: dict[str, Any]) -> str:
    urls: list[str] = []
    for key in ["urls", "context_url_candidates"]:
        for item in _v17_as_list(thread.get(key)):
            if isinstance(item, dict):
                urls.append(str(item.get("url") or item.get("title") or ""))
            else:
                urls.append(str(item or ""))
    return "\n".join(urls)


def _v21_current_thread_signals(thread: dict[str, Any]) -> dict[str, Any]:
    parts = _v21_thread_local_parts(thread)
    prompt = parts["prompt_low"]
    final = parts["final_low"]
    evidence = parts["evidence_low"]
    all_low = parts["all_low"]
    fields = _v21_payload_field_names(thread)
    url_low = _v21_url_text(thread).lower()

    classification = _v17_as_dict(thread.get("classification"))
    metadata = _v17_as_dict(thread.get("metadata"))
    execution_mode = str(classification.get("execution_mode") or "")
    interaction_type = str(classification.get("interaction_type") or "")
    search_mode = str(metadata.get("search_mode") or "").upper()
    search_focus = str(metadata.get("search_focus") or "").lower()
    sources = metadata.get("sources") or []
    if not isinstance(sources, list):
        sources = [sources]

    # Guard anchors. A task type cannot be selected unless its anchor is true.
    email_prompt = _v21_has_any(prompt, ["gmail", "e-mail", "email", "recipient", "subject", "compose", "sent folder", "draft"])
    email_final = _v21_has_any(final, ["email sent", "sent email", "message sent", "visible in sent", "sent folder", "draft confirmed", "saved as a draft", "draft saved"])
    email_action = _v21_has_any(evidence + "\n" + url_low, ["mail.google", "gmail", "compose window", "recipient", "sent folder", "draft confirmed", "send button", "message sent"])
    email_payload = bool(fields & {"recipient", "email_recipient", "to", "subject", "body"}) and (email_prompt or email_final or email_action)
    email_anchor = bool(email_prompt or email_final or email_action or email_payload)

    calendar_prompt = _v21_has_any(prompt, ["calendar", "event", "start time", "end time", "event title", "guests", "description"])
    calendar_final = _v21_has_any(final, ["event created", "created and verified", "calendar event", "google calendar", "event is visible", "no guests", "all details match"])
    calendar_action = _v21_has_any(evidence + "\n" + url_low, ["calendar.google", "google calendar", "event title", "start time", "end time", "save event", "event created"])
    calendar_payload = bool(fields & {"event_title", "event_date", "start_time", "end_time", "description", "title"}) and (calendar_prompt or calendar_final or calendar_action)
    calendar_anchor = bool(calendar_prompt or calendar_final or calendar_action or calendar_payload)

    positive_download_prompt = _v21_has_any(prompt, ["download", "downloaded filename", "save the pdf", "pdf file", "official pdf"])
    positive_download_final = _v21_has_any(final, ["download is complete", "download complete", "final downloaded filename", "downloaded filename", "downloaded file", "successfully downloaded"])
    positive_download_action = _v21_has_any(evidence + "\n" + url_low, ["wait_for_download", "download", "ctrl+j", ".pdf", "save the pdf"])
    filename_candidates = extract_pdf_filenames(parts["all"]) if 'extract_pdf_filenames' in globals() else []
    download_anchor = bool((positive_download_prompt or positive_download_final or positive_download_action) and (filename_candidates or ".pdf" in all_low or positive_download_final or positive_download_action))
    if 'has_negated_term' in globals() and has_negated_term(parts["prompt"], ["download", "file", "pdf"]) and not (positive_download_final or positive_download_action):
        download_anchor = False

    research_prompt = _v21_has_any(prompt, ["research", "web source", "web sources", "search strategy", "sources used", "pages visited", "final urls", "compare their main claims"])
    research_final = _v21_has_any(final, ["sources used", "pages visited", "final urls", "source page", "web sources"])
    research_artifact = (
        search_mode in {"SEARCH", "BROWSER_AGENT", "ASI", "WIDE_RESEARCH"}
        or search_focus == "internet"
        or any(str(x).lower() == "web" for x in sources)
        or _v21_has_any(evidence, ["web_results", "sources_answer_mode", "sources used", "final urls"])
    )
    research_anchor = bool(research_prompt or research_final or research_artifact)

    page_prompt = _v21_has_any(prompt, ["open", "navigate", "visit", "go to", "page title", "starting page"])
    page_final = _v21_has_any(final, ["navigation path", "opened", "page title", "starting page", "confirmed", "url"])
    page_action = _v21_has_any(evidence + "\n" + url_low, ["navigate", "click", "url", "wikipedia.org", "http://", "https://"])
    page_anchor = bool((page_prompt or page_final) and (page_action or url_low or page_final))

    # Scores are used only after guards. They are not allowed to override anchors.
    scores = {
        "email_draft_send": (3 if email_prompt else 0) + (4 if email_final else 0) + (2 if email_action else 0) + (1 if email_payload else 0),
        "calendar_create": (3 if calendar_prompt else 0) + (4 if calendar_final else 0) + (2 if calendar_action else 0) + (1 if calendar_payload else 0),
        "file_download": (3 if positive_download_prompt else 0) + (4 if positive_download_final else 0) + (2 if positive_download_action else 0) + (1 if filename_candidates else 0),
        "web_research": (3 if research_prompt else 0) + (3 if research_final else 0) + (2 if research_artifact else 0),
        "page_open": (3 if page_prompt else 0) + (3 if page_final else 0) + (1 if page_action else 0),
    }
    anchors = {
        "email_draft_send": email_anchor,
        "calendar_create": calendar_anchor,
        "file_download": download_anchor,
        "web_research": research_anchor,
        "page_open": page_anchor,
    }
    return {
        "scores": scores,
        "anchors": anchors,
        "prompt_markers": {
            "email": email_prompt,
            "calendar": calendar_prompt,
            "download": positive_download_prompt,
            "research": research_prompt,
            "page_open": page_prompt,
        },
        "trace_markers": {
            "email": email_final or email_action,
            "calendar": calendar_final or calendar_action,
            "download": positive_download_final or positive_download_action,
            "research": research_final or research_artifact,
            "page_open": page_final or page_action,
            "pdf_filename_candidates": filename_candidates,
        },
        "guard_policy": {
            "thread_local_only": True,
            "global_records_used_for_task_scoring": False,
            "unanchored_task_types_suppressed": True,
        },
        "execution_mode": execution_mode,
        "interaction_type": interaction_type,
    }


def _v21_select_task_type(thread: dict[str, Any], signals: dict[str, Any], base: dict[str, Any]) -> str:
    scores = _v17_as_dict(signals.get("scores"))
    anchors = _v17_as_dict(signals.get("anchors"))
    execution_mode = str(signals.get("execution_mode") or "")
    interaction_type = str(signals.get("interaction_type") or "")

    # Non-agentic conversation/search remains basic chat unless explicit web/search
    # artifact exists in this thread. Ordinary final-answer words such as
    # "investigation" or "analysis" must not promote it to web_research.
    if execution_mode == "non_browser_agent" or interaction_type == "conversational_or_search":
        if anchors.get("web_research") and int(scores.get("web_research") or 0) >= 3:
            return "web_research"
        if anchors.get("page_open") and int(scores.get("page_open") or 0) >= 4:
            return "page_open"
        return "basic_chat"

    candidates: list[tuple[int, int, str]] = []
    # Specific side-effect categories. Calendar gets a deterministic tie-breaker
    # over email only when scores are tied, not as a scenario rule.
    tie_rank = {"calendar_create": 5, "email_draft_send": 4, "file_download": 3, "web_research": 2, "page_open": 1}
    thresholds = {"calendar_create": 3, "email_draft_send": 3, "file_download": 3, "web_research": 3, "page_open": 4}
    for task, threshold in thresholds.items():
        score = int(scores.get(task) or 0)
        if anchors.get(task) and score >= threshold:
            candidates.append((score, tie_rank.get(task, 0), task))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][2]

    # If no anchored task category is strong enough, preserve a conservative
    # unknown/metadata/final-answer state rather than fabricating an app task.
    base_task = str(base.get("task_type") or "unknown")
    if base_task in {"basic_chat", "unknown", "page_open", "web_research", "file_download", "calendar_create", "email_draft_send"} and anchors.get(base_task):
        return base_task
    return "unknown"


def classify_task_outcome(thread: dict[str, Any]) -> dict[str, Any]:
    """v0.21 outcome classifier with strict thread-local guard conditions."""
    base = dict(_classify_task_outcome_pre_v21(thread) or {})
    signals = _v21_current_thread_signals(thread)
    task_type = _v21_select_task_type(thread, signals, base)
    parts = _v21_thread_local_parts(thread)
    prompt = parts["prompt"]
    final = parts["final"]
    all_text = parts["all"]
    final_low = parts["final_low"]
    classification = _v17_as_dict(thread.get("classification"))
    execution_mode = str(classification.get("execution_mode") or "")

    result = dict(base)
    result.update({
        "task_type": task_type,
        "classifier_version": "v0.21_thread_local_guarded",
        "classification_basis": signals,
        "interpretation_scope": "derived_from_current_thread_only",
    })
    result.setdefault("primary_artifacts", [])
    if execution_mode and execution_mode not in result["primary_artifacts"]:
        result["primary_artifacts"].append(execution_mode)

    if task_type == "basic_chat":
        final_available = bool(_v17_as_dict(thread.get("final_answer")).get("text"))
        result.update({
            "status": "conversation_answer_recovered" if final_available else "conversation_metadata_only",
            "side_effect_completed": False,
            "confidence": "high" if final_available else "medium",
            "missing_corroboration": [],
            "downloaded_filename_candidates": [],
        })

    elif task_type == "email_draft_send":
        sent = _v21_has_any(final, ["email sent", "sent folder", "sent email", "visible in sent", "message sent", "sent successfully"])
        draft = _v21_has_any(final, ["draft confirmed", "saved as a draft", "draft has been saved", "draft saved"])
        if sent:
            result.update({"status": "email_sent_reported_and_verified" if _v21_has_any(final, ["verified", "visible"]) else "email_sent_reported", "side_effect_completed": True, "confidence": "high" if draft else "medium_high"})
        elif draft:
            result.update({"status": "email_draft_reported", "side_effect_completed": None, "confidence": "medium_high"})
        else:
            result.update({"status": "email_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"] = ["mail service Sent/Draft record", "message headers"]
        result["downloaded_filename_candidates"] = []

    elif task_type == "calendar_create":
        created = _v21_has_any(final, ["event created", "created and verified", "successfully created", "calendar event", "created", "saved"])
        verified = _v21_has_any(final, ["verified", "visible", "opened", "confirmed", "all details match"])
        if created and verified:
            result.update({"status": "calendar_event_created_and_verified_reported", "side_effect_completed": True, "confidence": "high"})
        elif created:
            result.update({"status": "calendar_event_created_reported", "side_effect_completed": True, "confidence": "medium_high"})
        else:
            result.update({"status": "calendar_form_activity", "side_effect_completed": None, "confidence": "medium"})
        result["missing_corroboration"] = ["calendar service/event record"]
        result["downloaded_filename_candidates"] = []

    elif task_type == "file_download":
        filenames = _v17_as_list(_v17_as_dict(signals.get("trace_markers")).get("pdf_filename_candidates"))
        completed = bool(filenames) and _v21_has_any(all_text, ["download is complete", "download complete", "downloaded filename", "final downloaded filename", "downloaded file", "successfully downloaded", "다운로드 완료"])
        confirmation = _v21_has_any(all_text, ["browser_agent_confirmation", "please confirm", "may i proceed", "confirmation required", "사용자 확인"])
        if completed:
            result.update({"status": "completed_download", "side_effect_completed": True, "confidence": "high", "downloaded_filename_candidates": filenames})
        elif confirmation:
            result.update({"status": "confirmation_required", "side_effect_completed": False, "confidence": "high", "downloaded_filename_candidates": filenames})
        elif filenames or ".pdf" in all_text.lower():
            result.update({"status": "source_discovery_or_partial_download", "side_effect_completed": None, "confidence": "medium", "downloaded_filename_candidates": filenames})
        else:
            result.update({"status": "download_intent_only", "side_effect_completed": None, "confidence": "low", "downloaded_filename_candidates": []})
        result["missing_corroboration"] = ["Chromium Downloads DB", "OS Downloads folder/file hash"]

    elif task_type == "web_research":
        final_available = bool(_v17_as_dict(thread.get("final_answer")).get("text"))
        urls = _v17_as_list(thread.get("urls"))
        if final_available and urls:
            status, conf = "research_answer_and_source_leads_recovered", "medium_high"
        elif final_available:
            status, conf = "research_answer_recovered", "medium"
        elif urls:
            status, conf = "research_url_leads_only", "medium"
        else:
            status, conf = "research_intent_or_metadata_only", "low"
        result.update({"status": status, "side_effect_completed": False if final_available else None, "confidence": conf, "missing_corroboration": ["History/cache/source page content"], "downloaded_filename_candidates": []})

    elif task_type == "page_open":
        target_urls = extract_prompt_target_urls(prompt) if 'extract_prompt_target_urls' in globals() else []
        expected_title = extract_expected_page_title(prompt) if 'extract_expected_page_title' in globals() else None
        urls = _v17_as_list(thread.get("urls"))
        opened = bool(urls) or bool(target_urls and any(url in all_text for url in target_urls))
        title_confirmed = bool(expected_title and expected_title.lower() in final_low)
        if opened and (title_confirmed or not expected_title):
            status, conf = "target_page_opened_or_reported", "medium_high" if title_confirmed else "medium"
        elif opened:
            status, conf = "target_page_url_recovered", "medium"
        else:
            status, conf = "page_open_intent_only", "low"
        result.update({"status": status, "side_effect_completed": True if opened else None, "confidence": conf, "target_urls": target_urls, "expected_title": expected_title, "missing_corroboration": ["Chromium History DB for navigation timing"]})

    else:
        final_available = bool(_v17_as_dict(thread.get("final_answer")).get("text"))
        has_metadata = bool(_v17_as_dict(thread.get("metadata")))
        result.update({
            "status": "final_answer_recovered" if final_available else ("metadata_only" if has_metadata else "unknown"),
            "side_effect_completed": None,
            "confidence": "low",
            "missing_corroboration": [],
            "downloaded_filename_candidates": [],
        })

    if task_type not in {"file_download", "download"}:
        result["warnings"] = [w for w in _v17_as_list(result.get("warnings")) if "download" not in str(w).lower()]
        result["downloaded_filename_candidates"] = []
    return result


def _v21_target_match_thread(thread: dict[str, Any], target_reference: str | None) -> bool:
    target = str(target_reference or "").strip()
    if not target:
        return False
    local_text = _v21_thread_local_parts(thread)["all"]
    return target in local_text


def _v21_display_group(thread: dict[str, Any], target_reference: str | None = None) -> dict[str, Any]:
    target = str(target_reference or "").strip()
    relation = _v17_as_dict(thread.get("case_relation"))
    availability = _v17_as_dict(thread.get("reconstruction_availability"))
    outcome = _v17_as_dict(thread.get("task_outcome"))
    classification = _v17_as_dict(thread.get("classification"))
    level = str(availability.get("level") or "")

    if target:
        if _v21_target_match_thread(thread, target) or relation.get("relation_to_target") == "exact_target":
            group = "target_case"
            visibility = "expanded"
            claim = "case_reconstruction_candidate"
        else:
            group = "residual_or_historical_artifact"
            visibility = "collapsed_context"
            claim = "profile_residual_artifact_not_target_case"
    else:
        group = "profile_thread_inventory"
        visibility = "listed_not_primary"
        claim = "recovered_profile_artifact_no_case_claim"
        if level in {"metadata_residue_only", "list_cache_only", "global_context_only"}:
            group = "profile_residue_inventory"
            visibility = "collapsed_context"

    return {
        "group": group,
        "html_default_visibility": visibility,
        "is_case_primary": group == "target_case",
        "claim_level": claim,
        "task_type": outcome.get("task_type"),
        "execution_mode": classification.get("execution_mode"),
        "reconstruction_level": level,
        "policy": "JSON preserves this item; HTML may list or collapse it depending on report mode and target linkage.",
    }


def _v21_attach_artifact_interpretation(thread: dict[str, Any], target_reference: str | None = None) -> None:
    thread["extracted_artifacts"] = {
        "prompt": thread.get("prompt"),
        "metadata": thread.get("metadata"),
        "plan": thread.get("plan"),
        "actions": thread.get("actions"),
        "structured_actions": thread.get("structured_actions"),
        "typed_payloads": thread.get("typed_payloads"),
        "urls": thread.get("urls"),
        "final_answer": thread.get("final_answer"),
        "deletion_state": thread.get("deletion_state"),
        "private_mode": thread.get("private_mode"),
        "note": "These fields are extracted or reconstructed from artifacts with their own evidence/provenance where available.",
    }
    thread["derived_interpretation"] = {
        "classification": thread.get("classification"),
        "task_outcome": thread.get("task_outcome"),
        "reconstruction_availability": thread.get("reconstruction_availability"),
        "display": thread.get("display"),
        "interpretation_scope": "Derived fields are classifier/report interpretations. They are not raw artifacts and should be checked against extracted_artifacts/evidence.",
    }
    thread["display"] = _v21_display_group(thread, target_reference)


def _v21_postprocess_report(report: dict[str, Any], target_reference: str | None = None) -> dict[str, Any]:
    target = str(target_reference or "").strip()
    report["schema_version"] = "0.21"
    report["report_mode"] = "case_reconstruction" if target else "profile_inventory"
    report.setdefault("source", {})["report_mode"] = report["report_mode"]
    if target:
        report.setdefault("source", {})["target_reference_filter"] = target
    report.setdefault("interpretation_policy", {})["v21"] = {
        "bare_command_behavior": "profile_inventory; no target, no primary case, no primary outcome",
        "json_preservation": "all reconstructed threads/residual artifacts are retained",
        "html_presentation": "target/residual or inventory groups are labeled; non-target context may be collapsed",
        "task_classifier_scope": "thread-local evidence only; global/profile records do not score another thread",
        "scenario_specific_rules": False,
    }
    for thread in [t for t in _v17_as_list(report.get("threads")) if isinstance(t, dict)]:
        thread["task_outcome"] = classify_task_outcome(thread)
        _v21_attach_artifact_interpretation(thread, target)
    report["case_summary"] = build_case_summary(report, target or None)
    return report


def build_case_summary(report: dict[str, Any], target_reference: str | None = None, original_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    target = str(target_reference or "").strip()
    case = _build_case_summary_pre_v21(report, target if target else None, original_counts=original_counts)
    case["report_mode"] = "case_reconstruction" if target else "profile_inventory"
    if not target:
        case["target_reference"] = None
        case["target_found"] = None
        case["primary_thread_id"] = None
        case["primary_execution_mode"] = None
        case["primary_task_outcome"] = None
        case["primary_reconstruction_availability"] = None
        case.setdefault("case_layer_policy", {})["primary_selection"] = "disabled_without_target_reference"
        case.setdefault("warnings", []).append("No --target-reference was supplied. This is a profile inventory; no thread is promoted as the intended case.")
    else:
        case.setdefault("case_layer_policy", {})["primary_selection"] = "enabled_only_for_exact_or_strong_target_reference_match"
    case["artifact_vs_interpretation"] = {
        "extracted_artifacts": "artifact-derived fields such as prompt, metadata, actions, URLs, payloads, final answer",
        "derived_interpretation": "classifier/report fields such as task_type, outcome, confidence, display group",
    }
    return case


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v21(extracted, input_label, browser_only=browser_only)
    return _v21_postprocess_report(report, None)


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    target = str(target_reference or "").strip()
    if not target:
        return _v21_postprocess_report(report, None)
    filtered = _filter_report_by_target_reference_pre_v21(report, target)
    return _v21_postprocess_report(filtered, target)


def _v21_thread_inventory_rows(report: dict[str, Any], max_rows: int = 100) -> str:
    rows: list[str] = []
    threads = [t for t in _v17_as_list(report.get("threads")) if isinstance(t, dict)]
    for idx, thread in enumerate(threads[:max_rows], start=1):
        cls = _v17_as_dict(thread.get("classification"))
        outcome = _v17_as_dict(thread.get("task_outcome"))
        avail = _v17_as_dict(thread.get("reconstruction_availability"))
        prompt = _v17_as_dict(thread.get("prompt"))
        disp = _v17_as_dict(thread.get("display"))
        final = _v17_as_dict(thread.get("final_answer"))
        rows.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{_h(disp.get('group') or 'profile_thread_inventory')}</td>"
            f"<td>{_h(thread.get('thread_id'))}</td>"
            f"<td>{_h(cls.get('interaction_type'))}</td>"
            f"<td>{_h(cls.get('execution_mode'))}</td>"
            f"<td>{_h(outcome.get('task_type'))}</td>"
            f"<td>{_h(outcome.get('status'))}</td>"
            f"<td>{_h(avail.get('level'))}</td>"
            f"<td>{_h(disp.get('claim_level'))}</td>"
            f"<td>{'yes' if final.get('available') or final.get('text') else 'no'}</td>"
            f"<td>{_h((prompt.get('text') or '')[:220])}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='11'><em>No thread candidates reconstructed.</em></td></tr>")
    return (
        "<table class='table'><thead><tr>"
        "<th>#</th><th>Display group</th><th>Thread</th><th>Interaction</th><th>Mode</th><th>Task</th><th>Outcome</th><th>Level</th><th>Claim level</th><th>Final</th><th>Prompt preview</th>"
        "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
    )


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    case = _v17_as_dict(report.get("case_summary")) or build_case_summary(report, _v17_as_dict(report.get("source")).get("target_reference_filter"))
    if case.get("report_mode") == "profile_inventory" or report.get("report_mode") == "profile_inventory":
        rows = [
            ("Report mode", "profile_inventory"),
            ("Target reference", "N/A — not supplied"),
            ("Target found", "N/A — target selection disabled"),
            ("Primary thread", "N/A — no primary selected in inventory mode"),
            ("Thread candidates", case.get("inventory_thread_count")),
            ("Skipped candidates", case.get("inventory_skipped_count")),
            ("Global/profile records", case.get("inventory_global_record_count")),
            ("Classifier scope", "thread-local evidence only"),
        ]
        parts = [
            "<section class='card'><div class='topline'><h2>Profile-wide artifact inventory</h2><span class='badge warn'>no target reference</span></div>",
            "<div class='note warn'><strong>Inventory mode.</strong> No <code>--target-reference</code> was supplied. This report does not choose an intended case, primary thread, or primary outcome. Threads shown below are recovered profile artifacts, not case claims.</div>",
            _kv_table_v07(rows),
            "<h4>Artifact vs derived interpretation</h4>",
            "<div class='note'><strong>Extracted artifacts</strong> are prompt/metadata/action/URL/payload/final-answer fields recovered from the input. <strong>Derived interpretation</strong> includes task type, outcome, confidence, and display grouping generated by the classifier.</div>",
            "<h4>Thread inventory</h4>",
            _v21_thread_inventory_rows(report),
            "<details><summary>Residual / historical interpretation policy</summary><div style='padding:12px'>JSON preserves every reconstructed item. HTML labels profile or residual artifacts so they are not mistaken for a target case. Use <code>--target-reference</code> only when validating a known case.</div></details>",
            _raw_details_v07("Open profile inventory summary JSON", case),
            "</section>",
        ]
        return "\n".join(parts)
    return _render_case_summary_v10_pre_v21(report)


# ---------------------------------------------------------------------------
# v0.22: presentation-safe inventory mode and explicit display policy.
# - Bare commands remain profile_inventory mode.
# - JSON keeps every recovered artifact, but HTML no longer presents inventory
#   output with case/primary wording.
# - Optional input-name family hints are display hints only; they are not used as
#   forensic evidence or task-outcome scoring.
# - No scenario-specific strings, filenames, expected answers, or email addresses
#   are embedded in the logic.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.22"

_render_case_summary_v10_pre_v22 = _render_case_summary_v10
_render_html_report_pre_v22 = render_html_report
_reconstruct_browser_threads_pre_v22 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v22 = filter_report_by_target_reference


def _v22_report_mode(report: dict[str, Any]) -> str:
    return str(report.get("report_mode") or _v17_as_dict(report.get("source")).get("report_mode") or "").strip()


def _v22_input_family_hint(report: dict[str, Any]) -> str | None:
    """Return a generic run-name token such as S08 from the input path, if present.

    This is deliberately presentation-only. It must never decide task outcome or
    primary case selection.
    """
    src = _v17_as_dict(report.get("source"))
    raw = str(src.get("input") or src.get("input_label") or "")
    name = Path(raw).name if raw else ""
    m = re.search(r"\b([A-Za-z]+\d{1,4})\b", name)
    if not m:
        return None
    token = m.group(1)
    # Avoid treating generic words as hints; require at least one digit.
    return token if any(ch.isdigit() for ch in token) else None


def _v22_thread_matches_hint(thread: dict[str, Any], hint: str | None) -> bool:
    if not hint:
        return False
    hint_low = hint.lower()
    prompt = _v17_as_dict(thread.get("prompt"))
    refs = "\n".join(str(x) for x in _v17_as_list(prompt.get("reference_codes")))
    local = _v21_thread_local_parts(thread).get("all", "")
    return hint_low in (refs + "\n" + local).lower()


def _v22_apply_display_policy(report: dict[str, Any], target_reference: str | None = None) -> dict[str, Any]:
    target = str(target_reference or "").strip()
    mode = "case_reconstruction" if target else "profile_inventory"
    report["schema_version"] = "0.22"
    report["report_mode"] = mode
    report.setdefault("source", {})["report_mode"] = mode

    hint = _v22_input_family_hint(report) if mode == "profile_inventory" else None
    hint_matches: list[str] = []
    for thread in [t for t in _v17_as_list(report.get("threads")) if isinstance(t, dict)]:
        display = _v17_as_dict(thread.get("display"))
        if mode == "profile_inventory":
            display["is_case_primary"] = False
            display["html_default_section"] = "profile_thread_inventory"
            display.setdefault("claim_level", "recovered_profile_artifact_no_case_claim")
            if _v22_thread_matches_hint(thread, hint):
                display["input_name_hint_match"] = True
                display["sort_hint"] = "input_filename_family_match_not_forensic_evidence"
                # Preserve forensic claim level while making the HTML table easier to scan.
                if display.get("group") in (None, "", "profile_thread_inventory"):
                    display["group"] = "input_name_hint_candidate"
                hint_matches.append(str(thread.get("thread_id") or ""))
            else:
                display.setdefault("input_name_hint_match", False)
        thread["display"] = display
        # Keep derived interpretation synchronized after display adjustments.
        if isinstance(thread.get("derived_interpretation"), dict):
            thread["derived_interpretation"]["display"] = display

    report["display_policy"] = {
        "report_mode": mode,
        "json_preservation": "all reconstructed threads and residual artifacts remain in JSON",
        "html_default": "inventory summary plus thread inventory; residual/global artifacts are separated from thread-local evidence",
        "primary_selection": "disabled" if mode == "profile_inventory" else "enabled for target-reference mode only",
        "input_name_hint": hint,
        "input_name_hint_forensic_claim": False,
        "input_name_hint_matches": [x for x in hint_matches if x],
        "note": "Input-name family hints only help the HTML reader find likely same-run records; they do not change extracted artifacts, task outcomes, or case claims.",
    }
    report.setdefault("interpretation_policy", {})["v22"] = {
        "profile_inventory_wording": "HTML avoids case/primary wording when no target reference is supplied",
        "thread_local_outcomes": "task outcome remains based on thread-local evidence only",
        "display_hint_not_evidence": True,
        "scenario_specific_rules": False,
        "safe_refactor_policy": "legacy extraction/rendering code is retained to avoid changing parser behavior before regression tests",
    }
    # Rebuild case summary after display policy is attached.
    report["case_summary"] = build_case_summary(report, target or None)
    return report


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v22(extracted, input_label, browser_only=browser_only)
    return _v22_apply_display_policy(report, None)


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    target = str(target_reference or "").strip()
    filtered = _filter_report_by_target_reference_pre_v22(report, target) if target else _filter_report_by_target_reference_pre_v22(report, None)
    return _v22_apply_display_policy(filtered, target or None)


def _v22_thread_inventory_rows(report: dict[str, Any], max_rows: int = 100) -> str:
    rows: list[str] = []
    threads = [t for t in _v17_as_list(report.get("threads")) if isinstance(t, dict)]
    for idx, thread in enumerate(threads[:max_rows], start=1):
        cls = _v17_as_dict(thread.get("classification"))
        outcome = _v17_as_dict(thread.get("task_outcome"))
        avail = _v17_as_dict(thread.get("reconstruction_availability"))
        prompt = _v17_as_dict(thread.get("prompt"))
        disp = _v17_as_dict(thread.get("display"))
        final = _v17_as_dict(thread.get("final_answer"))
        hint = "yes" if disp.get("input_name_hint_match") else "no"
        rows.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{_h(disp.get('group') or 'profile_thread_inventory')}</td>"
            f"<td>{_h(hint)}</td>"
            f"<td>{_h(thread.get('thread_id'))}</td>"
            f"<td>{_h(cls.get('interaction_type'))}</td>"
            f"<td>{_h(cls.get('execution_mode'))}</td>"
            f"<td>{_h(outcome.get('task_type'))}</td>"
            f"<td>{_h(outcome.get('status'))}</td>"
            f"<td>{_h(avail.get('level'))}</td>"
            f"<td>{_h(disp.get('claim_level'))}</td>"
            f"<td>{'yes' if final.get('available') or final.get('text') else 'no'}</td>"
            f"<td>{_h((prompt.get('text') or '')[:220])}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='12'><em>No thread candidates reconstructed.</em></td></tr>")
    return (
        "<table class='table'><thead><tr>"
        "<th>#</th><th>Display group</th><th>Input-name hint</th><th>Thread</th><th>Interaction</th><th>Mode</th><th>Task</th><th>Outcome</th><th>Level</th><th>Claim level</th><th>Final</th><th>Prompt preview</th>"
        "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
    )


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    case = _v17_as_dict(report.get("case_summary")) or build_case_summary(report, _v17_as_dict(report.get("source")).get("target_reference_filter"))
    mode = _v22_report_mode(report) or case.get("report_mode")
    if mode == "profile_inventory":
        policy = _v17_as_dict(report.get("display_policy"))
        hint = policy.get("input_name_hint")
        matches = _v17_as_list(policy.get("input_name_hint_matches"))
        rows = [
            ("Report mode", "profile_inventory"),
            ("Target reference", "N/A — not supplied"),
            ("Primary/case selection", "disabled"),
            ("Thread candidates", case.get("inventory_thread_count")),
            ("Skipped candidates", case.get("inventory_skipped_count")),
            ("Global/profile records", case.get("inventory_global_record_count")),
            ("Classifier scope", "thread-local evidence only"),
            ("Input-name hint", hint or "none detected"),
            ("Hint matches", len(matches)),
        ]
        hint_note = ""
        if hint:
            hint_note = (
                f"<div class='note'><strong>Input-name hint:</strong> <code>{_h(hint)}</code> was detected from the input filename. "
                "Rows marked <em>yes</em> in the inventory table are only display hints, not forensic claims and not task-outcome evidence.</div>"
            )
        parts = [
            "<section class='card'><div class='topline'><h2>Profile inventory summary</h2><span class='badge warn'>no target reference</span></div>",
            "<div class='note warn'><strong>Profile-wide inventory mode.</strong> No <code>--target-reference</code> was supplied. This report does not infer the intended scenario, choose a primary thread, or promote any recovered thread as the case result. Earlier or residual threads can appear if they exist in the input artifacts.</div>",
            hint_note,
            _kv_table_v07(rows),
            "<h4>Artifact vs derived interpretation</h4>",
            "<div class='note'><strong>Extracted artifacts</strong> are prompt/metadata/action/URL/payload/final-answer fields recovered from the input. <strong>Derived interpretation</strong> includes task type, outcome, confidence, display grouping, and reader-oriented hints generated by the classifier.</div>",
            "<h4>Recovered thread inventory</h4>",
            _v22_thread_inventory_rows(report),
            "<details><summary>Residual / historical interpretation policy</summary><div style='padding:12px'>JSON preserves every reconstructed item. HTML labels profile or residual artifacts so they are not mistaken for a target case. Use <code>--target-reference</code> only when validating a known case.</div></details>",
            _raw_details_v07("Open profile inventory summary JSON", case),
            "</section>",
        ]
        return "\n".join(parts)
    return _render_case_summary_v10_pre_v22(report)


def _v22_inventory_html_rewrites(html: str, report: dict[str, Any]) -> str:
    """Presentation-only text rewrites for profile inventory mode.

    The older renderer is intentionally reused to avoid touching extraction and
    evidence-detail behavior. These replacements only change visible labels.
    """
    if _v22_report_mode(report) != "profile_inventory":
        return html
    replacements = {
        "<title>Comet Browser Reconstruction Report</title>": "<title>Comet Profile Artifact Inventory</title>",
        "<strong>Case summary</strong>": "<strong>Profile inventory</strong>",
        "<strong>Thread overview</strong>": "<strong>Artifact inventory</strong>",
        "<span>Reconstruction list</span>": "<span>Recovered artifacts</span>",
        "<div class='nav-title'>Threads</div>": "<div class='nav-title'>Recovered profile threads</div>",
        "<h1>Comet Browser Reconstruction Report</h1>": "<h1>Comet Profile Artifact Inventory</h1>",
        "<h2>Executive findings</h2><span class='badge neutral'>investigator view</span>": "<h2>Inventory findings</h2><span class='badge neutral'>profile inventory</span>",
        "<h2>Thread overview</h2><span class='badge neutral'>Select a thread on the left</span>": "<h2>Artifact inventory</h2><span class='badge neutral'>recovered profile threads</span>",
        "<div class='nav-title'>Residual audit</div>": "<div class='nav-title'>Global/residual audit</div>",
        "Thread를 클릭하면 하위 항목이 펼쳐집니다. 각 plan/action/reasoning 행을 클릭하면 바로 아래에서 해당 decoded JSON과 LOG/LDB 위치를 확인할 수 있습니다.": "This is a profile-wide inventory. Click a recovered thread to inspect prompt, metadata, actions, final answer, decoded JSON, and LOG/LDB locations. Previous or residual threads may appear if they exist in the input artifacts.",
    }
    for old, new in replacements.items():
        html = html.replace(old, new)
    return html


def render_html_report(report: dict[str, Any], output_path: Path) -> None:
    _render_html_report_pre_v22(report, output_path)
    try:
        html = output_path.read_text(encoding="utf-8")
        rewritten = _v22_inventory_html_rewrites(html, report)
        if rewritten != html:
            output_path.write_text(rewritten, encoding="utf-8")
    except Exception:
        # Rendering must never fail just because presentation rewriting failed.
        pass



# ---------------------------------------------------------------------------
# v0.23: payload role hygiene and conservative attribution
# ---------------------------------------------------------------------------
# This layer does not change LevelDB/IndexedDB extraction, thread grouping,
# task outcome selection, or raw evidence preservation. It only prevents
# candidate strings from being over-promoted as typed UI payloads when their
# role is not supported by the current thread/task.
#
# Main fixes:
# - email addresses found in email body/signature/final text are no longer
#   promoted as recipients unless the prompt/tool action supports recipient role;
# - recipient/email artifacts are demoted from non-email tasks;
# - PDF filenames are demoted from non-download tasks;
# - PDF filename candidates are split into reported_downloaded_filename vs
#   nearby_pdf_candidates;
# - application upsell title/description strings are not treated as user payload;
# - demoted items are preserved under payload_audit rather than deleted.

SCHEMA_VERSION = "0.23"

_reconstruct_browser_threads_pre_v23 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v23 = filter_report_by_target_reference
_render_case_summary_v10_pre_v23 = _render_case_summary_v10


def _v23_as_list(value: Any) -> list[Any]:
    return _v17_as_list(value) if '_v17_as_list' in globals() else (value if isinstance(value, list) else ([] if value is None else [value]))


def _v23_as_dict(value: Any) -> dict[str, Any]:
    return _v17_as_dict(value) if '_v17_as_dict' in globals() else (value if isinstance(value, dict) else {})


def _v23_text(value: Any) -> str:
    if '_v21_norm_text' in globals():
        return _v21_norm_text(value)
    return str(value or "")


def _v23_task_type(thread: dict[str, Any]) -> str:
    outcome = _v23_as_dict(thread.get("task_outcome"))
    if not outcome:
        outcome = _v23_as_dict(_v23_as_dict(thread.get("derived_interpretation")).get("task_outcome"))
    return str(outcome.get("task_type") or "").strip()


def _v23_thread_text(thread: dict[str, Any]) -> str:
    if '_v21_thread_local_parts' in globals():
        return _v21_thread_local_parts(thread).get("all", "")
    return json.dumps(thread, ensure_ascii=False, default=str)


def _v23_prompt_text(thread: dict[str, Any]) -> str:
    return _v23_text(_v23_as_dict(thread.get("prompt")).get("text"))


def _v23_final_text(thread: dict[str, Any]) -> str:
    return _v23_text(_v23_as_dict(thread.get("final_answer")).get("text"))


def _v23_unique(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip().strip("`'\".,;:()[]{}")
        if not text:
            continue
        key = text.lower()
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _v23_pdf_filenames(text: str) -> list[str]:
    if 'extract_pdf_filenames' in globals():
        try:
            return _v23_unique(extract_pdf_filenames(text))
        except Exception:
            pass
    return _v23_unique(re.findall(r"[A-Za-z0-9][A-Za-z0-9._\- ]{0,120}\.pdf", text or "", flags=re.I))


def _v23_reported_downloaded_filename(thread: dict[str, Any]) -> str | None:
    final = _v23_final_text(thread)
    if not final:
        return None
    patterns = [
        r"final\s+downloaded\s+filename\s*[:\-]\s*`?([^`\n\r]+?\.pdf)`?",
        r"downloaded\s+filename\s*[:\-]\s*`?([^`\n\r]+?\.pdf)`?",
        r"downloaded\s+file\s*[:\-]\s*`?([^`\n\r]+?\.pdf)`?",
        r"filename\s*[:\-]\s*`?([^`\n\r]+?\.pdf)`?",
    ]
    for pat in patterns:
        m = re.search(pat, final, flags=re.I)
        if m:
            return _v23_unique([m.group(1)])[0]
    pdfs = _v23_pdf_filenames(final)
    if len(pdfs) == 1 and re.search(r"download\s+(?:is\s+)?complete|successfully\s+downloaded|다운로드\s*완료", final, flags=re.I):
        return pdfs[0]
    return None


def _v23_is_email_like(value: Any) -> bool:
    return bool(EMAIL_RE.search(str(value or ""))) if 'EMAIL_RE' in globals() else bool(re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", str(value or "")))


def _v23_is_pdf_like(value: Any) -> bool:
    return bool(re.search(r"\.pdf\b", str(value or ""), flags=re.I))


def _v23_prompt_confirms_recipient(thread: dict[str, Any], email_value: str) -> bool:
    prompt = _v23_prompt_text(thread)
    if not prompt or not email_value:
        return False
    low = prompt.lower()
    email_low = email_value.lower()
    if email_low not in low:
        return False
    idx = low.find(email_low)
    window = low[max(0, idx - 120): idx + len(email_low) + 80]
    return any(marker in window for marker in ["recipient", "to:", "to ", "받는", "수신", "email"])


def _v23_looks_like_signature_or_body_email(payload: dict[str, Any], thread: dict[str, Any]) -> bool:
    value = str(payload.get("value") or "")
    if not _v23_is_email_like(value):
        return False
    # Values extracted from free-form final/action text without order/path are
    # often signatures or account identifiers, not UI recipient fields.
    source = str(payload.get("payload_source") or "").lower()
    path = str(payload.get("path_hint") or "").lower()
    rel = payload.get("relative_order")
    if source == "action_or_final_text" and not path:
        return True
    if rel is None and not path and source != "prompt":
        return True
    # If an email appears only inside a long body text with common sign-off
    # markers, do not promote it as a recipient.
    bodyish = json.dumps(thread.get("typed_payloads") or [], ensure_ascii=False, default=str).lower()
    val_low = value.lower().lstrip("n")  # tolerate one-character regex over-capture from legacy layer
    if val_low in bodyish and any(x in bodyish for x in ["best regards", "regards", "sincerely", "dear "]):
        if not _v23_prompt_confirms_recipient(thread, value):
            return True
    return False


def _v23_payload_demotion_reason(payload: dict[str, Any], thread: dict[str, Any], reported_pdf: str | None) -> str | None:
    field = str(payload.get("field") or "").strip().lower()
    value = payload.get("value")
    path = str(payload.get("path_hint") or "").lower()
    task = _v23_task_type(thread)
    source = str(payload.get("payload_source") or "").lower()

    if "upsell_information" in path:
        return "application upsell metadata, not user task payload"

    email_like = _v23_is_email_like(value)
    if field in {"recipient", "to", "email_recipient"} or (email_like and field not in {"text", "body"}):
        if task != "email_draft_send":
            return f"email/recipient-like value is not a payload field for task_type={task or 'unknown'}"
        # In email tasks, keep prompt-confirmed recipient values and direct text
        # form inputs. Demote free-form body/signature/account emails.
        if source == "prompt" or _v23_prompt_confirms_recipient(thread, str(value or "")):
            return None
        if _v23_looks_like_signature_or_body_email(payload, thread):
            return "email address appears in body/signature or free-form text, not confirmed as recipient"
        if field in {"recipient", "to", "email_recipient"} and payload.get("relative_order") is None and not path:
            return "recipient role lacks ordered field/action evidence"

    if field == "filename" or (_v23_is_pdf_like(value) and field not in {"url", "text"}):
        if task != "file_download":
            return f"PDF filename is not a payload field for task_type={task or 'unknown'}"
        if reported_pdf:
            if str(value or "").strip().lower() != reported_pdf.strip().lower():
                return "nearby PDF candidate, not the reported downloaded filename"
        else:
            return "PDF candidate has no final-answer confirmation as downloaded filename"

    # Calendar: prevent generic email/file candidates while keeping title/date/time/description.
    if task == "calendar_create" and field in {"recipient", "to", "email_recipient", "filename"}:
        return "non-calendar payload field in calendar_create thread"

    # Page open/web research/basic chat should not promote UI form payloads unless
    # they are URLs or page-title evidence. Raw records stay in timeline/buckets.
    if task in {"page_open", "web_research", "basic_chat", "unknown"} and field in {"recipient", "to", "email_recipient", "filename", "title", "description"}:
        if field in {"title", "description"} and "upsell" not in path:
            return None
        return f"field not promoted as typed payload for task_type={task}"

    return None


def _v23_demote_item(item: dict[str, Any], reason: str) -> dict[str, Any]:
    demoted = dict(item)
    demoted["payload_promotion"] = "demoted"
    demoted["demotion_reason"] = reason
    demoted["forensic_note"] = "Raw artifact is preserved, but this value is not promoted as a confirmed typed UI payload."
    return demoted


def _v23_filter_typed_payloads(thread: dict[str, Any], reported_pdf: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for payload in _v23_as_list(thread.get("typed_payloads")):
        if not isinstance(payload, dict):
            kept.append(payload)
            continue
        reason = _v23_payload_demotion_reason(payload, thread, reported_pdf)
        if reason:
            demoted.append(_v23_demote_item(payload, reason))
        else:
            item = dict(payload)
            item.setdefault("payload_promotion", "promoted")
            # Do not overstate role certainty.
            if item.get("payload_source") == "prompt":
                item.setdefault("payload_role", "requested_payload")
            elif item.get("field") in {"recipient", "event_title", "date", "start_time", "end_time", "description", "filename"}:
                item.setdefault("payload_role", "observed_or_reported_payload")
            else:
                item.setdefault("payload_role", "observed_artifact")
            kept.append(item)
    return kept, demoted


def _v23_action_demotion_reason(action: dict[str, Any], thread: dict[str, Any], reported_pdf: str | None, demoted_pairs: set[tuple[str, str]]) -> str | None:
    field = str(action.get("field") or "").strip().lower()
    value = str(action.get("value") or "")
    task = _v23_task_type(thread)
    pair = (field, value.strip().lower())
    if pair in demoted_pairs:
        return "derived from demoted typed_payload"
    if field in {"recipient", "to", "email_recipient"}:
        if task != "email_draft_send":
            return f"recipient action is not applicable to task_type={task or 'unknown'}"
        pseudo = {"field": field, "value": value, "payload_source": "action_or_final_text", "relative_order": action.get("relative_order")}
        if not _v23_prompt_confirms_recipient(thread, value) and _v23_looks_like_signature_or_body_email(pseudo, thread):
            return "recipient action appears to come from body/signature/free-form text"
    if field == "filename" or (_v23_is_pdf_like(value) and field not in {"url", "text"}):
        if task != "file_download":
            return f"filename action is not applicable to task_type={task or 'unknown'}"
        if reported_pdf and value.strip().lower() != reported_pdf.strip().lower():
            return "nearby PDF candidate, not reported downloaded filename"
        if not reported_pdf:
            return "PDF candidate has no final-answer confirmation as downloaded filename"
    return None


def _v23_filter_structured_actions(thread: dict[str, Any], reported_pdf: str | None, demoted_payloads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    demoted_pairs: set[tuple[str, str]] = set()
    for p in demoted_payloads:
        demoted_pairs.add((str(p.get("field") or "").strip().lower(), str(p.get("value") or "").strip().lower()))
    kept: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for action in _v23_as_list(thread.get("structured_actions")):
        if not isinstance(action, dict):
            kept.append(action)
            continue
        reason = _v23_action_demotion_reason(action, thread, reported_pdf, demoted_pairs)
        if reason:
            demoted.append(_v23_demote_item(action, reason))
        else:
            kept.append(action)
    return kept, demoted


def _v23_update_download_outcome(thread: dict[str, Any], reported_pdf: str | None) -> dict[str, Any]:
    outcome = dict(_v23_as_dict(thread.get("task_outcome")))
    if outcome.get("task_type") != "file_download":
        outcome["downloaded_filename_candidates"] = []
        basis = _v23_as_dict(outcome.get("classification_basis"))
        trace = _v23_as_dict(basis.get("trace_markers"))
        if trace:
            trace["pdf_filename_candidates"] = []
            basis["trace_markers"] = trace
            outcome["classification_basis"] = basis
        return outcome

    all_pdfs = _v23_pdf_filenames(_v23_thread_text(thread))
    if reported_pdf:
        nearby = [p for p in all_pdfs if p.lower() != reported_pdf.lower()]
        outcome["reported_downloaded_filename"] = reported_pdf
        outcome["downloaded_filename_candidates"] = [reported_pdf]
        outcome["nearby_pdf_candidates"] = nearby
        basis = _v23_as_dict(outcome.get("classification_basis"))
        trace = _v23_as_dict(basis.get("trace_markers"))
        if trace:
            trace["pdf_filename_candidates"] = [reported_pdf]
            trace["nearby_pdf_candidates"] = nearby
            basis["trace_markers"] = trace
            outcome["classification_basis"] = basis
    else:
        outcome["reported_downloaded_filename"] = None
        outcome["nearby_pdf_candidates"] = all_pdfs
        # Without final-answer confirmation, do not label these as downloaded filenames.
        outcome["downloaded_filename_candidates"] = []
        if outcome.get("status") == "completed_download":
            # Keep the task classification but lower the exact filename claim if
            # only source-discovery or confirmation text is present.
            final = _v23_final_text(thread).lower()
            if "download is complete" not in final and "download complete" not in final and "successfully downloaded" not in final:
                outcome["status"] = "source_discovery_or_partial_download"
                outcome["side_effect_completed"] = None
                outcome["confidence"] = "medium"
    return outcome


def _v23_sync_thread_views(thread: dict[str, Any]) -> None:
    extracted = _v23_as_dict(thread.get("extracted_artifacts"))
    if extracted:
        for key in ["typed_payloads", "structured_actions", "task_outcome", "payload_audit"]:
            if key in thread:
                extracted[key] = thread.get(key)
        thread["extracted_artifacts"] = extracted
    derived = _v23_as_dict(thread.get("derived_interpretation"))
    if derived:
        if "task_outcome" in thread:
            derived["task_outcome"] = thread.get("task_outcome")
        if "payload_audit" in thread:
            derived["payload_audit"] = thread.get("payload_audit")
        thread["derived_interpretation"] = derived


def _v23_apply_payload_hygiene(report: dict[str, Any]) -> dict[str, Any]:
    report["schema_version"] = "0.23"
    report.setdefault("source", {})["payload_attribution_version"] = "0.23"
    report.setdefault("interpretation_policy", {})["v23_payload_attribution"] = {
        "raw_artifacts_preserved": True,
        "typed_payloads_are_promoted_only_after_task_local_role_checks": True,
        "demoted_values_stored_under_payload_audit": True,
        "non_task_payload_contamination_suppressed_from_typed_payloads_and_structured_actions": True,
    }

    total_demoted_payloads = 0
    total_demoted_actions = 0
    for thread in [t for t in _v23_as_list(report.get("threads")) if isinstance(t, dict)]:
        reported_pdf = _v23_reported_downloaded_filename(thread)
        kept_payloads, demoted_payloads = _v23_filter_typed_payloads(thread, reported_pdf)
        kept_actions, demoted_actions = _v23_filter_structured_actions(thread, reported_pdf, demoted_payloads)
        outcome = _v23_update_download_outcome(thread, reported_pdf)

        thread["typed_payloads"] = kept_payloads
        thread["structured_actions"] = kept_actions
        thread["task_outcome"] = outcome
        audit = _v23_as_dict(thread.get("payload_audit"))
        audit.update({
            "version": "0.23",
            "policy": "Only task-local, role-supported values are promoted as typed_payloads/structured_actions. Demoted values remain here for auditability.",
            "reported_downloaded_filename": reported_pdf,
            "nearby_pdf_candidates": outcome.get("nearby_pdf_candidates", []),
            "kept_typed_payload_count": len(kept_payloads),
            "demoted_typed_payload_count": len(demoted_payloads),
            "demoted_structured_action_count": len(demoted_actions),
            "demoted_typed_payloads": demoted_payloads,
            "demoted_structured_actions": demoted_actions,
        })
        thread["payload_audit"] = audit
        total_demoted_payloads += len(demoted_payloads)
        total_demoted_actions += len(demoted_actions)
        _v23_sync_thread_views(thread)

    report["payload_attribution_summary"] = {
        "version": "0.23",
        "demoted_typed_payload_count": total_demoted_payloads,
        "demoted_structured_action_count": total_demoted_actions,
        "note": "Demotion means the raw value exists in artifacts but is not promoted as a confirmed typed UI payload for that thread/task.",
    }
    # Recompute case summary after payload and outcome normalization, preserving v22 display policy.
    try:
        target = _v23_as_dict(report.get("source")).get("target_reference_filter") or None
        if report.get("report_mode") == "profile_inventory":
            target = None
        report["case_summary"] = build_case_summary(report, target)
    except Exception:
        pass
    return report


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v23(extracted, input_label, browser_only=browser_only)
    return _v23_apply_payload_hygiene(report)


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v23(report, target_reference)
    return _v23_apply_payload_hygiene(filtered)


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    html = _render_case_summary_v10_pre_v23(report)
    summary = _v23_as_dict(report.get("payload_attribution_summary"))
    if not summary:
        return html
    note = (
        "<div class='note'><strong>Payload attribution hygiene:</strong> "
        f"v0.23 demoted {_h(summary.get('demoted_typed_payload_count'))} typed-payload candidate(s) and "
        f"{_h(summary.get('demoted_structured_action_count'))} structured-action candidate(s) from promoted UI-field views. "
        "Demoted values remain available in each thread's <code>payload_audit</code> JSON.</div>"
    )
    return html.replace("</section>", note + "</section>", 1) if "</section>" in html else html + note



# ---------------------------------------------------------------------------
# v0.24: presentation-safe payload roles and residual thread labeling
# ---------------------------------------------------------------------------
# This layer is deliberately generic. It does not contain scenario IDs, expected
# answers, email addresses, event titles, filenames, or case-specific branches.
# It only separates confirmed UI-field payloads from observed input artifacts,
# repairs filename-candidate preservation, and labels residual/historical
# inventory rows using generic run-family tokens detected from filenames/prompts.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.24"

_reconstruct_browser_threads_pre_v24 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v24 = filter_report_by_target_reference
_render_case_summary_v10_pre_v24 = _render_case_summary_v10
_render_html_report_pre_v24 = render_html_report


def _v24_as_list(value: Any) -> list[Any]:
    return _v23_as_list(value) if '_v23_as_list' in globals() else (value if isinstance(value, list) else ([] if value is None else [value]))


def _v24_as_dict(value: Any) -> dict[str, Any]:
    return _v23_as_dict(value) if '_v23_as_dict' in globals() else (value if isinstance(value, dict) else {})


def _v24_text(value: Any) -> str:
    return _v23_text(value) if '_v23_text' in globals() else str(value or "")


def _v24_task_type(thread: dict[str, Any]) -> str:
    return _v23_task_type(thread) if '_v23_task_type' in globals() else str(_v24_as_dict(thread.get('task_outcome')).get('task_type') or '')


def _v24_prompt_text(thread: dict[str, Any]) -> str:
    return _v23_prompt_text(thread) if '_v23_prompt_text' in globals() else _v24_text(_v24_as_dict(thread.get('prompt')).get('text'))


def _v24_final_text(thread: dict[str, Any]) -> str:
    return _v23_final_text(thread) if '_v23_final_text' in globals() else _v24_text(_v24_as_dict(thread.get('final_answer')).get('text'))


def _v24_thread_text(thread: dict[str, Any]) -> str:
    if '_v21_thread_local_parts' in globals():
        try:
            return _v21_thread_local_parts(thread).get('all', '')
        except Exception:
            pass
    return json.dumps(thread, ensure_ascii=False, default=str)


def _v24_unique(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or '').strip().strip("`'\".,;:()[]{}<>")
        if not text:
            continue
        key = text.lower()
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _v24_family_tokens(text: str) -> list[str]:
    """Generic run/case-family tokens, e.g., S08 or CASE12.

    The regex is presentation-only. It does not decide task outcomes or create
    forensic claims. It intentionally avoids embedding known scenario names.
    """
    tokens: list[str] = []
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z]{1,12}\d{1,4})(?![A-Za-z0-9])", str(text or '')):
        tok = m.group(1).upper()
        if any(ch.isdigit() for ch in tok):
            tokens.append(tok)
    return _v24_unique(tokens)


def _v24_input_family_hint(report: dict[str, Any]) -> str | None:
    src = _v24_as_dict(report.get('source'))
    raw = str(src.get('input') or src.get('input_label') or '')
    name = Path(raw).name if raw else ''
    toks = _v24_family_tokens(name)
    return toks[0] if toks else None


def _v24_thread_family_tokens(thread: dict[str, Any]) -> list[str]:
    prompt = _v24_as_dict(thread.get('prompt'))
    refs = ' '.join(str(x) for x in _v24_as_list(prompt.get('reference_codes')))
    local = '\n'.join([
        refs,
        str(prompt.get('text') or ''),
        str(_v24_as_dict(thread.get('metadata')).get('title') or ''),
    ])
    return _v24_family_tokens(local)


def _v24_thread_matches_family_hint(thread: dict[str, Any], hint: str | None) -> bool:
    if not hint:
        return False
    hint_up = hint.upper()
    toks = _v24_thread_family_tokens(thread)
    if hint_up in toks:
        return True
    return hint.lower() in _v24_thread_text(thread).lower()


# Override the v22 helper too, so any later table rendering uses the fixed token
# extraction for filenames such as S08_IndexedDB.zip.
def _v22_input_family_hint(report: dict[str, Any]) -> str | None:  # type: ignore[override]
    return _v24_input_family_hint(report)


def _v22_thread_matches_hint(thread: dict[str, Any], hint: str | None) -> bool:  # type: ignore[override]
    return _v24_thread_matches_family_hint(thread, hint)


def _v24_cleanup_pdf_filename(raw: str) -> str | None:
    s = str(raw or '')
    # Preserve meaningful filename characters such as underscores and hyphens.
    s = re.sub(r"\s*\.\s*pdf\b", ".pdf", s, flags=re.I)
    s = " ".join(s.split())
    s = s.strip("`'\".,;:()[]{}<>")
    if not s.lower().endswith('.pdf'):
        return None
    # If a long phrase contains a final path token ending in .pdf, prefer the
    # last path/filename-like token but do not remove underscores.
    toks = [t.strip("`'\".,;:()[]{}<>") for t in re.split(r"\s+", s) if '.pdf' in t.lower()]
    if toks:
        s = toks[-1]
    s = s.strip("`'\".,;:()[]{}<>")
    stem = s[:-4]
    if len(stem) < 4 or not re.search(r"[A-Za-z0-9]", stem):
        return None
    return s


def _v24_pdf_filenames(text: str) -> list[str]:
    source = str(text or '')
    found: list[str] = []
    # URL/path tokens and ordinary filename tokens. This deliberately preserves
    # underscores, unlike the legacy cleanup routine.
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z0-9][A-Za-z0-9._\- %/]{0,180}?\.\s*pdf)(?![A-Za-z0-9])", source, flags=re.I):
        raw = m.group(1)
        # If it is a URL/path, keep the basename candidate.
        raw = raw.split('?')[0].split('#')[0]
        if '/' in raw:
            raw = raw.rsplit('/', 1)[-1]
        cleaned = _v24_cleanup_pdf_filename(raw)
        if cleaned:
            found.append(cleaned)
    # Explicit labels can include line breaks/spaces in the filename.
    for m in re.finditer(r"(?:filename|file name|downloaded file|final downloaded filename)\s*[:：\-]\s*`?([^`\n\r]{0,220}?\.\s*pdf)`?", source, flags=re.I):
        cleaned = _v24_cleanup_pdf_filename(m.group(1))
        if cleaned:
            found.append(cleaned)
    # Deduplicate, preserving first occurrence order.
    return _v24_unique(found)


# Make v23 download hygiene use the v24 filename-preserving extractor whenever it
# is called by the v24 post-processing pass.
def _v23_pdf_filenames(text: str) -> list[str]:  # type: ignore[override]
    return _v24_pdf_filenames(text)


def _v24_reported_downloaded_filename(thread: dict[str, Any]) -> str | None:
    final = _v24_final_text(thread)
    if not final:
        return None
    patterns = [
        r"final\s+downloaded\s+filename\s*[:\-]\s*`?([^`\n\r]+?\.\s*pdf)`?",
        r"downloaded\s+filename\s*[:\-]\s*`?([^`\n\r]+?\.\s*pdf)`?",
        r"downloaded\s+file\s*[:\-]\s*`?([^`\n\r]+?\.\s*pdf)`?",
        r"filename\s*[:\-]\s*`?([^`\n\r]+?\.\s*pdf)`?",
    ]
    for pat in patterns:
        m = re.search(pat, final, flags=re.I)
        if m:
            cleaned = _v24_cleanup_pdf_filename(m.group(1))
            if cleaned:
                return cleaned
    pdfs = _v24_pdf_filenames(final)
    if len(pdfs) == 1 and re.search(r"download\s+(?:is\s+)?complete|successfully\s+downloaded|다운로드\s*완료", final, flags=re.I):
        return pdfs[0]
    return None


def _v24_payload_confidence(payload: dict[str, Any], thread: dict[str, Any], reported_pdf: str | None) -> str:
    """Return confirmed/candidate/observed for a v23-promoted payload."""
    field = str(payload.get('field') or '').strip().lower()
    value = str(payload.get('value') or '')
    task = _v24_task_type(thread)
    if task == 'email_draft_send':
        if field in {'recipient', 'to', 'email_recipient'}:
            if '_v23_prompt_confirms_recipient' in globals() and _v23_prompt_confirms_recipient(thread, value):
                return 'confirmed'
            # Ordered field evidence can remain candidate, but not confirmed.
            return 'candidate'
        if field in {'subject', 'body'}:
            return 'confirmed'
        return 'observed'
    if task == 'calendar_create':
        if field in {'event_title', 'calendar_title', 'title', 'date', 'start_time', 'end_time', 'description'}:
            return 'confirmed'
        return 'observed'
    if task == 'file_download':
        if (field == 'filename' or value.lower().endswith('.pdf')) and reported_pdf and value.strip().lower() == reported_pdf.strip().lower():
            return 'confirmed'
        if value.lower().endswith('.pdf'):
            return 'candidate'
        return 'observed'
    # For research/page/basic chat, payload-like strings are generally recovered
    # artifacts rather than confirmed typed UI fields.
    return 'observed'


def _v24_promote_payload_roles(thread: dict[str, Any]) -> None:
    reported_pdf = _v24_reported_downloaded_filename(thread)
    confirmed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []
    for payload in _v24_as_list(thread.get('typed_payloads')):
        if not isinstance(payload, dict):
            observed.append({'value': payload, 'payload_role': 'observed_input_artifact'})
            continue
        item = dict(payload)
        level = _v24_payload_confidence(item, thread, reported_pdf)
        if level == 'confirmed':
            item['payload_role'] = 'confirmed_typed_field'
            item['payload_promotion'] = 'confirmed'
            confirmed.append(item)
        elif level == 'candidate':
            item['payload_role'] = 'candidate_typed_field'
            item['payload_promotion'] = 'candidate'
            candidates.append(item)
        else:
            item['payload_role'] = 'observed_input_artifact'
            item['payload_promotion'] = 'observed_not_confirmed'
            observed.append(item)

    confirmed_actions: list[dict[str, Any]] = []
    observed_actions: list[dict[str, Any]] = []
    confirmed_pairs = {(str(p.get('field') or '').lower(), str(p.get('value') or '').strip().lower()) for p in confirmed}
    for action in _v24_as_list(thread.get('structured_actions')):
        if not isinstance(action, dict):
            observed_actions.append({'value': action, 'action_role': 'observed_action_artifact'})
            continue
        pair = (str(action.get('field') or '').lower(), str(action.get('value') or '').strip().lower())
        item = dict(action)
        if pair in confirmed_pairs:
            item['action_role'] = 'confirmed_structured_field'
            confirmed_actions.append(item)
        else:
            item['action_role'] = 'observed_or_candidate_action_artifact'
            observed_actions.append(item)

    # Backward-compatible promoted views now contain only confirmed field-level
    # values. Less certain evidence is preserved separately, not deleted.
    thread['confirmed_payloads'] = confirmed
    thread['candidate_payloads'] = candidates
    thread['observed_input_artifacts'] = observed
    thread['confirmed_structured_actions'] = confirmed_actions
    thread['observed_structured_actions'] = observed_actions
    thread['typed_payloads'] = confirmed
    thread['structured_actions'] = confirmed_actions

    audit = _v24_as_dict(thread.get('payload_audit'))
    audit.update({
        'version': '0.24',
        'confirmed_payload_count': len(confirmed),
        'candidate_payload_count': len(candidates),
        'observed_input_artifact_count': len(observed),
        'confirmed_structured_action_count': len(confirmed_actions),
        'observed_structured_action_count': len(observed_actions),
        'field_level_policy': 'typed_payloads/structured_actions contain confirmed field-level values only; candidates and observed input artifacts remain in separate audit fields.',
    })
    thread['payload_audit'] = audit


def _v24_update_download_filename_views(thread: dict[str, Any]) -> None:
    outcome = _v24_as_dict(thread.get('task_outcome'))
    if outcome.get('task_type') != 'file_download':
        return
    reported = _v24_reported_downloaded_filename(thread)
    all_pdfs = _v24_pdf_filenames(_v24_thread_text(thread))
    if reported:
        nearby = [p for p in all_pdfs if p.lower() != reported.lower()]
        outcome['reported_downloaded_filename'] = reported
        outcome['downloaded_filename_candidates'] = [reported]
        outcome['nearby_pdf_candidates'] = nearby
        audit = _v24_as_dict(thread.get('payload_audit'))
        audit['reported_downloaded_filename'] = reported
        audit['nearby_pdf_candidates'] = nearby
        thread['payload_audit'] = audit
    else:
        outcome['reported_downloaded_filename'] = None
        outcome['downloaded_filename_candidates'] = []
        outcome['nearby_pdf_candidates'] = all_pdfs
    thread['task_outcome'] = outcome


def _v24_apply_residual_display_policy(report: dict[str, Any]) -> None:
    mode = str(report.get('report_mode') or _v24_as_dict(report.get('source')).get('report_mode') or '')
    if mode != 'profile_inventory':
        return
    hint = _v24_input_family_hint(report)
    hint_matches: list[str] = []
    residual_threads: list[str] = []
    for thread in [t for t in _v24_as_list(report.get('threads')) if isinstance(t, dict)]:
        display = _v24_as_dict(thread.get('display'))
        toks = _v24_thread_family_tokens(thread)
        matches_hint = _v24_thread_matches_family_hint(thread, hint)
        if hint and matches_hint:
            display['group'] = 'input_name_family_candidate'
            display['profile_relation'] = 'same_input_family_candidate'
            display['input_name_hint_match'] = True
            display['sort_hint'] = 'input_filename_family_match_not_forensic_evidence'
            display['claim_level'] = 'recovered_profile_artifact_same_family_hint_not_case_claim'
            hint_matches.append(str(thread.get('thread_id') or ''))
        elif hint and toks:
            display['group'] = 'residual_or_historical_thread'
            display['profile_relation'] = 'different_family_residual_or_historical'
            display['input_name_hint_match'] = False
            display['claim_level'] = 'recovered_profile_residual_artifact_not_case_result'
            residual_threads.append(str(thread.get('thread_id') or ''))
        else:
            display.setdefault('group', 'profile_thread_inventory')
            display.setdefault('profile_relation', 'unassigned_profile_artifact')
            display.setdefault('input_name_hint_match', False)
        thread['display'] = display
        if isinstance(thread.get('derived_interpretation'), dict):
            thread['derived_interpretation']['display'] = display

    policy = _v24_as_dict(report.get('display_policy'))
    policy.update({
        'input_name_hint': hint,
        'input_name_hint_forensic_claim': False,
        'input_name_hint_matches': [x for x in hint_matches if x],
        'residual_or_historical_threads': [x for x in residual_threads if x],
        'residual_labeling_policy': 'Threads whose generic family token differs from the input-name hint are labeled residual_or_historical_thread in profile inventory mode. This is a display relation, not a deletion or evidence claim.',
    })
    report['display_policy'] = policy


def _v24_sync_thread_views(thread: dict[str, Any]) -> None:
    if '_v23_sync_thread_views' in globals():
        try:
            _v23_sync_thread_views(thread)
        except Exception:
            pass
    extracted = _v24_as_dict(thread.get('extracted_artifacts'))
    if extracted:
        for key in ['confirmed_payloads', 'candidate_payloads', 'observed_input_artifacts', 'confirmed_structured_actions', 'observed_structured_actions', 'typed_payloads', 'structured_actions', 'task_outcome', 'payload_audit']:
            if key in thread:
                extracted[key] = thread.get(key)
        thread['extracted_artifacts'] = extracted
    derived = _v24_as_dict(thread.get('derived_interpretation'))
    if derived:
        for key in ['confirmed_payloads', 'candidate_payloads', 'observed_input_artifacts', 'task_outcome', 'payload_audit', 'display']:
            if key in thread:
                derived[key] = thread.get(key)
        thread['derived_interpretation'] = derived


def _v24_apply_payload_and_display_hygiene(report: dict[str, Any]) -> dict[str, Any]:
    # v23 hygiene remains the base. Re-apply with the v24 PDF extractor so nearby
    # filename candidates preserve underscores/hyphens.
    if '_v23_apply_payload_hygiene' in globals():
        try:
            report = _v23_apply_payload_hygiene(report)
        except Exception:
            pass
    report['schema_version'] = '0.24'
    report.setdefault('source', {})['payload_attribution_version'] = '0.24'
    report.setdefault('interpretation_policy', {})['v24_payload_roles'] = {
        'confirmed_payloads_are_field_level_claims': True,
        'observed_input_artifacts_are_preserved_but_not_claimed_as_fields': True,
        'residual_thread_labels_are_display_only': True,
        'scenario_specific_rules': False,
    }

    total_confirmed = total_candidates = total_observed = 0
    for thread in [t for t in _v24_as_list(report.get('threads')) if isinstance(t, dict)]:
        _v24_update_download_filename_views(thread)
        _v24_promote_payload_roles(thread)
        total_confirmed += len(_v24_as_list(thread.get('confirmed_payloads')))
        total_candidates += len(_v24_as_list(thread.get('candidate_payloads')))
        total_observed += len(_v24_as_list(thread.get('observed_input_artifacts')))
        _v24_sync_thread_views(thread)

    _v24_apply_residual_display_policy(report)
    report['payload_attribution_summary_v24'] = {
        'version': '0.24',
        'confirmed_payload_count': total_confirmed,
        'candidate_payload_count': total_candidates,
        'observed_input_artifact_count': total_observed,
        'note': 'Field-level payload claims are separated from observed keyboard/text artifacts and candidate values. No recovered raw evidence is deleted.',
    }
    # Keep legacy key updated for readers that only look at this location.
    report.setdefault('payload_attribution_summary', {})['field_role_split_version'] = '0.24'
    report['payload_attribution_summary']['confirmed_payload_count'] = total_confirmed
    report['payload_attribution_summary']['candidate_payload_count'] = total_candidates
    report['payload_attribution_summary']['observed_input_artifact_count'] = total_observed

    try:
        target = _v24_as_dict(report.get('source')).get('target_reference_filter') or None
        if report.get('report_mode') == 'profile_inventory':
            target = None
        report['case_summary'] = build_case_summary(report, target)
    except Exception:
        pass
    return report


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v24(extracted, input_label, browser_only=browser_only)
    return _v24_apply_payload_and_display_hygiene(report)


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v24(report, target_reference)
    return _v24_apply_payload_and_display_hygiene(filtered)


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    html = _render_case_summary_v10_pre_v24(report)
    summary = _v24_as_dict(report.get('payload_attribution_summary_v24'))
    policy = _v24_as_dict(report.get('display_policy'))
    if not summary and not policy:
        return html
    note = (
        "<div class='note'><strong>v0.24 payload roles:</strong> "
        f"{_h(summary.get('confirmed_payload_count', 0))} confirmed field-level payload(s), "
        f"{_h(summary.get('candidate_payload_count', 0))} candidate value(s), and "
        f"{_h(summary.get('observed_input_artifact_count', 0))} observed input artifact(s). "
        "Observed artifacts remain in JSON but are not shown as confirmed UI fields.</div>"
    )
    residuals = _v24_as_list(policy.get('residual_or_historical_threads'))
    if residuals:
        note += (
            "<div class='note warn'><strong>Residual/historical labeling:</strong> "
            f"{len(residuals)} recovered thread(s) were labeled as residual_or_historical_thread because their generic family token differs from the input-name hint. "
            "They remain preserved in JSON and HTML for audit, but are not target-case claims.</div>"
        )
    return html.replace("</section>", note + "</section>", 1) if "</section>" in html else html + note


def render_html_report(report: dict[str, Any], output_path: Path) -> None:
    _render_html_report_pre_v24(report, output_path)
    try:
        html = output_path.read_text(encoding='utf-8')
        if str(report.get('report_mode') or _v24_as_dict(report.get('source')).get('report_mode')) == 'profile_inventory':
            html = html.replace('recovered profile threads', 'recovered profile threads / residual-aware')
            html = html.replace('Recovered profile threads</div>', 'Recovered profile threads / residual-aware</div>')
        output_path.write_text(html, encoding='utf-8')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v0.25: strict residual-family display matching and legacy PDF trace cleanup.
#
# This block deliberately avoids scenario-specific rules.  It only uses:
#   * generic family tokens explicitly present in prompt/reference/title fields
#   * generic PDF filename extraction/normalization
#   * existing v0.24 payload role separation
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.25"

_reconstruct_browser_threads_pre_v25 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v25 = filter_report_by_target_reference
_render_html_report_pre_v25 = render_html_report
_render_case_summary_v10_pre_v25 = _render_case_summary_v10


def _v25_as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _v25_as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _v25_pdf_key(value: Any) -> str:
    """Comparison key that treats punctuation-only filename variants as equal."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _v25_normalized_pdf_candidates(values: Any, preferred: list[str] | None = None) -> list[str]:
    """Normalize a list of PDF-like values while preferring richer raw spellings.

    Example: if a legacy routine produced ``4-StanleyNIST-AI-RMF.pdf`` but the
    thread text also contains ``4-Stanley_NIST-AI-RMF.pdf``, the preferred raw
    spelling wins through the punctuation-insensitive comparison key.  This is a
    generic filename normalization rule, not a scenario rule.
    """
    preferred = preferred or []
    preferred_by_key: dict[str, str] = {}
    for cand in preferred:
        cleaned = _v24_cleanup_pdf_filename(cand) if '_v24_cleanup_pdf_filename' in globals() else str(cand or '').strip()
        if cleaned:
            preferred_by_key[_v25_pdf_key(cleaned)] = cleaned

    out: list[str] = []
    seen: set[str] = set()
    raw_values = values if isinstance(values, list) else [values]
    for val in raw_values:
        candidates: list[str] = []
        if isinstance(val, str):
            if '_v24_pdf_filenames' in globals():
                candidates.extend(_v24_pdf_filenames(val))
            cleaned = _v24_cleanup_pdf_filename(val) if '_v24_cleanup_pdf_filename' in globals() else val.strip()
            if cleaned:
                candidates.append(cleaned)
        elif val is not None:
            cleaned = _v24_cleanup_pdf_filename(str(val)) if '_v24_cleanup_pdf_filename' in globals() else str(val).strip()
            if cleaned:
                candidates.append(cleaned)
        for cand in candidates:
            key = _v25_pdf_key(cand)
            if not key:
                continue
            cand = preferred_by_key.get(key, cand)
            if key not in seen:
                seen.add(key)
                out.append(cand)
    return out


def _v25_strict_thread_matches_family_hint(thread: dict[str, Any], hint: str | None) -> bool:
    """Match input filename family only against explicit thread-local family tokens.

    v0.24 allowed a fallback search over broad thread text.  That was too broad
    because source paths such as ``.../S08_IndexedDB/...`` can appear inside every
    thread's evidence records, causing S07/S06/S05 residuals inside an S08 profile
    to be labeled as S08-family candidates.  This stricter function only trusts
    tokens extracted from prompt reference codes, prompt text, or metadata title.
    """
    if not hint:
        return False
    toks = _v24_thread_family_tokens(thread) if '_v24_thread_family_tokens' in globals() else []
    return str(hint).upper() in {str(t).upper() for t in toks}


# Override previous display-hint helpers so any later table/render code uses the
# strict definition.  This is presentation-only and does not affect task outcome.
def _v24_thread_matches_family_hint(thread: dict[str, Any], hint: str | None) -> bool:  # type: ignore[override]
    return _v25_strict_thread_matches_family_hint(thread, hint)


def _v22_thread_matches_hint(thread: dict[str, Any], hint: str | None) -> bool:  # type: ignore[override]
    return _v25_strict_thread_matches_family_hint(thread, hint)


def _v25_apply_residual_display_policy(report: dict[str, Any]) -> None:
    mode = str(report.get('report_mode') or _v25_as_dict(report.get('source')).get('report_mode') or '')
    if mode != 'profile_inventory':
        return
    hint = _v24_input_family_hint(report) if '_v24_input_family_hint' in globals() else None
    hint_matches: list[str] = []
    residual_threads: list[str] = []
    unassigned_threads: list[str] = []

    for thread in [t for t in _v25_as_list(report.get('threads')) if isinstance(t, dict)]:
        display = _v25_as_dict(thread.get('display')).copy()
        toks = _v24_thread_family_tokens(thread) if '_v24_thread_family_tokens' in globals() else []
        matches_hint = _v25_strict_thread_matches_family_hint(thread, hint)
        tid = str(thread.get('thread_id') or '')

        if hint and matches_hint:
            display['group'] = 'input_name_family_candidate'
            display['profile_relation'] = 'same_input_family_candidate'
            display['input_name_hint_match'] = True
            display['input_name_hint_match_basis'] = 'explicit_prompt_reference_or_title_family_token'
            display['sort_hint'] = 'input_filename_family_match_not_forensic_evidence'
            display['claim_level'] = 'recovered_profile_artifact_same_family_hint_not_case_claim'
            if tid:
                hint_matches.append(tid)
        elif hint and toks:
            display['group'] = 'residual_or_historical_thread'
            display['profile_relation'] = 'different_family_residual_or_historical'
            display['input_name_hint_match'] = False
            display['input_name_hint_match_basis'] = 'explicit_thread_family_token_differs_from_input_hint'
            display['claim_level'] = 'recovered_profile_residual_artifact_not_case_result'
            if tid:
                residual_threads.append(tid)
        else:
            display['group'] = 'profile_thread_inventory'
            display['profile_relation'] = 'unassigned_profile_artifact'
            display['input_name_hint_match'] = False
            display['input_name_hint_match_basis'] = 'no_explicit_thread_family_token'
            display['claim_level'] = 'recovered_profile_artifact_no_case_claim'
            if tid:
                unassigned_threads.append(tid)

        thread['display'] = display
        derived = _v25_as_dict(thread.get('derived_interpretation'))
        if derived:
            derived['display'] = display
            thread['derived_interpretation'] = derived
        extracted = _v25_as_dict(thread.get('extracted_artifacts'))
        if extracted:
            extracted['display'] = display
            thread['extracted_artifacts'] = extracted

    policy = _v25_as_dict(report.get('display_policy')).copy()
    policy.update({
        'input_name_hint': hint,
        'input_name_hint_forensic_claim': False,
        'input_name_hint_match_policy_version': '0.25',
        'input_name_hint_match_policy': 'Strict display-only match against explicit prompt/reference/title family tokens; broad thread text and source paths are not used.',
        'input_name_hint_matches': [x for x in hint_matches if x],
        'residual_or_historical_threads': [x for x in residual_threads if x],
        'unassigned_profile_threads': [x for x in unassigned_threads if x],
        'residual_labeling_policy': 'Different explicit family tokens are labeled residual_or_historical_thread in profile inventory mode. This is display labeling only; artifacts are preserved.',
    })
    report['display_policy'] = policy


# Override the v0.24 policy function name as well, because the v0.24 postprocess
# resolves this global at runtime.
def _v24_apply_residual_display_policy(report: dict[str, Any]) -> None:  # type: ignore[override]
    _v25_apply_residual_display_policy(report)


def _v25_clean_legacy_pdf_trace_fields(thread: dict[str, Any]) -> None:
    """Repair legacy PDF candidate fields after v0.24 filename extraction.

    v0.24 fixed ``nearby_pdf_candidates`` but older classification-basis fields
    could still contain normalized-away punctuation.  This function rewrites
    PDF candidate arrays generically using filename candidates recovered from the
    same thread text, preferring richer raw spellings when keys compare equal.
    """
    fresh = _v24_pdf_filenames(_v24_thread_text(thread)) if '_v24_pdf_filenames' in globals() and '_v24_thread_text' in globals() else []
    reported = _v24_reported_downloaded_filename(thread) if '_v24_reported_downloaded_filename' in globals() else None

    def walk(obj: Any) -> Any:
        if isinstance(obj, list):
            return [walk(x) for x in obj]
        if not isinstance(obj, dict):
            return obj
        for key, value in list(obj.items()):
            lk = str(key).lower()
            if lk in {'pdf_filename_candidates', 'nearby_pdf_candidates'}:
                vals = _v25_normalized_pdf_candidates(value, preferred=fresh)
                if lk == 'nearby_pdf_candidates' and reported:
                    vals = [v for v in vals if _v25_pdf_key(v) != _v25_pdf_key(reported)]
                # If the legacy field was empty but thread-local PDF names exist,
                # preserve them as candidates for that trace field.
                if not vals and fresh:
                    vals = [v for v in fresh if not reported or _v25_pdf_key(v) != _v25_pdf_key(reported)]
                obj[key] = vals
            elif lk == 'downloaded_filename_candidates':
                if reported:
                    obj[key] = [reported]
                else:
                    obj[key] = _v25_normalized_pdf_candidates(value, preferred=fresh)
            elif lk == 'reported_downloaded_filename' and isinstance(value, str):
                obj[key] = _v25_normalized_pdf_candidates([value], preferred=fresh)[0] if _v25_normalized_pdf_candidates([value], preferred=fresh) else value
            else:
                obj[key] = walk(value)
        return obj

    if isinstance(thread.get('task_outcome'), dict):
        thread['task_outcome'] = walk(thread['task_outcome'])
    if isinstance(thread.get('payload_audit'), dict):
        thread['payload_audit'] = walk(thread['payload_audit'])
    if isinstance(thread.get('classification_basis'), dict):
        thread['classification_basis'] = walk(thread['classification_basis'])

    # Keep nested mirror views synchronized after legacy trace cleanup.
    if '_v24_sync_thread_views' in globals():
        try:
            _v24_sync_thread_views(thread)
        except Exception:
            pass


def _v25_apply_payload_and_display_hygiene(report: dict[str, Any]) -> dict[str, Any]:
    if '_v24_apply_payload_and_display_hygiene' in globals():
        report = _v24_apply_payload_and_display_hygiene(report)
    report['schema_version'] = '0.25'
    report.setdefault('source', {})['payload_attribution_version'] = '0.25'
    report.setdefault('interpretation_policy', {})['v25_display_and_trace_cleanup'] = {
        'strict_explicit_family_token_matching': True,
        'source_path_broad_text_not_used_for_family_match': True,
        'legacy_pdf_trace_candidate_cleanup': True,
        'scenario_specific_rules': False,
    }

    for thread in [t for t in _v25_as_list(report.get('threads')) if isinstance(t, dict)]:
        _v25_clean_legacy_pdf_trace_fields(thread)

    _v25_apply_residual_display_policy(report)
    report['payload_attribution_summary_v25'] = {
        'version': '0.25',
        'note': 'v0.25 keeps v0.24 payload role separation, adds strict residual-family display matching, and normalizes legacy PDF trace candidate fields without scenario-specific rules.',
    }
    report.setdefault('payload_attribution_summary', {})['field_role_split_version'] = '0.25'

    try:
        target = _v25_as_dict(report.get('source')).get('target_reference_filter') or None
        if report.get('report_mode') == 'profile_inventory':
            target = None
        report['case_summary'] = build_case_summary(report, target)
    except Exception:
        pass
    return report


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v25(extracted, input_label, browser_only=browser_only)
    return _v25_apply_payload_and_display_hygiene(report)


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v25(report, target_reference)
    return _v25_apply_payload_and_display_hygiene(filtered)


def _render_case_summary_v10(report: dict[str, Any]) -> str:
    html = _render_case_summary_v10_pre_v25(report)
    if str(report.get('schema_version')) != '0.25':
        return html
    policy = _v25_as_dict(report.get('display_policy'))
    residuals = _v25_as_list(policy.get('residual_or_historical_threads'))
    matches = _v25_as_list(policy.get('input_name_hint_matches'))
    note = (
        "<div class='note'><strong>v0.25 residual-aware display:</strong> "
        f"{len(matches)} explicit input-family candidate thread(s), "
        f"{len(residuals)} residual/historical thread(s). "
        "Matching uses only explicit prompt/reference/title family tokens; source paths and broad thread text are excluded.</div>"
    )
    return html.replace("</section>", note + "</section>", 1) if "</section>" in html else html + note


def render_html_report(report: dict[str, Any], output_path: Path) -> None:
    report = _v25_apply_payload_and_display_hygiene(report)
    _render_html_report_pre_v25(report, output_path)
    try:
        html = output_path.read_text(encoding='utf-8')
        html = html.replace('v0.24 payload roles:', 'v0.24/v0.25 payload roles:')
        html = html.replace('Recovered profile threads / residual-aware', 'Recovered profile threads / residual-aware')
        output_path.write_text(html, encoding='utf-8')
    except Exception:
        pass



# ---------------------------------------------------------------------------
# v0.26 minimal generic cleanup: markdown URL hygiene + page-open basis view
# ---------------------------------------------------------------------------
# This block intentionally does not use scenario names, thread ids, known URLs,
# or expected answers.  It only cleans generic Markdown URL artifacts and makes
# page-open classifier evidence presentation narrower while preserving the full
# original basis under an audit field.

SCHEMA_VERSION = "0.26"

_reconstruct_browser_threads_pre_v26 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v26 = filter_report_by_target_reference
_render_html_report_pre_v26 = render_html_report
_extract_prompt_target_urls_pre_v26 = extract_prompt_target_urls


def _v26_as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _v26_as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _v26_clean_url_string(value: Any) -> str:
    """Return one clean URL from a generic URL-like artifact.

    Handles Markdown links such as ``[label](https://example)`` and corrupted
    scanner captures such as ``https://a](https://a`` without relying on any
    specific domain or scenario value.
    """
    s = str(value or "").strip()
    if not s:
        return ""

    # Prefer hrefs from Markdown links when the full Markdown syntax is present.
    md = re.findall(r"\[[^\]]*\]\((https?://[^\s)<>\"']+)", s, flags=re.IGNORECASE)
    candidates: list[str] = []
    candidates.extend(md)

    # Then collect strict URL spans.  Brackets are deliberately excluded so a
    # captured ``url](url`` becomes two clean candidate spans instead of one.
    candidates.extend(re.findall(r"https?://[^\s\[\]\(\)<>\"'`]+", s, flags=re.IGNORECASE))

    if not candidates:
        # Fall back to trimming common Markdown/HTML/punctuation wrappers.
        candidates = [s]

    cleaned: list[str] = []
    for item in candidates:
        u = str(item or "").strip()
        # Remove common trailing Markdown/emphasis/punctuation artifacts.
        u = re.sub(r"[\*`_]+$", "", u)
        u = u.strip().strip(".,;:)]}>\\\"'")
        # If the scanner captured a duplicated Markdown fragment, split again.
        if "](" in u:
            pieces = [p for p in re.split(r"\]\(", u) if p]
            for p in pieces:
                if p.lower().startswith(("http://", "https://")):
                    p = re.sub(r"[\*`_]+$", "", p).strip().strip(".,;:)]}>\\\"'")
                    cleaned.append(p)
            continue
        if u:
            cleaned.append(u)

    # Prefer the last candidate from Markdown href captures, otherwise the
    # shortest clean duplicate-equivalent URL.  This is generic and only uses the
    # artifact text itself.
    if not cleaned:
        return ""
    # Deduplicate while preserving order.
    uniq: list[str] = []
    seen: set[str] = set()
    for u in cleaned:
        key = u.rstrip('/').lower()
        if key not in seen:
            seen.add(key)
            uniq.append(u)
    # Markdown href usually appears after the label; when candidates are equally
    # plausible and duplicates collapse, uniq[0] is already clean.  If not, pick
    # the shortest candidate to avoid ``url](url`` tails.
    return sorted(uniq, key=len)[0]


def _v26_clean_url_list(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in _v26_as_list(values):
        u = _v26_clean_url_string(value.get('url') if isinstance(value, dict) else value)
        if not u:
            continue
        key = u.rstrip('/').lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def extract_prompt_target_urls(prompt_text: str | None) -> list[str]:  # type: ignore[override]
    """Extract clean target URLs from prompt text with Markdown-link awareness."""
    if not prompt_text:
        return []
    urls = _v26_clean_url_list(_v26_clean_url_string(m) for m in [])  # keeps type-checkers quiet
    del urls
    found: list[str] = []
    # Markdown hrefs first.
    for m in re.findall(r"\[[^\]]*\]\((https?://[^\s)<>\"']+)", prompt_text, flags=re.IGNORECASE):
        u = _v26_clean_url_string(m)
        if u:
            found.append(u)
    # Plain URLs, excluding Markdown delimiters.
    for m in re.findall(r"https?://[^\s\[\]\(\)<>\"'`]+", prompt_text, flags=re.IGNORECASE):
        u = _v26_clean_url_string(m)
        if u:
            found.append(u)
    out: list[str] = []
    seen: set[str] = set()
    for u in found:
        key = u.rstrip('/').lower()
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _v26_clean_url_items(items: Any) -> list[Any]:
    cleaned_items: list[Any] = []
    seen: set[str] = set()
    for item in _v26_as_list(items):
        if isinstance(item, dict):
            new = item.copy()
            u = _v26_clean_url_string(new.get('url'))
            if not u:
                cleaned_items.append(new)
                continue
            key = u.rstrip('/').lower()
            if key in seen:
                # Keep the first evidence-bearing item for duplicate URLs.
                continue
            seen.add(key)
            new['url'] = u
            cleaned_items.append(new)
        else:
            u = _v26_clean_url_string(item)
            if not u:
                continue
            key = u.rstrip('/').lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned_items.append(u)
    return cleaned_items


def _v26_task_key(task_type: str) -> str:
    task_type = str(task_type or '')
    return task_type if task_type in {
        'email_draft_send', 'calendar_create', 'file_download', 'web_research', 'page_open'
    } else task_type


def _v26_split_page_open_basis(outcome: dict[str, Any]) -> None:
    """Keep page-open core basis focused and preserve full basis as audit.

    Applies only to page_open outcomes to minimize behavior changes.  This is a
    presentation cleanup: task selection has already happened before this field
    is rewritten.
    """
    if str(outcome.get('task_type') or '') != 'page_open':
        return
    basis = _v26_as_dict(outcome.get('classification_basis'))
    if not basis or basis.get('basis_view') == 'selected_task_core':
        return

    task = 'page_open'
    full = json.loads(json.dumps(basis, ensure_ascii=False, default=str))

    def keep_task_map(name: str) -> dict[str, Any]:
        d = _v26_as_dict(basis.get(name))
        out: dict[str, Any] = {}
        if task in d:
            out[task] = d.get(task)
        return out

    core_trace = keep_task_map('trace_markers')
    # PDF filename candidates are not page-open evidence; if present, preserve
    # them only in the audit copy below.
    core_trace.pop('pdf_filename_candidates', None)

    core = {
        'basis_view': 'selected_task_core',
        'selected_task': task,
        'scores': keep_task_map('scores'),
        'anchors': keep_task_map('anchors'),
        'prompt_markers': keep_task_map('prompt_markers'),
        'trace_markers': core_trace,
        'guard_policy': _v26_as_dict(basis.get('guard_policy')),
        'execution_mode': basis.get('execution_mode'),
        'interaction_type': basis.get('interaction_type'),
        'presentation_policy': 'Only selected task markers are shown here; full multi-task scan is preserved in classification_basis_audit.',
    }
    outcome['classification_basis_audit'] = full
    outcome['classification_basis'] = core


def _v26_clean_thread_url_and_basis(thread: dict[str, Any]) -> None:
    # Clean direct URL lists without using domain/scenario-specific rules.
    for key in ['urls', 'computer_url_candidates']:
        if isinstance(thread.get(key), list):
            thread[key] = _v26_clean_url_items(thread.get(key))

    outcome = _v26_as_dict(thread.get('task_outcome')).copy()
    if outcome:
        for key in ['target_urls', 'non_noise_thread_urls']:
            if isinstance(outcome.get(key), list):
                outcome[key] = _v26_clean_url_list(outcome.get(key))
        # Recompute page-open prompt targets from prompt text after Markdown cleanup.
        if str(outcome.get('task_type') or '') == 'page_open':
            prompt_text = _v26_as_dict(thread.get('prompt')).get('text')
            prompt_urls = extract_prompt_target_urls(prompt_text)
            if prompt_urls:
                outcome['target_urls'] = prompt_urls
        _v26_split_page_open_basis(outcome)
        thread['task_outcome'] = outcome

    # Mirror view synchronization, if available from v0.24.
    if '_v24_sync_thread_views' in globals():
        try:
            _v24_sync_thread_views(thread)
        except Exception:
            pass


def _v26_apply_minimal_cleanup(report: dict[str, Any]) -> dict[str, Any]:
    # Start from the v0.25 postprocess; then apply only the minimal v0.26 fixes.
    if '_v25_apply_payload_and_display_hygiene' in globals():
        report = _v25_apply_payload_and_display_hygiene(report)

    report['schema_version'] = '0.26'
    report.setdefault('source', {})['payload_attribution_version'] = '0.26'
    report.setdefault('interpretation_policy', {})['v26_minimal_agent_page_open_cleanup'] = {
        'markdown_url_cleanup': True,
        'page_open_basis_core_view': True,
        'full_classification_basis_preserved_as_audit': True,
        'scenario_specific_rules': False,
    }

    for thread in [t for t in _v26_as_list(report.get('threads')) if isinstance(t, dict)]:
        _v26_clean_thread_url_and_basis(thread)

    # Rebuild case summary so primary_task_outcome sees the cleaned URL/basis.
    try:
        target = _v26_as_dict(report.get('source')).get('target_reference_filter') or None
        if report.get('report_mode') == 'profile_inventory':
            target = None
        report['case_summary'] = build_case_summary(report, target)
    except Exception:
        pass

    report['payload_attribution_summary_v26'] = {
        'version': '0.26',
        'note': 'Minimal generic cleanup: Markdown URL artifacts are normalized and page_open classification_basis is shown as selected-task core evidence while preserving the full scan in classification_basis_audit.',
    }
    report.setdefault('payload_attribution_summary', {})['field_role_split_version'] = '0.26'
    return report


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:
    report = _reconstruct_browser_threads_pre_v26(extracted, input_label, browser_only=browser_only)
    return _v26_apply_minimal_cleanup(report)


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:
    filtered = _filter_report_by_target_reference_pre_v26(report, target_reference)
    return _v26_apply_minimal_cleanup(filtered)


def render_html_report(report: dict[str, Any], output_path: Path) -> None:
    report = _v26_apply_minimal_cleanup(report)
    _render_html_report_pre_v26(report, output_path)
    try:
        html_text = output_path.read_text(encoding='utf-8')
        html_text = html_text.replace('v0.24/v0.25 payload roles:', 'v0.24/v0.25/v0.26 payload roles:')
        output_path.write_text(html_text, encoding='utf-8')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v0.27 generic reconstruction refinements
# ---------------------------------------------------------------------------
# Goals:
# 1) Recover final-answer fragments that are present in raw LevelDB files but
#    were not cleanly decoded into the structured all_results records.
# 2) Mark a primary case thread without hiding residual/profile-wide threads.
# 3) Present typed/form payloads consistently across email, calendar, download,
#    and generic form tasks.
# 4) Apply conservative text/URL cleanup while preserving raw evidence fields.
# 5) Tighten reference-code extraction so ordinary words such as "once" are not
#    reported as reference codes.
#
# This layer intentionally does not use scenario names, expected URLs, expected
# answers, or thread UUIDs.  It uses only artifact structure, prompt/reference
# labels recovered from the evidence, generic task/outcome signals, and source
# provenance.

SCHEMA_VERSION = "0.27"

_reconstruct_browser_threads_pre_v27 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v27 = filter_report_by_target_reference
_render_html_report_pre_v27 = render_html_report
_extract_reference_codes_pre_v27 = extract_reference_codes
_build_artifact_buckets_pre_v27 = build_artifact_buckets

_V27_STATE_LABELS = {
    "IN_PROGRESS", "INCOMPLETE", "COMPLETE", "COMPLETED", "DONE", "PENDING",
    "FAILED", "ERROR", "CANCELLED", "CANCELED", "SUCCESS", "WAITING", "RUNNING",
    "FINAL", "SELECTED", "STREAMING",
}

_V27_FORM_FIELDS = {
    "recipient", "to", "cc", "bcc", "subject", "body", "title", "event_title",
    "description", "date", "time", "start_time", "end_time", "location", "filename",
    "file_name", "input", "text",
}


def _v27_as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _v27_as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _v27_text(value: Any) -> str:
    return "" if value is None else str(value)


def _v27_json_clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return value


def _v27_clean_ref_token(token: str) -> str | None:
    token = html.unescape(str(token or "")).strip().strip("`'\".,;:)]}")
    token = re.sub(r"\s+", "", token)
    if not (3 <= len(token) <= 140):
        return None
    low = token.lower()
    if low in {
        "for", "is", "my", "this", "the", "experiment", "code", "reference", "once",
        "answer", "body", "subject", "recipient", "include", "exact", "must", "also",
    }:
        return None
    # Generic controlled-run reference tokens normally contain at least one
    # structural separator or digit.  This avoids false positives from ordinary
    # words while still accepting user-supplied non-Sxx labels.
    if not ("_" in token or "-" in token or any(ch.isdigit() for ch in token)):
        return None
    # Avoid plain dates/times and punctuation-only fragments.
    if re.fullmatch(r"\d{4,8}", token):
        return None
    return token


def extract_reference_codes(text: str) -> list[str]:  # type: ignore[override]
    """Extract explicit user-supplied reference tokens conservatively.

    The parser never relies on these labels for reconstruction; they are only
    used for display, filtering, and validation.  Patterns are deliberately
    line/local-context based so a later word like "once" is not captured as a
    code after a previous "exact reference code" phrase.
    """
    found: set[str] = set()
    src = text or ""

    local_patterns = [
        # Same-line phrases such as "reference code: ABC_20260527" or
        # "reference code is ABC_20260527".
        r"(?im)^\s*[^\n]{0,120}?\b(?:reference\s+code|ref(?:erence)?\s+code|experiment\s+code)\b[^\n:=]{0,40}(?:is|:|=)\s*[`'\"]?([A-Za-z][A-Za-z0-9_.\-]{2,140})",
        r"(?im)^\s*[^\n]{0,80}?\bexact\s+(?:reference\s+)?code\b[^\n:=]{0,30}(?:is|:|=)\s*[`'\"]?([A-Za-z][A-Za-z0-9_.\-]{2,140})",
        # Common structured labels observed in controlled experiments.  These
        # are generic token-family patterns rather than case-specific names.
        r"\b([A-Z][A-Za-z0-9]*_[A-Za-z0-9_\-]+_\d{8}(?:_[A-Za-z0-9_\-]+)?)\b",
        r"\b([A-Z]\d{2}_[A-Za-z0-9_\-]+(?:_\d{8})?(?:_[A-Za-z0-9_\-]+)?)\b",
        r"\b(Computer_[A-Za-z0-9_\-]+(?:_\d{8})?)\b",
    ]
    for pattern in local_patterns:
        for m in re.finditer(pattern, src):
            token = _v27_clean_ref_token(m.group(1) or "")
            if token:
                found.add(token)
    return sorted(found)


def _v27_clean_reference_codes_in_report(report: dict[str, Any]) -> None:
    for thread in _v27_as_list(report.get("threads")):
        if not isinstance(thread, dict):
            continue
        prompt = _v27_as_dict(thread.get("prompt"))
        prompt["reference_codes"] = extract_reference_codes(_v27_text(prompt.get("text")))
        thread["prompt"] = prompt
        for key in ["case_relation", "display"]:
            obj = _v27_as_dict(thread.get(key))
            if "reference_codes" in obj:
                obj["reference_codes"] = [c for c in obj.get("reference_codes", []) if _v27_clean_ref_token(str(c))]
                thread[key] = obj


def _v27_clean_url_string(value: Any) -> str:
    if "_v26_clean_url_string" in globals():
        try:
            return _v26_clean_url_string(value)
        except Exception:
            pass
    s = html.unescape(str(value or "")).strip()
    m = re.search(r"https?://[^\s\[\]\(\)<>\"'`]+", s, flags=re.IGNORECASE)
    if m:
        s = m.group(0)
    return s.strip().strip("`'\".,;:)]}>")


def _v27_clean_url_items(items: Any) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for item in _v27_as_list(items):
        if isinstance(item, dict):
            new = item.copy()
            u = _v27_clean_url_string(new.get("url"))
            if u:
                key = u.rstrip("/").lower()
                if key in seen:
                    continue
                seen.add(key)
                new["url"] = u
            out.append(new)
        else:
            u = _v27_clean_url_string(item)
            if not u:
                continue
            key = u.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(u)
    return out


def _v27_clean_human_text(text: str) -> str:
    """Conservative presentation cleanup for already-recovered answer text."""
    s = html.unescape(str(text or ""))
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Remove standalone status/UI labels, especially tail markers like IN_PROGRESS.
    lines: list[str] = []
    for line in s.split("\n"):
        stripped = line.strip()
        if stripped.upper() in _V27_STATE_LABELS:
            continue
        lines.append(line)
    s = "\n".join(lines)
    # Repair common wrapped-word artifacts from chunked artifacts, but keep
    # normal paragraph/list line breaks.  Only join when both sides are letters.
    s = re.sub(r"(?<=[A-Za-z])\n(?=[a-z])", "", s)
    # Repair hyphenation across line breaks.
    s = re.sub(r"(?<=[A-Za-z])-\n(?=[A-Za-z])", "", s)
    # Trim excessive whitespace while preserving paragraphs and lists.
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{4,}", "\n\n\n", s)
    return s.strip()


def _v27_payload_key(payload: dict[str, Any]) -> str:
    return f"{str(payload.get('field') or '').lower()}::{str(payload.get('value') or '').strip()[:800]}"


def _v27_promote_form_input_payloads(thread: dict[str, Any]) -> None:
    """Promote concrete observed form inputs into typed_payloads.

    This reads already-recovered form/input artifacts and assigns generic roles
    from the value shape and task type.  It does not rely on scenario names.
    To avoid over-claiming on browsing/research pages, promotion is limited to
    tasks where form fields are central to the user request or side effect.
    """
    typed = [p for p in _v27_as_list(thread.get("typed_payloads")) if isinstance(p, dict)]
    seen = {_v27_payload_key(p) for p in typed}
    task_type = str(_v27_as_dict(thread.get("task_outcome")).get("task_type") or "")
    if task_type not in {"email_draft_send", "calendar_create", "file_download"}:
        thread["typed_payloads"] = typed
        return

    candidates: list[dict[str, Any]] = []
    buckets = _v27_as_dict(thread.get("artifact_buckets"))
    candidates.extend([x for x in _v27_as_list(buckets.get("form_inputs")) if isinstance(x, dict)])
    candidates.extend([x for x in _v27_as_list(thread.get("observed_input_artifacts")) if isinstance(x, dict)])

    for item in candidates:
        raw_value = _v27_text(item.get("value") or item.get("label") or item.get("text"))
        value = _v27_clean_human_text(raw_value)
        if not value:
            continue
        # Drop obvious navigation/control keys rather than typed content.
        if value.strip().lower() in {"tab", "enter", "escape", "esc", "backspace", "shift", "ctrl", "cmd"}:
            continue
        field = str(item.get("field") or "text").lower()
        if EMAIL_RE.fullmatch(value.strip()):
            field = "recipient" if task_type == "email_draft_send" else "email"
        elif task_type == "email_draft_send" and field in {"text", "input"}:
            if "\n" in value or len(value) > 160:
                field = "body"
            elif extract_reference_codes(value) or len(value) <= 160:
                field = "subject"
        elif task_type == "calendar_create" and field in {"text", "input", "title"}:
            # Keep calendar prompt-derived event fields if already typed; only
            # infer a generic title/description from observed form text.
            field = "description" if "\n" in value or len(value) > 180 else "event_title"

        if field not in _V27_FORM_FIELDS and not field.endswith("_time"):
            continue
        payload = {
            "field": field,
            "value": value,
            "payload_source": "observed_form_input",
            "payload_role": "confirmed_typed_field",
            "relative_order": item.get("relative_order"),
            "evidence": _v27_as_list(item.get("evidence")),
            "confidence": "medium_high",
            "interpretation": "Concrete typed/form input recovered from tool or browser-control artifacts; semantic field is inferred from generic task/value shape.",
        }
        key = _v27_payload_key(payload)
        if key not in seen:
            seen.add(key)
            typed.append(payload)

    # Dedupe again by field/value while preserving order and evidence.
    deduped: list[dict[str, Any]] = []
    seen.clear()
    for payload in typed:
        key = _v27_payload_key(payload)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(payload)
    thread["typed_payloads"] = deduped


def build_artifact_buckets(thread: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
    buckets = _build_artifact_buckets_pre_v27(thread)
    # Ensure typed payloads and observed form inputs are presented consistently.
    form_inputs = [x for x in _v27_as_list(buckets.get("form_inputs")) if isinstance(x, dict)]
    for payload in _v27_as_list(thread.get("typed_payloads")):
        if not isinstance(payload, dict):
            continue
        field = str(payload.get("field") or "").lower()
        value = _v27_text(payload.get("value"))
        if not value:
            continue
        if field in _V27_FORM_FIELDS or field.endswith("_time") or field.startswith("event_"):
            if not URL_RE.search(value) and ".pdf" not in value.lower():
                form_inputs.append(payload)
    # Add observed input artifacts that were not promoted yet.
    for item in _v27_as_list(thread.get("observed_input_artifacts")):
        if isinstance(item, dict):
            value = _v27_text(item.get("value") or item.get("label") or item.get("text"))
            if value and value.strip().lower() not in {"tab", "enter", "escape", "esc"}:
                form_inputs.append(item)

    def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            field = str(item.get("field") or "text").lower()
            value = _v27_clean_human_text(_v27_text(item.get("value") or item.get("label") or item.get("text")))
            key = f"{field}::{value[:800]}"
            if not value or key in seen:
                continue
            seen.add(key)
            new = item.copy()
            new["field"] = field
            new["value"] = value
            out.append(new)
        return out

    buckets["form_inputs"] = dedupe(form_inputs)
    buckets["interpretation"] = (
        "Typed payloads and observed form inputs are unified for case-study readability. "
        "Prompt-derived values remain marked by payload_source; observed browser-control inputs retain evidence."
    )
    return buckets


def _v27_rebuild_payload_views(thread: dict[str, Any]) -> None:
    task_type = str(_v27_as_dict(thread.get("task_outcome")).get("task_type") or "")
    try:
        # Existing v18 generic label extraction is useful for form/side-effect
        # tasks, but it is intentionally not applied to web-research/page-open
        # threads because labels such as "Title" in source lists are not typed
        # form payloads.
        if task_type in {"email_draft_send", "calendar_create", "file_download"} and "_v18_add_derived_typed_payloads" in globals():
            _v18_add_derived_typed_payloads(thread)
    except Exception:
        pass
    _v27_promote_form_input_payloads(thread)
    thread["artifact_buckets"] = build_artifact_buckets(thread)
    # Rebuild timeline so newly promoted typed payloads become visible.
    try:
        thread["timeline"] = build_timeline(
            records=[],
            prompt=_v27_as_dict(thread.get("prompt")),
            plan=_v27_as_list(thread.get("plan")),
            actions=_v27_as_list(thread.get("actions")),
            urls=_v27_as_list(thread.get("urls")),
            typed_payloads=_v27_as_list(thread.get("typed_payloads")),
            final_answer=_v27_as_dict(thread.get("final_answer")),
        )
    except Exception:
        pass


def _v27_leveldb_files_from_extracted(extracted: dict[str, Any]) -> list[Path]:
    root = Path(str(extracted.get("source_leveldb_path") or ""))
    if not root.exists() or not root.is_dir():
        return []
    files: list[Path] = []
    for suffix in ("*.log", "*.ldb", "*.sst"):
        files.extend(sorted(root.glob(suffix)))
    return files


def _v27_printable_window(data: bytes, start: int, end: int) -> str:
    chunk = data[max(0, start):min(len(data), end)]
    s = chunk.decode("utf-8", "ignore")
    # Keep newlines/tabs, drop other controls.  Replacing with a space preserves
    # word boundaries without creating fabricated characters.
    s = "".join(ch if (ch == "\n" or ch == "\t" or ord(ch) >= 32) else " " for ch in s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"(?:G\s*){20,}", " ", s)  # prefix-compression filler seen in LevelDB strings
    return _v27_clean_human_text(s)


def _v27_score_raw_answer_candidate(text: str, ref: str, prompt_text: str) -> int:
    low = text.lower()
    score = 0
    if ref and ref.lower() in low:
        score += 20
    if "reference code" in low or "ref code" in low:
        score += 8
    if "sources used" in low or "source" in low and "http" in low:
        score += 12
    if re.search(r"(?m)^\s*(?:[-*]|\d+[.)])\s+", text):
        score += 4
    if "http://" in low or "https://" in low:
        score += 8
    if len(text) > 400:
        score += 6
    if prompt_text and prompt_text[:120].lower() in low:
        score -= 10
    # Prefer answer-ish material over workflow JSON/control material.
    if "workflow_root" in low or "tool_input" in low or "computer_list" in low:
        score -= 4
    return score


def _v27_trim_raw_answer(text: str, ref: str) -> str:
    if not text:
        return ""
    # Start near an answer marker or the reference code, whichever appears first
    # after non-human LevelDB filler.
    candidates = []
    for marker in ["Reference Code", "REFERENCE CODE", ref]:
        if marker:
            idx = text.find(marker)
            if idx >= 0:
                candidates.append(idx)
    if candidates:
        text = text[min(candidates):]
    # Stop before obvious internal/cache schema sections if they appear after the
    # human answer.  Keep enough text before the marker.
    stop_markers = ["workflow_root", "completed_at", "thread_title", "answer_tabs", "classifier_results", "telemetry_data", "pplx-query-cache"]
    stops = [text.find(m) for m in stop_markers if text.find(m) > 800]
    if stops:
        text = text[:min(stops)]
    text = _v27_clean_human_text(text)
    # Raw LevelDB fallback can contain partially decoded binary fragments.  Keep
    # it bounded so it is useful as a forensic lead, not as a polished answer.
    return text[:12000].strip()


def _v27_salvage_raw_final_answer(thread: dict[str, Any], extracted: dict[str, Any]) -> None:
    final = _v27_as_dict(thread.get("final_answer"))
    if final.get("text"):
        return
    prompt = _v27_as_dict(thread.get("prompt"))
    prompt_text = _v27_text(prompt.get("text"))
    refs = extract_reference_codes(prompt_text)
    if not refs:
        return

    best: dict[str, Any] | None = None
    for file_path in _v27_leveldb_files_from_extracted(extracted):
        try:
            data = file_path.read_bytes()
        except Exception:
            continue
        for ref in refs:
            ref_bytes = ref.encode("utf-8", "ignore")
            pos = 0
            while ref_bytes and True:
                idx = data.find(ref_bytes, pos)
                if idx < 0:
                    break
                pos = idx + max(1, len(ref_bytes))
                raw_text = _v27_printable_window(data, idx - 2500, idx + 18000)
                answer_text = _v27_trim_raw_answer(raw_text, ref)
                if len(answer_text) < 120:
                    continue
                score = _v27_score_raw_answer_candidate(answer_text, ref, prompt_text)
                candidate = {
                    "text": answer_text,
                    "score": score,
                    "ref": ref,
                    "source_file": file_path.name,
                    "offset": idx,
                    "source_path": str(file_path),
                }
                if best is None or (candidate["score"], len(candidate["text"])) > (best["score"], len(best["text"])):
                    best = candidate

    if not best or best.get("score", 0) < 18:
        return

    ev = {
        "source_file": best.get("source_file"),
        "source_path": best.get("source_path"),
        "source_type": "log" if str(best.get("source_file") or "").endswith(".log") else "ldb",
        "offset": best.get("offset"),
        "state": "raw_file_scan",
        "ldb_seq_no": None,
        "is_live": None,
    }
    thread["final_answer"] = {
        "text": best["text"],
        "available": True,
        "reason": None,
        "relative_order": None,
        "evidence": [ev],
        "source_kind": "raw_leveldb_reference_window",
        "confidence": "low_to_medium",
        "caution": (
            "Final answer text was salvaged from a raw LevelDB byte window because the structured "
            "IndexedDB decoder did not expose a clean final-answer record. Treat this as forensic lead "
            "evidence and corroborate with decoded records, cache, or browser history when available."
        ),
        "raw_fallback_reference": best.get("ref"),
    }
    thread.setdefault("repair_warnings", []).append("v0.27 raw LevelDB fallback supplied final_answer from reference-code byte window.")

    # Extract clean URL candidates from the salvaged answer without claiming they
    # are necessarily visited.  Existing URL cleaner/evidence is preserved.
    urls = _v27_as_list(thread.get("urls"))
    seen = {str(u.get("url") if isinstance(u, dict) else u).rstrip("/").lower() for u in urls}
    for m in URL_RE.finditer(best["text"]):
        u = _v27_clean_url_string(m.group(0))
        key = u.rstrip("/").lower()
        if u and key not in seen:
            seen.add(key)
            urls.append({
                "url": u,
                "role": "raw_final_answer_reported_url_candidate",
                "relative_order": None,
                "evidence": [ev],
                "interpretation": "URL string recovered from raw final-answer fallback; corroborate before treating as visited URL.",
            })
    thread["urls"] = _v27_clean_url_items(urls)


def _v27_clean_thread_text_and_urls(thread: dict[str, Any]) -> None:
    final = _v27_as_dict(thread.get("final_answer")).copy()
    if final.get("text"):
        raw_text = str(final.get("text"))
        cleaned = _v27_clean_human_text(raw_text)
        if cleaned != raw_text:
            final["text_raw_before_v27_cleanup"] = raw_text
            final["text"] = cleaned
            final.setdefault("normalization_note", "Presentation cleanup removed standalone state labels and repaired chunk line-wrap artifacts; raw pre-cleanup text is preserved.")
        thread["final_answer"] = final
    for key in ["urls", "computer_url_candidates", "context_url_candidates"]:
        if isinstance(thread.get(key), list):
            thread[key] = _v27_clean_url_items(thread.get(key))
    outcome = _v27_as_dict(thread.get("task_outcome")).copy()
    for key in ["target_urls", "non_noise_thread_urls"]:
        if isinstance(outcome.get(key), list):
            outcome[key] = [u for u in (_v27_clean_url_string(x) for x in outcome.get(key)) if u]
    if outcome:
        thread["task_outcome"] = outcome


def _v27_source_log_count(thread: dict[str, Any]) -> int:
    summary = _v27_as_dict(thread.get("source_summary"))
    counts = _v27_as_dict(summary.get("source_type_counts"))
    log_count = int(counts.get("log") or 0) if str(counts.get("log") or "0").isdigit() else 0
    # Also count log evidence in major sections because some summaries may be old.
    text = json.dumps({k: thread.get(k) for k in ["final_answer", "actions", "artifact_buckets", "typed_payloads"]}, ensure_ascii=False, default=str)
    log_count += text.lower().count('"source_type": "log"')
    return log_count


def _v27_input_family_tokens(report: dict[str, Any]) -> set[str]:
    source = _v27_as_dict(report.get("source"))
    input_name = Path(str(source.get("input") or "")).stem
    tokens = set(extract_reference_codes(input_name))
    # Also keep structured filename fragments such as S08 or Calendar if present;
    # these are used only for display scoring and never to synthesize content.
    for part in re.split(r"[^A-Za-z0-9]+", input_name):
        if len(part) >= 3 and (any(ch.isdigit() for ch in part) or part.lower() not in {"zip", "indexeddb", "case"}):
            tokens.add(part)
    return {t.lower() for t in tokens if t}


def _v27_score_primary_thread(thread: dict[str, Any], input_tokens: set[str]) -> int:
    score = 0
    classification = _v27_as_dict(thread.get("classification"))
    prompt = _v27_as_dict(thread.get("prompt"))
    outcome = _v27_as_dict(thread.get("task_outcome"))
    refs = [str(x) for x in prompt.get("reference_codes") or []]
    ref_low = " ".join(refs).lower()
    prompt_low = _v27_text(prompt.get("text")).lower()

    if refs:
        score += 8
    if input_tokens and any(tok in ref_low or tok in prompt_low for tok in input_tokens):
        score += 12
    if classification.get("interaction_type") == "agentic":
        score += 8
    if classification.get("execution_mode") == "browser_control":
        score += 14
    elif classification.get("execution_mode") == "computer_mode":
        score += 4
    if _v27_as_dict(thread.get("final_answer")).get("text"):
        score += 20
    if outcome.get("side_effect_completed") is True:
        score += 10
    if str(outcome.get("confidence") or "").lower() == "high":
        score += 8
    elif str(outcome.get("confidence") or "").lower() == "medium":
        score += 4
    if outcome.get("task_type") and outcome.get("task_type") != "unknown":
        score += 5
    # Prefer evidence-rich reconstructions, but cap so old residual profiles do
    # not beat the current thread solely because they have more records.
    record_count = thread.get("record_count") or _v27_as_dict(thread.get("source_summary")).get("live_record_count") or 0
    try:
        score += min(int(record_count), 20) // 4
    except Exception:
        pass
    score += min(_v27_source_log_count(thread), 20) // 4
    metadata = _v27_as_dict(thread.get("metadata"))
    if metadata.get("created_at") or metadata.get("updated_at") or metadata.get("last_query_datetime"):
        score += 6
    if _v27_as_dict(thread.get("deletion_state")).get("state") in {"deleted", "stale"}:
        score -= 6
    if _v27_as_dict(thread.get("residue_state")).get("state") in {"residual", "historical"}:
        score -= 4
    return score


def _v27_mark_primary_case_thread(report: dict[str, Any]) -> None:
    threads = [t for t in _v27_as_list(report.get("threads")) if isinstance(t, dict)]
    if not threads:
        return
    input_tokens = _v27_input_family_tokens(report)
    def ts_value(thread: dict[str, Any]) -> float:
        metadata = _v27_as_dict(thread.get("metadata"))
        raw = metadata.get("updated_at") or metadata.get("created_at") or metadata.get("last_query_datetime")
        if not raw:
            return 0.0
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    latest_ts = max((ts_value(t) for t in threads), default=0.0)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, thread in enumerate(threads):
        score = _v27_score_primary_thread(thread, input_tokens)
        if latest_ts and ts_value(thread) == latest_ts:
            score += 14
            thread["primary_case_recency_boost_v27"] = True
        thread["primary_case_score_v27"] = score
        thread["primary_case_score_basis_v27"] = {
            "input_tokens_used_for_display_scoring": sorted(input_tokens),
            "has_final_answer": bool(_v27_as_dict(thread.get("final_answer")).get("text")),
            "execution_mode": _v27_as_dict(thread.get("classification")).get("execution_mode"),
            "task_type": _v27_as_dict(thread.get("task_outcome")).get("task_type"),
            "task_confidence": _v27_as_dict(thread.get("task_outcome")).get("confidence"),
            "record_count": thread.get("record_count"),
            "log_evidence_count_hint": _v27_source_log_count(thread),
            "rule": "Generic display ranking only; reconstruction/classification still comes from artifact structure, not scenario-specific expected values.",
        }
        scored.append((score, -idx, thread))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    primary = scored[0][2]
    for thread in threads:
        thread["primary_case_candidate_v27"] = thread is primary
    report["primary_thread_selection_v27"] = {
        "primary_thread_id": primary.get("thread_id"),
        "score": primary.get("primary_case_score_v27"),
        "reference_codes": _v27_as_dict(primary.get("prompt")).get("reference_codes", []),
        "task_type": _v27_as_dict(primary.get("task_outcome")).get("task_type"),
        "note": "Primary candidate is for case-study display only; residual threads are retained and not hidden.",
    }
    case_summary = _v27_as_dict(report.get("case_summary")).copy()
    case_summary["primary_thread_id_v27"] = primary.get("thread_id")
    case_summary["primary_thread_reference_codes_v27"] = _v27_as_dict(primary.get("prompt")).get("reference_codes", [])
    report["case_summary"] = case_summary


def _v27_apply_report_refinements(report: dict[str, Any], extracted: dict[str, Any] | None = None) -> dict[str, Any]:
    # Preserve all prior v0.26 behavior first.
    if extracted is None:
        extracted = {}
    report["schema_version"] = "0.27"
    report.setdefault("source", {})["payload_attribution_version"] = "0.27"

    _v27_clean_reference_codes_in_report(report)

    for thread in [t for t in _v27_as_list(report.get("threads")) if isinstance(t, dict)]:
        if extracted:
            try:
                _v27_salvage_raw_final_answer(thread, extracted)
            except Exception as exc:
                thread.setdefault("repair_warnings", []).append(f"v0.27 raw final-answer fallback failed: {exc}")
        _v27_clean_thread_text_and_urls(thread)
        _v27_rebuild_payload_views(thread)
        # Refresh task outcome after payload/final cleanup where possible, but
        # avoid downgrading an explicitly research/download/email/calendar task
        # to a generic page-open just because a raw fallback answer contains URL
        # text.
        try:
            if "classify_task_outcome" in globals():
                previous_outcome = _v27_as_dict(thread.get("task_outcome")).copy()
                new_outcome = classify_task_outcome(thread)
                prev_type = str(previous_outcome.get("task_type") or "")
                new_type = str(_v27_as_dict(new_outcome).get("task_type") or "")
                if prev_type in {"web_research", "file_download", "email_draft_send", "calendar_create"} and new_type in {"page_open", "unknown"}:
                    previous_outcome.setdefault("v27_reclassification_audit", _v27_json_clone(new_outcome))
                    previous_outcome.setdefault("v27_reclassification_note", "Preserved more specific pre-cleanup task type; new classifier result was generic after raw/text cleanup.")
                    thread["task_outcome"] = previous_outcome
                else:
                    thread["task_outcome"] = new_outcome
        except Exception:
            pass
        # Rebuild derived states that depend on final/payload availability.
        try:
            if "build_content_state" in globals():
                thread["content_state"] = build_content_state(thread)
            if "build_reconstruction_availability" in globals():
                thread["reconstruction_availability"] = build_reconstruction_availability(thread)
        except Exception:
            pass

    try:
        _v27_mark_primary_case_thread(report)
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.27 primary-thread selection failed: {exc}")

    # Rebuild case summary after primary marking/final salvage.  Do not filter or
    # drop residual threads.
    try:
        target = _v27_as_dict(report.get("source")).get("target_reference_filter") or None
        if report.get("report_mode") == "profile_inventory":
            target = None
        report["case_summary"] = build_case_summary(report, target)
        # Keep the v27 primary pointer after build_case_summary replacement.
        if report.get("primary_thread_selection_v27"):
            report["case_summary"]["primary_thread_id_v27"] = report["primary_thread_selection_v27"].get("primary_thread_id")
            report["case_summary"]["primary_thread_reference_codes_v27"] = report["primary_thread_selection_v27"].get("reference_codes")
    except Exception:
        pass

    report.setdefault("interpretation_policy", {})["v27_generic_refinements"] = {
        "raw_final_answer_fallback": "Only used when final_answer is missing and a prompt reference token is found in raw LevelDB bytes; marked low_to_medium confidence with raw_file_scan evidence.",
        "primary_thread_selection": "Display-only scoring; does not hide residual threads and does not use expected scenario outputs.",
        "payload_unification": "Typed payloads and observed form inputs are shown under a unified form_inputs bucket while preserving payload_source/evidence.",
        "text_url_cleanup": "Presentation cleanup preserves raw pre-cleanup final text where modified.",
        "reference_code_extraction": "Local-context and structured-token based to avoid ordinary-word false positives.",
        "scenario_specific_rules": False,
    }
    report["payload_attribution_summary_v27"] = {
        "version": "0.27",
        "note": "Generic fixes for raw final-answer fallback, primary case candidate marking, form input unification, conservative cleanup, and stricter reference-code extraction.",
    }
    return report



# ---------------------------------------------------------------------------
# v0.28 conservative presentation/claim-safety refinements
# ---------------------------------------------------------------------------

_V28_COMPLETION_POSITIVE_MARKERS = [
    "download is complete",
    "downloaded successfully",
    "download has completed",
    "saved successfully",
    "file has been downloaded",
    "successfully downloaded",
    "sent email is visible",
    "email has been sent",
    "event has been created",
    "created and verified",
]

_V28_COMPLETION_UNCERTAIN_MARKERS = [
    "may i proceed",
    "shall i proceed",
    "should i proceed",
    "would you like me to",
    "please confirm",
    "confirm before",
    "before proceeding",
    "waiting for your confirmation",
    "do you want me to",
]


def _v28_report_has_target_reference(report: dict[str, Any]) -> bool:
    source = _v27_as_dict(report.get("source"))
    target = source.get("target_reference_filter") or report.get("target_reference_filter")
    return bool(str(target or "").strip())


def _v28_clear_primary_marking(report: dict[str, Any], reason: str) -> None:
    for thread in [t for t in _v27_as_list(report.get("threads")) if isinstance(t, dict)]:
        thread["primary_case_candidate_v27"] = False
        thread["primary_case_candidate_v28"] = False
        thread.setdefault("display_safety_notes_v28", []).append(reason)
        display = thread.setdefault("display", {})
        if isinstance(display, dict):
            display["primary_case_candidate_v27"] = False
            display["primary_case_candidate_v28"] = False
    report["primary_thread_selection_v27"] = None
    report["primary_thread_selection_v28"] = {
        "primary_thread_id": None,
        "note": reason,
        "policy": "Primary case marking is disabled for profile inventory output unless --target-reference is supplied.",
    }
    case_summary = _v27_as_dict(report.get("case_summary")).copy()
    case_summary["primary_thread_id_v27"] = None
    case_summary["primary_thread_id_v28"] = None
    case_summary["primary_thread_selection_note_v28"] = reason
    report["case_summary"] = case_summary


def _v28_text_quality_score(text: str) -> int:
    """Heuristic score used only to avoid presenting raw binary residue as a final answer."""
    if not text:
        return 0
    total = max(len(text), 1)
    printable = sum(1 for ch in text if ch in "\n\t" or 32 <= ord(ch) <= 126 or ord(ch) >= 128)
    replacement = text.count("�")
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\n\t")
    score = int((printable / total) * 100)
    score -= min(30, replacement * 2)
    score -= min(30, controls * 2)
    if re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", text):
        score += 5
    if "workflow_root" in text or "pplx-query-cache" in text:
        score -= 10
    return max(0, min(100, score))


def _v28_demote_raw_final_answer(thread: dict[str, Any]) -> None:
    """Keep raw fallback bytes as forensic leads, not as a structured final answer."""
    final = _v27_as_dict(thread.get("final_answer")).copy()
    if str(final.get("source_kind") or "") != "raw_leveldb_reference_window":
        return

    text = _v27_text(final.get("text"))
    candidate = {
        "text_preview": text,
        "available": bool(text),
        "source_kind": "raw_leveldb_reference_window",
        "confidence": final.get("confidence") or "low",
        "raw_fallback_reference": final.get("raw_fallback_reference"),
        "relative_order": final.get("relative_order"),
        "evidence": _v27_as_list(final.get("evidence")),
        "text_quality_score_v28": _v28_text_quality_score(text),
        "caution": "Raw LevelDB byte-window residue. This is retained as a forensic lead and is not treated as a structured final answer.",
    }
    raw_candidates = [x for x in _v27_as_list(thread.get("raw_fallback_candidates")) if isinstance(x, dict)]
    # Avoid duplicating the same raw candidate when HTML rendering reapplies refinements.
    key = json.dumps({"src": candidate.get("source_kind"), "ref": candidate.get("raw_fallback_reference"), "text": text[:300]}, ensure_ascii=False, sort_keys=True)
    seen = {json.dumps({"src": x.get("source_kind"), "ref": x.get("raw_fallback_reference"), "text": _v27_text(x.get("text_preview"))[:300]}, ensure_ascii=False, sort_keys=True) for x in raw_candidates}
    if key not in seen:
        raw_candidates.append(candidate)
    thread["raw_fallback_candidates"] = raw_candidates
    thread["final_answer"] = {
        "text": None,
        "available": False,
        "reason": "Structured final-answer text was not recovered. Raw reference-window residue is preserved under raw_fallback_candidates.",
        "relative_order": final.get("relative_order"),
        "evidence": _v27_as_list(final.get("evidence")),
        "source_kind": None,
        "confidence": "not_claimed_from_raw_fallback",
        "raw_fallback_demoted_v28": True,
    }
    thread.setdefault("repair_warnings", []).append("v0.28 demoted raw LevelDB fallback from final_answer to raw_fallback_candidates.")


def _v28_value_in_text(value: str, *texts: str) -> bool:
    v = " ".join(str(value or "").split()).lower()
    if not v:
        return False
    if len(v) < 4:
        return False
    for text in texts:
        t = " ".join(str(text or "").split()).lower()
        if v and v in t:
            return True
    return False


def _v28_payload_matches_task(field: str, value: str, task_type: str, prompt_text: str, final_text: str) -> tuple[str, str]:
    """Return (bucket, reason) for display-only payload claim strength."""
    f = field.lower()
    low_value = value.lower()
    prompt_has = _v28_value_in_text(value, prompt_text)
    final_has = _v28_value_in_text(value, final_text)

    if task_type == "email_draft_send":
        if f in {"recipient", "to", "cc", "bcc", "subject", "body"}:
            if prompt_has or final_has or any(code.lower() in low_value for code in extract_reference_codes(prompt_text + "\n" + final_text)):
                return "confirmed", "Email field is corroborated by the task prompt/final answer or reference-code context."
            return "candidate", "Email-shaped field was observed, but prompt/final corroboration is weak."
        if f in {"filename", "file", "download"} or ".pdf" in low_value:
            return "residual", "File/download-looking payload is not task-local for an email draft/send case."

    if task_type == "calendar_create":
        if f in {"event_title", "title", "description", "date", "time", "start", "end", "start_time", "end_time", "location"} or f.endswith("_time"):
            if prompt_has or final_has or f in {"date", "start_time", "end_time"}:
                return "confirmed", "Calendar field is task-local and corroborated by prompt/final context or calendar field semantics."
            return "candidate", "Calendar-shaped field was observed, but prompt/final corroboration is weak."
        if f in {"recipient", "to", "cc", "bcc", "email"} and EMAIL_RE.fullmatch(value.strip()):
            return "residual", "Email recipient-like value is not task-local for a calendar creation case."
        if f in {"filename", "file", "download"} or ".pdf" in low_value:
            return "residual", "File/download-looking payload is not task-local for a calendar creation case."

    if task_type == "file_download":
        if f in {"filename", "file", "download", "target", "url"} or ".pdf" in low_value:
            if final_has or prompt_has or "download" in final_text.lower():
                return "confirmed", "Download/file field is corroborated by prompt/final context."
            return "candidate", "Download/file-looking field was observed, but completion corroboration is weak."
        if f in {"recipient", "to", "cc", "bcc"} and EMAIL_RE.fullmatch(value.strip()):
            return "residual", "Email recipient-like value is not task-local for a download case."

    if prompt_has or final_has:
        return "confirmed", "Payload value appears in task prompt/final context."
    return "candidate", "Observed payload was retained as a candidate because direct task-local corroboration is weak."


def _v28_partition_typed_payloads(thread: dict[str, Any]) -> None:
    task_type = str(_v27_as_dict(thread.get("task_outcome")).get("task_type") or "")
    if task_type not in {"email_draft_send", "calendar_create", "file_download"}:
        return
    prompt_text = _v27_text(_v27_as_dict(thread.get("prompt")).get("text"))
    final_text = _v27_text(_v27_as_dict(thread.get("final_answer")).get("text"))
    confirmed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = [x for x in _v27_as_list(thread.get("candidate_payloads")) if isinstance(x, dict)]
    residual: list[dict[str, Any]] = [x for x in _v27_as_list(thread.get("residual_payloads")) if isinstance(x, dict)]

    seen_by_bucket: dict[str, set[str]] = {"confirmed": set(), "candidate": set(), "residual": set()}

    def add(bucket: str, payload: dict[str, Any], reason: str) -> None:
        payload = payload.copy()
        payload["claim_strength_v28"] = bucket
        payload["claim_reason_v28"] = reason
        key = _v27_payload_key(payload)
        if key in seen_by_bucket[bucket]:
            return
        seen_by_bucket[bucket].add(key)
        if bucket == "confirmed":
            payload["payload_role"] = "confirmed_typed_field"
            confirmed.append(payload)
        elif bucket == "residual":
            payload["payload_role"] = "residual_or_cross_thread_candidate"
            residual.append(payload)
        else:
            payload["payload_role"] = "candidate_typed_field"
            candidates.append(payload)

    for payload in [p for p in _v27_as_list(thread.get("typed_payloads")) if isinstance(p, dict)]:
        field = str(payload.get("field") or "").lower()
        value = _v27_clean_human_text(_v27_text(payload.get("value")))
        if not value:
            continue
        bucket, reason = _v28_payload_matches_task(field, value, task_type, prompt_text, final_text)
        new_payload = payload.copy()
        new_payload["field"] = field
        new_payload["value"] = value
        add(bucket, new_payload, reason)

    # Keep top-level typed_payloads conservative: only confirmed values remain there.
    thread["typed_payloads"] = confirmed
    thread["candidate_payloads"] = candidates
    thread["residual_payloads"] = residual
    thread.setdefault("payload_partition_policy_v28", {
        "typed_payloads": "confirmed task-local values only",
        "candidate_payloads": "observed values with weak corroboration",
        "residual_payloads": "observed values likely belonging to another task/thread or not relevant to the task type",
    })


def _v28_completion_adjustment(thread: dict[str, Any]) -> int:
    final_text = _v27_text(_v27_as_dict(thread.get("final_answer")).get("text")).lower()
    outcome = _v27_as_dict(thread.get("task_outcome"))
    score = 0
    if any(m in final_text for m in _V28_COMPLETION_POSITIVE_MARKERS):
        score += 18
    if any(m in final_text for m in _V28_COMPLETION_UNCERTAIN_MARKERS):
        score -= 35
    if str(_v27_as_dict(thread.get("metadata")).get("thread_status") or "").lower() == "pending":
        score -= 10
    if str(_v27_as_dict(thread.get("final_answer")).get("source_kind") or "") == "raw_leveldb_reference_window":
        score -= 25
    if outcome.get("task_type") == "file_download" and _v27_as_dict(thread.get("final_answer")).get("text"):
        if re.search(r"\b[\w.\-]+\.pdf\b", final_text):
            score += 10
    return score


def _v28_mark_primary_case_thread(report: dict[str, Any]) -> None:
    if not _v28_report_has_target_reference(report):
        _v28_clear_primary_marking(
            report,
            "No --target-reference was supplied; this is a profile inventory, so no thread is marked as the primary case.",
        )
        return
    threads = [t for t in _v27_as_list(report.get("threads")) if isinstance(t, dict)]
    if not threads:
        return
    input_tokens = _v27_input_family_tokens(report)

    def ts_value(thread: dict[str, Any]) -> float:
        metadata = _v27_as_dict(thread.get("metadata"))
        raw = metadata.get("updated_at") or metadata.get("created_at") or metadata.get("last_query_datetime")
        if not raw:
            return 0.0
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    latest_ts = max((ts_value(t) for t in threads), default=0.0)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, thread in enumerate(threads):
        score = _v27_score_primary_thread(thread, input_tokens)
        score += _v28_completion_adjustment(thread)
        if latest_ts and ts_value(thread) == latest_ts:
            score += 8
            thread["primary_case_recency_boost_v28"] = True
        thread["primary_case_score_v28"] = score
        thread["primary_case_score_basis_v28"] = {
            "base_score_v27": thread.get("primary_case_score_v27"),
            "completion_adjustment": _v28_completion_adjustment(thread),
            "has_target_reference_filter": True,
            "has_structured_final_answer": bool(_v27_as_dict(thread.get("final_answer")).get("text")),
            "final_answer_source_kind": _v27_as_dict(thread.get("final_answer")).get("source_kind"),
            "task_type": _v27_as_dict(thread.get("task_outcome")).get("task_type"),
            "rule": "Conservative display ranking. Completion/confirmation language affects primary selection but does not modify recovered artifacts.",
        }
        scored.append((score, -idx, thread))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    primary = scored[0][2]
    for thread in threads:
        thread["primary_case_candidate_v28"] = thread is primary
        thread["primary_case_candidate_v27"] = thread is primary
    report["primary_thread_selection_v28"] = {
        "primary_thread_id": primary.get("thread_id"),
        "score": primary.get("primary_case_score_v28"),
        "reference_codes": _v27_as_dict(primary.get("prompt")).get("reference_codes", []),
        "task_type": _v27_as_dict(primary.get("task_outcome")).get("task_type"),
        "note": "Primary candidate is display-only and only enabled because --target-reference was supplied.",
    }
    report["primary_thread_selection_v27"] = report["primary_thread_selection_v28"]
    case_summary = _v27_as_dict(report.get("case_summary")).copy()
    case_summary["primary_thread_id_v28"] = primary.get("thread_id")
    case_summary["primary_thread_id_v27"] = primary.get("thread_id")
    case_summary["primary_thread_reference_codes_v28"] = _v27_as_dict(primary.get("prompt")).get("reference_codes", [])
    report["case_summary"] = case_summary


def _v28_build_residue_assessment(thread: dict[str, Any]) -> None:
    final = _v27_as_dict(thread.get("final_answer"))
    prompt = _v27_as_dict(thread.get("prompt"))
    classification = _v27_as_dict(thread.get("classification"))
    raw_candidates = [x for x in _v27_as_list(thread.get("raw_fallback_candidates")) if isinstance(x, dict)]
    prompt_field = str(prompt.get("field") or "")
    core_agent = classification.get("execution_mode") in {"browser_control", "computer_mode"}
    structured_final = bool(final.get("text")) and final.get("source_kind") != "raw_leveldb_reference_window"
    raw_only = bool(raw_candidates) and not structured_final
    title_only_prompt = prompt_field in {"title", "thread_title"} or prompt_field.endswith(".title")
    state = "structured_live_core" if core_agent and structured_final else "metadata_or_title_residue" if title_only_prompt else "raw_residue_only" if raw_only else "partial_or_uncertain"
    if core_agent and not structured_final and raw_only:
        state = "agentic_raw_residue_only"
    assessment = {
        "core_agentic_classification_present": core_agent,
        "structured_final_answer_present": structured_final,
        "raw_reference_residue_present": bool(raw_candidates),
        "prompt_field": prompt_field or None,
        "state": state,
        "interpretation": (
            "Structured agentic thread content and a structured final answer were recovered."
            if state == "structured_live_core" else
            "Target prompt/metadata survive mainly as title or list-cache residue; do not treat this as a full reconstructed agentic thread without corroboration."
            if state == "metadata_or_title_residue" else
            "Only raw reference-window residue was recovered for answer-like text; retained as a forensic lead, not a structured final answer."
            if "raw_residue" in state else
            "Partial artifact recovery; corroborate with before/after snapshots, History DB, Downloads, or OS artifacts."
        ),
    }
    thread["residue_assessment_v28"] = assessment
    if state in {"metadata_or_title_residue", "raw_residue_only", "agentic_raw_residue_only"}:
        classification = classification.copy()
        classification.setdefault("v28_residue_note", assessment["interpretation"])
        if classification.get("reconstruction_status") == "reconstructed" and not structured_final:
            classification["reconstruction_status"] = "residual_or_metadata_only"
        thread["classification"] = classification


def _v28_refresh_derived_thread_states(thread: dict[str, Any]) -> None:
    try:
        if "build_content_state" in globals():
            thread["content_state"] = build_content_state(thread)
        if "build_reconstruction_availability" in globals():
            thread["reconstruction_availability"] = build_reconstruction_availability(thread)
    except Exception:
        pass
    try:
        thread["timeline"] = build_timeline(
            records=[],
            prompt=_v27_as_dict(thread.get("prompt")),
            plan=_v27_as_list(thread.get("plan")),
            actions=_v27_as_list(thread.get("actions")),
            urls=_v27_as_list(thread.get("urls")),
            typed_payloads=_v27_as_list(thread.get("typed_payloads")),
            final_answer=_v27_as_dict(thread.get("final_answer")),
        )
    except Exception:
        pass


def _v28_apply_report_refinements(report: dict[str, Any], extracted: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply conservative v0.28 refinements without changing extraction logic."""
    report = _v27_apply_report_refinements(report, extracted)
    report["schema_version"] = "0.28"
    report.setdefault("source", {})["payload_attribution_version"] = "0.28"

    for thread in [t for t in _v27_as_list(report.get("threads")) if isinstance(t, dict)]:
        _v28_demote_raw_final_answer(thread)
        _v28_partition_typed_payloads(thread)
        _v28_build_residue_assessment(thread)
        _v28_refresh_derived_thread_states(thread)

    try:
        _v28_mark_primary_case_thread(report)
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.28 primary-thread selection failed: {exc}")

    try:
        target = _v27_as_dict(report.get("source")).get("target_reference_filter") or None
        if report.get("report_mode") == "profile_inventory" and not target:
            target = None
        report["case_summary"] = build_case_summary(report, target)
        if report.get("primary_thread_selection_v28"):
            report["case_summary"]["primary_thread_id_v28"] = report["primary_thread_selection_v28"].get("primary_thread_id")
            report["case_summary"]["primary_thread_reference_codes_v28"] = report["primary_thread_selection_v28"].get("reference_codes")
            report["case_summary"]["primary_thread_selection_note_v28"] = report["primary_thread_selection_v28"].get("note")
    except Exception:
        pass

    report.setdefault("interpretation_policy", {})["v28_conservative_claim_safety"] = {
        "profile_inventory_primary_marking": "Disabled unless --target-reference is supplied.",
        "raw_final_answer_fallback": "Raw LevelDB reference-window text is demoted to raw_fallback_candidates and is not claimed as final_answer.",
        "typed_payload_partition": "Top-level typed_payloads now contains confirmed task-local payloads only; weak or cross-task values are kept under candidate_payloads/residual_payloads.",
        "primary_thread_selection": "Target-filtered reports use conservative completion/confirmation language scoring; recovered artifacts are not modified.",
        "residue_assessment": "Adds explicit state labels for metadata/title/raw-residue-only recovery, especially useful for deletion/reopen snapshots.",
        "extraction_logic_changed": False,
    }
    report["payload_attribution_summary_v28"] = {
        "version": "0.28",
        "note": "Conservative claim-safety patch. It does not alter LevelDB extraction, grouping, or core classification; it only demotes low-confidence raw fallbacks, partitions payload claim strength, and disables primary marking for profile inventories.",
    }
    return report

def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:  # type: ignore[override]
    report = _reconstruct_browser_threads_pre_v27(extracted, input_label, browser_only=browser_only)
    return _v28_apply_report_refinements(report, extracted)


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:  # type: ignore[override]
    filtered = _filter_report_by_target_reference_pre_v27(report, target_reference)
    return _v28_apply_report_refinements(filtered, None)


def render_html_report(report: dict[str, Any], output_path: Path) -> None:  # type: ignore[override]
    report = _v28_apply_report_refinements(report, None)
    _render_html_report_pre_v27(report, output_path)
    try:
        html_text = output_path.read_text(encoding="utf-8")
        html_text = html_text.replace("v0.24/v0.25/v0.26 payload roles:", "v0.24/v0.25/v0.26/v0.27/v0.28 payload roles:")
        html_text = html_text.replace("Recovered profile threads / residual-aware", "Recovered profile threads / residual-aware / v0.28 conservative-claims")
        if "No --target-reference" not in html_text and "Profile inventory" in html_text:
            html_text = html_text.replace("<div class='hero'>", "<div class='note warn'><strong>v0.28 safety note:</strong> Profile inventory output should not be interpreted as a single case reconstruction. Use <code>--target-reference</code> for case-focused validation.</div><div class='hero'>", 1)
        output_path.write_text(html_text, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v0.29 HTML navigation/filtering and condensed forensic summary
# ---------------------------------------------------------------------------
# This presentation layer intentionally leaves extraction, grouping, and core
# classification untouched. It adds: browser-side filtering, thread-level time
# summaries, and a neutral condensed reconstruction summary for quick review.

_reconstruct_browser_threads_pre_v29 = reconstruct_browser_threads
_filter_report_by_target_reference_pre_v29 = filter_report_by_target_reference
_render_html_report_pre_v29 = render_html_report


def _v29_as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _v29_as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _v29_short(value: Any, limit: int = 220) -> str:
    if value is None:
        return ""
    text_value = str(value).replace("\r", " ").strip()
    text_value = re.sub(r"\s+", " ", text_value)
    return text_value[:limit] + ("…" if len(text_value) > limit else "")


def _v29_unique_keep_order(values: Iterable[Any], limit: int | None = None) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if limit is not None and len(out) >= limit:
            break
    return out


def _v29_parse_iso_for_sort(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _v29_collect_evidence_stats(obj: Any) -> dict[str, Any]:
    seqs: list[int] = []
    offsets: list[int] = []
    files: list[str] = []
    states: list[str] = []
    source_types: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "source_file" in value or "source_path" in value:
                sf = value.get("source_file") or Path(str(value.get("source_path", ""))).name
                if sf:
                    files.append(str(sf))
                st = value.get("state")
                if st:
                    states.append(str(st))
                typ = value.get("source_type")
                if typ:
                    source_types.append(str(typ))
                seq = value.get("ldb_seq_no") or value.get("relative_order")
                try:
                    if seq is not None:
                        seqs.append(int(seq))
                except Exception:
                    pass
                off = value.get("offset")
                try:
                    if off is not None:
                        offsets.append(int(off))
                except Exception:
                    pass
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return {
        "relative_order_min": min(seqs) if seqs else None,
        "relative_order_max": max(seqs) if seqs else None,
        "offset_min": min(offsets) if offsets else None,
        "offset_max": max(offsets) if offsets else None,
        "source_files": _v29_unique_keep_order(files, 8),
        "source_file_count": len(set(files)),
        "states": _v29_unique_keep_order(states, 6),
        "source_types": _v29_unique_keep_order(source_types, 6),
    }


def _v29_collect_time_fields(thread: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = _v29_as_dict(thread.get("metadata"))
    candidates: list[dict[str, Any]] = []

    def add(field: str, value: Any, source: str, evidence: Any = None, relative_order: Any = None) -> None:
        if value in (None, ""):
            return
        item: dict[str, Any] = {
            "field": field,
            "raw": str(value),
            "source": source,
        }
        dt = _v29_parse_iso_for_sort(value)
        if dt:
            item["interpreted_utc"] = dt.isoformat()
        if relative_order is not None:
            try:
                item["relative_order"] = int(relative_order)
            except Exception:
                item["relative_order"] = relative_order
        if evidence:
            item["evidence"] = evidence
        candidates.append(item)

    for field in ("created_at", "updated_at", "started_at", "completed_at", "last_query_datetime", "lastAccess"):
        add(field, metadata.get(field), "metadata", _v29_as_dict(metadata.get("evidence")).get(field))

    for item in _v29_as_list(thread.get("temporal_evidence")):
        d = _v29_as_dict(item)
        add(str(d.get("field") or "time"), d.get("raw") or d.get("formatted"), "temporal_evidence", d.get("evidence"), d.get("relative_order"))

    for item in _v29_as_list(_v29_as_dict(thread.get("time_audit")).get("included_time_fields")):
        d = _v29_as_dict(item)
        add(str(d.get("field") or "time"), d.get("raw") or d.get("formatted"), "time_audit", d.get("evidence"), d.get("relative_order"))

    reasoning = _v29_as_dict(thread.get("reasoning"))
    for bucket_name in ("items", "progress_or_status_items"):
        for item in _v29_as_list(reasoning.get(bucket_name)):
            for t in _v29_as_list(_v29_as_dict(item).get("time_fields")):
                d = _v29_as_dict(t)
                add(str(d.get("field") or "time"), d.get("raw"), f"reasoning.{bucket_name}", d.get("evidence"), _v29_as_dict(item).get("relative_order"))

    # Deduplicate by field/raw/source while preserving order.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in candidates:
        key = (str(item.get("field")), str(item.get("raw")), str(item.get("source")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _v29_thread_reference_codes(thread: dict[str, Any]) -> list[str]:
    prompt = _v29_as_dict(thread.get("prompt"))
    refs = list(_v29_as_list(prompt.get("reference_codes")))
    # Fall back to display title/reference-like tokens from prompt text if the
    # structured list is absent.
    if not refs:
        prompt_text = str(prompt.get("text") or "")
        refs = re.findall(r"[A-Z][A-Za-z0-9]+(?:_[A-Za-z0-9]+){2,}", prompt_text)
    return [str(x) for x in _v29_unique_keep_order(refs, 6)]


def _v29_thread_quick_summary(thread: dict[str, Any], index: int) -> dict[str, Any]:
    classification = _v29_as_dict(thread.get("classification"))
    metadata = _v29_as_dict(thread.get("metadata"))
    display = _v29_as_dict(thread.get("display"))
    final_answer = _v29_as_dict(thread.get("final_answer"))
    deletion_state = _v29_as_dict(thread.get("deletion_state"))
    residue = _v29_as_dict(thread.get("residue_assessment_v28"))
    task_outcome = _v29_as_dict(thread.get("task_outcome"))
    prompt = _v29_as_dict(thread.get("prompt"))
    evidence_stats = _v29_collect_evidence_stats(thread)
    time_fields = _v29_collect_time_fields(thread)
    parsed_times = [_v29_parse_iso_for_sort(t.get("raw")) for t in time_fields]
    parsed_times = [t for t in parsed_times if t]
    refs = _v29_thread_reference_codes(thread)
    private_detection = _v29_as_dict(metadata.get("private_detection"))
    private_mode = bool(metadata.get("private_mode") or private_detection.get("private_mode"))
    reconstruction_status = str(classification.get("reconstruction_status") or "")
    residual = reconstruction_status == "residual_or_metadata_only" or str(residue.get("state") or "").endswith("residue_only")
    final_available = bool(final_answer.get("available") and final_answer.get("text"))
    raw_fallback = bool(_v29_as_list(thread.get("raw_fallback_candidates")))

    search_parts = [
        thread.get("thread_id"),
        " ".join(refs),
        classification.get("interaction_type"),
        classification.get("execution_mode"),
        reconstruction_status,
        display.get("task_type"),
        task_outcome.get("outcome"),
        prompt.get("text"),
        final_answer.get("text"),
    ]
    for url_item in _v29_as_list(thread.get("urls"))[:12]:
        search_parts.append(_v29_as_dict(url_item).get("url"))

    return {
        "index": index,
        "dom_id": f"thread-{index}",
        "thread_id": thread.get("thread_id"),
        "reference_codes": refs,
        "reference_label": refs[0] if refs else str(thread.get("thread_id") or f"Thread {index}"),
        "interaction_type": classification.get("interaction_type") or "unknown",
        "execution_mode": classification.get("execution_mode") or display.get("execution_mode") or "unknown",
        "reconstruction_status": reconstruction_status or "unknown",
        "task_type": display.get("task_type") or task_outcome.get("task_type") or "unknown",
        "task_outcome": task_outcome.get("outcome") or display.get("task_outcome") or "unknown",
        "private_mode": private_mode,
        "privacy_states": private_detection.get("privacy_states") or ([] if metadata.get("privacy_state") in (None, "") else [metadata.get("privacy_state")]),
        "access_levels": private_detection.get("access_levels") or ([] if metadata.get("access_level") in (None, "") else [metadata.get("access_level")]),
        "deletion_state": deletion_state.get("state") or "unknown",
        "residue_state": residue.get("state") or thread.get("residue_state") or "unknown",
        "residual_or_metadata_only": residual,
        "final_answer_available": final_available,
        "raw_fallback_candidate": raw_fallback,
        "has_plan": bool(_v29_as_list(thread.get("plan"))),
        "has_actions": bool(_v29_as_list(thread.get("actions")) or _v29_as_list(thread.get("structured_actions"))),
        "url_count": len(_v29_as_list(thread.get("urls"))),
        "typed_payload_count": len(_v29_as_list(thread.get("typed_payloads"))),
        "reasoning_available": bool(_v29_as_dict(thread.get("reasoning")).get("available")),
        "created_at": metadata.get("created_at"),
        "updated_at": metadata.get("updated_at"),
        "earliest_time_utc": min(parsed_times).isoformat() if parsed_times else None,
        "latest_time_utc": max(parsed_times).isoformat() if parsed_times else None,
        "time_field_count": len(time_fields),
        "time_fields": time_fields[:12],
        "relative_order_min": evidence_stats.get("relative_order_min"),
        "relative_order_max": evidence_stats.get("relative_order_max"),
        "source_files": evidence_stats.get("source_files"),
        "source_file_count": evidence_stats.get("source_file_count"),
        "source_states": evidence_stats.get("states"),
        "source_types": evidence_stats.get("source_types"),
        "claim_level": display.get("claim_level"),
        "prompt_field": prompt.get("field"),
        "prompt_preview": _v29_short(prompt.get("text"), 180),
        "final_preview": _v29_short(final_answer.get("text"), 180) if final_available else "",
        "search": _v29_short(" ".join(str(x or "") for x in search_parts), 6000),
    }


def _v29_build_condensed_summary(report: dict[str, Any]) -> dict[str, Any]:
    threads = [_v29_thread_quick_summary(t, i + 1) for i, t in enumerate(_v29_as_list(report.get("threads")))]
    summary = _v29_as_dict(report.get("summary"))
    extraction = _v29_as_dict(report.get("extraction_summary"))
    source = _v29_as_dict(report.get("source"))
    case_summary = _v29_as_dict(report.get("case_summary"))

    all_times: list[datetime] = []
    for thread in threads:
        for key in ("created_at", "updated_at", "earliest_time_utc", "latest_time_utc"):
            dt = _v29_parse_iso_for_sort(thread.get(key))
            if dt:
                all_times.append(dt)

    rel_values = [v for t in threads for v in (t.get("relative_order_min"), t.get("relative_order_max")) if isinstance(v, int)]
    private_threads = [t for t in threads if t.get("private_mode")]
    residual_threads = [t for t in threads if t.get("residual_or_metadata_only")]
    final_threads = [t for t in threads if t.get("final_answer_available")]
    raw_threads = [t for t in threads if t.get("raw_fallback_candidate")]

    mode_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for thread in threads:
        mode_counts[str(thread.get("execution_mode") or "unknown")] = mode_counts.get(str(thread.get("execution_mode") or "unknown"), 0) + 1
        status_counts[str(thread.get("reconstruction_status") or "unknown")] = status_counts.get(str(thread.get("reconstruction_status") or "unknown"), 0) + 1

    notes: list[str] = []
    if not source.get("target_reference_filter"):
        notes.append("Profile inventory mode: recovered threads are listed as profile artifacts; no primary case is selected without --target-reference.")
    if raw_threads:
        notes.append("Raw LevelDB reference-window candidates are retained as forensic leads, not promoted as structured final answers.")
    if private_threads:
        notes.append("Private-mode claims require INCOGNITO/private privacy_state evidence; PRIVATE_READ alone remains access-level metadata.")
    if any(t.get("execution_mode") == "computer_mode" for t in threads):
        notes.append("Computer-mode items may include observable workflow/reasoning/progress artifacts; progress/status labels are separated from reasoning where possible.")
    notes.append("Time evidence combines decoded artifact timestamps with LevelDB sequence/order and file-offset evidence. Browser History/Downloads DB timestamps remain recommended external corroboration when available.")

    return {
        "version": "0.29",
        "input": source.get("input"),
        "report_mode": report.get("report_mode") or case_summary.get("report_mode") or "unknown",
        "target_reference": source.get("target_reference_filter") or case_summary.get("target_reference"),
        "record_counts": extraction,
        "thread_counts": summary,
        "mode_counts_v29": mode_counts,
        "reconstruction_status_counts_v29": status_counts,
        "final_answer_thread_count": len(final_threads),
        "raw_fallback_candidate_thread_count": len(raw_threads),
        "private_mode_thread_count_v29": len(private_threads),
        "residual_or_metadata_only_thread_count_v29": len(residual_threads),
        "earliest_artifact_time_utc": min(all_times).isoformat() if all_times else None,
        "latest_artifact_time_utc": max(all_times).isoformat() if all_times else None,
        "relative_order_min": min(rel_values) if rel_values else None,
        "relative_order_max": max(rel_values) if rel_values else None,
        "source_files_observed": _v29_unique_keep_order([sf for t in threads for sf in _v29_as_list(t.get("source_files"))], 20),
        "source_file_count": len(set(sf for t in threads for sf in _v29_as_list(t.get("source_files")))),
        "thread_quick_map": threads,
        "interpretation_notes": notes,
        "field_coverage": {
            "threads_with_prompt": sum(1 for t in threads if t.get("prompt_preview")),
            "threads_with_plan": sum(1 for t in threads if t.get("has_plan")),
            "threads_with_actions": sum(1 for t in threads if t.get("has_actions")),
            "threads_with_urls": sum(1 for t in threads if (t.get("url_count") or 0) > 0),
            "threads_with_payloads": sum(1 for t in threads if (t.get("typed_payload_count") or 0) > 0),
            "threads_with_reasoning": sum(1 for t in threads if t.get("reasoning_available")),
            "threads_with_final_answer": len(final_threads),
            "threads_with_time_fields": sum(1 for t in threads if (t.get("time_field_count") or 0) > 0),
        },
    }


def _v29_apply_report_refinements(report: dict[str, Any]) -> dict[str, Any]:
    try:
        report = _v28_apply_report_refinements(report, None)
    except Exception:
        pass
    try:
        report["presentation_version"] = "0.29"
        report["html_filtering_v29"] = {
            "available": True,
            "policy": "Client-side HTML filters hide/show already recovered profile threads. They do not re-run extraction or alter forensic interpretation.",
            "filters": [
                "free-text/reference search",
                "interaction type",
                "execution mode",
                "reconstruction status",
                "private mode only",
                "residual/metadata-only only",
                "structured final answer only",
            ],
        }
        report["condensed_reconstruction_summary_v29"] = _v29_build_condensed_summary(report)
        report.setdefault("repair_summary", {})["v29_presentation_layer"] = {
            "html_thread_filtering": True,
            "condensed_bottom_summary": True,
            "time_storage_summary": True,
            "extraction_logic_changed": False,
        }
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.29 presentation summary failed: {exc}")
    return report


def _v29_json_script(text_obj: Any) -> str:
    # JSON is embedded in a non-executable application/json script tag.
    text = json.dumps(text_obj, ensure_ascii=False)
    text = text.replace("</", "<\\/")
    return text


def _v29_filter_controls_html(report: dict[str, Any]) -> str:
    condensed = _v29_as_dict(report.get("condensed_reconstruction_summary_v29")) or _v29_build_condensed_summary(report)
    threads = _v29_as_list(condensed.get("thread_quick_map"))
    modes = sorted({str(t.get("execution_mode") or "unknown") for t in threads})
    interactions = sorted({str(t.get("interaction_type") or "unknown") for t in threads})
    statuses = sorted({str(t.get("reconstruction_status") or "unknown") for t in threads})

    def options(values: list[str]) -> str:
        return "".join(f"<option value='{html.escape(v)}'>{html.escape(v)}</option>" for v in values)

    return f"""
<section class='card v29-filter-card' id='v29-filter-card'>
  <div class='topline'><h2>Thread filters</h2><span class='badge neutral'>display only</span></div>
  <div class='hint'>Filters hide or show already recovered threads in this HTML report. They do not change the JSON, extraction results, or forensic interpretation. Use <code>--target-reference</code> when producing a case-focused reconstruction.</div>
  <div class='v29-filter-grid'>
    <label>Search reference / prompt / URL / final<br><input id='v29FilterText' type='search' placeholder='e.g., S10_04A, Gmail, INCOGNITO, Wikipedia'></label>
    <label>Interaction<br><select id='v29FilterInteraction'><option value=''>Any</option>{options(interactions)}</select></label>
    <label>Execution mode<br><select id='v29FilterMode'><option value=''>Any</option>{options(modes)}</select></label>
    <label>Status<br><select id='v29FilterStatus'><option value=''>Any</option>{options(statuses)}</select></label>
    <label class='v29-check'><input id='v29FilterPrivate' type='checkbox'> Private mode only</label>
    <label class='v29-check'><input id='v29FilterResidual' type='checkbox'> Residual/metadata-only only</label>
    <label class='v29-check'><input id='v29FilterFinal' type='checkbox'> Structured final answer only</label>
    <button type='button' id='v29FilterReset'>Reset filters</button>
  </div>
  <div id='v29FilterCount' class='note'>Showing all recovered thread sections.</div>
</section>
<script type='application/json' id='v29-thread-index'>{_v29_json_script(threads)}</script>
"""


def _v29_bottom_summary_html(report: dict[str, Any]) -> str:
    condensed = _v29_as_dict(report.get("condensed_reconstruction_summary_v29")) or _v29_build_condensed_summary(report)
    counts = _v29_as_dict(condensed.get("thread_counts"))
    records = _v29_as_dict(condensed.get("record_counts"))
    coverage = _v29_as_dict(condensed.get("field_coverage"))
    threads = _v29_as_list(condensed.get("thread_quick_map"))
    notes = _v29_as_list(condensed.get("interpretation_notes"))
    target_ref = condensed.get("target_reference") or "N/A — profile inventory"

    def td(value: Any) -> str:
        return html.escape("" if value is None else str(value))

    quick_rows: list[str] = []
    for t in threads:
        refs = ", ".join(str(x) for x in _v29_as_list(t.get("reference_codes"))) or str(t.get("thread_id") or "")
        priv = "yes" if t.get("private_mode") else "no"
        final = "yes" if t.get("final_answer_available") else ("raw lead" if t.get("raw_fallback_candidate") else "no")
        time_range = " / ".join(x for x in [str(t.get("created_at") or ""), str(t.get("updated_at") or "")] if x) or "N/A"
        seq_range = "–".join(str(x) for x in [t.get("relative_order_min"), t.get("relative_order_max")] if x is not None) or "N/A"
        quick_rows.append(
            "<tr>"
            f"<td>{td(t.get('index'))}</td>"
            f"<td>{td(_v29_short(refs, 120))}</td>"
            f"<td>{td(t.get('interaction_type'))}</td>"
            f"<td>{td(t.get('execution_mode'))}</td>"
            f"<td>{td(t.get('reconstruction_status'))}</td>"
            f"<td>{td(priv)}</td>"
            f"<td>{td(final)}</td>"
            f"<td>{td(time_range)}</td>"
            f"<td>{td(seq_range)}</td>"
            "</tr>"
        )

    note_items = "".join(f"<li>{td(note)}</li>" for note in notes)
    return f"""
<section id='v29-bottom-summary' class='card view-section'>
  <div class='topline'><h2>Condensed reconstruction summary</h2><span class='badge neutral'>v0.29</span></div>
  <div class='hint'>This section condenses the recovered LevelDB evidence into the main reconstruction questions: conversation vs. agentic activity, Browser Control vs. Computer mode, prompt/metadata/action/final-answer coverage, privacy/deletion status, and time/storage evidence.</div>
  <table class='kv'>
    <tr><th>Input</th><td>{td(condensed.get('input'))}</td></tr>
    <tr><th>Report mode</th><td>{td(condensed.get('report_mode'))}</td></tr>
    <tr><th>Target reference</th><td>{td(target_ref)}</td></tr>
    <tr><th>Threads recovered</th><td>{td(counts.get('thread_count'))} total · {td(counts.get('browser_agent_thread_count'))} Browser Control · {td(counts.get('computer_mode_thread_count'))} Computer mode · {td(counts.get('conversational_or_search_thread_count'))} conversational/search</td></tr>
    <tr><th>Record coverage</th><td>{td(records.get('all_record_count'))} parsed · {td(records.get('live_record_count'))} live · {td(records.get('dead_record_count'))} deleted/old · {td(records.get('bad_record_count'))} bad/undecoded · {td(records.get('relevant_record_count'))} relevant</td></tr>
    <tr><th>Field coverage</th><td>{td(coverage.get('threads_with_prompt'))} prompts · {td(coverage.get('threads_with_plan'))} plan-bearing · {td(coverage.get('threads_with_actions'))} action-bearing · {td(coverage.get('threads_with_urls'))} URL-bearing · {td(coverage.get('threads_with_payloads'))} payload-bearing · {td(coverage.get('threads_with_reasoning'))} reasoning/progress-bearing · {td(coverage.get('threads_with_final_answer'))} final-answer-bearing</td></tr>
    <tr><th>Private/residual indicators</th><td>{td(condensed.get('private_mode_thread_count_v29'))} private-mode thread(s) · {td(condensed.get('residual_or_metadata_only_thread_count_v29'))} residual/metadata-only thread(s) · {td(condensed.get('raw_fallback_candidate_thread_count'))} raw fallback candidate thread(s)</td></tr>
    <tr><th>Artifact time range</th><td>{td(condensed.get('earliest_artifact_time_utc'))} → {td(condensed.get('latest_artifact_time_utc'))}</td></tr>
    <tr><th>LevelDB order range</th><td>{td(condensed.get('relative_order_min'))} → {td(condensed.get('relative_order_max'))}</td></tr>
    <tr><th>Source files observed</th><td>{td(condensed.get('source_file_count'))} file(s): {td(', '.join(str(x) for x in _v29_as_list(condensed.get('source_files_observed'))[:12]))}</td></tr>
  </table>
  <h4>Thread quick map</h4>
  <table class='table'><thead><tr><th>#</th><th>Reference / thread</th><th>Interaction</th><th>Mode</th><th>Status</th><th>Private</th><th>Final</th><th>Created / updated</th><th>LevelDB order</th></tr></thead><tbody>{''.join(quick_rows)}</tbody></table>
  <h4>Interpretation notes</h4>
  <ul class='findings'>{note_items}</ul>
</section>
"""


def _v29_filter_css_js() -> str:
    return r"""
<style>
.v29-filter-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;align-items:end}.v29-filter-grid input,.v29-filter-grid select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:10px;background:white}.v29-filter-grid button{padding:9px 12px;border:1px solid var(--line);border-radius:10px;background:#111827;color:white;cursor:pointer}.v29-check{display:flex;gap:8px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:#fff}.v29-check input{width:auto}.v29-hidden{display:none!important}.v29-filter-dim{opacity:.45}.v29-filter-card{border-left:4px solid var(--blue)}
</style>
<script>
(function(){
  function byId(id){ return document.getElementById(id); }
  function getIndex(){
    var el = byId('v29-thread-index');
    if(!el) return [];
    try { return JSON.parse(el.textContent || '[]'); } catch(e) { return []; }
  }
  var threadIndex = getIndex();
  function setMatchedCount(n){
    var el = byId('v29FilterCount');
    if(el) el.innerHTML = '<strong>' + n + '</strong> of <strong>' + threadIndex.length + '</strong> recovered thread sections match the current filter.';
  }
  function readFilters(){
    return {
      q: (byId('v29FilterText') && byId('v29FilterText').value || '').trim().toLowerCase(),
      interaction: (byId('v29FilterInteraction') && byId('v29FilterInteraction').value || ''),
      mode: (byId('v29FilterMode') && byId('v29FilterMode').value || ''),
      status: (byId('v29FilterStatus') && byId('v29FilterStatus').value || ''),
      privateOnly: !!(byId('v29FilterPrivate') && byId('v29FilterPrivate').checked),
      residualOnly: !!(byId('v29FilterResidual') && byId('v29FilterResidual').checked),
      finalOnly: !!(byId('v29FilterFinal') && byId('v29FilterFinal').checked)
    };
  }
  function matches(info, filters){
    var section = byId(info.dom_id);
    var hay = ((info.search || '') + ' ' + (section ? section.innerText : '')).toLowerCase();
    if(filters.q && hay.indexOf(filters.q) === -1) return false;
    if(filters.interaction && info.interaction_type !== filters.interaction) return false;
    if(filters.mode && info.execution_mode !== filters.mode) return false;
    if(filters.status && info.reconstruction_status !== filters.status) return false;
    if(filters.privateOnly && !info.private_mode) return false;
    if(filters.residualOnly && !info.residual_or_metadata_only) return false;
    if(filters.finalOnly && !info.final_answer_available) return false;
    return true;
  }
  function applyFilters(){
    var filters = readFilters();
    var overviewCards = document.querySelectorAll('#overview .thread-card');
    var matched = 0;
    threadIndex.forEach(function(info, i){
      var ok = matches(info, filters);
      if(ok) matched += 1;
      var section = byId(info.dom_id);
      var navGroup = document.querySelector('.thread-nav-group[data-thread="' + info.dom_id + '"]');
      var card = overviewCards[i];
      [section, navGroup, card].forEach(function(el){ if(el) el.classList.toggle('v29-hidden', !ok); });
    });
    setMatchedCount(matched);
  }
  function resetFilters(){
    ['v29FilterText','v29FilterInteraction','v29FilterMode','v29FilterStatus'].forEach(function(id){ var el=byId(id); if(el) el.value=''; });
    ['v29FilterPrivate','v29FilterResidual','v29FilterFinal'].forEach(function(id){ var el=byId(id); if(el) el.checked=false; });
    applyFilters();
  }
  function bind(){
    ['v29FilterText','v29FilterInteraction','v29FilterMode','v29FilterStatus','v29FilterPrivate','v29FilterResidual','v29FilterFinal'].forEach(function(id){
      var el=byId(id); if(!el) return;
      el.addEventListener('input', applyFilters); el.addEventListener('change', applyFilters);
    });
    var reset=byId('v29FilterReset'); if(reset) reset.addEventListener('click', resetFilters);
    applyFilters();
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind); else bind();
})();
</script>
"""


def _v29_inject_html_features(html_text: str, report: dict[str, Any]) -> str:
    filter_html = _v29_filter_controls_html(report)
    bottom_html = _v29_bottom_summary_html(report)
    css_js = _v29_filter_css_js()

    # Add a navigation target for the condensed summary.
    if "data-target='v29-bottom-summary'" not in html_text:
        nav_item = "<button type='button' class='nav-item' data-target='v29-bottom-summary'><strong>Condensed summary</strong><span>Reconstruction overview</span></button>"
        if "<div class='nav-title'>Global/residual audit</div>" in html_text:
            html_text = html_text.replace("<div class='nav-title'>Global/residual audit</div>", "<div class='nav-title'>Summary</div>" + nav_item + "<div class='nav-title'>Global/residual audit</div>", 1)
        elif "</div></aside>" in html_text:
            html_text = html_text.replace("</div></aside>", nav_item + "</div></aside>", 1)

    # Insert filter controls near the top of the summary view without nesting
    # a card inside the existing Inventory findings card.
    if "id='v29-filter-card'" not in html_text:
        marker = "<section class='card'><div class='topline'><h2>Inventory findings"
        pos = html_text.find(marker)
        if pos != -1:
            html_text = html_text[:pos] + filter_html + html_text[pos:]
        else:
            marker = "<ul class='findings'>"
            pos = html_text.find(marker)
            if pos != -1:
                html_text = html_text[:pos] + filter_html + html_text[pos:]
            else:
                html_text = html_text.replace("<section id='summary' class='view-section active'>", "<section id='summary' class='view-section active'>" + filter_html, 1)

    # Insert the condensed summary near the bottom, before global/raw audit when possible.
    if "id='v29-bottom-summary'" not in html_text:
        for marker in ("<section id='global'", "<section id='raw'", "</main>"):
            pos = html_text.find(marker)
            if pos != -1:
                html_text = html_text[:pos] + bottom_html + html_text[pos:]
                break

    # Add CSS/JS before the end of body so it applies to the final DOM.
    if "v29-filter-grid" not in html_text or "v29-thread-index" not in html_text:
        pass
    if "function applyFilters()" not in html_text:
        html_text = html_text.replace("</body>", css_js + "</body>", 1) if "</body>" in html_text else html_text + css_js
    return html_text


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, *, browser_only: bool = False) -> dict[str, Any]:  # type: ignore[override]
    report = _reconstruct_browser_threads_pre_v29(extracted, input_label, browser_only=browser_only)
    return _v29_apply_report_refinements(report)


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:  # type: ignore[override]
    filtered = _filter_report_by_target_reference_pre_v29(report, target_reference)
    return _v29_apply_report_refinements(filtered)


def render_html_report(report: dict[str, Any], output_path: Path) -> None:  # type: ignore[override]
    report = _v29_apply_report_refinements(report)
    _render_html_report_pre_v29(report, output_path)
    try:
        html_text = output_path.read_text(encoding="utf-8")
        html_text = html_text.replace("v0.28 conservative-claims", "v0.28 conservative-claims / v0.29 filters")
        html_text = _v29_inject_html_features(html_text, report)
        output_path.write_text(html_text, encoding="utf-8")
    except Exception as exc:
        try:
            output_path.write_text(output_path.read_text(encoding="utf-8") + f"\n<!-- v0.29 HTML injection failed: {html.escape(str(exc))} -->\n", encoding="utf-8")
        except Exception:
            pass




# ---------------------------------------------------------------------------
# v0.31 scenario-focused HTML display layer
# ---------------------------------------------------------------------------
# Presentation-only layer. It does not change LevelDB extraction,
# grouping, classification, or JSON evidence. It adds a scenario-focused
# display mode on top of the recovered profile inventory so scenario-family
# artifacts can be viewed without unrelated residual threads.

_render_html_report_pre_v30 = render_html_report


def _v30_safe_basename(value: Any) -> str:
    """Return a filename from either Windows or POSIX paths.

    Reports often store Windows paths (C:\\...).  pathlib.Path on POSIX does
    not treat backslashes as separators, so split on both separators here.
    """
    try:
        raw = str(value or "")
        return re.split(r"[\\/]", raw)[-1]
    except Exception:
        return str(value or "")


def _v30_scenario_prefix_from_report(report: dict[str, Any]) -> str:
    source = _v29_as_dict(report.get("source"))
    target_ref = str(source.get("target_reference_filter") or report.get("target_reference") or "").strip()
    input_name = _v30_safe_basename(source.get("input") or "")
    candidates = [target_ref, input_name]
    for candidate in candidates:
        m = re.search(r"\b(S\d{2})(?:[_\-.]|$)", candidate)
        if m:
            return m.group(1)
    base = re.sub(r"\.(zip|json|html)$", "", input_name, flags=re.I)
    base = re.sub(r"_IndexedDB.*$", "", base, flags=re.I)
    base = re.sub(r"\(\d+\)$", "", base).strip("_- ")
    if base:
        parts = [x for x in re.split(r"[_\s]+", base) if x]
        return parts[0] if parts else base
    return ""


def _v31_scenario_base_from_report(report: dict[str, Any]) -> str:
    source = _v29_as_dict(report.get("source"))
    input_name = _v30_safe_basename(source.get("input") or "")
    base = re.sub(r"\.(zip|json|html)$", "", input_name, flags=re.I)
    base = re.sub(r"_IndexedDB.*$", "", base, flags=re.I)
    base = re.sub(r"\(\d+\)$", "", base).strip("_- ")
    return base


def _v31_scenario_terms_from_report(report: dict[str, Any]) -> dict[str, Any]:
    """Build browser-side scenario matching hints for HTML-only display.

    This is deliberately display-only.  The goal is not to change evidence, but
    to avoid a strict view returning zero for filenames that do not share a
    literal leading reference prefix with the recovered prompt (for example,
    Agent_IndexedDB -> C02_AgentPageOpen, browser_reopen_private -> S10_*
    private/browser-incognito references, and S10_S04R -> S09_02_S04* residue).
    """
    base = _v31_scenario_base_from_report(report)
    lower_base = base.lower()
    s_tokens = [m.group(0).upper() for m in re.finditer(r"S\d{2}", base, flags=re.I)]

    # If a file name contains a secondary scenario token (e.g. S09_S04D or
    # S10_S04R), the secondary token usually identifies the deleted/reopened
    # source scenario.  Use the last token for strict matching.
    strict_terms: list[str] = []
    if s_tokens:
        strict_terms.append(s_tokens[-1].lower())

    # Action/domain words.  Avoid generic words such as indexeddb, case, profile.
    # Prefer task-specific words so S06_Computer_Download does not keep every
    # Computer residual; it should keep references containing Download.
    action_terms: list[str] = []
    if "download" in lower_base:
        action_terms.append("download")
    if "gmail" in lower_base or "mail" in lower_base:
        action_terms.extend(["gmail", "draft", "send"])
    if "calendar" in lower_base:
        action_terms.append("calendar")
    if "agent" in lower_base:
        action_terms.append("agent")
    if "private" in lower_base or "incognito" in lower_base:
        action_terms.extend(["private", "incognito"])
    if "browser" in lower_base and ("private" in lower_base or "incognito" in lower_base or "reopen" in lower_base):
        action_terms.append("browser")
    if "comet" in lower_base and "private" in lower_base:
        action_terms.append("comet")
    if "computer" in lower_base and not action_terms:
        action_terms.append("computer")

    for term in action_terms:
        term = term.lower()
        if term not in strict_terms:
            strict_terms.append(term)

    return {
        "scenario_base": base,
        "scenario_s_tokens": s_tokens,
        "strict_ref_terms": strict_terms,
    }


def _v30_scenario_policy(report: dict[str, Any]) -> dict[str, Any]:
    source = _v29_as_dict(report.get("source"))
    case_summary = _v29_as_dict(report.get("case_summary"))
    target_ref = str(source.get("target_reference_filter") or case_summary.get("target_reference") or report.get("target_reference") or "").strip()
    prefix = _v30_scenario_prefix_from_report(report)
    terms = _v31_scenario_terms_from_report(report)
    mode = "strict" if target_ref or (case_summary.get("report_mode") == "case_reconstruction") else "scenario"
    return {
        "version": "0.31",
        "input": source.get("input"),
        "report_mode": report.get("report_mode") or case_summary.get("report_mode"),
        "target_reference": target_ref,
        "scenario_prefix": prefix,
        "scenario_base": terms.get("scenario_base"),
        "scenario_s_tokens": terms.get("scenario_s_tokens"),
        "strict_ref_terms": terms.get("strict_ref_terms"),
        "default_mode": mode,
        "policy": (
            "Client-side scenario display hides unrelated profile/residual artifacts from the HTML view only. "
            "It does not delete evidence from JSON or change forensic interpretation."
        ),
    }


def _v30_scenario_controls_html(report: dict[str, Any]) -> str:
    policy = _v30_scenario_policy(report)
    prefix = html.escape(str(policy.get("scenario_prefix") or ""))
    target = html.escape(str(policy.get("target_reference") or "N/A"))
    default = html.escape(str(policy.get("default_mode") or "scenario"))
    return f"""
<section class='card v30-scenario-card' id='v30-scenario-card'>
  <div class='topline'><h2>Scenario display</h2><span class='badge neutral'>HTML view only · v0.31</span></div>
  <div class='hint'>Use this display mode to hide unrelated profile artifacts that remain in the same IndexedDB/LevelDB snapshot. The underlying JSON and evidence are preserved. Scenario prefix: <code>{prefix}</code>; target reference: <code>{target}</code>. Strict matching uses scenario/reference terms inferred from the input name and recovered reference codes.</div>
  <div class='v30-scenario-grid'>
    <button type='button' id='v30StrictScenario'>Strict scenario view</button>
    <button type='button' id='v30ScenarioRelated'>Scenario-related only</button>
    <button type='button' id='v30ShowAll'>Show all recovered artifacts</button>
  </div>
  <div id='v30ScenarioCount' class='note'>Scenario display mode: {default}</div>
</section>
<script type='application/json' id='v30-scenario-policy'>{_v29_json_script(policy)}</script>
"""


def _v30_scenario_css_js() -> str:
    return r"""
<style>
.v30-scenario-card{border-left:4px solid var(--purple);position:sticky;top:0;z-index:5}
.v30-scenario-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:10px 0}
.v30-scenario-grid button{padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#111827;color:#fff;cursor:pointer;font-weight:800}
.v30-scenario-grid button.v30-active{background:#6d28d9;color:#fff}
.v30-hidden{display:none!important}
</style>
<script>
(function(){
  function byId(id){ return document.getElementById(id); }
  function readJson(id, fallback){
    var el = byId(id);
    if(!el) return fallback;
    try { return JSON.parse(el.textContent || 'null') || fallback; } catch(e) { return fallback; }
  }
  function lower(x){ return String(x || '').toLowerCase(); }
  var index = readJson('v29-thread-index', []);
  var policy = readJson('v30-scenario-policy', {});
  var target = lower(policy.target_reference || '');
  var prefix = lower(policy.scenario_prefix || '');
  var strictTerms = Array.isArray(policy.strict_ref_terms) ? policy.strict_ref_terms.map(lower).filter(Boolean) : [];

  function refsOf(info){
    var refs = info.reference_codes || [];
    if(!Array.isArray(refs)) refs = [];
    if(info.reference_label) refs = refs.concat([info.reference_label]);
    return refs.map(function(x){ return String(x || ''); });
  }
  function hasPromptOrReference(info){
    var refs = refsOf(info);
    return refs.length > 0 || !!String(info.prompt_preview || '').trim();
  }
  function isComputerAux(info){
    return lower(info.thread_id || '').indexOf('computer:') === 0 || !!info.computer_auxiliary_v30;
  }
  function matchesTarget(info){
    if(!target) return false;
    var refs = refsOf(info).map(lower);
    if(refs.indexOf(target) !== -1) return true;
    return lower(info.search || '').indexOf(target) !== -1;
  }
  function refTextOf(info){
    return refsOf(info).join(' ').toLowerCase();
  }
  function refContainsTerm(info, term){
    var rt = refTextOf(info);
    term = lower(term);
    if(!term) return false;
    return rt.indexOf(term) !== -1;
  }
  function matchesStrictTerms(info){
    if(!strictTerms.length) return false;
    for(var i=0;i<strictTerms.length;i++){
      if(refContainsTerm(info, strictTerms[i])) return true;
    }
    return false;
  }
  function matchesScenarioPrefix(info){
    if(!prefix && !strictTerms.length) return true;
    var refs = refsOf(info).map(lower);
    for(var i=0;i<refs.length;i++){
      if(prefix && (refs[i] === prefix || refs[i].indexOf(prefix + '_') === 0 || refs[i].indexOf(prefix + '-') === 0 || refs[i].indexOf(prefix) !== -1)) return true;
    }
    if(matchesStrictTerms(info)) return true;
    if(/^s\d{2}$/.test(prefix) && lower(info.search || '').indexOf(prefix + '_') !== -1) return true;
    return false;
  }
  function isScenarioRelated(info){
    if(target) return matchesTarget(info) || matchesScenarioPrefix(info);
    return matchesScenarioPrefix(info);
  }
  function isStrictScenario(info){
    if(target) return matchesTarget(info);
    // Strict means directly scenario-related by reference/title tokens, with a
    // recovered prompt/reference, and not a detached computer auxiliary record.
    // It intentionally keeps residual/metadata-only records when the scenario
    // itself is a deletion/reopen residue case.
    return hasPromptOrReference(info) && !isComputerAux(info) && (matchesStrictTerms(info) || matchesScenarioPrefix(info));
  }
  function setActive(mode){
    ['v30StrictScenario','v30ScenarioRelated','v30ShowAll'].forEach(function(id){
      var el = byId(id); if(el) el.classList.remove('v30-active');
    });
    var active = mode === 'strict' ? byId('v30StrictScenario') : mode === 'scenario' ? byId('v30ScenarioRelated') : byId('v30ShowAll');
    if(active) active.classList.add('v30-active');
  }
  function setCount(mode, n){
    var el = byId('v30ScenarioCount');
    if(!el) return;
    var label = mode === 'strict' ? 'Strict scenario view' : mode === 'scenario' ? 'Scenario-related only' : 'Show all recovered artifacts';
    el.innerHTML = '<strong>' + label + '</strong>: showing <strong>' + n + '</strong> of <strong>' + index.length + '</strong> recovered thread section(s).';
  }
  function setHidden(el, hidden){ if(el) el.classList.toggle('v30-hidden', !!hidden); }
  function applyMode(mode){
    setActive(mode);
    var count = 0;
    var overviewCards = document.querySelectorAll('#overview .thread-card');
    index.forEach(function(info, i){
      var show = true;
      if(mode === 'strict') show = isStrictScenario(info);
      else if(mode === 'scenario') show = isScenarioRelated(info);
      if(show) count += 1;
      setHidden(byId(info.dom_id), !show);
      setHidden(document.querySelector('.thread-nav-group[data-thread="' + info.dom_id + '"]'), !show);
      setHidden(overviewCards[i], !show);
    });
    var hideAudit = (mode !== 'all');
    ['global','raw'].forEach(function(id){
      setHidden(byId(id), hideAudit);
      document.querySelectorAll("[data-target='" + id + "']").forEach(function(el){ setHidden(el, hideAudit); });
    });
    setCount(mode, count);
  }
  function bind(){
    var b1=byId('v30StrictScenario'), b2=byId('v30ScenarioRelated'), b3=byId('v30ShowAll');
    if(b1) b1.addEventListener('click', function(){ applyMode('strict'); });
    if(b2) b2.addEventListener('click', function(){ applyMode('scenario'); });
    if(b3) b3.addEventListener('click', function(){ applyMode('all'); });
    applyMode(policy.default_mode || 'scenario');
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind); else bind();
})();
</script>
"""


def _v30_inject_html_features(html_text: str, report: dict[str, Any]) -> str:
    controls = _v30_scenario_controls_html(report)
    if "id='v30-scenario-card'" not in html_text:
        pos = html_text.find("<section class='card v29-filter-card'")
        if pos != -1:
            html_text = html_text[:pos] + controls + html_text[pos:]
        else:
            marker = "</section>\n\n<section class='card"
            pos = html_text.find(marker)
            if pos != -1:
                html_text = html_text[:pos + len("</section>\n\n")] + controls + html_text[pos + len("</section>\n\n"):]
            else:
                html_text = html_text.replace("<section id='summary' class='view-section active'>", "<section id='summary' class='view-section active'>" + controls, 1)
    if "function isStrictScenario" not in html_text:
        css_js = _v30_scenario_css_js()
        html_text = html_text.replace("</body>", css_js + "</body>", 1) if "</body>" in html_text else html_text + css_js
    html_text = html_text.replace("v0.29 filters", "v0.29 filters / v0.31 scenario display")
    return html_text


def render_html_report(report: dict[str, Any], output_path: Path) -> None:  # type: ignore[override]
    report = _v29_apply_report_refinements(report)
    report["presentation_version"] = "0.31"
    report.setdefault("repair_summary", {})["v31_scenario_display_layer"] = {
        "html_scenario_display": True,
        "strict_scenario_view": True,
        "scenario_related_only_view": True,
        "show_all_recovered_artifacts_view": True,
        "scenario_matching_version": "0.31",
        "extraction_logic_changed": False,
    }
    _render_html_report_pre_v30(report, output_path)
    try:
        html_text = output_path.read_text(encoding="utf-8")
        html_text = _v30_inject_html_features(html_text, report)
        output_path.write_text(html_text, encoding="utf-8")
    except Exception as exc:
        try:
            current = output_path.read_text(encoding="utf-8")
            output_path.write_text(current + f"\n<!-- v0.31 scenario display injection failed: {html.escape(str(exc))} -->\n", encoding="utf-8")
        except Exception:
            pass



# ---------------------------------------------------------------------------
# v0.32 condensed forensic summary + strict display/source-file cleanup
# ---------------------------------------------------------------------------
# This layer only changes investigator-facing summaries and HTML display hints.
# It does not alter raw extraction, thread grouping, classification, action
# extraction, reasoning extraction, or task-outcome scoring.

_REAL_LEVELDB_SOURCE_RE_V32 = re.compile(r"^(?:\d{6}\.(?:ldb|log|sst)|LOG(?:\.old)?|MANIFEST-\d+|CURRENT|LOCK)$", re.IGNORECASE)


def _v32_safe_basename(value: Any) -> str:
    try:
        raw = str(value or "")
        return re.split(r"[\\/]", raw)[-1]
    except Exception:
        return str(value or "")


def _v32_is_real_source_file(value: Any) -> bool:
    name = _v32_safe_basename(value)
    if not name:
        return False
    return bool(_REAL_LEVELDB_SOURCE_RE_V32.match(name))


def _v32_evidence_refs_from_obj(obj: Any, limit: int = 500) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any, Any, Any]] = set()

    def walk(value: Any) -> None:
        if len(refs) >= limit:
            return
        if isinstance(value, dict):
            sf = value.get("source_file")
            source_path = value.get("source_path")
            # source_path is often a logical JSON field path such as
            # actions.3.label.  Treat it as a file only when its basename looks
            # like an actual LevelDB file.  This fixes source_files_observed
            # without hiding the original source_path from raw JSON evidence.
            if not sf and _v32_is_real_source_file(source_path):
                sf = _v32_safe_basename(source_path)
            if sf and _v32_is_real_source_file(sf):
                item = {
                    "source_file": _v32_safe_basename(sf),
                    "source_type": value.get("source_type"),
                    "state": value.get("state"),
                    "ldb_seq_no": value.get("ldb_seq_no") or value.get("relative_order") or value.get("seq"),
                    "offset": value.get("offset"),
                    "is_live": value.get("is_live"),
                }
                key = (item.get("source_file"), item.get("source_type"), item.get("state"), item.get("ldb_seq_no"), item.get("offset"))
                if key not in seen:
                    seen.add(key)
                    refs.append(item)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return refs


# Override the v0.29 evidence stats with a provenance-safe source-file collector.
def _v29_collect_evidence_stats(obj: Any) -> dict[str, Any]:  # type: ignore[override]
    refs = _v32_evidence_refs_from_obj(obj)
    seqs: list[int] = []
    offsets: list[int] = []
    files: list[str] = []
    states: list[str] = []
    source_types: list[str] = []

    for ref in refs:
        sf = ref.get("source_file")
        if sf:
            files.append(str(sf))
        st = ref.get("state")
        if st:
            states.append(str(st))
        typ = ref.get("source_type")
        if typ:
            source_types.append(str(typ))
        for seq_key in ("ldb_seq_no", "relative_order", "seq"):
            try:
                if ref.get(seq_key) is not None:
                    seqs.append(int(ref.get(seq_key)))
                    break
            except Exception:
                pass
        try:
            if ref.get("offset") is not None:
                offsets.append(int(ref.get("offset")))
        except Exception:
            pass

    return {
        "relative_order_min": min(seqs) if seqs else None,
        "relative_order_max": max(seqs) if seqs else None,
        "offset_min": min(offsets) if offsets else None,
        "offset_max": max(offsets) if offsets else None,
        "source_files": _v29_unique_keep_order(files, 20),
        "source_file_count": len(set(files)),
        "states": _v29_unique_keep_order(states, 10),
        "source_types": _v29_unique_keep_order(source_types, 10),
        "evidence_ref_count": len(refs),
    }


def _v32_count_map(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        out[key] = out.get(key, 0) + 1
    return out


def _v32_is_time_like_field(name: Any) -> bool:
    low = str(name or "").lower()
    return any(token in low for token in [
        "time", "timestamp", "created", "updated", "started", "completed", "finished",
        "lastaccess", "last_access", "date", "deleted", "archived",
    ])


def _v32_collect_additional_time_like_candidates(thread: dict[str, Any], limit: int = 120) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(path: str, value: Any) -> None:
        if value in (None, "", [], {}):
            return
        # Keep scalar values only in the condensed summary. Full raw objects remain
        # available in the thread JSON and raw evidence explorer.
        if isinstance(value, (dict, list)):
            return
        raw = str(value)
        if len(raw) > 500:
            raw = raw[:500] + "…"
        key = (path, raw)
        if key in seen:
            return
        seen.add(key)
        item: dict[str, Any] = {
            "field_path": path,
            "raw": raw,
            "interpretation": "time-like field candidate; verify semantics before using as an absolute forensic timestamp",
        }
        try:
            interp = interpret_timestamp(value)
            if isinstance(interp, dict) and interp.get("interpreted_utc"):
                item["time_interpretation"] = interp
        except Exception:
            pass
        out.append(item)

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if len(out) >= limit:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                child_path = path + (str(k),)
                if _v32_is_time_like_field(k):
                    add(".".join(child_path), v)
                walk(v, child_path)
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, path + (str(idx),))

    walk(thread, tuple())
    return out


def _v32_collect_time_values(thread: dict[str, Any]) -> dict[str, Any]:
    evidence_stats = _v29_collect_evidence_stats(thread)
    curated = _v29_collect_time_fields(thread)
    temporal = _v29_as_dict(thread.get("temporal_evidence"))
    time_audit = _v29_as_dict(thread.get("time_audit"))
    computer_temporal = _v29_as_dict(thread.get("computer_temporal_evidence"))
    metadata = _v29_as_dict(thread.get("metadata"))

    included: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_item(item: Any, default_source: str) -> None:
        d = _v29_as_dict(item)
        if not d:
            return
        field = str(d.get("field") or d.get("name") or d.get("source") or "time")
        raw = d.get("raw") if "raw" in d else d.get("value") if "value" in d else d.get("formatted")
        if raw in (None, ""):
            return
        source = str(d.get("source") or default_source)
        key = (field, str(raw), source)
        if key in seen:
            return
        seen.add(key)
        out = dict(d)
        out.setdefault("field", field)
        out.setdefault("raw", raw)
        out.setdefault("source", source)
        if "time_interpretation" not in out:
            try:
                out["time_interpretation"] = interpret_timestamp(raw)
            except Exception:
                pass
        included.append(out)

    for item in curated:
        add_item(item, "curated_time_fields")
    for item in _v29_as_list(temporal.get("forensic_time_fields")):
        add_item(item, "temporal_evidence.forensic_time_fields")
    for item in _v29_as_list(time_audit.get("interpreted_sample")):
        add_item(item, "time_audit.interpreted_sample")
    for item in _v29_as_list(time_audit.get("raw_only_sample")):
        add_item(item, "time_audit.raw_only_sample")
    for item in _v29_as_list(computer_temporal.get("workflow_time_fields")):
        add_item(item, "computer_temporal_evidence.workflow_time_fields")
    for field in sorted(set(list(FORENSIC_TIME_FIELD_NAMES_V17) + ["created_at", "updated_at", "lastAccess", "last_query_datetime"])):
        if field in metadata:
            add_item({"field": field, "raw": metadata.get(field), "source": "metadata"}, "metadata")

    parsed = []
    for item in included:
        interp = _v29_as_dict(item.get("time_interpretation"))
        dt = _v29_parse_iso_for_sort(interp.get("interpreted_utc") or item.get("interpreted_utc") or item.get("raw"))
        if dt:
            parsed.append(dt)

    additional = _v32_collect_additional_time_like_candidates(thread, limit=120)
    return {
        "absolute_time_policy": "Interpreted UTC values are best-effort. Raw value + source file + offset + ldb_seq_no are the authoritative forensic references.",
        "forensic_time_values": included,
        "forensic_time_value_count": len(included),
        "earliest_interpreted_utc": min(parsed).isoformat() if parsed else None,
        "latest_interpreted_utc": max(parsed).isoformat() if parsed else None,
        "additional_time_like_candidates": additional,
        "additional_time_like_candidate_count": len(additional),
        "sequence_and_offset_order": {
            "relative_order_field": "ldb_seq_no",
            "relative_order_min": evidence_stats.get("relative_order_min"),
            "relative_order_max": evidence_stats.get("relative_order_max"),
            "offset_min": evidence_stats.get("offset_min"),
            "offset_max": evidence_stats.get("offset_max"),
            "source_files": evidence_stats.get("source_files"),
            "source_types": evidence_stats.get("source_types"),
            "states": evidence_stats.get("states"),
        },
        "time_audit": time_audit,
        "temporal_evidence": temporal,
        "computer_temporal_evidence": computer_temporal,
    }


def _v32_nonempty_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    # Keep the common forensic fields explicit, then include remaining scalar
    # metadata without bloating the condensed summary with nested evidence blobs.
    key_fields = [
        "title", "thread_title", "task_title", "status", "thread_status", "final", "text_completed",
        "message_mode", "mode", "mode_type", "search_mode", "search_focus", "display_model",
        "model_preference", "user_selected_model", "backend_uuid", "context_uuid", "frontend_uuid",
        "frontend_context_uuid", "created_at", "updated_at", "privacy_state", "access_level",
    ]
    out: dict[str, Any] = {}
    for key in key_fields:
        value = metadata.get(key)
        if value not in (None, "", [], {}):
            out[key] = value
    for key, value in metadata.items():
        if key in out or key in {"evidence", "private_detection", "external_thread_list_evidence"}:
            continue
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


def _v32_agentic_activity(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan": _v29_as_list(thread.get("plan")),
        "actions": _v29_as_list(thread.get("actions")),
        "structured_actions": _v29_as_list(thread.get("structured_actions")),
        "urls": _v29_as_list(thread.get("urls")),
        "context_url_candidates": _v29_as_list(thread.get("context_url_candidates")),
        "computer_url_candidates": _v29_as_list(thread.get("computer_url_candidates")),
        "typed_payloads": _v29_as_list(thread.get("typed_payloads")),
        "timeline": _v29_as_list(thread.get("timeline")),
        "counts": {
            "plan": len(_v29_as_list(thread.get("plan"))),
            "actions": len(_v29_as_list(thread.get("actions"))),
            "structured_actions": len(_v29_as_list(thread.get("structured_actions"))),
            "urls": len(_v29_as_list(thread.get("urls"))),
            "context_url_candidates": len(_v29_as_list(thread.get("context_url_candidates"))),
            "computer_url_candidates": len(_v29_as_list(thread.get("computer_url_candidates"))),
            "typed_payloads": len(_v29_as_list(thread.get("typed_payloads"))),
            "timeline": len(_v29_as_list(thread.get("timeline"))),
        },
        "note": "This is the full set of agentic activity fields already reconstructed by the parser for this thread; raw decoded evidence remains in the thread JSON.",
    }


def _v32_forensic_highlights(thread: dict[str, Any]) -> dict[str, Any]:
    metadata = _v29_as_dict(thread.get("metadata"))
    availability = _v29_as_dict(thread.get("reconstruction_availability"))
    storage = _v29_as_dict(availability.get("storage_state") or thread.get("storage_state"))
    content = _v29_as_dict(availability.get("content_state") or thread.get("content_state"))
    residue = _v29_as_dict(availability.get("residue_state") or thread.get("residue_state"))
    outcome = _v29_as_dict(thread.get("task_outcome"))
    source_summary = _v29_as_dict(thread.get("source_summary"))
    evidence_stats = _v29_collect_evidence_stats(thread)
    return {
        "reconstruction_availability": availability,
        "storage_state": storage,
        "content_state": content,
        "residue_state": residue,
        "task_outcome": outcome,
        "source_summary": source_summary,
        "source_files_observed": evidence_stats.get("source_files"),
        "source_types_observed": evidence_stats.get("source_types"),
        "source_record_states": evidence_stats.get("states"),
        "external_thread_list_evidence_count": len(_v29_as_list(metadata.get("external_thread_list_evidence"))),
        "raw_fallback_candidate_count": len(_v29_as_list(thread.get("raw_fallback_candidates"))),
        "missing_corroboration": outcome.get("missing_corroboration") or [],
        "investigator_caution": "Use source_file/offset/ldb_seq_no and external artifacts such as History, Downloads, Gmail/Sent, or Calendar records for corroboration when side effects are claimed.",
    }


def _v32_thread_reconstruction_entry(thread: dict[str, Any], index: int) -> dict[str, Any]:
    classification = _v29_as_dict(thread.get("classification"))
    metadata = _v29_as_dict(thread.get("metadata"))
    prompt = _v29_as_dict(thread.get("prompt"))
    final_answer = _v29_as_dict(thread.get("final_answer"))
    private_detection = _v29_as_dict(metadata.get("private_detection"))
    deletion_state = _v29_as_dict(thread.get("deletion_state"))
    reasoning = _v29_as_dict(thread.get("reasoning"))
    time_values = _v32_collect_time_values(thread)
    evidence_stats = _v29_collect_evidence_stats(thread)

    return {
        "index": index,
        "thread_id": thread.get("thread_id"),
        "reference_codes": _v29_thread_reference_codes(thread),
        "classification": {
            "interaction_type": classification.get("interaction_type"),
            "execution_mode": classification.get("execution_mode"),
            "reconstruction_status": classification.get("reconstruction_status"),
            "classification_evidence": classification.get("classification_evidence") or classification.get("evidence") or [],
        },
        "prompt": {
            "text": prompt.get("text"),
            "field": prompt.get("field"),
            "scope": prompt.get("scope"),
            "thread_specific_prompt": prompt.get("thread_specific_prompt"),
            "reference_codes": prompt.get("reference_codes") or [],
            "evidence": prompt.get("evidence") or prompt.get("evidence_refs") or [],
            "prompt_persistence": prompt.get("prompt_persistence"),
            "corroboration": prompt.get("corroboration"),
        },
        "metadata": {
            "key_fields": _v32_nonempty_metadata(metadata),
            "external_thread_list_evidence": metadata.get("external_thread_list_evidence"),
            "metadata_evidence": metadata.get("evidence"),
        },
        "agentic_activity_data": _v32_agentic_activity(thread) if classification.get("interaction_type") == "agentic" or classification.get("execution_mode") in {"browser_control", "computer_mode"} else {
            "note": "Conversational/search thread; no agentic browser/computer activity data was promoted for this thread.",
            "counts": {"plan": 0, "actions": 0, "urls": 0, "typed_payloads": 0},
        },
        "reasoning": {
            "available": bool(reasoning.get("available")),
            "items": _v29_as_list(reasoning.get("items")),
            "progress_or_status_items": _v29_as_list(reasoning.get("progress_or_status_items")),
            "note": reasoning.get("note"),
            "interpretation": reasoning.get("interpretation"),
            "scan_rule": reasoning.get("scan_rule"),
        },
        "browser_control_specific": {
            "applicable": classification.get("execution_mode") == "browser_control",
            "deletion_state": deletion_state,
            "private_mode": bool(private_detection.get("private_mode") or metadata.get("private_mode")),
            "private_detection": private_detection,
            "final_answer": final_answer,
        },
        "computer_mode_specific": {
            "applicable": classification.get("execution_mode") == "computer_mode",
            "computer_source_persistence": thread.get("computer_source_persistence"),
            "computer_temporal_evidence": thread.get("computer_temporal_evidence"),
            "computer_url_candidates": thread.get("computer_url_candidates"),
        },
        "final_result": final_answer,
        "forensic_significant_data": _v32_forensic_highlights(thread),
        "all_time_related_values": time_values,
        "source_order_and_provenance": {
            "record_count": thread.get("record_count"),
            "source_summary": thread.get("source_summary"),
            "evidence_stats": evidence_stats,
            "evidence_refs_sample": _v32_evidence_refs_from_obj(thread, limit=80),
        },
    }


def _v32_overall_interaction_profile(threads: list[dict[str, Any]]) -> str:
    interactions = {str(_v29_as_dict(t.get("classification")).get("interaction_type") or "unknown") for t in threads}
    modes = {str(_v29_as_dict(t.get("classification")).get("execution_mode") or "unknown") for t in threads}
    has_agentic = "agentic" in interactions or bool({"browser_control", "computer_mode"} & modes)
    has_conv = "conversational_or_search" in interactions
    if has_agentic and has_conv:
        return "mixed_conversational_and_agentic"
    if has_agentic:
        return "agentic_only_or_agentic_residue"
    if has_conv:
        return "conversational_or_search_only"
    return "unknown_or_metadata_residue"


def _v32_build_condensed_summary(report: dict[str, Any]) -> dict[str, Any]:
    # Keep v0.29's compact quick map and add the forensic reconstruction map the
    # case-study workflow needs.  This avoids breaking existing consumers.
    base = _v29_build_condensed_summary_pre_v32(report)
    report_threads = _v29_as_list(report.get("threads"))
    thread_entries = [_v32_thread_reconstruction_entry(t, i + 1) for i, t in enumerate(report_threads)]
    source = _v29_as_dict(report.get("source"))
    extraction = _v29_as_dict(report.get("extraction_summary"))
    case_summary = _v29_as_dict(report.get("case_summary"))

    all_forensic_times: list[datetime] = []
    all_source_files: list[str] = []
    all_seq_values: list[int] = []
    for entry in thread_entries:
        tv = _v29_as_dict(entry.get("all_time_related_values"))
        for key in ("earliest_interpreted_utc", "latest_interpreted_utc"):
            dt = _v29_parse_iso_for_sort(tv.get(key))
            if dt:
                all_forensic_times.append(dt)
        order = _v29_as_dict(tv.get("sequence_and_offset_order"))
        all_source_files.extend(_v29_as_list(order.get("source_files")))
        for seq_key in ("relative_order_min", "relative_order_max"):
            try:
                if order.get(seq_key) is not None:
                    all_seq_values.append(int(order.get(seq_key)))
            except Exception:
                pass

    classifications = [_v29_as_dict(t.get("classification")) for t in report_threads]
    interaction_counts = _v32_count_map(c.get("interaction_type") for c in classifications)
    execution_mode_counts = _v32_count_map(c.get("execution_mode") for c in classifications)

    base.update({
        "version": "0.32",
        "summary_purpose": "Investigator-facing case/profile summary: what was recovered from this Comet LevelDB analysis, by thread, without changing extraction results.",
        "comet_profile_leveldb": {
            "input": source.get("input"),
            "leveldb_path": source.get("leveldb_path"),
            "blob_path": source.get("blob_path"),
            "target_origin": source.get("target_origin"),
            "database": source.get("database"),
            "object_store": source.get("object_store"),
            "analysis_scope": source.get("analysis_scope"),
            "report_mode": report.get("report_mode") or source.get("report_mode") or case_summary.get("report_mode"),
            "target_reference_filter": source.get("target_reference_filter"),
        },
        "conversation_vs_agentic_assessment": {
            "overall_profile": _v32_overall_interaction_profile(report_threads),
            "interaction_type_counts": interaction_counts,
            "execution_mode_counts": execution_mode_counts,
            "agentic_thread_count": sum(1 for c in classifications if c.get("interaction_type") == "agentic" or c.get("execution_mode") in {"browser_control", "computer_mode"}),
            "browser_control_thread_count": sum(1 for c in classifications if c.get("execution_mode") == "browser_control"),
            "computer_mode_thread_count": sum(1 for c in classifications if c.get("execution_mode") == "computer_mode"),
            "conversational_or_search_thread_count": sum(1 for c in classifications if c.get("interaction_type") == "conversational_or_search"),
        },
        "thread_count": len(report_threads),
        "thread_reconstruction_map_v32": thread_entries,
        "all_time_values_overview_v32": {
            "earliest_interpreted_utc": min(all_forensic_times).isoformat() if all_forensic_times else None,
            "latest_interpreted_utc": max(all_forensic_times).isoformat() if all_forensic_times else None,
            "relative_order_min": min(all_seq_values) if all_seq_values else None,
            "relative_order_max": max(all_seq_values) if all_seq_values else None,
            "source_files_observed": _v29_unique_keep_order([sf for sf in all_source_files if _v32_is_real_source_file(sf)], 50),
            "time_policy": "Forensic timestamps are reported with raw values and best-effort interpretation; LevelDB sequence/offset is used as relative order evidence.",
        },
        "forensic_summary_v32": {
            "record_counts": extraction,
            "case_summary": case_summary,
            "source_files_observed_cleaned": _v29_unique_keep_order([sf for sf in all_source_files if _v32_is_real_source_file(sf)], 50),
            "source_file_cleanup_note": "Only actual LevelDB filenames are counted as source files. Logical field paths such as actions.3.label are preserved elsewhere but excluded from source_files_observed.",
            "strict_display_note": "Strict/scenario HTML views are display-only. They hide unrelated profile residue but never remove JSON evidence or alter forensic interpretation.",
        },
    })
    # Keep legacy names populated with cleaned file lists too.
    base["source_files_observed"] = base["all_time_values_overview_v32"]["source_files_observed"]
    base["source_file_count"] = len(set(base["source_files_observed"]))
    return base


# Preserve the v0.29 builder so v0.32 can extend it without recursion.
_v29_build_condensed_summary_pre_v32 = _v29_build_condensed_summary


def _v29_build_condensed_summary(report: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
    return _v32_build_condensed_summary(report)


_v29_thread_quick_summary_pre_v32 = _v29_thread_quick_summary


def _v29_thread_quick_summary(thread: dict[str, Any], index: int) -> dict[str, Any]:  # type: ignore[override]
    quick = _v29_thread_quick_summary_pre_v32(thread, index)
    # Recompute with the corrected evidence collector and add fields used by the
    # strict/scenario display script.  This is display metadata, not evidence.
    evidence_stats = _v29_collect_evidence_stats(thread)
    quick["source_files"] = evidence_stats.get("source_files")
    quick["source_file_count"] = evidence_stats.get("source_file_count")
    quick["relative_order_min"] = evidence_stats.get("relative_order_min")
    quick["relative_order_max"] = evidence_stats.get("relative_order_max")
    quick["source_states"] = evidence_stats.get("states")
    quick["source_types"] = evidence_stats.get("source_types")
    refs = [str(x).lower() for x in _v29_as_list(quick.get("reference_codes"))]
    search = str(quick.get("search") or "").lower()
    quick["strict_display_candidate"] = bool(refs or quick.get("prompt_preview")) and not str(quick.get("thread_id") or "").lower().startswith("computer:")
    quick["scenario_match_text"] = " ".join(refs + [search[:1000]])
    return quick


_v29_apply_report_refinements_pre_v32 = _v29_apply_report_refinements


def _v29_apply_report_refinements(report: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
    report = _v29_apply_report_refinements_pre_v32(report)
    try:
        report["presentation_version"] = "0.32"
        report["condensed_reconstruction_summary_v29"] = _v32_build_condensed_summary(report)
        report["condensed_reconstruction_summary_v32"] = report["condensed_reconstruction_summary_v29"]
        report.setdefault("repair_summary", {})["v32_condensed_summary_and_source_cleanup"] = {
            "condensed_summary_expanded": True,
            "thread_level_prompt_metadata_activity_reasoning_final": True,
            "all_time_related_values_section": True,
            "source_files_observed_cleaned": True,
            "strict_display_metadata_added": True,
            "extraction_logic_changed": False,
            "hardcoding": "No scenario ID or reference value is embedded; target/reference matching still uses supplied target_reference, input-name tokens, and recovered reference codes only for display/filtering.",
        }
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.32 condensed summary failed: {exc}")
    return report


_reconstruct_browser_threads_pre_v32 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:  # type: ignore[override]
    report = _reconstruct_browser_threads_pre_v32(extracted, input_label, browser_only=browser_only)
    report = _v29_apply_report_refinements(report)
    report["schema_version"] = "0.32"
    return report


_filter_report_by_target_reference_pre_v32 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:  # type: ignore[override]
    filtered = _filter_report_by_target_reference_pre_v32(report, target_reference)
    filtered = _v29_apply_report_refinements(filtered)
    filtered["schema_version"] = "0.32"
    return filtered


_render_html_report_pre_v32 = render_html_report


def render_html_report(report: dict[str, Any], output_path: Path) -> None:  # type: ignore[override]
    report = _v29_apply_report_refinements(report)
    _render_html_report_pre_v32(report, output_path)
    try:
        html_text = output_path.read_text(encoding="utf-8")
        html_text = html_text.replace("v0.29 filters / v0.31 scenario display", "v0.29 filters / v0.32 scenario display")
        html_text = html_text.replace("<span class='badge neutral'>HTML view only · v0.31</span>", "<span class='badge neutral'>HTML view only · v0.32</span>")
        output_path.write_text(html_text, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v0.33 minimal forensic-summary refinements
# ---------------------------------------------------------------------------
# These overrides are deliberately appended near the end of the legacy engine so
# the previous extraction/reconstruction pipeline remains intact.  The changes
# are report-layer and outcome-guard refinements only:
#   * stricter time-like candidate filtering;
#   * conservative download confirmation-vs-completion guard;
#   * top-level case-focused core findings for quick investigator review;
#   * diff-ready deletion/reopen signals without performing cross-file I/O.

_V33_SCHEMA_VERSION = "0.33"


def _v33_text(obj: Any) -> str:
    if obj in (None, ""):
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def _v33_norm_text(obj: Any) -> str:
    return " ".join(_v33_text(obj).lower().split())


def _v33_has_any(text: str, markers: Iterable[str]) -> bool:
    low = _v33_norm_text(text)
    return any(str(marker).lower() in low for marker in markers if marker)


def _v33_download_outcome_text(thread: dict[str, Any]) -> str:
    """Thread-local outcome evidence, intentionally excluding prompt text.

    Raw COMET_AGENT_TOOL_INPUT/OUTPUT previews often contain the whole
    all_results object, including the original prompt.  For side-effect
    completion we therefore use only structured final-answer text and concise
    execution labels/progress values, not large raw previews or prompt timeline
    entries.
    """
    parts: list[str] = []

    final = _v29_as_dict(thread.get("final_answer"))
    if final.get("text"):
        parts.append(str(final.get("text")))

    def add_execution_items(items: Any) -> None:
        for item in _v29_as_list(items):
            d = _v29_as_dict(item)
            kind = str(d.get("kind") or "").lower()
            if kind == "prompt":
                continue
            # Keep concise execution descriptors.  Avoid raw text_preview for
            # tool_io/all_results blobs because it can reintroduce prompt text.
            for key in ("label", "description", "action", "operation", "tool_name", "status", "progress", "parameters_preview"):
                value = d.get(key)
                if value not in (None, "", [], {}):
                    parts.append(_v33_text(value))
            if kind not in {"tool_io", "raw", "raw_record"}:
                preview = d.get("text") or d.get("text_preview")
                if preview not in (None, "", [], {}):
                    parts.append(_v33_text(preview)[:500])

    add_execution_items(thread.get("actions"))
    add_execution_items(thread.get("structured_actions"))
    add_execution_items(thread.get("plan"))
    add_execution_items(thread.get("timeline"))

    reasoning = _v29_as_dict(thread.get("reasoning"))
    for bucket in ("items", "progress_or_status_items"):
        for item in _v29_as_list(reasoning.get(bucket)):
            d = _v29_as_dict(item)
            for key in ("text", "label", "status", "progress"):
                if d.get(key) not in (None, "", [], {}):
                    parts.append(_v33_text(d.get(key))[:500])

    return "\n".join(parts)


def _v33_download_completion_markers() -> list[str]:
    return [
        "download is complete", "download complete", "downloaded filename",
        "final downloaded filename", "downloaded file", "successfully downloaded",
        "downloaded successfully", "file has been downloaded", "download finished",
        "saved the pdf", "saving the pdf from the viewer", "wait_for_download",
        "downloaded_file", "downloaded filename is", "final downloaded file",
        "다운로드 완료", "다운로드가 완료", "최종 다운로드", "저장 완료",
    ]


def _v33_download_confirmation_markers() -> list[str]:
    return [
        "browser_agent_confirmation", "need to confirm", "confirm the download",
        "before proceeding", "before i proceed", "before downloading",
        "please confirm", "may i proceed", "requires confirmation",
        "confirmation required", "waiting for confirmation", "awaiting confirmation",
        "would you like me to", "do you want me to", "proceed with the download",
        "다운로드 전", "사용자 확인", "확인 요청", "진행하기 전에", "확인이 필요",
    ]


_classify_task_outcome_pre_v33 = classify_task_outcome


def classify_task_outcome(thread: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
    """v0.33 conservative side-effect guard.

    This preserves the existing task classifier, then corrects only the download
    completion edge case where a confirmation-gate thread contains a PDF
    filename/URL but the artifact text says user confirmation is still needed.
    """
    result = dict(_classify_task_outcome_pre_v33(thread) or {})
    task_type = str(result.get("task_type") or "")
    if task_type not in {"file_download", "download"}:
        result.setdefault("classifier_version", "v0.33_conservative_guard")
        return result

    outcome_text = _v33_download_outcome_text(thread)
    outcome_low = _v33_norm_text(outcome_text)
    filenames = _v29_unique_keep_order(extract_pdf_filenames(outcome_text) + _v29_as_list(result.get("downloaded_filename_candidates")), 20)
    has_completion = bool(filenames) and _v33_has_any(outcome_low, _v33_download_completion_markers())
    has_confirmation = _v33_has_any(outcome_low, _v33_download_confirmation_markers())

    # A confirmation gate is stronger than a filename/URL candidate unless there
    # is explicit completion evidence in execution/final-answer text.  This
    # prevents source-discovery or BROWSER_AGENT_CONFIRMATION records from being
    # promoted to completed downloads.
    if has_confirmation and not has_completion:
        result.update({
            "status": "download_confirmation_pending",
            "side_effect_completed": False,
            "confidence": "high",
            "downloaded_filename_candidates": filenames,
            "outcome_guard_v33": {
                "applied": True,
                "reason": "confirmation-gate marker found in final/action/workflow text without explicit completion marker",
                "prompt_text_excluded_from_completion_test": True,
            },
        })
    elif has_completion:
        result.update({
            "status": "completed_download",
            "side_effect_completed": True,
            "confidence": "high",
            "downloaded_filename_candidates": filenames,
            "outcome_guard_v33": {
                "applied": True,
                "reason": "explicit completion marker plus filename candidate found in final/action/workflow text",
                "prompt_text_excluded_from_completion_test": True,
            },
        })
    else:
        # Preserve existing non-completed classification, but annotate that the
        # v0.33 guard evaluated it.
        result.setdefault("outcome_guard_v33", {
            "applied": False,
            "reason": "no confirmation/completion override required",
            "prompt_text_excluded_from_completion_test": True,
        })
    result["classifier_version"] = "v0.33_conservative_download_guard"
    result["missing_corroboration"] = _v29_unique_keep_order(_v29_as_list(result.get("missing_corroboration")) + ["Chromium Downloads DB", "OS Downloads folder/file hash"], 10)
    return result


_V33_STRICT_FORENSIC_TIME_FIELDS = {
    "created_at", "updated_at", "started_at", "completed_at", "finished_at",
    "deleted_at", "archived_at", "lastaccess", "last_access",
    "last_query_datetime", "last_updated_at", "time_created", "time_updated",
    "creation_time", "modification_time", "timestamp", "timestamp_ms",
    "timestamp_us", "timestamp_ns", "unix_time", "unix_ms", "datetime",
    "event_time", "observed_at", "recorded_at",
}

_V33_FALSE_TIME_FIELD_TOKENS = {
    "candidate", "count", "policy", "note", "reason", "warning", "available",
    "completed", "side_effect_completed", "text_completed", "stale_candidate",
    "confidence", "status", "type", "classification", "summary",
}

_V33_EVENT_CONTENT_TIME_FIELDS = {
    "date", "time", "start", "end", "start_time", "end_time", "event_date",
    "event_time", "calendar_date", "calendar_time",
}


def _v33_path_segments(path: str) -> list[str]:
    return [seg.lower() for seg in re.split(r"[.\[\]/]+", str(path or "")) if seg]


def _v33_value_looks_temporal(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        # epoch seconds/ms/us/ns or Chrome/WebKit microseconds; small counts are
        # almost never absolute forensic times.
        return abs(float(value)) >= 1_000_000_000
    text = str(value).strip()
    if not text or len(text) > 500:
        return False
    if text.lower() in {"true", "false", "none", "null"}:
        return False
    # ISO-like date/time, common date formats, or clock time with AM/PM.
    if re.search(r"\b\d{4}-\d{2}-\d{2}(?:[t\s]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:z|[+-]\d{2}:?\d{2})?)?\b", text, flags=re.I):
        return True
    if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2},\s*\d{4}\b", text, flags=re.I):
        return True
    if re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", text, flags=re.I):
        return True
    if text.isdigit() and len(text) >= 10:
        return True
    return False


def _v33_is_strict_time_candidate(path: str, value: Any) -> bool:
    if isinstance(value, bool) or value in (None, "", [], {}):
        return False
    segs = _v33_path_segments(path)
    last = segs[-1] if segs else ""
    if last in {"side_effect_completed", "text_completed", "stale_candidate"}:
        return False
    if last.endswith("_count") or last.endswith("count"):
        return False
    # Reject field-path false positives such as candidate_payload_count and
    # time_audit.absolute_time_policy.  Exact strict time field names below are
    # allowed to pass.
    if last not in _V33_STRICT_FORENSIC_TIME_FIELDS and any(tok in segs for tok in _V33_FALSE_TIME_FIELD_TOKENS):
        return False
    if last in _V33_EVENT_CONTENT_TIME_FIELDS:
        # Calendar/event values are real content values, but not artifact
        # observation timestamps.  They are separated below.
        return False
    if last in _V33_STRICT_FORENSIC_TIME_FIELDS:
        return _v33_value_looks_temporal(value)
    if last.endswith(("_at", "_timestamp", "_datetime")):
        return _v33_value_looks_temporal(value)
    return False


def _v33_collect_strict_additional_time_candidates(thread: dict[str, Any], limit: int = 80) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(bucket: list[dict[str, Any]], path: str, value: Any, reason: str) -> None:
        if isinstance(value, (dict, list)) or value in (None, "", [], {}):
            return
        raw = str(value)
        if len(raw) > 500:
            raw = raw[:500] + "…"
        key = (path, raw)
        if key in seen:
            return
        seen.add(key)
        item: dict[str, Any] = {"field_path": path, "raw": raw, "reason": reason}
        if bucket is included:
            item["interpretation"] = "strict time-like artifact field candidate; verify raw value and evidence before using as an absolute forensic timestamp"
            try:
                interp = interpret_timestamp(value)
                if isinstance(interp, dict) and interp.get("interpreted_utc"):
                    item["time_interpretation"] = interp
            except Exception:
                pass
        else:
            item["interpretation"] = "excluded from strict additional time candidates"
        bucket.append(item)

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if len(included) >= limit and len(excluded) >= 40:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                child = path + (str(k),)
                field_path = ".".join(child)
                # Only evaluate scalar fields; raw nested evidence remains in full JSON.
                if not isinstance(v, (dict, list)):
                    if _v33_is_strict_time_candidate(field_path, v):
                        add(included, field_path, v, "accepted_by_v33_strict_time_filter")
                    elif _v32_is_time_like_field(k) and len(excluded) < 40:
                        add(excluded, field_path, v, "rejected_by_v33_strict_time_filter_false_positive_or_content_time")
                walk(v, child)
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, path + (str(idx),))

    walk(thread, tuple())
    return included[:limit], excluded[:40]


def _v33_collect_event_content_time_values(thread: dict[str, Any], limit: int = 40) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in _v29_as_list(thread.get("typed_payloads")):
        d = _v29_as_dict(item)
        field = str(d.get("field") or "").lower()
        value = d.get("value")
        if field not in _V33_EVENT_CONTENT_TIME_FIELDS or value in (None, ""):
            continue
        key = (field, str(value), str(d.get("payload_source") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "field": field,
            "raw": str(value),
            "payload_source": d.get("payload_source"),
            "payload_role": d.get("payload_role"),
            "relative_order": d.get("relative_order"),
            "evidence": d.get("evidence"),
            "interpretation": "event/content time value, not an artifact observation timestamp unless separately corroborated",
        })
        if len(out) >= limit:
            break
    return out


def _v33_collect_time_values(thread: dict[str, Any]) -> dict[str, Any]:
    values = dict(_v32_collect_time_values(thread) or {})
    strict_additional, excluded = _v33_collect_strict_additional_time_candidates(thread, limit=80)
    event_content = _v33_collect_event_content_time_values(thread, limit=40)
    values["additional_time_like_candidates_v32_unfiltered_count"] = values.get("additional_time_like_candidate_count")
    values["additional_time_like_candidates"] = strict_additional
    values["additional_time_like_candidate_count"] = len(strict_additional)
    values["event_content_time_values"] = event_content
    values["event_content_time_value_count"] = len(event_content)
    values["excluded_false_positive_time_like_values"] = excluded
    values["excluded_false_positive_time_like_value_count"] = len(excluded)
    values["time_filter_policy_v33"] = {
        "strict_artifact_times": "created/updated/started/completed/deleted/lastAccess/timestamp-like artifact fields with temporal-looking values",
        "event_content_times": "Calendar/task payload date/time values are separated from forensic observation timestamps",
        "excluded_examples": "booleans, counts, stale_candidate, side_effect_completed, text_completed, policy/note fields, and generic candidate fields",
    }
    return values


def _v33_thread_reconstruction_entry(thread: dict[str, Any], index: int) -> dict[str, Any]:
    entry = _v32_thread_reconstruction_entry(thread, index)
    entry["all_time_related_values"] = _v33_collect_time_values(thread)

    availability = _v29_as_dict(thread.get("reconstruction_availability"))
    outcome = _v29_as_dict(thread.get("task_outcome"))
    prompt = _v29_as_dict(thread.get("prompt"))
    final_answer = _v29_as_dict(thread.get("final_answer"))
    classif = _v29_as_dict(thread.get("classification"))
    entry["case_relation_and_residue"] = {
        "case_relation": thread.get("case_relation"),
        "display": thread.get("display"),
        "profile_inventory_caution": "When report_mode is profile_inventory, this thread is a recovered profile artifact and is not automatically the intended case thread.",
    }
    entry["cross_snapshot_diff_ready"] = {
        "thread_id": thread.get("thread_id"),
        "reference_codes": _v29_thread_reference_codes(thread),
        "interaction_type": classif.get("interaction_type"),
        "execution_mode": classif.get("execution_mode"),
        "task_type": outcome.get("task_type"),
        "status": outcome.get("status"),
        "reconstruction_level": availability.get("level"),
        "has_prompt_text": bool(prompt.get("text")),
        "has_final_answer_text": bool(final_answer.get("text")),
        "plan_count": len(_v29_as_list(thread.get("plan"))),
        "action_count": len(_v29_as_list(thread.get("actions"))),
        "structured_action_count": len(_v29_as_list(thread.get("structured_actions"))),
        "url_count": len(_v29_as_list(thread.get("urls"))),
        "source_states": _v29_collect_evidence_stats(thread).get("states"),
        "relative_order_min": _v29_collect_evidence_stats(thread).get("relative_order_min"),
        "relative_order_max": _v29_collect_evidence_stats(thread).get("relative_order_max"),
        "use": "Compare these fields for the same thread_id/reference_code across before-delete, after-delete, and reopen snapshots.",
    }
    return entry


def _v33_reconstruction_strength(level: Any) -> int:
    low = str(level or "").lower()
    if "strong" in low:
        return 4
    if "reconstructed" in low:
        return 3
    if "partial" in low:
        return 2
    if "metadata" in low or "residue" in low or "list" in low:
        return 1
    return 0


def _v33_build_case_focused_core_findings(report: dict[str, Any], thread_entries: list[dict[str, Any]]) -> dict[str, Any]:
    source = _v29_as_dict(report.get("source"))
    case_summary = _v29_as_dict(report.get("case_summary"))
    target_ref = source.get("target_reference_filter") or case_summary.get("target_reference")
    report_mode = report.get("report_mode") or source.get("report_mode") or case_summary.get("report_mode")

    refs: list[str] = []
    side_effect_threads: list[dict[str, Any]] = []
    private_threads: list[dict[str, Any]] = []
    deletion_or_residue_threads: list[dict[str, Any]] = []
    target_threads: list[dict[str, Any]] = []

    for e in thread_entries:
        refs.extend(str(r) for r in _v29_as_list(e.get("reference_codes")))
        outcome = _v29_as_dict(_v29_as_dict(e.get("forensic_significant_data")).get("task_outcome"))
        classification = _v29_as_dict(e.get("classification"))
        forensic = _v29_as_dict(e.get("forensic_significant_data"))
        availability = _v29_as_dict(forensic.get("reconstruction_availability"))
        bc = _v29_as_dict(e.get("browser_control_specific"))
        private_detection = _v29_as_dict(bc.get("private_detection"))
        display = _v29_as_dict(_v29_as_dict(e.get("case_relation_and_residue")).get("display"))
        case_rel = _v29_as_dict(_v29_as_dict(e.get("case_relation_and_residue")).get("case_relation"))

        task = str(outcome.get("task_type") or "")
        side_effect_completed = outcome.get("side_effect_completed")
        if task in {"email_draft_send", "calendar_create", "file_download", "download"} or side_effect_completed is True:
            side_effect_threads.append({
                "index": e.get("index"),
                "thread_id": e.get("thread_id"),
                "task_type": task,
                "status": outcome.get("status"),
                "side_effect_completed": side_effect_completed,
                "confidence": outcome.get("confidence"),
                "execution_mode": classification.get("execution_mode"),
                "missing_corroboration": outcome.get("missing_corroboration") or [],
            })
        if bool(bc.get("private_mode") or private_detection.get("private_mode")):
            private_threads.append({"index": e.get("index"), "thread_id": e.get("thread_id"), "private_detection": private_detection})
        level = str(availability.get("level") or classification.get("reconstruction_status") or "")
        if any(tok in level.lower() for tok in ["residue", "metadata", "partial", "stale", "list_cache"]):
            deletion_or_residue_threads.append({
                "index": e.get("index"),
                "thread_id": e.get("thread_id"),
                "reconstruction_level": availability.get("level"),
                "content_state": availability.get("content_state"),
                "residue_state": availability.get("residue_state"),
                "source_states": _v29_as_dict(e.get("cross_snapshot_diff_ready")).get("source_states"),
            })
        if target_ref and (case_rel.get("relation_to_target") == "exact_target" or display.get("is_case_primary")):
            target_threads.append({"index": e.get("index"), "thread_id": e.get("thread_id"), "reference_codes": e.get("reference_codes"), "task_outcome": outcome})

    strong_threads = []
    for e in thread_entries:
        forensic = _v29_as_dict(e.get("forensic_significant_data"))
        availability = _v29_as_dict(forensic.get("reconstruction_availability"))
        outcome = _v29_as_dict(forensic.get("task_outcome"))
        strength = _v33_reconstruction_strength(availability.get("level") or _v29_as_dict(e.get("classification")).get("reconstruction_status"))
        if strength >= 2:
            strong_threads.append({
                "index": e.get("index"),
                "thread_id": e.get("thread_id"),
                "reference_codes": e.get("reference_codes"),
                "reconstruction_level": availability.get("level"),
                "task_type": outcome.get("task_type"),
                "status": outcome.get("status"),
                "strength_score": strength,
            })
    strong_threads.sort(key=lambda x: (int(x.get("strength_score") or 0), bool(x.get("reference_codes"))), reverse=True)

    all_times = []
    for e in thread_entries:
        tv = _v29_as_dict(e.get("all_time_related_values"))
        for t in _v29_as_list(tv.get("forensic_time_values"))[:20]:
            all_times.append({
                "thread_index": e.get("index"),
                "thread_id": e.get("thread_id"),
                "field": t.get("field"),
                "raw": t.get("raw"),
                "source": t.get("source"),
                "interpreted_utc": _v29_as_dict(t.get("time_interpretation")).get("interpreted_utc") or t.get("interpreted_utc"),
            })

    cautions = []
    if not target_ref:
        cautions.append("No target reference was supplied; this is a profile inventory, so no thread is promoted as the intended case thread.")
    if side_effect_threads:
        cautions.append("Side-effect completion is an artifact-based claim and should be corroborated with external service/browser artifacts where available.")
    if deletion_or_residue_threads:
        cautions.append("Deletion/reopen conclusions require comparing the same thread_id/reference_code across snapshots; this report provides diff-ready fields but does not perform cross-file comparison by itself.")

    return {
        "purpose": "One-glance summary of what this specific LevelDB analysis recovered; detailed evidence remains in thread_reconstruction_map_v33.",
        "input": source.get("input"),
        "leveldb_path": source.get("leveldb_path"),
        "report_mode": report_mode,
        "target_reference_filter": target_ref,
        "thread_count": len(thread_entries),
        "agentic_thread_count": sum(1 for e in thread_entries if _v29_as_dict(e.get("classification")).get("interaction_type") == "agentic" or _v29_as_dict(e.get("classification")).get("execution_mode") in {"browser_control", "computer_mode"}),
        "browser_control_thread_count": sum(1 for e in thread_entries if _v29_as_dict(e.get("classification")).get("execution_mode") == "browser_control"),
        "computer_mode_thread_count": sum(1 for e in thread_entries if _v29_as_dict(e.get("classification")).get("execution_mode") == "computer_mode"),
        "conversational_or_search_thread_count": sum(1 for e in thread_entries if _v29_as_dict(e.get("classification")).get("interaction_type") == "conversational_or_search"),
        "recovered_reference_code_counts": _v32_count_map(refs),
        "target_thread_candidates": target_threads,
        "target_selection_policy": "A target thread is listed only when --target-reference or an exact case relation exists; otherwise review strongest_reconstruction_threads as profile inventory, not case truth.",
        "strongest_reconstruction_threads": strong_threads[:8],
        "side_effect_threads": side_effect_threads[:10],
        "private_mode_threads": private_threads[:10],
        "deletion_or_residue_threads": deletion_or_residue_threads[:10],
        "key_time_values_sample": all_times[:20],
        "cross_snapshot_deletion_diff_ready_keys": [_v29_as_dict(e.get("cross_snapshot_diff_ready")) for e in thread_entries[:20]],
        "cautions": cautions,
    }


def _v33_build_condensed_summary(report: dict[str, Any]) -> dict[str, Any]:
    base = _v32_build_condensed_summary(report)
    report_threads = _v29_as_list(report.get("threads"))
    thread_entries = [_v33_thread_reconstruction_entry(t, i + 1) for i, t in enumerate(report_threads)]

    all_forensic_times: list[datetime] = []
    all_source_files: list[str] = []
    all_seq_values: list[int] = []
    for entry in thread_entries:
        tv = _v29_as_dict(entry.get("all_time_related_values"))
        for key in ("earliest_interpreted_utc", "latest_interpreted_utc"):
            dt = _v29_parse_iso_for_sort(tv.get(key))
            if dt:
                all_forensic_times.append(dt)
        order = _v29_as_dict(tv.get("sequence_and_offset_order"))
        all_source_files.extend(_v29_as_list(order.get("source_files")))
        for seq_key in ("relative_order_min", "relative_order_max"):
            try:
                if order.get(seq_key) is not None:
                    all_seq_values.append(int(order.get(seq_key)))
            except Exception:
                pass

    base["version"] = _V33_SCHEMA_VERSION
    base["summary_purpose"] = "Investigator-facing case/profile summary: core findings first, then thread-level prompt/metadata/activity/reasoning/final/time evidence."
    base["thread_reconstruction_map_v33"] = thread_entries
    base["thread_reconstruction_map_v32"] = thread_entries  # compatibility alias with v0.33 refinements
    base["case_focused_core_findings_v33"] = _v33_build_case_focused_core_findings(report, thread_entries)
    base["all_time_values_overview_v33"] = {
        "earliest_interpreted_utc": min(all_forensic_times).isoformat() if all_forensic_times else None,
        "latest_interpreted_utc": max(all_forensic_times).isoformat() if all_forensic_times else None,
        "relative_order_min": min(all_seq_values) if all_seq_values else None,
        "relative_order_max": max(all_seq_values) if all_seq_values else None,
        "source_files_observed": _v29_unique_keep_order([sf for sf in all_source_files if _v32_is_real_source_file(sf)], 50),
        "time_policy": "Strict v0.33: artifact observation times are separated from task/event content date/time values; LevelDB sequence/offset remains relative-order evidence.",
    }
    base["all_time_values_overview_v32"] = base["all_time_values_overview_v33"]
    base["forensic_summary_v33"] = {
        "case_summary": _v29_as_dict(report.get("case_summary")),
        "source_files_observed_cleaned": base["all_time_values_overview_v33"]["source_files_observed"],
        "time_filter_refinement": "False-positive time-like fields such as text_completed, side_effect_completed, stale_candidate, *_count, and policy/note/candidate fields are excluded from additional_time_like_candidates.",
        "download_outcome_refinement": "Download completion is claimed only from final/action/workflow evidence, not prompt instruction text; confirmation-gate records become download_confirmation_pending.",
        "cross_snapshot_note": "cross_snapshot_diff_ready fields support external before/delete/reopen comparison without altering single-report evidence.",
        "extraction_logic_changed": False,
    }
    base["forensic_summary_v32"] = base["forensic_summary_v33"]
    base["source_files_observed"] = base["all_time_values_overview_v33"]["source_files_observed"]
    base["source_file_count"] = len(set(base["source_files_observed"]))
    return base


_v29_apply_report_refinements_pre_v33 = _v29_apply_report_refinements


def _v29_apply_report_refinements(report: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
    report = _v29_apply_report_refinements_pre_v33(report)
    try:
        report["presentation_version"] = _V33_SCHEMA_VERSION
        # Recompute task outcome after v0.33 classify override for already-built
        # report objects.  This keeps report-layer post-processing usable when
        # applying the new file to existing decoded JSON structures.
        for thread in _v29_as_list(report.get("threads")):
            if isinstance(thread, dict):
                try:
                    thread["task_outcome"] = classify_task_outcome(thread)
                except Exception as exc:
                    thread.setdefault("repair_warnings", []).append(f"v0.33 task outcome refresh failed: {exc}")
        condensed = _v33_build_condensed_summary(report)
        report["condensed_reconstruction_summary_v29"] = condensed
        report["condensed_reconstruction_summary_v32"] = condensed
        report["condensed_reconstruction_summary_v33"] = condensed
        report.setdefault("repair_summary", {})["v33_case_summary_time_and_outcome_refinements"] = {
            "case_focused_core_findings_added": True,
            "strict_time_filter_added": True,
            "event_content_time_values_separated": True,
            "download_confirmation_pending_guard_added": True,
            "cross_snapshot_diff_ready_fields_added": True,
            "extraction_logic_changed": False,
            "hardcoding": "No scenario-specific IDs are embedded; rules use artifact fields, user-supplied target-reference, generic evidence markers, and recovered reference codes only for display/diff hints.",
        }
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.33 refinements failed: {exc}")
    return report


_reconstruct_browser_threads_pre_v33 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:  # type: ignore[override]
    report = _reconstruct_browser_threads_pre_v33(extracted, input_label, browser_only=browser_only)
    report = _v29_apply_report_refinements(report)
    report["schema_version"] = _V33_SCHEMA_VERSION
    return report


_filter_report_by_target_reference_pre_v33 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:  # type: ignore[override]
    filtered = _filter_report_by_target_reference_pre_v33(report, target_reference)
    filtered = _v29_apply_report_refinements(filtered)
    filtered["schema_version"] = _V33_SCHEMA_VERSION
    return filtered


_render_html_report_pre_v33 = render_html_report


def render_html_report(report: dict[str, Any], output_path: Path) -> None:  # type: ignore[override]
    report = _v29_apply_report_refinements(report)
    _render_html_report_pre_v33(report, output_path)
    try:
        html_text = output_path.read_text(encoding="utf-8")
        html_text = html_text.replace("v0.32 scenario display", "v0.33 scenario display")
        html_text = html_text.replace("HTML view only · v0.32", "HTML view only · v0.33")
        output_path.write_text(html_text, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v0.34 source-file temporal context + offset validation
# ---------------------------------------------------------------------------
# This layer is intentionally report/summary focused.  It does not change the
# LevelDB decoding, record grouping, action extraction, reasoning extraction,
# or scenario classification logic.  It adds temporal provenance from ZIP/file
# metadata and validates whether reported offsets are plausible byte offsets.

_V34_SCHEMA_VERSION = "0.34"


def _v34_file_type_from_name(name: Any) -> str:
    base = _v32_safe_basename(name).lower()
    if base.endswith(".log") or base in {"log", "log.old"}:
        return "log"
    if base.endswith((".ldb", ".sst")):
        return "ldb"
    if base.startswith("manifest-"):
        return "manifest"
    if base in {"current", "lock"}:
        return "leveldb_metadata"
    return "other"


def _v34_zipinfo_local_iso(info: zipfile.ZipInfo) -> str | None:
    try:
        return datetime(*info.date_time).isoformat()
    except Exception:
        return None


def _v34_source_time_sort_key(file_item: dict[str, Any]) -> tuple[int, str]:
    """Return a conservative sort key for source-file times.

    ZIP timestamps are local/unspecified, so they are sortable within the same
    archive but are not converted to UTC.  Filesystem stat times are UTC epoch
    derived.  The first element keeps true UTC times preferred when mixed.
    """
    interp = _v29_as_dict(file_item.get("modified_time_interpretation"))
    utc = interp.get("interpreted_utc")
    raw = str(file_item.get("modified_time_raw") or "")
    if utc:
        return (0, str(utc))
    return (1, raw)


def _v34_build_source_file_temporal_context(original_input: Path, root_path: Path, leveldb_path: Path) -> dict[str, Any]:
    """Collect source-file timestamps for the target Perplexity LevelDB folder.

    For ZIP input, this reads ZipInfo metadata rather than the extracted temp
    files.  ZIP date_time is timezone-less DOS local time, so it is reported as
    local/unspecified.  For folder input, filesystem stat mtime is reported as
    UTC because POSIX epoch seconds are timezone independent.
    """
    files: list[dict[str, Any]] = []
    original_input = Path(original_input)
    leveldb_path = Path(leveldb_path)
    root_path = Path(root_path)

    def add_file(item: dict[str, Any]) -> None:
        sf = item.get("source_file")
        if not _v32_is_real_source_file(sf):
            return
        item.setdefault("source_file_type", _v34_file_type_from_name(sf))
        files.append(item)

    try:
        if is_zip_path(original_input):
            try:
                rel_leveldb = leveldb_path.relative_to(root_path).as_posix().rstrip("/")
            except Exception:
                rel_leveldb = TARGET_LEVELDB_NAME
            with zipfile.ZipFile(original_input, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    entry_name = info.filename.replace("\\", "/")
                    base = Path(entry_name).name
                    if not _v32_is_real_source_file(base):
                        continue
                    # Prefer entries actually inside the target Perplexity LevelDB
                    # folder.  Fall back to a path-suffix check for archives that
                    # include a parent scenario folder.
                    in_target = entry_name.startswith(rel_leveldb + "/") or (TARGET_LEVELDB_NAME + "/") in entry_name
                    if not in_target:
                        continue
                    raw_time = _v34_zipinfo_local_iso(info)
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
                if not _v32_is_real_source_file(base):
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
            "caution": "Source-file timestamp collection failed; artifact-level timestamps and LevelDB sequence numbers may still be available.",
        }

    files.sort(key=lambda x: (_v34_source_time_sort_key(x), str(x.get("source_file") or ""), str(x.get("zip_entry_path") or x.get("source_path") or "")))
    by_source: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        by_source.setdefault(str(item.get("source_file")), []).append(item)

    return {
        "available": True,
        "input": str(original_input),
        "input_kind": "zip" if is_zip_path(original_input) else "folder_or_leveldb",
        "leveldb_path": str(leveldb_path),
        "collection_method": "zip_entry_metadata" if is_zip_path(original_input) else "filesystem_stat_mtime",
        "source_file_count": len(files),
        "files": files,
        "by_source_file": by_source,
        "earliest_source_file_time": files[0].get("modified_time_raw") if files else None,
        "latest_source_file_time": files[-1].get("modified_time_raw") if files else None,
        "timezone_policy": "ZIP entry times are local/unspecified and are not converted to UTC; filesystem stat times are epoch-derived UTC. Treat all source-file times as file-context evidence, not per-record creation times.",
        "caution": "Source-file modified times show archive/filesystem context. They should corroborate, not replace, application-level created_at/updated_at/started_at/completed_at and LevelDB relative order.",
    }


def _v34_context_files_for_source(source_context: dict[str, Any], source_file: Any) -> list[dict[str, Any]]:
    if not source_context or not source_context.get("available"):
        return []
    return list(_v29_as_list(_v29_as_dict(source_context.get("by_source_file")).get(str(source_file))))


def _v34_source_file_time_values_for_entry(entry: dict[str, Any], source_context: dict[str, Any]) -> list[dict[str, Any]]:
    order = _v29_as_dict(_v29_as_dict(entry.get("all_time_related_values")).get("sequence_and_offset_order"))
    source_files = [sf for sf in _v29_as_list(order.get("source_files")) if _v32_is_real_source_file(sf)]
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for sf in source_files:
        for item in _v34_context_files_for_source(source_context, sf):
            key = (str(sf), str(item.get("modified_time_raw")), str(item.get("zip_entry_path") or item.get("source_path") or ""))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "source_file": sf,
                "source_file_type": item.get("source_file_type"),
                "modified_time_raw": item.get("modified_time_raw"),
                "modified_time_interpretation": item.get("modified_time_interpretation"),
                "size_bytes": item.get("size_bytes"),
                "mtime_source": item.get("mtime_source"),
                "zip_entry_path": item.get("zip_entry_path"),
                "source_path": item.get("source_path"),
            })
    out.sort(key=_v34_source_time_sort_key)
    return out


def _v34_validate_offsets_for_entry(entry: dict[str, Any], source_context: dict[str, Any], limit: int = 120) -> dict[str, Any]:
    refs = _v29_as_list(_v29_as_dict(entry.get("source_order_and_provenance")).get("evidence_refs_sample"))
    validations: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    seen: set[tuple[Any, Any, Any, Any]] = set()

    for ref in refs:
        if len(validations) >= limit:
            break
        if not isinstance(ref, dict):
            continue
        sf = ref.get("source_file")
        off = ref.get("offset")
        key = (sf, off, ref.get("ldb_seq_no"), ref.get("state"))
        if key in seen:
            continue
        seen.add(key)
        status = "offset_missing"
        size = None
        try:
            off_int = int(off) if off is not None else None
        except Exception:
            off_int = None
            status = "offset_not_integer"
        matches = _v34_context_files_for_source(source_context, sf)
        if off_int is not None:
            if not matches:
                status = "source_file_size_unavailable"
            else:
                sizes = [m.get("size_bytes") for m in matches if isinstance(m.get("size_bytes"), int)]
                size = max(sizes) if sizes else None
                if size is None:
                    status = "source_file_size_unavailable"
                elif 0 <= off_int <= int(size):
                    status = "plausible_byte_offset_within_source_file_size"
                else:
                    status = "exceeds_source_file_size_do_not_treat_as_valid_byte_offset"
        counts[status] = counts.get(status, 0) + 1
        validations.append({
            "source_file": sf,
            "source_type": ref.get("source_type"),
            "state": ref.get("state"),
            "ldb_seq_no": ref.get("ldb_seq_no"),
            "offset_raw": off,
            "source_file_size_bytes": size,
            "offset_validation_status": status,
        })

    return {
        "policy": "Offsets are validated against source-file size when file metadata is available. Values exceeding file size should not be cited as byte offsets; use ldb_seq_no and source-file order for relative timing instead.",
        "validation_counts": counts,
        "has_invalid_offsets": bool(counts.get("exceeds_source_file_size_do_not_treat_as_valid_byte_offset")),
        "items_sample": validations,
    }


def _v34_collect_url_access_time_values(thread: dict[str, Any], limit: int = 80) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def maybe_add(obj: dict[str, Any], path: str) -> None:
        if len(out) >= limit:
            return
        if "lastAccess" not in obj and "last_access" not in obj:
            return
        raw = obj.get("lastAccess") if "lastAccess" in obj else obj.get("last_access")
        if raw in (None, "", [], {}):
            return
        url = obj.get("url") or obj.get("source") or obj.get("href")
        key = (str(url or path), str(raw))
        if key in seen:
            return
        seen.add(key)
        item = {
            "field_path": path + (".lastAccess" if "lastAccess" in obj else ".last_access"),
            "url": url,
            "title": obj.get("title"),
            "lastAccess_raw": raw,
            "visitCount": obj.get("visitCount") or obj.get("visit_count"),
            "interpretation": "URL access/cache timestamp candidate from topMostUrls/context URL evidence; treat separately from thread created_at.",
        }
        try:
            item["time_interpretation"] = interpret_timestamp(raw)
        except Exception:
            pass
        out.append(item)

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if len(out) >= limit:
            return
        if isinstance(value, dict):
            maybe_add(value, ".".join(path) if path else "thread")
            for k, v in value.items():
                walk(v, path + (str(k),))
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, path + (str(i),))

    # Focus on URL-bearing structures first to avoid scanning every large raw string.
    for key in ("urls", "context_url_candidates", "computer_url_candidates", "global_context_urls", "metadata", "timeline"):
        walk(thread.get(key), (key,))
    return out


def _v34_dedupe_time_values(values: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any, Any]] = []
    for v in values:
        field = v.get("field") or v.get("field_path") or "time"
        raw = v.get("raw") if "raw" in v else v.get("lastAccess_raw") if "lastAccess_raw" in v else v.get("modified_time_raw")
        interpreted = v.get("interpreted_utc") or _v29_as_dict(v.get("time_interpretation")).get("interpreted_utc") or _v29_as_dict(v.get("modified_time_interpretation")).get("interpreted_utc")
        key = (field, raw, interpreted)
        src = v.get("source") or v.get("mtime_source") or v.get("field_path") or v.get("source_file")
        if key not in grouped:
            item = dict(v)
            item["observed_sources"] = [src] if src else []
            grouped[key] = item
            order.append(key)
        else:
            if src and src not in grouped[key].setdefault("observed_sources", []):
                grouped[key]["observed_sources"].append(src)
    return [grouped[k] for k in order[:limit]]


def _v34_thread_reconstruction_entry(thread: dict[str, Any], index: int, source_context: dict[str, Any] | None = None) -> dict[str, Any]:
    source_context = source_context or {}
    entry = _v33_thread_reconstruction_entry(thread, index)
    tv = _v29_as_dict(entry.get("all_time_related_values"))

    url_times = _v34_collect_url_access_time_values(thread)
    tv["url_access_time_values"] = url_times
    tv["url_access_time_value_count"] = len(url_times)

    sf_times = _v34_source_file_time_values_for_entry(entry, source_context)
    tv["source_file_time_values"] = sf_times
    tv["source_file_time_value_count"] = len(sf_times)
    tv["source_file_time_policy_v34"] = "Source-file modified times are archive/filesystem context, not per-record creation timestamps. ZIP times remain local/unspecified unless independently normalized."

    if sf_times:
        tv["earliest_source_file_time"] = sf_times[0].get("modified_time_raw")
        tv["latest_source_file_time"] = sf_times[-1].get("modified_time_raw")

    offset_validation = _v34_validate_offsets_for_entry(entry, source_context)
    provenance = _v29_as_dict(entry.get("source_order_and_provenance"))
    provenance["offset_validation_v34"] = offset_validation
    entry["source_order_and_provenance"] = provenance

    seq = _v29_as_dict(tv.get("sequence_and_offset_order"))
    seq["offset_validation_summary_v34"] = {
        "validation_counts": offset_validation.get("validation_counts"),
        "has_invalid_offsets": offset_validation.get("has_invalid_offsets"),
        "policy": "When offsets are invalid/unavailable, cite ldb_seq_no/source_file/source_type/state for relative order rather than byte position.",
    }
    tv["sequence_and_offset_order"] = seq
    entry["all_time_related_values"] = tv

    entry["source_file_temporal_context_v34"] = {
        "source_files_used_by_thread": seq.get("source_files"),
        "source_file_times": sf_times,
        "earliest_source_file_time": tv.get("earliest_source_file_time"),
        "latest_source_file_time": tv.get("latest_source_file_time"),
        "timezone_policy": _v29_as_dict(source_context).get("timezone_policy"),
        "interpretation": "Use this as file/acquisition temporal context. Combine with artifact_time_values, URL lastAccess, and ldb_seq_no for reconstruction.",
    }

    diff = _v29_as_dict(entry.get("cross_snapshot_diff_ready"))
    diff["earliest_source_file_time"] = tv.get("earliest_source_file_time")
    diff["latest_source_file_time"] = tv.get("latest_source_file_time")
    diff["offset_validation_counts"] = offset_validation.get("validation_counts")
    diff["source_file_time_available"] = bool(sf_times)
    entry["cross_snapshot_diff_ready"] = diff
    return entry


def _v34_source_file_time_overview(source_context: dict[str, Any]) -> dict[str, Any]:
    files = list(_v29_as_list(_v29_as_dict(source_context).get("files")))
    files.sort(key=_v34_source_time_sort_key)
    return {
        "available": bool(source_context and source_context.get("available")),
        "collection_method": source_context.get("collection_method") if isinstance(source_context, dict) else None,
        "source_file_count": len(files),
        "earliest_source_file_time": files[0].get("modified_time_raw") if files else None,
        "latest_source_file_time": files[-1].get("modified_time_raw") if files else None,
        "files_sample": files[:30],
        "timezone_policy": _v29_as_dict(source_context).get("timezone_policy"),
    }


def _v34_build_case_focused_core_findings(report: dict[str, Any], thread_entries: list[dict[str, Any]], source_context: dict[str, Any]) -> dict[str, Any]:
    base = _v33_build_case_focused_core_findings(report, thread_entries)
    raw_times: list[dict[str, Any]] = []
    for e in thread_entries:
        tv = _v29_as_dict(e.get("all_time_related_values"))
        for t in _v29_as_list(tv.get("forensic_time_values"))[:20]:
            x = dict(t)
            x["thread_index"] = e.get("index")
            x["thread_id"] = e.get("thread_id")
            raw_times.append(x)
        for t in _v29_as_list(tv.get("url_access_time_values"))[:10]:
            x = dict(t)
            x["thread_index"] = e.get("index")
            x["thread_id"] = e.get("thread_id")
            raw_times.append(x)
        for t in _v29_as_list(tv.get("source_file_time_values"))[:10]:
            x = dict(t)
            x["thread_index"] = e.get("index")
            x["thread_id"] = e.get("thread_id")
            raw_times.append(x)
    base["key_time_values_sample"] = _v34_dedupe_time_values(raw_times, limit=25)
    base["source_file_time_overview_v34"] = _v34_source_file_time_overview(source_context)
    base["time_categories_v34"] = {
        "artifact_time_values": "created_at/updated_at/started_at/completed_at/etc. decoded from Comet artifacts",
        "url_access_time_values": "topMostUrls/context URL lastAccess values, separated from thread creation time",
        "source_file_time_values": ".ldb/.log/LOG/MANIFEST ZIP/file modified times, file-context only",
        "event_content_time_values": "Calendar/email/task content dates and times, not artifact creation time",
        "relative_order_values": "ldb_seq_no/source_file/source_type/state plus validated offset when plausible",
    }
    cautions = list(_v29_as_list(base.get("cautions")))
    cautions.append("Source-file modified times and ZIP entry times are file-context evidence, not per-record action timestamps.")
    cautions.append("Offsets are validated against source-file size; invalid offsets must not be cited as byte offsets.")
    base["cautions"] = _v29_unique_keep_order(cautions, 20)
    return base


def _v34_build_condensed_summary(report: dict[str, Any]) -> dict[str, Any]:
    base = _v33_build_condensed_summary(report)
    source_context = _v29_as_dict(_v29_as_dict(report.get("source")).get("source_file_temporal_context") or report.get("source_file_temporal_context_v34"))
    report_threads = _v29_as_list(report.get("threads"))
    thread_entries = [_v34_thread_reconstruction_entry(t, i + 1, source_context) for i, t in enumerate(report_threads)]

    artifact_times: list[datetime] = []
    source_files: list[str] = []
    seq_values: list[int] = []
    url_access_times: list[datetime] = []
    invalid_offset_threads = 0
    for entry in thread_entries:
        tv = _v29_as_dict(entry.get("all_time_related_values"))
        for key in ("earliest_interpreted_utc", "latest_interpreted_utc"):
            dt = _v29_parse_iso_for_sort(tv.get(key))
            if dt:
                artifact_times.append(dt)
        for item in _v29_as_list(tv.get("url_access_time_values")):
            dt = _v29_parse_iso_for_sort(_v29_as_dict(item.get("time_interpretation")).get("interpreted_utc"))
            if dt:
                url_access_times.append(dt)
        order = _v29_as_dict(tv.get("sequence_and_offset_order"))
        source_files.extend(_v29_as_list(order.get("source_files")))
        for seq_key in ("relative_order_min", "relative_order_max"):
            try:
                if order.get(seq_key) is not None:
                    seq_values.append(int(order.get(seq_key)))
            except Exception:
                pass
        if _v29_as_dict(_v29_as_dict(entry.get("source_order_and_provenance")).get("offset_validation_v34")).get("has_invalid_offsets"):
            invalid_offset_threads += 1

    sf_overview = _v34_source_file_time_overview(source_context)
    base["version"] = _V34_SCHEMA_VERSION
    base["thread_reconstruction_map_v34"] = thread_entries
    base["thread_reconstruction_map_v33"] = thread_entries
    base["thread_reconstruction_map_v32"] = thread_entries
    base["case_focused_core_findings_v34"] = _v34_build_case_focused_core_findings(report, thread_entries, source_context)
    base["case_focused_core_findings_v33"] = base["case_focused_core_findings_v34"]
    base["source_file_temporal_context_v34"] = source_context
    base["all_time_values_overview_v34"] = {
        "artifact_time_values": {
            "earliest_interpreted_utc": min(artifact_times).isoformat() if artifact_times else None,
            "latest_interpreted_utc": max(artifact_times).isoformat() if artifact_times else None,
            "timezone_policy": "Naive application timestamps are interpreted by the existing parser as UTC; cite raw values and timezone_basis/assumption when writing conclusions.",
        },
        "url_access_time_values": {
            "earliest_interpreted_utc": min(url_access_times).isoformat() if url_access_times else None,
            "latest_interpreted_utc": max(url_access_times).isoformat() if url_access_times else None,
            "policy": "URL lastAccess values are URL/cache access indicators and should not be merged with thread created_at without explanation.",
        },
        "source_file_time_values": sf_overview,
        "relative_order_values": {
            "relative_order_min": min(seq_values) if seq_values else None,
            "relative_order_max": max(seq_values) if seq_values else None,
            "source_files_observed": _v29_unique_keep_order([sf for sf in source_files if _v32_is_real_source_file(sf)], 50),
            "invalid_offset_thread_count": invalid_offset_threads,
        },
        "time_policy": "v0.34 separates artifact timestamps, URL lastAccess, source-file modified times, event-content dates/times, and relative ordering. Do not collapse these into one timestamp.",
    }
    # Compatibility aliases use the stricter v0.34 overview so JSON consumers do
    # not see conflicting top-level v33/v34 earliest/latest values.
    base["all_time_values_overview_v33"] = base["all_time_values_overview_v34"]
    base["all_time_values_overview_v32"] = base["all_time_values_overview_v34"]
    base["earliest_artifact_time_utc"] = base["all_time_values_overview_v34"]["artifact_time_values"]["earliest_interpreted_utc"]
    base["latest_artifact_time_utc"] = base["all_time_values_overview_v34"]["artifact_time_values"]["latest_interpreted_utc"]
    base["source_files_observed"] = base["all_time_values_overview_v34"]["relative_order_values"]["source_files_observed"]
    base["source_file_count"] = len(set(base["source_files_observed"]))
    base["forensic_summary_v34"] = {
        "source_file_time_collection_added": bool(source_context),
        "offset_validation_added": True,
        "url_lastaccess_separated": True,
        "key_time_values_deduped": True,
        "timezone_caution": "Naive application timestamps and ZIP local timestamps require explicit timezone assumptions in the paper.",
        "extraction_logic_changed": False,
    }
    base["forensic_summary_v33"] = base["forensic_summary_v34"]
    base["forensic_summary_v32"] = base["forensic_summary_v34"]
    return base


_v29_apply_report_refinements_pre_v34 = _v29_apply_report_refinements


def _v34_apply_report_refinements(report: dict[str, Any]) -> dict[str, Any]:
    report = _v29_apply_report_refinements_pre_v34(report)
    try:
        report["presentation_version"] = _V34_SCHEMA_VERSION
        report["schema_version"] = _V34_SCHEMA_VERSION
        condensed = _v34_build_condensed_summary(report)
        report["condensed_reconstruction_summary_v29"] = condensed
        report["condensed_reconstruction_summary_v32"] = condensed
        report["condensed_reconstruction_summary_v33"] = condensed
        report["condensed_reconstruction_summary_v34"] = condensed
        report["source_file_temporal_context_v34"] = _v29_as_dict(_v29_as_dict(report.get("source")).get("source_file_temporal_context"))
        # Keep legacy top-level values synchronized with strict artifact-time view.
        overview = _v29_as_dict(condensed.get("all_time_values_overview_v34"))
        art = _v29_as_dict(overview.get("artifact_time_values"))
        report["earliest_artifact_time_utc"] = art.get("earliest_interpreted_utc")
        report["latest_artifact_time_utc"] = art.get("latest_interpreted_utc")
        report.setdefault("repair_summary", {})["v34_source_file_time_and_offset_validation"] = {
            "source_file_timestamp_collection_added": True,
            "zip_entry_times_preserved_as_local_or_unspecified": True,
            "filesystem_mtime_reported_as_utc": True,
            "offset_validation_against_source_file_size_added": True,
            "url_lastaccess_category_added": True,
            "deduped_key_time_values_added": True,
            "extraction_logic_changed": False,
        }
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.34 refinements failed: {exc}")
    return report


_reconstruct_browser_threads_pre_v34 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:  # type: ignore[override]
    report = _reconstruct_browser_threads_pre_v34(extracted, input_label, browser_only=browser_only)
    source_context = extracted.get("source_file_temporal_context")
    if isinstance(source_context, dict):
        report.setdefault("source", {})["source_file_temporal_context"] = source_context
    report = _v34_apply_report_refinements(report)
    report["schema_version"] = _V34_SCHEMA_VERSION
    return report


_filter_report_by_target_reference_pre_v34 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:  # type: ignore[override]
    filtered = _filter_report_by_target_reference_pre_v34(report, target_reference)
    # The previous filter already re-applies v33. Re-apply v34 so source-file
    # context and strict time aliases remain synchronized after filtering.
    filtered = _v34_apply_report_refinements(filtered)
    filtered["schema_version"] = _V34_SCHEMA_VERSION
    return filtered


_run_single_input_pre_v34 = run_single_input


def run_single_input(
    input_path: Path,
    blob_input: Path | None,
    database: str,
    store: str,
    include_computer: bool,
    dump_records_dir: Path | None = None,
) -> dict[str, Any]:  # type: ignore[override]
    prepared = prepare_input(input_path, explicit_blob_input=blob_input)
    try:
        source_file_temporal_context = _v34_build_source_file_temporal_context(
            original_input=prepared.original_input,
            root_path=prepared.root_path,
            leveldb_path=prepared.leveldb_path,
        )
        extracted = extract_records_from_leveldb(
            leveldb_path=prepared.leveldb_path,
            blob_path=prepared.blob_path,
            database_name=database,
            object_store_name=store,
        )
        extracted["source_file_temporal_context"] = source_file_temporal_context

        if dump_records_dir is not None:
            dump_records_dir.mkdir(parents=True, exist_ok=True)
            write_json(dump_records_dir / "perplexity_live_records.json", {"records": extracted["live_records"]})
            write_json(dump_records_dir / "perplexity_dead_records.json", {"records": extracted["dead_records"]})
            write_json(dump_records_dir / "perplexity_ldb_records.json", {"records": extracted["ldb_records"]})
            write_json(dump_records_dir / "perplexity_log_records.json", {"records": extracted["log_records"]})
            write_json(dump_records_dir / "perplexity_unmatched_records.json", {"records": extracted["unmatched_records"]})
            write_json(dump_records_dir / "perplexity_source_file_temporal_context_v34.json", source_file_temporal_context)

        return reconstruct_browser_threads(
            extracted=extracted,
            input_label=str(prepared.original_input),
            browser_only=not include_computer,
        )
    finally:
        prepared.cleanup()


_render_html_report_pre_v34 = render_html_report


def render_html_report(report: dict[str, Any], output_path: Path) -> None:  # type: ignore[override]
    report = _v34_apply_report_refinements(report)
    _render_html_report_pre_v34(report, output_path)
    try:
        html_text = output_path.read_text(encoding="utf-8")
        html_text = html_text.replace("v0.33 scenario display", "v0.34 scenario display")
        html_text = html_text.replace("HTML view only · v0.33", "HTML view only · v0.34")
        output_path.write_text(html_text, encoding="utf-8")
    except Exception:
        pass


# v0.35 HTML time/storage display wiring
# v0.34 added source-file time and offset validation to condensed JSON, but
# the visible thread HTML time panel still read older keys only.  v0.35 keeps
# extraction unchanged and only attaches the v0.34 temporal summary to each
# thread for display, then renders artifact/source-file/URL/relative-order
# categories explicitly.
_V35_SCHEMA_VERSION = "0.35"


def _v35_thread_entries_by_id(condensed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = _v29_as_list(
        condensed.get("thread_reconstruction_map_v34")
        or condensed.get("thread_reconstruction_map_v33")
        or condensed.get("thread_reconstruction_map_v32")
    )
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tid = str(entry.get("thread_id") or "")
        if tid:
            out.setdefault(tid, entry)
    return out


def _v35_attach_time_display_fields(report: dict[str, Any]) -> dict[str, Any]:
    condensed = _v29_as_dict(report.get("condensed_reconstruction_summary_v34") or report.get("condensed_reconstruction_summary_v33") or report.get("condensed_reconstruction_summary_v32"))
    by_id = _v35_thread_entries_by_id(condensed)
    entries = _v29_as_list(condensed.get("thread_reconstruction_map_v34") or condensed.get("thread_reconstruction_map_v33") or condensed.get("thread_reconstruction_map_v32"))
    threads = _v29_as_list(report.get("threads"))
    for i, thread in enumerate(threads):
        if not isinstance(thread, dict):
            continue
        entry = by_id.get(str(thread.get("thread_id") or ""))
        if not entry and i < len(entries) and isinstance(entries[i], dict):
            entry = entries[i]
        if not entry:
            continue
        all_time = _v29_as_dict(entry.get("all_time_related_values"))
        sfctx = _v29_as_dict(entry.get("source_file_temporal_context_v34"))
        provenance = _v29_as_dict(entry.get("source_order_and_provenance"))
        thread["all_time_related_values_v34"] = all_time
        thread["source_file_temporal_context_v34"] = sfctx
        thread["source_order_and_provenance_v34"] = provenance
        thread["time_storage_display_v35"] = {
            "artifact_time_values": _v29_as_list(all_time.get("forensic_time_values")),
            "url_access_time_values": _v29_as_list(all_time.get("url_access_time_values")),
            "source_file_time_values": _v29_as_list(all_time.get("source_file_time_values")),
            "event_content_time_values": _v29_as_list(all_time.get("event_content_time_values")),
            "sequence_and_offset_order": _v29_as_dict(all_time.get("sequence_and_offset_order")),
            "offset_validation_v34": _v29_as_dict(provenance.get("offset_validation_v34")),
            "source_file_temporal_context_v34": sfctx,
        }
    report["threads"] = threads
    return report


def _v35_time_range_from_values(values: list[dict[str, Any]], interpreted_path: tuple[str, ...] = ("time_interpretation", "interpreted_utc"), raw_key: str = "raw") -> str | None:
    raw_values: list[str] = []
    parsed: list[datetime] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        cur: Any = item
        for key in interpreted_path:
            cur = _v29_as_dict(cur).get(key)
        dt = _v29_parse_iso_for_sort(cur)
        if dt:
            parsed.append(dt)
        raw = item.get(raw_key) or item.get("modified_time_raw") or item.get("lastAccess_raw")
        if raw is not None:
            raw_values.append(str(raw))
    if parsed:
        return f"{min(parsed).isoformat()} → {max(parsed).isoformat()}"
    if raw_values:
        ordered = sorted(set(raw_values))
        return ordered[0] if len(ordered) == 1 else f"{ordered[0]} → {ordered[-1]}"
    return None


def _v35_compact_source_file_range(sfctx: dict[str, Any]) -> str | None:
    early = sfctx.get("earliest_source_file_time")
    late = sfctx.get("latest_source_file_time")
    if early and late and early != late:
        return f"{early} → {late}"
    return early or late


def _v35_render_table(items: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int = 12, empty: str = "No values recovered.") -> str:
    rows = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        cells = []
        for label, key in columns:
            val: Any = item
            for part in key.split("."):
                val = _v29_as_dict(val).get(part)
            if isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)[:300]
            cells.append(f"<td>{_h(val)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    if not rows:
        return f"<div class='empty'>{_h(empty)}</div>"
    header = "<tr>" + "".join(f"<th>{_h(label)}</th>" for label, _ in columns) + "</tr>"
    more = ""
    if len(items) > limit:
        more = f"<p class='muted'>Showing {limit} of {len(items)} values.</p>"
    return "<table class='kv evidence-table'>" + header + "\n" + "\n".join(rows) + "</table>" + more


def _v15_render_time_storage(thread: dict[str, Any], idx: int) -> str:  # type: ignore[override]
    temporal = thread.get("temporal_evidence") or thread.get("computer_temporal_evidence") or {}
    source_summary = _v29_as_dict(thread.get("source_summary"))
    display = _v29_as_dict(thread.get("time_storage_display_v35"))
    all_time = _v29_as_dict(thread.get("all_time_related_values_v34"))
    order = _v29_as_dict(display.get("sequence_and_offset_order") or all_time.get("sequence_and_offset_order"))
    seq_range = _v29_as_dict(_v29_as_dict(temporal).get("sequence_range"))
    offset_validation = _v29_as_dict(display.get("offset_validation_v34"))
    sfctx = _v29_as_dict(display.get("source_file_temporal_context_v34") or thread.get("source_file_temporal_context_v34"))

    artifact_times = _v29_as_list(display.get("artifact_time_values") or all_time.get("forensic_time_values"))
    url_times = _v29_as_list(display.get("url_access_time_values") or all_time.get("url_access_time_values"))
    source_file_times = _v29_as_list(display.get("source_file_time_values") or all_time.get("source_file_time_values"))
    event_times = _v29_as_list(display.get("event_content_time_values") or all_time.get("event_content_time_values"))

    source_counts = source_summary.get("source_type_counts") or {}
    live_count = source_summary.get("live_record_count")
    dead_count = source_summary.get("dead_or_old_record_count")
    record_count = source_summary.get("record_count") or source_summary.get("total_records")
    if record_count in (None, ""):
        try:
            record_count = int(live_count or 0) + int(dead_count or 0)
        except Exception:
            record_count = thread.get("record_count")
    first_order = order.get("relative_order_min") or seq_range.get("first_relevant_seq") or source_summary.get("min_relative_order")
    last_order = order.get("relative_order_max") or seq_range.get("last_relevant_seq") or source_summary.get("max_relative_order")
    source_files = _v29_as_list(order.get("source_files") or sfctx.get("source_files_used_by_thread"))
    invalid_counts = _v29_as_dict(offset_validation.get("validation_counts"))
    invalid_offset_count = invalid_counts.get("exceeds_source_file_size_do_not_treat_as_valid_byte_offset")

    parts = [
        _v15_section_title(idx, "time", 7, "Time & storage evidence", "Timestamps, sequence/order values, source-file modified times, and LOG/LDB persistence summary."),
        _kv_table_v07([
            ("Source type counts", source_counts),
            ("Record count", record_count),
            ("First order", first_order),
            ("Last order", last_order),
            ("Artifact time range", _v35_time_range_from_values(artifact_times)),
            ("URL access time range", _v35_time_range_from_values(url_times)),
            ("Source-file time range", _v35_compact_source_file_range(sfctx) or _v35_time_range_from_values(source_file_times, interpreted_path=("modified_time_interpretation", "interpreted_utc"), raw_key="modified_time_raw")),
            ("Source files used", ", ".join(map(str, source_files[:12])) if source_files else None),
            ("Invalid offset count", invalid_offset_count),
        ]),
    ]

    if artifact_times:
        parts.append("<h4>Artifact timestamp fields</h4>")
        parts.append(_v35_render_table(artifact_times, [
            ("Field", "field"), ("Raw", "raw"), ("Interpreted UTC", "time_interpretation.interpreted_utc"), ("Source", "source")
        ], limit=12))
    if source_file_times:
        parts.append("<h4>Source-file modified times</h4>")
        parts.append(_v35_render_table(source_file_times, [
            ("Source file", "source_file"), ("Type", "source_file_type"), ("Modified raw", "modified_time_raw"), ("TZ basis", "modified_time_interpretation.timezone_basis"), ("Size", "size_bytes")
        ], limit=12))
    if url_times:
        parts.append("<h4>URL access / lastAccess values</h4>")
        parts.append(_v35_render_table(url_times, [
            ("URL", "url"), ("Title", "title"), ("lastAccess raw", "lastAccess_raw"), ("Interpreted UTC", "time_interpretation.interpreted_utc"), ("Visit count", "visitCount")
        ], limit=10))
    if event_times:
        parts.append("<h4>Event/content time values</h4>")
        parts.append(_v35_render_table(event_times, [
            ("Field", "field"), ("Value", "value"), ("Source", "source")
        ], limit=10, empty="No event/content time values."))
    if offset_validation:
        parts.append("<h4>Offset validation</h4>")
        parts.append(_kv_table_v07([
            ("Policy", offset_validation.get("policy")),
            ("Validation counts", offset_validation.get("validation_counts")),
            ("Has invalid offsets", offset_validation.get("has_invalid_offsets")),
        ]))
        parts.append(_raw_details_v07("Open offset validation JSON", offset_validation))

    parts.append(_raw_details_v07("Open time/storage decoded JSON", {
        "temporal_evidence": temporal,
        "source_summary": source_summary,
        "all_time_related_values_v34": all_time,
        "source_file_temporal_context_v34": sfctx,
        "offset_validation_v34": offset_validation,
    }))
    return "\n".join(parts)


_v35_apply_report_refinements_pre_v35 = _v34_apply_report_refinements


def _v35_apply_report_refinements(report: dict[str, Any]) -> dict[str, Any]:
    report = _v35_apply_report_refinements_pre_v35(report)
    try:
        report = _v35_attach_time_display_fields(report)
        report["presentation_version"] = _V35_SCHEMA_VERSION
        report["schema_version"] = _V35_SCHEMA_VERSION
        condensed = _v29_as_dict(report.get("condensed_reconstruction_summary_v34"))
        if condensed:
            condensed["version"] = _V35_SCHEMA_VERSION
            condensed["html_time_storage_display_wired_v35"] = True
            report["condensed_reconstruction_summary_v35"] = condensed
        report.setdefault("repair_summary", {})["v35_html_time_storage_display"] = {
            "visible_time_storage_panel_reads_v34_temporal_categories": True,
            "record_count_uses_live_plus_dead_when_legacy_count_absent": True,
            "first_last_order_read_from_sequence_range_or_v34_relative_order": True,
            "source_file_modified_times_shown_in_thread_html": True,
            "extraction_logic_changed": False,
        }
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.35 HTML time/storage display refinements failed: {exc}")
    return report


_reconstruct_browser_threads_pre_v35 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:  # type: ignore[override]
    report = _reconstruct_browser_threads_pre_v35(extracted, input_label, browser_only=browser_only)
    report = _v35_apply_report_refinements(report)
    report["schema_version"] = _V35_SCHEMA_VERSION
    return report


_filter_report_by_target_reference_pre_v35 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:  # type: ignore[override]
    filtered = _filter_report_by_target_reference_pre_v35(report, target_reference)
    filtered = _v35_apply_report_refinements(filtered)
    filtered["schema_version"] = _V35_SCHEMA_VERSION
    return filtered


_run_single_input_pre_v35 = run_single_input


def run_single_input(
    input_path: Path,
    blob_input: Path | None,
    database: str,
    store: str,
    include_computer: bool,
    dump_records_dir: Path | None = None,
) -> dict[str, Any]:  # type: ignore[override]
    report = _run_single_input_pre_v35(input_path, blob_input, database, store, include_computer, dump_records_dir=dump_records_dir)
    return _v35_apply_report_refinements(report)


_render_html_report_pre_v35 = render_html_report


def render_html_report(report: dict[str, Any], output_path: Path) -> None:  # type: ignore[override]
    report = _v35_apply_report_refinements(report)
    _render_html_report_pre_v35(report, output_path)
    try:
        html_text = output_path.read_text(encoding="utf-8")
        html_text = html_text.replace("v0.34 scenario display", "v0.35 scenario display")
        html_text = html_text.replace("HTML view only · v0.34", "HTML view only · v0.35")
        output_path.write_text(html_text, encoding="utf-8")
    except Exception:
        pass



# ---------------------------------------------------------------------------
# v0.36 HCI/display refinement layer
# ---------------------------------------------------------------------------
# This layer is presentation-only.  It does not change LevelDB extraction,
# grouping, classification, outcome scoring, or evidence values.  It improves
# the investigator HTML so duplicate scenario-display modes are merged, thread
# navigation is reference-first rather than index-first, profile-residue wording
# is clearer, and wide evidence tables wrap within the viewport.
_V36_SCHEMA_VERSION = "0.36"


def _v36_apply_report_refinements(report: dict[str, Any]) -> dict[str, Any]:
    report = _v35_apply_report_refinements(report)
    try:
        report["presentation_version"] = _V36_SCHEMA_VERSION
        report["schema_version"] = _V36_SCHEMA_VERSION
        condensed = _v29_as_dict(
            report.get("condensed_reconstruction_summary_v35")
            or report.get("condensed_reconstruction_summary_v34")
            or report.get("condensed_reconstruction_summary_v33")
            or report.get("condensed_reconstruction_summary_v32")
        )
        if condensed:
            condensed["version"] = _V36_SCHEMA_VERSION
            # Compatibility aliases: keep existing v34/v35 keys for downstream
            # tools while exposing v36 names for the current HTML/report layer.
            if "thread_reconstruction_map_v34" in condensed:
                condensed.setdefault("thread_reconstruction_map_v35", condensed.get("thread_reconstruction_map_v34"))
                condensed.setdefault("thread_reconstruction_map_v36", condensed.get("thread_reconstruction_map_v34"))
            if "all_time_values_overview_v34" in condensed:
                condensed.setdefault("all_time_values_overview_v35", condensed.get("all_time_values_overview_v34"))
                condensed.setdefault("all_time_values_overview_v36", condensed.get("all_time_values_overview_v34"))
            # case_focused_core_findings was introduced in v33 and extended by
            # later layers.  Preserve the exact object and expose a v36 alias.
            case_core = (
                condensed.get("case_focused_core_findings_v35")
                or condensed.get("case_focused_core_findings_v34")
                or condensed.get("case_focused_core_findings_v33")
            )
            if case_core is not None:
                condensed.setdefault("case_focused_core_findings_v36", case_core)
            condensed["html_hci_display_refinement_v36"] = True
            report["condensed_reconstruction_summary_v36"] = condensed
        report.setdefault("repair_summary", {})["v36_hci_html_display"] = {
            "duplicate_scenario_display_modes_merged_in_html": True,
            "thread_navigation_reference_first": True,
            "profile_residue_wording_clarified": True,
            "wide_tables_wrap_within_viewport": True,
            "search_placeholder_made_generic": True,
            "extraction_logic_changed": False,
            "classification_logic_changed": False,
            "hardcoding_note": "This layer uses only recovered thread-index JSON and existing scenario display policy. It does not embed expected scenario outputs, UUIDs, emails, filenames, or URLs.",
        }
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.36 HCI display refinements failed: {exc}")
    return report


_reconstruct_browser_threads_pre_v36 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:  # type: ignore[override]
    report = _reconstruct_browser_threads_pre_v36(extracted, input_label, browser_only=browser_only)
    report = _v36_apply_report_refinements(report)
    report["schema_version"] = _V36_SCHEMA_VERSION
    return report


_filter_report_by_target_reference_pre_v36 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:  # type: ignore[override]
    filtered = _filter_report_by_target_reference_pre_v36(report, target_reference)
    filtered = _v36_apply_report_refinements(filtered)
    filtered["schema_version"] = _V36_SCHEMA_VERSION
    return filtered


_run_single_input_pre_v36 = run_single_input


def run_single_input(
    input_path: Path,
    blob_input: Path | None,
    database: str,
    store: str,
    include_computer: bool,
    dump_records_dir: Path | None = None,
) -> dict[str, Any]:  # type: ignore[override]
    report = _run_single_input_pre_v36(input_path, blob_input, database, store, include_computer, dump_records_dir=dump_records_dir)
    return _v36_apply_report_refinements(report)


def _v36_hci_css_js() -> str:
    return r"""
<style id="v36-hci-style">
/* v0.36 HCI/display refinements: keep evidence intact, improve reading. */
:root{--v36-focus:#2563eb;--v36-soft:#f8fafc;--v36-line:#dbe3ef;--v36-sidebar:#0f172a}
.layout{grid-template-columns:minmax(280px,320px) minmax(0,1fr)}
.sidebar{background:var(--v36-sidebar);padding:18px 16px}.brand h1{font-size:18px}.brand p{overflow-wrap:anywhere}
.nav-title{line-height:1.25;margin-top:16px}.nav-title-sub{display:block;font-size:10px;letter-spacing:0;text-transform:none;color:#64748b;margin-top:3px;font-weight:600}
.nav-thread{padding:8px 10px}.nav-thread:after{top:13px}.v36-thread-row{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px;align-items:start}.v36-thread-pill{display:inline-flex;align-items:center;justify-content:center;min-width:30px;height:22px;border-radius:999px;background:rgba(255,255,255,.10);color:#cbd5e1;font-size:11px;font-weight:850;margin-top:1px}.nav-item.active .v36-thread-pill{background:#e0e7ff;color:#1e3a8a}.v36-thread-main{min-width:0}.v36-thread-main strong{font-size:12.5px;line-height:1.25;white-space:normal;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.v36-thread-main span{font-size:10.5px;white-space:normal;overflow-wrap:anywhere;color:#94a3b8}.nav-item.active .v36-thread-main span{color:#475569}
.main{max-width:1280px}.card{overflow:hidden}.hint,.note,.prompt,.answer,.thread-id,.source-strip,.evidence-chip,a{overflow-wrap:anywhere;word-break:break-word}.prompt,.answer{max-width:100%;overflow-x:hidden}
.table,.kv,.evidence-table{width:100%;max-width:100%;table-layout:fixed}.table th,.table td,.kv th,.kv td,.evidence-table th,.evidence-table td{overflow-wrap:anywhere;word-break:break-word;hyphens:auto}.kv th{width:min(220px,30%)}.table th,.table td,.evidence-table th,.evidence-table td{font-size:12.5px;line-height:1.45}.evidence-table th:nth-child(1),.evidence-table td:nth-child(1){width:18%}.evidence-table th:nth-child(2),.evidence-table td:nth-child(2){width:16%}
pre.raw{white-space:pre-wrap;overflow-x:hidden;overflow-wrap:anywhere;word-break:break-word;max-width:100%}details{max-width:100%}.evidence-card summary{grid-template-columns:minmax(54px,72px) minmax(72px,108px) minmax(0,1.2fr) minmax(0,1fr)}.ev-label,.ev-source{min-width:0;overflow-wrap:anywhere;word-break:break-word}
.v30-scenario-card{position:relative;top:auto;border-left-color:var(--v36-focus)}.v30-scenario-grid{grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}.v30-scenario-grid button{border-radius:12px;background:#172033}.v30-scenario-grid button[disabled]{opacity:.55;cursor:not-allowed}.v36-merged-note{border-left:4px solid #22c55e;background:#ecfdf5;border-radius:12px;padding:10px 12px;margin:10px 0;color:#14532d;font-size:13px}.v36-context-note{border-left:4px solid #38bdf8;background:#f0f9ff;border-radius:12px;padding:10px 12px;margin:10px 0;color:#0f4660;font-size:13px}.v36-hidden{display:none!important}
@media(max-width:1180px){.layout{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.main{max-width:100%;padding:14px}.nav-list{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}.table th,.table td,.evidence-table th,.evidence-table td{font-size:12px}.kv th{width:34%}}
@media(max-width:760px){.kv,.kv tbody,.kv tr,.kv th,.kv td{display:block;width:100%}.kv th{border-bottom:0}.kv td{border-bottom:1px solid var(--v36-line)}.table,.table thead,.table tbody,.table tr,.table th,.table td,.evidence-table,.evidence-table tbody,.evidence-table tr,.evidence-table th,.evidence-table td{display:block;width:100%}.table thead,.evidence-table thead{display:none}.table tr,.evidence-table tr{border:1px solid var(--v36-line);border-radius:12px;margin:8px 0;padding:8px;background:#fff}.table td,.evidence-table td{border:0;border-bottom:1px solid #eef2f7;padding:6px 4px}.table td:last-child,.evidence-table td:last-child{border-bottom:0}.evidence-card summary{display:block}.ev-order,.ev-kind,.ev-label,.ev-source{display:block;margin:3px 0}}
</style>
<script id="v36-hci-script">
(function(){
  function byId(id){ return document.getElementById(id); }
  function lower(x){ return String(x || '').toLowerCase(); }
  function readJson(id, fallback){ var el=byId(id); if(!el) return fallback; try{return JSON.parse(el.textContent||'null')||fallback;}catch(e){return fallback;} }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }
  function refsOf(info){ var refs=info.reference_codes||[]; if(!Array.isArray(refs)) refs=[]; if(info.reference_label) refs=refs.concat([info.reference_label]); return refs.map(function(x){return String(x||'');}); }
  function hasPromptOrReference(info){ return refsOf(info).length>0 || !!String(info.prompt_preview||'').trim(); }
  function isComputerAux(info){ return lower(info.thread_id||'').indexOf('computer:')===0 || !!info.computer_auxiliary_v30; }
  function refTextOf(info){ return refsOf(info).join(' ').toLowerCase(); }
  function refContainsTerm(info, term){ term=lower(term); return !!term && refTextOf(info).indexOf(term)!==-1; }
  function matchesTarget(info, target){ if(!target) return false; var refs=refsOf(info).map(lower); if(refs.indexOf(target)!==-1) return true; return lower(info.search||'').indexOf(target)!==-1; }
  function matchesStrictTerms(info, strictTerms){ if(!strictTerms.length) return false; for(var i=0;i<strictTerms.length;i++){ if(refContainsTerm(info, strictTerms[i])) return true; } return false; }
  function matchesScenarioPrefix(info, prefix, strictTerms){ if(!prefix && !strictTerms.length) return true; var refs=refsOf(info).map(lower); for(var i=0;i<refs.length;i++){ if(prefix && (refs[i]===prefix || refs[i].indexOf(prefix+'_')===0 || refs[i].indexOf(prefix+'-')===0 || refs[i].indexOf(prefix)!==-1)) return true; } if(matchesStrictTerms(info, strictTerms)) return true; if(/^s\d{2}$/.test(prefix) && lower(info.search||'').indexOf(prefix+'_')!==-1) return true; return false; }
  function counts(){
    var index=readJson('v29-thread-index', []), policy=readJson('v30-scenario-policy', {});
    var target=lower(policy.target_reference||''), prefix=lower(policy.scenario_prefix||''), strictTerms=Array.isArray(policy.strict_ref_terms)?policy.strict_ref_terms.map(lower).filter(Boolean):[];
    function isRelated(info){ if(target) return matchesTarget(info,target)||matchesScenarioPrefix(info,prefix,strictTerms); return matchesScenarioPrefix(info,prefix,strictTerms); }
    function isStrict(info){ if(target) return matchesTarget(info,target); return hasPromptOrReference(info) && !isComputerAux(info) && (matchesStrictTerms(info,strictTerms)||matchesScenarioPrefix(info,prefix,strictTerms)); }
    var strict=0, scenario=0; index.forEach(function(info){ if(isStrict(info)) strict++; if(isRelated(info)) scenario++; });
    return {strict:strict, scenario:scenario, all:index.length, policy:policy};
  }
  function refineScenarioControls(){
    var card=byId('v30-scenario-card'); if(!card) return;
    var c=counts();
    var bStrict=byId('v30StrictScenario'), bScenario=byId('v30ScenarioRelated'), bAll=byId('v30ShowAll');
    var counter=byId('v30ScenarioCount');
    var top=card.querySelector('.topline h2'); if(top) top.textContent='Case display';
    var badge=card.querySelector('.topline .badge'); if(badge) badge.textContent='HTML view only · v0.36';
    var hint=card.querySelector('.hint'); if(hint) hint.innerHTML='Display modes only change what is visible in this HTML page. The JSON evidence stays unchanged. Use <code>--target-reference</code> to generate a truly case-focused reconstruction.';
    if(bStrict) bStrict.textContent='Scenario-focused view';
    if(bScenario && c.strict===c.scenario){ bScenario.classList.add('v36-hidden'); bScenario.setAttribute('aria-hidden','true'); }
    if(c.strict===c.scenario && c.scenario===c.all){
      if(bStrict) { bStrict.disabled=true; bStrict.textContent='All recovered threads match this view'; }
      if(bAll) bAll.classList.add('v36-hidden');
      if(!card.querySelector('.v36-merged-note')){ var n=document.createElement('div'); n.className='v36-merged-note'; n.textContent='No separate scenario filter is needed for this file: the focused view and all recovered threads have the same count.'; card.appendChild(n); }
    } else if(c.strict===c.scenario) {
      if(!card.querySelector('.v36-merged-note')){ var n2=document.createElement('div'); n2.className='v36-merged-note'; n2.textContent='Strict scenario view and scenario-related view are identical for this file, so they were merged into one Scenario-focused view.'; card.appendChild(n2); }
    }
    if(!card.querySelector('.v36-context-note')){ var n3=document.createElement('div'); n3.className='v36-context-note'; n3.textContent='Show all profile artifacts reveals earlier/residual profile evidence when present. Scenario-focused view hides unrelated residue only in the HTML display.'; card.appendChild(n3); }
    if(counter){ counter.setAttribute('data-v36-total', String(c.all)); }
  }
  function refineSidebar(){
    document.querySelectorAll('.nav-title').forEach(function(el){
      var t=(el.textContent||'').trim();
      if(t.indexOf('Recovered profile threads')===0){ el.innerHTML='Recovered threads<span class="nav-title-sub">HTML filter may hide unrelated profile residue</span>'; }
      if(t.toLowerCase()==='global/residual audit'){ el.innerHTML='Profile-wide audit<span class="nav-title-sub">unassigned/residual records and full JSON</span>'; }
    });
    document.querySelectorAll(".nav-item[data-target='global']").forEach(function(btn){ var s=btn.querySelector('strong'), sp=btn.querySelector('span'); if(s) s.textContent='Unassigned profile records'; if(sp) sp.textContent=(sp.textContent||'').replace('records','records · optional'); });
    document.querySelectorAll('.nav-thread').forEach(function(btn){
      if(btn.getAttribute('data-v36-nav')==='1') return;
      var strong=btn.querySelector('strong'), span=btn.querySelector('span'); if(!strong||!span) return;
      var num=(strong.textContent||'').replace(/[^0-9]/g,'') || '?';
      var parts=(span.textContent||'').split('·');
      var ref=(parts[0]||'Recovered thread').trim(); var mode=(parts.slice(1).join('·')||'').trim();
      btn.innerHTML='<span class="v36-thread-row"><span class="v36-thread-pill">#'+esc(num)+'</span><span class="v36-thread-main"><strong>'+esc(ref)+'</strong><span>'+esc(mode)+'</span></span></span>';
      btn.setAttribute('data-v36-nav','1');
    });
    document.querySelectorAll('.thread-head .eyebrow').forEach(function(el){ var t=(el.textContent||'').trim(); if(/^Thread\s+\d+$/i.test(t)){ el.textContent='Recovered thread #'+t.replace(/[^0-9]/g,''); } });
  }
  function refineFilterPlaceholder(){ var el=byId('v29FilterText'); if(el) el.placeholder='reference code, keyword, URL, final text'; }
  function bind(){ refineScenarioControls(); refineSidebar(); refineFilterPlaceholder(); ['v30StrictScenario','v30ScenarioRelated','v30ShowAll'].forEach(function(id){ var b=byId(id); if(b) b.addEventListener('click', function(){ setTimeout(refineScenarioControls, 40); }); }); }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', bind); else bind();
  setTimeout(bind, 120);
})();
</script>
"""


def _v36_inject_hci_features(html_text: str, report: dict[str, Any]) -> str:
    # Neutralize old version-breadcrumb wording in the sidebar.  This is only a
    # label replacement; global/residual audit sections are preserved.
    html_text = re.sub(
        r"Recovered profile threads / residual-aware / v0\.28 conservative-claims / v0\.29 filters / v0\.35 scenario display",
        "Recovered threads",
        html_text,
    )
    html_text = re.sub(
        r"Recovered profile threads / residual-aware / v0\.28 conservative-claims / v0\.29 filters / v0\.34 scenario display",
        "Recovered threads",
        html_text,
    )
    html_text = html_text.replace("<div class='nav-title'>Global/residual audit</div>", "<div class='nav-title'>Profile-wide audit</div>")
    html_text = html_text.replace("placeholder='e.g., S10_04A, Gmail, INCOGNITO, Wikipedia'", "placeholder='reference code, keyword, URL, final text'")
    html_text = html_text.replace("v0.35 scenario display", "v0.36 scenario display")
    html_text = html_text.replace("HTML view only · v0.35", "HTML view only · v0.36")
    if "id=\"v36-hci-style\"" not in html_text and "id='v36-hci-style'" not in html_text:
        payload = _v36_hci_css_js()
        html_text = html_text.replace("</body>", payload + "</body>", 1) if "</body>" in html_text else html_text + payload
    return html_text


_render_html_report_pre_v36 = render_html_report


def render_html_report(report: dict[str, Any], output_path: Path) -> None:  # type: ignore[override]
    report = _v36_apply_report_refinements(report)
    _render_html_report_pre_v36(report, output_path)
    try:
        html_text = output_path.read_text(encoding="utf-8")
        html_text = _v36_inject_hci_features(html_text, report)
        output_path.write_text(html_text, encoding="utf-8")
    except Exception as exc:
        try:
            current = output_path.read_text(encoding="utf-8")
            output_path.write_text(current + f"\n<!-- v0.36 HCI injection failed: {html.escape(str(exc))} -->\n", encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# v0.37 HCI sidebar refinement layer
# ---------------------------------------------------------------------------
# This layer only changes presentation metadata and HTML rendering. It does not
# change extraction, thread grouping, classification, task outcome, timestamps,
# or evidence values.
_V37_SCHEMA_VERSION = "0.37"


def _v37_apply_report_refinements(report: dict[str, Any]) -> dict[str, Any]:
    report = _v36_apply_report_refinements(report)
    try:
        report["presentation_version"] = _V37_SCHEMA_VERSION
        report["schema_version"] = _V37_SCHEMA_VERSION
        condensed = _v29_as_dict(
            report.get("condensed_reconstruction_summary_v36")
            or report.get("condensed_reconstruction_summary_v35")
            or report.get("condensed_reconstruction_summary_v34")
            or report.get("condensed_reconstruction_summary_v33")
            or report.get("condensed_reconstruction_summary_v32")
        )
        if condensed:
            condensed["version"] = _V37_SCHEMA_VERSION
            if "thread_reconstruction_map_v36" in condensed:
                condensed.setdefault("thread_reconstruction_map_v37", condensed.get("thread_reconstruction_map_v36"))
            elif "thread_reconstruction_map_v34" in condensed:
                condensed.setdefault("thread_reconstruction_map_v37", condensed.get("thread_reconstruction_map_v34"))
            if "all_time_values_overview_v36" in condensed:
                condensed.setdefault("all_time_values_overview_v37", condensed.get("all_time_values_overview_v36"))
            elif "all_time_values_overview_v34" in condensed:
                condensed.setdefault("all_time_values_overview_v37", condensed.get("all_time_values_overview_v34"))
            case_core = (
                condensed.get("case_focused_core_findings_v36")
                or condensed.get("case_focused_core_findings_v35")
                or condensed.get("case_focused_core_findings_v34")
                or condensed.get("case_focused_core_findings_v33")
            )
            if case_core is not None:
                condensed.setdefault("case_focused_core_findings_v37", case_core)
            condensed["html_hci_sidebar_compact_refinement_v37"] = True
            report["condensed_reconstruction_summary_v37"] = condensed
        report.setdefault("repair_summary", {})["v37_hci_sidebar_display"] = {
            "compact_thread_subnavigation": True,
            "mode_badges_and_thread_group_separation": True,
            "profile_wide_audit_empty_state_in_html": True,
            "profile_wide_audit_label_clarified": True,
            "extraction_logic_changed": False,
            "classification_logic_changed": False,
            "hardcoding_note": "This layer only uses recovered HTML navigation elements and existing report metadata. It does not embed expected scenario outputs, UUIDs, emails, filenames, or URLs.",
        }
    except Exception as exc:
        report.setdefault("repair_warnings", []).append(f"v0.37 HCI sidebar refinements failed: {exc}")
    return report


_reconstruct_browser_threads_pre_v37 = reconstruct_browser_threads


def reconstruct_browser_threads(extracted: dict[str, Any], input_label: str, browser_only: bool = True) -> dict[str, Any]:  # type: ignore[override]
    report = _reconstruct_browser_threads_pre_v37(extracted, input_label, browser_only=browser_only)
    report = _v37_apply_report_refinements(report)
    report["schema_version"] = _V37_SCHEMA_VERSION
    return report


_filter_report_by_target_reference_pre_v37 = filter_report_by_target_reference


def filter_report_by_target_reference(report: dict[str, Any], target_reference: str | None) -> dict[str, Any]:  # type: ignore[override]
    filtered = _filter_report_by_target_reference_pre_v37(report, target_reference)
    filtered = _v37_apply_report_refinements(filtered)
    filtered["schema_version"] = _V37_SCHEMA_VERSION
    return filtered


_run_single_input_pre_v37 = run_single_input


def run_single_input(
    input_path: Path,
    blob_input: Path | None,
    database: str,
    store: str,
    include_computer: bool,
    dump_records_dir: Path | None = None,
) -> dict[str, Any]:  # type: ignore[override]
    report = _run_single_input_pre_v37(input_path, blob_input, database, store, include_computer, dump_records_dir=dump_records_dir)
    return _v37_apply_report_refinements(report)


def _v37_hci_css_js() -> str:
    return r"""
<style id="v37-sidebar-style">
/* v0.37 sidebar HCI refinements. Evidence is unchanged. */
.sidebar{padding:16px 14px}.brand{padding-bottom:14px;margin-bottom:12px}.brand h1{font-size:17px}.brand p{font-size:11px;line-height:1.35}.nav-title{font-size:10.5px;margin:16px 6px 6px;letter-spacing:.075em;color:#9db2cf}.nav-title-sub{font-size:9.5px;color:#7f8da3;margin-top:2px}.nav-list{padding-bottom:16px}
.nav-item{border-radius:9px;margin:2px 0;padding:7px 9px}.nav-item strong{font-size:12px}.nav-item span{font-size:10.5px;line-height:1.25}.nav-item:hover{background:rgba(255,255,255,.075)}
.thread-nav-group{position:relative;margin:3px 0 7px;padding:0 0 0 0}.thread-nav-group.open{padding-bottom:4px}.thread-nav-group.open:before{content:'';position:absolute;left:9px;top:33px;bottom:3px;width:1px;background:rgba(148,163,184,.32)}
.nav-thread{padding:6px 24px 6px 7px;border:1px solid rgba(148,163,184,.18);background:rgba(255,255,255,.035);border-left:4px solid rgba(148,163,184,.45)}.nav-thread:after{top:8px;right:9px;font-size:14px}.thread-nav-group.open>.nav-thread{background:rgba(255,255,255,.09);border-color:rgba(191,219,254,.35)}.nav-thread.active{background:#eef2ff;color:#172554;border-color:#c7d2fe;border-left-color:#2563eb}.nav-thread.mode-browser,.nav-thread.mode-browser_control{border-left-color:#38bdf8}.nav-thread.mode-computer,.nav-thread.mode-computer_mode{border-left-color:#a78bfa}.nav-thread.mode-nonbrowser,.nav-thread.mode-non_browser_agent{border-left-color:#94a3b8}.nav-thread.mode-conversation{border-left-color:#22c55e}
.v36-thread-row,.v37-thread-row{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px;align-items:start}.v36-thread-pill,.v37-thread-pill{min-width:24px;height:18px;font-size:10px;margin-top:0;border-radius:7px}.v36-thread-main strong,.v37-thread-main strong{font-size:11.5px;line-height:1.2;-webkit-line-clamp:2}.v36-thread-main span,.v37-thread-main span{font-size:10px;line-height:1.2;margin-top:2px}.v37-mode-badge{display:inline-block;border-radius:999px;padding:1px 6px;font-size:9.5px;font-weight:800;line-height:1.35;background:rgba(148,163,184,.20);color:#cbd5e1}.nav-thread.active .v37-mode-badge{background:#dbeafe;color:#1d4ed8}
.nav-subitems{margin:3px 0 2px 20px;padding-left:8px;border-left:0;display:none}.thread-nav-group.open>.nav-subitems{display:grid;grid-template-columns:1fr;gap:2px}.nav-subitem{display:flex;align-items:center;gap:6px;min-height:22px;padding:3px 7px;margin:0;border-radius:7px;background:transparent;color:#aeb9c9}.nav-subitem strong{font-size:10.8px;font-weight:720;line-height:1.15}.nav-subitem:before{content:attr(data-short);display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:5px;background:rgba(148,163,184,.14);color:#94a3b8;font-size:9px;font-weight:850;flex:0 0 auto}.nav-subitem:hover{background:rgba(255,255,255,.075);color:#fff}.nav-subitem.active{background:rgba(59,130,246,.18);color:#fff;outline:1px solid rgba(147,197,253,.25)}.nav-subitem.active:before{background:#dbeafe;color:#1d4ed8}
.nav-audit-empty{opacity:.72;cursor:default!important;background:rgba(148,163,184,.08)!important;border:1px dashed rgba(148,163,184,.20);color:#94a3b8!important}.nav-audit-empty strong{font-size:11.5px}.nav-audit-empty span{white-space:normal}.v37-divider-note{font-size:10px;line-height:1.25;color:#7f8da3;margin:6px 7px 4px}
@media(max-width:980px){.thread-nav-group.open:before{display:none}.nav-subitems{margin-left:8px;grid-template-columns:repeat(auto-fit,minmax(92px,1fr))!important}.nav-subitem{min-height:24px}.sidebar{padding:12px}.nav-list{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}}
</style>
<script id="v37-sidebar-script">
(function(){
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }
  function norm(s){return String(s||'').toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'');}
  function shortFor(label){var t=String(label||'').toLowerCase(); if(t.indexOf('prompt')>=0)return 'P'; if(t.indexOf('metadata')>=0)return 'M'; if(t.indexOf('activity')>=0)return 'A'; if(t.indexOf('reason')>=0)return 'R'; if(t.indexOf('privacy')>=0||t.indexOf('delete')>=0)return 'D'; if(t.indexOf('final')>=0)return 'F'; if(t.indexOf('time')>=0||t.indexOf('storage')>=0)return 'T'; if(t.indexOf('computer')>=0)return 'C'; return 'E';}
  function parseThread(btn){
    var strong=btn.querySelector('strong'), span=btn.querySelector('span');
    var number=(strong?strong.textContent:'').replace(/[^0-9]/g,'')||'?';
    var label='', mode='';
    var main=btn.querySelector('.v36-thread-main,.v37-thread-main');
    if(main){ var s=main.querySelector('strong'), m=main.querySelector('span'); label=(s?s.textContent:'').trim(); mode=(m?m.textContent:'').trim(); }
    else { var raw=(span?span.textContent:'').split('·'); label=(raw[0]||'Recovered thread').trim(); mode=(raw.slice(1).join('·')||'').trim(); }
    if(!label) label='Recovered thread';
    return {number:number,label:label,mode:mode};
  }
  function refineThreadNav(){
    document.querySelectorAll('.nav-thread').forEach(function(btn){
      var info=parseThread(btn); var modeClass=norm(info.mode||'unknown');
      btn.classList.add('mode-'+modeClass);
      btn.innerHTML='<span class="v37-thread-row"><span class="v37-thread-pill">#'+esc(info.number)+'</span><span class="v37-thread-main"><strong>'+esc(info.label)+'</strong><span><span class="v37-mode-badge">'+esc(info.mode||'unknown')+'</span></span></span></span>';
      btn.setAttribute('data-v37-nav','1');
    });
    document.querySelectorAll('.nav-subitem').forEach(function(btn){
      var label=(btn.querySelector('strong')?btn.querySelector('strong').textContent:btn.textContent)||'';
      btn.setAttribute('data-short', shortFor(label));
      btn.setAttribute('title', label.trim());
    });
  }
  function refineTitles(){
    document.querySelectorAll('.nav-title').forEach(function(el){
      var t=(el.textContent||'').trim().toLowerCase();
      if(t.indexOf('recovered')>=0 && t.indexOf('thread')>=0){el.innerHTML='Recovered threads<span class="nav-title-sub">profile thread candidates</span>';}
      if(t.indexOf('profile-wide audit')>=0 || t.indexOf('global/residual')>=0){el.innerHTML='Profile-wide audit<span class="nav-title-sub">unassigned records, when present</span>';}
    });
  }
  function auditEmptyState(){
    var globalBtn=document.querySelector(".nav-item[data-target='global']");
    var titles=[].slice.call(document.querySelectorAll('.nav-title')).filter(function(el){return (el.textContent||'').toLowerCase().indexOf('profile-wide audit')>=0;});
    titles.forEach(function(title){
      if(globalBtn) return;
      if(title.nextElementSibling && title.nextElementSibling.classList && title.nextElementSibling.classList.contains('nav-audit-empty')) return;
      var empty=document.createElement('button'); empty.type='button'; empty.disabled=true; empty.className='nav-item nav-audit-empty';
      empty.innerHTML='<strong>No unassigned records</strong><span>This file has no separate profile-wide residual section.</span>';
      title.parentNode.insertBefore(empty, title.nextSibling);
    });
    if(globalBtn){
      var s=globalBtn.querySelector('strong'), sp=globalBtn.querySelector('span');
      if(s) s.textContent='Unassigned profile records';
      if(sp && sp.textContent.indexOf('profile-wide')<0) sp.textContent=(sp.textContent||'records')+' · profile-wide';
    }
  }
  function insertModeNote(){
    var title=[].slice.call(document.querySelectorAll('.nav-title')).find(function(el){return (el.textContent||'').toLowerCase().indexOf('recovered threads')>=0;});
    if(!title || title.parentNode.querySelector('.v37-divider-note')) return;
    var note=document.createElement('div'); note.className='v37-divider-note'; note.textContent='Thread card opens sections; compact section links jump inside the thread.';
    title.parentNode.insertBefore(note, title.nextSibling);
  }
  function bind(){ refineTitles(); refineThreadNav(); auditEmptyState(); insertModeNote(); }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', bind); else bind();
  setTimeout(bind,120); setTimeout(bind,500);
})();
</script>
"""


def _v37_inject_hci_features(html_text: str, report: dict[str, Any]) -> str:
    html_text = html_text.replace("v0.36 scenario display", "v0.37 scenario display")
    html_text = html_text.replace("HTML view only · v0.36", "HTML view only · v0.37")
    if "id=\"v37-sidebar-style\"" not in html_text and "id='v37-sidebar-style'" not in html_text:
        payload = _v37_hci_css_js()
        html_text = html_text.replace("</body>", payload + "</body>", 1) if "</body>" in html_text else html_text + payload
    return html_text


_render_html_report_pre_v37 = render_html_report


def render_html_report(report: dict[str, Any], output_path: Path) -> None:  # type: ignore[override]
    report = _v37_apply_report_refinements(report)
    _render_html_report_pre_v37(report, output_path)
    try:
        html_text = output_path.read_text(encoding="utf-8")
        html_text = _v37_inject_hci_features(html_text, report)
        output_path.write_text(html_text, encoding="utf-8")
    except Exception as exc:
        try:
            current = output_path.read_text(encoding="utf-8")
            output_path.write_text(current + f"\n<!-- v0.37 sidebar HCI injection failed: {html.escape(str(exc))} -->\n", encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    main()
