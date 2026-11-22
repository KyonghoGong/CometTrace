"""Plan report destinations and publish a completed set of report files."""
from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import unicodedata
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from urllib.parse import quote

from .. import legacy_engine as legacy


def _name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def check_destinations(paths):
    resolved_paths = [path.resolve() for path in paths]
    names = set()
    identities = set()
    for path, resolved in zip(paths, resolved_paths):
        if os.name == "nt" and any(
            part.endswith((" ", ".")) or ":" in part or Path(part).is_reserved()
            for part in resolved.parts[1:]
        ):
            raise ValueError(f"Invalid Windows output path: {path}")
        if any(resolved in other.parents for other in resolved_paths):
            raise ValueError(f"Output is also a parent directory: {path}")
        key = _name_key(str(resolved))
        if key in names:
            raise ValueError(f"Output path collision: {path}")
        names.add(key)
        if resolved.exists():
            if not resolved.is_file():
                raise ValueError(f"Output path is not a regular file: {path}")
            info = resolved.stat()
            identity = (info.st_dev, info.st_ino)
            if info.st_ino and identity in identities:
                raise ValueError(f"Output paths refer to the same file: {path}")
            identities.add(identity)


@contextmanager
def staged_files(paths):
    """Render beside each destination, then replace, restoring previous files on error."""
    check_destinations(paths)
    targets = [p.resolve() for p in paths]
    work = {}
    replaced = []
    retain = set()
    try:
        for dest in targets:
            dest.parent.mkdir(parents=True, exist_ok=True)
            token = hashlib.sha256(_name_key(dest.name).encode("utf-8")).hexdigest()[:24]
            scratch = dest.parent / (".comettrace-write-" + token)
            if any(scratch == other or scratch in other.parents for other in targets):
                raise ValueError(f"Output conflicts with staging directory: {scratch}")
            try:
                scratch.mkdir()
            except FileExistsError as exc:
                raise OSError(f"Output is already reserved: {dest} (see {scratch})") from exc
            work[dest] = scratch
        yield [scratch / "pending" for scratch in work.values()]
        for dest, scratch in work.items():
            if not (scratch / "pending").is_file():
                raise OSError(f"Report was not written: {dest}")
            if dest.exists():
                try:
                    os.link(dest, scratch / "previous")
                except OSError:
                    shutil.copy2(dest, scratch / "previous")
        for dest, scratch in work.items():
            os.replace(scratch / "pending", dest)
            replaced.append(dest)
    except BaseException:
        for dest in reversed(replaced):
            backup = work[dest] / "previous"
            try:
                if backup.exists():
                    os.replace(backup, dest)
                else:
                    dest.unlink()
            except OSError:
                retain.add(work[dest])
        if retain:
            raise OSError(f"Rollback incomplete. Recovery files retained in: {sorted(map(str, retain))}")
        raise
    finally:
        for scratch in work.values():
            if scratch not in retain:
                shutil.rmtree(scratch)


def _stage_token(stage_id, used):
    name = unicodedata.normalize("NFC", stage_id)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "stage"
    # Leave room for the output stem, extension and disambiguating suffix.
    name = name.encode("utf-8")[:100].decode("utf-8", errors="ignore")
    token = name
    counter = 1
    while _name_key(token) in used:
        counter += 1
        token = f"{name}_{counter}"
    used.add(_name_key(token))
    return token


def _html_index(stages):
    rows = []
    for stage in stages:
        href = quote(Path(stage["output_html"]).name, safe="")
        label = html.escape(stage["stage_id"])
        source = html.escape(stage["rel_leveldb_path"])
        rows.append(f'<li><a href="{href}">{label}</a> <code>{source}</code></li>')
    return ('<!doctype html>\n<html lang="en"><meta charset="utf-8">'
            '<title>CometTrace stage reports</title><h1>CometTrace stage reports</h1>\n<ul>\n'
            + "\n".join(rows) + "\n</ul></html>\n")


def save_reports(report, json_path, html_path=None, stage_reports=()):
    outputs = []
    used = set()
    stages = report["stages"] if stage_reports else ()
    for stage, (location, result) in zip(stages, stage_reports, strict=True):
        token = _stage_token(location.stage_id, used)
        for parent, key, writer in ((json_path, "output_json", partial(legacy.write_json, payload=result)),
                                    (html_path, "output_html", partial(legacy.render_html_report, result))):
            if parent is not None:
                child = parent.with_name(f"{parent.stem}__{token}{parent.suffix}")
                stage[key] = str(child)
                outputs.append((child, writer))
    # The frozen HTML renderer updates its report in place. Serialize JSON first.
    outputs.append((json_path, partial(legacy.write_json, payload=report)))
    if html_path is not None:
        if stage_reports:
            index = _html_index(stages)
            outputs.append((html_path, lambda path: path.write_text(index, encoding="utf-8")))
        else:
            outputs.append((html_path, partial(legacy.render_html_report, report)))
    with staged_files([path for path, _ in outputs]) as pending:
        for (_, writer), path in zip(outputs, pending):
            writer(path)
