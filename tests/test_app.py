"""Drive the real UI against FakeKA11. Needs a desktop and Windows 11's Segoe fonts. Skipped on CI,
where Tk on the hosted build machines fails to start now and then; run it locally with:
python -m pytest tests"""
import os
import runpy
import time

import pytest

FONT = r"C:\Windows\Fonts\SegUIVar.ttf"
pytestmark = pytest.mark.skipif(not os.path.exists(FONT) or bool(os.environ.get("CI")),
                                reason="needs Windows 11 fonts and a desktop session")


@pytest.fixture
def app(tmp_path, monkeypatch):
    import tkinter as tk

    import ka11
    import winvolume
    from fake_ka11 import FakeKA11

    monkeypatch.setenv("APPDATA", str(tmp_path))
    FakeKA11.state = dict(level=20, led="off", filter=0, uac=2, sample_rate=48000)
    FakeKA11.calls = []
    monkeypatch.setattr(ka11, "KA11", FakeKA11)
    monkeypatch.setattr(winvolume, "shared_format", lambda: (48000, 32, 2))
    monkeypatch.setattr(winvolume, "is_default", lambda: True)
    mod = runpy.run_path(os.path.join(os.path.dirname(__file__), "..", "ka11_control.pyw"), run_name="test")

    class NoShell:  # no real tray icon or global hotkeys from tests
        def __init__(self, cb): pass
        def set_tray_icon(self, *a): pass
        def set_tip(self, *a): pass
        def set_hotkeys(self, enabled): return []
        def poll(self): return []
        def close(self): pass

    monkeypatch.setattr(mod["winshell"], "Shell", NoShell)
    monkeypatch.setattr(mod["settings"], "DEFAULTS", dict(mod["settings"].DEFAULTS, check_updates=False))
    root = tk.Tk()
    root.attributes("-alpha", 0.0)  # a real, un-hidden window, just invisible while tests click through it
    a = mod["App"](root)
    a.fake = FakeKA11
    pump(a)
    yield a
    root.destroy()


def pump(app, seconds=0.6):
    """Let Tk and the worker threads run."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.root.update()
        time.sleep(0.005)
    while app.busy.locked() or not app.results.empty() or app.polling:
        app.root.update()
        time.sleep(0.005)
    app.root.update()


def level_writes(app):
    return [c[1] for c in app.fake.calls if c[0] == "set_level"]


def test_reads_state(app):
    assert (app.level, app.led, app.filter, app.uac) == (20, "off", 0, 2)
    assert app.info["windows_format"] == (48000, 32, 2)


def test_volume_steps_respect_the_limit(app):
    app.settings["volume_limit"] = 25
    for _ in range(10):
        app.volume_step(1, osd=False)
    pump(app)
    assert app.level == 25 and level_writes(app) == [25]


def test_jump_guard_asks_and_cancel_restores(app):
    app.level = 40
    app.apply_level()
    pump(app)
    assert app.dialog["kind"] == "volume" and level_writes(app) == []
    app.cancel_dialog()
    assert app.level == 20


def test_jump_guard_confirm(app):
    app.level = 40
    app.apply_level()
    app.dialog_primary()
    pump(app)
    assert level_writes(app) == [40]


def test_mute_and_unmute(app):
    app.toggle_mute()
    pump(app)
    app.toggle_mute()
    pump(app)
    assert level_writes(app) == [0, 20]  # unmuting is never blocked by the jump guard


def test_profiles_save_apply_delete(app):
    app.save_profile("IEMs")
    app.fake.state.update(level=25, filter=3, led="on")
    app.refresh()
    pump(app)
    app.apply_profile(0)
    pump(app, 1.0)
    assert app.fake.state["filter"] == 0 and app.fake.state["led"] == "off" and app.fake.state["level"] == 20
    app.set_dialog(dict(kind="delete_profile", index=0, primary="Delete", title="", body=""))
    app.dialog_primary()
    assert app.settings["profiles"] == []


def test_profile_with_big_jump_asks(app):
    app.settings["profiles"] = [dict(name="Loud", level=45, filter=1, led="on")]
    app.apply_profile(0)
    pump(app, 1.0)
    assert app.fake.state["filter"] == 1 and app.dialog["kind"] == "volume" and level_writes(app) == []


def test_blind_test_restores_original_filter(app):
    app.click("bt_start")
    for side in "ABABA":
        app.click(f"bt_listen:{side}")
        pump(app, 0.2)
        app.click(f"bt_prefer:{side}")
        pump(app, 0.2)
    pump(app)
    assert app.blind["stage"] == "results" and len(app.blind["picks"]) == 5
    assert app.fake.state["filter"] == 0  # the listener's filter is back on


def test_close_hides_to_tray(app):
    app.on_close()
    app.root.update()
    assert app.root.state() == "withdrawn"


def test_unplug_shows_status(app, monkeypatch):
    import ka11
    monkeypatch.setattr(ka11, "find_path", lambda: None)
    app.on_device_change(False)
    pump(app)
    assert app.status == ("Unplugged", "error")


def test_every_page_renders(app):
    for key, _, _ in app.__class__.__init__.__globals__["PAGES"]:
        app.page = key
        app.render_base()


def test_unplug_burst_does_not_crash():
    """Regression: a USB unplug is broadcast to every window at once. Handling it by calling Tk from
    the shell window's procedure crashed the app ("Fatal Python error: PyEval_RestoreThread").
    Runs in a subprocess because the failure kills the whole process."""
    import subprocess
    import sys
    script = os.path.join(os.path.dirname(__file__), "device_burst.py")
    result = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "survived" in result.stdout
