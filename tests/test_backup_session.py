"""Tests for the non-GUI core, run against a real (generated) encrypted
backup through the real ``iphone_backup_decrypt`` library."""

import inspect
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import backuplens as app
from tests import fixture_backup as fb

try:
    from iphone_backup_decrypt import EncryptedBackup
except ImportError:  # pragma: no cover
    EncryptedBackup = None


def read(path):
    with open(app.fs_path(path), "rb") as handle:
        return handle.read()


@unittest.skipIf(EncryptedBackup is None, "iphone_backup_decrypt not installed")
class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ibe-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.backup_dir, self.ids = fb.build_backup(self.tmp)
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)
        self.session = app.BackupSession()
        self.addCleanup(self._close_session)

    def _close_session(self):
        self.session.close()
        self.session._executor.shutdown(wait=True)

    def open(self, passphrase=fb.PASSPHRASE):
        return self.session.open(self.backup_dir, passphrase).result(60)

    def file_id(self, domain, rel_path):
        return self.ids[(domain, rel_path)]

    def extract(self, *keys):
        ids = [self.file_id(*k) for k in keys]
        return self.session.extract(ids, self.out).result(60)

    def out_path(self, domain, rel_path, windows=None):
        return app.build_output_path(self.out, domain, rel_path,
                                     windows=windows)


class OpenAndQueryTests(SessionTestCase):
    def test_open_lists_domains_and_file_count(self):
        domains, total = self.open()
        self.assertIn(fb.NOTES_DOMAIN, domains)
        self.assertEqual(total, len(fb.DEFAULT_FILES))  # folder excluded

    def test_wrong_password_is_recognised(self):
        error = self.session.open(self.backup_dir, "nope").exception(60)
        self.assertTrue(app.is_wrong_passphrase(error), repr(error))

    def test_failed_open_keeps_previous_backup_usable(self):
        self.open()
        self.assertIsNotNone(self.session.open(self.backup_dir, "nope")
                             .exception(60))
        rows, _ = self.session.query_files().result(60)
        self.assertTrue(rows)

    def test_queries_work_from_many_threads(self):
        """Regression: 'SQLite objects created in a thread can only be
        used in that same thread'."""
        self.open()
        errors = []

        def worker():
            try:
                for _ in range(5):
                    self.session.query_files(None, "txt").result(60)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_scope_search_and_truncation(self):
        self.open()
        rows, total = self.session.query_files().result(60)
        self.assertEqual((len(rows), total),
                         (len(fb.DEFAULT_FILES), len(fb.DEFAULT_FILES)))

        rows, total = self.session.query_files(None, "", 3).result(60)
        self.assertEqual((len(rows), total), (3, len(fb.DEFAULT_FILES)))

        rows, total = self.session.query_files(
            [fb.NOTES_DOMAIN]).result(60)
        self.assertEqual({r[1] for r in rows}, {fb.NOTES_DOMAIN})
        self.assertEqual(total, 2)

        self.assertEqual(self.session.query_files([]).result(60), ([], 0))

        rows, _ = self.session.query_files(None, "VOICE_MEMO_01").result(60)
        self.assertEqual([r[2] for r in rows], [fb.NOTES_MOV])

    def test_search_treats_like_wildcards_literally(self):
        self.open()
        rows, _ = self.session.query_files(None, "a_c").result(60)
        self.assertEqual({r[1] for r in rows}, {"AppDomain-com.a_c"})
        rows, _ = self.session.query_files(None, "%").result(60)
        self.assertEqual(rows, [])

    def test_rows_carry_size_and_date(self):
        self.open()
        rows, _ = self.session.query_files(
            None, "plain.txt").result(60)
        file_id, domain, rel_path, size, modified = rows[0]
        self.assertEqual(size, "20 B")
        self.assertRegex(modified, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_queries_need_an_open_backup(self):
        self.assertIsInstance(
            self.session.query_files().exception(60), RuntimeError)


class ExtractionTests(SessionTestCase):
    def setUp(self):
        super().setUp()
        self.open()

    def test_notes_recording_is_decrypted_not_raw(self):
        """Regression: extraction used to write the *encrypted* blob."""
        key = (fb.NOTES_DOMAIN, fb.NOTES_MOV)
        report = self.extract(key)
        self.assertEqual((report.extracted, report.errors, report.skipped),
                         (1, [], []))
        data = read(self.out_path(*key))
        self.assertEqual(data[4:8], b"ftyp")
        self.assertEqual(data, fb.content_of(*key))
        # And it is not just the encrypted file copied across.
        file_id = self.file_id(*key)
        self.assertNotEqual(
            data, read(os.path.join(self.backup_dir, file_id[:2], file_id)))

    def test_modification_time_is_restored(self):
        key = ("HomeDomain", "Library/Preferences/plain.txt")
        self.extract(key)
        self.assertEqual(int(os.path.getmtime(self.out_path(*key))),
                         1_700_000_000)

    def test_domains_differing_only_at_a_like_wildcard(self):
        a = ("AppDomain-com.a_c", "Documents/data.txt")
        b = ("AppDomain-com.a-c", "Documents/data.txt")
        report = self.extract(a, b)
        self.assertEqual((report.extracted, report.errors), (2, []))
        self.assertEqual(read(self.out_path(*a)), b"data of a_c")
        self.assertEqual(read(self.out_path(*b)), b"data of a-c")

    def test_empty_file(self):
        key = ("HomeDomain", "Library/empty.txt")
        report = self.extract(key)
        self.assertEqual((report.extracted, report.errors), (1, []))
        self.assertEqual(read(self.out_path(*key)), b"")

    def test_windows_hostile_name_is_sanitised(self):
        key = ("HomeDomain", "Library/Notes/what?:file*.txt")
        report = self.extract(key)
        self.assertEqual((report.extracted, report.errors), (1, []))
        data = read(self.out_path(*key))
        self.assertEqual(data, b"windows-hostile name")

    def test_path_over_260_characters(self):
        key = next((d, r) for d, r, _ in fb.DEFAULT_FILES
                   if r.endswith("deep.bin"))
        self.assertGreater(len(self.out_path(*key)), 260)
        report = self.extract(key)
        self.assertEqual((report.extracted, report.errors), (1, []))
        self.assertEqual(read(self.out_path(*key)), b"deep file")

    def test_missing_data_is_an_error_and_never_a_raw_copy(self):
        key = ("HomeDomain", "Library/missing.bin")
        report = self.extract(key)
        self.assertEqual(report.extracted, 0)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("missing.bin", report.errors[0][0])
        self.assertFalse(os.path.exists(self.out_path(*key)))

    def test_unknown_file_id_is_reported(self):
        report = self.session.extract(["0" * 40], self.out).result(60)
        self.assertEqual((report.extracted, len(report.errors)), (0, 1))

    def test_progress_and_cancel(self):
        calls = []
        ids = [self.file_id(d, r) for d, r, c in fb.DEFAULT_FILES if c]
        report = self.session.extract(
            ids, self.out, lambda n, total: calls.append((n, total))
        ).result(60)
        self.assertEqual(calls[-1], (len(ids), len(ids)))
        self.assertFalse(report.cancelled)

        self.session.cancel_extraction()
        # extract() re-arms cancellation, so cancel from the progress hook.
        report = self.session.extract(
            ids, self.out, lambda n, total: self.session.cancel_extraction()
        ).result(60)
        self.assertTrue(report.cancelled)
        self.assertLess(report.extracted, len(ids))


class CleanupTests(SessionTestCase):
    def test_close_removes_decrypted_manifest(self):
        made = []
        real_mkdtemp = tempfile.mkdtemp

        def spy(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            made.append(path)
            return path

        with mock.patch("tempfile.mkdtemp", spy):
            self.open()
        self.assertEqual(len(made), 1)
        self.assertTrue(os.path.exists(os.path.join(made[0], "Manifest.db")))
        self.session.close()
        self.session._executor.shutdown(wait=True)
        self.assertFalse(os.path.exists(made[0]))


@unittest.skipIf(EncryptedBackup is None, "iphone_backup_decrypt not installed")
class LibraryContractTests(unittest.TestCase):
    """The call this app makes must match the installed library's API."""

    def test_extract_file_keyword_is_domain_like(self):
        signature = inspect.signature(EncryptedBackup.extract_file)
        signature.bind(None, relative_path="p", domain_like="d",
                       output_filename="o")
        with self.assertRaises(TypeError):
            # What the app used to send; the TypeError was swallowed and
            # the raw encrypted file got copied out instead.
            signature.bind(None, relative_path="p", domain="d",
                           output_filename="o")

    def test_extract_files_accepts_a_filter_callback(self):
        parameters = inspect.signature(EncryptedBackup.extract_files)
        self.assertIn("filter_callback", parameters.parameters)


class HelperTests(unittest.TestCase):
    def test_escape_like(self):
        self.assertEqual(app.escape_like(r"50%_off\x"), r"50\%\_off\\x")

    def test_sanitize_component_windows(self):
        s = app.sanitize_component
        self.assertEqual(s('a<b>c:d"e|f?g*h', True), "a_b_c_d_e_f_g_h")
        self.assertEqual(s("CON", True), "_CON")
        self.assertEqual(s("nul.txt", True), "_nul.txt")
        self.assertEqual(s("name. ", True), "name._")
        self.assertEqual(s("", True), "_")
        self.assertEqual(s("what?:x", False), "what?:x")

    def test_build_output_path_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(app.UnsafePathError):
                app.build_output_path(dest, "HomeDomain", "../../evil")
            with self.assertRaises(app.UnsafePathError):
                app.build_output_path(dest, "HomeDomain", "a/../../evil")
            with self.assertRaises(app.UnsafePathError):
                app.build_output_path(dest, "HomeDomain", "")
            inside = app.build_output_path(dest, "HomeDomain", "/abs/x.txt")
            self.assertTrue(inside.startswith(os.path.realpath(dest)))

    def test_build_output_path_disambiguates_collisions(self):
        with tempfile.TemporaryDirectory() as dest:
            used = set()
            first = app.build_output_path(dest, "D", "Photo.JPG", "a" * 40,
                                          used)
            second = app.build_output_path(dest, "D", "Photo.JPG", "b" * 40,
                                           used)
            self.assertNotEqual(first, second)
            self.assertTrue(second.endswith("Photo_bbbbbbbb.JPG"))

    @unittest.skipUnless(os.name == "nt", "Windows long-path handling")
    def test_fs_path_extended_length_prefix(self):
        self.assertEqual(app.fs_path("C:\\short.txt"), "C:\\short.txt")
        long_path = "C:\\" + "x" * 300
        self.assertTrue(app.fs_path(long_path).startswith("\\\\?\\C:\\"))

    def test_read_file_info(self):
        blob = fb._file_record(1234, 1_700_000_000, 3)
        self.assertEqual(app.read_file_info(blob), (1234, 1_700_000_000))
        self.assertEqual(app.read_file_info(None), (None, None))
        self.assertEqual(app.read_file_info(b"garbage"), (None, None))

    def test_format_size(self):
        self.assertEqual(app.format_size(0), "0 B")
        self.assertEqual(app.format_size(1536), "1.5 KB")
        self.assertEqual(app.format_size(57010528), "54.4 MB")
        self.assertEqual(app.format_size(5 * 1024 ** 3), "5.00 GB")


if __name__ == "__main__":
    unittest.main()
