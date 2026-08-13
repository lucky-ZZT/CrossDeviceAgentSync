import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import generic_sync
import session_merge_planner as planner


class GenericSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.left = self.root / "agent-a"
        self.right = self.root / "agent-b"
        self.left.mkdir()
        self.right.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_selective_custom_file_sync_and_conflict_preservation(self):
        (self.left / "shared.txt").write_text("left", encoding="utf-8")
        (self.left / "only-left.json").write_text("left-only", encoding="utf-8")
        (self.left / ".env").write_text("SECRET", encoding="utf-8")
        (self.right / "shared.txt").write_text("right", encoding="utf-8")
        (self.right / "only-right.json").write_text("right-only", encoding="utf-8")

        left = generic_sync.snapshot(self.left, "agent-a", "**/*", "")
        right = generic_sync.snapshot(self.right, "agent-b", "**/*", "")
        self.assertNotIn(".env", {item["path"] for item in left["items"]})
        plan = generic_sync.compare(left, right, "bidirectional")
        selected = {entry["path"] for entry in plan["entries"] if entry["selected"]}
        self.assertIn("shared.txt", selected)
        self.assertIn("only-left.json", selected)
        self.assertIn("only-right.json", selected)

        left_snapshot = self.root / "left.json"
        right_snapshot = self.root / "right.json"
        plan_path = self.root / "plan.json"
        planner.write_json(left_snapshot, left)
        planner.write_json(right_snapshot, right)
        planner.write_json(plan_path, plan)
        bundle = self.root / "bundle.zip"
        generic_sync.create_bundle(left_snapshot, plan_path, "left", bundle)
        report = generic_sync.restore_bundle(bundle, self.right, require_empty_lock=True)

        self.assertTrue((self.right / "only-left.json").is_file())
        self.assertTrue(any("from-agent-a" in path.name for path in self.right.glob("shared*.txt")))
        self.assertTrue(Path(report["backup_path"]).is_dir())

    def test_local_agent_roots_can_be_compared_without_codex_specific_state(self):
        (self.left / "agent-config.json").write_text('{"agent":"a"}', encoding="utf-8")
        (self.right / "agent-config.json").write_text('{"agent":"b"}', encoding="utf-8")
        left = generic_sync.snapshot(self.left, "local-agent-a", ["agent-config.json"])
        right = generic_sync.snapshot(self.right, "local-agent-b", ["agent-config.json"])
        plan = generic_sync.compare(left, right, "left-to-right")
        self.assertEqual(plan["summary"]["conflicts"], 1)
        self.assertEqual(plan["entries"][0]["action"], "copy_left_as_conflict")


if __name__ == "__main__":
    unittest.main()
