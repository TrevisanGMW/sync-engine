"""UTF-8 log files shared by GUI and scheduled backup sessions."""

import logging
import os
from datetime import datetime


def configure_logging(logs_directory):
    """Create a session log and attach it to the SyncEngine logger.

    Args:
        logs_directory (str or Path): Directory for persistent session logs.

    Returns:
        str: Absolute path of this session's log file.
    """
    os.makedirs(logs_directory, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base_name = f"SyncEngine_{timestamp}"
    log_path = os.path.abspath(os.path.join(logs_directory, f"{base_name}.log"))
    suffix = 2
    while os.path.exists(log_path):
        log_path = os.path.abspath(os.path.join(logs_directory, f"{base_name}_{suffix}.log"))
        suffix += 1
    logger = logging.getLogger("sync_engine")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.info("SyncEngine session started; process=%s", os.getpid())
    return log_path
