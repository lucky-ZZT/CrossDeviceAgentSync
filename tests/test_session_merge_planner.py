import importlib.util
import json
import tempfile
import unittest
import uuid
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "session_merge_planner.py"
SPEC = importlib.util.spec_from_file_location("session_merge_planner", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def event(event_type, payload):
    return json.dumps({"type": event_type, "payload": payload}, sort_keys=True) + "\n"


class SessionMergePlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.left_home = self.root / "left"
        self.right_home = self.root / "right"
        (self.left_home / "sessions").mkdir(parents=True)
        (self.right_home / "sessions").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_accepts_modern_uuid_v7_session_ids(self):
        task_id = "019fd2a1-150f-7672-af12-5d6b7ec234fd"
        self.assertEqual(MODULE.first_uuid(task_id), task_id)

    def write_session(self, home, task_id, rows):
        path = home / "sessions" / f"{task_id}.jsonl"
        path.write_text("".join(rows), encoding="utf-8")

    def base(self, task_id, cwd="C:/left", provider="openai"):
        return event("session_meta", {
            "id": task_id,
            "thread_name": f"Task {task_id[:8]}",
            "cwd": cwd,
            "model_provider": provider,
        })

    def test_classifies_all_primary_relationships(self):
        ids = {name: str(uuid.uuid4()) for name in (
            "identical", "equivalent", "left_only", "right_only", "left_ahead", "right_ahead", "diverged", "collision"
        )}
        common = event("message", {"role": "user", "content": "common"})

        self.write_session(self.left_home, ids["identical"], [self.base(ids["identical"]), common])
        self.write_session(self.right_home, ids["identical"], [self.base(ids["identical"]), common])
        self.write_session(self.left_home, ids["equivalent"], [self.base(ids["equivalent"], "C:/left", "p1"), common])
        self.write_session(self.right_home, ids["equivalent"], [self.base(ids["equivalent"], "/Users/right", "p2"), common])
        self.write_session(self.left_home, ids["left_only"], [self.base(ids["left_only"]), common])
        self.write_session(self.right_home, ids["right_only"], [self.base(ids["right_only"]), common])
        self.write_session(self.left_home, ids["left_ahead"], [self.base(ids["left_ahead"]), common, event("message", {"content": "left new"})])
        self.write_session(self.right_home, ids["left_ahead"], [self.base(ids["left_ahead"]), common])
        self.write_session(self.left_home, ids["right_ahead"], [self.base(ids["right_ahead"]), common])
        self.write_session(self.right_home, ids["right_ahead"], [self.base(ids["right_ahead"]), common, event("message", {"content": "right new"})])
        self.write_session(self.left_home, ids["diverged"], [self.base(ids["diverged"]), common, event("message", {"content": "left tail"})])
        self.write_session(self.right_home, ids["diverged"], [self.base(ids["diverged"]), common, event("message", {"content": "right tail"})])
        self.write_session(self.left_home, ids["collision"], [self.base(ids["collision"]), event("message", {"content": "left"})])
        self.write_session(self.right_home, ids["collision"], [event("other", {"id": ids["collision"], "content": "unrelated"})])

        left = MODULE.inventory(self.left_home, "left")
        right = MODULE.inventory(self.right_home, "right")
        plan = MODULE.compare_inventories(left, right, "bidirectional", set(), set())
        actual = {entry["task_id"]: entry["classification"] for entry in plan["entries"]}

        expected_names = {
            "equivalent": "metadata_equivalent",
            "collision": "id_collision",
        }
        for name, task_id in ids.items():
            self.assertEqual(actual[task_id], expected_names.get(name, name))
        self.assertEqual(plan["summary"]["total"], len(ids))

    def test_include_and_exclude_seal_selection(self):
        left_only = str(uuid.uuid4())
        excluded = str(uuid.uuid4())
        self.write_session(self.left_home, left_only, [self.base(left_only)])
        self.write_session(self.left_home, excluded, [self.base(excluded)])
        left = MODULE.inventory(self.left_home, "left")
        right = MODULE.inventory(self.right_home, "right")

        plan = MODULE.compare_inventories(left, right, "left-to-right", {left_only}, {excluded})
        selected = {entry["task_id"] for entry in plan["entries"] if entry["selected"]}

        self.assertEqual(selected, {left_only})


if __name__ == "__main__":
    unittest.main()
