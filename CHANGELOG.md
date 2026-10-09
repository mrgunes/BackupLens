# Changelog

All notable changes to BackupLens will be documented in this file.

## [Unreleased]

### Added
- **Unencrypted backups** can be opened, browsed and extracted without a password ([#4](https://github.com/mrgunes/BackupLens/issues/4)). The backup type is detected from `Manifest.plist`; the password box is disabled for unencrypted backups. They are opened read-only and `immutable`, so the backup folder is never modified
- Clear messages for folders that are not backups and for old `Manifest.mbdb` backups
- `tests/test_unencrypted_backups.py` (run with `python -m unittest discover -s tests -t .`)

## [1.0.0] - 2026-04-03

### Added
- Initial release
- Decrypt encrypted iPhone/iPad backups (iOS 13+)
- Browse files by category (Camera Roll, Apps, Health, Messages, etc.)
- Search across all files
- Extract individual files or bulk export
- Auto-detect backup location on Windows and macOS
- Cross-platform support (Windows, macOS, Linux)
- Path traversal protection during extraction
- Warm, trustworthy GUI with platform-native fonts
