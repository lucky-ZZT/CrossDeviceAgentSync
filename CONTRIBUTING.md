# Contributing

## Development Setup

Use Windows 10 or Windows 11 with Python 3.12 or later.

```powershell
py -m pip install pyinstaller
py -m unittest discover -s tests -v
```

## Change Requirements

- Keep data-changing operations backup-first and transactionally recoverable.
- Never weaken credential, secret, browser-state, or runtime-file exclusions.
- Add focused regression tests for every migration, restore, Provider, update-checker, or path-handling change.
- Do not test against a real user `.codex` directory. Use an isolated temporary Codex home.
- Keep the folder name, `SKILL.md` name, and `agents/openai.yaml` display name identical.
- Run the EXE `--self-test` after every release build.

## Pull Requests

Describe:

- the user-visible problem
- the safety and compatibility impact
- tests added or changed
- whether migration packages or backup formats changed
- manual verification performed

Do not include personal conversations, credentials, tokens, or unredacted logs in commits or issues.
