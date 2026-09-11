import subprocess
from datetime import datetime, timezone
from pathlib import Path


def filesystem_timestamps(path):
    """
    Return true filesystem creation/birth time when available,
    plus filesystem modification time.

    Linux ctime is intentionally NOT treated as creation time.

    Python on Linux often does not expose st_birthtime even when
    the filesystem stores birth time, so GNU stat %W is used as
    a fallback.
    """

    path = Path(path)
    stat_result = path.stat()

    modified_at = datetime.fromtimestamp(
        stat_result.st_mtime,
        tz=timezone.utc,
    ).isoformat()

    created_at = None

    birth_time = getattr(
        stat_result,
        "st_birthtime",
        None,
    )

    if birth_time is None:
        try:
            result = subprocess.run(
                [
                    "stat",
                    "--format=%W",
                    "--",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            raw_birth_time = (
                result.stdout.strip()
            )

            if raw_birth_time:
                parsed_birth_time = int(
                    raw_birth_time
                )

                if parsed_birth_time > 0:
                    birth_time = (
                        parsed_birth_time
                    )

        except (
            OSError,
            ValueError,
            subprocess.CalledProcessError,
        ):
            birth_time = None

    if (
        birth_time is not None
        and birth_time > 0
    ):
        created_at = datetime.fromtimestamp(
            birth_time,
            tz=timezone.utc,
        ).isoformat()

    return {
        "created_at": created_at,
        "modified_at": modified_at,
    }


def format_timestamp(value):
    """
    Format a stored ISO timestamp for the TapeBox UI.

    UTC catalog timestamps are converted to the server's local
    timezone before display.

    Missing/invalid values display as an em dash.
    """

    if not value:
        return "—"

    try:
        text = str(value).strip()

        if text.endswith("Z"):
            text = (
                text[:-1]
                + "+00:00"
            )

        dt = datetime.fromisoformat(
            text
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        dt = dt.astimezone()

        return dt.strftime(
            "%Y-%m-%d %I:%M:%S %p"
        )

    except (
        TypeError,
        ValueError,
        OSError,
    ):
        return "—"
