"""Read the Windows endpoint volume of the KA11 (Core Audio, via raw COM calls through ctypes).

The KA11's DAC has one attenuator shared by Windows' USB Audio volume and the device volume, so
any direct DAC write must include Windows' current attenuation - the official app does the same.
"""
import ctypes
from ctypes import POINTER, WINFUNCTYPE, byref, c_float, c_void_p
from ctypes import wintypes as wt

ole32 = ctypes.OleDLL("ole32")
ole32.CoTaskMemFree.restype = None
ole32.CoTaskMemFree.argtypes = [c_void_p]

CLSCTX_ALL = 0x17
E_RENDER, DEVICE_STATE_ACTIVE, STGM_READ = 0, 0x1, 0
ENDPOINT_HARDWARE_SUPPORT_VOLUME = 0x1
VT_LPWSTR = 31


class GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, s):
        g = cls()
        ole32.CLSIDFromString(s, byref(g))
        return g


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wt.USHORT), ("r1", wt.WORD), ("r2", wt.WORD), ("r3", wt.WORD),
                ("ptr", c_void_p), ("pad", c_void_p)]


CLSID_MMDeviceEnumerator = GUID.parse("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID.parse("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID.parse("{5CDF2C82-841E-4546-9722-0CF74078229A}")
PKEY_Device_FriendlyName = PROPERTYKEY(GUID.parse("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)


def _call(obj, index, *args):
    """Call vtable slot `index` of a COM object; args are (ctype, value) pairs. Raises on failure HRESULT."""
    fn = WINFUNCTYPE(ctypes.HRESULT, c_void_p, *(a for a, _ in args))(
        ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents[index])
    return fn(obj, *(v for _, v in args))


def _release(obj):
    if obj:
        WINFUNCTYPE(wt.ULONG, c_void_p)(ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents[2])(obj)


def _friendly_name(device):
    store, pv = c_void_p(), PROPVARIANT()
    try:
        _call(device, 4, (wt.DWORD, STGM_READ), (POINTER(c_void_p), byref(store)))  # OpenPropertyStore
        _call(store, 5, (POINTER(PROPERTYKEY), byref(PKEY_Device_FriendlyName)),
              (POINTER(PROPVARIANT), byref(pv)))  # GetValue
        return ctypes.wstring_at(pv.ptr) if pv.vt == VT_LPWSTR and pv.ptr else ""
    finally:
        ole32.PropVariantClear(byref(pv))
        _release(store)


def ka11_volume(name="FIIO KA11"):
    """Return (dB, hardware_volume, muted) for the KA11's playback endpoint, or None if not found."""
    try:
        ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED; fine if this thread already initialised COM
    except OSError:
        pass
    enum, coll = c_void_p(), c_void_p()
    try:
        ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                               byref(IID_IMMDeviceEnumerator), byref(enum))
        _call(enum, 3, (ctypes.c_int, E_RENDER), (wt.DWORD, DEVICE_STATE_ACTIVE),
              (POINTER(c_void_p), byref(coll)))  # EnumAudioEndpoints
        count = wt.UINT()
        _call(coll, 3, (POINTER(wt.UINT), byref(count)))  # GetCount
        for i in range(count.value):
            device = c_void_p()
            _call(coll, 4, (wt.UINT, i), (POINTER(c_void_p), byref(device)))  # Item
            try:
                if name.lower() not in _friendly_name(device).lower():
                    continue
                vol = c_void_p()
                _call(device, 3, (POINTER(GUID), byref(IID_IAudioEndpointVolume)), (wt.DWORD, CLSCTX_ALL),
                      (c_void_p, None), (POINTER(c_void_p), byref(vol)))  # Activate
                try:
                    db, muted, support = c_float(), wt.BOOL(), wt.DWORD()
                    _call(vol, 8, (POINTER(c_float), byref(db)))  # GetMasterVolumeLevel
                    _call(vol, 15, (POINTER(wt.BOOL), byref(muted)))  # GetMute
                    _call(vol, 19, (POINTER(wt.DWORD), byref(support)))  # QueryHardwareSupport
                    return db.value, bool(support.value & ENDPOINT_HARDWARE_SUPPORT_VOLUME), bool(muted.value)
                finally:
                    _release(vol)
            finally:
                _release(device)
        return None
    finally:
        _release(coll)
        _release(enum)


if __name__ == "__main__":
    print(ka11_volume())
