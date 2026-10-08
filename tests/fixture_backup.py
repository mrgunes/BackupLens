"""Build a small, genuinely encrypted iOS backup for tests.

The layout follows what ``iphone_backup_decrypt`` reads: a Manifest.plist
holding a passphrase-protected key bag, an AES-encrypted Manifest.db, and
one AES-encrypted blob per file under ``<id[:2]>/<id>``. Nothing here is
derived from a real device.
"""

import hashlib
import os
import plistlib
import sqlite3
import struct
import tempfile

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

PASSPHRASE = "correct horse battery staple"

# Protection classes used by the fixture. Class 4 is "no protection",
# which is what real backups use for the Manifest key.
MANIFEST_CLASS = 4
FILE_CLASS = 3

NOTES_DOMAIN = "AppDomainGroup-group.com.apple.notes"
NOTES_MOV = ("Accounts/00000000-0000-0000-0000-00000000000A/Media/"
             "00000000-0000-0000-0000-00000000000B/1_00000000-0000-0000-0000-00000000000C/"
             "voice_memo_01-audio.MOV")


def _tlv(tag, data):
    return tag + struct.pack(">L", len(data)) + data


def _u32(value):
    return struct.pack(">L", value)


def _cbc_encrypt(key, data):
    cipher = AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16)
    return cipher.encrypt(pad(data, 16))


def _wrap(kek, key):
    return AES.new(kek, AES.MODE_KW).seal(key)


def _file_record(size, mtime, protection_class, wrapped_key=None):
    """An NSKeyedArchiver-style blob like Manifest.db's ``file`` column."""
    record = {
        "Size": size,
        "LastModified": mtime,
        "Birth": mtime,
        "Mode": 0o100644,
        "ProtectionClass": protection_class,
    }
    objects = ["$null", record]
    if wrapped_key is not None:
        record["EncryptionKey"] = plistlib.UID(2)
        objects.append({"NS.data": struct.pack("<L", protection_class)
                        + wrapped_key})
    return plistlib.dumps(
        {"$archiver": "NSKeyedArchiver", "$objects": objects,
         "$top": {"root": plistlib.UID(1)}, "$version": 100000},
        fmt=plistlib.FMT_BINARY,
    )


def file_id_for(domain, relative_path):
    return hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()


# (domain, relative_path, content)  -- content None = no data in the backup
DEFAULT_FILES = [
    # Mirrors the real-world case: a large (multi-chunk) QuickTime file.
    (NOTES_DOMAIN, NOTES_MOV,
     b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00qt  " + os.urandom(2_500_000)),
    ("HomeDomain", "Library/Preferences/plain.txt", b"hello from the phone"),
    ("HomeDomain", "Library/Notes/what?:file*.txt", b"windows-hostile name"),
    ("HomeDomain", "Library/empty.txt", b""),
    # Two domains that differ only where a '_' LIKE wildcard matches '-'.
    # '-' sorts before '_', so a naive LIKE lookup of the first returns
    # the second one's data.
    ("AppDomain-com.a_c", "Documents/data.txt", b"data of a_c"),
    ("AppDomain-com.a-c", "Documents/data.txt", b"data of a-c"),
    # Deep path: exceeds 260 characters once joined to an output folder.
    (NOTES_DOMAIN, "/".join(["d" * 90, "e" * 90, "f" * 40, "deep.bin"]),
     b"deep file"),
    # Listed in the manifest but its data is absent from the backup.
    ("HomeDomain", "Library/missing.bin", None),
]


def build_backup(parent_dir, files=None, passphrase=PASSPHRASE):
    """Create the backup under *parent_dir*; return ``(backup_dir, ids)``.

    ``ids`` maps ``(domain, relative_path)`` to the file ID.
    """
    files = DEFAULT_FILES if files is None else files
    backup_dir = os.path.join(parent_dir, "00000000-0000000000000000")
    os.makedirs(backup_dir)

    # Key bag, protected by a key derived from the passphrase.
    dpsl, salt = os.urandom(20), os.urandom(20)
    dpic, iterations = 10, 10
    round1 = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), dpsl, dpic, 32)
    passphrase_key = hashlib.pbkdf2_hmac("sha1", round1, salt, iterations, 32)
    class_keys = {c: os.urandom(32) for c in (FILE_CLASS, MANIFEST_CLASS)}

    keybag = (_tlv(b"VERS", _u32(4)) + _tlv(b"TYPE", _u32(1))
              + _tlv(b"UUID", os.urandom(16)) + _tlv(b"WRAP", _u32(1))
              + _tlv(b"SALT", salt) + _tlv(b"ITER", _u32(iterations))
              + _tlv(b"DPIC", _u32(dpic)) + _tlv(b"DPSL", dpsl))
    for protection_class, class_key in class_keys.items():
        keybag += (_tlv(b"UUID", os.urandom(16))
                   + _tlv(b"CLAS", _u32(protection_class))
                   + _tlv(b"WRAP", _u32(3)) + _tlv(b"KTYP", _u32(0))
                   + _tlv(b"WPKY", _wrap(passphrase_key, class_key)))

    manifest_key = os.urandom(32)
    wrapped_manifest_key = _wrap(class_keys[MANIFEST_CLASS], manifest_key)
    with open(os.path.join(backup_dir, "Manifest.plist"), "wb") as handle:
        plistlib.dump({
            "IsEncrypted": True,
            "BackupKeyBag": keybag,
            "ManifestKey": struct.pack("<l", MANIFEST_CLASS)
                           + wrapped_manifest_key,
            "Version": "10.0",
        }, handle)

    # Manifest.db, built in plaintext then encrypted.
    ids = {}
    with tempfile.TemporaryDirectory() as scratch:
        plain_db = os.path.join(scratch, "Manifest.db")
        conn = sqlite3.connect(plain_db)
        conn.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, "
                     "domain TEXT, relativePath TEXT, flags INTEGER, "
                     "file BLOB)")
        mtime = 1_700_000_000  # arbitrary fixed timestamp
        for domain, rel_path, content in files:
            file_id = file_id_for(domain, rel_path)
            ids[(domain, rel_path)] = file_id
            size = 100 if content is None else len(content)
            if size == 0:
                record = _file_record(0, mtime, FILE_CLASS)
            else:
                file_key = os.urandom(32)
                record = _file_record(
                    size, mtime, FILE_CLASS,
                    _wrap(class_keys[FILE_CLASS], file_key))
                if content is not None:
                    blob_dir = os.path.join(backup_dir, file_id[:2])
                    os.makedirs(blob_dir, exist_ok=True)
                    with open(os.path.join(blob_dir, file_id), "wb") as out:
                        out.write(_cbc_encrypt(file_key, content))
            conn.execute("INSERT INTO Files VALUES (?, ?, ?, 1, ?)",
                         (file_id, domain, rel_path, record))
        # A folder entry (flags=2) is not an extractable file.
        conn.execute("INSERT INTO Files VALUES (?, ?, ?, 2, ?)",
                     (file_id_for("HomeDomain", "Library"), "HomeDomain",
                      "Library", _file_record(0, mtime, FILE_CLASS)))
        conn.commit()
        conn.close()
        with open(plain_db, "rb") as handle:
            encrypted_db = _cbc_encrypt(manifest_key, handle.read())
    with open(os.path.join(backup_dir, "Manifest.db"), "wb") as handle:
        handle.write(encrypted_db)

    return backup_dir, ids


def content_of(domain, relative_path):
    for d, r, content in DEFAULT_FILES:
        if (d, r) == (domain, relative_path):
            return content
    raise KeyError((domain, relative_path))
