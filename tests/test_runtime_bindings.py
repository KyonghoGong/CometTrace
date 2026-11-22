"""Verify that nested entry and exception handling preserve the enclosing engine."""
import unittest

from comettrace import legacy_engine
from comettrace.v038.runtime import _bindings, _v038_runtime_patches


class RuntimeBindings(unittest.TestCase):
    def test_nested_exception_restores_every_binding(self):
        handlers = _bindings()
        original = {key: getattr(legacy_engine, key) for key in handlers}
        with _v038_runtime_patches():
            outer = {key: getattr(legacy_engine, key) for key in handlers}
            self.assertEqual(handlers, outer)
            with self.assertRaisesRegex(ValueError, "fixture"):
                with _v038_runtime_patches():
                    raise ValueError("fixture")
            for key in handlers:
                self.assertIs(getattr(legacy_engine, key), outer[key])
        for key in handlers:
            self.assertIs(getattr(legacy_engine, key), original[key])
