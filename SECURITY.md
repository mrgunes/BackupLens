# Security Policy

## Our Commitment

BackupLens is designed from the ground up with your privacy and security as the top priority. We understand that iPhone backups contain some of the most sensitive data on your devices — photos, messages, health data, passwords, and more.

## How BackupLens Protects You

### 100% Offline
- BackupLens makes **zero network connections**. None. Ever.
- No HTTP requests, no DNS lookups, no telemetry, no analytics.
- You can verify this yourself: disconnect from the internet and the app works identically.
- You can also verify by reading every line of the source code — it's intentionally small and auditable.

### No Data Storage
- Your backup password is **never saved** to disk, logs, or any file.
- The password is discarded from the UI immediately after successful decryption and is not retained when you close the app.
- BackupLens does not create any config files, temp files, or caches.

### No Tracking
- No usage analytics. No crash reporting. No fingerprinting.
- We don't know who you are, what you're extracting, or that you even use this tool.

### Open Source & Auditable
- The entire codebase is a single Python file — small enough to read in one sitting.
- We encourage security researchers to audit the code.
- All dependencies are listed in `requirements.txt` and are open source themselves.

## Your Backup Password

The password you enter into BackupLens is the **encryption password you set in iTunes or Finder** when you enabled encrypted backups. This is NOT your Apple ID password.

- If you don't remember your backup password, check your system keychain:
  - **macOS:** Keychain Access app, search for "iOS Backup"
  - **Windows:** The password is not stored in Windows Credential Manager by default
- BackupLens cannot recover or bypass your password. This is by design — it means no one else can either.

## Unencrypted Backups

Unencrypted backups need no password, so none is asked for or used. They are opened read-only (the backup folder is never written to, and no temporary copy of its index is made), and extracted files are copied out as they are.

## Supported Backup Sources

BackupLens only reads **local iPhone/iPad backups** created by:
- iTunes (Windows)
- Finder (macOS 10.15+)
- Apple Devices app (Windows 11)

It does **not** access iCloud backups, which are stored on Apple's servers.

## Reporting Security Issues

If you discover a security vulnerability in BackupLens, please report it responsibly:

1. **Do NOT open a public GitHub issue** for security vulnerabilities.
2. Use [GitHub's private security advisory feature](https://github.com/mrgunes/BackupLens/security/advisories/new) to report the issue.
3. Include steps to reproduce, the potential impact, and any suggested fixes.

We will respond within 48 hours and work with you to address the issue before any public disclosure.

## Dependencies

BackupLens depends on:
- **Python 3.8+** — [python.org](https://python.org)
- **tkinter** — Included with Python (standard library GUI toolkit)
- **iphone_backup_decrypt** — [GitHub](https://github.com/jsharkey13/iphone_backup_decrypt) — MIT licensed, open source library for decrypting iOS backups

We monitor our dependencies for known vulnerabilities.
