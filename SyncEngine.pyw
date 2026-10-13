"""Launch SyncEngine from the application folder without a console window."""

import sys
from pathlib import Path

application_root = Path(__file__).resolve().parent
sys.path.insert(0, str(application_root / "sync_engine"))

from launch_sync_engine import main


if __name__ == "__main__":
    raise SystemExit(main())
