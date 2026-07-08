"""Pure tail-follow logic for the live log viewer window.

No Qt dependency -- directly testable against tmp files. See
samsara/ui/log_viewer_qt.py for the window that drives this on a QTimer.
"""

from pathlib import Path


class LogTailer:
    """Tail-follows a text log file, with rotation detection.

    Handles the RotatingFileHandler (5MB x 3 backups) rotation scheme:
    when the log rolls over, samsara.log is renamed to samsara.log.1 and a
    fresh, empty samsara.log is created in its place -- so the file this
    object watches shrinks (or a new file with a different creation time
    appears at the same path) whenever that happens.
    """

    INITIAL_TAIL_BYTES = 200 * 1024
    ROTATION_SEPARATOR = "--- log rotated ---"

    def __init__(self, path):
        self.path = Path(path)
        self._offset = 0
        self._last_size = 0
        self._last_ctime = None

    def initial_tail(self) -> list:
        """Read the last ~200KB of the file, discarding the first
        (likely partial) line, and set the read offset to end-of-file so
        the next poll() only returns lines appended after this point.

        Returns a list of complete lines (no trailing newlines). Never
        raises -- a missing file yields an empty list.
        """
        try:
            stat = self.path.stat()
        except OSError:
            self._offset = 0
            return []

        start = max(0, stat.st_size - self.INITIAL_TAIL_BYTES)
        with open(self.path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(start)
            data = f.read()
            self._offset = f.tell()

        lines = data.split('\n')
        if start > 0 and lines:
            lines = lines[1:]           # discard the partial first line
        if lines and lines[-1] == '':
            lines = lines[:-1]          # trailing split artifact from a final \n

        self._last_size = stat.st_size
        self._last_ctime = stat.st_ctime
        return lines

    def poll(self) -> list:
        """Read newly appended lines since the last initial_tail()/poll()
        call. Detects rotation (file size shrank below our current offset,
        or size shrank below what we last saw AND the file's ctime
        changed -- the Windows-safe proxy for "this is a different file
        now" since inode numbers aren't a reliable rotation signal there)
        and, on rotation, resets to a fresh read from byte 0 and prepends a
        visible separator line.

        Never raises -- a missing file yields an empty list.
        """
        try:
            stat = self.path.stat()
        except OSError:
            return []

        ctime_changed = self._last_ctime is not None and stat.st_ctime != self._last_ctime
        rotated = (
            stat.st_size < self._offset
            or (stat.st_size < self._last_size and ctime_changed)
        )

        if rotated:
            self._offset = 0

        new_lines = self._read_from_offset()

        self._last_size = stat.st_size
        self._last_ctime = stat.st_ctime

        if rotated:
            return [self.ROTATION_SEPARATOR] + new_lines
        return new_lines

    def _read_from_offset(self) -> list:
        with open(self.path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(self._offset)
            data = f.read()
            self._offset = f.tell()
        if not data:
            return []
        lines = data.split('\n')
        if lines and lines[-1] == '':
            lines = lines[:-1]
        return lines
