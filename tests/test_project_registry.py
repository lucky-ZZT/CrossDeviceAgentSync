import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import project_registry


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / ".codex"
        self.codex_home.mkdir()
        self.project = self.root / "projects" / "demo"
        self.project.mkdir(parents=True)
        self.state_path = self.codex_home / ".codex-global-state.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_creates_normal_project_registration_and_assigns_tasks(self):
        preview = project_registry.inspect_project_conflicts(
            self.codex_home, self.project, "demo"
        )

        self.assertEqual(preview["environment"], "empty")
        self.assertEqual(preview["recommended_action"], "create_project")
        result = project_registry.register_project_offline(
            self.codex_home,
            self.project,
            "demo",
            "create_project",
            task_ids=["task-1"],
            expected_state_sha256=preview["global_state_sha256"],
            require_codex_closed=False,
        )

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        project = state["local-projects"][result["project_id"]]
        self.assertEqual(project["rootPaths"], [str(self.project.resolve())])
        self.assertFalse(project["rootPaths"][0].startswith("\\\\?\\"))
        self.assertIn(result["project_id"], state["project-order"])
        self.assertIn(result["project_id"], state["electron-saved-workspace-roots"])
        self.assertEqual(
            state["thread-project-assignments"]["task-1"]["projectId"],
            result["project_id"],
        )
        self.assertTrue(Path(result["backup_path"]).is_dir())

    def test_reuses_existing_project_without_creating_duplicate(self):
        normal = str(self.project.resolve())
        self.state_path.write_text(json.dumps({
            "project-order": ["existing"],
            "local-projects": {
                "existing": {"id": "existing", "name": "demo", "rootPaths": [normal]},
            },
        }), encoding="utf-8")
        preview = project_registry.inspect_project_conflicts(
            self.codex_home, self.project, "demo"
        )

        self.assertEqual(preview["conflict"], "same_path_existing")
        result = project_registry.register_project_offline(
            self.codex_home,
            self.project,
            "demo",
            "reuse_existing",
            task_ids=["task-2"],
            keeper_id="existing",
            expected_state_sha256=preview["global_state_sha256"],
            require_codex_closed=False,
        )

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(list(state["local-projects"]), ["existing"])
        self.assertEqual(result["project_id"], "existing")
        self.assertEqual(state["thread-project-assignments"]["task-2"]["projectId"], "existing")

    def test_merges_extended_duplicate_into_normal_keeper(self):
        normal = str(self.project.resolve())
        extended = "\\\\?\\" + normal
        self.state_path.write_text(json.dumps({
            "project-order": ["duplicate", "keeper"],
            "selected-project": {"type": "local", "projectId": "duplicate"},
            "thread-project-assignments": {
                "old-task": {"projectKind": "local", "projectId": "duplicate"},
            },
            "local-projects": {
                "keeper": {"id": "keeper", "name": "demo", "rootPaths": [normal]},
                "duplicate": {"id": "duplicate", "name": "demo", "rootPaths": [extended]},
            },
        }), encoding="utf-8")
        preview = project_registry.inspect_project_conflicts(
            self.codex_home, self.project, "demo"
        )

        self.assertEqual(preview["conflict"], "same_path_duplicate")
        result = project_registry.register_project_offline(
            self.codex_home,
            self.project,
            "demo",
            "merge_registration",
            task_ids=["new-task"],
            keeper_id="keeper",
            expected_state_sha256=preview["global_state_sha256"],
            require_codex_closed=False,
        )

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(result["merged_project_ids"], ["duplicate"])
        self.assertNotIn("duplicate", state["local-projects"])
        self.assertEqual(state["project-order"], ["keeper"])
        self.assertEqual(state["selected-project"]["projectId"], "keeper")
        self.assertEqual(state["thread-project-assignments"]["old-task"]["projectId"], "keeper")
        self.assertEqual(state["thread-project-assignments"]["new-task"]["projectId"], "keeper")

    def test_rejects_state_changed_after_preview(self):
        preview = project_registry.inspect_project_conflicts(
            self.codex_home, self.project, "demo"
        )
        self.state_path.write_text(json.dumps({"local-projects": {}}), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "changed after preview"):
            project_registry.register_project_offline(
                self.codex_home,
                self.project,
                "demo",
                "create_project",
                expected_state_sha256=preview["global_state_sha256"],
                require_codex_closed=False,
            )


if __name__ == "__main__":
    unittest.main()
