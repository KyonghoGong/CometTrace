import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from comettrace import engine_v038 as engine
from comettrace.v038 import outputs
from comettrace.v038.stages import discover_leveldb_stages


class ReportOutputs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.json = self.root / "case.json"
        self.html = self.root / "case.html"

    def bundle(self, names):
        rows = [dict(stage_id=s, rel_leveldb_path=f"profile-{i}/db") for i, s in enumerate(names)]
        return {"stages": rows}, [(SimpleNamespace(stage_id=s), {"stage": i}) for i, s in enumerate(names)]

    def test_unicode_case_and_normalization_collisions(self):
        import json
        names = ["단계 가", "단계 나", "Café", "Cafe\u0301", "CAFÉ", "a#&b"]
        bundle, parts = self.bundle(names)
        with patch.object(outputs.legacy, "render_html_report", lambda report, path: path.write_text("stage")):
            outputs.save_reports(bundle, self.json, self.html, parts)
        rows = json.loads(self.json.read_bytes())["stages"]
        self.assertEqual(len({r["output_json"].casefold() for r in rows}), len(names))
        index = self.html.read_text(encoding="utf8")
        for i, row in enumerate(rows):
            self.assertEqual(json.loads(Path(row["output_json"]).read_bytes()), {"stage": i})
            self.assertIn('href="' + quote(Path(row["output_html"]).name, safe="") + '"', index)
        self.assertIn("단계 가", Path(rows[0]["output_html"]).name)
        self.assertIn("a#&amp;b", index)

    def test_duplicate_directory_labels_remain_selectable(self):
        for top, label in [("one", "repeat"), ("two", "repeat"), ("three", "repeat_2")]:
            (self.root / top / label / "IndexedDB" / engine.leg.TARGET_LEVELDB_NAME).mkdir(parents=True)
        stages = discover_leveldb_stages(self.root)
        self.assertEqual(len({s.stage_id.casefold() for s in stages}), 3)
        for stage in stages:
            self.assertIs(engine.select_stage(stages, stage.stage_id), stage)

    def test_aliases_rejected_before_analysis(self):
        with patch.object(engine, "run_input_v038") as analyze:
            for engine_name in ("v038", "v037"):
                with self.subTest(engine=engine_name), self.assertRaises(SystemExit) as rc:
                    engine.main(["--engine", engine_name, "--input", "dummy", "--output",
                                 str(self.json), "--html-output", str(self.root / "CASE.JSON")])
                self.assertNotEqual(rc.exception.code, 0)
            analyze.assert_not_called()
        self.json.write_bytes(b"previous")
        os.link(self.json, self.html)
        with self.assertRaisesRegex(ValueError, "same file"):
            outputs.check_destinations([self.json, self.html])

    def test_child_colliding_with_requested_index(self):
        bundle, parts = self.bundle(["U"])
        with self.assertRaisesRegex(ValueError, "collision"):
            outputs.save_reports(bundle, self.json, self.root / "case__U.json", parts)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_json_precedes_mutating_html_renderer(self):
        import json
        report = {"schema_version": "0.38"}
        def render(value, path):
            value["schema_version"] = "display"
            path.write_bytes(b"html")
        with patch.object(outputs.legacy, "render_html_report", render):
            outputs.save_reports(report, self.json, self.html)
        self.assertEqual(json.loads(self.json.read_bytes()), {"schema_version": "0.38"})
        self.assertEqual(self.html.read_bytes(), b"html")

    def test_render_failure_preserves_every_previous_file(self):
        bundle, parts = self.bundle(["first", "second"])
        self.html.write_bytes(b"old index")
        stage = self.root / "case__first.json"
        stage.write_bytes(b"old stage")
        with patch.object(outputs.legacy, "render_html_report", side_effect=[None, OSError("full")]):
            with self.assertRaisesRegex(OSError, "full"):
                outputs.save_reports(bundle, self.json, self.html, parts)
        self.assertEqual(self.html.read_bytes(), b"old index")
        self.assertEqual(stage.read_bytes(), b"old stage")
        self.assertEqual(set(self.root.iterdir()), {stage, self.html})

    def test_failed_replace_restores_replaced_and_new_files(self):
        new = self.root / "new"
        self.json.write_bytes(b"old JSON")
        self.html.write_bytes(b"old HTML")
        replace = os.replace
        def fail_last(src, dst):
            if dst == self.html and src.name == "pending":
                raise PermissionError("locked")
            return replace(src, dst)
        with patch.object(outputs.os, "replace", side_effect=fail_last):
            with self.assertRaisesRegex(PermissionError, "locked"):
                with outputs.staged_files([self.json, new, self.html]) as pending:
                    for path in pending:
                        path.write_bytes(b"changed")
        self.assertFalse(new.exists())
        self.assertEqual(self.html.read_bytes(), b"old HTML")
        self.assertEqual(self.json.read_bytes(), b"old JSON")

    def test_overlapping_writer_leaves_first_writer_intact(self):
        with outputs.staged_files([self.json]) as pending:
            with self.assertRaisesRegex(OSError, "already reserved"):
                with outputs.staged_files([self.json]):
                    self.fail("second writer acquired a reserved destination")
            pending[0].write_bytes(b"complete")
        self.assertEqual(self.json.read_bytes(), b"complete")
