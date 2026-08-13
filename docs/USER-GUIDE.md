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

## Restore a Backup

1. Close Codex completely.
2. Open **Backup and Restore**.
3. Select a completed, restorable backup.
4. Review its operation, size, item count, and full path.
5. Start restore.

The application creates another safety backup of the current state before restoring, so the restore itself can be reversed.

## Check Updates

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
