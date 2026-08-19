import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import generic_sync
import project_import


class ProjectImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "old-project"
        self.source.mkdir()
        (self.source / "README.md").write_text("old project", encoding="utf-8")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "config").write_text("[core]", encoding="utf-8")
        (self.source / ".env").write_text("TOKEN=secret", encoding="utf-8")
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules" / "large.js").write_text("skip", encoding="utf-8")
        self.bundle = self.root / "old-project.cdas.zip"
        self.projects_root = self.root / "Imported Projects"

    def tearDown(self):
        self.temporary.cleanup()

    def test_project_import_creates_unique_directory_without_touching_existing_projects(self):
        project_import.create_project_bundle(self.source, self.bundle)
        existing = self.projects_root / "old-project-from-old-computer"
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_text("local project", encoding="utf-8")

        preview = project_import.prepare_project_import(self.bundle, self.projects_root)

        self.assertEqual(preview["target_root"].name, "old-project-from-old-computer-2")
        self.assertFalse(preview["target_root"].exists())
        self.assertEqual((existing / "keep.txt").read_text(encoding="utf-8"), "local project")
        result = project_import.restore_project_bundle(
            self.bundle,
            self.projects_root,
            preview["target_root"],
        )
        imported = Path(result["project_path"])
        self.assertEqual((imported / "README.md").read_text(encoding="utf-8"), "old project")
        self.assertTrue((imported / ".git" / "config").is_file())
        self.assertFalse((imported / ".env").exists())
        self.assertFalse((imported / "node_modules").exists())
        self.assertEqual((existing / "keep.txt").read_text(encoding="utf-8"), "local project")
        self.assertTrue(Path(result["backup_path"]).is_dir())

    def test_preview_is_read_only_when_project_root_does_not_exist(self):
        project_import.create_project_bundle(self.source, self.bundle)
        preview = project_import.prepare_project_import(self.bundle, self.projects_root)

        self.assertEqual(preview["file_count"], 2)
        self.assertFalse(self.projects_root.exists())
        self.assertFalse(preview["target_root"].exists())

    def test_sensitive_files_are_only_exported_when_selected(self):
        project_import.create_project_bundle(self.source, self.bundle, include_sensitive=True)
        preview = project_import.prepare_project_import(self.bundle, self.projects_root)
        project_import.restore_project_bundle(self.bundle, self.projects_root, preview["target_root"])

        self.assertEqual((preview["target_root"] / ".env").read_text(encoding="utf-8"), "TOKEN=secret")

    def test_generic_bundle_cannot_be_imported_as_a_project(self):
        empty = self.root / "empty"
        empty.mkdir()
        left = generic_sync.snapshot(self.source, "source")
        right = generic_sync.snapshot(empty, "target")
        plan = generic_sync.compare(left, right, "left-to-right")
        left_path = self.root / "left.json"
        plan_path = self.root / "plan.json"
        left_path.write_text(__import__("json").dumps(left), encoding="utf-8")
        plan_path.write_text(__import__("json").dumps(plan), encoding="utf-8")
        generic_bundle = self.root / "generic.zip"
        generic_sync.create_bundle(left_path, plan_path, "left", generic_bundle)

        with self.assertRaisesRegex(ValueError, "not a project"):
            project_import.prepare_project_import(generic_bundle, self.projects_root)


if __name__ == "__main__":
    unittest.main()
