import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import local_agent_handoff
import session_merge_planner as planner


class LocalAgentHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".codex"
        (self.home / "sessions").mkdir(parents=True)
        self.task_id = str(uuid.uuid4())
        self.session = self.home / "sessions" / f"{self.task_id}.jsonl"
        self.session.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": self.task_id, "thread_name": "Source chat", "agent_nickname": "Bohr"}}) + "\n",
            encoding="utf-8",
        )
        (self.home / "session_index.jsonl").write_text(
            json.dumps({"id": self.task_id, "thread_name": "Source chat", "rollout_path": str(self.session)}) + "\n",
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "create table threads (id text primary key, rollout_path text not null, created_at integer not null, updated_at integer not null, "
            "source text not null, model_provider text not null, cwd text not null, title text not null, sandbox_policy text not null, "
            "approval_mode text not null, tokens_used integer not null default 0, has_user_event integer not null default 0, archived integer not null default 0, "
            "cli_version text not null default '', first_user_message text not null default '', memory_mode text not null default 'enabled', "
            "preview text not null default '', recency_at integer not null default 0, recency_at_ms integer not null default 0, history_mode text not null default 'legacy', "
            "is_pinned integer not null default 0, agent_nickname text, agent_path text)"
        )
        connection.execute(
            "insert into threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode,has_user_event,agent_nickname) "
            "values (?,?,1,1,'vscode','openai',?,'Source chat','{}','on-request',1,'Bohr')",
            (self.task_id, str(self.session), str(self.home)),
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_clones_selected_thread_to_target_agent(self):
        report = local_agent_handoff.handoff(
            self.home, "Bohr", "Hooke", "", {self.task_id}, require_codex_closed=False
        )
        self.assertEqual(report["imported"], 1)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        rows = connection.execute("select id,agent_nickname from threads order by id").fetchall()
        connection.close()
        self.assertEqual({nickname for _, nickname in rows}, {"Bohr", "Hooke"})
        self.assertEqual(len(planner.inventory(self.home, "test")["conversations"]), 2)


if __name__ == "__main__":
    unittest.main()
