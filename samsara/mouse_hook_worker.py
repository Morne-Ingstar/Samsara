"""1255: isolated mouse pump. Also a standalone PyInstaller console entry.

No application imports or Python threads. Callbacks only count/enqueue; the
pump polls stdin and an OVERLAPPED stdout write, so pipe backpressure cannot
block the hook. The first mask command is a barrier: the parent must assign
our process to its kill-on-close job before allowing any hook installation.
"""
import sys
import os
import ctypes
import struct
import gc


# Wire v1: button, pressed, source, injected, counter/result, QPC microseconds.
FRAME = struct.Struct('<BBBBIQ')
CONTROL = struct.Struct('<BB')
VERSION = 1
LOW_LEVEL, RAW_INPUT, LL_STATUS, RAW_STATUS = 1, 2, 3, 4
READY, REFRESHED, HEARTBEAT = 5, 6, 7
SET_MASK, REFRESH, STOP = 1, 2, 3
POLL_MS = 5
STATUS_US = 50_000


class Controls:
    def __init__(self):
        self.pending = b''
        self.mask = 0
        self.started = False
        self.stopping = False

    def feed(self, data):
        if data is None:  # broken stdin / EOF
            self.stopping = True
            return False
        self.pending += data
        refresh = False
        while len(self.pending) >= CONTROL.size:
            command, value = CONTROL.unpack(self.pending[:CONTROL.size])
            self.pending = self.pending[CONTROL.size:]
            if command == SET_MASK:
                self.mask = value & 3
                self.started = True
            elif command == REFRESH:
                refresh = True
            elif command == STOP:
                self.stopping = True
            else:
                raise ValueError('unknown mouse worker command')
        return refresh


def native(library, name, args, result):
    fn = library[name]
    fn.argtypes, fn.restype = args, result
    return fn


# Explicit Windows ABI types; keep this module importable for protocol tests.
DWORD, WORD, BOOL = ctypes.c_uint32, ctypes.c_uint16, ctypes.c_int32
HANDLE, WPARAM, LPARAM = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ssize_t
CALLBACK = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)
HookProc = CALLBACK(LPARAM, ctypes.c_int, WPARAM, LPARAM)
WindowProc = CALLBACK(LPARAM, HANDLE, DWORD, WPARAM, LPARAM)


class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int32), ('y', ctypes.c_int32)]


class MSG(ctypes.Structure):
    _fields_ = [('hwnd', HANDLE), ('message', DWORD), ('wParam', WPARAM),
                ('lParam', LPARAM), ('time', DWORD), ('pt', POINT), ('lPrivate', DWORD)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', POINT), ('mouseData', DWORD), ('flags', DWORD),
                ('time', DWORD), ('dwExtraInfo', WPARAM)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [('dwType', DWORD), ('dwSize', DWORD), ('hDevice', HANDLE), ('wParam', WPARAM)]


class RawButtons(ctypes.Structure):
    _fields_ = [('flags', WORD), ('data', WORD)]


class RawUnion(ctypes.Union):
    _anonymous_ = ('buttons',)
    _fields_ = [('all', DWORD), ('buttons', RawButtons)]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('usFlags', WORD), ('u', RawUnion), ('rawButtons', DWORD),
                ('x', ctypes.c_int32), ('y', ctypes.c_int32), ('extra', DWORD)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [('header', RAWINPUTHEADER), ('mouse', RAWMOUSE)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [('page', WORD), ('usage', WORD), ('flags', DWORD), ('target', HANDLE)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [('style', DWORD), ('proc', WindowProc), ('clsExtra', ctypes.c_int),
                ('wndExtra', ctypes.c_int), ('instance', HANDLE), ('icon', HANDLE),
                ('cursor', HANDLE), ('background', HANDLE), ('menu', ctypes.c_wchar_p),
                ('name', ctypes.c_wchar_p)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', WPARAM), ('InternalHigh', WPARAM),
                ('Offset', DWORD), ('OffsetHigh', DWORD), ('hEvent', HANDLE)]


class Output:
    """One outstanding asynchronous write; retain all subsequent edge frames."""
    def __init__(self, kernel, handle):
        self.handle = handle
        self.frames = []
        self.head = 0
        self.buffer = None
        self.write = native(kernel, 'WriteFile', (HANDLE, HANDLE, DWORD, HANDLE, HANDLE), BOOL)
        self.result = native(kernel, 'GetOverlappedResult', (HANDLE, HANDLE, HANDLE, BOOL), BOOL)
        self.cancel = native(kernel, 'CancelIoEx', (HANDLE, HANDLE), BOOL)
        self.close_handle = native(kernel, 'CloseHandle', (HANDLE,), BOOL)
        self.reset = native(kernel, 'ResetEvent', (HANDLE,), BOOL)
        self.overlapped = OVERLAPPED()
        self.overlapped.hEvent = native(kernel, 'CreateEventW',
                                       (HANDLE, BOOL, BOOL, ctypes.c_wchar_p), HANDLE)(None, True, False, None)
        if not self.overlapped.hEvent:
            raise ctypes.WinError()

    def put(self, button, pressed, source, injected, value, stamp):
        self.frames.append(FRAME.pack(button, bool(pressed), source, bool(injected),
                                      value & 0xFFFFFFFF, stamp))

    def flush(self):
        transferred = DWORD()
        if self.buffer is not None:
            if not self.result(self.handle, ctypes.byref(self.overlapped), ctypes.byref(transferred), False):
                if ctypes.get_last_error() == 996:  # ERROR_IO_INCOMPLETE
                    return
                raise ctypes.WinError()
            if transferred.value != len(self.buffer.raw) - 1:
                raise OSError('short mouse worker pipe write')
            self.buffer = None
        if self.head == len(self.frames):
            self.frames.clear()
            self.head = 0
            return
        end = min(self.head + 256, len(self.frames))
        data = b''.join(self.frames[self.head:end])
        self.head = end
        if self.head >= 4096:
            del self.frames[:self.head]
            self.head = 0
        self.buffer = ctypes.create_string_buffer(data)
        self.reset(self.overlapped.hEvent)
        if self.write(self.handle, self.buffer, len(data), ctypes.byref(transferred), ctypes.byref(self.overlapped)):
            if transferred.value != len(data):
                raise OSError('short mouse worker pipe write')
            self.buffer = None
        elif ctypes.get_last_error() != 997:  # ERROR_IO_PENDING: pump must keep running
            raise ctypes.WinError()

    def close(self):
        if self.buffer is not None:
            self.cancel(self.handle, ctypes.byref(self.overlapped))
            # Cancellation must finish before releasing the OVERLAPPED/buffer.
            self.result(self.handle, ctypes.byref(self.overlapped), ctypes.byref(DWORD()), True)
            self.buffer = None
        self.close_handle(self.overlapped.hEvent)


class Pump:
    def __init__(self, user, kernel, output):
        self.user, self.kernel, self.output = user, kernel, output
        self.controls = Controls()
        self.hook = None
        self.retired = []
        self.window = None
        self.class_name = None
        self.instance = None
        self.ll_count = self.raw_count = 0
        self.ll_at = self.raw_at = 0
        self.raw_since = 0
        self.callback_errors = 0
        self.hook_proc = HookProc(self.on_hook)
        self.window_proc = WindowProc(self.on_window)
        self.frequency = ctypes.c_int64()
        native(kernel, 'QueryPerformanceFrequency', (HANDLE,), BOOL)(ctypes.byref(self.frequency))
        self.qpc = native(kernel, 'QueryPerformanceCounter', (HANDLE,), BOOL)
        self.set_hook = native(user, 'SetWindowsHookExW', (ctypes.c_int, HookProc, HANDLE, DWORD), HANDLE)
        self.unhook = native(user, 'UnhookWindowsHookEx', (HANDLE,), BOOL)
        self.next_hook = native(user, 'CallNextHookEx', (HANDLE, ctypes.c_int, WPARAM, LPARAM), LPARAM)
        self.def_window = native(user, 'DefWindowProcW', (HANDLE, DWORD, WPARAM, LPARAM), LPARAM)

    def clock(self):
        ticks = ctypes.c_int64()
        self.qpc(ctypes.byref(ticks))
        return ticks.value * 1_000_000 // self.frequency.value

    def on_hook(self, code, message, pointer):
        try:
            if code >= 0:
                info = ctypes.cast(pointer, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                injected = bool(info.flags & 1)
                stamp = self.clock()
                if not injected:
                    self.ll_count += 1
                    self.ll_at = stamp
                if message in (0x020B, 0x020C):
                    xbutton = (info.mouseData >> 16) & 0xFFFF
                    if xbutton in (1, 2):
                        self.output.put(xbutton + 3, message == 0x020B, LOW_LEVEL,
                                        injected, 0, stamp)
                        if self.controls.mask & xbutton:
                            return 1
        except BaseException:
            self.callback_errors += 1
        try:
            return self.next_hook(self.hook, code, message, pointer)
        except BaseException:
            self.callback_errors += 1
            return 0

    def on_raw(self, packet):
        if packet.header.dwType != 0 or not packet.header.hDevice:
            return
        mouse = packet.mouse
        if not (mouse.x or mouse.y or mouse.flags):
            return
        self.raw_count += 1
        self.raw_at = stamp = self.clock()
        if not self.raw_since:
            self.raw_since = stamp
        for button, down, up in ((4, 0x40, 0x80), (5, 0x100, 0x200)):
            for flag, pressed in ((down, True), (up, False)):
                if mouse.flags & flag:
                    self.output.put(button, pressed, RAW_INPUT, False, 0, stamp)

    def on_window(self, window, message, wparam, lparam):
        try:
            if message == 0x00FF:
                packet = RAWINPUT()
                size = DWORD(ctypes.sizeof(packet))
                count = native(self.user, 'GetRawInputData', (HANDLE, DWORD, HANDLE, HANDLE, DWORD), DWORD)(
                    lparam, 0x10000003, ctypes.byref(packet), ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
                if count == ctypes.sizeof(packet) and packet.header.dwSize >= count:
                    self.on_raw(packet)
        except BaseException:
            self.callback_errors += 1
        return self.def_window(window, message, wparam, lparam)

    def start(self):
        self.instance = native(self.kernel, 'GetModuleHandleW', (ctypes.c_wchar_p,), HANDLE)(None)
        self.class_name = 'SamsaraMouseWorker_' + str(os.getpid())
        wc = WNDCLASS()
        wc.proc, wc.instance, wc.name = self.window_proc, self.instance, self.class_name
        if not native(self.user, 'RegisterClassW', (HANDLE,), WORD)(ctypes.byref(wc)):
            self.class_name = None
            raise ctypes.WinError()
        self.window = native(self.user, 'CreateWindowExW',
                             (DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              HANDLE, HANDLE, HANDLE, HANDLE), HANDLE)(
                                  0, self.class_name, '', 0, 0, 0, 0, 0, -3, None, self.instance, None)
        if not self.window:
            raise ctypes.WinError()
        device = RAWINPUTDEVICE(1, 2, 0x100, self.window)
        if not native(self.user, 'RegisterRawInputDevices', (HANDLE, DWORD, DWORD), BOOL)(
                ctypes.byref(device), 1, ctypes.sizeof(device)):
            raise ctypes.WinError()
        self.hook = self.set_hook(14, self.hook_proc, self.instance, 0)
        if not self.hook:
            raise ctypes.WinError()
        self.output.put(0, False, READY, False, VERSION, self.clock())

    def refresh(self):
        # Install before removing the old handle, retaining the current mask.
        replacement = self.set_hook(14, self.hook_proc, self.instance, 0)
        error = 0 if replacement else ctypes.get_last_error()
        if replacement:
            if self.hook:
                self.retired.append(self.hook)
            self.hook = replacement
        for old in tuple(self.retired):
            if self.unhook(old) or ctypes.get_last_error() == 1404:
                self.retired.remove(old)
            else:
                error = ctypes.get_last_error()
        self.output.put(0, bool(replacement), REFRESHED, not self.retired, error, self.clock())

    def close(self):
        # Always release the low-level hook before cleaning up pipes/windows.
        for handle in self.retired + ([self.hook] if self.hook else []):
            self.unhook(handle)
        self.hook = None
        self.retired.clear()
        if self.window:
            device = RAWINPUTDEVICE(1, 2, 1, None)
            native(self.user, 'RegisterRawInputDevices', (HANDLE, DWORD, DWORD), BOOL)(
                ctypes.byref(device), 1, ctypes.sizeof(device))
            native(self.user, 'DestroyWindow', (HANDLE,), BOOL)(self.window)
            self.window = None
        if self.class_name:
            native(self.user, 'UnregisterClassW', (ctypes.c_wchar_p, HANDLE), BOOL)(self.class_name, self.instance)
            self.class_name = None

    def run(self, read_control, pump_messages, wait):
        next_status = 0
        try:
            while not self.controls.stopping:
                refresh = self.controls.feed(read_control())
                if self.controls.stopping:
                    break
                if self.controls.started and not self.hook:
                    self.start()
                if refresh and self.hook:
                    self.refresh()
                if not pump_messages():
                    break
                now = self.clock()
                if self.hook and now >= next_status:
                    self.output.put(0, False, LL_STATUS, False, self.ll_count, self.ll_at)
                    # Earliest raw event in this batch: its LL callback normally
                    # precedes raw delivery slightly. Stamping all 50 events at
                    # the LAST raw event would falsely imply no LL callbacks.
                    self.output.put(0, False, RAW_STATUS, False, self.raw_count,
                                    self.raw_since or self.raw_at)
                    self.raw_since = 0
                    self.output.put(0, False, HEARTBEAT, False, self.callback_errors, now)
                    next_status = now + STATUS_US
                self.output.flush()
                wait(POLL_MS)
        finally:
            self.close()


def main():
    if sys.platform != 'win32':
        return 1
    gc.disable()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    current = native(kernel, 'GetCurrentProcess', (), HANDLE)()
    if not native(kernel, 'SetPriorityClass', (HANDLE, DWORD), BOOL)(current, 0x80):  # HIGH_PRIORITY_CLASS
        raise ctypes.WinError()
    std_handle = native(kernel, 'GetStdHandle', (DWORD,), HANDLE)
    stdin, stdout = std_handle(-10), std_handle(-11)
    peek = native(kernel, 'PeekNamedPipe', (HANDLE, HANDLE, DWORD, HANDLE, HANDLE, HANDLE), BOOL)
    read = native(kernel, 'ReadFile', (HANDLE, HANDLE, DWORD, HANDLE, HANDLE), BOOL)

    def read_control():
        available = DWORD()
        if not peek(stdin, None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() in (109, 232):
                return None
            raise ctypes.WinError()
        if not available.value:
            return b''
        buffer = ctypes.create_string_buffer(min(available.value, 256))
        count = DWORD()
        if not read(stdin, buffer, len(buffer), ctypes.byref(count), None):
            if ctypes.get_last_error() in (109, 232):
                return None
            raise ctypes.WinError()
        return buffer.raw[:count.value] if count.value else None

    message = MSG()
    peek_message = native(user, 'PeekMessageW', (HANDLE, HANDLE, DWORD, DWORD, DWORD), BOOL)
    translate = native(user, 'TranslateMessage', (HANDLE,), BOOL)
    dispatch = native(user, 'DispatchMessageW', (HANDLE,), LPARAM)

    def pump_messages():
        # A continuous movement flood must still check stdin EOF every turn.
        for _ in range(64):
            if not peek_message(ctypes.byref(message), None, 0, 0, 1):
                break
            if message.message == 0x12:
                return False
            translate(ctypes.byref(message))
            dispatch(ctypes.byref(message))
        return True

    wait = native(user, 'MsgWaitForMultipleObjectsEx', (DWORD, HANDLE, DWORD, DWORD, DWORD), DWORD)
    output = Output(kernel, stdout)
    try:
        Pump(user, kernel, output).run(read_control, pump_messages,
                                       lambda ms: wait(0, None, ms, 0x04FF, 0x0004))
    finally:
        output.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
