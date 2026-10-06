"""Windows shell integration via ctypes: tray icon and menu, global hotkeys, device plug/unplug
notifications, single-instance handling and Start with Windows.

Everything hangs off one hidden window created on the Tk thread. Tcl's Windows event loop
dispatches its messages, but the window procedure only records events for the app to poll:
calling Tk from inside it can crash Tkinter (see Shell).
"""
import collections
import ctypes
import sys
import winreg
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CLASS_NAME = "KA11ControlShellWindow"
APP_NAME = "KA11 Control"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

WM_DESTROY, WM_COMMAND, WM_HOTKEY, WM_DEVICECHANGE = 0x0002, 0x0111, 0x0312, 0x0219
WM_LBUTTONUP, WM_RBUTTONUP, WM_NULL = 0x0202, 0x0205, 0x0000
WM_APP = 0x8000
WM_TRAY = WM_APP + 1  # tray icon mouse events
WM_SHOW = WM_APP + 2  # another instance asked us to show the window
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE, DBT_DEVTYP_DEVICEINTERFACE = 0x8000, 0x8004, 5
MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_GRAYED = 0x0, 0x800, 0x8, 0x1
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_BOTTOMALIGN = 0x2, 0x100, 0x20
ERROR_ALREADY_EXISTS = 183
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


class DEV_BROADCAST_DEVICEINTERFACE_W(ctypes.Structure):
    _fields_ = [("dbcc_size", wt.DWORD), ("dbcc_devicetype", wt.DWORD), ("dbcc_reserved", wt.DWORD),
                ("dbcc_classguid", GUID), ("dbcc_name", wt.WCHAR * 1)]


class DEV_BROADCAST_HDR(ctypes.Structure):
    _fields_ = [("dbch_size", wt.DWORD), ("dbch_devicetype", wt.DWORD), ("dbch_reserved", wt.DWORD)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.RegisterDeviceNotificationW.restype = wt.HANDLE
user32.RegisterDeviceNotificationW.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD]
user32.UnregisterDeviceNotification.argtypes = [wt.HANDLE]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.DestroyIcon.argtypes = [wt.HICON]
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.DestroyWindow.argtypes = [wt.HWND]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

GUID_DEVINTERFACE_HID = GUID(0x4D1E55B2, 0xF16F, 0x11CF, (ctypes.c_ubyte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))


# ---------- single instance ----------

_mutex = None


def claim_single_instance():
    """True if this is the only running copy. Otherwise asks the running copy to show its window."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, "Local\\KA11Control.SingleInstance")
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return True
    hwnd = user32.FindWindowW(CLASS_NAME, None)
    if hwnd:
        user32.PostMessageW(hwnd, WM_SHOW, 0, 0)
    return False


# ---------- Start with Windows ----------

def launch_command():
    """Command line that starts this app hidden in the tray."""
    if getattr(sys, "frozen", False):  # packaged exe
        return f'"{sys.executable}" --tray'
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pythonw}" "{sys.argv[0]}" --tray'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def allow_dark_menus(dark):
    """Dark tray menus in dark mode (uxtheme's SetPreferredAppMode; undocumented but stable since 1903)."""
    try:
        uxtheme = ctypes.WinDLL("uxtheme")
        uxtheme[135](2 if dark else 3)  # SetPreferredAppMode: ForceDark / ForceLight
        uxtheme[136]()  # FlushMenuThemes
    except (OSError, AttributeError):
        pass


# ---------- the hidden shell window ----------

class Shell:
    """Owns the hidden window, tray icon, hotkeys and device notifications.

    The window procedure never calls back into the app. Windows can run it in the middle of Tk's
    own event processing (a USB unplug is broadcast to every window at once), and calling Tk from
    there corrupts Tkinter's thread-state bookkeeping, which kills the process with
    "Fatal Python error: PyEval_RestoreThread". Instead it records small event codes, and the app
    collects them with poll() from an ordinary Tk timer.

    menu_items() is the one exception: it builds the tray menu while the menu is open. It must not
    touch Tk either (it only reads settings).
    """

    HOTKEYS = {1: ("volume_up", MOD_CONTROL | MOD_ALT, 0x26),  # Ctrl+Alt+Up
               2: ("volume_down", MOD_CONTROL | MOD_ALT, 0x28),  # Ctrl+Alt+Down
               3: ("mute", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x4D)}  # Ctrl+Alt+M

    def __init__(self, menu_items):
        self.menu_items = menu_items
        self.events = collections.deque()  # ("tray_click",), ("hotkey", name), ("command", id), ...
        self.icon = None
        self.tip = APP_NAME
        self.tray_added = False
        self.hotkeys_on = set()
        self.notify = None
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")  # Explorer restarted
        # Set everything _wndproc reads before CreateWindowExW: Windows sends messages during creation.
        self._proc = WNDPROC(self._wndproc)  # keep a reference: the window calls it for its lifetime
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._proc, hInstance=hinst,
                         lpszClassName=CLASS_NAME)
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, CLASS_NAME, APP_NAME, 0, 0, 0, 0, 0, None, None, hinst, None)
        f = DEV_BROADCAST_DEVICEINTERFACE_W(dbcc_size=ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE_W),
                                            dbcc_devicetype=DBT_DEVTYP_DEVICEINTERFACE,
                                            dbcc_classguid=GUID_DEVINTERFACE_HID)
        self.notify = user32.RegisterDeviceNotificationW(self.hwnd, ctypes.byref(f), 0)

    def poll(self):
        """Events recorded since the last call, oldest first. Call from the Tk thread."""
        out = []
        while self.events:
            out.append(self.events.popleft())
        return out

    # tray icon
    def set_tray_icon(self, ico_path, tip):
        old = self.icon
        self.icon = user32.LoadImageW(None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        self.tip = tip
        self._notify(NIM_MODIFY if self.tray_added else NIM_ADD)
        self.tray_added = True
        if old:
            user32.DestroyIcon(old)

    def set_tip(self, tip):
        if self.tray_added and tip != self.tip:
            self.tip = tip
            self._notify(NIM_MODIFY)

    def _notify(self, action):
        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                              uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY, hIcon=self.icon)
        nid.szTip = self.tip[:127]
        shell32.Shell_NotifyIconW(action, ctypes.byref(nid))

    # hotkeys
    def set_hotkeys(self, enabled):
        """Returns the names of hotkeys that couldn't be registered (taken by another app)."""
        failed = []
        for hid, (name, mods, vk) in self.HOTKEYS.items():
            if enabled and hid not in self.hotkeys_on:
                if user32.RegisterHotKey(self.hwnd, hid, mods, vk):
                    self.hotkeys_on.add(hid)
                else:
                    failed.append(name)
            elif not enabled and hid in self.hotkeys_on:
                user32.UnregisterHotKey(self.hwnd, hid)
                self.hotkeys_on.discard(hid)
        return failed

    def show_menu(self):
        menu = user32.CreatePopupMenu()
        for cmd, label, flags in self.menu_items():
            if label is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, MF_STRING | flags, cmd, label)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)  # so the menu closes when you click elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_BOTTOMALIGN, pt.x, pt.y, 0,
                                    self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            self.events.append(("command", cmd))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        # Record only; see the class docstring for why nothing here may call into Tk.
        try:
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONUP:
                    self.events.append(("tray_click",))
                elif lparam == WM_RBUTTONUP:
                    self.show_menu()
                return 0
            if msg == WM_HOTKEY and wparam in self.HOTKEYS:
                self.events.append(("hotkey", self.HOTKEYS[wparam][0]))
                return 0
            if msg == WM_DEVICECHANGE and wparam in (DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE) and lparam:
                hdr = ctypes.cast(lparam, ctypes.POINTER(DEV_BROADCAST_HDR)).contents
                if hdr.dbch_devicetype == DBT_DEVTYP_DEVICEINTERFACE:
                    name = ctypes.wstring_at(lparam + DEV_BROADCAST_DEVICEINTERFACE_W.dbcc_name.offset)
                    if "vid_2972" in name.lower():  # FiiO
                        self.events.append(("device", wparam == DBT_DEVICEARRIVAL))
                return 1
            if msg == WM_SHOW:
                self.events.append(("show",))
                return 0
            if msg == self.taskbar_created and self.tray_added:
                self._notify(NIM_ADD)  # Explorer restarted and lost our icon
                return 0
        except Exception:  # never let an exception escape into Windows' message dispatch
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def close(self):
        self.set_hotkeys(False)
        if self.tray_added:
            self._notify(NIM_DELETE)
            self.tray_added = False
        if self.notify:
            user32.UnregisterDeviceNotification(self.notify)
        if self.icon:
            user32.DestroyIcon(self.icon)
        user32.DestroyWindow(self.hwnd)
