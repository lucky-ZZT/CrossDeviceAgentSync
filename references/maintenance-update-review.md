# Maintainer Update Review

This workflow is for Codex maintaining CrossDeviceAgentSync. It is not an application feature and must not appear in the EXE UI.

## Reference Projects

- `Dailin521/codex-provider-sync`
- `CalebYcj/codex-rehome`

The reviewed commit for each project is stored in `scripts/upstream_update_checker.py`.

## Review Workflow

When the user asks Codex to check whether the project should be updated:

1. Run `scripts/upstream_update_checker.py` through its Python API to generate one report covering both reference projects.
2. Read each project's latest commit, latest Release, changed files, and commit messages from the report.
3. Fetch the actual upstream differences when the report is insufficient. Never execute upstream code merely to inspect it.
4. Decide whether the changes improve CrossDeviceAgentSync. The script's keyword recommendation is triage only, not an approval decision.
5. If no change is worth adopting, do not modify the product or rebuild the EXE solely to advance a version number.
6. If changes are worth adopting, modify the smallest relevant surface, run the complete test and validation suite, build one new EXE, and prepare a normal GitHub Release.
7. Report separately what each reference project changed, what was adopted, what was not adopted and why, user-visible effects, data-safety and compatibility effects, tests, version, EXE path, and SHA-256.
8. Advance `reviewed_commit` only for projects whose latest changes were successfully inspected. Never advance a failed check.

## Application Boundary

The EXE's **Check Updates** action checks only this project's latest stable GitHub Release. It does not inspect the reference projects, judge design value, generate a Codex handoff, download an EXE, or install an update.

Before the first public build, set `GITHUB_REPOSITORY` in `scripts/app_release_checker.py` to the final `owner/repository`. Do not publish a build with an empty repository setting.
