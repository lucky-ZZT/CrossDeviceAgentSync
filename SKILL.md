---
name: cross-device-agent-sync
description: Compare, selectively migrate, and safely reconcile named agent workspaces, Codex conversations, and custom files across agents and computers. Use when several local agents need shared state, when two computers contain different workspace versions, when the user wants to select files or chats to transfer, or when coordinating Codex ReHome and codex-provider-sync with a broader file synchronization workflow.
---

# Cross-Device Agent Sync

## Overview

Reconcile named endpoints without treating either side as an unconditional source of truth. An endpoint can be a local agent workspace, a Codex home, a directory on another computer, or a user-selected custom-file root. Inventory both sides, classify each item, obtain an explicit selection, migrate through verified packages, and preserve conflicts rather than silently overwriting them.

For Windows users, launch the latest `assets/CrossDeviceAgentSync-vX.Y.Z.exe`. The default screen offers Provider-isolated sidebar switching, selective conversation reassignment or cloning, computer transfer, selected-folder synchronization, backup recovery, and a read-only check for this project's latest GitHub Release. Technical settings remain available only under Advanced Mode. The EXE is portable and does not require a Python installation.

Use `scripts/generic_sync.py` for general endpoint snapshots, custom-file selection, packages, and conflict copies. Use Codex ReHome for project/Skill/Plugin-aware Codex migration. Use codex-provider-sync only for local provider/model visibility repair after restore. Use `scripts/session_merge_planner.py` and `scripts/migration_bundle.py` for Codex conversation-aware operations.

## Safety Rules

- Close Codex before packaging, restoring, or repairing local state.
- Never copy `auth.json`, cookies, `.env` files, private keys, browser login state, or runtime sockets.
- Never write directly to both computers in one operation. Finish and verify one target before changing the other.
- Never concatenate divergent JSONL session files.
- Preserve both histories when the same task ID has different non-prefix event tails.
- Treat an apparent fast-forward as a candidate until the common prefix and selected direction are confirmed.
- Back up the target before every restore. For local Provider synchronization, keep the full-data backup selected by default; allow an explicit no-backup choice only after warning that no later recovery will be available.
- Treat Codex's physical catalog as shared, then enforce the product's Provider view explicitly: the normal sidebar must contain only active conversations owned by the selected Provider.
- Never permanently delete another Provider's conversation merely to hide it. Move it to managed archive storage, remove its live catalog row, and restore it only when its owning Provider becomes active.
- Preserve user-created archive state separately from Provider-managed hiding. Activating a Provider must not unarchive conversations the user archived manually.
- Keep one authoritative Provider owner per conversation. Selected reassignment changes that owner; cloning creates a new conversation ID and therefore a separate future history.
- Do not report success until session files, `session_index.jsonl`, SQLite thread rows, paths, and project registration are verified. When the current `threads` schema contains `archived_at`, a healthy active task requires `archived=0`, `archived_at=NULL`, and a rollout under `sessions`; a healthy archived task requires `archived=1`, a non-null `archived_at`, and a rollout under `archived_sessions`. Treat every other combination as an actionable consistency error.
- Treat Windows `\\?\C:\...` and `\\?\UNC\...` rollout or local-project paths as a compatibility condition, not an archive flag. Report conversation and project registry paths separately during content scans. Repair rollout paths only when they normalize to an existing file inside the selected Codex home. Repair project registry paths only when the normalized project directory exists; a missing project may be removed as stale only when the thread database is available and no conversation still references it. Back up every affected Codex database or global-state file first, require Codex to be fully closed, and leave unverifiable paths unchanged. Do not install permanent triggers in Codex-owned databases. Remove only this tool's two known legacy normalization triggers after explicit user confirmation and record every repaired entry.

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
- `scripts/migration_bundle.py`: Checksummed selective package creation, backup-first three-layer restore, and single-conversation recovery from a completed conversation-delete backup.
- `scripts/generic_sync.py`: Generic endpoint snapshots, glob selection, package creation, and conflict-preserving restore.
- `scripts/project_import.py`: Project package creation and safe new-directory import that preserves existing local projects.
- `scripts/project_registry.py`: Backup-first offline normal-path project registration, project-ID reuse, and duplicate registry merging.
- `scripts/computer_transfer.py`: Combined old-computer package for project files plus related selectable conversations.
- `scripts/content_manager.py`: Streaming conversation/project/image inventory, rollout-path health inspection and backup-first repair, Codex sidebar/catalog reconciliation, duplicate-candidate grouping, recoverable conversation deletion, image cleanup, and project trash restore.
- `scripts/cross_device_agent_sync_gui.py`: Tkinter desktop application source.
- `scripts/simple_sync_gui.py`: Default guided interface that hides endpoint IDs, directions, glob rules, and package internals.
- `scripts/app_release_checker.py`: Read-only comparison between the running version and this project's latest stable GitHub Release.
- `scripts/upstream_update_checker.py`: Maintainer-only reference-project report generator. Do not expose it through the EXE UI.
- `scripts/build_windows_exe.ps1`: Rebuild the Windows executable with PyInstaller.
- `references/architecture.md`: Conflict model, checkpoints, and bidirectional sequencing.
- `references/upstream-integration.md`: Responsibilities and boundaries of Codex ReHome and codex-provider-sync.
- `references/maintenance-update-review.md`: Codex-only process for deciding whether reference-project changes merit a new release.

## Windows EXE Usage

1. Choose **Synchronize two local agents**, **Transfer between two computers**, or **Synchronize selected folders**.
2. Local Provider synchronization needs only one Codex home. The conversation table always shows the current Provider owner and a visibility state: **正在显示**, **Provider 隐藏**, **手动隐藏**, **用户归档**, or **状态异常**. The user can select conversations and operate **显示所选** or **隐藏所选** without changing ownership.
3. **切换所选归属** changes `model_provider`; with **切换归属后自动更新侧栏状态** enabled, the target Provider becomes the active sidebar, the source Provider's remaining conversations are managed-hidden and removed from `local_thread_catalog`, and target-owned managed-hidden conversations are restored. User-archived conversations are never restored automatically. The user can disable this option, but the UI warns that ownership and sidebar visibility may then diverge. **Create selected copies** gives the target a new task ID; warn that the source and copy can diverge and are no longer one history.
4. Leave the full-data backup option selected when independent recovery is required. Clearing it retains only temporary rollback data until the operation succeeds. Display, hide, reassignment, and provider-managed catalog cleanup update rollout locations, SQLite `archived` and `archived_at` state, compatibility index paths, the sidebar catalog, and the visibility ledger transactionally. Provider activation may restore only tasks recorded as Provider-managed hidden; it must preserve user-created archives.
5. Select the source and destination locations, then use the check button before synchronizing.
6. Conversation selection applies to reassignment and advanced cloning. Sidebar display switching always evaluates the complete registered conversation set.
7. For another computer, export one combined package containing project files, related Codex conversations, or both. On the new computer, first check the package, choose whether to import project files, select individual conversations, and resolve each project conflict. The preview must not write target data.
8. Use Advanced Mode only for explicit endpoint IDs, directions, include/exclude patterns, or manual plan files.
9. Use **Backup and Restore** to review timestamped Codex backups. The conversation table shows backup time and last activity, supports title/task-ID/project-path search across all restorable conversation-delete backups, and provides a read-only preview of the selected backup rollout before recovery. Open a folder, restore one selected conversation, or restore a complete snapshot only when an intentional full rollback is required. Close Codex before recovery; every recovery operation first creates another safety backup so it can also be undone. Prefer single-conversation recovery when only one task was deleted.
10. Use **Check Updates** only to compare the running version with CrossDeviceAgentSync's latest stable GitHub Release and read its release notes. The application never inspects reference projects or decides which design changes to adopt.
11. When a newer version is found, **Update Now** downloads the versioned EXE and `SHA256SUMS.txt`, verifies the SHA-256, then exits, replaces, and restarts the application. Codex performs reference-project review separately under `references/maintenance-update-review.md` when the user requests it.
12. When the destination is empty or has no overlap, map the project directly below the selected project root. When a directory or project name overlaps, offer a renamed `-from-old-computer` directory; when the same directory already has a registration, allow conversation-only reuse or duplicate-registration merge without merging project files. Complex project-file histories are never auto-merged. Imported structured rollout path fields and SQLite `cwd` are rewritten from the old project root to the selected new root; user message text is not rewritten.
13. Project registration is performed offline while Codex is closed and writes an ordinary absolute path directly to `.codex-global-state.json`; do not call `codex app <path>`. Back up and hash-check project state, update project order and selected task ownership, atomically write, and verify the new/merged project ID. Export never deletes the source. After destination verification, use Content Management on the old computer to select obsolete copies.
14. In **Content Management**, scan before every operation. Merge projects inferred from conversation `cwd` values with `.codex-global-state.json` sidebar registrations by normalized path. Keep registered projects visible in both the project table and project filter even when they have zero conversations, and label their registration, directory, and no-chat status explicitly. Group conversations by normalized project path, show a readable project name, and use the project filter to limit visible selections without changing data. Double-click one conversation or use **Preview conversation** to inspect a read-only excerpt before deletion. Treat `local_thread_catalog.display_title` as the authoritative Codex sidebar name. Preserve UUIDs, `Work in` wrappers, hyphenated names, and user renames; apply only reversible mojibake repair for display, and mark unrecoverable suspicious text without changing it. If the sidebar catalog has no title, fall back to the stored thread or session title, never the first request or project name. Show the first request only inside the preview. Archive and unarchive selected conversations by moving rollouts between `sessions/YYYY/MM/DD` and `archived_sessions`, updating `archived`, setting or clearing `archived_at`, and updating existing compatibility index paths. Detect partial archive states where the SQLite flag, archive timestamp, and rollout directory disagree, label them as inconsistent, keep them visible even when ordinary archived content is hidden, and allow either archive or unarchive to reconcile every layer. Match Codex's current authoritative-removal behavior by deleting the sidebar-catalog row and incrementing `catalog_revision` when a visible row is removed; never use `missing_candidate` as an archive flag. After unarchive, let Codex rebuild the full catalog row through its observer on the next start instead of fabricating version-specific catalog fields. Back up the rollout files, SQLite, sidebar database, and session index before either operation. Treat same-title conversations and `-from-old-computer` project names only as duplicate candidates; never auto-delete them. Conversation deletion uses the same complete backup layers and its final confirmation must list the selected titles and projects, not only a count. Image cleanup groups identical embedded images by SHA-256, shows repeated occurrence count and a heuristic impact level, and backs up every affected rollout before rewriting it. Prefer **Keep one and clean duplicate images**; reserve **Clean images completely** for a manually reviewed selection. Project removal moves directories to the managed project trash and provides restore instead of permanent deletion.
15. The Content Management scan also reports extended Windows rollout paths, extended project registry paths, same-directory duplicate project records, stale project registrations, and the two legacy normalization triggers previously installed during incident repair. **Repair path problems** lists each project registration ID as a separate row with its full path, verified path status, known-reference count, related-conversation count, recommendation, and planned action. Every row uses the same operation panel: inspect, keep, normalize, repoint to an existing directory, rename the sidebar entry, remove that registration, or fully delete the project. Enable or disable each operation only from explicit evidence. For example, normalization requires an extended path whose normalized directory exists; registration removal requires no unknown references; full deletion additionally requires a readable thread database, complete safe rollout enumeration, a non-protected existing directory, and no other registration sharing that directory. Show the exact disabled reason. A duplicate-row removal migrates known references to a retained row; full deletion backs up and deletes associated conversations, removes registration metadata, and moves project files into the recoverable project trash. Require Codex closed, rescan before mutation, back up all affected state, write atomically where applicable, verify, and roll back on failure.

Application diagnostics are written as redacted JSON Lines to `%LOCALAPPDATA%\CrossDeviceAgentSync\logs\application.log`. Logs rotate at 5 MB with five retained files; conversation bodies and known credential fields are not logged.

The EXE directly handles generic files, selected conversations, safe project copy-import, and recoverable local content management. Skills, Plugins, generated artifacts, cross-OS path mapping, and Git-history merging remain delegated to Codex ReHome.
