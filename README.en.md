# CrossDeviceAgentSync

Portable Windows tool for selectively synchronizing Codex conversations, local Providers, agent workspaces, and custom files across agents and computers.

## Features

- Automatically discover local Codex Providers.
- Reassign selected conversations without creating duplicate histories.
- Clone selected conversations when both Provider copies are required.
- Export and import selected conversations between computers.
- Synchronize selected folders while preserving conflicts.
- Create complete, restorable backups before data changes.
- Run aggregate preflight checks before Provider writes.
- Keep rotating, redacted diagnostic logs.
- Check this project's latest stable GitHub Release.
- Display the latest version and release notes without automatically downloading or installing it.

## Download And Use

Download `CrossDeviceAgentSync-vX.Y.Z.exe` from [Releases](../../releases). No Python installation is required.

Close Codex before migrations, Provider writes, imports, or restores. Run the preflight check first and retain full backups whenever recovery may be required.

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
