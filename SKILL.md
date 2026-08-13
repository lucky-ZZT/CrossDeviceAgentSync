---
name: cross-device-agent-sync
description: Compare, selectively migrate, and safely reconcile named agent workspaces, Codex conversations, and custom files across agents and computers. Use when several local agents need shared state, when two computers contain different workspace versions, when the user wants to select files or chats to transfer, or when coordinating Codex ReHome and codex-provider-sync with a broader file synchronization workflow.
---

# Cross-Device Agent Sync

## Overview

Reconcile named endpoints without treating either side as an unconditional source of truth. An endpoint can be a local agent workspace, a Codex home, a directory on another computer, or a user-selected custom-file root. Inventory both sides, classify each item, obtain an explicit selection, migrate through verified packages, and preserve conflicts rather than silently overwriting them.

For Windows users, launch the latest `assets/CrossDeviceAgentSync-vX.Y.Z.exe`. The default screen offers Provider reassignment or cloning, computer transfer, selected-folder synchronization, backup recovery, and a read-only check for this project's latest GitHub Release. Technical settings remain available only under Advanced Mode. The EXE is portable and does not require a Python installation.

Use `scripts/generic_sync.py` for general endpoint snapshots, custom-file selection, packages, and conflict copies. Use Codex ReHome for project/Skill/Plugin-aware Codex migration. Use codex-provider-sync only for local provider/model visibility repair after restore. Use `scripts/session_merge_planner.py` and `scripts/migration_bundle.py` for Codex conversation-aware operations.

## Safety Rules

- Close Codex before packaging, restoring, or repairing local state.
- Never copy `auth.json`, cookies, `.env` files, private keys, browser login state, or runtime sockets.
- Never write directly to both computers in one operation. Finish and verify one target before changing the other.
- Never concatenate divergent JSONL session files.
- Preserve both histories when the same task ID has different non-prefix event tails.
- Treat an apparent fast-forward as a candidate until the common prefix and selected direction are confirmed.
- Back up the target before every restore. For local Provider reassignment, keep the full-data backup selected by default; allow an explicit no-backup choice only after warning that no later recovery will be available.
- Do not report success until session files, `session_index.jsonl`, SQLite thread rows, paths, and project registration are verified.

## Workflow

1. Read [architecture.md](references/architecture.md) before planning a reconciliation. For a normal Windows migration, use the bundled EXE instead of invoking the scripts manually.
2. Assign stable endpoint IDs. A local agent is represented by its workspace root plus an ID such as `agent-a`; another computer can use an ID such as `laptop-b`.
3. Use the generic endpoint page for local agent roots and custom files. Set include patterns such as `**/*`, `configs/*.json`, or `prompts/**`; default exclusions protect credentials, caches, dependencies, and private keys.
4. Compare the endpoints, deselect files or directories that should not move, and export a plan.
5. Create a generic `.cdas.zip` package from the selected side and transfer it privately. On the target, preview and restore it. Existing conflicting files remain in place; the incoming copy gets a `.from-<endpoint>-<hash>` suffix.
6. For Codex conversation-aware migration, create an inventory on each computer:

```powershell
python scripts/session_merge_planner.py inventory --codex-home "$env:USERPROFILE\.codex" --device-id desktop-a --output desktop-a.json
```

```bash
python3 scripts/session_merge_planner.py inventory --codex-home "$HOME/.codex" --device-id laptop-b --output laptop-b.json
```

7. Bring the two Codex inventory JSON files onto one computer and create a comparison plan:

```powershell
python scripts/session_merge_planner.py compare --left desktop-a.json --right laptop-b.json --direction bidirectional --output merge-plan.json
```

8. Present a selection table containing task ID, title, classification, last update on each side, and recommended action. Let the user select individual conversations or a project-scoped group. Use `--include`, `--include-file`, and `--exclude` to seal the selection into a new plan.
9. Read [upstream-integration.md](references/upstream-integration.md), then create a ReHome package containing only the selected source conversations and any explicitly selected projects, skills, plugins, or generated artifacts.
10. Preview the target restore. For an absent task, import normally. For a different existing task with the same ID, import as a branch unless a separately verified fast-forward procedure is available.
11. Apply and verify the first direction. If bidirectional reconciliation is selected, rescan both computers before planning the reverse direction. Do not reuse a stale comparison plan.
12. If restored conversations are hidden only because their provider/model metadata differs from the current Codex configuration, run codex-provider-sync status first, then its backup-first repair operation.
13. Save the final inventories, selected plan, package checksums, restore reports, and verification reports as the reconciliation checkpoint.

## Classification Policy

- `identical`: Skip.
- `metadata_equivalent`: Keep session content; optionally reconcile paths/provider metadata locally.
- `left_only` or `right_only`: Offer selective import to the missing side.
- `left_ahead` or `right_ahead`: Offer a fast-forward candidate. Default to branch import until the write path proves prefix-safe replacement.
- `diverged`: Keep both and import the remote history as a branch. Optionally create a new semantic handoff conversation after both branches are visible.
- `id_collision`: Treat as unrelated histories that reused an ID; keep both with a rewritten branch ID.
- `duplicate_local_id`: Stop automatic planning for that task and require inspection.

## Semantic Merge

When both branches contain useful work, do not rewrite their event logs into one thread. Create a new continuation with:

- both source task IDs and device labels
- the last shared event position
- decisions and artifacts unique to each branch
- unresolved conflicts
- the agreed project path and current source of truth

Keep the original branches immutable so the synthesis remains auditable.

## Resources

- `scripts/session_merge_planner.py`: Read-only inventory and comparison planner.
- `scripts/migration_bundle.py`: Checksummed selective package creation and backup-first three-layer restore.
- `scripts/generic_sync.py`: Generic endpoint snapshots, glob selection, package creation, and conflict-preserving restore.
- `scripts/cross_device_agent_sync_gui.py`: Tkinter desktop application source.
- `scripts/simple_sync_gui.py`: Default guided interface that hides endpoint IDs, directions, glob rules, and package internals.
- `scripts/app_release_checker.py`: Read-only comparison between the running version and this project's latest stable GitHub Release.
- `scripts/upstream_update_checker.py`: Maintainer-only reference-project report generator. Do not expose it through the EXE UI.
- `scripts/build_windows_exe.ps1`: Rebuild the Windows executable with PyInstaller.
- `references/architecture.md`: Conflict model, checkpoints, and bidirectional sequencing.
- `references/upstream-integration.md`: Responsibilities and boundaries of Codex ReHome and codex-provider-sync.
- `references/maintenance-update-review.md`: Codex-only process for deciding whether reference-project changes merit a new release.

## Windows EXE Usage

1. Choose **Synchronize two local agents**, **Transfer to another computer**, or **Synchronize selected folders**.
2. Local Provider synchronization needs only one Codex home. The EXE discovers Provider IDs from `config.toml`, rollout files, and SQLite, then offers two modes: **Reassign ownership** keeps the same conversation ID and path without creating a duplicate, while **Create copy** preserves the source and creates a separate target conversation.
3. For **Reassign ownership**, leave the full-data backup option selected when recovery is required. It copies the selected rollout files and SQLite state and can therefore approach the selected conversation size. Clearing it keeps only temporary rollback data during execution and leaves no restorable backup after success.
4. Select the source and destination locations, then use the check button before synchronizing.
5. Double-click a result only when it should be excluded or reselected.
6. For another computer, export on the old computer. On the new computer, first use **Check migration package**, review direct imports, identical skips, and branch/conflict copies, then use **Start import**. The preview must not write target data.
7. Use Advanced Mode only for explicit endpoint IDs, directions, include/exclude patterns, or manual plan files.
8. Use **Backup and Restore** to review timestamped Codex backups, open their folders, or restore a selected completed backup. Close Codex before recovery; the recovery operation first creates another safety backup so it can also be undone.
9. Use **Check Updates** only to compare the running version with CrossDeviceAgentSync's latest stable GitHub Release and read its release notes. The application never inspects reference projects or decides which design changes to adopt.
10. Obtain newer versions manually from GitHub Releases and verify the published SHA-256. Codex performs reference-project review separately under `references/maintenance-update-review.md` when the user requests it.

Application diagnostics are written as redacted JSON Lines to `%LOCALAPPDATA%\CrossDeviceAgentSync\logs\application.log`. Logs rotate at 5 MB with five retained files; conversation bodies and known credential fields are not logged.

The EXE directly handles generic files and selected conversations. Projects, Skills, Plugins, generated artifacts, and cross-OS path mapping remain delegated to Codex ReHome.
