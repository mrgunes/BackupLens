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
import threading
import os
import plistlib
import platform
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime

FUSE_SUPPORTED_PLATFORMS = ("Linux", "Darwin")

__version__ = "1.0.0"
APP_NAME = "BackupLens"


class PlainBackup:
    """Read-only access to a non-encrypted iOS backup.

    Mirrors the subset of EncryptedBackup's interface used by this app
    (manifest_db_cursor / extract_file) so callers don't need to branch
    on whether a backup is encrypted.
    """

    def __init__(self, *, backup_directory):
        self._backup_directory = backup_directory
        _manifest_db_path = os.path.expanduser(os.path.join(backup_directory, "Manifest.db"))
        self._manifest_db_uri = f"file:{_manifest_db_path}?mode=ro&immutable=1"


    @contextmanager
    def manifest_db_cursor(self):
        conn = sqlite3.connect(self._manifest_db_uri, uri=True)
        try:
            cur = conn.cursor()
            yield cur
            cur.close()
        finally:
            conn.close()

    def extract_file(self, *, relative_path, domain, output_filename):
        with self.manifest_db_cursor() as cur:
            cur.execute(
                "SELECT fileID FROM Files WHERE relativePath=? AND domain=? "
                "AND flags=1 LIMIT 1",
                (relative_path, domain),
            )
            row = cur.fetchone()
        if not row:
            raise FileNotFoundError(relative_path)
        file_id = row[0]
        src = os.path.join(self._backup_directory, file_id[:2], file_id)
        shutil.copy2(src, output_filename)


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

        self.fuse_mountpoint = None
        self.fuse_cache_dir = None
        self.fuse_thread = None
        self.mount_btn = None

        self._apply_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
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
        path_entry = ttk.Entry(row1, textvariable=self.path_var, width=75)
        path_entry.pack(side="left", padx=8)
        ttk.Button(row1, text="Browse...",
                    command=self._browse_folder).pack(side="left")

        row2 = ttk.Frame(conn_frame)
        row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="Password (if encrypted):").pack(side="left")
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(row2, textvariable=self.pass_var,
                                     show="*", width=40)
        self.pass_entry.pack(side="left", padx=8)
        self.pass_entry.bind("<Return>", lambda e: self._decrypt())

        self.decrypt_btn = ttk.Button(row2, text="Open Backup",
                                       command=self._decrypt, style="Safe.TButton")
        self.decrypt_btn.pack(side="left", padx=8)

        self.status_var = tk.StringVar(
            value="Select a backup folder to begin. Enter a password "
                  "only if the backup is encrypted."
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
        ttk.Button(toolbar, text="Extract Entire Backup",
                    command=self._extract_full_backup).pack(side="right", padx=6)
        if platform.system() in FUSE_SUPPORTED_PLATFORMS:
            self.mount_btn = ttk.Button(
                toolbar, text="Mount as Filesystem (FUSE)",
                command=self._toggle_fuse_mount,
            )
            self.mount_btn.pack(side="right", padx=6)

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
                self.path_var.set(subs[0])
                self.status_var.set(
                    f"Auto-detected backup: {os.path.basename(subs[0])}. "
                    "Enter your password to decrypt."
                )

    # ── Folder browser ───────────────────────────────────────

    def _browse_folder(self):
        initial = self.BACKUP_PATHS.get(platform.system(), "")
        path = filedialog.askdirectory(
            title="Select iPhone/iPad Backup Folder",
            initialdir=initial if os.path.isdir(initial) else None,
        )
        if path:
            self.path_var.set(path)

    # ── Decryption ───────────────────────────────────────────

    @staticmethod
    def _is_backup_encrypted(backup_dir):
        """Inspect Manifest.plist to determine if the backup is encrypted."""
        manifest_plist_path = os.path.join(backup_dir, "Manifest.plist")
        try:
            with open(manifest_plist_path, "rb") as f:
                manifest = plistlib.load(f)
            return bool(manifest.get("IsEncrypted", False))
        except Exception:
            # If we can't tell, assume encrypted so the user isn't
            # silently handed a decryption failure with no explanation.
            return True

    def _decrypt(self):
        backup_dir = self.path_var.get().strip()
        passphrase = self.pass_var.get().strip()

        if not backup_dir or not os.path.isdir(backup_dir):
            messagebox.showerror("Error", "Please select a valid backup folder.")
            return

        encrypted = self._is_backup_encrypted(backup_dir)
        if encrypted and not passphrase:
            messagebox.showerror(
                "Error",
                "This backup is encrypted. Please enter the backup "
                "encryption password.",
            )
            return

        self.decrypt_btn.configure(state="disabled")
        self.status_var.set(
            "Decrypting... this may take a moment." if encrypted
            else "Opening backup..."
        )
        self.root.update_idletasks()

        threading.Thread(target=self._decrypt_thread,
                          args=(backup_dir, passphrase, encrypted),
                          daemon=True).start()

    def _query_manifest(self, callback, *args):
        """Execute a callback with a manifest DB cursor (context-managed)."""
        with self.backup.manifest_db_cursor() as cur:
            return callback(cur, *args)

    def _decrypt_thread(self, backup_dir, passphrase, encrypted):
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
            f"Loaded! {total_files:,} files across {len(domains)} domains."
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

    def _extract_full_backup(self):
        """Extract every file in the backup, preserving domain/relativePath.

        Unlike Extract Selected / Extract All in View, this reads straight
        from the manifest instead of the (10,000-row-capped) UI file list,
        so nothing is left out for large backups.
        """
        if not self.backup:
            messagebox.showinfo("Info", "Open a backup first.")
            return
        dest = filedialog.askdirectory(
            title="Select Output Folder for Full Backup Extraction"
        )
        if not dest:
            return
        if not messagebox.askyesno(
            "Confirm",
            "This will extract every file in the backup into "
            "domain/relativePath folders (e.g. "
            "HomeDomain/Library/SMS/sms.db), matching the layout tools "
            "like iphone-backup-tools expect. This may take a while and "
            "use significant disk space. Continue?",
        ):
            return
        self.status_var.set("Extracting entire backup...")
        self.root.update_idletasks()
        threading.Thread(target=self._extract_full_thread, args=(dest,),
                          daemon=True).start()

    def _extract_full_thread(self, dest):
        real_dest = os.path.realpath(dest)

        try:
            with self.backup.manifest_db_cursor() as cur:
                cur.execute(
                    "SELECT fileID, domain, relativePath FROM Files "
                    "WHERE flags=1"
                )
                rows = cur.fetchall()
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"Error: {e}"))
            self.root.after(0, lambda: messagebox.showerror(
                "Error", f"Failed to read backup manifest:\n{e}"
            ))
            return

        total = len(rows)
        extracted = 0
        errors = 0
        skipped = 0

        for i, (file_id, domain, rel_path) in enumerate(rows, start=1):
            try:
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

            if i % 200 == 0 or i == total:
                done = i
                self.root.after(0, lambda d=done, t=total: self.status_var.set(
                    f"Extracting entire backup... {d:,}/{t:,} files"
                ))

        msg = f"Extracted {extracted:,} of {total:,} files to {dest}"
        if errors:
            msg += f" ({errors} errors)"
        if skipped:
            msg += f" ({skipped} skipped)"

        sms_path = os.path.join(dest, "HomeDomain", "Library", "SMS", "sms.db")
        addr_path = os.path.join(
            dest, "HomeDomain", "Library", "AddressBook", "AddressBook.sqlitedb"
        )
        if os.path.exists(sms_path):
            msg += (
                "\n\nTo browse messages with iphone-backup-tools:\n"
                f'  python message_viewer.py "{sms_path}"'
            )
            if os.path.exists(addr_path):
                msg += f' --addressbook "{addr_path}"'

        self.root.after(0, lambda: self.status_var.set(
            f"Extracted {extracted:,} of {total:,} files to {dest}"
        ))
        self.root.after(0, lambda: messagebox.showinfo(
            "Extraction Complete", msg
        ))

    # ── FUSE mount ───────────────────────────────────────────

    def _toggle_fuse_mount(self):
        if self.fuse_mountpoint:
            self._unmount_fuse()
        else:
            self._mount_fuse()

    def _mount_fuse(self):
        if not self.backup:
            messagebox.showinfo("Info", "Open a backup first.")
            return
        try:
            import backuplens_fuse
        except Exception as e:
            messagebox.showerror(
                "Missing Dependency",
                "Mounting requires the 'fusepy' package and libfuse.\n\n"
                "Install with:\n  pip install fusepy\n\n"
                "Linux: libfuse2/libfuse3 is usually preinstalled.\n"
                "macOS: install macFUSE from https://osxfuse.github.io/\n\n"
                f"Details: {e}",
            )
            return

        mountpoint = filedialog.askdirectory(
            title="Select an EMPTY folder to mount the backup on"
        )
        if not mountpoint:
            return
        if os.listdir(mountpoint):
            messagebox.showerror(
                "Error", "The mount point folder must be empty."
            )
            return

        cache_dir = tempfile.mkdtemp(prefix="backuplens_fuse_cache_")
        mount_time = time.time()

        def run():
            try:
                backuplens_fuse.mount(
                    self.backup, mountpoint, cache_dir, mount_time,
                    foreground=True,
                )
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Mount Failed", str(e)
                ))
            finally:
                self.root.after(0, self._on_fuse_stopped)

        self.fuse_mountpoint = mountpoint
        self.fuse_cache_dir = cache_dir
        self.fuse_thread = threading.Thread(target=run, daemon=True)
        self.fuse_thread.start()
        self.mount_btn.configure(text="Unmount Filesystem")
        self.status_var.set(f"Mounted at {mountpoint}")

    def _unmount_syscall(self, mountpoint):
        if platform.system() == "Darwin":
            subprocess.run(["umount", mountpoint], check=True)
        else:
            cmd = shutil.which("fusermount3") or shutil.which("fusermount") \
                or "umount"
            subprocess.run([cmd, "-u", mountpoint], check=True)

    def _unmount_fuse(self):
        if not self.fuse_mountpoint:
            return
        self.status_var.set("Unmounting...")
        self.root.update_idletasks()
        try:
            self._unmount_syscall(self.fuse_mountpoint)
        except Exception as e:
            messagebox.showerror("Unmount Failed", str(e))
            self.status_var.set("Unmount failed. See error above.")
            return
        # The blocking FUSE() call in the mount thread returns once the
        # kernel finishes the unmount; _on_fuse_stopped resets UI state then.

    def _on_fuse_stopped(self):
        cache_dir = self.fuse_cache_dir
        self.fuse_mountpoint = None
        self.fuse_cache_dir = None
        self.fuse_thread = None
        if cache_dir:
            shutil.rmtree(cache_dir, ignore_errors=True)
        if self.mount_btn:
            self.mount_btn.configure(text="Mount as Filesystem (FUSE)")
        self.status_var.set("Filesystem unmounted.")

    def _on_close(self):
        if self.fuse_mountpoint:
            try:
                self._unmount_syscall(self.fuse_mountpoint)
            except Exception:
                pass
            if self.fuse_cache_dir:
                shutil.rmtree(self.fuse_cache_dir, ignore_errors=True)
        self.root.destroy()

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
