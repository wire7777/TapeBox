"""
TapeBox external software updater command-line helper.

This module intentionally runs outside the TapeBox Flask request process.
It provides a command-line interface to the safe update and rollback
functions implemented in tapebox.updater.
"""

import argparse
import json
import sys

from tapebox.updater import (
    UpdateError,
    check_for_updates,
    complete_manual_update,
    perform_update,
    read_state,
    rollback_update_checkpoint,
    update_environment,
)


def print_json(value):
    print(
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def command_status(args):
    environment = update_environment()
    state = read_state()

    return {
        "success": True,
        "environment": environment,
        "update_state": state,
    }


def command_check(args):
    return check_for_updates()


def command_update(args):
    return perform_update(
        tag_name=args.tag,
        expected_version=args.version,
        health_url=args.health_url,
    )


def command_complete(args):
    return complete_manual_update(
        health_url=args.health_url,
    )


def command_rollback(args):
    state = read_state()

    if not isinstance(state, dict):
        raise UpdateError(
            "No TapeBox rollback checkpoint is available."
        )

    result = rollback_update_checkpoint(
        state
    )

    return {
        "success": True,
        "status": "rolled_back",
        "rollback": result,
        "manual_restart_required": (
            state.get("runtime_mode")
            == "development"
        ),
    }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="tapebox-updater",
        description=(
            "TapeBox safe software update and rollback helper."
        ),
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Show updater environment and saved state.",
    )
    status_parser.set_defaults(
        handler=command_status
    )

    check_parser = subparsers.add_parser(
        "check",
        help="Check GitHub for a newer stable TapeBox release.",
    )
    check_parser.set_defaults(
        handler=command_check
    )

    update_parser = subparsers.add_parser(
        "update",
        help="Install a specific validated TapeBox release tag.",
    )
    update_parser.add_argument(
        "tag",
        help="Release tag to install, for example v0.2.0.",
    )
    update_parser.add_argument(
        "--version",
        help=(
            "Expected semantic version. "
            "The release is rejected if it does not match."
        ),
    )
    update_parser.add_argument(
        "--health-url",
        default="http://127.0.0.1:8080/",
        help=(
            "TapeBox HTTP URL used for post-update "
            "health validation."
        ),
    )
    update_parser.set_defaults(
        handler=command_update
    )

    complete_parser = subparsers.add_parser(
        "complete",
        help=(
            "Complete validation after a manual development restart."
        ),
    )
    complete_parser.add_argument(
        "--health-url",
        default="http://127.0.0.1:8080/",
        help="TapeBox HTTP URL used for health validation.",
    )
    complete_parser.set_defaults(
        handler=command_complete
    )

    rollback_parser = subparsers.add_parser(
        "rollback",
        help="Restore the saved source and catalog checkpoint.",
    )
    rollback_parser.set_defaults(
        handler=command_rollback
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = args.handler(
            args
        )

        print_json(
            result
        )

        return 0

    except UpdateError as exc:
        print_json(
            {
                "success": False,
                "error": str(exc),
            }
        )

        return 1

    except KeyboardInterrupt:
        print_json(
            {
                "success": False,
                "error": "Interrupted.",
            }
        )

        return 130

    except Exception as exc:
        print_json(
            {
                "success": False,
                "error": (
                    f"Unexpected updater error: {exc}"
                ),
            }
        )

        return 1


if __name__ == "__main__":
    sys.exit(
        main()
    )
