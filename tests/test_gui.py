"""End-to-end GUI test: the real window driving a real encrypted backup.

Skipped when no display is available (e.g. headless CI).
"""

import os
import shutil
import tempfile
import time
import tkinter as tk
import unittest
from concurrent.futures import Future
from unittest import mock

import backuplens as app
from tests import fixture_backup as fb

try:
    import iphone_backup_decrypt  # noqa: F401
except ImportError:  # pragma: no cover
    iphone_backup_decrypt = None


def _make_root():
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


@unittest.skipIf(iphone_backup_decrypt is None, "library not installed")
class GuiFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _make_root()
        if cls.root is None:
            raise unittest.SkipTest("no display available")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ibe-gui-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.backup_dir, self.ids = fb.build_backup(self.tmp)
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

        self.explorer = app.BackupLens(self.root)
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        if self.explorer._poll_job is not None:
            self.root.after_cancel(self.explorer._poll_job)
        self.explorer.session.close()
        self.explorer.session._executor.shutdown(wait=True)

    def wait_for(self, condition, what, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}; status="
                  f"{self.explorer.status_var.get()!r}")

    def open_backup(self, password=fb.PASSPHRASE):
        self.explorer.path_var.set(self.backup_dir)
        self.explorer.pass_var.set(password)
        self.explorer._decrypt()

    def test_wrong_password_shows_incorrect_password(self):
        self.open_backup("wrong")
        self.wait_for(lambda: self.dialogs, "error dialog")
        self.assertEqual(self.dialogs[0][0], "showerror")
        self.assertEqual(self.dialogs[0][1][0], "Decryption Failed")
        self.assertFalse(self.explorer.backup_open)

    def test_password_is_not_stripped(self):
        self.open_backup(" " + fb.PASSPHRASE)  # wrong: leading space
        self.wait_for(lambda: self.dialogs, "error dialog")
        self.assertEqual(self.dialogs[0][1][0], "Decryption Failed")

    def test_browse_search_and_extract(self):
        self.open_backup()
        self.wait_for(lambda: self.explorer.backup_open, "backup to open")
        self.assertEqual(self.explorer.pass_var.get(), "")  # cleared

        self.explorer.domain_tree.selection_set("__ALL__")
        self.wait_for(lambda: len(self.explorer.file_tree.get_children())
                      == len(fb.DEFAULT_FILES), "file list")

        # Search runs against the whole backup, not just what is loaded.
        self.explorer.search_var.set("voice_memo_01")
        self.wait_for(lambda: len(self.explorer.file_tree.get_children())
                      == 1, "search result")
        self.assertEqual(self.explorer.count_var.get(), "1 files")

        file_id = self.explorer.file_tree.get_children()[0]
        self.explorer.file_tree.selection_set(file_id)
        self.explorer._extract_selected()
        self.wait_for(lambda: not self.explorer._extracting
                      and self.dialogs, "extraction")
        self.assertEqual(self.dialogs[-1][0], "showinfo",
                         self.dialogs[-1])

        key = (fb.NOTES_DOMAIN, fb.NOTES_MOV)
        with open(app.fs_path(app.build_output_path(self.out, *key)),
                  "rb") as handle:
            data = handle.read()
        self.assertEqual(data, fb.content_of(*key))

    def test_category_selection(self):
        self.open_backup()
        self.wait_for(lambda: self.explorer.backup_open, "backup to open")
        self.explorer.domain_tree.selection_set("__CAT__Apps")
        self.wait_for(lambda: len(self.explorer.file_tree.get_children())
                      == 4, "apps category")  # notes x2 + a_c + a-c

    def test_load_error_shows_the_real_message(self):
        """Regression: a NameError about 'e' used to hide the real error."""
        self.open_backup()
        self.wait_for(lambda: self.explorer.backup_open, "backup to open")
        failed = Future()
        failed.set_exception(RuntimeError("the real problem"))
        with mock.patch.object(self.explorer.session, "query_files",
                               return_value=failed):
            self.explorer.domain_tree.selection_set("__ALL__")
            self.wait_for(
                lambda: "the real problem" in self.explorer.status_var.get(),
                "error status")
        self.assertTrue(
            self.explorer.status_var.get().startswith("Error loading:"))

    def _new_explorer(self):
        explorer = app.BackupLens(self.root)
        self.addCleanup(self._close_extra, explorer)
        return explorer

    def _close_extra(self, explorer):
        if explorer._poll_job is not None:
            self.root.after_cancel(explorer._poll_job)
        explorer.session.close()
        explorer.session._executor.shutdown(wait=True)

    def test_auto_detect_picks_newest_backup(self):
        base = os.path.join(self.tmp, "Backup")
        for name, age in (("old", 100), ("new", 0)):
            path = os.path.join(base, name)
            os.makedirs(path)
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        with mock.patch.object(app.platform, "system",
                               return_value="TestOS"), \
                mock.patch.dict(app.BackupLens.BACKUP_PATHS,
                                {"TestOS": [base]}):
            explorer = self._new_explorer()
        self.assertEqual(os.path.basename(explorer.path_var.get()), "new")

    def test_auto_detect_survives_a_blocked_backup_folder(self):
        """macOS raises PermissionError when listing MobileSync/Backup."""
        base = os.path.join(self.tmp, "Backup")
        os.makedirs(base)
        with mock.patch.object(app.platform, "system",
                               return_value="TestOS"), \
                mock.patch.dict(app.BackupLens.BACKUP_PATHS,
                                {"TestOS": [base]}), \
                mock.patch.object(app.os, "listdir",
                                  side_effect=PermissionError):
            explorer = self._new_explorer()
        self.assertEqual(explorer.path_var.get(), "")
        self.assertIn("Browse", explorer.status_var.get())

    def test_truncation_is_reported(self):
        self.open_backup()
        self.wait_for(lambda: self.explorer.backup_open, "backup to open")
        with mock.patch.object(app, "MAX_ROWS", 2):
            self.explorer.domain_tree.selection_set("__ALL__")
            self.wait_for(lambda: "Showing the first 2"
                          in self.explorer.status_var.get(), "truncation")
        self.assertEqual(self.explorer.count_var.get(),
                         f"2 of {len(fb.DEFAULT_FILES)} files")


if __name__ == "__main__":
    unittest.main()
