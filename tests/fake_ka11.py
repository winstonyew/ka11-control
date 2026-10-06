"""An in-memory stand-in for ka11.KA11, for exercising the app without touching real hardware."""
import ka11


class FakeKA11:
    """Same interface as ka11.KA11. State is shared across instances like a real dongle's would be."""

    state = dict(level=20, led="off", filter=0, uac=2, sample_rate=48000)
    calls = []
    firmware = "0.08"
    last_response_ms = 2.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def close(self):
        pass

    def _record(self, *call):
        type(self).calls.append(call)

    def get_level(self):
        return self.state["level"]

    def set_level(self, level, allow_jump=False):
        increase = level - self.state["level"]
        if increase > ka11.JUMP_GUARD_DB and not allow_jump:
            raise ka11.VolumeJumpError(increase)
        self._record("set_level", level)
        self.state["level"] = level

    def get_led(self):
        return self.state["led"]

    def set_led(self, mode):
        self._record("set_led", mode)
        self.state["led"] = mode

    def get_filter(self):
        return self.state["filter"]

    def set_filter(self, index):
        self._record("set_filter", index)
        self.state["filter"] = index

    def get_uac(self):
        return self.state["uac"]

    def set_uac(self, version):
        self._record("set_uac", version)
        self.state["uac"] = version

    def get_sample_rate(self):
        return self.state["sample_rate"]

    def restore_defaults(self):
        self._record("restore_defaults")
        self.state.update(led="on", filter=0)
