"""Synchronous startup wrapper for interactive scheduled runs."""

import sys

from sync_engine_startup import startup_main


def main(arguments=None):
    """Starts a scheduled GUI action and waits for its final result.

    Args:
        arguments (list, optional): Arguments forwarded to SyncEngine.

    Returns:
        int: Scheduled operation result.
    """
    forwarded = list(sys.argv[1:] if arguments is None else arguments)
    return startup_main(forwarded, gui=False)


if __name__ == "__main__":
    raise SystemExit(main())
