# Upstream Integration

## Codex ReHome

Use `CalebYcj/codex-rehome` or its advanced `codex-rehome-skill` as the content migration engine.

Responsibilities:

- discover Codex sessions, indexes, state databases, projects, skills, plugins, and generated artifacts
- create checksummed migration packages
- exclude secrets and unsafe filesystem entries
- rewrite cross-OS paths
- import missing sessions and same-ID conflicts as branches
- back up, restore, roll back, and verify the target
- register restored projects with Codex Desktop

Important boundary: current ReHome planning treats any same-ID content difference as a branch conflict. The local merge planner adds prefix classification and user selection before packaging, but should continue using branch import as the default write behavior.

## codex-provider-sync

Use `Dailin521/codex-provider-sync` after migration only when local provider/model metadata needs repair.

Responsibilities:

- inspect provider metadata in rollout files and SQLite state
- coordinate rollout, SQLite, config, and global-state writes
- acquire a local lock
- create managed backups
- record a durable transaction journal
- restore or expose explicit recovery requirements after failure

Important boundary: provider metadata repair does not migrate conversations and cannot make provider-specific encrypted content portable.

## Integration Order

1. Inventory and compare with this skill.
2. Obtain explicit selection and direction.
3. Package and restore with ReHome.
4. Verify files, index rows, paths, and project registration.
5. Inspect provider status.
6. Run provider repair only when necessary.
7. Verify again and save the checkpoint.

Do not run provider repair before the selected migration is verified; doing so makes failures harder to attribute and expands rollback scope.

