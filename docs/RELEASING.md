# Release Process

1. Confirm `GITHUB_REPOSITORY` in `scripts/app_release_checker.py` is the final `owner/repository` and update `APP_VERSION` in both GUI entry points.
2. Update `SKILL.md` and the project design notes.
3. Run all unit tests, Python compilation, skill validation, and name validation.
4. Build the versioned EXE with `scripts/build_windows_exe.ps1`.
5. Run the EXE with `--self-test`.
6. Confirm `assets/SHA256SUMS.txt` matches the versioned EXE.
7. Create a GitHub Release tagged `vX.Y.Z`.
8. Upload the versioned EXE and publish its SHA-256 in the release notes.
9. Publish release notes using `.github/RELEASE_TEMPLATE.md`.
10. Download the Release asset once, verify its SHA-256, and run `--self-test` on the downloaded copy.

Do not publish an automatic-install manifest. The application button checks only this project's latest stable Release. Reference-project review and the decision to create a new version remain Codex maintainer responsibilities documented in `references/maintenance-update-review.md`.
