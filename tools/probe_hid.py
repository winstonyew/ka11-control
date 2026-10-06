"""Read-only probe of the FiiO KA11 HID interface(s): lists collections and report capabilities."""
import ctypes
from ctypes import wintypes as wt

VID, PID = 0x2972, 0x0081

hid = ctypes.WinDLL("hid")
setupapi = ctypes.WinDLL("setupapi")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("guid", GUID), ("flags", wt.DWORD), ("reserved", ctypes.c_void_p)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT), ("ProductID", wt.USHORT), ("Version", wt.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT), ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT), ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT), ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT), ("NumberOutputValueCaps", wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT), ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


# HIDP_VALUE_CAPS / HIDP_BUTTON_CAPS share a 72-byte layout; we only read the leading fields.
class HIDP_CAPS_COMMON(ctypes.Structure):
    _fields_ = [("UsagePage", wt.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", wt.USHORT), ("LinkCollection", wt.USHORT),
                ("LinkUsage", wt.USHORT), ("LinkUsagePage", wt.USHORT),
                ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte), ("IsAbsolute", ctypes.c_ubyte),
                ("HasNull", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte),
                ("BitSize", wt.USHORT), ("ReportCount", wt.USHORT), ("Reserved2", wt.USHORT * 5),
                ("UnitsExp", wt.ULONG), ("Units", wt.ULONG),
                ("LogicalMin", wt.LONG), ("LogicalMax", wt.LONG),
                ("PhysicalMin", wt.LONG), ("PhysicalMax", wt.LONG),
                ("UsageMin", wt.USHORT), ("UsageMax", wt.USHORT),
                ("pad", ctypes.c_ubyte * 12)]


setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), wt.DWORD,
                                                 ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                      ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
for fn in ("HidP_GetValueCaps", "HidP_GetButtonCaps"):
    getattr(hid, fn).argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wt.USHORT), ctypes.c_void_p]


def hid_paths():
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    devs = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
    i = 0
    while True:
        ifd = SP_DEVICE_INTERFACE_DATA(cbSize=ctypes.sizeof(SP_DEVICE_INTERFACE_DATA))
        if not setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(guid), i, ctypes.byref(ifd)):
            break
        i += 1
        need = wt.DWORD()
        setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
        buf = ctypes.create_string_buffer(need.value)
        ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = 8  # cbSize on x64
        setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(ifd), buf, need, None, None)
        yield ctypes.wstring_at(ctypes.addressof(buf) + 4)


def dump_caps(pp, kind, n, fn, label):
    if not n:
        return
    arr = (HIDP_CAPS_COMMON * n)()
    cnt = wt.USHORT(n)
    getattr(hid, fn)(kind, arr, ctypes.byref(cnt), pp)
    for c in arr[:cnt.value]:
        usage = f"{c.UsageMin:#x}-{c.UsageMax:#x}" if c.IsRange else f"{c.UsageMin:#x}"
        print(f"    {label}: reportID={c.ReportID} page={c.UsagePage:#06x} usage={usage} "
              f"bits={c.BitSize}x{c.ReportCount} logical=[{c.LogicalMin},{c.LogicalMax}]")


for path in hid_paths():
    if f"vid_{VID:04x}&pid_{PID:04x}" not in path.lower():
        continue
    print(path)
    h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)  # no access rights needed for attrs/caps
    if h in (None, wt.HANDLE(-1).value):
        print("  open failed", ctypes.get_last_error())
        continue
    buf = ctypes.create_unicode_buffer(256)
    for name, fn in (("Manufacturer", "HidD_GetManufacturerString"), ("Product", "HidD_GetProductString")):
        if getattr(hid, fn)(h, buf, ctypes.sizeof(buf)):
            print(f"  {name}: {buf.value}")
    pp = ctypes.c_void_p()
    hid.HidD_GetPreparsedData(h, ctypes.byref(pp))
    caps = HIDP_CAPS()
    hid.HidP_GetCaps(pp, ctypes.byref(caps))
    print(f"  TopLevel page={caps.UsagePage:#06x} usage={caps.Usage:#x} "
          f"in={caps.InputReportByteLength} out={caps.OutputReportByteLength} feat={caps.FeatureReportByteLength}")
    for kind, name in ((0, "Input"), (1, "Output"), (2, "Feature")):
        dump_caps(pp, kind, getattr(caps, f"Number{name}ValueCaps"), "HidP_GetValueCaps", f"{name} value")
        dump_caps(pp, kind, getattr(caps, f"Number{name}ButtonCaps"), "HidP_GetButtonCaps", f"{name} button")
    hid.HidD_FreePreparsedData(pp)
    k32.CloseHandle(h)
