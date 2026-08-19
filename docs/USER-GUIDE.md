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

## Move Data to Another Computer

On the old computer:

1. Open **Transfer to another computer**.
2. Select **Export**.
3. Choose Codex conversations or an agent/custom directory.
4. Save the `.cdas.zip` package to a private transfer location.

On the new computer:

1. Close Codex.
2. Open **Transfer to another computer** and select **Import**.
3. Choose the package and target directory.
4. Click **Check migration package**. This is read-only and shows direct imports, identical skips, and conflicts.
5. Review every conflict. Same-ID conversations with different content stay on the new computer and the incoming conversation is added as a separate migrated branch.
6. Click **Start import** only after confirming the preview.
7. Verify imported conversations before deleting the package.

## Synchronize Custom Files

1. Open **Synchronize selected folders**.
2. Choose the two endpoint folders.
3. Scan differences.
4. Select individual files.
5. Start synchronization.

Existing conflicting files remain in place. The incoming copy receives a source and hash suffix.

## Keep Local Projects And Import An Old-Computer Project

On the old computer:

1. Open **Import an old-computer project** and choose **Export project on this computer**.
2. Select the project directory and save the package.
3. Keep **Include Git history** selected unless Git metadata must not leave the computer.
4. Leave sensitive data unselected unless the package is being transferred through a trusted private channel and the new computer needs it.

On the new computer:

1. Choose **Import project on this computer** and select the package.
2. Choose a parent directory for imported projects, normally `Documents\Imported Projects`.
3. Click **Check project package**. This creates no directory and does not inspect or change existing local projects.
4. Confirm the proposed unique `-from-old-computer` directory, then click **Start project import**.
5. Open the resulting directory in Codex or an IDE and install the project's dependencies as documented by that project.

The import refuses to run if the proposed new directory appears after checking. It does not merge Git histories or overwrite an existing project. Default export excludes `node_modules`, virtual environments, caches, build output, local databases, `.env` files, credentials, and private keys.

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
