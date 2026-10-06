"""The KA11's Windows audio endpoint, via Core Audio (raw COM calls through ctypes).

- ka11_volume(): Windows volume in dB. The KA11's DAC has one attenuator shared by Windows' USB
  Audio volume and the device volume, so any direct DAC write must include Windows' attenuation -
  the official app does the same.
- shared_format(): the sample rate/bit depth Windows mixes at in shared mode.
- is_default() / make_default(): whether the KA11 is the default playback device, and setting it.
"""
import ctypes
import struct
from ctypes import POINTER, WINFUNCTYPE, byref, c_float, c_void_p
from ctypes import wintypes as wt

ole32 = ctypes.OleDLL("ole32")
ole32.CoTaskMemFree.restype = None
ole32.CoTaskMemFree.argtypes = [c_void_p]

CLSCTX_ALL = 0x17
E_RENDER, DEVICE_STATE_ACTIVE, STGM_READ = 0, 0x1, 0
E_CONSOLE, E_MULTIMEDIA, E_COMMUNICATIONS = 0, 1, 2
ENDPOINT_HARDWARE_SUPPORT_VOLUME = 0x1
VT_LPWSTR, VT_BLOB = 31, 65
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


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
    # vt + 3 reserved words, then the value union. For VT_BLOB the union holds a ULONG size
    # (padded to 8 bytes) followed by the data pointer.
    _fields_ = [("vt", wt.USHORT), ("r1", wt.WORD), ("r2", wt.WORD), ("r3", wt.WORD),
                ("ptr", c_void_p), ("ptr2", c_void_p)]


CLSID_MMDeviceEnumerator = GUID.parse("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID.parse("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID.parse("{5CDF2C82-841E-4546-9722-0CF74078229A}")
# IPolicyConfig is undocumented but is what Windows' own Sound settings use to change the default
# device; it has been stable since Windows 7.
CLSID_PolicyConfigClient = GUID.parse("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
IID_IPolicyConfig = GUID.parse("{F8679F50-850A-41CF-9C72-430F290290C8}")
PKEY_Device_FriendlyName = PROPERTYKEY(GUID.parse("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)
PKEY_AudioEngine_DeviceFormat = PROPERTYKEY(GUID.parse("{F19F064D-082C-4E27-BC73-6882A1BB8E4C}"), 0)


def _call(obj, index, *args):
    """Call vtable slot `index` of a COM object; args are (ctype, value) pairs. Raises on failure HRESULT."""
    fn = WINFUNCTYPE(ctypes.HRESULT, c_void_p, *(a for a, _ in args))(
        ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents[index])
    return fn(obj, *(v for _, v in args))


def _release(obj):
    if obj:
        WINFUNCTYPE(wt.ULONG, c_void_p)(ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents[2])(obj)


def _init_com():
    try:
        ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED; fine if this thread already initialised COM
    except OSError:
        pass


def _property(device, key):
    """Read a property from a device's store; returns str for strings, bytes for blobs, else None."""
    store, pv = c_void_p(), PROPVARIANT()
    try:
        _call(device, 4, (wt.DWORD, STGM_READ), (POINTER(c_void_p), byref(store)))  # OpenPropertyStore
        _call(store, 5, (POINTER(PROPERTYKEY), byref(key)), (POINTER(PROPVARIANT), byref(pv)))  # GetValue
        if pv.vt == VT_LPWSTR and pv.ptr:
            return ctypes.wstring_at(pv.ptr)
        if pv.vt == VT_BLOB and pv.ptr2:
            size = ctypes.c_ulong.from_address(ctypes.addressof(pv) + 8).value
            return ctypes.string_at(pv.ptr2, size)
        return None
    finally:
        ole32.PropVariantClear(byref(pv))
        _release(store)


def _device_id(device):
    p = ctypes.c_wchar_p()
    _call(device, 5, (POINTER(ctypes.c_wchar_p), byref(p)))  # GetId
    try:
        return p.value
    finally:
        ole32.CoTaskMemFree(p)


def _enumerator():
    _init_com()
    enum = c_void_p()
    ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL, byref(IID_IMMDeviceEnumerator),
                           byref(enum))
    return enum


def _with_ka11(fn, name="FIIO KA11"):
    """Run fn(device) on the KA11's active playback endpoint; None if it isn't there."""
    enum, coll = _enumerator(), c_void_p()
    try:
        _call(enum, 3, (ctypes.c_int, E_RENDER), (wt.DWORD, DEVICE_STATE_ACTIVE),
              (POINTER(c_void_p), byref(coll)))  # EnumAudioEndpoints
        count = wt.UINT()
        _call(coll, 3, (POINTER(wt.UINT), byref(count)))  # GetCount
        for i in range(count.value):
            device = c_void_p()
            _call(coll, 4, (wt.UINT, i), (POINTER(c_void_p), byref(device)))  # Item
            try:
                if name.lower() in (_property(device, PKEY_Device_FriendlyName) or "").lower():
                    return fn(device)
            finally:
                _release(device)
        return None
    finally:
        _release(coll)
        _release(enum)


def ka11_volume():
    """(dB, hardware_volume, muted) for the KA11's playback endpoint, or None if not found."""
    def read(device):
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
    return _with_ka11(read)


def shared_format():
    """(sample_rate, bits, channels) Windows uses for the KA11 in shared mode, or None."""
    def read(device):
        blob = _property(device, PKEY_AudioEngine_DeviceFormat)
        if not blob or len(blob) < 16:
            return None
        tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", blob)
        if tag == WAVE_FORMAT_EXTENSIBLE and len(blob) >= 20:
            bits = struct.unpack_from("<H", blob, 18)[0] or bits  # valid bits, e.g. 24 in a 32-bit container
        return rate, bits, channels
    return _with_ka11(read)


def _default_id():
    enum, device = _enumerator(), c_void_p()
    try:
        _call(enum, 4, (ctypes.c_int, E_RENDER), (ctypes.c_int, E_MULTIMEDIA),
              (POINTER(c_void_p), byref(device)))  # GetDefaultAudioEndpoint
        return _device_id(device)
    except OSError:
        return None  # no playback devices at all
    finally:
        _release(device)
        _release(enum)


def is_default():
    """True/False whether the KA11 is the default playback device; None if it isn't connected."""
    ka11_id = _with_ka11(_device_id)
    return None if ka11_id is None else ka11_id == _default_id()


def make_default():
    """Make the KA11 the default playback device for all roles. Returns False if it isn't connected."""
    ka11_id = _with_ka11(_device_id)
    if ka11_id is None:
        return False
    policy = c_void_p()
    ole32.CoCreateInstance(byref(CLSID_PolicyConfigClient), None, CLSCTX_ALL, byref(IID_IPolicyConfig),
                           byref(policy))
    try:
        for role in (E_CONSOLE, E_MULTIMEDIA, E_COMMUNICATIONS):
            _call(policy, 13, (wt.LPCWSTR, ka11_id), (ctypes.c_int, role))  # SetDefaultEndpoint
        return True
    finally:
        _release(policy)


if __name__ == "__main__":
    print("volume:", ka11_volume())
    print("shared format:", shared_format())
    print("is default:", is_default())
