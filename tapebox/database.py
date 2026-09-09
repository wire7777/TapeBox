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
                created_at TEXT NOT NULL,
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
                sha256 TEXT,
                tape_id INTEGER,
                tape_path TEXT,
                is_spanned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                restored_at TEXT,

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
