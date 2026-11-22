"""Content, package-relative names and membership contribute to provenance."""
import tempfile
import unittest
from pathlib import Path

from comettrace.provenance import module_file_hashes, modules_digest


class ModuleIntegrity(unittest.TestCase):
    def test_edits_renames_and_missing_files_change_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            folder = package / "v038"
            folder.mkdir()
            path = folder / "records.py"
            path.write_bytes(b"first")
            original = modules_digest(module_file_hashes(package))
            path.write_bytes(b"edited")
            self.assertNotEqual(original, modules_digest(module_file_hashes(package)))
            path.write_bytes(b"first")
            path.rename(folder / "other.py")
            self.assertNotEqual(original, modules_digest(module_file_hashes(package)))
            (folder / "other.py").unlink()
            self.assertNotEqual(original, modules_digest(module_file_hashes(package)))
