import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path("/var/lib/tapebox/catalog.db")


def utc_now():
    """
    Return current UTC time in ISO-8601 format.
    """
    return datetime.now(timezone.utc).isoformat()


def connect():
    """
    Open the TapeBox SQLite database.
    """
    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = sqlite3.connect(
        DB_PATH,
    )

    connection.row_factory = sqlite3.Row

    connection.execute(
        "PRAGMA foreign_keys = ON"
    )

    connection.execute(
        "PRAGMA journal_mode = WAL"
    )

    return connection


def initialize_database():
    """
    Create the TapeBox catalog schema if it does not exist.
    """

    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tapes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL UNIQUE,
                barcode TEXT,
                ltfs_uuid TEXT,
                generation INTEGER,
                capacity_bytes INTEGER,
                used_bytes INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'available',
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                notes TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_tapes_ltfs_uuid
            ON tapes(ltfs_uuid)
            WHERE ltfs_uuid IS NOT NULL;

            CREATE TABLE IF NOT EXISTS archive_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                total_files INTEGER DEFAULT 0,
                total_bytes INTEGER DEFAULT 0,
                bytes_written INTEGER DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                archive_job_id INTEGER,
                original_path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                checksum_sha256 TEXT,
                tape_id INTEGER,
                tape_path TEXT,
                is_spanned INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT,
                verified_at TEXT,

                FOREIGN KEY (archive_job_id)
                    REFERENCES archive_jobs(id),

                FOREIGN KEY (tape_id)
                    REFERENCES tapes(id)
            );

            CREATE TABLE IF NOT EXISTS file_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                part_number INTEGER NOT NULL,
                tape_id INTEGER NOT NULL,
                tape_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT,

                FOREIGN KEY (file_id)
                    REFERENCES files(id)
                    ON DELETE CASCADE,

                FOREIGN KEY (tape_id)
                    REFERENCES tapes(id),

                UNIQUE(file_id, part_number)
            );

            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                archive_job_id INTEGER,
                event_type TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL,

                FOREIGN KEY (archive_job_id)
                    REFERENCES archive_jobs(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_files_filename
            ON files(filename);

            CREATE INDEX IF NOT EXISTS idx_files_original_path
            ON files(original_path);

            CREATE INDEX IF NOT EXISTS idx_files_tape_id
            ON files(tape_id);

            CREATE INDEX IF NOT EXISTS idx_file_parts_file_id
            ON file_parts(file_id);
            """
        )


def add_tape(label):
    """
    Add a tape manually to the catalog.
    """

    label = label.strip().upper()

    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO tapes (
                label,
                created_at
            )
            VALUES (?, ?)
            """,
            (
                label,
                utc_now(),
            ),
        )

        return cursor.lastrowid


def list_tapes():
    """
    Return every cataloged tape.
    """

    with connect() as db:
        rows = db.execute(
            """
            SELECT *
            FROM tapes
            ORDER BY id
            """
        ).fetchall()

    return rows


def get_tape_by_id(tape_id):
    """
    Return a tape by database ID.
    """

    with connect() as db:
        return db.execute(
            """
            SELECT *
            FROM tapes
            WHERE id = ?
            """,
            (tape_id,),
        ).fetchone()


def get_tape_by_label(label):
    """
    Return a tape by friendly TapeBox label.
    """

    if not label:
        return None

    with connect() as db:
        return db.execute(
            """
            SELECT *
            FROM tapes
            WHERE label = ?
            """,
            (
                label.strip().upper(),
            ),
        ).fetchone()


def get_tape_by_uuid(ltfs_uuid):
    """
    Return a tape by permanent LTFS UUID.
    """

    if not ltfs_uuid:
        return None

    with connect() as db:
        return db.execute(
            """
            SELECT *
            FROM tapes
            WHERE ltfs_uuid = ?
            """,
            (
                ltfs_uuid.strip(),
            ),
        ).fetchone()


def reconcile_loaded_tape(
    label,
    ltfs_uuid,
    generation=None,
    capacity_bytes=None,
    used_bytes=None,
    barcode=None,
):
    """
    Match a loaded LTFS cartridge against the TapeBox catalog.

    Match order:
      1. LTFS UUID
      2. Existing TapeBox label

    Unknown tapes are not automatically registered.

    Existing manually-added tapes can have their real LTFS UUID
    attached when the label matches and no UUID is already stored.
    """

    label = (
        label.strip().upper()
        if label
        else None
    )

    ltfs_uuid = (
        ltfs_uuid.strip()
        if ltfs_uuid
        else None
    )

    barcode = (
        barcode.strip()
        if barcode
        else None
    )

    now = utc_now()

    with connect() as db:

        #
        # Strongest identity:
        # permanent LTFS UUID.
        #

        tape = None

        if ltfs_uuid:
            tape = db.execute(
                """
                SELECT *
                FROM tapes
                WHERE ltfs_uuid = ?
                """,
                (ltfs_uuid,),
            ).fetchone()

        if tape:
            db.execute(
                """
                UPDATE tapes
                SET
                    barcode = COALESCE(?, barcode),
                    generation = COALESCE(?, generation),
                    capacity_bytes = COALESCE(?, capacity_bytes),
                    used_bytes = COALESCE(?, used_bytes),
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    barcode,
                    generation,
                    capacity_bytes,
                    used_bytes,
                    now,
                    tape["id"],
                ),
            )

            updated = db.execute(
                """
                SELECT *
                FROM tapes
                WHERE id = ?
                """,
                (tape["id"],),
            ).fetchone()

            return {
                "state": "registered",
                "matched_by": "uuid",
                "tape": updated,
            }

        #
        # UUID not known yet.
        # Try the friendly TapeBox label.
        #

        if label:
            tape = db.execute(
                """
                SELECT *
                FROM tapes
                WHERE label = ?
                """,
                (label,),
            ).fetchone()

        if tape:
            existing_uuid = tape["ltfs_uuid"]

            #
            # Same label but a different physical cartridge.
            # Never overwrite an already-known UUID.
            #

            if (
                existing_uuid
                and ltfs_uuid
                and existing_uuid != ltfs_uuid
            ):
                return {
                    "state": "conflict",
                    "matched_by": "label",
                    "tape": tape,
                    "message": (
                        f"Catalog label {label} is already bound "
                        f"to LTFS UUID {existing_uuid}, but loaded "
                        f"cartridge UUID is {ltfs_uuid}."
                    ),
                }

            #
            # Existing manually-added tape.
            # Attach its actual LTFS identity.
            #

            db.execute(
                """
                UPDATE tapes
                SET
                    ltfs_uuid = COALESCE(ltfs_uuid, ?),
                    barcode = COALESCE(?, barcode),
                    generation = COALESCE(?, generation),
                    capacity_bytes = COALESCE(?, capacity_bytes),
                    used_bytes = COALESCE(?, used_bytes),
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    ltfs_uuid,
                    barcode,
                    generation,
                    capacity_bytes,
                    used_bytes,
                    now,
                    tape["id"],
                ),
            )

            updated = db.execute(
                """
                SELECT *
                FROM tapes
                WHERE id = ?
                """,
                (tape["id"],),
            ).fetchone()

            return {
                "state": "registered",
                "matched_by": "label",
                "tape": updated,
            }

        #
        # Foreign / unknown LTFS cartridge.
        #

        return {
            "state": "unregistered",
            "matched_by": None,
            "tape": None,
        }


def record_archived_file(
    original_path,
    relative_path,
    filename,
    size_bytes,
    sha256,
    tape_id,
    tape_path,
    archive_job_id=None,
):
    """
    Record a successfully archived single file.
    """

    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO files (
                archive_job_id,
                original_path,
                relative_path,
                filename,
                size_bytes,
                checksum_sha256,
                tape_id,
                tape_path,
                is_spanned,
                archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                archive_job_id,
                original_path,
                relative_path,
                filename,
                size_bytes,
                sha256,
                tape_id,
                tape_path,
                utc_now(),
            ),
        )

        return cursor.lastrowid



def get_files_by_tape(tape_id):
    """
    Return all cataloged files stored on one TapeBox cartridge.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                archive_job_id,
                original_path,
                relative_path,
                filename,
                size_bytes,
                checksum_sha256,
                tape_id,
                tape_path,
                is_spanned,
                archived_at,
                verified_at
            FROM files
            WHERE tape_id = ?
            ORDER BY id
            """,
            (tape_id,),
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


def list_files():
    """
    Return archived files with their tape information.
    """

    with connect() as db:
        return db.execute(
            """
            SELECT
                files.id,
                files.filename,
                files.original_path,
                files.relative_path,
                files.size_bytes,
                files.checksum_sha256,
                files.tape_path,
                files.is_spanned,
                files.archived_at,
                tapes.id AS tape_id,
                tapes.label AS tape_label,
                tapes.ltfs_uuid
            FROM files
            LEFT JOIN tapes
                ON tapes.id = files.tape_id
            ORDER BY files.id
            """
        ).fetchall()


def get_file_by_id(file_id):
    """
    Return one archived file with its tape information.
    """

    with connect() as db:
        return db.execute(
            """
            SELECT
                files.id,
                files.archive_job_id,
                files.original_path,
                files.relative_path,
                files.filename,
                files.size_bytes,
                files.checksum_sha256,
                files.tape_id,
                files.tape_path,
                files.is_spanned,
                files.archived_at,
                files.verified_at,
                tapes.label AS tape_label,
                tapes.ltfs_uuid
            FROM files
            LEFT JOIN tapes
                ON tapes.id = files.tape_id
            WHERE files.id = ?
            """,
            (file_id,),
        ).fetchone()


def mark_file_verified(file_id):
    """
    Record successful read-back verification time.
    """

    with connect() as db:
        db.execute(
            """
            UPDATE files
            SET verified_at = ?
            WHERE id = ?
            """,
            (
                utc_now(),
                file_id,
            ),
        )


def create_archive_job(
    source_path,
    total_files,
    total_bytes,
):
    """
    Create a resumable archive job.
    """

    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO archive_jobs (
                source_path,
                status,
                total_files,
                total_bytes,
                bytes_written,
                started_at
            )
            VALUES (?, 'running', ?, ?, 0, ?)
            """,
            (
                source_path,
                total_files,
                total_bytes,
                utc_now(),
            ),
        )

        return cursor.lastrowid


def get_archive_job(job_id):
    """
    Return one archive job.
    """

    with connect() as db:
        return db.execute(
            """
            SELECT
                id,
                source_path,
                status,
                total_files,
                total_bytes,
                bytes_written,
                started_at,
                completed_at,
                error
            FROM archive_jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()


def get_archive_job_files(job_id):
    """
    Return files already completed by an archive job.
    """

    with connect() as db:
        return db.execute(
            """
            SELECT
                id,
                relative_path,
                filename,
                size_bytes,
                checksum_sha256,
                tape_id,
                tape_path
            FROM files
            WHERE archive_job_id = ?
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()


def update_archive_job(
    job_id,
    status=None,
    bytes_written=None,
    completed=False,
    error=None,
):
    """
    Update archive job progress/state.
    """

    fields = []
    values = []

    if status is not None:
        fields.append("status = ?")
        values.append(status)

    if bytes_written is not None:
        fields.append("bytes_written = ?")
        values.append(bytes_written)

    if completed:
        fields.append("completed_at = ?")
        values.append(utc_now())

    if error is not None:
        fields.append("error = ?")
        values.append(error)

    if not fields:
        return

    values.append(job_id)

    with connect() as db:
        db.execute(
            f"""
            UPDATE archive_jobs
            SET {", ".join(fields)}
            WHERE id = ?
            """,
            values,
        )


def list_archive_jobs():
    """
    Return archive jobs newest first.
    """

    with connect() as db:
        return db.execute(
            """
            SELECT
                id,
                source_path,
                status,
                total_files,
                total_bytes,
                bytes_written,
                started_at,
                completed_at,
                error
            FROM archive_jobs
            ORDER BY id DESC
            """
        ).fetchall()


def import_tape_manifest_records(
    tape_metadata,
    manifest,
    generation=None,
):
    """
    Recover a TapeBox cartridge and its file records from the
    on-tape metadata.

    Safety rules:
      - LTFS UUID is the permanent cartridge identity.
      - Never replace an existing tape UUID.
      - Never overwrite an existing file record.
      - Exact existing file records are skipped.
      - Any conflicting file aborts the whole import.
      - archive_job_id is deliberately not restored here because the
        original archive_jobs table may no longer exist after DB loss.
    """

    label = str(
        tape_metadata["label"]
    ).strip().upper()

    ltfs_uuid = str(
        tape_metadata["ltfs_uuid"]
    ).strip()

    files = manifest.get(
        "files",
        [],
    )

    now = utc_now()

    with connect() as db:

        #
        # Find cartridge by permanent UUID first.
        #
        tape = db.execute(
            """
            SELECT *
            FROM tapes
            WHERE ltfs_uuid = ?
            """,
            (ltfs_uuid,),
        ).fetchone()

        if tape is None:
            tape_by_label = db.execute(
                """
                SELECT *
                FROM tapes
                WHERE label = ?
                """,
                (label,),
            ).fetchone()

            #
            # Same friendly label already belongs to another cartridge.
            #
            if (
                tape_by_label is not None
                and tape_by_label["ltfs_uuid"]
                and tape_by_label["ltfs_uuid"] != ltfs_uuid
            ):
                raise RuntimeError(
                    f"Tape label conflict: {label} is already "
                    f"assigned to LTFS UUID "
                    f"{tape_by_label['ltfs_uuid']}."
                )

            if tape_by_label is not None:
                db.execute(
                    """
                    UPDATE tapes
                    SET
                        ltfs_uuid = ?,
                        generation = COALESCE(generation, ?),
                        last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        ltfs_uuid,
                        generation,
                        now,
                        tape_by_label["id"],
                    ),
                )

                tape_id = tape_by_label["id"]
                tape_created = False

            else:
                cursor = db.execute(
                    """
                    INSERT INTO tapes (
                        label,
                        ltfs_uuid,
                        generation,
                        status,
                        created_at,
                        last_seen_at
                    )
                    VALUES (?, ?, ?, 'available', ?, ?)
                    """,
                    (
                        label,
                        ltfs_uuid,
                        generation,
                        now,
                        now,
                    ),
                )

                tape_id = cursor.lastrowid
                tape_created = True

        else:
            tape_id = tape["id"]
            tape_created = False

            db.execute(
                """
                UPDATE tapes
                SET
                    generation = COALESCE(generation, ?),
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    generation,
                    now,
                    tape_id,
                ),
            )

        #
        # First pass:
        # detect every conflict before inserting anything.
        #
        conflicts = []
        existing_count = 0
        missing = []

        for entry in files:
            tape_path = str(
                entry["tape_path"]
            )

            size_bytes = int(
                entry["size_bytes"]
            )

            sha256 = entry.get(
                "sha256"
            )

            existing = db.execute(
                """
                SELECT *
                FROM files
                WHERE tape_id = ?
                  AND tape_path = ?
                """,
                (
                    tape_id,
                    tape_path,
                ),
            ).fetchone()

            if existing is None:
                missing.append(
                    entry
                )
                continue

            same_size = (
                int(existing["size_bytes"])
                == size_bytes
            )

            same_hash = (
                existing["checksum_sha256"]
                == sha256
            )

            if same_size and same_hash:
                existing_count += 1
                continue

            conflicts.append(
                {
                    "tape_path": tape_path,
                    "catalog_size": existing["size_bytes"],
                    "manifest_size": size_bytes,
                    "catalog_sha256": existing["checksum_sha256"],
                    "manifest_sha256": sha256,
                }
            )

        if conflicts:
            paths = ", ".join(
                item["tape_path"]
                for item in conflicts
            )

            raise RuntimeError(
                "Catalog conflict detected. No recovery records "
                f"were imported. Conflicting path(s): {paths}"
            )

        #
        # Second pass:
        # insert only records that are genuinely missing.
        #
        imported_count = 0

        for entry in missing:
            relative_path = str(
                entry.get("relative_path")
                or entry["tape_path"].lstrip("/")
            )

            filename = str(
                entry.get("filename")
                or relative_path.rsplit("/", 1)[-1]
            )

            tape_path = str(
                entry["tape_path"]
            )

            archived_at = (
                entry.get("archived_at")
                or now
            )

            verified_at = entry.get(
                "verified_at"
            )

            is_spanned = (
                1
                if entry.get("is_spanned")
                else 0
            )

            recovered_original_path = (
                f"recovered://{label}/"
                f"{relative_path}"
            )

            db.execute(
                """
                INSERT INTO files (
                    archive_job_id,
                    original_path,
                    relative_path,
                    filename,
                    size_bytes,
                    checksum_sha256,
                    tape_id,
                    tape_path,
                    is_spanned,
                    archived_at,
                    verified_at
                )
                VALUES (
                    NULL,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    recovered_original_path,
                    relative_path,
                    filename,
                    int(entry["size_bytes"]),
                    entry.get("sha256"),
                    tape_id,
                    tape_path,
                    is_spanned,
                    archived_at,
                    verified_at,
                ),
            )

            imported_count += 1

        return {
            "tape_id": tape_id,
            "tape_created": tape_created,
            "label": label,
            "ltfs_uuid": ltfs_uuid,
            "files_in_manifest": len(files),
            "files_imported": imported_count,
            "files_existing": existing_count,
        }


BACKUP_DIR = Path("/var/lib/tapebox/backups")


def backup_catalog():
    """
    Create a consistent SQLite backup using SQLite's backup API.

    Returns a dict containing the backup path and size.
    """

    BACKUP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%d-%H%M%S")

    backup_path = (
        BACKUP_DIR
        / f"catalog-{timestamp}.db"
    )

    source = sqlite3.connect(
        DB_PATH,
    )

    destination = sqlite3.connect(
        backup_path,
    )

    try:
        source.backup(
            destination
        )

        destination.execute(
            "PRAGMA wal_checkpoint(FULL)"
        )

        destination.commit()

    finally:
        destination.close()
        source.close()

    size_bytes = backup_path.stat().st_size

    return {
        "success": True,
        "path": str(backup_path),
        "size_bytes": size_bytes,
    }


def validate_catalog_database(path):
    """
    Validate a TapeBox SQLite catalog before restore.
    """

    path = Path(path)

    if not path.is_file():
        return {
            "success": False,
            "error": f"Backup file does not exist: {path}",
        }

    try:
        db = sqlite3.connect(
            path,
        )

        db.row_factory = sqlite3.Row

        integrity = db.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        if integrity != "ok":
            return {
                "success": False,
                "error": (
                    "SQLite integrity check failed: "
                    f"{integrity}"
                ),
            }

        required_tables = {
            "tapes",
            "files",
            "archive_jobs",
            "file_parts",
            "job_events",
        }

        rows = db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()

        existing_tables = {
            row["name"]
            for row in rows
        }

        missing = sorted(
            required_tables
            - existing_tables
        )

        if missing:
            return {
                "success": False,
                "error": (
                    "Backup is missing required table(s): "
                    + ", ".join(missing)
                ),
            }

        tape_count = db.execute(
            "SELECT COUNT(*) FROM tapes"
        ).fetchone()[0]

        file_count = db.execute(
            "SELECT COUNT(*) FROM files"
        ).fetchone()[0]

        return {
            "success": True,
            "path": str(path),
            "tapes": tape_count,
            "files": file_count,
        }

    except sqlite3.Error as exc:
        return {
            "success": False,
            "error": str(exc),
        }

    finally:
        try:
            db.close()
        except Exception:
            pass


def restore_catalog(backup_path):
    """
    Restore the TapeBox catalog from a validated SQLite backup.

    A pre-restore backup of the current live catalog is created first.
    """

    backup_path = Path(
        backup_path
    )

    validation = validate_catalog_database(
        backup_path
    )

    if not validation.get(
        "success"
    ):
        return validation

    #
    # Protect the current live DB before touching it.
    #
    pre_restore = backup_catalog()

    #
    # Restore using SQLite's backup API rather than raw file copying.
    #
    source = sqlite3.connect(
        backup_path,
    )

    destination = sqlite3.connect(
        DB_PATH,
    )

    try:
        source.backup(
            destination
        )

        destination.commit()

        integrity = destination.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        if integrity != "ok":
            raise RuntimeError(
                "Restored database failed integrity check: "
                f"{integrity}"
            )

    finally:
        destination.close()
        source.close()

    return {
        "success": True,
        "restored_from": str(
            backup_path
        ),
        "pre_restore_backup": (
            pre_restore["path"]
        ),
        "tapes": validation["tapes"],
        "files": validation["files"],
    }
