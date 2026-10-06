"""Control the FiiO KA11 from Windows: volume, indicator LED, digital filter, UAC mode.

Protocol as used by FiiO's own FiiO Control Android app (4.2.1) for the KA11, worked out for
interoperability: 16-byte HID output reports on interface 0 (no report ID); replies arrive as
input reports. Windows' built-in HID driver is enough - no driver changes.

  read  : [seq, 0x12, 0xE4, 0xA2, reg_hi, reg_lo, len, 0...]          reply[6]==seq, reply[7]==len, data at [8:]
  write : [seq, 0x11, 0xA0, 0xA2, reg_hi, reg_lo, len, value, 0...]   (no reply)
  dac   : [seq, 0x11, 0x80, 0x60, 0, 0, 5, a, b, c, 1, value, 0...]   reply[6]==seq, reply[7]==0 on success
  led   : [0x08, 0x51, a, b, 0...]                                     (no reply)

Registers: 0x0010/0x0011 device volume (attenuation, 0.5 dB steps; shown as level 0-50 where
50 = 0 dB and 0 = mute), 0x0020 sample rate (Hz, 4 bytes LE), 0x0047 UAC mode, 0x0200 LED,
0x0202 filter. The DAC's volume registers are shared with Windows' USB Audio volume, so a volume
write must include Windows' attenuation (see KA11.set_level).

Usage:
  python ka11.py                              show current state
  python ka11.py set <0-50> [--force]         set volume level (--force allows a >10 dB jump up)
  python ka11.py led <on|off-once|off>        set indicator LED
  python ka11.py filter <0-4>                 set digital filter (see FILTERS)
  python ka11.py uac <1|2>                    switch USB Audio Class 1.0 / 2.0
  python ka11.py restore                      restore defaults (LED on, filter 0)
"""
import ctypes
import math
import random
import sys
import time
from ctypes import wintypes as wt

import winvolume

VID, PID = 0x2972, 0x0081  # PID as seen in UAC 2.0 mode
PRODUCT_NAME = "FIIO KA11"
REG_FILTER = (0x02, 0x02)
REG_VOLUME = (0x00, 0x10)
REG_VOLUME_2 = (0x00, 0x11)
DAC_VOLUME_REGS = ((9, 0, 2), (9, 0, 1), (7, 0, 1), (7, 0, 0))
REG_LED = (0x02, 0x00)
REG_SAMPLE_RATE = (0x00, 0x20)
REG_UAC = (0x00, 0x47)
DAC_FILTER_REG = (9, 0, 0)
JUMP_GUARD_DB = 10  # biggest volume increase allowed in one change without confirmation
# Names in the order the app lists them; index 4 is stored as 0x22, the rest as (index << 6) | 2.
FILTERS = ("Minimum phase fast roll-off", "Fast roll-off, phase-compensated", "Minimum phase slow roll-off",
           "Slow roll-off, phase-compensated", "Non-oversampling")
# The app's three indicator options; "off-once" presumably lasts until the dongle is replugged.
LED_MODES = {"on": (0xFF, 0x00), "off-once": (0xFE, 0x01), "off": (0xFD, 0x02)}

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
                ("Counts", wt.USHORT * 10)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), wt.DWORD,
                                                 ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                      ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k32.CreateEventW.restype = wt.HANDLE
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
k32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wt.DWORD), wt.BOOL]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.CancelIo.argtypes = [wt.HANDLE]
k32.CloseHandle.argtypes = [wt.HANDLE]
hid.HidD_SetOutputReport.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]
hid.HidD_FlushQueue.argtypes = [wt.HANDLE]
hid.HidD_GetAttributes.argtypes = [wt.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetProductString.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]
hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]

INVALID_HANDLE = wt.HANDLE(-1).value


def find_path():
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    devs = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
    try:
        i = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA(cbSize=ctypes.sizeof(SP_DEVICE_INTERFACE_DATA))
            if not setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(guid), i, ctypes.byref(ifd)):
                return None
            i += 1
            need = wt.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = 8  # cbSize of SP_DEVICE_INTERFACE_DETAIL_DATA_W on x64
            setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(ifd), buf, need, None, None)
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            if f"vid_{VID:04x}&" in path.lower() and is_ka11_control(path):
                return path
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)


def is_ka11_control(path):
    """Match like the official app does - by product name, since the USB product ID may differ
    between UAC modes - and only the HID collection that takes 16-byte command reports."""
    h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)  # no access rights needed for strings/caps
    if h in (None, INVALID_HANDLE):
        return False
    try:
        name = ctypes.create_unicode_buffer(128)
        if not hid.HidD_GetProductString(h, name, ctypes.sizeof(name)) or name.value.upper() != PRODUCT_NAME:
            return False
        caps = HIDP_CAPS()
        pp = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(h, ctypes.byref(pp)):
            return False
        hid.HidP_GetCaps(pp, ctypes.byref(caps))
        hid.HidD_FreePreparsedData(pp)
        return caps.OutputReportByteLength == 17
    finally:
        k32.CloseHandle(h)


class KA11:
    def __init__(self):
        path = find_path()
        if not path:
            raise SystemExit("FiiO KA11 not found - is it plugged in?")
        # GENERIC_READ|GENERIC_WRITE, share read/write, OPEN_EXISTING, FILE_FLAG_OVERLAPPED
        self.h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, None)
        if self.h in (None, INVALID_HANDLE):
            raise SystemExit(f"Could not open KA11 HID interface (error {ctypes.get_last_error()})")
        self.event = k32.CreateEventW(None, True, False, None)
        # Replies are matched only by sequence number, so don't start every run at 0 where a
        # late reply from a previous run could be mistaken for ours.
        self.seq = random.randint(0, 250)
        self.last_response_ms = None
        attrs = HIDD_ATTRIBUTES(Size=ctypes.sizeof(HIDD_ATTRIBUTES))
        hid.HidD_GetAttributes(self.h, ctypes.byref(attrs))
        # The official app shows the USB bcdDevice as the firmware version, e.g. 0x0008 -> "0.08".
        self.firmware = f"{attrs.Version >> 8:x}.{attrs.Version & 0xFF:02x}"

    def close(self):
        k32.CloseHandle(self.event)
        k32.CloseHandle(self.h)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _next_seq(self):
        seq = self.seq
        self.seq = 0 if seq == 250 else seq + 1
        return seq

    def _send(self, packet):
        time.sleep(0.1)  # the app waits 100 ms before every transfer
        report = (ctypes.c_ubyte * 17)(0, *packet)  # report ID 0 + 16-byte payload
        if not hid.HidD_SetOutputReport(self.h, report, 17):
            raise OSError(f"HID write failed (error {ctypes.get_last_error()})")

    def _drain(self):
        """Discard input reports already queued by the driver or still pending on the device."""
        hid.HidD_FlushQueue(self.h)
        while self._recv(None, timeout_ms=30) is not None:
            pass

    def _recv(self, seq, timeout_ms=500):
        """Return the first input report whose sequence byte matches (any report if seq is None),
        or None on timeout."""
        deadline = time.monotonic() + timeout_ms / 1000
        while (remaining := deadline - time.monotonic()) > 0:
            buf = (ctypes.c_ubyte * 33)()
            ov = OVERLAPPED(hEvent=self.event)
            got = wt.DWORD()
            if not k32.ReadFile(self.h, buf, 33, None, ctypes.byref(ov)) and ctypes.get_last_error() != 997:
                raise OSError(f"HID read failed (error {ctypes.get_last_error()})")
            if k32.WaitForSingleObject(self.event, int(remaining * 1000)) != 0:
                k32.CancelIo(self.h)
                k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), True)
                return None
            k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), False)
            reply = bytes(buf)[1:]  # drop report ID
            if seq is None or reply[6] == seq:
                return reply
        return None

    def read_reg(self, reg, length=1):
        for _ in range(3):
            seq = self._next_seq()
            self._drain()
            self._send([seq, 0x12, 0xE4, 0xA2, *reg, length] + [0] * 9)
            sent = time.perf_counter()
            reply = self._recv(seq)
            if reply and reply[7] == length:
                self.last_response_ms = (time.perf_counter() - sent) * 1000
                return reply[8:8 + length]
        raise TimeoutError(f"No reply reading register {reg[0]:02x}{reg[1]:02x}")

    def write_reg(self, reg, value):
        self._send([self._next_seq(), 0x11, 0xA0, 0xA2, *reg, 1, value] + [0] * 8)

    def write_dac(self, addr, value):
        for _ in range(3):
            seq = self._next_seq()
            self._drain()
            self._send([seq, 0x11, 0x80, 0x60, 0, 0, 5, *addr, 1, value] + [0] * 4)
            reply = self._recv(seq)
            if reply and reply[7] == 0:
                return
        raise TimeoutError(f"DAC register {bytes(addr).hex()} write not acknowledged")

    def get_led(self):
        state = tuple(self.read_reg(REG_LED, 2))
        return next((name for name, value in LED_MODES.items() if value == state), f"unknown ({bytes(state).hex()})")

    def set_led(self, mode):
        self._send([0x08, 0x51, *LED_MODES[mode]] + [0] * 12)

    def get_sample_rate(self):
        """Current playback sample rate in Hz, as reported by the dongle."""
        return int.from_bytes(self.read_reg(REG_SAMPLE_RATE, 4), "little")

    def get_level(self):
        atten = self.read_reg(REG_VOLUME)[0]
        return 50 - atten // 2 if atten < 100 else 0

    def get_filter(self):
        """Index into FILTERS."""
        raw = self.read_reg(REG_FILTER)[0]
        return 4 if raw & 0x20 else (raw >> 6) & 3  # same decoding as the app's filter page

    def set_filter(self, index):
        value = 0x22 if index == 4 else (index << 6) | 2
        self.write_reg(REG_FILTER, value)
        self.write_dac(DAC_FILTER_REG, value)

    def get_uac(self):
        """USB Audio Class mode: 1 (UAC 1.0) or 2 (UAC 2.0)."""
        return 1 if self.read_reg(REG_UAC)[0] == 1 else 2

    def set_uac(self, version):
        """Switch UAC 1.0/2.0. The setting is stored straight away but (as tested on firmware 0.08)
        only takes effect the next time the dongle is plugged in."""
        self.write_reg(REG_UAC, version)

    def restore_defaults(self):
        """The app's "Restore to default settings": S/PDIF off, LED on, default filter.
        Volume and UAC mode are left as they are."""
        self._send([0x0A, 0x52, 0x00] + [0] * 13)  # S/PDIF output off (not exposed on the KA11)
        time.sleep(1)
        self.set_led("on")
        time.sleep(1)
        self.set_filter(0)

    def set_level(self, level, allow_jump=False):
        """level 0-50 like the app's slider (1 dB per step).

        The DAC has a single attenuator that Windows' USB Audio volume also drives, so the value
        written to it must be Windows' attenuation plus the device attenuation - exactly what the
        official app does (it reads the USB volume, keeps whole dB, and adds them). Writing the device
        attenuation alone would discard Windows' volume and make playback much louder than the
        Windows slider says. If Windows' volume can't be read, nothing is changed.

        Jump guard: raising the volume by more than JUMP_GUARD_DB in one go raises VolumeJumpError
        unless allow_jump is set (coming out of mute counts as starting from level 0)."""
        increase = level - self.get_level()
        if increase > JUMP_GUARD_DB and not allow_jump:
            raise VolumeJumpError(increase)
        atten = 100 if level == 0 else (50 - level) * 2
        dac_value = 255 if atten >= 100 else min(windows_attenuation() + atten, 254)
        self.write_reg(REG_VOLUME, atten)
        self.write_reg(REG_VOLUME_2, atten)
        for addr in DAC_VOLUME_REGS:
            self.write_dac(addr, dac_value)


class VolumeJumpError(Exception):
    def __init__(self, increase_db):
        super().__init__(f"That would raise the volume by {increase_db} dB at once")
        self.increase_db = increase_db


def windows_attenuation():
    """Windows' current volume for the KA11 as DAC attenuation steps (0.5 dB each)."""
    state = winvolume.ka11_volume()
    if state is None:
        raise RuntimeError("Couldn't read the Windows volume for the KA11, so the volume wasn't changed")
    db, hardware, _ = state
    if not hardware:
        return 0  # Windows is scaling the audio in software, so the DAC carries no Windows attenuation
    return max(0, -math.floor(db + 1e-6) * 2)  # whole dB like the official app, never louder than Windows


def main(argv):
    with KA11() as dev:
        if len(argv) >= 2 and argv[0] == "set":
            level = int(argv[1])
            if not 0 <= level <= 50:
                raise SystemExit("Level must be 0-50")
            try:
                dev.set_level(level, allow_jump="--force" in argv)
            except VolumeJumpError as e:
                raise SystemExit(f"{e}. Add --force if you really want that.")
            print(f"Set volume level to {level}")
        elif len(argv) >= 2 and argv[0] == "led":
            if argv[1] not in LED_MODES:
                raise SystemExit(f"LED mode must be one of: {', '.join(LED_MODES)}")
            dev.set_led(argv[1])
            print(f"Set LED to {argv[1]}")
        elif len(argv) >= 2 and argv[0] == "filter":
            index = int(argv[1])
            if not 0 <= index < len(FILTERS):
                raise SystemExit(f"Filter must be 0-{len(FILTERS) - 1}")
            dev.set_filter(index)
            print(f"Set filter to {FILTERS[index]}")
        elif len(argv) >= 2 and argv[0] == "uac":
            if argv[1] not in ("1", "2"):
                raise SystemExit("UAC mode must be 1 or 2")
            dev.set_uac(int(argv[1]))
            print(f"Switched to UAC {argv[1]}.0 - replug the dongle for it to take effect")
            return
        elif argv and argv[0] == "restore":
            dev.restore_defaults()
            print("Restored defaults")
        level = dev.get_level()
        db = "mute" if level == 0 else f"-{(50 - level):.0f} dB"
        print(f"Volume level: {level}/50 ({db})")
        print(f"Filter: {FILTERS[dev.get_filter()]}")
        print(f"UAC mode: {dev.get_uac()}.0")
        print(f"LED: {dev.get_led()}")
        print(f"Sample rate: {dev.get_sample_rate()} Hz")
        print(f"Firmware: {dev.firmware}")


if __name__ == "__main__":
    main(sys.argv[1:])
