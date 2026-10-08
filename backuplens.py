"""
BackupLens — Your data. Your eyes only.

A free, open-source GUI tool to decrypt, browse, and extract files
from encrypted iPhone/iPad backups made by iTunes or Finder.

100% offline. No tracking. No telemetry. No network calls.
Your backup password never leaves your machine.

https://github.com/mrgunes/BackupLens
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import concurrent.futures
import queue
import sys
import threading
import traceback
import time
import os
import plistlib
import platform
import re
from datetime import datetime

__version__ = "1.0.0"
APP_NAME = "BackupLens"

# The file list shows at most this many rows. The status bar says so when
# a query matched more, and the search box queries the whole backup.
MAX_ROWS = 10000

# Rows per "domain IN (...)" query; SQLite builds limit bound variables.
_DOMAIN_CHUNK = 500


# ── Helpers (no GUI) ─────────────────────────────────────────

def format_size(size):
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.2f} GB"


def escape_like(text):
    """Escape SQL LIKE wildcards so *text* matches literally (ESCAPE '\\')."""
    return (text.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_"))


def read_file_info(blob):
    """Return ``(size, mtime)`` from a Manifest.db ``file`` blob.

    Either value is ``None`` when it cannot be read.
    """
    if not blob:
        return None, None
    try:
        meta = plistlib.loads(blob)
    except Exception:
        return None, None
    objects = meta.get("$objects") if isinstance(meta, dict) else None
    if not isinstance(objects, list):
        return None, None

    info = None
    try:
        info = objects[meta["$top"]["root"].data]
    except (KeyError, IndexError, AttributeError, TypeError):
        pass
    if not isinstance(info, dict):
        info = objects[1] if len(objects) > 1 else None
    if not isinstance(info, dict):
        return None, None

    size = info.get("Size")
    mtime = info.get("LastModified")
    return (size if isinstance(size, int) else None,
            mtime if isinstance(mtime, (int, float)) else None)


_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE
)


class UnsafePathError(ValueError):
    """A backup path that cannot be written safely under the output folder."""


def sanitize_component(part, windows=None):
    """Make one path component valid on the host file system."""
    if windows is None:
        windows = os.name == "nt"
    part = part.replace("\x00", "_")
    if not windows:
        return part
    part = _WINDOWS_ILLEGAL.sub("_", part)
    if _WINDOWS_RESERVED.match(part.split(".")[0]):
        part = "_" + part
    if part.endswith((".", " ")):
        part = part[:-1] + "_"
    return part or "_"


def build_output_path(dest, domain, relative_path, file_id="", used=None,
                      windows=None):
    """Return where a backup file is written: ``dest/domain/relative_path``.

    Raises UnsafePathError if the path would escape *dest*. Names that
    collide with one already written in this run (*used*, case-insensitive)
    get the file ID appended instead of overwriting each other.
    """
    parts = relative_path.split("/")
    if ".." in parts:
        raise UnsafePathError("path contains '..'")
    parts = [p for p in parts if p not in ("", ".")]
    if not parts:
        raise UnsafePathError("empty path")
    parts = [sanitize_component(domain, windows)] + [
        sanitize_component(p, windows) for p in parts
    ]

    real_dest = os.path.realpath(dest)
    out_path = os.path.join(real_dest, *parts)
    real_out = os.path.realpath(out_path)
    try:
        inside = os.path.commonpath(
            [os.path.normcase(real_dest), os.path.normcase(real_out)]
        ) == os.path.normcase(real_dest)
    except ValueError:
        inside = False
    if not inside or os.path.normcase(real_out) == os.path.normcase(real_dest):
        raise UnsafePathError("path escapes the output folder")

    if used is not None:
        key = os.path.normcase(out_path)
        if key in used and file_id:
            stem, ext = os.path.splitext(parts[-1])
            parts[-1] = f"{stem}_{file_id[:8]}{ext}"
            out_path = os.path.join(real_dest, *parts)
            key = os.path.normcase(out_path)
        used.add(key)
    return out_path


def fs_path(path):
    """Use the Windows extended-length form for very long paths.

    iOS backups contain deep paths (e.g. Notes attachments) that exceed
    the 260 character limit once joined to the output folder.
    """
    if os.name != "nt":
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\") or len(path) < 240:
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def is_wrong_passphrase(exc):
    """True if *exc* means the backup password was rejected."""
    return (type(exc).__name__ == "IncorrectPassphraseError"
            or (isinstance(exc, ValueError)
                and "incorrect passphrase" in str(exc).lower()))


class ExtractionReport:
    """Outcome of one extraction run."""

    def __init__(self):
        self.extracted = 0
        self.skipped = []   # (label, reason)
        self.errors = []    # (label, reason)
        self.cancelled = False


def _default_backup_factory(**kwargs):
    from iphone_backup_decrypt import EncryptedBackup
    return EncryptedBackup(**kwargs)


class BackupSession:
    """Owns the opened backup and runs every call on ONE worker thread.

    ``iphone_backup_decrypt`` keeps a SQLite connection to the decrypted
    manifest, and SQLite connections may only be used on the thread that
    created them. The public methods therefore never touch the backup
    directly: they queue the work on a single dedicated thread and return a
    ``concurrent.futures.Future``. That thread opens the backup, runs all
    queries and extractions, and releases it, so the connection is always
    used (and closed) where it was created.
    """

    def __init__(self, backup_factory=None):
        self._factory = backup_factory or _default_backup_factory
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="backup-worker"
        )
        self._backup = None
        self._cancel = threading.Event()
        self._closed = False

    # ── Public API (any thread; each returns a Future) ───────

    def open(self, backup_dir, passphrase):
        """Decrypt and open a backup. Future -> (domains, total_files)."""
        return self._executor.submit(self._open, backup_dir, passphrase)

    def query_files(self, domains=None, search="", limit=MAX_ROWS):
        """List files. Future -> (rows, total_matching).

        *domains* is a list of domains, or None for the whole backup.
        Each row is (file_id, domain, relative_path, size_str, mod_str).
        """
        return self._executor.submit(self._query_files, domains, search,
                                     limit)

    def extract(self, file_ids, dest, progress=None):
        """Decrypt files into *dest*. Future -> ExtractionReport."""
        self._cancel.clear()
        return self._executor.submit(self._extract, list(file_ids), dest,
                                     progress)

    def cancel_extraction(self):
        self._cancel.set()

    def close(self):
        """Release the backup (and its temporary decrypted manifest)."""
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        self._executor.submit(self._discard)
        self._executor.shutdown(wait=False)

    # ── Worker-thread internals ──────────────────────────────

    def _discard(self):
        self._swap_backup(None)

    def _swap_backup(self, new_backup):
        # Dropping the last reference to the old backup runs the library's
        # cleanup here, on the thread that owns its SQLite connection, and
        # deletes its temporary decrypted Manifest.db.
        old, self._backup = self._backup, new_backup
        del old

    def _require_backup(self):
        if self._backup is None:
            raise RuntimeError("No backup is open.")
        return self._backup

    def _open(self, backup_dir, passphrase):
        # The previously opened backup (if any) stays usable until the new
        # one has opened successfully.
        backup = self._factory(backup_directory=backup_dir,
                               passphrase=passphrase)
        try:
            with backup.manifest_db_cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT domain FROM Files ORDER BY domain"
                )
                domains = [r[0] for r in cur.fetchall() if r[0]]
                cur.execute("SELECT COUNT(*) FROM Files WHERE flags=1")
                total = cur.fetchone()[0]
        except BaseException:
            # Do not let the traceback keep the half-open backup alive
            # past this thread.
            backup = None
            raise
        self._swap_backup(backup)
        return domains, total

    def _query_files(self, domains, search, limit):
        backup = self._require_backup()
        if domains is not None and not domains:
            return [], 0

        where = ["flags=1"]
        params = []
        if search:
            where.append("(domain LIKE ? ESCAPE '\\' "
                         "OR relativePath LIKE ? ESCAPE '\\')")
            pattern = "%" + escape_like(search) + "%"
            params += [pattern, pattern]

        chunks = [None] if domains is None else [
            domains[i:i + _DOMAIN_CHUNK]
            for i in range(0, len(domains), _DOMAIN_CHUNK)
        ]
        raw_rows = []
        total = 0
        with backup.manifest_db_cursor() as cur:
            for chunk in chunks:
                clauses = list(where)
                chunk_params = list(params)
                if chunk is not None:
                    clauses.append(
                        "domain IN (%s)" % ",".join("?" * len(chunk))
                    )
                    chunk_params += chunk
                condition = " AND ".join(clauses)

                cur.execute("SELECT COUNT(*) FROM Files WHERE " + condition,
                            chunk_params)
                total += cur.fetchone()[0]

                remaining = limit - len(raw_rows)
                if remaining > 0:
                    cur.execute(
                        "SELECT fileID, domain, relativePath, file "
                        "FROM Files WHERE " + condition +
                        " ORDER BY domain, relativePath LIMIT ?",
                        chunk_params + [remaining],
                    )
                    raw_rows.extend(cur.fetchall())
        return self._display_rows(raw_rows), total

    @staticmethod
    def _display_rows(raw_rows):
        rows = []
        for file_id, domain, rel_path, blob in raw_rows:
            size, mtime = read_file_info(blob)
            size_str = format_size(size) if size else ""
            mod_str = ""
            if mtime:
                try:
                    mod_str = datetime.fromtimestamp(mtime).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                except (OverflowError, OSError, ValueError):
                    pass
            rows.append((file_id, domain or "", rel_path or "", size_str,
                         mod_str))
        return rows

    def _extract(self, file_ids, dest, progress):
        backup = self._require_backup()
        report = ExtractionReport()
        used = set()
        total = len(file_ids)
        last_report = 0.0

        with backup.manifest_db_cursor() as cur:
            for n, file_id in enumerate(file_ids, 1):
                if self._cancel.is_set():
                    report.cancelled = True
                    break
                try:
                    self._extract_one(backup, cur, file_id, dest, used,
                                      report)
                except Exception as exc:
                    # Not listed in the manifest (or the manifest query
                    # itself failed): nothing better to show than the ID.
                    report.errors.append(
                        (file_id, f"{type(exc).__name__}: {exc}")
                    )
                if progress:
                    now = time.monotonic()
                    if n == total or now - last_report >= 0.1:
                        last_report = now
                        progress(n, total)
        return report

    def _extract_one(self, backup, cur, file_id, dest, used, report):
        cur.execute(
            "SELECT domain, relativePath, file FROM Files "
            "WHERE fileID=? AND flags=1", (file_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError("file is not listed in the backup manifest")
        domain, rel_path, blob = row
        label = f"{domain}/{rel_path}"
        try:
            if not domain or not rel_path:
                raise UnsafePathError("entry has no domain or path")
            out_path = fs_path(
                build_output_path(dest, domain, rel_path, file_id, used)
            )
            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            size, mtime = read_file_info(blob)
            if size == 0:
                # Empty files have no key and no data in the backup.
                open(out_path, "wb").close()
                if mtime:
                    os.utime(out_path, (mtime, mtime))
            else:
                self._decrypt_to(backup, cur, file_id, domain, rel_path,
                                 out_path)
            report.extracted += 1
        except UnsafePathError as exc:
            report.skipped.append((label, str(exc)))
        except Exception as exc:
            report.errors.append((label, f"{type(exc).__name__}: {exc}"))

    @staticmethod
    def _decrypt_to(backup, cur, file_id, domain, rel_path, out_path):
        # extract_file() looks the file up by relative path and a LIKE
        # pattern on the domain, so a '_' or '%' in the domain could also
        # match a different domain holding the same path.
        cur.execute(
            "SELECT COUNT(*) FROM Files "
            "WHERE relativePath = ? AND domain LIKE ? AND flags=1",
            (rel_path, domain),
        )
        if cur.fetchone()[0] <= 1:
            backup.extract_file(relative_path=rel_path, domain_like=domain,
                                output_filename=out_path)
            return

        # Ambiguous: pick the exact row by file ID instead.
        def only_this_file(**kwargs):
            return out_path if kwargs.get("file_id") == file_id else False

        count = backup.extract_files(
            relative_paths_like=rel_path, domain_like=domain,
            output_folder=os.path.dirname(out_path),
            filter_callback=only_this_file,
        )
        if count != 1:
            raise RuntimeError("could not select this file unambiguously")


class BackupLens:
    """Main application class for BackupLens."""

    # Where iTunes / Finder / the Apple Devices app keep local backups.
    # Windows has two: the classic installer and the Microsoft Store app.
    BACKUP_PATHS = {
        "Windows": [
            os.path.expandvars(r"%APPDATA%\Apple Computer\MobileSync\Backup"),
            os.path.expandvars(r"%USERPROFILE%\Apple\MobileSync\Backup"),
        ],
        "Darwin": [
            os.path.expanduser(
                "~/Library/Application Support/MobileSync/Backup"
            ),
        ],
    }

    def __init__(self, root, session=None):
        self.root = root
        self.root.title(f"{APP_NAME} v{__version__}")
        self.root.geometry("1200x750")
        self.root.minsize(900, 550)

        self.session = session or BackupSession()
        self.backup_open = False
        self._scope_domains = None   # None = every domain
        self._scope_set = False
        self._request_id = 0         # discards out-of-date file lists
        self._search_job = None
        self._extracting = False

        # Worker threads must never call into Tk (it can hang at shutdown),
        # so they hand work to the Tk thread through this queue.
        self._ui_queue = queue.Queue()
        self._poll_job = None

        self._apply_style()
        self._build_ui()
        self._auto_detect_backup()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_ui_queue()

    # ── Theming ──────────────────────────────────────────────

    @staticmethod
    def _system_font():
        """Return the best available font family for the current platform."""
        system = platform.system()
        if system == "Darwin":
            return "Helvetica Neue"
        elif system == "Windows":
            return "Segoe UI"
        return "DejaVu Sans"

    def _apply_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        font = self._system_font()

        # Warm, trustworthy palette
        self.colors = {
            "bg": "#faf7f2",
            "fg": "#2d2d2d",
            "surface": "#ffffff",
            "border": "#e5ddd0",
            "accent": "#d97706",       # warm amber
            "accent_hover": "#b45309",
            "green": "#059669",
            "green_light": "#d1fae5",
            "muted": "#6b7280",
            "heading": "#1f2937",
        }
        c = self.colors

        style.configure(".", background=c["bg"], foreground=c["fg"],
                         fieldbackground=c["surface"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"],
                         font=(font, 10))
        style.configure("Title.TLabel", background=c["bg"],
                         foreground=c["accent"], font=(font, 18, "bold"))
        style.configure("Subtitle.TLabel", background=c["bg"],
                         foreground=c["muted"], font=(font, 9))
        style.configure("Status.TLabel", background=c["green_light"],
                         foreground=c["green"], font=(font, 9),
                         padding=6)
        style.configure("TButton", background=c["accent"],
                         foreground="#ffffff", font=(font, 10, "bold"),
                         padding=8)
        style.map("TButton",
                   background=[("active", c["accent_hover"])],
                   foreground=[("active", "#ffffff")])
        style.configure("Safe.TButton", background=c["green"],
                         foreground="#ffffff")
        style.map("Safe.TButton",
                   background=[("active", "#047857")])
        style.configure("TEntry", fieldbackground=c["surface"],
                         foreground=c["fg"], insertcolor=c["fg"],
                         padding=4)
        style.configure("Treeview", background=c["surface"],
                         foreground=c["fg"], fieldbackground=c["surface"],
                         font=(font, 9), rowheight=26)
        style.configure("Treeview.Heading", background=c["bg"],
                         foreground=c["heading"],
                         font=(font, 9, "bold"))
        style.map("Treeview",
                   background=[("selected", c["accent"])],
                   foreground=[("selected", "#ffffff")])
        style.configure("TLabelframe", background=c["bg"],
                         foreground=c["accent"])
        style.configure("TLabelframe.Label", background=c["bg"],
                         foreground=c["accent"],
                         font=(font, 10, "bold"))

    # ── UI Layout ────────────────────────────────────────────

    def _build_ui(self):
        # Header
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=20, pady=(14, 0))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Your data. Your eyes only.",
                  style="Subtitle.TLabel").pack(side="left", padx=(12, 0),
                                                 pady=(6, 0))

        # Privacy banner
        privacy_frame = ttk.Frame(self.root)
        privacy_frame.pack(fill="x", padx=20, pady=(6, 0))
        privacy_lbl = tk.Label(
            privacy_frame,
            text="  100% Offline  •  No data leaves your computer  "
                 "•  Open source  •  Your password is never stored  ",
            bg=self.colors["green_light"], fg=self.colors["green"],
            font=(self._system_font(), 9), padx=10, pady=4, anchor="w",
        )
        privacy_lbl.pack(fill="x")

        # Connection frame
        conn_frame = ttk.LabelFrame(self.root, text="Open Backup", padding=12)
        conn_frame.pack(fill="x", padx=20, pady=(10, 0))

        row1 = ttk.Frame(conn_frame)
        row1.pack(fill="x", pady=3)
        ttk.Label(row1, text="Backup Folder:").pack(side="left")
        self.path_var = tk.StringVar()
        path_entry = ttk.Entry(row1, textvariable=self.path_var, width=75)
        path_entry.pack(side="left", padx=8)
        ttk.Button(row1, text="Browse...",
                    command=self._browse_folder).pack(side="left")

        row2 = ttk.Frame(conn_frame)
        row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="Password:").pack(side="left")
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(row2, textvariable=self.pass_var,
                                     show="*", width=40)
        self.pass_entry.pack(side="left", padx=8)
        self.pass_entry.bind("<Return>", lambda e: self._decrypt())

        self.decrypt_btn = ttk.Button(row2, text="Decrypt & Open",
                                       command=self._decrypt, style="Safe.TButton")
        self.decrypt_btn.pack(side="left", padx=8)

        self.status_var = tk.StringVar(
            value="Select a backup folder and enter your password to begin."
        )
        status_bar = ttk.Label(conn_frame, textvariable=self.status_var,
                                style="Status.TLabel")
        status_bar.pack(fill="x", pady=(8, 0))

        # Main content
        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=20, pady=10)

        # Left: categories
        left_frame = ttk.LabelFrame(paned, text="Categories", padding=6)
        paned.add(left_frame, weight=1)

        self.domain_tree = ttk.Treeview(left_frame, show="tree",
                                         selectmode="browse")
        domain_scroll = ttk.Scrollbar(left_frame, orient="vertical",
                                       command=self.domain_tree.yview)
        self.domain_tree.configure(yscrollcommand=domain_scroll.set)
        self.domain_tree.pack(side="left", fill="both", expand=True)
        domain_scroll.pack(side="right", fill="y")
        self.domain_tree.bind("<<TreeviewSelect>>", self._on_domain_select)

        # Right: file list
        right_frame = ttk.LabelFrame(paned, text="Files", padding=6)
        paned.add(right_frame, weight=3)

        toolbar = ttk.Frame(right_frame)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(toolbar, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._schedule_search())
        ttk.Entry(toolbar, textvariable=self.search_var, width=35).pack(
            side="left", padx=8)
        self.count_var = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self.count_var,
                  style="Subtitle.TLabel").pack(side="right")
        ttk.Button(toolbar, text="Extract Selected",
                    command=self._extract_selected).pack(side="right", padx=6)
        ttk.Button(toolbar, text="Extract All in View",
                    command=self._extract_all_view).pack(side="right", padx=2)

        cols = ("domain", "path", "size", "modified")
        self.file_tree = ttk.Treeview(right_frame, columns=cols,
                                       show="headings", selectmode="extended")
        self.file_tree.heading("domain", text="Domain")
        self.file_tree.heading("path", text="Relative Path")
        self.file_tree.heading("size", text="Size")
        self.file_tree.heading("modified", text="Modified")
        self.file_tree.column("domain", width=160, minwidth=100)
        self.file_tree.column("path", width=400, minwidth=200)
        self.file_tree.column("size", width=80, minwidth=60)
        self.file_tree.column("modified", width=140, minwidth=100)

        fy = ttk.Scrollbar(right_frame, orient="vertical",
                            command=self.file_tree.yview)
        fx = ttk.Scrollbar(right_frame, orient="horizontal",
                            command=self.file_tree.xview)
        self.file_tree.configure(yscrollcommand=fy.set, xscrollcommand=fx.set)
        # Pack order matters: bottom scrollbar first, then right, then tree
        fx.pack(side="bottom", fill="x")
        fy.pack(side="right", fill="y")
        self.file_tree.pack(side="left", fill="both", expand=True)

    # ── Background work ──────────────────────────────────────

    def _post(self, func, *args):
        """Queue ``func(*args)`` to run on the Tk thread (thread-safe)."""
        self._ui_queue.put((func, args))

    def _drain_ui_queue(self):
        try:
            while True:
                func, args = self._ui_queue.get_nowait()
                try:
                    func(*args)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
        except queue.Empty:
            pass
        finally:
            self._poll_job = self.root.after(50, self._drain_ui_queue)

    def _when_done(self, future, callback):
        """Run ``callback(future)`` on the Tk thread once *future* is done."""
        future.add_done_callback(lambda fut: self._post(callback, fut))

    # ── Auto-detect backup ───────────────────────────────────

    def _auto_detect_backup(self):
        newest = None
        blocked = False
        for base in self.BACKUP_PATHS.get(platform.system(), []):
            if not os.path.isdir(base):
                continue
            try:
                for name in os.listdir(base):
                    path = os.path.join(base, name)
                    if os.path.isdir(path) and (
                            newest is None
                            or os.path.getmtime(path)
                            > os.path.getmtime(newest)):
                        newest = path
            except OSError:
                # macOS blocks listing MobileSync/Backup unless the user
                # picks the folder in a file dialog.
                blocked = True
        if newest:
            # Pick the most recently modified backup
            self.path_var.set(newest)
            self.status_var.set(
                f"Auto-detected backup: {os.path.basename(newest)}. "
                "Enter your password to decrypt."
            )
        elif blocked:
            self.status_var.set(
                "The operating system is blocking automatic backup "
                "detection. Click Browse and select the backup folder "
                "(the long hex name inside MobileSync/Backup)."
            )

    # ── Folder browser ───────────────────────────────────────

    def _browse_folder(self):
        initial = next(
            (p for p in self.BACKUP_PATHS.get(platform.system(), [])
             if os.path.isdir(p)), None
        )
        path = filedialog.askdirectory(
            title="Select iPhone/iPad Backup Folder",
            initialdir=initial,
        )
        if path:
            self.path_var.set(path)

    # ── Decryption ───────────────────────────────────────────

    def _decrypt(self):
        backup_dir = self.path_var.get().strip()
        # Not stripped: a backup password may contain leading/trailing spaces.
        passphrase = self.pass_var.get()

        if not backup_dir or not os.path.isdir(backup_dir):
            messagebox.showerror("Error", "Please select a valid backup folder.")
            return
        if not passphrase:
            messagebox.showerror("Error",
                                  "Please enter the backup encryption password.")
            return

        self.decrypt_btn.configure(state="disabled")
        self.status_var.set("Decrypting... this may take a moment.")
        self.root.update_idletasks()

        future = self.session.open(backup_dir, passphrase)
        self._when_done(
            future, lambda fut: self._on_open_done(fut, passphrase)
        )

    def _on_open_done(self, future, passphrase):
        error = future.exception()
        if error is not None:
            self._on_decrypt_fail(error, passphrase)
            return
        # Clear password from the UI after successful decryption
        self.pass_var.set("")
        domains, total = future.result()
        self._on_decrypt_success(domains, total)

    def _on_decrypt_success(self, domains, total_files):
        self.decrypt_btn.configure(state="normal")
        self.backup_open = True
        self._scope_set = False
        self._scope_domains = None
        self._request_id += 1
        self.file_tree.delete(*self.file_tree.get_children())
        self.count_var.set("")
        self.status_var.set(
            f"Decrypted! {total_files:,} files across {len(domains)} domains."
        )
        self.domain_tree.delete(*self.domain_tree.get_children())

        categories = {}
        friendly = {
            "CameraRollDomain": "Camera Roll / Photos",
            "MediaDomain": "Media (Music, Videos)",
            "HomeDomain": "Home / Settings",
            "HealthDomain": "Health Data",
            "KeychainDomain": "Keychain",
            "WirelessDomain": "Wireless / Network",
            "ManagedPreferencesDomain": "Managed Preferences",
            "RootDomain": "Root / System",
            "SystemPreferencesDomain": "System Preferences",
            "DatabaseDomain": "Databases",
            "InstallDomain": "Installed Apps",
            "SysContainerDomain": "System Containers",
            "SysSharedContainerDomain": "Shared Containers",
        }

        self.domain_tree.insert("", "end", iid="__ALL__",
                                 text=f"All Files ({total_files:,})")

        for domain in domains:
            base = domain.split("-")[0] if "-" in domain else domain
            name = friendly.get(domain, friendly.get(base, None))

            if base.startswith("AppDomain"):
                cat = "Apps"
            elif base.startswith("SysContainerDomain") or \
                    base.startswith("SysSharedContainer"):
                cat = "System Containers"
            elif name:
                cat = name
            else:
                cat = "Other"

            categories.setdefault(cat, []).append(domain)

        for cat in sorted(categories):
            cat_id = f"__CAT__{cat}"
            self.domain_tree.insert(
                "", "end", iid=cat_id,
                text=f"{cat} ({len(categories[cat])})",
            )
            for domain in sorted(categories[cat]):
                self.domain_tree.insert(cat_id, "end", iid=domain,
                                         text=domain)

    def _on_decrypt_fail(self, error, passphrase=""):
        self.decrypt_btn.configure(state="normal")
        message = str(error) or type(error).__name__
        # Never echo the password back in an error message.
        if passphrase and len(passphrase) >= 4:
            message = message.replace(passphrase, "****")

        if isinstance(error, ImportError):
            self.status_var.set("Missing dependency. See error above.")
            messagebox.showerror(
                "Missing Dependency",
                "A required package could not be loaded:\n"
                f"  {message}\n\n"
                "Install the requirements by running:\n"
                "  pip install -r requirements.txt",
            )
        elif is_wrong_passphrase(error):
            self.status_var.set("Incorrect password. Please try again.")
            messagebox.showerror(
                "Decryption Failed",
                "The password is incorrect. Please try again.\n\n"
                "Tip: This is the password you set when enabling\n"
                "encrypted backups in iTunes or Finder. Check for\n"
                "accidental leading or trailing spaces.",
            )
        else:
            self.status_var.set(f"Error: {message}")
            messagebox.showerror("Error",
                                  f"Failed to load backup:\n{message}")

    # ── File loading ─────────────────────────────────────────

    def _on_domain_select(self, event):
        sel = self.domain_tree.selection()
        if not sel or not self.backup_open:
            return
        selected = sel[0]
        if selected == "__ALL__":
            self._scope_domains = None
        elif selected.startswith("__CAT__"):
            self._scope_domains = list(self.domain_tree.get_children(selected))
        else:
            self._scope_domains = [selected]
        self._scope_set = True
        self._refresh_files()

    def _schedule_search(self):
        """Re-query shortly after the user stops typing."""
        if not self.backup_open:
            return
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(300, self._refresh_files)

    def _refresh_files(self):
        self._search_job = None
        if not self.backup_open:
            return
        if not self._scope_set:
            # Searching before choosing a category searches the whole backup.
            self._scope_domains = None
            self._scope_set = True

        self._request_id += 1
        request_id = self._request_id
        self.status_var.set(
            "Waiting for the running extraction to finish..."
            if self._extracting else "Loading files..."
        )
        future = self.session.query_files(
            self._scope_domains, self.search_var.get().strip(), MAX_ROWS
        )
        self._when_done(
            future, lambda fut: self._on_files_loaded(fut, request_id)
        )

    def _on_files_loaded(self, future, request_id):
        if request_id != self._request_id:
            return  # a newer selection or search superseded this one
        error = future.exception()
        if error is not None:
            self.status_var.set(
                f"Error loading: {str(error) or type(error).__name__}"
            )
            return
        rows, total = future.result()
        self._populate_files(rows, total)

    def _populate_files(self, rows, total):
        self.file_tree.delete(*self.file_tree.get_children())
        for file_id, domain, rel_path, size_str, mod_str in rows:
            self.file_tree.insert("", "end", iid=file_id,
                                   values=(domain, rel_path, size_str, mod_str))

        if total > len(rows):
            self.count_var.set(f"{len(rows):,} of {total:,} files")
            self.status_var.set(
                f"Showing the first {len(rows):,} of {total:,} matching "
                "files. Narrow the category or use the search box to "
                "see the rest."
            )
        else:
            self.count_var.set(f"{len(rows):,} files")
            self.status_var.set(f"Loaded {len(rows):,} files.")

    # ── Extraction ───────────────────────────────────────────

    def _extract_selected(self):
        items = self.file_tree.selection()
        if not items:
            messagebox.showinfo("Info", "Select files to extract first.")
            return
        self._extract_files(items)

    def _extract_all_view(self):
        items = self.file_tree.get_children()
        if not items:
            messagebox.showinfo("Info", "No files to extract.")
            return
        if len(items) > 100:
            if not messagebox.askyesno(
                "Confirm", f"Extract {len(items):,} files?"
            ):
                return
        self._extract_files(items)

    def _extract_files(self, file_ids):
        if self._extracting:
            messagebox.showinfo("Info", "An extraction is already running.")
            return
        dest = filedialog.askdirectory(title="Select Output Folder")
        if not dest:
            return
        self._extracting = True
        self.status_var.set(f"Extracting {len(file_ids)} files...")
        self.root.update_idletasks()

        def progress(done, total):
            self._post(self._show_progress, done, total)

        future = self.session.extract(file_ids, dest, progress)
        self._when_done(future, lambda fut: self._on_extract_done(fut, dest))

    def _show_progress(self, done, total):
        if self._extracting:
            self.status_var.set(f"Extracting... {done:,} of {total:,} files")

    def _on_extract_done(self, future, dest):
        self._extracting = False
        error = future.exception()
        if error is not None:
            msg = f"Extraction failed: {str(error) or type(error).__name__}"
            self.status_var.set(msg)
            messagebox.showerror("Error", msg)
            return

        report = future.result()
        msg = f"Extracted {report.extracted:,} files to {dest}"
        if report.errors:
            msg += f" ({len(report.errors)} errors)"
        if report.skipped:
            msg += f" ({len(report.skipped)} skipped)"
        if report.cancelled:
            msg += " (cancelled)"
        self.status_var.set(msg)

        problems = [f"{label}: {reason}"
                    for label, reason in report.errors + report.skipped]
        if problems:
            shown = "\n".join(problems[:10])
            more = len(problems) - 10
            if more > 0:
                shown += f"\n...and {more} more"
            messagebox.showwarning("Done, with problems",
                                    f"{msg}\n\n{shown}")
        else:
            messagebox.showinfo("Done", msg)

    # ── Shutdown ─────────────────────────────────────────────

    def _on_close(self):
        if self._extracting and not messagebox.askyesno(
            "Extraction running", "An extraction is still running. Quit anyway?"
        ):
            return
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
        self.session.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    root.iconname(APP_NAME)
    BackupLens(root)
    root.mainloop()


if __name__ == "__main__":
    main()
