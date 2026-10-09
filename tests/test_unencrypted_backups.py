"""Unencrypted backups (issue #4): detection, reading and the GUI flow.

Self-contained: builds a small unencrypted backup on the fly, so these tests
need neither a real backup nor iphone_backup_decrypt. The GUI tests skip
themselves when no display is available.

Run with:  python -m unittest discover -s tests -t .
"""

import hashlib
import os
import plistlib
import shutil
import sqlite3
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

import backuplens as app

MTIME = 1_700_000_000
FILES = [
    # (domain, relative_path, content)
    ("HomeDomain", "Library/Notes/note.txt", b"hello from the phone"),
    ("MediaDomain", "Media/DCIM/100APPLE/IMG_0001.HEIC", os.urandom(300_000)),
    ("AppDomain-com.a_c", "Documents/data.txt", b"data of a_c"),
    ("AppDomain-com.a-c", "Documents/data.txt", b"data of a-c"),
    ("HomeDomain", "Library/empty.txt", b""),
    ("HomeDomain", "Library/missing.bin", None),  # listed, data absent
]
IMG = ("MediaDomain", "Media/DCIM/100APPLE/IMG_0001.HEIC")


def file_id_for(domain, rel_path):
    return hashlib.sha1(f"{domain}-{rel_path}".encode()).hexdigest()


def file_record(size):
    """A Manifest.db `file` blob (NSKeyedArchiver-style) for a plain file."""
    return plistlib.dumps(
        {"$archiver": "NSKeyedArchiver",
         "$objects": ["$null", {"Size": size, "LastModified": MTIME,
                                "Mode": 0o100644, "ProtectionClass": 4}],
         "$top": {"root": plistlib.UID(1)}, "$version": 100000},
        fmt=plistlib.FMT_BINARY)


def build_plain_backup(parent):
    """Create an unencrypted backup folder inside *parent*; return its path."""
    backup = os.path.join(parent, "0" * 8 + "-" + "0" * 16)
    os.makedirs(backup)
    with open(os.path.join(backup, "Manifest.plist"), "wb") as handle:
        plistlib.dump({"IsEncrypted": False, "Version": "10.0"}, handle)
    conn = sqlite3.connect(os.path.join(backup, "Manifest.db"))
    conn.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                 "relativePath TEXT, flags INTEGER, file BLOB)")
    for domain, rel_path, content in FILES:
        file_id = file_id_for(domain, rel_path)
        if content:
            os.makedirs(os.path.join(backup, file_id[:2]), exist_ok=True)
            with open(os.path.join(backup, file_id[:2], file_id), "wb") as f:
                f.write(content)
        conn.execute("INSERT INTO Files VALUES (?, ?, ?, 1, ?)",
                     (file_id, domain, rel_path,
                      file_record(100 if content is None else len(content))))
    conn.execute("INSERT INTO Files VALUES (?, 'HomeDomain', 'Library', 2, ?)",
                 (file_id_for("HomeDomain", "Library"), file_record(0)))
    conn.commit()
    conn.close()
    return backup


def build_stub_encrypted_backup(parent):
    """Enough of an encrypted backup to be recognised as one."""
    backup = os.path.join(parent, "encrypted")
    os.makedirs(backup)
    with open(os.path.join(backup, "Manifest.plist"), "wb") as handle:
        plistlib.dump({"IsEncrypted": True}, handle)
    with open(os.path.join(backup, "Manifest.db"), "wb") as handle:
        handle.write(os.urandom(4096))
    return backup


def content_of(domain, rel_path):
    return next(c for d, r, c in FILES if (d, r) == (domain, rel_path))


def snapshot(root):
    state = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            info = os.stat(path)
            state[os.path.relpath(path, root)] = (info.st_size,
                                                  info.st_mtime_ns)
    return state


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backuplens-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_dir(self, **files):
        folder = tempfile.mkdtemp(dir=self.tmp)
        for name, content in files.items():
            with open(os.path.join(folder, name), "wb") as handle:
                handle.write(content)
        return folder


class DetectBackupTests(TempDirCase):
    def test_encrypted_and_unencrypted(self):
        plain = build_plain_backup(self.make_dir())
        stub = build_stub_encrypted_backup(self.make_dir())
        self.assertEqual(app.detect_backup(plain), app.UNENCRYPTED)
        self.assertEqual(app.detect_backup(stub), app.ENCRYPTED)

    def test_missing_flag_means_unencrypted(self):
        folder = self.make_dir(**{
            "Manifest.plist": plistlib.dumps({"Version": "10.0"}),
            "Manifest.db": b"x"})
        self.assertEqual(app.detect_backup(folder), app.UNENCRYPTED)

    def assertRejected(self, folder, *fragments):
        with self.assertRaises(app.BackupFormatError) as ctx:
            app.detect_backup(folder)
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))

    def test_folders_that_are_not_backups(self):
        self.assertRejected(self.make_dir(), "Manifest.plist")
        self.assertRejected(os.path.join(self.tmp, "nope"), "does not exist")
        self.assertRejected(self.make_dir(**{"Manifest.plist": b"\x00junk"}),
                            "could not be read")
        self.assertRejected(
            self.make_dir(**{"Manifest.plist": plistlib.dumps([1, 2])}),
            "expected format")
        self.assertRejected(
            self.make_dir(**{"Manifest.plist":
                             plistlib.dumps({"IsEncrypted": False})}),
            "no Manifest.db")

    def test_old_mbdb_backups_are_explained(self):
        self.assertRejected(self.make_dir(**{"Manifest.mbdb": b"x"}),
                            "Manifest.mbdb")
        self.assertRejected(
            self.make_dir(**{"Manifest.plist": plistlib.dumps({}),
                             "Manifest.mbdb": b"x"}),
            "Manifest.mbdb")


class PlainBackupTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.backup_dir = build_plain_backup(self.make_dir())
        self.backup = app.PlainBackup(backup_directory=self.backup_dir)
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)

    def extract(self, folder, rel_path, **kwargs):
        target = os.path.join(self.out, folder, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        self.backup.extract_file(relative_path=rel_path,
                                 output_filename=target, **kwargs)
        return target

    def read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def test_manifest_can_be_queried(self):
        with self.backup.manifest_db_cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM Files WHERE flags=1")
            self.assertEqual(cur.fetchone()[0], len(FILES))

    def test_manifest_is_usable_from_any_thread(self):
        """The app queries from several threads, unlike the encrypted case."""
        errors = []

        def worker():
            try:
                for _ in range(5):
                    with self.backup.manifest_db_cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM Files")
                        cur.fetchone()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_extracts_byte_for_byte_with_either_keyword(self):
        for kwargs in ({"domain": IMG[0]}, {"domain_like": IMG[0]}):
            path = self.extract(*IMG, **kwargs)
            self.assertEqual(self.read(path), content_of(*IMG))
        self.assertEqual(int(os.path.getmtime(path)), MTIME)

    def test_domains_differing_only_at_a_like_wildcard(self):
        for domain, data in (("AppDomain-com.a_c", b"data of a_c"),
                             ("AppDomain-com.a-c", b"data of a-c")):
            path = self.extract(domain, "Documents/data.txt", domain=domain)
            self.assertEqual(self.read(path), data)

    def test_empty_file(self):
        path = self.extract("HomeDomain", "Library/empty.txt",
                            domain="HomeDomain")
        self.assertEqual(self.read(path), b"")

    def test_missing_data_raises_and_leaves_no_debris(self):
        with self.assertRaises(FileNotFoundError):
            self.extract("HomeDomain", "Library/missing.bin",
                         domain="HomeDomain")
        folder = os.path.join(self.out, "HomeDomain", "Library")
        self.assertEqual(os.listdir(folder), [])

    def test_unknown_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.backup.extract_file(
                relative_path="nope", domain="HomeDomain",
                output_filename=os.path.join(self.out, "x"))

    def test_hostile_file_id_in_the_manifest_is_refused(self):
        conn = sqlite3.connect(os.path.join(self.backup_dir, "Manifest.db"))
        conn.execute("INSERT INTO Files VALUES ('../../escape', 'HomeDomain', "
                     "'Library/evil.txt', 1, NULL)")
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            self.backup.extract_file(
                relative_path="Library/evil.txt", domain="HomeDomain",
                output_filename=os.path.join(self.out, "evil.txt"))

    def test_the_backup_folder_is_never_modified(self):
        before = snapshot(self.backup_dir)
        with mock.patch("tempfile.mkdtemp") as mkdtemp:
            backup = app.PlainBackup(backup_directory=self.backup_dir)
            with backup.manifest_db_cursor() as cur:
                cur.execute("SELECT * FROM Files")
                cur.fetchall()
            self.extract(*IMG, domain=IMG[0])
        mkdtemp.assert_not_called()  # no temporary copy of the index
        self.assertEqual(snapshot(self.backup_dir), before)

    def test_unreadable_manifest_db_is_a_clear_error(self):
        with open(os.path.join(self.backup_dir, "Manifest.db"), "wb") as f:
            f.write(b"this is not a database" * 100)
        with self.assertRaises(app.BackupFormatError):
            app.PlainBackup(backup_directory=self.backup_dir)


class GuiTests(TempDirCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        super().setUp()
        self.plain = build_plain_backup(self.make_dir())
        self.encrypted = build_stub_encrypted_backup(self.make_dir())
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)

        self.dialogs = []
        for name in ("showerror", "showinfo", "showwarning"):
            patcher = mock.patch.object(
                app.messagebox, name,
                side_effect=lambda *a, _n=name, **k: self.dialogs.append(
                    (_n, a)))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(app.filedialog, "askdirectory",
                                    return_value=self.out)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.app = app.BackupLens(self.root)
        self.addCleanup(self._cancel_timer)

    def _cancel_timer(self):
        if self.app._detect_job is not None:
            self.root.after_cancel(self.app._detect_job)

    def run_until(self, condition, what, timeout=30):
        """Run the Tk main loop until *condition()* (worker threads report
        back through ``root.after``, which needs the loop to be running)."""
        deadline = time.monotonic() + timeout

        def poll():
            if condition() or time.monotonic() > deadline:
                self.root.quit()
            else:
                self.root.after(20, poll)

        self.root.after(20, poll)
        self.root.mainloop()
        self.assertTrue(condition(), f"timed out waiting for {what}; status="
                                     f"{self.app.status_var.get()!r}")

    def test_choosing_a_folder_adapts_the_form(self):
        self.app.path_var.set(self.plain)
        self.run_until(lambda: "not encrypted" in self.app.status_var.get(),
                       "unencrypted hint")
        self.assertEqual(self.app.decrypt_btn.cget("text"), "Open Backup")
        self.assertTrue(self.app.pass_entry.instate(["disabled"]))

        self.app.path_var.set(self.encrypted)
        self.run_until(lambda: "is encrypted" in self.app.status_var.get(),
                       "encrypted hint")
        self.assertEqual(self.app.decrypt_btn.cget("text"), "Decrypt & Open")
        self.assertFalse(self.app.pass_entry.instate(["disabled"]))

    def test_open_browse_and_extract_without_a_password(self):
        a = self.app
        a.path_var.set(self.plain)
        a._decrypt()  # no password typed
        self.run_until(lambda: a.domain_tree.get_children(), "backup to open")
        self.assertEqual(self.dialogs, [])
        self.assertTrue(a.status_var.get().startswith("Opened!"),
                        a.status_var.get())

        a.domain_tree.selection_set("__ALL__")
        self.run_until(lambda: len(a.file_tree.get_children()) == len(FILES),
                       "file list")

        file_id = file_id_for(*IMG)
        a.file_tree.selection_set(file_id)
        a._extract_selected()
        self.run_until(lambda: self.dialogs, "extraction")
        self.assertEqual(self.dialogs[-1][0], "showinfo", self.dialogs[-1])
        with open(os.path.join(self.out, *IMG[0:1], *IMG[1].split("/")),
                  "rb") as handle:
            self.assertEqual(handle.read(), content_of(*IMG))

    def test_encrypted_backup_without_a_password_asks_for_one(self):
        self.app.path_var.set(self.encrypted)
        self.app._decrypt()
        self.assertEqual(self.dialogs[0][0], "showerror")
        self.assertIn("encrypted", self.dialogs[0][1][1])

    def test_a_folder_that_is_not_a_backup_is_explained(self):
        self.app.path_var.set(self.make_dir())
        self.app._decrypt()
        self.assertEqual(self.dialogs[0][1][0], "Not a usable backup")
        self.assertIn("Manifest.plist", self.dialogs[0][1][1])


if __name__ == "__main__":
    unittest.main()
