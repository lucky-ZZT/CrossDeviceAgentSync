# Security Policy

## Supported Versions

Only the latest GitHub Release is supported with security fixes.

## Reporting A Vulnerability

Do not open a public issue containing exploit details, credentials, conversation data, migration packages, or unredacted logs. Use the repository owner's private GitHub contact or security-advisory channel after the repository is created.

Include the affected version, workflow, minimal reproduction, and expected impact. Replace all personal paths, Provider IDs, conversation IDs, tokens, and content with synthetic values.

## Release Trust

The application does not automatically download or install updates. Release maintainers must publish the SHA-256 of the tested EXE in the release notes. Users should verify the downloaded file before running it. The **Check Updates** action reads this project's latest stable GitHub Release metadata only and does not execute release code.
