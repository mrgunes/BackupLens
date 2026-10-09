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
import contextlib
import pathlib
import re
import sqlite3
import tempfile
import threading
import os
import plistlib
import platform
import shutil
from datetime import datetime

__version__ = "1.0.0"
APP_NAME = "BackupLens"

ENCRYPTED = "encrypted"
UNENCRYPTED = "unencrypted"

_FILE_ID = re.compile(r"[0-9a-f]{40}\Z")

_LEGACY_FORMAT = (
    "This backup uses the old Manifest.mbdb format (made by iTunes for "
    "iOS 9 and earlier), which this app cannot open."
)


class BackupFormatError(ValueError):
    """The folder is not an iOS backup this app can open."""


def detect_backup(backup_dir):
    """Return ENCRYPTED or UNENCRYPTED for the backup in *backup_dir*.

    Raises BackupFormatError, with a message fit to show the user, if the
    folder is not a backup this app can open.
    """
    if not os.path.isdir(backup_dir):
        raise BackupFormatError("The backup folder does not exist.")
    plist_path = os.path.join(backup_dir, "Manifest.plist")
    try:
        with open(plist_path, "rb") as handle:
            manifest = plistlib.load(handle)
    except FileNotFoundError:
        if os.path.exists(os.path.join(backup_dir, "Manifest.mbdb")):
            raise BackupFormatError(_LEGACY_FORMAT) from None
        raise BackupFormatError(
            "This folder has no Manifest.plist, so it does not look like an "
            "iOS backup. Select the long hex-named folder inside "
            "MobileSync/Backup."
        ) from None
    except Exception as exc:
        raise BackupFormatError(
            f"Manifest.plist could not be read ({type(exc).__name__})."
        ) from exc
    if not isinstance(manifest, dict):
        raise BackupFormatError("Manifest.plist is not in the expected format.")
    if not os.path.isfile(os.path.join(backup_dir, "Manifest.db")):
        if os.path.exists(os.path.join(backup_dir, "Manifest.mbdb")):
            raise BackupFormatError(_LEGACY_FORMAT)
        raise BackupFormatError("This backup has no Manifest.db file.")
    return ENCRYPTED if manifest.get("IsEncrypted") else UNENCRYPTED


def _file_record(blob):
    """Return ``(size, mtime)`` from a Manifest.db ``file`` blob."""
    try:
        meta = plistlib.loads(blob)
        info = meta["$objects"][meta["$top"]["root"].data]
        size, mtime = info.get("Size"), info.get("LastModified")
    except Exception:
        return None, None
    return (size if isinstance(size, int) else None,
            mtime if isinstance(mtime, (int, float)) else None)


class PlainBackup:
    """Read-only access to an unencrypted iOS backup.

    Offers the part of ``iphone_backup_decrypt.EncryptedBackup`` that this
    app uses (``manifest_db_cursor`` / ``extract_file``), so the rest of the
    app does not need to care which kind of backup it has.

    Manifest.db is opened read-only and ``immutable``: SQLite takes no locks
    and writes no journal files next to the user's backup, and no copy of
    the index is made. Every cursor gets its own connection, so this object
    can be used from any thread.
    """

    def __init__(self, *, backup_directory):
        self._dir = os.path.abspath(backup_directory)
        manifest = pathlib.Path(self._dir, "Manifest.db")
        self._uri = manifest.as_uri() + "?mode=ro&immutable=1"
        try:
            with self.manifest_db_cursor() as cur:
                cur.execute("SELECT 1 FROM Files LIMIT 1")
        except sqlite3.DatabaseError as exc:
            raise BackupFormatError(
                f"Manifest.db could not be read as a backup index ({exc})."
            ) from exc

    @contextlib.contextmanager
    def manifest_db_cursor(self):
        conn = sqlite3.connect(self._uri, uri=True)
        try:
            cur = conn.cursor()
            yield cur
            cur.close()
        finally:
            conn.close()

    def extract_file(self, *, relative_path, domain=None, domain_like=None,
                     output_filename):
        # `domain_like` is accepted so this works with either spelling of
        # the keyword; here it is matched exactly, not as a LIKE pattern.
        domain = domain if domain is not None else domain_like
        query = ("SELECT fileID, file FROM Files "
                 "WHERE relativePath=? AND flags=1")
        params = [relative_path]
        if domain is not None:
            query += " AND domain=?"
            params.append(domain)
        with self.manifest_db_cursor() as cur:
            cur.execute(query + " ORDER BY domain LIMIT 1", params)
            row = cur.fetchone()
        if not row:
            raise FileNotFoundError(relative_path)
        file_id, blob = row
        # file_id comes from the backup itself, so do not trust it to be a
        # plain name when building a path.
        if not _FILE_ID.match(file_id):
            raise ValueError("invalid file ID in the backup manifest")

        size, mtime = _file_record(blob)
        directory = os.path.dirname(output_filename) or "."
        handle, partial = tempfile.mkstemp(dir=directory)
        os.close(handle)
        try:
            if size != 0:  # empty files have no data in the backup
                shutil.copyfile(
                    os.path.join(self._dir, file_id[:2], file_id), partial
                )
            os.replace(partial, output_filename)
        except BaseException:
            if os.path.exists(partial):
                os.remove(partial)
            raise
        if mtime:
            os.utime(output_filename, (mtime, mtime))


class BackupLens:
    """Main application class for BackupLens."""

    # Default backup locations by platform
    BACKUP_PATHS = {
        "Windows": os.path.expandvars(
            r"%APPDATA%\Apple Computer\MobileSync\Backup"
        ),
        "Darwin": os.path.expanduser(
            "~/Library/Application Support/MobileSync/Backup"
        ),
    }

    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{__version__}")
        self.root.geometry("1200x750")
        self.root.minsize(900, 550)

        self.backup = None
        self.backup_dir = None
        self.file_list = []
        self._opening_encrypted = True
        self._detect_job = None
        self._auto_path = None       # folder chosen by auto-detect

        self._apply_style()
        self._build_ui()
        self._auto_detect_backup()

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
            text="  100% Offline  \u2022  No data leaves your computer  "
                 "\u2022  Open source  \u2022  Your password is never stored  ",
            bg=self.colors["green_light"], fg=self.colors["green"],
            font=("Segoe UI", 9), padx=10, pady=4, anchor="w",
        )
        privacy_lbl.pack(fill="x")

        # Connection frame
        conn_frame = ttk.LabelFrame(self.root, text="Open Backup", padding=12)
        conn_frame.pack(fill="x", padx=20, pady=(10, 0))

        row1 = ttk.Frame(conn_frame)
        row1.pack(fill="x", pady=3)
        ttk.Label(row1, text="Backup Folder:").pack(side="left")
        self.path_var = tk.StringVar()
        self.path_var.trace_add("write", lambda *a: self._schedule_detect())
        path_entry = ttk.Entry(row1, textvariable=self.path_var, width=75)
        path_entry.pack(side="left", padx=8)
        ttk.Button(row1, text="Browse...",
                    command=self._browse_folder).pack(side="left")

        row2 = ttk.Frame(conn_frame)
        row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="Password (encrypted backups):").pack(
            side="left")
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(row2, textvariable=self.pass_var,
                                     show="*", width=40)
        self.pass_entry.pack(side="left", padx=8)
        self.pass_entry.bind("<Return>", lambda e: self._decrypt())

        self.decrypt_btn = ttk.Button(row2, text="Open Backup",
                                       command=self._decrypt, style="Safe.TButton")
        self.decrypt_btn.pack(side="left", padx=8)

        self.status_var = tk.StringVar(
            value="Select a backup folder to begin. A password is only "
                  "needed for encrypted backups."
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
        self.search_var.trace_add("write", lambda *a: self._filter_files())
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

    # ── Auto-detect backup ───────────────────────────────────

    def _auto_detect_backup(self):
        system = platform.system()
        base = self.BACKUP_PATHS.get(system)
        if base and os.path.isdir(base):
            subs = [
                os.path.join(base, d) for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d))
            ]
            if subs:
                # Pick the most recently modified backup
                subs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                self._auto_path = subs[0]
                self.path_var.set(subs[0])
                self.status_var.set(
                    f"Auto-detected backup: {os.path.basename(subs[0])}."
                )
                self._update_backup_kind()

    # ── Folder browser ───────────────────────────────────────

    def _browse_folder(self):
        initial = self.BACKUP_PATHS.get(platform.system(), "")
        path = filedialog.askdirectory(
            title="Select iPhone/iPad Backup Folder",
            initialdir=initial if os.path.isdir(initial) else None,
        )
        if path:
            self.path_var.set(path)

    # ── Backup type ──────────────────────────────────────────

    def _schedule_detect(self):
        """Check the chosen folder shortly after the user stops typing."""
        if self._detect_job is not None:
            self.root.after_cancel(self._detect_job)
        self._detect_job = self.root.after(300, self._update_backup_kind)

    def _update_backup_kind(self):
        """Adapt the password field and button to the chosen backup."""
        if self._detect_job is not None:
            self.root.after_cancel(self._detect_job)  # called directly
            self._detect_job = None
        path = self.path_var.get().strip()
        kind, problem = None, None
        if path and os.path.isdir(path):
            try:
                kind = detect_backup(path)
            except BackupFormatError as exc:
                problem = str(exc)

        self.pass_entry.configure(
            state="disabled" if kind == UNENCRYPTED else "normal"
        )
        self.decrypt_btn.configure(
            text="Decrypt & Open" if kind == ENCRYPTED else "Open Backup"
        )
        if self.decrypt_btn.instate(["disabled"]):
            return  # an open is in progress; leave its status alone

        prefix = ""
        if path == self._auto_path:
            prefix = f"Auto-detected backup: {os.path.basename(path)}. "
        if kind == ENCRYPTED:
            self.status_var.set(
                prefix + "This backup is encrypted. Enter its password "
                "to decrypt it.")
        elif kind == UNENCRYPTED:
            self.status_var.set(
                prefix + "This backup is not encrypted, so no password is "
                "needed. Click Open Backup.")
        elif problem:
            self.status_var.set(problem)

    # ── Decryption ───────────────────────────────────────────

    def _decrypt(self):
        backup_dir = self.path_var.get().strip()
        passphrase = self.pass_var.get().strip()

        if not backup_dir or not os.path.isdir(backup_dir):
            messagebox.showerror("Error", "Please select a valid backup folder.")
            return
        try:
            kind = detect_backup(backup_dir)
        except BackupFormatError as exc:
            messagebox.showerror("Not a usable backup", str(exc))
            return
        encrypted = kind == ENCRYPTED
        if encrypted and not passphrase:
            messagebox.showerror(
                "Error",
                "This backup is encrypted. Please enter the backup "
                "encryption password.",
            )
            return

        self._opening_encrypted = encrypted
        self.decrypt_btn.configure(state="disabled")
        self.status_var.set(
            "Decrypting... this may take a moment." if encrypted
            else "Opening backup..."
        )
        self.root.update_idletasks()

        threading.Thread(target=self._decrypt_thread,
                          args=(backup_dir, passphrase if encrypted else "",
                                encrypted),
                          daemon=True).start()

    def _query_manifest(self, callback, *args):
        """Execute a callback with a manifest DB cursor (context-managed)."""
        with self.backup.manifest_db_cursor() as cur:
            return callback(cur, *args)

    def _decrypt_thread(self, backup_dir, passphrase, encrypted=True):
        if encrypted:
            try:
                from iphone_backup_decrypt import EncryptedBackup
            except ImportError:
                self.root.after(0, lambda: (
                    self.decrypt_btn.configure(state="normal"),
                    messagebox.showerror(
                        "Missing Dependency",
                        "The 'iphone_backup_decrypt' package is required "
                        "for encrypted backups.\n\n"
                        "Install it by running:\n"
                        "  pip install iphone_backup_decrypt",
                    ),
                    self.status_var.set("Missing dependency. See error above."),
                ))
                return

        try:
            if encrypted:
                self.backup = EncryptedBackup(
                    backup_directory=backup_dir, passphrase=passphrase
                )
            else:
                self.backup = PlainBackup(backup_directory=backup_dir)
            self.backup_dir = backup_dir
            # Clear password from the UI after successful decryption
            self.root.after(0, lambda: self.pass_var.set(""))

            def load_initial(cur):
                cur.execute(
                    "SELECT DISTINCT domain FROM Files ORDER BY domain"
                )
                domains = [r[0] for r in cur.fetchall() if r[0]]
                cur.execute("SELECT COUNT(*) FROM Files WHERE flags=1")
                total = cur.fetchone()[0]
                return domains, total

            domains, total = self._query_manifest(load_initial)
            self.root.after(0, self._on_decrypt_success, domains, total)

        except Exception as e:
            # Sanitize error message to avoid leaking password
            err_msg = str(e)
            if passphrase and passphrase in err_msg:
                err_msg = err_msg.replace(passphrase, "****")
            self.root.after(0, self._on_decrypt_fail, err_msg)

    def _on_decrypt_success(self, domains, total_files):
        self.decrypt_btn.configure(state="normal")
        self.status_var.set(
            f"{'Decrypted' if self._opening_encrypted else 'Opened'}! "
            f"{total_files:,} files across {len(domains)} domains."
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

    def _on_decrypt_fail(self, error):
        self.decrypt_btn.configure(state="normal")
        err_lower = error.lower()
        if any(k in err_lower for k in ("password", "key", "decrypt")):
            self.status_var.set("Incorrect password. Please try again.")
            messagebox.showerror(
                "Decryption Failed",
                "The password is incorrect. Please try again.\n\n"
                "Tip: This is the password you set when enabling\n"
                "encrypted backups in iTunes or Finder.",
            )
        else:
            self.status_var.set(f"Error: {error}")
            messagebox.showerror("Error",
                                  f"Failed to load backup:\n{error}")

    # ── File loading ─────────────────────────────────────────

    def _on_domain_select(self, event):
        sel = self.domain_tree.selection()
        if not sel or not self.backup:
            return
        self.status_var.set("Loading files...")
        self.root.update_idletasks()
        threading.Thread(target=self._load_files, args=(sel[0],),
                          daemon=True).start()

    def _load_files(self, selected):
        try:
            if selected.startswith("__CAT__"):
                self.root.after(0, self._load_category_files, selected)
                return

            def query(cur):
                if selected == "__ALL__":
                    cur.execute(
                        "SELECT fileID, domain, relativePath, flags, file "
                        "FROM Files WHERE flags=1 LIMIT 10000"
                    )
                else:
                    cur.execute(
                        "SELECT fileID, domain, relativePath, flags, file "
                        "FROM Files WHERE domain=? AND flags=1 LIMIT 10000",
                        (selected,),
                    )
                return cur.fetchall()

            rows = self._query_manifest(query)
            file_data = self._parse_file_rows(rows)
            self.root.after(0, self._populate_files, file_data)

        except Exception as e:
            self.root.after(
                0, lambda: self.status_var.set(f"Error loading: {e}")
            )

    def _load_category_files(self, cat_id):
        children = self.domain_tree.get_children(cat_id)
        if not children:
            return
        domains = list(children)
        placeholders = ",".join("?" * len(domains))
        threading.Thread(target=self._query_domains,
                          args=(domains, placeholders), daemon=True).start()

    def _query_domains(self, domains, placeholders):
        try:
            def query(cur):
                cur.execute(
                    f"SELECT fileID, domain, relativePath, flags, file "
                    f"FROM Files WHERE domain IN ({placeholders}) "
                    f"AND flags=1 LIMIT 10000",
                    domains,
                )
                return cur.fetchall()

            rows = self._query_manifest(query)
            file_data = self._parse_file_rows(rows)
            self.root.after(0, self._populate_files, file_data)
        except Exception as e:
            self.root.after(
                0, lambda: self.status_var.set(f"Error: {e}")
            )

    def _parse_file_rows(self, rows):
        """Parse raw DB rows into display-friendly tuples."""
        file_data = []
        for row in rows:
            file_id, domain, rel_path, flags, file_blob = row
            size_str = ""
            mod_str = ""
            if file_blob:
                try:
                    meta = plistlib.loads(file_blob)
                    objects = meta.get("$objects", [])
                    if isinstance(objects, list) and len(objects) > 1:
                        obj1 = objects[1]
                        if isinstance(obj1, dict):
                            size = obj1.get("Size", 0)
                            if size:
                                size_str = self._format_size(size)
                    for obj in (objects if isinstance(objects, list) else []):
                        if isinstance(obj, dict) and "LastModified" in obj:
                            mod_str = datetime.fromtimestamp(
                                obj["LastModified"]
                            ).strftime("%Y-%m-%d %H:%M")
                            break
                except Exception:
                    pass
            file_data.append(
                (file_id, domain, rel_path or "", size_str, mod_str)
            )
        return file_data

    def _populate_files(self, file_data):
        self.file_tree.delete(*self.file_tree.get_children())
        self.file_list = file_data

        seen_ids = set()
        for fd in file_data:
            file_id, domain, rel_path, size_str, mod_str = fd
            # Avoid duplicate iid crash
            uid = file_id
            if uid in seen_ids:
                uid = f"{file_id}_{domain}_{rel_path}"
            seen_ids.add(uid)
            self.file_tree.insert("", "end", iid=uid,
                                   values=(domain, rel_path, size_str, mod_str))

        self.count_var.set(f"{len(file_data):,} files")
        self.status_var.set(f"Loaded {len(file_data):,} files.")

    def _filter_files(self):
        query = self.search_var.get().lower().strip()
        self.file_tree.delete(*self.file_tree.get_children())

        count = 0
        for fd in self.file_list:
            file_id, domain, rel_path, size_str, mod_str = fd
            if query in domain.lower() or query in rel_path.lower():
                self.file_tree.insert("", "end", iid=file_id,
                                       values=(domain, rel_path, size_str,
                                               mod_str))
                count += 1

        self.count_var.set(f"{count:,} files")

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
        dest = filedialog.askdirectory(title="Select Output Folder")
        if not dest:
            return
        self.status_var.set(f"Extracting {len(file_ids)} files...")
        self.root.update_idletasks()
        threading.Thread(target=self._extract_thread,
                          args=(file_ids, dest), daemon=True).start()

    def _extract_thread(self, file_ids, dest):
        extracted = 0
        errors = 0
        skipped = 0
        real_dest = os.path.realpath(dest)

        # Build lookup dict for O(1) access
        lookup = {fd[0]: fd for fd in self.file_list}

        for fid in file_ids:
            try:
                info = lookup.get(fid)
                if not info:
                    errors += 1
                    continue

                file_id, domain, rel_path, _, _ = info
                if not rel_path:
                    skipped += 1
                    continue

                out_path = os.path.join(dest, domain, rel_path)

                # Path traversal protection — ensure output stays inside dest
                real_out = os.path.realpath(out_path)
                if not real_out.startswith(real_dest + os.sep):
                    skipped += 1
                    continue

                os.makedirs(os.path.dirname(out_path), exist_ok=True)

                try:
                    self.backup.extract_file(
                        relative_path=rel_path, domain=domain,
                        output_filename=out_path,
                    )
                    extracted += 1
                except Exception:
                    src = os.path.join(self.backup_dir, file_id[:2], file_id)
                    if os.path.exists(src):
                        shutil.copy2(src, out_path)
                        extracted += 1
                    else:
                        errors += 1
            except Exception:
                errors += 1

        msg = f"Extracted {extracted:,} files to {dest}"
        if errors:
            msg += f" ({errors} errors)"
        if skipped:
            msg += f" ({skipped} skipped)"
        self.root.after(0, lambda: self.status_var.set(msg))
        self.root.after(0, lambda: messagebox.showinfo("Done", msg))

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _format_size(size):
        for unit in ("B", "KB", "MB"):
            if size < 1024:
                return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
            size /= 1024
        return f"{size:.2f} GB"


def main():
    root = tk.Tk()
    root.iconname(APP_NAME)
    BackupLens(root)
    root.mainloop()


if __name__ == "__main__":
    main()
