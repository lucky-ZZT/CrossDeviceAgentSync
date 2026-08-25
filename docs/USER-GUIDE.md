# User Guide

## Before You Start

- Keep the EXE in a writable folder owned by your Windows account.
- Close Codex completely before migration, Provider reassignment, import, or restore.
- Preserve the default full backup option unless disk space is insufficient and recovery is unnecessary.

## Reassign Conversations Between Providers

1. Open **Sync local Providers**.
2. Confirm the Codex data location, normally `%USERPROFILE%\.codex`.
3. Click **Auto-detect Provider**.
4. Choose the source and target Provider.
5. Click **Show source conversations**.
6. Select conversations using their checkbox or the bulk selection buttons.
7. Select **Reassign ownership** to keep the same conversation ID and avoid duplicates.
8. Click **Preflight check** and resolve every reported problem.
9. Close Codex and click **Start reassignment**.
10. Keep the completion report and backup path until the result is verified in Codex.

Use **Create copy** only when the original and target Provider must both retain separate conversations.

## Transfer A Project And Conversations To Another Computer

On the old computer:

1. Open **Transfer between two computers** and choose **Export on the old computer**.
2. Choose the project directory and old-computer Codex data location.
3. Select project files, related Codex conversations, or both.
4. Keep Git history selected unless it must not leave the computer. Sensitive files remain excluded by default.
5. Save the `.cdas.zip` package to a private transfer location.

On the new computer:

1. Close Codex.
2. Open **Transfer between two computers** and choose **Check and import on the new computer**.
3. Choose the package, destination project root, and new-computer Codex data location.
4. Check the package. The preview is read-only.
5. Choose whether to import project files, then select individual related conversations with the checkboxes and bulk selection controls.
6. Resolve the project prompt. An empty/non-overlapping destination maps directly. For overlap, prefer a renamed directory. Conversation-only import may reuse an existing same-directory project or merge duplicate registrations; project files are never merged into an existing directory.
7. Close Codex and start import. Structured project paths and SQLite `cwd` are mapped to the chosen directory; user-message text is not rewritten.
8. Restart Codex and verify the sidebar project and selected conversations. The tool writes an ordinary-path project registration offline and does not call `codex app`.

The first real destination import remains subject to this startup verification because the user elected not to perform a separate real-Codex offline-registration experiment. Import creates backups for project files, conversations, and global project state and rolls back completed layers if a later layer fails.

## Synchronize Custom Files

1. Open **Synchronize selected folders**.
2. Choose the two endpoint folders.
3. Scan differences.
4. Select individual files.
5. Start synchronization.

Existing conflicting files remain in place. The incoming copy receives a source and hash suffix.

## Manage Conversations, Projects, And Images

1. Open **Content Management**, confirm the Codex data directory, and click **Scan content**.
2. Use the **Project category** filter to show all conversations or only one normalized project path. The list shows both a short project name and its complete readable path; duplicate project names are disambiguated by path.
3. Review title, Provider, project directory, size, image usage, archive state, and possible same-title duplicates.
4. Select exactly one row and click **Preview conversation**, or double-click the row. The read-only window shows the original title when it differs, title source, task metadata, project, image usage, and a compact recent user/assistant transcript. System/developer instructions, raw image data, and large tool payloads are not shown.
5. Select active conversations and use **Archive selected** to move them into Codex's archive. Select archived conversations and use **Restore selected archive** to return them to `sessions/YYYY/MM/DD`. Close Codex first. Both operations back up the complete rollouts, SQLite database, and session index before updating all three layers together.
6. Treat the preview as evidence for a manual decision, not a guarantee that deletion is harmless. Delete selected conversations only after closing Codex. The application first backs up their rollout files, SQLite database, and session index.
7. Use the **Projects** tab to open directories, delete selected projects' related conversations, or move project directories to the managed project trash. Related conversations and project files are separate actions.
8. Use **Restore most recently removed project** to reverse the latest project move. If the original path has been reused, the project is restored to a uniquely named sibling directory.
9. Use the **Images** tab to review unique image size, repeated occurrences, total stored size, conversation, and image source. Preview images before selecting them.
10. **Select browser screenshots** selects images captured by the browser tool. Review each thumbnail and its impact level, close Codex, then prefer **Keep one and clean duplicate images**. Use **Back up and clean selected images completely** only when the images are confirmed unnecessary.

Image cleanup can either remove duplicate occurrences while retaining one copy, or remove every selected occurrence. The impact level is a heuristic, not a proof: user images and unique images are high risk because a future continuation may need their visual context; repeated browser screenshots are lower risk but still require review. The operation preserves surrounding text and events, does not delete temporary files or unrelated attachments, and copies every affected rollout in full to a restorable backup first.

### Repair Path Problems

1. In **Content Management**, click **Scan content** and open the consistency report.
2. Review conversation extended paths, project extended paths, same-directory duplicate projects, stale projects, and blocked unknown references separately.
3. Close Codex completely. Leaving a Codex window or background process open prevents repair.
4. Click **Repair path problems**. For each project, choose normalize, merge duplicate registration, remove stale registration, or leave unchanged; independently choose conversation-path and legacy-trigger cleanup.
5. Restart Codex and verify the project list.

The repair backs up `state_5.sqlite` and `.codex-global-state.json`. Duplicate project IDs are merged only when all references are recognized; project order, pins, selection, task ownership, project metadata, files, writable roots, and sidebar ordering are remapped to the keeper. Missing projects are removed only when they own no conversation or sidebar assignment. Unknown references are reported and left unchanged. The write is atomic, verified by re-reading the state, and restored from backup if verification fails.

## Restore a Backup

1. Close Codex completely.
2. Open **Backup and Restore**.
3. Select a completed, restorable backup.
4. Review its operation, size, item count, and full path.
5. If it contains multiple deleted conversations, select one in the lower conversation list and use **Restore specified conversation**. This avoids rolling back unrelated newer conversations.
6. Use **Restore full backup** only when you intentionally want to return the entire Codex data directory to that snapshot.
7. Start restore.

The conversation list also shows the backup time and the conversation's last activity. Type a title, task ID, or project path to filter the selected backup; use **Search all backups** to search every restorable conversation-delete backup. Double-click a result or choose **Preview selected** to read a bounded, read-only recent transcript before deciding which copy is current. Preview never restores or modifies data.

The application creates another safety backup of the current state before restoring, so the restore itself can be reversed.

## Repair Project Registration Paths

1. Open **Content Management**, select the Codex home, and scan while Codex is closed.
2. Choose **Repair path problems**. Each project registration ID appears on its own row with its exact path, path status, reference count, related conversations, recommendation, and current plan.
3. Select one row. The same action panel is always visible: inspect, keep, normalize, repoint, rename, remove registration, and fully delete project. An operation is disabled only when its verified preconditions fail; the exact reason is shown below the buttons.
4. For duplicate registrations, keep the normal-path row and remove the extended-path row when that matches the evidence. Known references from the removed ID are migrated to the retained ID.
5. For a missing-directory record, choose an existing directory to repoint it, rename it, keep it, or remove the registration. Normalization remains disabled when the normalized directory does not exist.
6. Ordinary registration removal preserves project files and conversations. Full-project deletion is a separate recoverable operation that backs up associated conversations, removes the registration, and moves project directories into the managed project trash.
7. Review the final concrete impact and execute.

The operation backs up the affected Codex state, rejects concurrent changes, writes atomically, verifies the result, and restores the backup on failure. Unknown project references block removal or merge instead of being guessed.

## Check Updates

1. Open **Check Updates** and click **Check for updates**.
2. When a newer version is found, click **Update Now**.
3. Confirm the download. The application verifies the downloaded EXE against `SHA256SUMS.txt`, then exits, replaces itself, and restarts.

The application never updates silently. If the current EXE folder is not writable, move the EXE to a writable folder and retry.

1. Open **Check Updates**.
2. Click **Check New Version**.
3. Compare the current version with the latest stable Release and read its notes.
4. When a newer version is available, open the Release page and download it manually.

The application only checks CrossDeviceAgentSync's own GitHub Releases. It does not inspect `codex-provider-sync` or `codex-rehome`, make design decisions, download files, install updates, or replace the running EXE.

Obtain reviewed application versions manually from GitHub Releases and verify the published SHA-256 before running them.

## Troubleshooting

Open the software log from the home screen. The primary log is:

```text
%LOCALAPPDATA%\CrossDeviceAgentSync\logs\application.log
```

When reporting a problem, include:

- application version
- operation attempted
- exact displayed error
- relevant redacted log lines
- whether Codex was fully closed
- whether a backup was enabled

Never publish conversation files, authentication files, tokens, or complete personal logs.
