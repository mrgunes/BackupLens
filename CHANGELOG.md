# Changelog

All notable changes to BackupLens will be documented in this file.

## [1.0.1] - 2026-09-07

### Fixed
- Extract now decrypts files. `extract_file()` was called with `domain=` (invalid); the exception handler copied the still-encrypted hashed backup blob, so exported HEIC/JPG files would not open.
- Listing and extract no longer share a SQLite connection across threads. All backup I/O runs on one worker thread.
- Auto-detect no longer crashes on macOS when listing `MobileSync/Backup` is denied; the UI asks you to Browse instead.

### Docs
- macOS backup-folder permissions and Homebrew tkinter (`python-tk`)

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
