import importlib.util
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pyinstaller_runtime_hook.py"
SPEC = importlib.util.spec_from_file_location("pyinstaller_runtime_hook_test", SCRIPT)
HOOK = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(HOOK)


class RuntimeHookTests(unittest.TestCase):
    def test_retries_transient_permission_error(self):
        class Reader:
            def __init__(self):
                self.calls = 0

            def extract(self, name, raw=False):
                self.calls += 1
                if self.calls < 3:
                    raise PermissionError("temporarily locked")
                return name, raw

        class ArchiveModule:
            ZlibArchiveReader = Reader

        self.assertTrue(HOOK.install_archive_retry(ArchiveModule))
        reader = Reader()
        with mock.patch.object(HOOK.time, "sleep") as sleep:
            self.assertEqual(reader.extract("module", raw=True), ("module", True))
        self.assertEqual(reader.calls, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(HOOK.install_archive_retry(ArchiveModule))

    def test_preserves_non_permission_errors(self):
        class Reader:
            def extract(self, name, raw=False):
                raise ValueError(name)

        class ArchiveModule:
            ZlibArchiveReader = Reader

        HOOK.install_archive_retry(ArchiveModule)
        with self.assertRaisesRegex(ValueError, "broken"):
            Reader().extract("broken")


if __name__ == "__main__":
    unittest.main()
