"""Protocol, volume maths and helpers. No hardware needed: the HID transport is replaced."""
import os
import runpy

import pytest

import ka11
import settings
import updates


class Transport:
    """Records sent packets and answers reads from a fake register map."""

    def __init__(self, registers=None):
        self.sent = []
        self.registers = registers or {}

    def attach(self, dev):
        dev._send = self.send
        dev._recv = self.recv
        dev._drain = lambda: None

    def send(self, packet):
        self.sent.append(list(packet))

    def recv(self, seq, timeout_ms=500):
        last = self.sent[-1]
        reply = [0] * 32
        reply[6] = seq
        if last[1] == 0x12:  # register read
            reg, length = (last[4], last[5]), last[6]
            data = self.registers.get(reg, [0] * length)
            reply[7] = length
            reply[8:8 + length] = data
        return bytes(reply)  # DAC writes are acknowledged with reply[7] == 0


@pytest.fixture
def dev(monkeypatch):
    d = object.__new__(ka11.KA11)  # skip opening real hardware
    d.seq = 7
    d.last_response_ms = None
    d.firmware = "0.08"
    t = Transport({ka11.REG_VOLUME: [60]})
    t.attach(d)
    d.transport = t
    monkeypatch.setattr(ka11, "windows_attenuation", lambda: 56)
    return d


def test_read_packet_format(dev):
    dev.read_reg(ka11.REG_SAMPLE_RATE, 4)
    assert dev.transport.sent[-1][:7] == [7, 0x12, 0xE4, 0xA2, 0x00, 0x20, 4]
    assert len(dev.transport.sent[-1]) == 16


def test_write_and_led_packets(dev):
    dev.write_reg(ka11.REG_UAC, 2)
    assert dev.transport.sent[-1][:8] == [7, 0x11, 0xA0, 0xA2, 0x00, 0x47, 1, 2]
    dev.set_led("off")
    assert dev.transport.sent[-1][:4] == [0x08, 0x51, 0xFD, 0x02]


def test_set_level_adds_windows_attenuation(dev):
    dev.set_level(20)
    writes = [p for p in dev.transport.sent if p[1] == 0x11]
    assert writes[0][4:8] == [0x00, 0x10, 1, 60]  # device attenuation, 0.5 dB steps
    assert writes[1][4:8] == [0x00, 0x11, 1, 60]
    dac = [p for p in writes if p[2:4] == [0x80, 0x60]]
    assert len(dac) == 4 and all(p[11] == 56 + 60 for p in dac)  # Windows + device


def test_mute_and_cap(dev, monkeypatch):
    dev.set_level(0)
    assert all(p[11] == 255 for p in dev.transport.sent if p[2:4] == [0x80, 0x60])
    monkeypatch.setattr(ka11, "windows_attenuation", lambda: 250)
    dev.transport.sent.clear()
    dev.set_level(10)
    assert all(p[11] == 254 for p in dev.transport.sent if p[2:4] == [0x80, 0x60])


def test_jump_guard(dev):
    with pytest.raises(ka11.VolumeJumpError) as e:
        dev.set_level(35)  # current level is 20
    assert e.value.increase_db == 15
    assert not any(p[1] == 0x11 for p in dev.transport.sent)  # nothing written
    dev.set_level(30)  # +10 is allowed
    dev.transport.registers[ka11.REG_VOLUME] = [40]
    dev.set_level(45, allow_jump=True)


def test_filter_encoding(dev):
    dev.set_filter(2)
    assert dev.transport.sent[-2][7] == 0x82
    dev.set_filter(4)
    assert dev.transport.sent[-2][7] == 0x22
    for raw, index in ((0x02, 0), (0x42, 1), (0x82, 2), (0xC2, 3), (0x22, 4)):
        dev.transport.registers[ka11.REG_FILTER] = [raw]
        assert dev.get_filter() == index


def test_windows_attenuation(monkeypatch):
    monkeypatch.setattr(ka11.winvolume, "ka11_volume", lambda: (-27.53, True, False))
    assert ka11.windows_attenuation() == 56  # whole dB, rounded towards quieter
    monkeypatch.setattr(ka11.winvolume, "ka11_volume", lambda: (-0.0, True, False))
    assert ka11.windows_attenuation() == 0
    monkeypatch.setattr(ka11.winvolume, "ka11_volume", lambda: (-40.0, False, False))
    assert ka11.windows_attenuation() == 0  # software volume: Windows scales the audio itself
    monkeypatch.setattr(ka11.winvolume, "ka11_volume", lambda: None)
    with pytest.raises(RuntimeError):
        ka11.windows_attenuation()


def test_struct_sizes_match_windows():
    import ctypes
    # Windows writes the full struct; a short declaration means it writes past the end of our buffer.
    assert ctypes.sizeof(ka11.HIDP_CAPS) == 66  # 5 + 17 reserved + 11 counts, all USHORT
    assert ctypes.sizeof(ka11.HIDD_ATTRIBUTES) == 12
    assert ctypes.sizeof(ka11.OVERLAPPED) == 32


def test_update_versions():
    assert updates.is_newer("v1.2.0", "1.1.0")
    assert not updates.is_newer("v1.1.0", "1.1.0")
    assert not updates.is_newer("v1.0.9", "1.1.0")
    assert updates.parse("garbage") == (0,)


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    data = settings.load()
    assert data["volume_limit"] == 50 and data["profiles"] == []
    data.update(volume_limit=99, profiles=[dict(name="IEMs", level=14, filter=0, led="off")])
    settings.save(data)
    loaded = settings.load()
    assert loaded["volume_limit"] == 50  # clamped
    assert loaded["profiles"][0]["name"] == "IEMs"


def test_easing_curves():
    app = runpy.run_path(os.path.join(os.path.dirname(__file__), "..", "ka11_control.pyw"), run_name="test")
    for curve in (app["DECELERATE"], app["ACCELERATE"], app["STANDARD"]):
        ease = app["EASINGS"][curve]
        assert ease(0) == pytest.approx(0, abs=1e-3) and ease(1) == pytest.approx(1, abs=1e-3)
        samples = [ease(i / 20) for i in range(21)]
        assert samples == sorted(samples)  # monotonic: no overshoot or wobble
