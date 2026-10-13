"""Serialize SyncEngine writes across GUI and scheduled processes."""

import os


class AlreadyRunningError(RuntimeError):
    """Raised when another SyncEngine process owns the backup lock."""


class RunLock:
    """Hold an operating-system file lock for one backup execution.

    Locks release automatically if the process exits or crashes. One execution
    per user also protects plans whose destination folders overlap.

    Args:
        directory (str or Path): User data directory containing the lock file.
    """

    def __init__(self, directory):
        """Initialize the lock without acquiring it.

        Args:
            directory (str or Path): Directory to store the lock file.
        """
        self.path = os.path.join(os.fspath(directory), "execution.lock")
        self.handle = None

    def acquire(self):
        """Acquire a nonblocking exclusive lock.

        Returns:
            RunLock: This acquired lock.

        Raises:
            AlreadyRunningError: Another process is currently syncing.
        """
        if self.handle is not None:
            return self
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.handle = open(self.path, "a+b")
        try:
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            self.handle = None
            raise AlreadyRunningError(
                "Another SyncEngine backup is running. Try again when it finishes."
            ) from error
        return self

    def release(self):
        """Release the lock and close its file handle."""
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        """Acquire this lock for a context manager.

        Returns:
            RunLock: This acquired lock.
        """
        return self.acquire()

    def __exit__(self, exception_type, exception, traceback):
        """Release this lock at the end of a context manager.

        Args:
            exception_type (type or None): Type of a pending exception.
            exception (Exception or None): Pending exception instance.
            traceback (object or None): Pending exception traceback.

        Returns:
            bool: False to propagate exceptions from the operation.
        """
        self.release()
        return False
