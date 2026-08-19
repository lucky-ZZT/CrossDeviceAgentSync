# CrossDeviceAgentSync

Portable Windows tool for selectively synchronizing Codex conversations, local Providers, agent workspaces, and custom files across agents and computers.

## Features

- Automatically discover local Codex Providers.
- Reassign selected conversations without creating duplicate histories.
- Clone selected conversations when both Provider copies are required.
- Export and import selected conversations between computers.
- Keep existing local projects and import an old-computer project into a separate new directory.
- Bulk-manage conversations, project associations, and embedded conversation images.
- Synchronize selected folders while preserving conflicts.
- Create complete, restorable backups before data changes.
- Run aggregate preflight checks before Provider writes.
- Keep rotating, redacted diagnostic logs.
- Check this project's latest stable GitHub Release.
- Display the latest version and release notes without automatically downloading or installing it.

## Download And Use

Download `CrossDeviceAgentSync-vX.Y.Z.exe` from [Releases](../../releases). No Python installation is required.

Close Codex before migrations, Provider writes, imports, or restores. For a cross-device import, first use **Check migration package** to review direct imports, identical skips, and preserved conflict copies; it does not write target data. For a project, use **Import an old-computer project**: the preview chooses a unique `project-from-old-computer` directory and the import never merges into or overwrites an existing local project. Git history is included by default, while dependencies, caches, build outputs, environment files, and credentials are excluded. When a newer application release is found, the user-confirmed updater downloads the EXE and SHA-256 manifest, verifies it, then replaces and restarts the application. Retain full backups whenever recovery may be required.

Cross-computer migration and old-project import remain separate choices. Export never deletes source data. After verifying the destination, use **Content Management** on the old computer to review possible duplicate conversations and projects. Conversations are grouped by normalized project path and can be filtered by readable project name; Windows device-path prefixes are hidden from display. A conversation can be opened in a read-only preview before deletion; UUIDs, internal labels, `Work in` wrappers, and reversible mojibake are replaced only in the displayed title, while the original title remains visible in the preview. Selected conversations can be archived or restored from the archive with coordinated rollout, SQLite, and index updates after a complete backup. Embedded images are grouped by content hash, can be previewed, and can be cleaned in bulk after a complete rollout backup. Conversations are deleted only after their rollouts, SQLite database, and index are backed up. Project folders move to a managed recoverable trash instead of being permanently deleted.

Image cleanup shows a heuristic impact level. User and unique images are high risk because a future continuation may need their visual context. Repeated browser screenshots are lower risk. Prefer **Keep one and clean duplicate images**; use complete removal only after reviewing the thumbnails and confirmation details.

See [docs/USER-GUIDE.md](docs/USER-GUIDE.md) for the complete workflow and safety rules.

## Updates

**Check Updates** compares the running version with this project's latest stable GitHub Release and displays its notes and assets. It never downloads, installs, or replaces the EXE. Download a newer release manually and verify its published SHA-256. Reference-project design review is a separate maintainer workflow performed by Codex when requested.

## Development

```powershell
py -m unittest discover -s tests -v
powershell -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1 -ApplicationName CrossDeviceAgentSync-v1.0.2
```

## Diagnostics

Logs are stored at `%LOCALAPPDATA%\CrossDeviceAgentSync\logs\application.log` and redact known conversation-content and credential fields.

## License

No license has been selected yet. Add one before making the repository public.
