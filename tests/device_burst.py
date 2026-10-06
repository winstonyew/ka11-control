"""Helper for test_app.test_unplug_burst_does_not_crash: runs the app against the fake dongle and
sends it the same WM_DEVICECHANGE broadcast a real unplug/replug produces, many times over, while
Tk timers keep firing. Before the fix this died with "Fatal Python error: PyEval_RestoreThread"."""
import ctypes
import os
import runpy
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="ka11-burst-")

import ka11  # noqa: E402
import winvolume  # noqa: E402
from fake_ka11 import FakeKA11  # noqa: E402

ka11.KA11 = FakeKA11
ka11.find_path = lambda: "fake"  # the "unplugged" check shouldn't look at real hardware
winvolume.shared_format = lambda: (48000, 32, 2)
winvolume.is_default = lambda: True
mod = runpy.run_path(os.path.join(os.path.dirname(HERE), "ka11_control.pyw"), run_name="burst")
ws = mod["winshell"]
ws.Shell.set_tray_icon = lambda self, *a: None  # no tray icon or global hotkeys from a test
ws.Shell.set_hotkeys = lambda self, enabled: []
ws.claim_single_instance = lambda: True

user32 = ctypes.WinDLL("user32")
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetParent.restype = wt.HWND

ctypes.windll.shcore.SetProcessDpiAwareness(1)
root = tk.Tk()
root.attributes("-alpha", 0.0)
app = mod["App"](root)

name = r"\\?\HID#VID_2972&PID_0081&MI_00#8&0&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
offset = ws.DEV_BROADCAST_DEVICEINTERFACE_W.dbcc_name.offset
buf = ctypes.create_string_buffer(offset + (len(name) + 1) * 2)
hdr = ws.DEV_BROADCAST_DEVICEINTERFACE_W.from_buffer(buf)
hdr.dbcc_size, hdr.dbcc_devicetype, hdr.dbcc_classguid = len(buf), ws.DBT_DEVTYP_DEVICEINTERFACE, ws.GUID_DEVINTERFACE_HID
ctypes.memmove(ctypes.addressof(buf) + offset, ctypes.create_unicode_buffer(name), (len(name) + 1) * 2)
shell_hwnd = app.shell.hwnd
tk_hwnd = user32.GetParent(root.winfo_id())


def blast():
    time.sleep(1.5)
    for _ in range(300):
        # Like Windows: the same broadcast reaches every top-level window of the thread.
        user32.SendMessageW(shell_hwnd, ws.WM_DEVICECHANGE, ws.DBT_DEVICEREMOVECOMPLETE, ctypes.addressof(buf))
        user32.SendMessageW(tk_hwnd, ws.WM_DEVICECHANGE, 0x0007, 0)  # DBT_DEVNODES_CHANGED
        user32.SendMessageW(shell_hwnd, ws.WM_DEVICECHANGE, ws.DBT_DEVICEARRIVAL, ctypes.addressof(buf))
    time.sleep(1.5)  # let the queued events be handled too
    print("survived", flush=True)
    os._exit(0)


def tick():  # the app's animation and polling timers mean Tk calls into Python constantly
    root.after(1, tick)


tick()
threading.Thread(target=blast, daemon=True).start()
root.mainloop()
