import json
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
                original_created_at TEXT,
                original_modified_at TEXT,
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
                checksum_sha256 TEXT,

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

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS restore_operations (
                id TEXT PRIMARY KEY,
                operation_type TEXT NOT NULL,
                file_ids_json TEXT NOT NULL,
                destination TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                transfer_json TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_restore_operations_status
            ON restore_operations(status);

            CREATE INDEX IF NOT EXISTS idx_restore_operations_updated_at
            ON restore_operations(updated_at);

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

        #
        # Tape catalog metadata added after the original schema.
        # Existing catalogs are upgraded in place.
        #
        tape_columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(tapes)"
            ).fetchall()
        }

        if "friendly_name" not in tape_columns:
            db.execute(
                """
                ALTER TABLE tapes
                ADD COLUMN friendly_name TEXT
                """
            )

        if "location" not in tape_columns:
            db.execute(
                """
                ALTER TABLE tapes
                ADD COLUMN location TEXT
                """
            )


        #
        # Preserve original source filesystem timestamps.
        # Existing catalog rows remain NULL because their
        # original timestamps cannot be reconstructed safely.
        #
        file_columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(files)"
            ).fetchall()
        }

        if "original_created_at" not in file_columns:
            db.execute(
                """
                ALTER TABLE files
                ADD COLUMN original_created_at TEXT
                """
            )

        if "original_modified_at" not in file_columns:
            db.execute(
                """
                ALTER TABLE files
                ADD COLUMN original_modified_at TEXT
                """
            )



DEFAULT_SETTINGS = {
    "restore_directory": "/mnt/tapebox/restored",
    "staging_directory": "/mnt/tapebox/staging",
}


def get_setting(key, default=None):
    """
    Return one TapeBox setting.
    """

    with connect() as db:
        row = db.execute(
            """
            SELECT value
            FROM settings
            WHERE key = ?
            """,
            (key,),
        ).fetchone()

    if row is None:
        return default

    return row["value"]


def set_setting(key, value):
    """
    Create or update one TapeBox setting.
    """

    key = str(key).strip()
    value = str(value).strip()

    if not key:
        raise ValueError("Setting key cannot be empty")

    with connect() as db:
        db.execute(
            """
            INSERT INTO settings (
                key,
                value,
                updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(key)
            DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (
                key,
                value,
                utc_now(),
            ),
        )


def get_settings():
    """
    Return TapeBox settings with defaults applied.
    """

    settings = dict(DEFAULT_SETTINGS)

    with connect() as db:
        rows = db.execute(
            """
            SELECT key, value
            FROM settings
            """
        ).fetchall()

    for row in rows:
        settings[row["key"]] = row["value"]

    return settings


def initialize_default_settings():
    """
    Insert default settings when they do not already exist.
    """

    with connect() as db:
        for key, value in DEFAULT_SETTINGS.items():
            db.execute(
                """
                INSERT OR IGNORE INTO settings (
                    key,
                    value,
                    updated_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    key,
                    value,
                    utc_now(),
                ),
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


def update_tape_catalog_metadata(
    tape_id,
    friendly_name=None,
    location=None,
    notes=None,
):
    """
    Update human-readable TapeBox cartridge metadata.

    These fields do not alter the physical LTFS label or UUID.
    """

    friendly_name = (
        friendly_name.strip()
        if friendly_name
        else None
    )

    location = (
        location.strip()
        if location
        else None
    )

    notes = (
        notes.strip()
        if notes
        else None
    )

    with connect() as db:
        cursor = db.execute(
            """
            UPDATE tapes
            SET
                friendly_name = ?,
                location = ?,
                notes = ?
            WHERE id = ?
            """,
            (
                friendly_name,
                location,
                notes,
                tape_id,
            ),
        )

        if cursor.rowcount == 0:
            raise ValueError(
                "Tape not found."
            )


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


def register_existing_ltfs_tape(
    label,
    ltfs_uuid,
    generation=None,
    capacity_bytes=None,
    used_bytes=None,
    barcode=None,
    friendly_name=None,
    location=None,
    notes=None,
):
    """
    Explicitly register an existing LTFS cartridge.

    This is intentionally separate from reconcile_loaded_tape().
    Unknown cartridges must never be registered merely because
    they were inserted or inspected.

    LTFS UUID is the permanent physical cartridge identity.
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

    friendly_name = (
        friendly_name.strip()
        if friendly_name
        else None
    )

    location = (
        location.strip()
        if location
        else None
    )

    notes = (
        notes.strip()
        if notes
        else None
    )

    if not label:
        raise ValueError(
            "LTFS cartridge label is required."
        )

    if not ltfs_uuid:
        raise ValueError(
            "LTFS cartridge UUID is required."
        )

    now = utc_now()

    with connect() as db:

        existing_uuid = db.execute(
            """
            SELECT *
            FROM tapes
            WHERE ltfs_uuid = ?
            """,
            (ltfs_uuid,),
        ).fetchone()

        if existing_uuid:
            return {
                "state": "already_registered",
                "tape": existing_uuid,
            }

        existing_label = db.execute(
            """
            SELECT *
            FROM tapes
            WHERE label = ?
            """,
            (label,),
        ).fetchone()

        if existing_label:
            existing_ltfs_uuid = (
                existing_label["ltfs_uuid"]
            )

            if (
                existing_ltfs_uuid
                and existing_ltfs_uuid != ltfs_uuid
            ):
                return {
                    "state": "conflict",
                    "tape": existing_label,
                    "message": (
                        f"Catalog label {label} is already "
                        f"assigned to LTFS UUID "
                        f"{existing_ltfs_uuid}."
                    ),
                }

            #
            # This is an older/manual TapeBox catalog entry
            # that has never had its physical LTFS identity
            # attached. Claim it instead of creating a
            # duplicate row.
            #
            db.execute(
                """
                UPDATE tapes
                SET
                    ltfs_uuid = ?,
                    barcode = COALESCE(?, barcode),
                    generation = COALESCE(?, generation),
                    capacity_bytes = COALESCE(
                        ?,
                        capacity_bytes
                    ),
                    used_bytes = COALESCE(
                        ?,
                        used_bytes
                    ),
                    friendly_name = COALESCE(
                        ?,
                        friendly_name
                    ),
                    location = COALESCE(
                        ?,
                        location
                    ),
                    notes = COALESCE(
                        ?,
                        notes
                    ),
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    ltfs_uuid,
                    barcode,
                    generation,
                    capacity_bytes,
                    used_bytes,
                    friendly_name,
                    location,
                    notes,
                    now,
                    existing_label["id"],
                ),
            )

            tape = db.execute(
                """
                SELECT *
                FROM tapes
                WHERE id = ?
                """,
                (existing_label["id"],),
            ).fetchone()

            return {
                "state": "registered",
                "matched_existing_label": True,
                "tape": tape,
            }

        cursor = db.execute(
            """
            INSERT INTO tapes (
                label,
                barcode,
                ltfs_uuid,
                generation,
                capacity_bytes,
                used_bytes,
                status,
                created_at,
                last_seen_at,
                notes,
                friendly_name,
                location
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                label,
                barcode,
                ltfs_uuid,
                generation,
                capacity_bytes,
                used_bytes or 0,
                "available",
                now,
                now,
                notes,
                friendly_name,
                location,
            ),
        )

        tape_id = cursor.lastrowid

        tape = db.execute(
            """
            SELECT *
            FROM tapes
            WHERE id = ?
            """,
            (tape_id,),
        ).fetchone()

        return {
            "state": "registered",
            "matched_existing_label": False,
            "tape": tape,
        }



def _refresh_tape_used_bytes(db, tape_id):
    """
    Recalculate physical cataloged bytes stored on one tape.

    Normal files store their physical location in files.tape_id.

    Spanned files have files.tape_id = NULL and their physical
    tape usage is stored in file_parts.
    """

    if tape_id is None:
        return 0

    row = db.execute(
        """
        SELECT
            COALESCE(
                (
                    SELECT SUM(size_bytes)
                    FROM files
                    WHERE tape_id = ?
                ),
                0
            )
            +
            COALESCE(
                (
                    SELECT SUM(size_bytes)
                    FROM file_parts
                    WHERE tape_id = ?
                ),
                0
            ) AS used_bytes
        """,
        (
            tape_id,
            tape_id,
        ),
    ).fetchone()

    used_bytes = int(row["used_bytes"] or 0)

    db.execute(
        """
        UPDATE tapes
        SET used_bytes = ?
        WHERE id = ?
        """,
        (
            used_bytes,
            tape_id,
        ),
    )

    return used_bytes


def refresh_tape_used_bytes(tape_id):
    """
    Rebuild catalog usage for one registered tape.
    """

    with connect() as db:
        return _refresh_tape_used_bytes(
            db,
            tape_id,
        )


def refresh_all_tape_used_bytes():
    """
    Rebuild catalog usage for every registered tape.
    """

    with connect() as db:
        tape_ids = [
            row["id"]
            for row in db.execute(
                """
                SELECT id
                FROM tapes
                ORDER BY id
                """
            ).fetchall()
        ]

        result = {}

        for tape_id in tape_ids:
            result[tape_id] = _refresh_tape_used_bytes(
                db,
                tape_id,
            )

        return result


def record_archived_file(
    original_path,
    relative_path,
    filename,
    size_bytes,
    sha256,
    tape_id,
    tape_path,
    archive_job_id=None,
    original_created_at=None,
    original_modified_at=None,
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
                original_created_at,
                original_modified_at,
                archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
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
                original_created_at,
                original_modified_at,
                utc_now(),
            ),
        )

        file_id = cursor.lastrowid

        _refresh_tape_used_bytes(
            db,
            tape_id,
        )

        return file_id



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
                original_created_at,
                original_modified_at,
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
                files.original_created_at,
                files.original_modified_at,
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
                files.original_created_at,
                files.original_modified_at,
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
                tape_path,
                is_spanned
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



def remove_archive_job_history(job_id):
    """
    Remove one finished archive job from operational history.

    This deliberately preserves the archive catalog.

    Cataloged files are detached from the historical job by
    setting files.archive_job_id to NULL. Files, file_parts,
    tapes, tape paths, checksums, and tape contents are never
    deleted by this operation.
    """
    with connect() as db:
        job = db.execute(
            """
            SELECT
                id,
                status
            FROM archive_jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()

        if job is None:
            raise ValueError(
                f"Archive job #{job_id} does not exist."
            )

        status = str(
            job["status"] or ""
        ).strip().lower()

        #
        # Never allow an active/resumable job to disappear.
        #
        if status not in {
            "completed",
            "error",
            "cancelled",
        }:
            raise ValueError(
                f"Archive job #{job_id} cannot be removed "
                f"while its status is '{status}'."
            )

        #
        # Preserve all cataloged files. Their archive-job
        # relationship is historical metadata only.
        #
        cursor = db.execute(
            """
            UPDATE files
            SET archive_job_id = NULL
            WHERE archive_job_id = ?
            """,
            (job_id,),
        )

        detached_files = cursor.rowcount

        #
        # job_events are operational history and can go away
        # with the archive job.
        #
        db.execute(
            """
            DELETE FROM job_events
            WHERE archive_job_id = ?
            """,
            (job_id,),
        )

        cursor = db.execute(
            """
            DELETE FROM archive_jobs
            WHERE id = ?
            """,
            (job_id,),
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Archive job #{job_id} was not removed."
            )

        return {
            "job_id": job_id,
            "status": status,
            "detached_files": detached_files,
        }


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

    spanned_parts = manifest.get(
        "spanned_parts",
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
                str(entry.get("original_path") or "").strip()
                or (
                    f"recovered://{label}/"
                    f"{relative_path}"
                )
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
                    original_created_at,
                    original_modified_at,
                    archived_at,
                    verified_at
                )
                VALUES (
                    NULL,
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?
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
                    entry.get(
                        "original_created_at"
                    ),
                    entry.get(
                        "original_modified_at"
                    ),
                    archived_at,
                    verified_at,
                ),
            )

            imported_count += 1

        #
        # Recover oversized-file parent rows and physical parts.
        #
        # Parent files intentionally have:
        #   tape_id   = NULL
        #   tape_path = NULL
        #
        # Each physical cartridge location lives only in
        # file_parts.
        #
        spanned_parts_imported = 0
        spanned_parts_existing = 0

        for entry in spanned_parts:
            relative_path = str(
                entry["relative_path"]
            )

            filename = str(
                entry["filename"]
            )

            whole_size = int(
                entry["file_size_bytes"]
            )

            whole_sha256 = str(
                entry["file_sha256"]
            )

            part_number = int(
                entry["part_number"]
            )

            part_size = int(
                entry["part_size_bytes"]
            )

            part_sha256 = str(
                entry["part_sha256"]
            )

            tape_path = str(
                entry["tape_path"]
            )

            archived_at = (
                entry.get("archived_at")
                or now
            )

            #
            # Find an already-recovered parent by logical archive
            # identity. archive_job_id is intentionally not used
            # because it may not exist after total DB loss.
            #
            parent = db.execute(
                """
                SELECT *
                FROM files
                WHERE relative_path = ?
                  AND filename = ?
                  AND is_spanned = 1
                  AND tape_id IS NULL
                  AND tape_path IS NULL
                """,
                (
                    relative_path,
                    filename,
                ),
            ).fetchone()

            if parent is None:
                recovered_original_path = (
                    str(
                        entry.get("original_path")
                        or ""
                    ).strip()
                    or (
                        "recovered://spanned/"
                        + relative_path
                    )
                )

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
                        original_created_at,
                        original_modified_at,
                        archived_at,
                        verified_at
                    )
                    VALUES (
                        NULL,
                        ?, ?, ?, ?, ?,
                        NULL, NULL, 1,
                        ?, ?, ?, NULL
                    )
                    """,
                    (
                        recovered_original_path,
                        relative_path,
                        filename,
                        whole_size,
                        whole_sha256,
                        entry.get(
                            "original_created_at"
                        ),
                        entry.get(
                            "original_modified_at"
                        ),
                        archived_at,
                    ),
                )

                parent_id = cursor.lastrowid

            else:
                parent_id = parent["id"]

                if (
                    int(parent["size_bytes"])
                    != whole_size
                    or parent["checksum_sha256"]
                    != whole_sha256
                ):
                    raise RuntimeError(
                        "Spanned parent conflict detected for "
                        f"{relative_path}. "
                        "No recovery records were imported."
                    )


                db.execute(
                    """
                    UPDATE files
                    SET
                        original_created_at = COALESCE(
                            original_created_at,
                            ?
                        ),
                        original_modified_at = COALESCE(
                            original_modified_at,
                            ?
                        )
                    WHERE id = ?
                    """,
                    (
                        entry.get(
                            "original_created_at"
                        ),
                        entry.get(
                            "original_modified_at"
                        ),
                        parent_id,
                    ),
                )

            existing_part = db.execute(
                """
                SELECT *
                FROM file_parts
                WHERE file_id = ?
                  AND part_number = ?
                """,
                (
                    parent_id,
                    part_number,
                ),
            ).fetchone()

            if existing_part is not None:
                same_part = (
                    int(existing_part["tape_id"])
                    == int(tape_id)
                    and existing_part["tape_path"]
                    == tape_path
                    and int(existing_part["size_bytes"])
                    == part_size
                    and existing_part["checksum_sha256"]
                    == part_sha256
                )

                if not same_part:
                    raise RuntimeError(
                        "Spanned part conflict detected for "
                        f"{relative_path} part {part_number}. "
                        "No recovery records were imported."
                    )

                spanned_parts_existing += 1
                continue

            #
            # Also guard against the same physical path being
            # claimed as a different part.
            #
            path_conflict = db.execute(
                """
                SELECT
                    file_parts.*,
                    files.relative_path
                FROM file_parts
                JOIN files
                    ON files.id = file_parts.file_id
                WHERE file_parts.tape_id = ?
                  AND file_parts.tape_path = ?
                """,
                (
                    tape_id,
                    tape_path,
                ),
            ).fetchone()

            if path_conflict is not None:
                raise RuntimeError(
                    "Spanned tape-path conflict detected: "
                    f"{tape_path}. "
                    "No recovery records were imported."
                )

            db.execute(
                """
                INSERT INTO file_parts (
                    file_id,
                    part_number,
                    tape_id,
                    tape_path,
                    size_bytes,
                    checksum_sha256
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    parent_id,
                    part_number,
                    tape_id,
                    tape_path,
                    part_size,
                    part_sha256,
                ),
            )

            spanned_parts_imported += 1

        return {
            "tape_id": tape_id,
            "tape_created": tape_created,
            "label": label,
            "ltfs_uuid": ltfs_uuid,
            "files_in_manifest": len(files),
            "files_imported": imported_count,
            "files_existing": existing_count,
            "spanned_parts_in_manifest": len(
                spanned_parts
            ),
            "spanned_parts_imported": (
                spanned_parts_imported
            ),
            "spanned_parts_existing": (
                spanned_parts_existing
            ),
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
    ).strftime("%Y%m%d-%H%M%S-%f")

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


def search_files(query):
    """
    Search the TapeBox catalog by filename or path.

    Searches:
      - filename
      - original_path
      - relative_path
      - tape_path
    """

    query = str(query).strip()

    if not query:
        return []

    pattern = f"%{query}%"

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
                files.original_created_at,
                files.original_modified_at,
                files.archived_at,
                files.verified_at,
                tapes.id AS tape_id,
                tapes.label AS tape_label,
                tapes.ltfs_uuid
            FROM files
            LEFT JOIN tapes
                ON tapes.id = files.tape_id
            WHERE
                files.filename LIKE ? COLLATE NOCASE
                OR files.original_path LIKE ? COLLATE NOCASE
                OR files.relative_path LIKE ? COLLATE NOCASE
                OR files.tape_path LIKE ? COLLATE NOCASE
            ORDER BY
                files.filename COLLATE NOCASE,
                files.id
            """,
            (
                pattern,
                pattern,
                pattern,
                pattern,
            ),
        ).fetchall()


def get_archive_restore_plan(job_id):
    """
    Return the logical files, physical file parts, and cartridges
    required to restore an archive job.

    Normal files have one physical tape location directly on files.

    Spanned files have no tape_id/tape_path on the parent. Their
    physical locations are returned through file_parts.
    """

    with connect() as db:
        job = db.execute(
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

        if job is None:
            return None

        rows = db.execute(
            """
            SELECT
                files.id,
                files.archive_job_id,
                files.filename,
                files.relative_path,
                files.size_bytes,
                files.checksum_sha256,
                files.tape_path,
                files.is_spanned,
                tapes.id AS tape_id,
                tapes.label AS tape_label,
                tapes.ltfs_uuid
            FROM files
            LEFT JOIN tapes
                ON tapes.id = files.tape_id
            WHERE files.archive_job_id = ?
            ORDER BY
                files.relative_path COLLATE NOCASE,
                files.id
            """,
            (job_id,),
        ).fetchall()

        part_rows = db.execute(
            """
            SELECT
                file_parts.id AS part_id,
                file_parts.file_id,
                file_parts.part_number,
                file_parts.tape_id,
                file_parts.tape_path,
                file_parts.size_bytes,
                file_parts.checksum_sha256,
                tapes.label AS tape_label,
                tapes.ltfs_uuid
            FROM file_parts
            JOIN files
                ON files.id = file_parts.file_id
            JOIN tapes
                ON tapes.id = file_parts.tape_id
            WHERE files.archive_job_id = ?
            ORDER BY
                files.relative_path COLLATE NOCASE,
                file_parts.part_number
            """,
            (job_id,),
        ).fetchall()

    files = [
        dict(row)
        for row in rows
    ]

    parts = [
        dict(row)
        for row in part_rows
    ]

    #
    # Attach each physical part to its logical parent as well.
    # This makes the restore plan convenient for CLI, restore
    # engine, and future web UI callers.
    #
    parts_by_file = {}

    for part in parts:
        parts_by_file.setdefault(
            part["file_id"],
            [],
        ).append(
            part
        )

    for file_row in files:
        file_row["parts"] = (
            parts_by_file.get(
                file_row["id"],
                [],
            )
        )

    #
    # Build the unique cartridge list from:
    #
    #   normal files -> files.tape_id
    #   spanned files -> file_parts.tape_id
    #
    tapes = []
    seen_tape_ids = set()

    for file_row in files:
        if (
            not file_row["is_spanned"]
            and file_row["tape_id"] is not None
            and file_row["tape_id"]
            not in seen_tape_ids
        ):
            tapes.append(
                {
                    "tape_id": file_row[
                        "tape_id"
                    ],
                    "tape_label": file_row[
                        "tape_label"
                    ],
                    "ltfs_uuid": file_row[
                        "ltfs_uuid"
                    ],
                }
            )

            seen_tape_ids.add(
                file_row["tape_id"]
            )

    for part in parts:
        if (
            part["tape_id"]
            not in seen_tape_ids
        ):
            tapes.append(
                {
                    "tape_id": part[
                        "tape_id"
                    ],
                    "tape_label": part[
                        "tape_label"
                    ],
                    "ltfs_uuid": part[
                        "ltfs_uuid"
                    ],
                }
            )

            seen_tape_ids.add(
                part["tape_id"]
            )

    return {
        "job": dict(job),
        "files": files,
        "parts": parts,
        "tapes": tapes,
    }


def create_spanned_file(
    original_path,
    relative_path,
    filename,
    size_bytes,
    sha256,
    archive_job_id=None,
):
    """
    Create the parent catalog record for a true multi-tape file.

    The parent file does not belong to one cartridge. Its physical
    locations are recorded in file_parts.
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
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 1, ?)
            """,
            (
                archive_job_id,
                original_path,
                relative_path,
                filename,
                size_bytes,
                sha256,
                utc_now(),
            ),
        )

        return cursor.lastrowid


def add_file_part(
    file_id,
    part_number,
    tape_id,
    tape_path,
    size_bytes,
    sha256,
):
    """
    Record one physical part of a spanned file.
    """

    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO file_parts (
                file_id,
                part_number,
                tape_id,
                tape_path,
                size_bytes,
                checksum_sha256
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                part_number,
                tape_id,
                tape_path,
                size_bytes,
                sha256,
            ),
        )

        return cursor.lastrowid


def get_file_parts(file_id):
    """
    Return all physical parts of a spanned file in restore order.
    """

    with connect() as db:
        rows = db.execute(
            """
            SELECT
                file_parts.id,
                file_parts.file_id,
                file_parts.part_number,
                file_parts.tape_id,
                file_parts.tape_path,
                file_parts.size_bytes,
                file_parts.checksum_sha256,
                tapes.label AS tape_label,
                tapes.ltfs_uuid
            FROM file_parts
            LEFT JOIN tapes
                ON tapes.id = file_parts.tape_id
            WHERE file_parts.file_id = ?
            ORDER BY file_parts.part_number
            """,
            (file_id,),
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


def get_next_file_part_number(file_id):
    """
    Return the next part number for a spanned file.
    """

    with connect() as db:
        row = db.execute(
            """
            SELECT MAX(part_number)
            FROM file_parts
            WHERE file_id = ?
            """,
            (file_id,),
        ).fetchone()

    highest = row[0]

    if highest is None:
        return 1

    return int(highest) + 1


def get_file_by_tape_path(
    tape_id,
    tape_path,
):
    """
    Return the cataloged normal file occupying one exact
    physical path on one tape.

    Spanned file parts are tracked separately in file_parts.
    """
    with connect() as db:
        return db.execute(
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
                verified_at,
                original_created_at,
                original_modified_at
            FROM files
            WHERE tape_id = ?
              AND tape_path = ?
            LIMIT 1
            """,
            (
                tape_id,
                tape_path,
            ),
        ).fetchone()


def get_spanned_file_by_job_path(
    archive_job_id,
    relative_path,
):
    """
    Find an existing spanned parent record while resuming a job.
    """

    with connect() as db:
        row = db.execute(
            """
            SELECT
                id,
                archive_job_id,
                original_path,
                relative_path,
                filename,
                size_bytes,
                checksum_sha256,
                is_spanned,
                archived_at
            FROM files
            WHERE archive_job_id = ?
              AND relative_path = ?
              AND is_spanned = 1
            LIMIT 1
            """,
            (
                archive_job_id,
                relative_path,
            ),
        ).fetchone()

    return row


def get_file_parts_by_tape(tape_id):
    """
    Return all spanned-file parts physically stored on one tape.
    """

    with connect() as db:
        rows = db.execute(
            """
            SELECT
                file_parts.id AS part_id,
                file_parts.file_id,
                file_parts.part_number,
                file_parts.tape_id,
                file_parts.tape_path,
                file_parts.size_bytes,
                file_parts.checksum_sha256,

                files.archive_job_id,
                files.original_path,
                files.relative_path,
                files.filename,
                files.size_bytes AS file_size_bytes,
                files.checksum_sha256 AS file_checksum_sha256,
                files.original_created_at,
                files.original_modified_at,
                files.archived_at,
                files.verified_at,

                tapes.label AS tape_label,
                tapes.ltfs_uuid

            FROM file_parts

            JOIN files
                ON files.id = file_parts.file_id

            JOIN tapes
                ON tapes.id = file_parts.tape_id

            WHERE file_parts.tape_id = ?

            ORDER BY
                files.id,
                file_parts.part_number
            """,
            (tape_id,),
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


def get_spanned_file_written_bytes(file_id):
    """
    Return the total number of bytes already safely cataloged
    across all parts of a spanned file.
    """

    with connect() as db:
        row = db.execute(
            """
            SELECT
                COALESCE(
                    SUM(size_bytes),
                    0
                )
            FROM file_parts
            WHERE file_id = ?
            """,
            (file_id,),
        ).fetchone()

    return int(row[0] or 0)


def get_spanned_file_part_count(file_id):
    """
    Return the number of completed/cataloged parts.
    """

    with connect() as db:
        row = db.execute(
            """
            SELECT COUNT(*)
            FROM file_parts
            WHERE file_id = ?
            """,
            (file_id,),
        ).fetchone()

    return int(row[0] or 0)


def record_spanned_file_part(
    original_path,
    relative_path,
    filename,
    file_size_bytes,
    file_sha256,
    archive_job_id,
    part_number,
    tape_id,
    tape_path,
    part_size_bytes,
    part_sha256,
    original_created_at=None,
    original_modified_at=None,
):
    """
    Atomically create/find a spanned parent file and record one
    successfully written physical tape part.

    The parent file does not belong to any single tape:
        tape_id   = NULL
        tape_path = NULL
        is_spanned = 1

    The physical location is recorded only in file_parts.
    """

    with connect() as db:
        row = db.execute(
            """
            SELECT
                id,
                size_bytes,
                checksum_sha256
            FROM files
            WHERE archive_job_id = ?
              AND relative_path = ?
              AND is_spanned = 1
            LIMIT 1
            """,
            (
                archive_job_id,
                relative_path,
            ),
        ).fetchone()

        if row is None:
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
                    original_created_at,
                    original_modified_at,
                    archived_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?,
                    NULL, NULL, 1,
                    ?, ?, ?
                )
                """,
                (
                    archive_job_id,
                    original_path,
                    relative_path,
                    filename,
                    file_size_bytes,
                    file_sha256,
                    original_created_at,
                    original_modified_at,
                    utc_now(),
                ),
            )

            file_id = cursor.lastrowid

        else:
            file_id = row["id"]

            if int(row["size_bytes"]) != int(file_size_bytes):
                raise RuntimeError(
                    "Spanned source file size changed since the "
                    "archive job began."
                )

            existing_sha = row["checksum_sha256"]

            if (
                existing_sha
                and file_sha256
                and existing_sha != file_sha256
            ):
                raise RuntimeError(
                    "Spanned source file checksum changed since the "
                    "archive job began."
                )


            db.execute(
                """
                UPDATE files
                SET
                    original_created_at = COALESCE(
                        original_created_at,
                        ?
                    ),
                    original_modified_at = COALESCE(
                        original_modified_at,
                        ?
                    )
                WHERE id = ?
                """,
                (
                    original_created_at,
                    original_modified_at,
                    file_id,
                ),
            )

        db.execute(
            """
            INSERT INTO file_parts (
                file_id,
                part_number,
                tape_id,
                tape_path,
                size_bytes,
                checksum_sha256
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                part_number,
                tape_id,
                tape_path,
                part_size_bytes,
                part_sha256,
            ),
        )

        _refresh_tape_used_bytes(
            db,
            tape_id,
        )

        return file_id


def _restore_operation_from_row(row):
    """
    Convert one restore_operations row to normal operation state.
    """

    if row is None:
        return None

    def load_json(value, default):
        if value is None:
            return default

        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    return {
        "id": row["id"],
        "type": row["operation_type"],
        "job_id": None,
        "file_ids": load_json(
            row["file_ids_json"],
            [],
        ),
        "destination": row["destination"],
        "status": row["status"],
        "message": row["message"],
        "messages": load_json(
            row["messages_json"],
            [],
        ),
        "transfer": load_json(
            row["transfer_json"],
            None,
        ),
        "result": load_json(
            row["result_json"],
            None,
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_restore_operation(operation):
    """
    Create or update one persistent selected-file restore operation.
    """

    if not isinstance(operation, dict):
        raise TypeError(
            "Restore operation must be a dictionary."
        )

    operation_id = str(
        operation.get("id", "")
    ).strip()

    if not operation_id:
        raise ValueError(
            "Restore operation ID cannot be empty."
        )

    operation_type = str(
        operation.get(
            "type",
            "restore_selected",
        )
    ).strip()

    if operation_type != "restore_selected":
        raise ValueError(
            "Only selected-file restore operations "
            "may be persisted here."
        )

    file_ids = [
        int(value)
        for value in operation.get(
            "file_ids",
            [],
        )
    ]

    destination = str(
        operation.get(
            "destination",
            "",
        )
    )

    status = str(
        operation.get(
            "status",
            "",
        )
    )

    message = str(
        operation.get(
            "message",
            "",
        )
    )

    messages = list(
        operation.get(
            "messages",
            [],
        )
    )

    now = utc_now()

    with connect() as db:
        existing = db.execute(
            """
            SELECT created_at
            FROM restore_operations
            WHERE id = ?
            """,
            (operation_id,),
        ).fetchone()

        created_at = (
            existing["created_at"]
            if existing is not None
            else now
        )

        db.execute(
            """
            INSERT INTO restore_operations (
                id,
                operation_type,
                file_ids_json,
                destination,
                status,
                message,
                messages_json,
                transfer_json,
                result_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                operation_type = excluded.operation_type,
                file_ids_json = excluded.file_ids_json,
                destination = excluded.destination,
                status = excluded.status,
                message = excluded.message,
                messages_json = excluded.messages_json,
                transfer_json = excluded.transfer_json,
                result_json = excluded.result_json,
                updated_at = excluded.updated_at
            """,
            (
                operation_id,
                operation_type,
                json.dumps(
                    file_ids,
                    separators=(",", ":"),
                ),
                destination,
                status,
                message,
                json.dumps(
                    messages,
                    separators=(",", ":"),
                    default=str,
                ),
                (
                    json.dumps(
                        operation.get("transfer"),
                        separators=(",", ":"),
                        default=str,
                    )
                    if operation.get("transfer")
                    is not None
                    else None
                ),
                (
                    json.dumps(
                        operation.get("result"),
                        separators=(",", ":"),
                        default=str,
                    )
                    if operation.get("result")
                    is not None
                    else None
                ),
                created_at,
                now,
            ),
        )


def get_restore_operation(operation_id):
    """
    Return one persistent selected-file restore operation.
    """

    with connect() as db:
        row = db.execute(
            """
            SELECT *
            FROM restore_operations
            WHERE id = ?
            LIMIT 1
            """,
            (str(operation_id),),
        ).fetchone()

    return _restore_operation_from_row(
        row
    )


def get_latest_restore_operation():
    """
    Return the most recently updated selected-file restore operation.
    """

    with connect() as db:
        row = db.execute(
            """
            SELECT *
            FROM restore_operations
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()

    return _restore_operation_from_row(
        row
    )


def get_latest_resumable_restore_operation():
    """
    Return the most recently updated unfinished selected-file restore.
    """

    with connect() as db:
        row = db.execute(
            """
            SELECT *
            FROM restore_operations
            WHERE operation_type = 'restore_selected'
              AND status IN (
                  'starting',
                  'running',
                  'waiting_for_tape'
              )
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()

    return _restore_operation_from_row(
        row
    )


def delete_restore_operation(operation_id):
    """
    Delete one persistent restore operation.
    """

    with connect() as db:
        db.execute(
            """
            DELETE FROM restore_operations
            WHERE id = ?
            """,
            (str(operation_id),),
        )


def check_catalog_health(path=None):
    """
    Perform a read-only health check of the TapeBox catalog.

    This function does not modify, vacuum, repair, rebuild,
    or otherwise change the database.
    """

    database_path = Path(path or DB_PATH)

    result = {
        "success": False,
        "healthy": False,
        "path": str(database_path),
        "size_bytes": 0,
        "integrity": "unknown",
        "foreign_keys": "unknown",
        "journal_mode": "unknown",
        "tables": {},
        "missing_tables": [],
        "error": None,
    }

    if not database_path.is_file():
        result["error"] = (
            f"Catalog database does not exist: "
            f"{database_path}"
        )
        return result

    try:
        result["size_bytes"] = (
            database_path.stat().st_size
        )

        db = sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            timeout=5.0,
        )

        db.row_factory = sqlite3.Row

        integrity_rows = db.execute(
            "PRAGMA integrity_check"
        ).fetchall()

        integrity_messages = [
            row[0]
            for row in integrity_rows
        ]

        result["integrity"] = (
            "ok"
            if integrity_messages == ["ok"]
            else "; ".join(integrity_messages)
        )

        foreign_key_rows = db.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        result["foreign_keys"] = (
            "ok"
            if not foreign_key_rows
            else (
                f"{len(foreign_key_rows)} "
                "violation(s)"
            )
        )

        result["journal_mode"] = (
            db.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
        )

        required_tables = {
            "tapes",
            "files",
            "archive_jobs",
            "file_parts",
            "job_events",
        }

        optional_tables = {
            "settings",
            "restore_operations",
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

        result["missing_tables"] = sorted(
            required_tables - existing_tables
        )

        for table in sorted(
            required_tables | optional_tables
        ):
            if table not in existing_tables:
                result["tables"][table] = None
                continue

            #
            # Table names here come only from the fixed
            # allow-list above, never from user input.
            #
            count = db.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

            result["tables"][table] = count

        result["healthy"] = (
            result["integrity"] == "ok"
            and result["foreign_keys"] == "ok"
            and not result["missing_tables"]
        )

        result["success"] = True

        return result

    except (sqlite3.Error, OSError) as exc:
        result["error"] = str(exc)
        return result

    finally:
        try:
            db.close()
        except Exception:
            pass


def repair_catalog_database():
    """
    Safely rebuild the live TapeBox SQLite catalog.

    Repair procedure:

      1. Validate/check the current catalog.
      2. Create a normal TapeBox safety backup.
      3. Rebuild SQLite into a separate temporary database
         using VACUUM INTO.
      4. Validate the rebuilt database.
      5. Replace the live database only after validation.
      6. Re-run the health check on the live catalog.

    The original catalog is not modified unless the rebuilt
    database passes validation.
    """

    if not DB_PATH.is_file():
        return {
            "success": False,
            "error": (
                "TapeBox catalog does not exist: "
                f"{DB_PATH}"
            ),
        }

    BACKUP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%d-%H%M%S-%f")

    repaired_path = (
        BACKUP_DIR
        / f"catalog-repair-{timestamp}.db"
    )

    #
    # Make a safety backup before doing anything that could
    # eventually replace the live catalog.
    #
    safety_backup = backup_catalog()

    source = None

    try:
        source = sqlite3.connect(
            DB_PATH,
            timeout=5.0,
        )

        source.execute(
            "PRAGMA foreign_keys = ON"
        )

        #
        # VACUUM INTO requires the destination not to exist.
        #
        if repaired_path.exists():
            repaired_path.unlink()

        #
        # SQLite does not allow a bound parameter for the
        # VACUUM INTO filename. Quote single quotes according
        # to SQLite string-literal rules.
        #
        target_sql = str(
            repaired_path
        ).replace("'", "''")

        source.execute(
            f"VACUUM INTO '{target_sql}'"
        )

    except (sqlite3.Error, OSError) as exc:
        try:
            if repaired_path.exists():
                repaired_path.unlink()
        except OSError:
            pass

        return {
            "success": False,
            "error": (
                "Database rebuild failed: "
                f"{exc}"
            ),
            "safety_backup": safety_backup["path"],
        }

    finally:
        if source is not None:
            source.close()

    #
    # The rebuilt database must pass the existing TapeBox
    # restore validator before it can replace anything.
    #
    validation = validate_catalog_database(
        repaired_path
    )

    if not validation.get("success"):
        return {
            "success": False,
            "error": (
                "Rebuilt database failed validation: "
                + validation.get(
                    "error",
                    "unknown validation error",
                )
            ),
            "safety_backup": safety_backup["path"],
            "repaired_database": str(repaired_path),
        }

    #
    # Also run the more detailed health checker.
    #
    repaired_health = check_catalog_health(
        repaired_path
    )

    if not (
        repaired_health.get("success")
        and repaired_health.get("healthy")
    ):
        return {
            "success": False,
            "error": (
                "Rebuilt database failed the TapeBox "
                "health check."
            ),
            "safety_backup": safety_backup["path"],
            "repaired_database": str(repaired_path),
            "health": repaired_health,
        }

    #
    # restore_catalog() performs another validation and creates
    # another pre-restore backup before replacing the live DB.
    #
    restored = restore_catalog(
        repaired_path
    )

    if not restored.get("success"):
        return {
            "success": False,
            "error": restored.get(
                "error",
                "Could not install repaired database.",
            ),
            "safety_backup": safety_backup["path"],
            "repaired_database": str(repaired_path),
        }

    #
    # Recreate any schema additions expected by this version
    # of TapeBox.
    #
    initialize_database()
    initialize_default_settings()

    final_health = check_catalog_health()

    if not (
        final_health.get("success")
        and final_health.get("healthy")
    ):
        #
        # The rebuilt catalog installed, but the final
        # verification failed. Restore the original safety
        # backup automatically so TapeBox is not left using
        # a catalog that failed verification.
        #
        rollback = restore_catalog(
            safety_backup["path"]
        )

        initialize_database()
        initialize_default_settings()

        rollback_health = check_catalog_health()

        return {
            "success": False,
            "error": (
                "Repaired catalog failed final verification. "
                "TapeBox attempted to restore the original "
                "catalog automatically."
            ),
            "rolled_back": bool(
                rollback.get("success")
                and rollback_health.get("success")
                and rollback_health.get("healthy")
            ),
            "safety_backup": safety_backup["path"],
            "pre_install_backup": restored.get(
                "pre_restore_backup"
            ),
            "repaired_database": str(repaired_path),
            "failed_health": final_health,
            "rollback_health": rollback_health,
        }

    return {
        "success": True,
        "message": "Database repaired successfully.",
        "safety_backup": safety_backup["path"],
        "pre_install_backup": restored.get(
            "pre_restore_backup"
        ),
        "repaired_database": str(repaired_path),
        "tapes": validation["tapes"],
        "files": validation["files"],
        "health": final_health,
    }


def import_existing_tape_files(
    tape_id,
    tape_label,
    scanned_files,
):
    """
    Import physical files discovered on an existing LTFS tape.

    This is for files that already existed on the cartridge before
    TapeBox cataloged them.

    Rules:
    - Exact tape path + same size: already cataloged.
    - Exact tape path + different size: conflict; import nothing.
    - New physical path: insert a normal file record.
    - No archive job is invented.
    - No checksum is invented.
    - archived_at remains NULL because TapeBox did not archive it.
    - The tape is not modified.
    """

    if tape_id is None:
        raise ValueError(
            "Registered tape ID is required."
        )

    label = str(
        tape_label or ""
    ).strip().upper()

    if not label:
        raise ValueError(
            "Tape label is required."
        )

    entries = list(
        scanned_files or []
    )

    with connect() as db:
        tape = db.execute(
            """
            SELECT *
            FROM tapes
            WHERE id = ?
            """,
            (tape_id,),
        ).fetchone()

        if tape is None:
            raise ValueError(
                f"Registered tape ID {tape_id} "
                "does not exist."
            )

        registered_label = str(
            tape["label"] or ""
        ).strip().upper()

        if registered_label != label:
            raise ValueError(
                "Loaded tape label does not match "
                "the registered cartridge."
            )

        existing_count = 0
        pending = []
        conflicts = []

        seen_paths = set()

        for entry in entries:
            tape_path = str(
                entry.get("tape_path") or ""
            ).strip()

            filename = str(
                entry.get("filename") or ""
            ).strip()

            relative_path = str(
                entry.get("relative_path") or ""
            ).strip()

            if not tape_path.startswith("/"):
                raise ValueError(
                    f"Invalid LTFS tape path: {tape_path!r}"
                )

            if (
                tape_path == "/.tapebox"
                or tape_path.startswith(
                    "/.tapebox/"
                )
            ):
                continue

            if not filename:
                raise ValueError(
                    f"Missing filename for {tape_path}"
                )

            if not relative_path:
                raise ValueError(
                    f"Missing relative path for {tape_path}"
                )

            try:
                size_bytes = int(
                    entry.get("size_bytes")
                )
            except (TypeError, ValueError):
                raise ValueError(
                    f"Invalid file size for {tape_path}"
                )

            if size_bytes < 0:
                raise ValueError(
                    f"Invalid file size for {tape_path}"
                )

            if tape_path in seen_paths:
                raise ValueError(
                    "Physical LTFS scan returned duplicate "
                    f"path: {tape_path}"
                )

            seen_paths.add(tape_path)

            existing = db.execute(
                """
                SELECT
                    id,
                    filename,
                    size_bytes,
                    tape_path
                FROM files
                WHERE tape_id = ?
                  AND tape_path = ?
                LIMIT 1
                """,
                (
                    tape_id,
                    tape_path,
                ),
            ).fetchone()

            if existing is not None:
                if (
                    int(existing["size_bytes"])
                    == size_bytes
                ):
                    existing_count += 1
                    continue

                conflicts.append(
                    {
                        "tape_path": tape_path,
                        "catalog_file_id": (
                            existing["id"]
                        ),
                        "catalog_size_bytes": int(
                            existing["size_bytes"]
                        ),
                        "physical_size_bytes": (
                            size_bytes
                        ),
                    }
                )
                continue

            part = db.execute(
                """
                SELECT
                    file_parts.id,
                    file_parts.file_id,
                    file_parts.size_bytes
                FROM file_parts
                WHERE file_parts.tape_id = ?
                  AND file_parts.tape_path = ?
                LIMIT 1
                """,
                (
                    tape_id,
                    tape_path,
                ),
            ).fetchone()

            if part is not None:
                if (
                    int(part["size_bytes"])
                    == size_bytes
                ):
                    existing_count += 1
                    continue

                conflicts.append(
                    {
                        "tape_path": tape_path,
                        "catalog_file_id": (
                            part["file_id"]
                        ),
                        "catalog_part_id": (
                            part["id"]
                        ),
                        "catalog_size_bytes": int(
                            part["size_bytes"]
                        ),
                        "physical_size_bytes": (
                            size_bytes
                        ),
                    }
                )
                continue

            pending.append(
                {
                    "tape_path": tape_path,
                    "relative_path": relative_path,
                    "filename": filename,
                    "size_bytes": size_bytes,
                    "modified_at": (
                        entry.get("modified_at")
                    ),
                }
            )

        if conflicts:
            return {
                "success": False,
                "tape_id": tape_id,
                "label": registered_label,
                "files_scanned": len(entries),
                "files_imported": 0,
                "files_existing": existing_count,
                "conflicts": conflicts,
                "conflict_count": len(conflicts),
                "error": (
                    "One or more physical tape paths "
                    "conflict with the catalog. "
                    "No files were imported."
                ),
            }

        imported_count = 0

        for entry in pending:
            relative_for_uri = (
                entry["tape_path"].lstrip("/")
            )

            original_path = (
                f"imported://{registered_label}/"
                f"{relative_for_uri}"
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
                    original_created_at,
                    original_modified_at,
                    archived_at,
                    verified_at
                )
                VALUES (
                    NULL,
                    ?,
                    ?,
                    ?,
                    ?,
                    NULL,
                    ?,
                    ?,
                    0,
                    NULL,
                    ?,
                    NULL,
                    NULL
                )
                """,
                (
                    original_path,
                    entry["relative_path"],
                    entry["filename"],
                    entry["size_bytes"],
                    tape_id,
                    entry["tape_path"],
                    entry["modified_at"],
                ),
            )

            imported_count += 1

        catalog_used_bytes = (
            _refresh_tape_used_bytes(
                db,
                tape_id,
            )
        )

        db.execute(
            """
            UPDATE tapes
            SET last_seen_at = ?
            WHERE id = ?
            """,
            (
                utc_now(),
                tape_id,
            ),
        )

        return {
            "success": True,
            "tape_id": tape_id,
            "label": registered_label,
            "files_scanned": len(entries),
            "files_imported": imported_count,
            "files_existing": existing_count,
            "conflicts": [],
            "conflict_count": 0,
            "catalog_used_bytes": (
                catalog_used_bytes
            ),
        }


def remove_tape_from_catalog(tape_id):
    """
    Remove one tape and all catalog records that depend on it.

    This is a SQLite/catalog operation only. It never accesses,
    mounts, formats, erases, ejects, or writes to the physical tape.

    Rules:
      - Normal files assigned directly to this tape are removed.
      - If this tape contains any part of a spanned logical file,
        the entire logical file and all of its parts are removed.
      - Other affected tapes have used_bytes recalculated.
      - A catalog backup is created before destructive changes.
    """

    tape_id = int(tape_id)

    #
    # Confirm the tape exists before creating a backup.
    #
    with connect() as db:
        tape = db.execute(
            """
            SELECT *
            FROM tapes
            WHERE id = ?
            """,
            (tape_id,),
        ).fetchone()

    if tape is None:
        raise ValueError(
            "Tape not found."
        )

    tape_label = tape["label"]

    #
    # Destructive catalog operation:
    # create a recoverable SQLite backup first.
    #
    backup_catalog()

    with connect() as db:
        #
        # Re-check inside the mutation transaction.
        #
        tape = db.execute(
            """
            SELECT *
            FROM tapes
            WHERE id = ?
            """,
            (tape_id,),
        ).fetchone()

        if tape is None:
            raise ValueError(
                "Tape no longer exists."
            )

        #
        # Files physically assigned directly to this tape.
        #
        direct_rows = db.execute(
            """
            SELECT id
            FROM files
            WHERE tape_id = ?
            """,
            (tape_id,),
        ).fetchall()

        direct_file_ids = {
            int(row["id"])
            for row in direct_rows
        }

        #
        # Logical files having one or more physical parts
        # on the tape being removed.
        #
        spanned_rows = db.execute(
            """
            SELECT DISTINCT file_id
            FROM file_parts
            WHERE tape_id = ?
            """,
            (tape_id,),
        ).fetchall()

        spanned_file_ids = {
            int(row["file_id"])
            for row in spanned_rows
        }

        file_ids_to_remove = (
            direct_file_ids
            | spanned_file_ids
        )

        #
        # Find every other tape that contains a part belonging
        # to one of the logical files being removed. Those tapes
        # need their catalog-used-byte totals refreshed afterward.
        #
        affected_other_tapes = set()

        parts_removed = 0

        if file_ids_to_remove:
            placeholders = ",".join(
                "?"
                for _ in file_ids_to_remove
            )

            file_id_values = tuple(
                sorted(file_ids_to_remove)
            )

            part_rows = db.execute(
                f"""
                SELECT
                    id,
                    tape_id
                FROM file_parts
                WHERE file_id IN ({placeholders})
                """,
                file_id_values,
            ).fetchall()

            parts_removed = len(part_rows)

            for row in part_rows:
                other_tape_id = int(
                    row["tape_id"]
                )

                if other_tape_id != tape_id:
                    affected_other_tapes.add(
                        other_tape_id
                    )

            #
            # file_parts has ON DELETE CASCADE from files,
            # so deleting the parent logical records also removes
            # every physical part record belonging to them.
            #
            db.execute(
                f"""
                DELETE FROM files
                WHERE id IN ({placeholders})
                """,
                file_id_values,
            )

        #
        # There must now be no files/file_parts referencing the
        # cartridge, allowing the NO ACTION tape FKs to protect us
        # if anything unexpected was missed.
        #
        db.execute(
            """
            DELETE FROM tapes
            WHERE id = ?
            """,
            (tape_id,),
        )

        #
        # Removing an entire logical spanned file may have removed
        # parts from other tapes as well.
        #
        for other_tape_id in sorted(
            affected_other_tapes
        ):
            still_exists = db.execute(
                """
                SELECT 1
                FROM tapes
                WHERE id = ?
                """,
                (other_tape_id,),
            ).fetchone()

            if still_exists:
                _refresh_tape_used_bytes(
                    db,
                    other_tape_id,
                )

        return {
            "success": True,
            "tape_id": tape_id,
            "label": tape_label,
            "files_removed": len(
                file_ids_to_remove
            ),
            "direct_files_removed": len(
                direct_file_ids
            ),
            "spanned_files_removed": len(
                spanned_file_ids
            ),
            "parts_removed": parts_removed,
            "physical_tape_modified": False,
        }
