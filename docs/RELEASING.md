# Release Process

1. Confirm `GITHUB_REPOSITORY` in `scripts/app_release_checker.py` is the final `owner/repository` and update `APP_VERSION` in both GUI entry points.
2. Update `SKILL.md` and the project design notes.
3. Run all unit tests, Python compilation, skill validation, and name validation.
4. Build the versioned EXE with `scripts/build_windows_exe.ps1`.
5. Run the EXE with `--self-test`.
6. Confirm `assets/SHA256SUMS.txt` matches the versioned EXE.
7. Update `.github/RELEASE_NOTES.md` with adopted and rejected changes, impact, tests, version, and SHA-256.
8. Commit and push the release-ready source and artifacts.
9. Create and push tag `vX.Y.Z`. `.github/workflows/release.yml` validates the version and checksum, then creates the GitHub Release and uploads the EXE and `SHA256SUMS.txt`.
10. Download the published Release asset once, verify its SHA-256, and run `--self-test` on the downloaded copy.

Do not publish an automatic-install manifest. The application button checks only this project's latest stable Release. Reference-project review and the decision to create a new version remain Codex maintainer responsibilities documented in `references/maintenance-update-review.md`.
