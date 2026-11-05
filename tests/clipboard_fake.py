"""Reusable in-memory Win32 clipboard; never calls the operating system (1148)."""

CF_UNICODETEXT = 13
SHELL_IDLIST = 0xC0A1


def _utf16(text):
    return (text + "\0").encode("utf-16-le")

class FakeClipboard:
    """In-memory Win32 clipboard + global memory, shaped like the calls
    samsara.clipboard makes. Pointers are the handle numbers."""

    def __init__(self, formats=None):
        self.formats = dict(formats or {})
        self.seq = 100
        self.is_open = False
        self.mem = {}
        self.handle_fmt = {}
        self.next_handle = 5000
        self.owner = 0
        self.open_attempts = 0
        self.open_failures_remaining = 0
        self.no_data_formats = set()
        self.alloc_fail_on = None      # 1-based GlobalAlloc call number that returns NULL
        self.alloc_calls = 0
        self.set_fail_fmts = set()
        self.set_fail_once_fmts = set()
        self.size_override = {}        # fmt -> reported GlobalSize
        self.before_open = None        # callable run just before a successful OpenClipboard
        self.empties = 0
        self.allocated = set()         # GlobalAlloc handles not yet freed or handed to SetClipboardData

    # -- user32 ---------------------------------------------------------
    def OpenClipboard(self, _hwnd):
        self.open_attempts += 1
        if self.open_failures_remaining:
            self.open_failures_remaining -= 1
            return 0
        if self.is_open:
            return 0
        if self.before_open:
            hook, self.before_open = self.before_open, None
            hook()
        self.is_open = True
        return 1

    def CloseClipboard(self):
        self.is_open = False
        return 1

    def GetOpenClipboardWindow(self):
        return 123 if self.is_open else 0

    def IsClipboardFormatAvailable(self, fmt):
        return fmt in self.formats

    def EmptyClipboard(self):
        assert self.is_open, "EmptyClipboard without OpenClipboard"
        self.formats = {}
        self.seq += 1
        self.empties += 1
        return 1

    def EnumClipboardFormats(self, current):
        assert self.is_open
        keys = list(self.formats)
        i = 0 if current == 0 else keys.index(current) + 1
        return keys[i] if i < len(keys) else 0

    def GetClipboardData(self, fmt):
        assert self.is_open
        if fmt not in self.formats or fmt in self.no_data_formats:
            return 0
        h = self._new_handle(bytearray(self.formats[fmt]))
        self.handle_fmt[h] = fmt
        return h

    def SetClipboardData(self, fmt, h):
        assert self.is_open
        if fmt in self.set_fail_fmts:
            return 0
        if fmt in self.set_fail_once_fmts:
            self.set_fail_once_fmts.remove(fmt)
            return 0
        self.formats[fmt] = bytes(self.mem.pop(h))
        self.allocated.discard(h)      # ownership passes to the clipboard
        self.seq += 1
        return h

    def GetClipboardSequenceNumber(self):
        return self.seq

    def GetClipboardOwner(self):
        return self.owner

    def IsHungAppWindow(self, _hwnd):
        return False

    def SendMessageTimeoutW(self, *_args):
        return 1

    def CountClipboardFormats(self):
        return len(self.formats)

    def GetClipboardFormatNameW(self, fmt, buf, _n):
        if fmt == SHELL_IDLIST:
            buf.value = "Shell IDList Array"
            return len(buf.value)
        return 0

    # -- kernel32 -------------------------------------------------------
    def GlobalAlloc(self, _flags, size):
        self.alloc_calls += 1
        if self.alloc_fail_on == self.alloc_calls:
            return 0
        h = self._new_handle(bytearray(size))
        self.allocated.add(h)
        return h

    def GlobalSize(self, h):
        fmt = self.handle_fmt.get(h)
        if fmt in self.size_override:
            return self.size_override[fmt]
        return len(self.mem[h])

    def GlobalLock(self, h):
        return h if h in self.mem else 0

    def GlobalUnlock(self, _h):
        return 1

    def GlobalFree(self, h):
        self.mem.pop(h, None)
        self.allocated.discard(h)
        return 0

    # -- helpers --------------------------------------------------------
    def _new_handle(self, data):
        self.next_handle += 1
        self.mem[self.next_handle] = data
        return self.next_handle

    def string_at(self, ptr, size):
        return bytes(self.mem[ptr][:size])

    def memmove(self, ptr, raw, n):
        self.mem[ptr][:n] = raw
        return ptr

    def user_copies_text(self, text):
        """What pyperclip.copy / a user's Ctrl+C does: one atomic replace."""
        self.formats = {CF_UNICODETEXT: _utf16(text)}
        self.seq += 1

    def text(self):
        raw = self.formats.get(CF_UNICODETEXT)
        return raw.decode("utf-16-le").rstrip("\0") if raw else ""
