"""
Read-only FUSE view over an opened iOS backup.

Presents the backup as a <domain>/<relativePath> tree — the same layout
`backuplens.py`'s full extraction produces — but without copying the whole
backup up front. Each file is decrypted/copied into a local cache directory
the first time something opens it, then served from that cached copy.

Linux/macOS only (needs libfuse + the `fusepy` package).
"""

from __future__ import annotations

import errno
import os
import plistlib
import stat
import threading

from fuse import FuseOSError, FUSE, Operations


class _Dir:
    __slots__ = ("children",)

    def __init__(self):
        self.children = {}


class _FileEntry:
    __slots__ = ("file_id", "domain", "relative_path", "size", "mtime")

    def __init__(self, file_id, domain, relative_path, size, mtime):
        self.file_id = file_id
        self.domain = domain
        self.relative_path = relative_path
        self.size = size
        self.mtime = mtime


def _parse_size_mtime(file_blob):
    size = 0
    mtime = None
    if file_blob:
        try:
            meta = plistlib.loads(file_blob)
            objects = meta.get("$objects", [])
            if isinstance(objects, list) and len(objects) > 1:
                obj1 = objects[1]
                if isinstance(obj1, dict):
                    size = obj1.get("Size", 0) or 0
            for obj in (objects if isinstance(objects, list) else []):
                if isinstance(obj, dict) and "LastModified" in obj:
                    mtime = obj["LastModified"]
                    break
        except Exception:
            pass
    return size, mtime


class BackupFS(Operations):
    """`backup` must expose `manifest_db_cursor()` and `extract_file(...)`,
    matching both `backuplens.PlainBackup` and
    `iphone_backup_decrypt.EncryptedBackup`.
    """

    def __init__(self, backup, cache_dir, mount_time):
        self.backup = backup
        self.cache_dir = cache_dir
        self.mount_time = mount_time
        self.root = _Dir()
        self._file_locks_guard = threading.Lock()
        self._file_locks = {}
        self._build_tree()

    def _build_tree(self):
        with self.backup.manifest_db_cursor() as cur:
            cur.execute(
                "SELECT fileID, domain, relativePath, file FROM Files "
                "WHERE flags=1 AND relativePath IS NOT NULL "
                "AND relativePath != ''"
            )
            rows = cur.fetchall()

        for file_id, domain, rel_path, file_blob in rows:
            if not domain or not rel_path:
                continue
            size, mtime = _parse_size_mtime(file_blob)
            parts = [domain] + [p for p in rel_path.split("/") if p]
            node = self.root
            collided = False
            for part in parts[:-1]:
                child = node.children.setdefault(part, _Dir())
                if not isinstance(child, _Dir):
                    # A file and a directory both claim this name — keep
                    # whichever showed up first and drop this entry.
                    collided = True
                    break
                node = child
            if not collided:
                node.children[parts[-1]] = _FileEntry(
                    file_id, domain, rel_path, size, mtime
                )

    def _lookup(self, path):
        if path in ("/", ""):
            return self.root
        node = self.root
        for part in path.strip("/").split("/"):
            if not isinstance(node, _Dir) or part not in node.children:
                return None
            node = node.children[part]
        return node

    def _ensure_cached(self, entry):
        cache_path = os.path.join(self.cache_dir, entry.file_id)
        if os.path.exists(cache_path):
            return cache_path
        with self._file_locks_guard:
            lock = self._file_locks.setdefault(entry.file_id, threading.Lock())
        with lock:
            if not os.path.exists(cache_path):
                tmp_path = f"{cache_path}.part"
                self.backup.extract_file(
                    relative_path=entry.relative_path,
                    domain=entry.domain,
                    output_filename=tmp_path,
                )
                os.replace(tmp_path, cache_path)
        return cache_path

    # ── FUSE operations ──────────────────────────────────────

    def getattr(self, path, fh=None):
        node = self._lookup(path)
        if node is None:
            raise FuseOSError(errno.ENOENT)
        if isinstance(node, _Dir):
            return {
                "st_mode": stat.S_IFDIR | 0o500,
                "st_nlink": 2,
                "st_size": 0,
                "st_ctime": self.mount_time,
                "st_mtime": self.mount_time,
                "st_atime": self.mount_time,
            }
        mtime = node.mtime if node.mtime is not None else self.mount_time
        return {
            "st_mode": stat.S_IFREG | 0o400,
            "st_nlink": 1,
            "st_size": node.size,
            "st_ctime": mtime,
            "st_mtime": mtime,
            "st_atime": mtime,
        }

    def readdir(self, path, fh):
        node = self._lookup(path)
        if not isinstance(node, _Dir):
            raise FuseOSError(errno.ENOTDIR)
        return ["." , ".."] + list(node.children.keys())

    def open(self, path, flags):
        node = self._lookup(path)
        if node is None or isinstance(node, _Dir):
            raise FuseOSError(errno.ENOENT)
        if (flags & os.O_ACCMODE) != os.O_RDONLY:
            raise FuseOSError(errno.EROFS)
        try:
            cache_path = self._ensure_cached(node)
        except FileNotFoundError:
            raise FuseOSError(errno.ENOENT)
        except Exception:
            raise FuseOSError(errno.EIO)
        return os.open(cache_path, os.O_RDONLY)

    def read(self, path, size, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, size)

    def release(self, path, fh):
        os.close(fh)
        return 0

    def statfs(self, path):
        st = os.statvfs(self.cache_dir)
        return {
            key: getattr(st, key)
            for key in (
                "f_bavail", "f_bfree", "f_blocks", "f_bsize", "f_favail",
                "f_ffree", "f_files", "f_flag", "f_frsize", "f_namemax",
            )
        }

    # Read-only filesystem — refuse anything that would mutate the backup.
    def write(self, path, data, offset, fh):
        raise FuseOSError(errno.EROFS)

    def create(self, path, mode, fi=None):
        raise FuseOSError(errno.EROFS)

    def unlink(self, path):
        raise FuseOSError(errno.EROFS)

    def mkdir(self, path, mode):
        raise FuseOSError(errno.EROFS)

    def rmdir(self, path):
        raise FuseOSError(errno.EROFS)

    def truncate(self, path, length, fh=None):
        raise FuseOSError(errno.EROFS)

    def chmod(self, path, mode):
        raise FuseOSError(errno.EROFS)

    def chown(self, path, uid, gid):
        raise FuseOSError(errno.EROFS)

    def rename(self, old, new):
        raise FuseOSError(errno.EROFS)


def mount(backup, mountpoint, cache_dir, mount_time, foreground=True):
    """Blocking call — mount `backup` at `mountpoint`.

    Meant to be run on a background thread. Returns once the filesystem is
    unmounted (e.g. via `fusermount -u`/`umount` on `mountpoint`).
    """
    fs = BackupFS(backup, cache_dir, mount_time)
    FUSE(fs, mountpoint, nothreads=False, foreground=foreground, ro=True)
