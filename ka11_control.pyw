"""KA11 Control - an unofficial Windows 11 (Fluent) style control panel for the FiiO KA11.

A Settings-style navigation pane with pages for Sound (volume, digital filter), Profiles, Device
(indicator light, USB audio mode, restore defaults), a blind filter test, Connection (diagnostics)
and Settings. It also lives in the notification area with a quick volume flyout, global hotkeys,
and reconnects by itself when the dongle is plugged back in.

The UI is painted with Pillow (supersampled for smooth corners) using the system's light/dark
mode, accent colour, Segoe UI Variable and Segoe Fluent Icons. Rendering is layered so motion
stays smooth: the page is drawn once and cached, and the navigation pane and dialog are cached
layers composited on top, so sliding/fading frames only cost a paste or a blend.
"""
import ctypes
import os
import queue
import random
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
import winreg

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

import filter_shapes
import ka11
import settings
import updates
import winshell
import winvolume
from version import __version__

W, H = 380, 512  # logical (96-dpi) window size; every page fits
SS = 3  # supersampling factor
FONT_TEXT = r"C:\Windows\Fonts\SegUIVar.ttf"
FONT_ICONS = r"C:\Windows\Fonts\SegoeIcons.ttf"
ICON_MUTE, ICON_VOL1, ICON_VOL2, ICON_VOL3, ICON_REFRESH = "\ue74f", "\ue993", "\ue994", "\ue995", "\ue72c"
ICON_HEADPHONES, ICON_CHEVRON_DOWN, ICON_COPY, ICON_DELETE, ICON_PLAY = "\ue7f6", "\ue70d", "\ue8c8", "\ue74d", "\ue768"
ICON_NAV, ICON_SOUND, ICON_DEVICE, ICON_CONNECTION = "\ue700", "\ue995", "\ue88e", "\ue71b"
ICON_PROFILES, ICON_TEST, ICON_SETTINGS = "\ue728", "\ue9d9", "\ue713"
PAGES = (("sound", "Sound", ICON_SOUND), ("profiles", "Profiles", ICON_PROFILES), ("device", "Device", ICON_DEVICE),
         ("blindtest", "Blind test", ICON_TEST), ("connection", "Connection", ICON_CONNECTION),
         ("settings", "Settings", ICON_SETTINGS))
LED_OPTIONS = (("on", "On"), ("off-once", "Off for now"), ("off", "Always off"))
LED_NAMES = dict(LED_OPTIONS)
RESTORE_DIALOG = dict(kind="restore", title="Restore default settings?", primary="Restore",
                      body="The indicator light turns on and the filter goes back to Minimum phase fast "
                           "roll-off. Volume and USB audio mode stay as they are.")
TOP_BAR_H = 44
CONTENT_Y = 132  # first card on each page
SLIDER_X0, SLIDER_X1, SLIDER_Y = 70, 344, 190
FILTER_BOX = (32, 270, 348, 304)  # dropdown button on the Sound page
BT_BOXES = ((32, 262, 348, 296), (32, 322, 348, 356))  # blind test filter pickers
LIMIT_X0, LIMIT_X1, LIMIT_Y = 32, 348, 450  # volume limit slider on the Settings page
ROW_H = 26
PANE_W, NAV_Y, NAV_ITEM_H = 280, 52, 40
FLYOUT_ITEM_H, FLYOUT_PAD = 36, 4
SHADOW = 24  # room around overlay layers for their soft shadows
BLIND_ROUNDS = 5
TRAY_W = 320

# Fluent motion: durations (seconds) and easing curves (cubic-bezier control points).
FAST, NORMAL, SLOW = 0.083, 0.167, 0.25
FRAME_MS = 4  # frame budget while animating; ~240 Hz, matching high-refresh laptop panels
DECELERATE = (0.1, 0.9, 0.2, 1.0)  # things arriving
ACCELERATE = (0.7, 0.0, 1.0, 0.5)  # things leaving
STANDARD = (0.8, 0.0, 0.2, 1.0)  # things moving from one place to another


def read_reg(path, name, default):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def system_theme():
    dark = not read_reg(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme", 1)
    palette = read_reg(r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent", "AccentPalette", None)
    # AccentPalette holds 8 RGBA swatches from lightest to darkest; Windows uses Light2 for
    # controls in dark mode and Dark1 in light mode.
    if palette and len(palette) >= 32:
        i = 1 if dark else 4
        accent = tuple(palette[i * 4:i * 4 + 3])
    else:
        accent = (0x4C, 0xC2, 0xFF) if dark else (0x00, 0x67, 0xC0)
    if dark:
        return dict(dark=True, bg=(32, 32, 32), card=(43, 43, 43), card_stroke=(29, 29, 29),
                    text=(255, 255, 255), text2=(200, 200, 200), text3=(140, 140, 140),
                    control=(55, 55, 55), control_hover=(62, 62, 62), control_stroke=(70, 70, 70),
                    track=(140, 140, 140), thumb_ring=(69, 69, 69),
                    accent=accent, on_accent=(0, 0, 0), flyout=(44, 44, 44), flyout_stroke=(60, 60, 60),
                    dialog_footer=(32, 32, 32), ok=(108, 203, 95), error=(255, 153, 164), caution=(252, 225, 0))
    return dict(dark=False, bg=(243, 243, 243), card=(251, 251, 251), card_stroke=(229, 229, 229),
                text=(26, 26, 26), text2=(93, 93, 93), text3=(140, 140, 140),
                control=(253, 253, 253), control_hover=(246, 246, 246), control_stroke=(214, 214, 214),
                track=(135, 135, 135), thumb_ring=(255, 255, 255),
                accent=accent, on_accent=(255, 255, 255), flyout=(249, 249, 249), flyout_stroke=(220, 220, 220),
                dialog_footer=(243, 243, 243), ok=(15, 123, 15), error=(196, 43, 28), caution=(157, 93, 0))


def hexcolor(rgb):
    return "#%02x%02x%02x" % rgb


def mix(a, b, t):
    """Blend two colours; t=0 gives a, t=1 gives b."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def cubic_bezier(x1, y1, x2, y2):
    """CSS-style easing function through (0,0), (x1,y1), (x2,y2), (1,1)."""
    def bez(t, a, b):
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3

    def ease(x):
        lo, hi = 0.0, 1.0
        for _ in range(24):  # bisect for the t that gives this x
            mid = (lo + hi) / 2
            if bez(mid, x1, x2) < x:
                lo = mid
            else:
                hi = mid
        return bez((lo + hi) / 2, y1, y2)

    return ease


EASINGS = {curve: cubic_bezier(*curve) for curve in (DECELERATE, ACCELERATE, STANDARD)}


class Tween:
    def __init__(self, start, end, duration, curve):
        self.start, self.end, self.duration = start, end, duration
        self.ease = EASINGS[curve]
        self.t0 = None  # starts on first use, so the frame that kicks it off doesn't count

    def value(self):
        if self.t0 is None:
            self.t0 = time.perf_counter()
        p = (time.perf_counter() - self.t0) / self.duration if self.duration else 1
        return self.end if p >= 1 else self.start + (self.end - self.start) * self.ease(p)

    @property
    def done(self):
        return self.t0 is not None and time.perf_counter() - self.t0 >= self.duration


class Painter:
    """Draws in logical (96-dpi) window units onto a supersampled image covering the given box."""

    def __init__(self, scale, bg, box=(0, 0, W, H), mode="RGB"):
        self.k = scale * SS
        self.ox, self.oy = box[0], box[1]
        width, height = box[2] - box[0], box[3] - box[1]
        self.size = (round(width * scale), round(height * scale))
        self.img = Image.new(mode, (self.size[0] * SS, self.size[1] * SS), bg)
        self.d = ImageDraw.Draw(self.img)
        self.fonts = {}

    def xy(self, x, y):
        return (x - self.ox) * self.k, (y - self.oy) * self.k

    def font(self, size, weight=400, icons=False):
        key = (size, weight, icons)
        if key not in self.fonts:
            f = ImageFont.truetype(FONT_ICONS if icons else FONT_TEXT, round(size * self.k))
            if not icons:
                f.set_variation_by_axes([weight, min(36, max(5, size * 0.75))])
            self.fonts[key] = f
        return self.fonts[key]

    def rect(self, x0, y0, x1, y1, r, fill, outline=None, width=1):
        self.d.rounded_rectangle((*self.xy(x0, y0), *self.xy(x1, y1)), r * self.k, fill=fill,
                                 outline=outline, width=round(width * self.k) if outline else 0)

    def circle(self, cx, cy, r, fill, outline=None, width=1):
        self.d.ellipse((*self.xy(cx - r, cy - r), *self.xy(cx + r, cy + r)), fill=fill, outline=outline,
                       width=round(width * self.k) if outline else 0)

    def line(self, points, fill, width):
        self.d.line([self.xy(x, y) for x, y in points], fill=fill, width=round(width * self.k), joint="curve")

    def text(self, x, y, s, size, fill, weight=400, anchor="ls", icons=False):
        self.d.text(self.xy(x, y), s, font=self.font(size, weight, icons), fill=fill, anchor=anchor)

    def text_width(self, s, size, weight=400):
        return self.font(size, weight).getlength(s) / self.k

    def fit(self, s, size, max_width, weight=400):
        """Shorten with an ellipsis so the text fits max_width."""
        if self.text_width(s, size, weight) <= max_width:
            return s
        while s and self.text_width(s + "…", size, weight) > max_width:
            s = s[:-1]
        return s.rstrip() + "…"

    def wrap(self, s, size, max_width, weight=400):
        lines, line = [], ""
        for word in s.split():
            candidate = f"{line} {word}".strip()
            if self.text_width(candidate, size, weight) <= max_width:
                line = candidate
            else:
                lines.append(line)
                line = word
        return lines + [line]

    def result(self):
        return self.img.reduce(SS)  # box filter: exactly what supersampling needs, and fast


def with_shadow(layer, scale, box, radius, offset_y, opacity):
    """Put a soft drop shadow under an RGBA layer. box is the casting shape within the layer (logical)."""
    shadow = Image.new("L", layer.size, 0)
    x0, y0, x1, y1 = (round(v * scale) for v in box)
    ImageDraw.Draw(shadow).rounded_rectangle((x0, y0 + round(offset_y * scale), x1, y1 + round(offset_y * scale)),
                                             round(8 * scale), fill=opacity)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius * scale))
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.putalpha(shadow)
    return Image.alpha_composite(out, layer)


def headphones_mask(size):
    """The Fluent headphones glyph as a filled silhouette (the font only has an outline version):
    flood the outside and treat everything the flood didn't reach as solid."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).text((size / 2, size / 2), ICON_HEADPHONES, font=ImageFont.truetype(FONT_ICONS, round(size * 0.92)),
                              fill=255, anchor="mm", stroke_width=max(1, size // 64), stroke_fill=255)
    ImageDraw.floodfill(mask, (0, 0), 128)
    return mask.point(lambda v: 0 if v == 128 else 255)


def headphones_image(size, color):
    img = Image.new("RGBA", (size, size), color + (0,))
    img.putalpha(headphones_mask(size))
    return img


def round_corners(window):
    """Ask DWM for Windows 11 rounded corners (no-op on Windows 10)."""
    window.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)


def work_area():
    """The desktop area not covered by the taskbar, in physical pixels."""
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom


class FilterFlyout:
    """Fluent-style dropdown list of the digital filters: a borderless popup over the box it belongs
    to, with the current choice lined up on the box (like a WinUI ComboBox). It slides down and
    fades in, and its hover highlights fade like the main window's."""

    def __init__(self, app, box, selected, on_pick, exclude=None):
        self.app, self.hover, self.box = app, None, box
        self.selected, self.on_pick, self.exclude = selected, on_pick, exclude
        self.tweens = {}
        self.job = None
        t, s = app.t, app.scale
        x0, y0, x1, y1 = box
        self.width = x1 - x0
        self.height = FLYOUT_PAD * 2 + FLYOUT_ITEM_H * len(ka11.FILTERS)
        top = (y0 + y1) / 2 - (FLYOUT_PAD + (selected or 0) * FLYOUT_ITEM_H + FLYOUT_ITEM_H / 2)
        top = max(8, min(top, H - 8 - self.height))  # stay inside the window
        self.x = app.root.winfo_rootx() + round(x0 * s)
        self.y = app.root.winfo_rooty() + round(top * s)
        self.win = tk.Toplevel(app.root, bg=hexcolor(t["flyout"]))
        self.win.overrideredirect(True)
        self.win.attributes("-alpha", 0.0)
        self.view = tk.Label(self.win, bd=0, highlightthickness=0)
        self.view.pack()
        self.view.bind("<Motion>", self.on_motion)
        self.view.bind("<Leave>", lambda _: self.set_hover(None))
        self.view.bind("<ButtonRelease-1>", self.on_click)
        self.win.bind("<Escape>", lambda _: app.close_flyout())
        self.win.bind("<FocusOut>", lambda _: app.root.after(50, app.close_flyout))
        self.open = Tween(0, 1, SLOW, DECELERATE)
        self.place(0)
        round_corners(self.win)
        self.draw()
        self.win.focus_force()
        self.animate()

    def place(self, p):
        s = self.app.scale
        self.win.geometry(f"{round(self.width * s)}x{round(self.height * s)}+{self.x}+{self.y - round(12 * s * (1 - p))}")
        self.win.attributes("-alpha", p)

    def animate(self):
        self.job = None
        now = time.perf_counter()
        if now - getattr(self, "last_frame", 0) < FRAME_MS / 1000:
            self.job = self.win.after(1, self.animate)
            return
        self.last_frame = now
        p = self.open.value()
        self.place(p)
        active = not self.open.done
        if any(not tw.done for tw in self.tweens.values()):
            self.draw()
            active = True
        if active:
            self.job = self.win.after(1, self.animate)  # see App.schedule_frame for why after(1)

    def amount(self, key, target, duration=FAST):
        tw = self.tweens.get(key)
        if tw is None:
            self.tweens[key] = tw = Tween(target, target, 0, DECELERATE)
        elif tw.end != target:
            self.tweens[key] = tw = Tween(tw.value(), target, duration, DECELERATE)
        return tw.value()

    def index_at(self, event):
        y = event.y / self.app.scale - FLYOUT_PAD
        i = int(y // FLYOUT_ITEM_H)
        return i if 0 <= y and i < len(ka11.FILTERS) and i != self.exclude else None

    def set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.draw()
            if not self.job:
                self.animate()

    def on_motion(self, event):
        self.set_hover(self.index_at(event))

    def on_click(self, event):
        i = self.index_at(event)
        self.app.close_flyout()
        if i is not None:
            self.on_pick(i)

    def draw(self):
        t = self.app.t
        p = Painter(self.app.scale, t["flyout"], (0, 0, self.width, self.height))
        p.rect(0, 0, self.width - 0.5, self.height - 0.5, 8, t["flyout"], t["flyout_stroke"])
        for i, name in enumerate(ka11.FILTERS):
            y0 = FLYOUT_PAD + i * FLYOUT_ITEM_H
            disabled = i == self.exclude
            h = self.amount(i, 1.0 if i == self.selected or i == self.hover else 0.0)
            if h:
                p.rect(4, y0 + 2, self.width - 4, y0 + FLYOUT_ITEM_H - 2, 4, mix(t["flyout"], t["control_hover"], h))
            if i == self.selected:
                p.rect(4, y0 + 10, 7, y0 + FLYOUT_ITEM_H - 10, 1.5, t["accent"])
            p.text(16, y0 + FLYOUT_ITEM_H / 2, name, 13, t["text3"] if disabled else t["text"], anchor="lm")
            self.app.draw_curve(p, self.width - 76, y0 + 8, self.width - 14, y0 + FLYOUT_ITEM_H - 6,
                                self.app.curves[i], 1.25)
        self.photo = ImageTk.PhotoImage(p.result())
        self.view.config(image=self.photo)

    def destroy(self):
        if self.job:
            self.win.after_cancel(self.job)
        self.win.destroy()


class TrayFlyout:
    """Quick controls that pop up above the notification area, like Windows' own volume flyout:
    device volume, mute, profiles, and a way into the full app. Also used as the on-screen display
    for the volume hotkeys."""

    def __init__(self, app):
        self.app = app
        self.win = None
        self.hover = self.pressed = None
        self.dragging = False
        self.hide_job = None
        self.hits = []

    @property
    def visible(self):
        return self.win is not None

    def height(self):
        return 180 + (48 if self.app.settings["profiles"] else 0)

    def show(self, focus=True, auto_hide=None):
        app, s = self.app, self.app.scale
        if self.win is None:
            self.win = tk.Toplevel(app.root, bg=hexcolor(app.t["flyout"]))
            self.win.overrideredirect(True)
            self.win.attributes("-topmost", True)
            self.view = tk.Label(self.win, bd=0, highlightthickness=0)
            self.view.pack()
            self.view.bind("<Motion>", self.on_motion)
            self.view.bind("<Leave>", lambda _: self.set_hover(None))
            self.view.bind("<ButtonPress-1>", self.on_press)
            self.view.bind("<B1-Motion>", self.on_drag)
            self.view.bind("<ButtonRelease-1>", self.on_release)
            self.view.bind("<MouseWheel>", lambda e: app.volume_step(1 if e.delta > 0 else -1, osd=False))
            self.win.bind("<Escape>", lambda _: self.hide())
            self.win.bind("<FocusOut>", lambda _: app.root.after(150, self._hide_if_unfocused))
            round_corners(self.win)
        left, top, right, bottom = work_area()
        w, h = round(TRAY_W * s), round(self.height() * s)
        margin = round(12 * s)
        self.win.geometry(f"{w}x{h}+{right - w - margin}+{bottom - h - margin}")
        self.draw()
        self.win.deiconify()
        self.win.lift()
        if focus:
            self.win.focus_force()
        if self.hide_job:
            self.win.after_cancel(self.hide_job)
            self.hide_job = None
        if auto_hide:
            self.hide_job = self.win.after(auto_hide, self._auto_hide)

    def _auto_hide(self):
        self.hide_job = None
        if self.hover is None and not self.dragging:
            self.hide()
        elif self.win:
            self.hide_job = self.win.after(800, self._auto_hide)

    def _hide_if_unfocused(self):
        if self.win and self.win.focus_get() is None:
            self.hide()

    def hide(self):
        if self.win:
            self.win.destroy()
            self.win = None
            self.hover = None
            self.hidden_at = time.perf_counter()

    def toggle(self):
        if self.win:
            self.hide()
        elif time.perf_counter() - getattr(self, "hidden_at", 0) > 0.4:
            # Clicking the tray icon while the flyout is open first closes it through focus-out;
            # that same click shouldn't open it again.
            self.show()

    def refresh(self):
        if self.win:
            self.draw()

    def slider_x(self, level):
        return 52 + (TRAY_W - 72 - 52) * level / 50

    def level_from_x(self, event):
        x = event.x / self.app.scale
        level = round((x - 52) / (TRAY_W - 72 - 52) * 50)
        return max(0, min(self.app.settings["volume_limit"], level))

    def hit(self, event):
        x, y = event.x / self.app.scale, event.y / self.app.scale
        return next((n for n, x0, y0, x1, y1 in self.hits if x0 <= x <= x1 and y0 <= y <= y1), None)

    def set_hover(self, name):
        if name != self.hover:
            self.hover = name
            self.draw()

    def on_motion(self, event):
        self.set_hover(self.hit(event))

    def on_press(self, event):
        name = self.hit(event)
        if name == "slider" and self.app.level is not None:
            self.dragging = True
            self.app.level = self.level_from_x(event)
            self.app.invalidate()
        self.pressed = name
        self.draw()

    def on_drag(self, event):
        if self.dragging:
            self.app.level = self.level_from_x(event)
            self.app.invalidate()
            self.draw()

    def on_release(self, event):
        app = self.app
        if self.dragging:
            self.dragging = False
            self.pressed = None
            app.apply_level()
            self.draw()
            return
        name, self.pressed = self.pressed, None
        self.draw()
        if not name or name != self.hit(event):
            return
        if name == "mute":
            app.toggle_mute()
        elif name == "open":
            self.hide()
            app.show_window()
        elif name.startswith("profile:"):
            app.apply_profile(int(name[8:]))

    def draw(self):
        if not self.win:
            return
        app, t = self.app, self.app.t
        h = self.height()
        p = Painter(app.scale, t["flyout"], (0, 0, TRAY_W, h))
        p.rect(0, 0, TRAY_W - 0.5, h - 0.5, 8, t["flyout"], t["flyout_stroke"])
        self.hits = []

        def button(name, x0, y0, x1, y1, label, accent=False, icon=None):
            hov = self.hover == name
            fill = t["accent"] if accent else (t["control_hover"] if hov else t["control"])
            if self.pressed == name and hov and not accent:
                fill = mix(fill, t["bg"], 0.5)
            p.rect(x0, y0, x1, y1, 4, fill, None if accent else t["control_stroke"])
            color = t["on_accent"] if accent else t["text"]
            if icon:
                p.text(x0 + 18, (y0 + y1) / 2, icon, 12, color, anchor="mm", icons=True)
                p.text(x0 + 32, (y0 + y1) / 2, label, 13, color, anchor="lm")
            else:
                p.text((x0 + x1) / 2, (y0 + y1) / 2, p.fit(label, 13, x1 - x0 - 12), 13, color, anchor="mm")
            self.hits.append((name, x0, y0, x1, y1))

        # Header: name and connection status
        p.text(16, 28, "KA11 Control", 14, t["text"], weight=600, anchor="lm")
        text, kind = app.status
        x = TRAY_W - 16 - p.text_width(p.fit(text, 12, 150), 12)
        p.circle(x - 10, 28, 3.5, {"ok": t["ok"], "busy": t["text3"], "error": t["error"]}[kind])
        p.text(TRAY_W - 16, 28, p.fit(text, 12, 150), 12, t["text2"], anchor="rm")

        # Volume row
        level, limit = app.level, app.settings["volume_limit"]
        y = 76
        icon = ICON_MUTE if not level else ICON_VOL1 if level < 17 else ICON_VOL2 if level < 34 else ICON_VOL3
        if self.hover == "mute":
            p.rect(16, y - 14, 44, y + 14, 4, t["control_hover"])
        p.text(30, y, icon, 16, t["text"], anchor="mm", icons=True)
        self.hits.append(("mute", 16, y - 14, 44, y + 14))
        x0, x1 = self.slider_x(0), self.slider_x(50)
        p.rect(x0, y - 2, x1, y + 2, 2, t["track"])
        if level:
            p.rect(x0, y - 2, self.slider_x(level), y + 2, 2, t["accent"])
        if limit < 50:
            lx = self.slider_x(limit)
            p.rect(lx - 0.75, y - 7, lx + 0.75, y + 7, 0.5, t["text3"])
        tx = self.slider_x(level or 0)
        p.circle(tx, y, 10, t["thumb_ring"])
        p.circle(tx, y, 5 if self.dragging else 7 if self.hover == "slider" else 6, t["accent"])
        self.hits.append(("slider", x0 - 10, y - 14, x1 + 10, y + 14))
        p.text(TRAY_W - 16, y, "—" if level is None else ("Muted" if level == 0 else str(level)), 13,
               t["text2"], anchor="rm")
        filter_name = "—" if app.filter is None else ka11.FILTERS[app.filter]
        p.text(16, 108, p.fit(filter_name, 12, TRAY_W - 32), 12, t["text3"], anchor="lm")

        # Profiles
        y = 128
        profiles = app.settings["profiles"][:3]
        if profiles:
            gap = 8
            bw = (TRAY_W - 32 - gap * (len(profiles) - 1)) / len(profiles)
            for i, prof in enumerate(profiles):
                bx = 16 + i * (bw + gap)
                button(f"profile:{i}", bx, y, bx + bw, y + 32, prof["name"])
            y += 48

        # Footer
        p.rect(0, y + 4, TRAY_W, y + 5, 0, t["flyout_stroke"])
        button("open", TRAY_W - 132, y + 14, TRAY_W - 16, y + 44, "Open app", icon=ICON_HEADPHONES)
        self.photo = ImageTk.PhotoImage(p.result())
        self.view.config(image=self.photo)


class App:
    def __init__(self, root, start_hidden=False):
        self.root = root
        self.t = system_theme()
        self.settings = settings.load()
        root.title("KA11 Control")
        root.resizable(False, False)
        root.configure(bg=hexcolor(self.t["bg"]))
        self.scale = root.winfo_fpixels("1i") / 96
        # A canvas with one persistent image per layer: sliding the pane only moves an item,
        # and redrawn layers are pasted into their existing images instead of replacing them.
        self.view = tk.Canvas(root, width=round(W * self.scale), height=round(H * self.scale),
                              bd=0, highlightthickness=0, bg=root["bg"])
        self.view.pack()
        self.photos, self.items = {}, {}
        self.base_shown = None  # which image the base item currently shows
        self.style_title_bar()
        self.set_icon()

        self.curves = filter_shapes.load_curves()
        self.page = "sound"
        self.nav_open = False
        self.dialog = None
        self.entry = None  # text box shown inside the "save profile" dialog
        self.flyout = None
        self.level = None
        self.unmuted_level = 25
        self.applied_level = None  # last level confirmed on the dongle; the jump guard compares to it
        self.led = None
        self.filter = None
        self.uac = None
        self.uac_active = None  # mode the dongle was plugged in with; a change applies on replug
        self.status = ("Connecting…", "busy")
        self.info = dict(sample_rate=None, firmware=None, response_ms=None, checked=None, error=None,
                         windows_format=None, default=None)
        self.update = None  # (tag, url) when a newer release exists
        self.hotkey_conflicts = []
        self.blind = None  # blind test state
        self.hover = None
        self.pressed = None
        self.dragging = None  # "volume" or "limit" while dragging a slider
        self.busy = threading.Lock()
        self.results = queue.Queue()  # worker threads hand results to the Tk thread through this
        self.polling = False
        self.apply_job = None
        self.reconnect_job = None

        # Rendering state: cached layers, which ones need redrawing, and running animations.
        self.tweens = {}
        self.active = False
        self.dirty = {"base", "pane", "dialog"}
        self.layers = {}
        self.base_hits, self.pane_hits, self.dialog_hits, self.hits = [], [], [], []
        self.frame_job = None

        self.view.bind("<ButtonPress-1>", self.on_press)
        self.view.bind("<B1-Motion>", self.on_drag)
        self.view.bind("<ButtonRelease-1>", self.on_release)
        self.view.bind("<Motion>", self.on_hover)
        self.view.bind("<Leave>", lambda _: self.set_hover(None))
        root.bind("<MouseWheel>", lambda e: self.nudge(1 if e.delta > 0 else -1))
        for key, step in (("<Left>", -1), ("<Down>", -1), ("<Right>", 1), ("<Up>", 1)):
            root.bind(key, lambda _, s=step: self.nudge(s))
        root.bind("<Escape>", lambda _: self.dismiss())
        # Close the filter list if the window moves (events from child widgets don't count).
        root.bind("<Configure>", lambda e: self.close_flyout() if e.widget is root else None)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Notification area, hotkeys and plug/unplug notifications
        self.tray = TrayFlyout(self)
        self.shell = winshell.Shell(self.tray_menu)
        winshell.allow_dark_menus(self.t["dark"])
        self.shell.set_tray_icon(self.tray_icon_file(), "KA11 Control")
        self.apply_hotkeys()
        self.poll_shell()

        if start_hidden:
            root.withdraw()
        self.frame()
        self.refresh()
        if self.settings["check_updates"]:
            self.check_updates()

    def style_title_bar(self):
        """Dark title bar in dark mode, and a caption colour that blends into the window."""
        self.root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1 if self.t["dark"] else 0)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        r, g, b = self.t["bg"]
        caption = ctypes.c_int(r | g << 8 | b << 16)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4)  # DWMWA_CAPTION_COLOR

    def set_icon(self):
        """Window/taskbar icon: a white headphones silhouette on a transparent background."""
        img = headphones_image(256, (255, 255, 255))
        self.icons = [ImageTk.PhotoImage(img.resize((s, s), Image.LANCZOS)) for s in (64, 32, 16)]
        self.root.iconphoto(True, *self.icons)

    def tray_icon_file(self):
        """Tray icon in the taskbar's colour scheme: white on a dark taskbar, black on a light one."""
        light = read_reg(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "SystemUsesLightTheme", 0)
        color = (0, 0, 0) if light else (255, 255, 255)
        path = os.path.join(tempfile.gettempdir(), f"ka11control-tray-{'light' if light else 'dark'}.ico")
        if not os.path.exists(path):
            headphones_image(256, color).save(path, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48)])
        return path

    # ---------- window and tray ----------

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.invalidate("base", "pane")

    def on_close(self):
        if self.settings["keep_in_tray"]:
            self.close_flyout()
            self.set_nav(False)
            self.root.withdraw()
        else:
            self.quit()

    def poll_shell(self):
        """Handle tray, hotkey and plug/unplug events. The shell window only records them, because
        calling Tk from inside its window procedure can crash Tkinter (see winshell.Shell)."""
        for event in self.shell.poll():
            kind = event[0]
            if kind == "tray_click":
                self.tray.toggle()
            elif kind == "hotkey":
                self.on_hotkey(event[1])
            elif kind == "command":
                self.tray_command(event[1])
            elif kind == "device":
                self.on_device_change(event[1])
            elif kind == "show":
                self.show_window()
        self.root.after(50, self.poll_shell)

    def quit(self):
        self.tray.hide()
        self.shell.close()
        self.root.destroy()

    def tray_menu(self):
        items = [(1, "Open KA11 Control", 0), (0, None, 0)]
        for i, prof in enumerate(self.settings["profiles"]):
            items.append((100 + i, f"Profile: {prof['name']}", 0))
        if self.settings["profiles"]:
            items.append((0, None, 0))
        items += [(2, "Start with Windows", winshell.MF_CHECKED if winshell.autostart_enabled() else 0),
                  (3, "Exit", 0)]
        return items

    def tray_command(self, cmd):
        if cmd == 1:
            self.show_window()
        elif cmd == 2:
            self.set_autostart(not winshell.autostart_enabled())
        elif cmd == 3:
            self.quit()
        elif cmd >= 100:
            self.apply_profile(cmd - 100)

    def update_tray(self):
        level = self.level
        vol = "—" if level is None else ("Muted" if level == 0 else f"Volume {level}")
        self.shell.set_tip(f"KA11 Control · {vol} · {self.status[0]}")
        self.tray.refresh()

    def set_autostart(self, enabled):
        try:
            winshell.set_autostart(enabled)
        except OSError as e:
            self.set_status(f"Couldn't change Start with Windows ({e.strerror})", "error")
        self.invalidate()

    # ---------- hotkeys and hot-plug ----------

    def apply_hotkeys(self):
        self.hotkey_conflicts = self.shell.set_hotkeys(self.settings["hotkeys"])
        self.invalidate()

    def on_hotkey(self, name):
        if name == "volume_up":
            self.volume_step(1)
        elif name == "volume_down":
            self.volume_step(-1)
        elif name == "mute":
            self.toggle_mute()
            self.show_osd()

    def show_osd(self):
        """Hotkeys show the tray flyout briefly, like Windows' volume OSD - unless the app is in front."""
        if self.root.state() != "normal" or self.root.focus_get() is None:
            self.tray.show(focus=False, auto_hide=1800)

    def on_device_change(self, arrived):
        if self.reconnect_job:
            self.root.after_cancel(self.reconnect_job)
        if arrived:
            # Give Windows a moment to finish setting up the audio and HID interfaces.
            self.uac_active = None  # a fresh plug-in applies whatever USB audio mode is stored
            self.reconnect_job = self.root.after(1200, self.refresh)
        else:
            self.reconnect_job = self.root.after(300, self.check_unplugged)

    def check_unplugged(self):
        self.reconnect_job = None
        if ka11.find_path() is None:
            self.set_status("Unplugged", "error")
            self.info["sample_rate"] = None

    # ---------- animation ----------

    def amount(self, key, target, duration=FAST, curve=DECELERATE):
        """Declarative animation: returns the current value of `key`, easing towards `target`
        whenever the target changes. Marks the frame as animating while it moves."""
        tw = self.tweens.get(key)
        if tw is None:
            self.tweens[key] = tw = Tween(target, target, 0, curve)
        elif tw.end != target:
            self.tweens[key] = tw = Tween(tw.value(), target, duration, curve)
        if not tw.done:
            self.active = True
        return tw.value()

    def start(self, key, end, duration, curve, begin=None, default=0.0):
        """Imperative animation to `end` from `begin`, else from wherever it currently is
        (`default` if it has never animated)."""
        tw = self.tweens.get(key)
        current = begin if begin is not None else (tw.value() if tw else default)
        self.tweens[key] = Tween(current, end, duration, curve)
        self.schedule_frame()

    def value(self, key, default):
        tw = self.tweens.get(key)
        if tw is None:
            return default
        if not tw.done:
            self.active = True
        return tw.value()

    def hover_amount(self, name):
        return self.amount(("hover", name), 1.0 if self.hover == name else 0.0)

    def invalidate(self, *layers):
        self.dirty.update(layers or ("base",))
        self.schedule_frame()

    def schedule_frame(self, delay=0):
        if self.frame_job:
            return
        if delay:
            # Tk rounds any after() delay of 2 ms or more up to Windows' 15.6 ms timer tick, which caps
            # animation near 60 fps with uneven gaps. after(1) is precise, so poll with it instead.
            self.next_frame_at = time.perf_counter() + delay / 1000
            self.frame_job = self.root.after(1, self.wait_for_frame)
        else:
            self.frame_job = self.root.after_idle(self.frame)

    def wait_for_frame(self):
        if time.perf_counter() >= self.next_frame_at:
            self.frame_job = None
            self.frame()
        else:
            self.frame_job = self.root.after(1, self.wait_for_frame)

    def frame(self):
        """Redraw whichever layers changed or are animating, update the canvas, and keep going while
        anything moves. Frames are paced by how long this one took, not a fixed delay."""
        self.frame_job = None
        began = time.perf_counter()
        animating = False
        for name, render in (("base", self.render_base), ("pane", self.render_pane), ("dialog", self.render_dialog)):
            if name in self.dirty:
                self.dirty.discard(name)
                self.active = False
                render()
                if name == "base":
                    self.base_shown = None
                elif name == "pane":
                    self.show("pane", self.layers["pane"])
                if self.active:  # a hover/selection fade inside this layer is still running
                    self.dirty.add(name)
                    animating = True
        self.active = False
        self.compose()
        animating |= self.active
        if animating:
            spent = (time.perf_counter() - began) * 1000
            self.schedule_frame(max(1, round(FRAME_MS - spent)))

    def show(self, name, img):
        """Put an image on the canvas item `name`, reusing its Tk image when the size matches."""
        photo = self.photos.get(name)
        if photo is not None and (photo.width(), photo.height()) == img.size:
            photo.paste(img)
        else:
            self.photos[name] = photo = ImageTk.PhotoImage(img)
            if name in self.items:
                self.view.itemconfigure(self.items[name], image=photo)
            else:
                self.items[name] = self.view.create_image(0, 0, image=photo, anchor="nw")
                for top in ("pane", "dialog"):  # keep the stacking order base < pane < dialog
                    if top in self.items:
                        self.view.tag_raise(self.items[top])

    def place(self, name, x, y, visible):
        item = self.items[name]
        self.view.coords(item, x, y)
        self.view.itemconfigure(item, state="normal" if visible else "hidden")

    # ---------- drawing helpers ----------

    def slider_x(self, level, x0=SLIDER_X0, x1=SLIDER_X1):
        return x0 + (x1 - x0) * level / 50

    def button(self, p, name, x0, y0, x1, y1, label, selected=False, icon=None, accent=False):
        t = self.t
        sel = 1.0 if accent else self.amount(("selected", name), 1.0 if selected else 0.0, NORMAL)
        hov = self.hover_amount(name)
        pressed = self.pressed == name and self.hover == name
        rest = mix(t["control"], t["control_hover"], hov)
        if pressed:
            rest = mix(rest, t["bg"], 0.5)
        fill = mix(rest, mix(t["accent"], t["text"], 0.1 * hov), sel)
        stroke = mix(t["control_stroke"], fill, sel)
        color = mix(t["text2"] if pressed else t["text"], t["on_accent"], sel)
        p.rect(x0, y0, x1, y1, 4, fill, stroke)
        cy = (y0 + y1) / 2
        if icon:
            p.text(x0 + 20, cy, icon, 12, color, anchor="mm", icons=True)
            p.text(x0 + 34, cy, label, 13, color, anchor="lm")
        else:
            p.text((x0 + x1) / 2, cy, p.fit(label, 13, x1 - x0 - 12), 13, color,
                   weight=600 if sel > 0.5 else 400, anchor="mm")
        return name, x0, y0, x1, y1

    def icon_button(self, p, name, x0, y0, x1, y1, icon, size=16, active=False, bg=None):
        """Borderless button (hamburger, refresh, delete) that fades in a subtle fill on hover."""
        t = self.t
        h = max(self.hover_amount(name), 1.0 if active else 0.0)
        if h:
            p.rect(x0, y0, x1, y1, 4, mix(bg or t["bg"], t["control_hover"], h))
        color = t["text2"] if self.pressed == name and self.hover == name else t["text"]
        p.text((x0 + x1) / 2, (y0 + y1) / 2, icon, size, color, anchor="mm", icons=True)
        return name, x0, y0, x1, y1

    def toggle(self, p, name, x1, cy, on):
        """Fluent ToggleSwitch: the knob slides and the track fills with the accent colour."""
        t = self.t
        k = self.amount(("toggle", name), 1.0 if on else 0.0, NORMAL, STANDARD)
        hov = self.hover == name
        x0 = x1 - 40
        track = mix(t["card"], t["accent"], k)
        p.rect(x0, cy - 10, x1, cy + 10, 10, track, mix(t["text2"], t["accent"], k))
        r = 7 if hov else 6
        p.circle(x0 + 10 + 20 * k, cy, r, mix(t["text2"], t["on_accent"], k))

    def dropdown(self, p, name, box, label, open_=False):
        t = self.t
        x0, y0, x1, y1 = box
        h = max(self.hover_amount(name), 1.0 if open_ else 0.0)
        p.rect(x0, y0, x1, y1, 4, mix(t["control"], t["control_hover"], h), t["control_stroke"])
        p.text(x0 + 12, (y0 + y1) / 2, p.fit(label, 13, x1 - x0 - 44), 13, t["text"], anchor="lm")
        p.text(x1 - 16, (y0 + y1) / 2, ICON_CHEVRON_DOWN, 10, t["text2"], anchor="mm", icons=True)
        return name, x0, y0, x1, y1

    def draw_curve(self, p, x0, y0, x1, y1, curve, width):
        """Illustrative impulse response: faint zero line, accent trace, peaks pointing up."""
        base = y0 + (y1 - y0) * 0.72
        amp = (y1 - y0) * 0.68
        p.rect(x0, base, x1, base + 0.5, 0, self.t["control_stroke"])
        step = max(1, len(curve) // 160)
        pts = [(x0 + (x1 - x0) * i / (len(curve) - 1), base - amp * v) for i, v in enumerate(curve)][::step]
        p.line(pts, self.t["accent"], width)

    def card(self, p, y0, y1):
        p.rect(16, y0, 364, y1, 8, self.t["card"], self.t["card_stroke"])

    def divider(self, p, y):
        p.rect(17, y, 363, y + 1, 0, self.t["card_stroke"])

    def card_title(self, p, y, title, caption, caption_color=None):
        p.text(32, y + 26, title, 14, self.t["text"])
        p.text(32, y + 46, caption, 12, caption_color or self.t["text2"])

    def windows_format_text(self):
        fmt = self.info["windows_format"]
        return "—" if not fmt else f"{fmt[0] / 1000:g} kHz · {fmt[1]}-bit"

    def detail_rows(self):
        info, (status, kind) = self.info, self.status
        rate = info["sample_rate"]
        default = {True: "Yes", False: "No", None: "—"}[info["default"]]
        return [
            ("Status", "Working…" if kind == "busy" else status),
            ("Sample rate", "—" if rate is None else f"{rate / 1000:g} kHz"),
            ("Windows mix format", self.windows_format_text()),
            ("Default output", default),
            ("Firmware", info["firmware"] or "—"),
            ("USB device", f"VID {ka11.VID:04X} · PID {ka11.PID:04X}"),
            ("USB audio mode", "—" if self.uac_active is None else f"UAC {self.uac_active}.0"),
            ("Response time", "—" if info["response_ms"] is None else f"{info['response_ms']:.0f} ms"),
            ("Last checked", info["checked"] or "—"),
            ("Last error", info["error"] or "None"),
        ]

    def bit_perfect_note(self):
        """What the sample rates say about resampling. Shared mode always runs at Windows' rate, so a
        different rate at the dongle means a player has it in exclusive mode."""
        rate, fmt = self.info["sample_rate"], self.info["windows_format"]
        if not rate or not fmt:
            return None, None
        if rate != fmt[0]:
            return (f"A player is using exclusive mode at {rate / 1000:g} kHz, so audio reaches the KA11 "
                    "untouched."), self.t["ok"]
        return (f"Windows mixes everything at {fmt[0] / 1000:g} kHz. Music at other sample rates is resampled "
                "unless your player uses exclusive mode."), self.t["text2"]

    # ---------- layers ----------

    def render_base(self):
        """Top bar and the current page."""
        t = self.t
        p = Painter(self.scale, t["bg"])
        hits = [self.icon_button(p, "nav", 4, 4, 44, 40, ICON_NAV, active=self.nav_open)]
        x = 56
        if self.info["firmware"]:
            label = f"Firmware {self.info['firmware']}"
            p.text(x, 22, label, 12, t["text2"], anchor="lm")
            x += p.text_width(label, 12) + 16
        text, kind = self.status
        right_edge = W - 52
        if self.update:
            label = f"Update {self.update[0]}"
            bw = p.text_width(label, 12) + 24
            hits.append(self.update_pill(p, right_edge - bw, label))
            right_edge -= bw + 8
        p.circle(x + 3.5, 22, 3.5, {"ok": t["ok"], "busy": t["text3"], "error": t["error"]}[kind])
        p.text(x + 12, 22, p.fit(text, 12, right_edge - x - 12), 12, t["text2"], anchor="lm")
        hits.append(self.icon_button(p, "refresh", W - 44, 4, W - 4, 40, ICON_REFRESH, size=14))

        title = next(label for key, label, _ in PAGES if key == self.page)
        p.text(20, 96, title, 28, t["text"], weight=600)
        pages = {"sound": self.draw_sound, "profiles": self.draw_profiles, "device": self.draw_device,
                 "blindtest": self.draw_blindtest, "connection": self.draw_connection, "settings": self.draw_settings}
        hits += pages[self.page](p)
        self.layers["base"] = p.result()
        self.base_hits = hits
        self.update_tray()

    def update_pill(self, p, x0, label):
        t = self.t
        h = self.hover_amount("update")
        p.rect(x0, 10, x0 + p.text_width(label, 12) + 24, 34, 12, mix(t["accent"], t["text"], 0.1 * h))
        p.text(x0 + 12, 22, label, 12, t["on_accent"], weight=600, anchor="lm")
        return "update", x0, 10, x0 + p.text_width(label, 12) + 24, 34

    def draw_volume_slider(self, p, hits, x0, x1, y, level, name, limit=None):
        t = self.t
        tx = self.slider_x(level or 0, x0, x1)
        p.rect(x0, y - 2, x1, y + 2, 2, t["track"])
        if level:
            p.rect(x0, y - 2, tx, y + 2, 2, t["accent"])
        if limit is not None and limit < 50:
            lx = self.slider_x(limit, x0, x1)
            p.rect(lx - 0.75, y - 7, lx + 0.75, y + 7, 0.5, t["text3"])
        p.circle(tx, y, 10, t["thumb_ring"])
        # Fluent slider thumb: the inner dot grows on hover and shrinks while dragging.
        inner = self.amount(("thumb", name), 5.0 if self.dragging == name else 7.0 if self.hover == name else 6.0)
        p.circle(tx, y, inner, t["accent"])
        hits.append((name, x0 - 10, y - 14, x1 + 10, y + 14))

    def draw_sound(self, p):
        t, level, limit = self.t, self.level, self.settings["volume_limit"]
        hits = []
        # Volume
        self.card(p, 132, 224)
        p.text(32, 160, "Volume", 14, t["text"])
        if level is not None:
            value = "Muted" if level == 0 else f"{level}  ·  {level - 50} dB".replace("-", "\u2212")
            p.text(348, 160, value, 13, t["text2"], anchor="rs")
        icon = ICON_MUTE if not level else ICON_VOL1 if level < 17 else ICON_VOL2 if level < 34 else ICON_VOL3
        h = self.hover_amount("mute")
        if h:
            p.rect(28, SLIDER_Y - 14, 56, SLIDER_Y + 14, 4, mix(t["card"], t["control_hover"], h))
        p.text(42, SLIDER_Y, icon, 16, t["text"], anchor="mm", icons=True)
        hits.append(("mute", 28, SLIDER_Y - 14, 56, SLIDER_Y + 14))
        self.draw_volume_slider(p, hits, SLIDER_X0, SLIDER_X1, SLIDER_Y, level, "volume", limit)

        # Digital filter: dropdown plus an illustrative impulse response of the selection
        self.card(p, 232, 384)
        p.text(32, 258, "Digital filter", 14, t["text"])
        p.text(348, 258, "DAC reconstruction filter", 12, t["text2"], anchor="rs")
        name = "—" if self.filter is None else ka11.FILTERS[self.filter]
        hits.append(self.dropdown(p, "filter", FILTER_BOX, name, open_=self.flyout_owner == "filter"))
        if self.filter is not None:
            p.text(348, 322, "Impulse response", 10, t["text3"], anchor="rm")
            self.draw_curve(p, 32, 316, 348, 352, self.curves[self.filter], 1.5)
            p.text(32, 372, filter_shapes.DESCRIPTIONS[self.filter], 12, t["text2"])
        return hits

    def draw_profiles(self, p):
        t = self.t
        hits = []
        profiles = self.settings["profiles"]
        self.card(p, 132, 196)
        full = len(profiles) >= settings.MAX_PROFILES
        self.card_title(p, 128, "Save current settings",
                        f"Up to {settings.MAX_PROFILES} profiles" if full else "Volume, filter and indicator light")
        if not full:
            hits.append(self.button(p, "profile_save", 260, 148, 348, 180, "Save…"))
        if not profiles:
            p.text(32, 236, "No profiles yet. Set things up the way you like for a pair of", 12, t["text2"])
            p.text(32, 254, "headphones, then save them here.", 12, t["text2"])
            return hits
        y0 = 204
        self.card(p, y0, y0 + 56 * len(profiles))
        for i, prof in enumerate(profiles):
            y = y0 + 56 * i
            if i:
                self.divider(p, y)
            p.text(32, y + 24, p.fit(prof["name"], 14, 170), 14, t["text"])
            filt = ka11.FILTERS[prof["filter"]].replace("Minimum phase", "Min. phase")
            summary = f"Volume {prof['level']} · {filt}"
            p.text(32, y + 42, p.fit(summary, 12, 200), 12, t["text2"])
            hits.append(self.button(p, f"profile_apply:{i}", 244, y + 12, 312, y + 44, "Apply"))
            hits.append(self.icon_button(p, f"profile_delete:{i}", 316, y + 12, 348, y + 44, ICON_DELETE,
                                         size=13, bg=t["card"]))
        return hits

    def draw_device(self, p):
        t = self.t
        hits = []
        # Indicator light
        self.card(p, 132, 252)
        self.card_title(p, 132, "Indicator light", "Status light on the dongle")
        gap = 8
        opt_w = (348 - 32 - gap * (len(LED_OPTIONS) - 1)) / len(LED_OPTIONS)
        for i, (mode, label) in enumerate(LED_OPTIONS):
            x0 = 32 + i * (opt_w + gap)
            hits.append(self.button(p, f"led:{mode}", x0, 196, x0 + opt_w, 232, label, selected=self.led == mode))

        # USB audio mode
        self.card(p, 260, 338)
        pending = self.uac is not None and self.uac_active is not None and self.uac != self.uac_active
        self.card_title(p, 260, "USB audio mode",
                        "Replug the dongle to apply" if pending else "Applies after replugging",
                        t["caution"] if pending else None)
        for i, version in enumerate((1, 2)):
            bx = 196 + i * 80
            hits.append(self.button(p, f"uac:{version}", bx, 283, bx + 72, 315, f"UAC {version}.0",
                                    selected=self.uac == version))

        # Restore defaults
        self.card(p, 346, 422)
        self.card_title(p, 346, "Restore defaults", "Light on, default filter")
        hits.append(self.button(p, "restore", 260, 368, 348, 400, "Restore…"))
        return hits

    def draw_blindtest(self, p):
        t, bt = self.t, self.blind
        hits = []
        if bt is None or bt["stage"] == "setup":
            picks = bt["filters"] if bt else self.default_blind_pair()
            self.card(p, 132, 420)
            p.text(32, 158, "Can you hear the difference?", 14, t["text"])
            body = ("Listen to two filters without knowing which is which, then pick the one you prefer. "
                    f"{BLIND_ROUNDS} rounds. Your filter is put back afterwards.")
            for i, line in enumerate(p.wrap(body, 12, 316)):
                p.text(32, 180 + i * 18, line, 12, t["text2"])
            for i, box in enumerate(BT_BOXES):
                p.text(32, box[1] - 8, ("First filter", "Second filter")[i], 12, t["text2"])
                hits.append(self.dropdown(p, f"bt_pick:{i}", box, ka11.FILTERS[picks[i]],
                                          open_=self.flyout_owner == f"bt_pick:{i}"))
            hits.append(self.button(p, "bt_start", 248, 372, 348, 404, "Start test", accent=True))
            return hits
        if bt["stage"] == "running":
            self.card(p, 132, 412)
            p.text(32, 158, f"Round {bt['round'] + 1} of {BLIND_ROUNDS}", 14, t["text"])
            p.text(32, 178, "Listen to both, as often as you like.", 12, t["text2"])
            for i, side in enumerate("AB"):
                bx = 32 + i * 164
                hits.append(self.button(p, f"bt_listen:{side}", bx, 194, bx + 152, 262, f"Play {side}",
                                        selected=bt["playing"] == side))
            p.text(32, 296, "Which did you prefer?", 12, t["text2"])
            for i, side in enumerate("AB"):
                bx = 32 + i * 164
                hits.append(self.button(p, f"bt_prefer:{side}", bx, 308, bx + 152, 340, side))
            hits.append(self.button(p, "bt_stop", 268, 364, 348, 396, "Stop"))
            return hits
        # results
        a, b = bt["filters"]
        votes = [bt["picks"].count(a), bt["picks"].count(b)]
        self.card(p, 132, 404)
        p.text(32, 158, "Results", 14, t["text"])
        if votes[0] == votes[1]:
            verdict = "No clear favourite. The difference may be too small to hear, which is common."
        else:
            winner = a if votes[0] > votes[1] else b
            verdict = f"You picked {ka11.FILTERS[winner]} in {max(votes)} of {BLIND_ROUNDS} rounds."
        for i, line in enumerate(p.wrap(verdict, 12, 316)):
            p.text(32, 180 + i * 18, line, 12, t["text2"])
        for i, f in enumerate((a, b)):
            y = 236 + i * 32
            p.text(32, y, p.fit(ka11.FILTERS[f], 13, 230), 13, t["text"], anchor="lm")
            p.text(348, y, f"{votes[i]} of {BLIND_ROUNDS}", 13, t["text2"], anchor="rm")
        if votes[0] != votes[1]:
            hits.append(self.button(p, "bt_use", 32, 300, 186, 332, "Use the winner", accent=True))
        hits.append(self.button(p, "bt_again", 194 if votes[0] != votes[1] else 32, 300, 348, 332, "Run again"))
        p.text(32, 368, f"Your filter ({ka11.FILTERS[bt['original']]}) is back on.", 12, t["text3"])
        return hits

    def draw_connection(self, p):
        t = self.t
        hits = []
        rows = self.detail_rows()
        y1 = CONTENT_Y + 16 + len(rows) * ROW_H
        self.card(p, CONTENT_Y, y1)
        for i, (label, value) in enumerate(rows):
            y = CONTENT_Y + 8 + ROW_H * i + ROW_H / 2
            p.text(32, y, label, 13, t["text2"], anchor="lm")
            p.text(348, y, p.fit(value, 13, 190), 13, t["text"], anchor="rm")  # full text: Copy details
        note, color = self.bit_perfect_note()
        if note:
            for i, line in enumerate(p.wrap(note, 12, 340)[:2]):
                p.text(20, y1 + 20 + i * 18, line, 12, color)
        by = H - 48
        if self.info["default"] is False:
            hits.append(self.button(p, "make_default", 16, by, 200, by + 32, "Make default output"))
        hits.append(self.button(p, "copy", 236, by, 364, by + 32, "Copy details", icon=ICON_COPY))
        return hits

    def draw_settings(self, p):
        t, cfg = self.t, self.settings
        hits = []
        hotkey_caption = "Ctrl+Alt+↑ / ↓ volume, Ctrl+Alt+M mute"
        if cfg["hotkeys"] and self.hotkey_conflicts:
            hotkey_caption = "Some keys are taken by another app"
        rows = [("autostart", "Start with Windows", "Opens quietly in the notification area",
                 winshell.autostart_enabled(), None),
                ("keep_in_tray", "Keep running in the tray", "Closing the window doesn't quit", cfg["keep_in_tray"], None),
                ("hotkeys", "Keyboard shortcuts", hotkey_caption, cfg["hotkeys"],
                 t["caution"] if cfg["hotkeys"] and self.hotkey_conflicts else None),
                ("check_updates", "Check for updates", "Asks GitHub for new releases at startup", cfg["check_updates"], None)]
        y0 = 132
        self.card(p, y0, y0 + 60 * len(rows))
        for i, (name, title, caption, on, color) in enumerate(rows):
            y = y0 + 60 * i
            if i:
                self.divider(p, y)
            h = self.hover_amount(f"set:{name}")
            if h:
                p.rect(17, y + 1, 363, y + 59, 0, mix(t["card"], t["control_hover"], h * 0.6))
            p.text(32, y + 26, title, 14, t["text"])
            p.text(32, y + 44, caption, 12, color or t["text2"])
            self.toggle(p, f"set:{name}", 348, y + 30, on)
            hits.append((f"set:{name}", 16, y, 364, y + 60))

        # Volume limit
        y = y0 + 60 * len(rows) + 8
        self.card(p, y, y + 92)
        limit = cfg["volume_limit"]
        p.text(32, y + 26, "Volume limit", 14, t["text"])
        p.text(348, y + 26, "No limit" if limit >= 50 else f"{limit}  ·  {limit - 50} dB".replace("-", "\u2212"), 13,
               t["text2"], anchor="rs")
        p.text(32, y + 44, "The highest device volume the app will set", 12, t["text2"])
        self.draw_volume_slider(p, hits, LIMIT_X0, LIMIT_X1, LIMIT_Y, limit, "limit")
        p.text(20, H - 18, f"KA11 Control {__version__}", 11, t["text3"], anchor="ls")
        return hits

    def render_pane(self):
        """Navigation pane as an RGBA layer with a soft shadow, slid in by compose()."""
        t = self.t
        box = (-8, TOP_BAR_H, PANE_W + SHADOW, H + 8)
        p = Painter(self.scale, (0, 0, 0, 0), box, "RGBA")
        p.rect(-8, TOP_BAR_H, PANE_W, H + 8, 8, t["flyout"], t["flyout_stroke"])
        hits = []
        selected = next(i for i, (key, _, _) in enumerate(PAGES) if key == self.page)
        # Selection pill glides between items, like NavigationView's indicator.
        pill = self.amount("pill", float(selected), 0.3, STANDARD)
        for i, (key, label, icon) in enumerate(PAGES):
            y0 = NAV_Y + i * (NAV_ITEM_H + 4)
            name = f"page:{key}"
            h = max(self.hover_amount(name), self.amount(("nav_sel", key), 1.0 if i == selected else 0.0, NORMAL))
            if h:
                p.rect(8, y0, PANE_W - 8, y0 + NAV_ITEM_H, 4, mix(t["flyout"], t["control_hover"], h))
            p.text(32, y0 + NAV_ITEM_H / 2, icon, 16, t["text"], anchor="mm", icons=True)
            p.text(56, y0 + NAV_ITEM_H / 2, label, 14, t["text"], anchor="lm")
            hits.append((name, 8, y0, PANE_W - 8, y0 + NAV_ITEM_H))
        py = NAV_Y + pill * (NAV_ITEM_H + 4)
        p.rect(8, py + 12, 11, py + NAV_ITEM_H - 12, 1.5, t["accent"])
        layer = p.result()
        self.layers["pane"] = with_shadow(layer, self.scale, (0, 0, PANE_W + 8, H - TOP_BAR_H + 16), 10, 0, 90)
        self.pane_hits = hits

    def render_dialog(self):
        """Confirmation dialog (ContentDialog) as an RGBA layer with a shadow."""
        if not self.dialog:
            return  # closing: keep the last layer so it can fade out
        t, spec = self.t, self.dialog
        x0, x1 = 28, 352
        probe = Painter(self.scale, (0, 0, 0, 0), (0, 0, 1, 1), "RGBA")
        body = probe.wrap(spec["body"], 13, x1 - x0 - 48)
        entry_h = 44 if spec.get("entry") is not None else 0
        content_h = 24 + 28 + len(body) * 20 + 20 + entry_h
        dialog_h = content_h + 80
        y0 = (H - dialog_h) / 2
        box = (x0 - SHADOW, y0 - SHADOW, x1 + SHADOW, y0 + dialog_h + SHADOW)
        p = Painter(self.scale, (0, 0, 0, 0), box, "RGBA")
        p.rect(x0, y0, x1, y0 + dialog_h, 8, t["dialog_footer"], t["flyout_stroke"])
        p.rect(x0 + 1, y0 + 1, x1 - 1, y0 + content_h, 8, t["card"])
        p.rect(x0 + 1, y0 + content_h - 10, x1 - 1, y0 + content_h, 0, t["card"])  # square off the bottom
        p.text(x0 + 24, y0 + 44, spec["title"], 18, t["text"], weight=600)
        for i, line in enumerate(body):
            p.text(x0 + 24, y0 + 72 + i * 20, line, 13, t["text"])
        if entry_h:
            ey = y0 + 72 + len(body) * 20
            p.rect(x0 + 24, ey, x1 - 24, ey + 32, 4, t["control"], t["control_stroke"])
            self.entry_box = (x0 + 24, ey, x1 - 24, ey + 32)
        by = y0 + content_h + 24
        mid = (x0 + x1) / 2
        hits = [self.button(p, "dialog:primary", x0 + 24, by, mid - 4, by + 32, spec["primary"], accent=True),
                self.button(p, "dialog:cancel", mid + 4, by, x1 - 24, by + 32, "Cancel")]
        shadow_box = (SHADOW, SHADOW, SHADOW + x1 - x0, SHADOW + dialog_h)
        self.layers["dialog"] = with_shadow(p.result(), self.scale, shadow_box, 14, 8, 120)
        self.layers["dialog_pos"] = (box[0], box[1])
        self.dialog_hits = hits

    def compose(self):
        """Update the canvas from the cached layers and the current animation values."""
        s, t = self.scale, self.t
        hits = list(self.base_hits)

        # Page entrance: new content rises 28 px and fades in (NavigationView's entrance transition).
        enter = self.value("page", 1.0)
        if enter < 1:
            img = self.layers["base"].copy()
            top = round(TOP_BAR_H * s)
            content = img.crop((0, top, img.width, img.height))
            backdrop = Image.new("RGB", content.size, t["bg"])
            shifted = backdrop.copy()
            shifted.paste(content, (0, round(28 * (1 - enter) * s)))
            img.paste(Image.blend(backdrop, shifted, enter), (0, top))
            self.show("base", img)
            self.base_shown = "entering"
        elif self.base_shown != "base":
            self.show("base", self.layers["base"])
            self.base_shown = "base"

        # Navigation pane slides in from the left: only its canvas item moves.
        pane = self.value("pane", 0.0)
        hidden = round((PANE_W + 8) * s)
        self.place("pane", round(-8 * s) - round(hidden * (1 - pane)), round(TOP_BAR_H * s), pane > 0)
        if self.nav_open:
            hits = [h for h in hits if h[0] in ("nav", "refresh")] + self.pane_hits + [("nav_dismiss", 0, 0, W, H)]

        # Dialog: scrim fades in, dialog scales from 105% to 100% while fading in.
        d = self.value("dialog", 0.0)
        if d > 0:
            base = self.layers["base"]
            img = Image.blend(base, Image.new("RGB", base.size, (0, 0, 0)), (0.55 if t["dark"] else 0.3) * d)
            layer = self.layers["dialog"]
            zoom = 1.05 - 0.05 * d
            if zoom != 1:
                layer = layer.resize((round(layer.width * zoom), round(layer.height * zoom)), Image.BILINEAR)
            x, y = self.layers["dialog_pos"]
            cx = round(x * s + self.layers["dialog"].width / 2 - layer.width / 2)
            cy = round(y * s + self.layers["dialog"].height / 2 - layer.height / 2)
            mask = layer.getchannel("A").point(lambda a: round(a * d))
            img.paste(layer, (cx, cy), mask)
            self.show("dialog", img)
        if "dialog" in self.items:
            self.place("dialog", 0, 0, d > 0)
        # The profile name box is a real text field, shown once its dialog has settled.
        self.show_entry(self.dialog is not None and self.dialog.get("entry") is not None and d >= 1)
        if self.dialog:
            hits = list(self.dialog_hits)
        self.hits = hits

    def show_entry(self, visible):
        t, s = self.t, self.scale
        if visible and self.entry is None:
            x0, y0, x1, y1 = self.entry_box
            self.entry = tk.Entry(self.view, bd=0, relief="flat", font=("Segoe UI Variable Text", 11),
                                  bg=hexcolor(t["control"]), fg=hexcolor(t["text"]),
                                  insertbackground=hexcolor(t["text"]), highlightthickness=0)
            self.entry.insert(0, self.dialog["entry"])
            self.entry.select_range(0, "end")
            self.entry.bind("<Return>", lambda _: self.dialog_primary())
            self.entry.bind("<Escape>", lambda _: self.cancel_dialog())
            self.entry_item = self.view.create_window(round((x0 + 10) * s), round((y0 + 5) * s), anchor="nw",
                                                      window=self.entry, width=round((x1 - x0 - 20) * s),
                                                      height=round((y1 - y0 - 10) * s))
            self.entry.focus_set()
        elif not visible and self.entry is not None:
            self.view.delete(self.entry_item)
            self.entry.destroy()
            self.entry = None

    # ---------- input ----------

    @property
    def flyout_owner(self):
        return self.flyout.owner if self.flyout else None

    def hit(self, event):
        x, y = event.x / self.scale, event.y / self.scale
        return next((name for name, x0, y0, x1, y1 in self.hits if x0 <= x <= x1 and y0 <= y <= y1), None)

    def level_from_x(self, event, x0=SLIDER_X0, x1=SLIDER_X1, lowest=0, highest=50):
        x = event.x / self.scale
        return max(lowest, min(highest, round((x - x0) / (x1 - x0) * 50)))

    def layer_of(self, name):
        if name is None:
            return None
        if name.startswith("dialog:"):
            return "dialog"
        return "pane" if name.startswith("page:") else "base"

    def set_hover(self, name):
        if name != self.hover:
            layers = {self.layer_of(self.hover), self.layer_of(name)} - {None}
            self.hover = name
            self.invalidate(*layers or {"base"})

    def on_hover(self, event):
        self.set_hover(self.hit(event))

    def on_press(self, event):
        if self.flyout:
            self.close_flyout()
            return
        name = self.hit(event)
        if name == "volume" and self.level is not None:
            self.dragging = "volume"
            self.level = self.level_from_x(event, highest=self.settings["volume_limit"])
        elif name == "limit":
            self.dragging = "limit"
            self.settings["volume_limit"] = self.level_from_x(event, LIMIT_X0, LIMIT_X1, lowest=1)
        else:
            self.pressed = name
        self.invalidate(self.layer_of(name) or "base")

    def on_drag(self, event):
        if self.dragging == "volume":
            self.level = self.level_from_x(event, highest=self.settings["volume_limit"])
            self.invalidate()
        elif self.dragging == "limit":
            self.settings["volume_limit"] = self.level_from_x(event, LIMIT_X0, LIMIT_X1, lowest=1)
            self.invalidate()

    def on_release(self, event):
        if self.dragging:
            dragged, self.dragging = self.dragging, None
            self.invalidate()
            if dragged == "volume":
                # Apply on release rather than on every drag step: each change is several USB writes.
                self.apply_level()
            else:
                self.save_settings()
                if self.level is not None and self.level > self.settings["volume_limit"]:
                    self.level = self.settings["volume_limit"]  # bring the dongle under the new limit
                    self.apply_level()
            return
        name, self.pressed = self.pressed, None
        self.invalidate(self.layer_of(name) or "base")
        if not name or name != self.hit(event):
            return
        self.click(name)

    def click(self, name):
        if name == "nav":
            self.set_nav(not self.nav_open)
        elif name == "nav_dismiss":
            self.set_nav(False)
        elif name.startswith("page:"):
            if name[5:] != self.page:
                self.page = name[5:]
                self.start("page", 1.0, 0.3, DECELERATE, begin=0.0)
                self.invalidate("base", "pane")
                if self.page == "connection":
                    self.refresh()  # fresh sample rate, Windows format and response time
            self.set_nav(False)
        elif name == "refresh":
            self.refresh()
        elif name == "update" and self.update:
            webbrowser.open(self.update[1])
        elif name == "copy":
            self.copy_details()
        elif name == "make_default":
            self.make_default()
        elif name == "mute":
            self.toggle_mute()
        elif name.startswith("led:"):
            self.apply_led(name[4:])
        elif name == "filter" and self.filter is not None:
            self.open_flyout("filter", FILTER_BOX, self.filter, self.apply_filter)
        elif name.startswith("uac:"):
            self.apply_uac(int(name[4:]))
        elif name == "restore":
            self.set_dialog(RESTORE_DIALOG)
        elif name == "profile_save":
            n = len(self.settings["profiles"]) + 1
            self.set_dialog(dict(kind="save_profile", title="Save profile", primary="Save", entry=f"Profile {n}",
                                 body="Give these settings a name, like IEMs or Headphones."))
        elif name.startswith("profile_apply:"):
            self.apply_profile(int(name.split(":")[1]))
        elif name.startswith("profile_delete:"):
            i = int(name.split(":")[1])
            self.set_dialog(dict(kind="delete_profile", index=i, primary="Delete",
                                 title=f"Delete {self.settings['profiles'][i]['name']}?",
                                 body="This only removes the saved profile. Your current settings don't change."))
        elif name.startswith("set:"):
            self.toggle_setting(name[4:])
        elif name.startswith("bt_"):
            self.blind_click(name)
        elif name == "dialog:cancel":
            self.cancel_dialog()
        elif name == "dialog:primary":
            self.dialog_primary()

    def dialog_primary(self):
        spec = self.dialog
        if not spec:
            return
        kind = spec["kind"]
        typed = self.entry.get().strip() if self.entry else None
        self.set_dialog(None)
        if kind == "restore":
            self.apply_restore()
        elif kind == "volume":
            self.apply_level(allow_jump=True)
        elif kind == "save_profile":
            self.save_profile(typed or spec["entry"])
        elif kind == "delete_profile":
            del self.settings["profiles"][spec["index"]]
            self.save_settings()

    def set_nav(self, open_):
        if open_ != self.nav_open:
            self.nav_open = open_
            if self.hover and self.hover.startswith("page:"):
                self.hover = None  # don't reopen with a stale item highlight; keep the hamburger's
                self.invalidate("pane")
            # Decelerate both ways: the pane responds instantly to the click and settles softly.
            # (An accelerating exit barely moves for the first ~40 ms, which reads as lag after a click.)
            self.start("pane", 1.0 if open_ else 0.0, SLOW if open_ else NORMAL, DECELERATE)
            self.invalidate("base")  # pane layer is already cached; only the hamburger highlight changes

    def set_dialog(self, dialog):
        if dialog != self.dialog:
            if dialog and self.root.state() != "normal":
                self.show_window()  # e.g. the jump guard asking from a tray flyout or hotkey
            self.dialog = dialog
            self.hover = None
            self.start("dialog", 1.0 if dialog else 0.0, SLOW if dialog else NORMAL, DECELERATE)
            self.invalidate("base", "dialog")

    def cancel_dialog(self):
        if self.dialog and self.dialog["kind"] == "volume" and self.applied_level is not None:
            self.level = self.applied_level  # put the slider back where the dongle actually is
        self.set_dialog(None)

    def dismiss(self):
        """Esc: close whatever is on top."""
        if self.flyout:
            self.close_flyout()
        elif self.dialog:
            self.cancel_dialog()
        elif self.nav_open:
            self.set_nav(False)

    def open_flyout(self, owner, box, selected, on_pick, exclude=None):
        self.flyout = FilterFlyout(self, box, selected, on_pick, exclude)
        self.flyout.owner = owner
        self.invalidate()

    def close_flyout(self):
        if self.flyout:
            self.flyout.destroy()
            self.flyout = None
            self.invalidate()

    def nudge(self, step):
        """Mouse wheel / arrow keys in the window: only on the Sound page, like a focused slider."""
        if self.dialog or self.nav_open or self.page != "sound":
            return
        self.volume_step(step, osd=False)

    def volume_step(self, step, osd=True):
        """Change the device volume by whole levels, applied once the changes stop coming."""
        if self.level is None or self.dialog:
            return
        self.level = max(0, min(self.settings["volume_limit"], self.level + step))
        self.invalidate()
        self.tray.refresh()
        if osd:
            self.show_osd()
        if self.apply_job:
            self.root.after_cancel(self.apply_job)
        self.apply_job = self.root.after(300, self.apply_level)

    def toggle_mute(self):
        if self.level is None:
            return
        if self.level:
            self.unmuted_level, self.level = self.level, 0
        else:
            self.level = min(self.unmuted_level, self.settings["volume_limit"])
        self.apply_level(allow_jump=True)  # unmuting returns to a level you already had

    # ---------- settings, profiles, blind test ----------

    def save_settings(self):
        try:
            settings.save(self.settings)
        except OSError as e:
            self.set_status(f"Couldn't save settings ({e.strerror})", "error")
        self.tray.refresh()

    def toggle_setting(self, name):
        if name == "autostart":
            self.set_autostart(not winshell.autostart_enabled())
            return
        self.settings[name] = not self.settings[name]
        self.save_settings()
        if name == "hotkeys":
            self.apply_hotkeys()
        elif name == "check_updates" and self.settings[name]:
            self.check_updates()
        self.invalidate()

    def save_profile(self, name):
        name = name[:40]
        profile = dict(name=name, level=self.level, filter=self.filter, led=self.led)
        if None in profile.values():
            self.set_status("Can't save a profile until the dongle has been read", "error")
            return
        profiles = [p for p in self.settings["profiles"] if p["name"].lower() != name.lower()]
        profiles.append(profile)
        self.settings["profiles"] = profiles[-settings.MAX_PROFILES:]
        self.save_settings()
        self.invalidate()

    def apply_profile(self, index):
        try:
            prof = self.settings["profiles"][index]
        except IndexError:
            return

        def work(dev):
            dev.set_filter(prof["filter"])
            dev.set_led(prof["led"])
            return dev.get_filter(), dev.get_led()

        def done(state):
            self.filter, self.led = state
            # Volume last, through the usual path: the limit and the jump guard both apply.
            self.level = min(prof["level"], self.settings["volume_limit"])
            self.apply_level()

        self._run(work, done, f"Applying {prof['name']}…", retry=lambda: self.apply_profile(index))

    def default_blind_pair(self):
        current = self.filter if self.filter is not None else 0
        return [current, 2 if current != 2 else 0]

    def blind_click(self, name):
        bt = self.blind
        if name.startswith("bt_pick:"):
            i = int(name[8:])
            picks = bt["filters"] if bt else self.default_blind_pair()

            def pick(f):
                self.blind = dict(stage="setup", filters=list(picks))
                self.blind["filters"][i] = f
                self.invalidate()

            self.open_flyout(name, BT_BOXES[i], picks[i], pick, exclude=picks[1 - i])
        elif name == "bt_start" and self.filter is not None:
            filters = bt["filters"] if bt else self.default_blind_pair()
            self.blind = dict(stage="running", filters=filters, original=self.filter, round=0, picks=[],
                              playing=None, order=None)
            self.next_blind_round()
        elif name.startswith("bt_listen:"):
            side = name[-1]
            bt["playing"] = side
            self.apply_filter(bt["order"][side])
        elif name.startswith("bt_prefer:"):
            bt["picks"].append(bt["order"][name[-1]])
            bt["round"] += 1
            if bt["round"] < BLIND_ROUNDS:
                self.next_blind_round()
            else:
                bt["stage"] = "results"
                self.apply_filter(bt["original"])  # put the listener's filter back
        elif name == "bt_stop":
            self.apply_filter(bt["original"])
            self.blind = dict(stage="setup", filters=bt["filters"])
        elif name == "bt_use":
            a, b = bt["filters"]
            self.apply_filter(a if bt["picks"].count(a) > bt["picks"].count(b) else b)
            self.blind = dict(stage="setup", filters=bt["filters"])
        elif name == "bt_again":
            self.blind = dict(stage="setup", filters=bt["filters"])
        self.invalidate()

    def next_blind_round(self):
        a, b = self.blind["filters"]
        if random.random() < 0.5:
            a, b = b, a
        self.blind["order"] = {"A": a, "B": b}
        self.blind["playing"] = None

    # ---------- device ----------

    def set_status(self, text, kind):
        self.status = (text, kind)
        self.invalidate()

    def record(self, meta, error):
        """Update the connection details after each operation."""
        self.info.update({k: v for k, v in meta.items() if v is not None})
        self.info["checked"] = time.strftime("%H:%M:%S")
        if error:
            self.info["error"] = f"{error} ({self.info['checked']})"

    def _run(self, work, done, message, retry=None):
        """Run device I/O off the UI thread, one operation at a time. If busy, call retry later."""
        if not self.busy.acquire(blocking=False):
            if retry:
                self.root.after(200, retry)
            return False
        self.set_status(message, "busy")

        def worker():
            try:
                with ka11.KA11() as dev:
                    result = work(dev)
                    meta = dict(firmware=dev.firmware, response_ms=dev.last_response_ms)

                def finish():
                    self.record(meta, None)
                    done(result)
                    self.set_status("Connected", "ok")
            except BaseException as e:  # SystemExit from ka11 when the dongle is unplugged
                message = str(e) or type(e).__name__

                def finish():
                    self.record({}, message)
                    self.set_status(message, "error")
            self.results.put(finish)
            self.busy.release()

        threading.Thread(target=worker, daemon=True).start()
        if not self.polling:
            self.poll_results()
        return True

    def poll_results(self):
        """Run finished work on the Tk thread. Tkinter isn't thread-safe, so workers never touch it."""
        while not self.results.empty():
            self.results.get()()
        if self.busy.locked() or not self.results.empty():
            self.polling = True
            self.root.after(10, self.poll_results)
        else:
            self.polling = False

    def refresh(self):
        self.reconnect_job = None

        def work(dev):
            state = dev.get_level(), dev.get_led(), dev.get_sample_rate(), dev.get_filter(), dev.get_uac()
            try:
                windows = winvolume.shared_format(), winvolume.is_default()
            except OSError:
                windows = None, None
            return state, windows

        def done(result):
            (self.level, self.led, self.info["sample_rate"], self.filter, self.uac), windows = result
            self.info["windows_format"], self.info["default"] = windows
            self.applied_level = self.level
            if self.uac_active is None:
                self.uac_active = self.uac
            if self.level:
                self.unmuted_level = self.level
            self.invalidate()

        self._run(work, done, "Reading…", retry=self.refresh)

    def copy_details(self):
        rows = self.detail_rows()
        note, _ = self.bit_perfect_note()
        text = f"KA11 Control {__version__} - connection details\n" + "\n".join(f"{k}: {v}" for k, v in rows)
        if note:
            text += "\n" + note
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("Copied connection details", "ok")

    def make_default(self):
        # On a worker thread like everything else that calls into Windows: COM calls can pump
        # window messages, which must not happen inside a Tk callback.
        def worker():
            try:
                ok, error = winvolume.make_default(), None
            except OSError as e:
                ok, error = False, e

            def finish():
                if ok:
                    self.info["default"] = True
                    self.set_status("KA11 is now the default output", "ok")
                elif error:
                    self.set_status(f"Couldn't change the default output ({error})", "error")
            self.results.put(finish)

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(500, self.poll_results)

    def check_updates(self):
        def worker():
            found = updates.check()
            if found:
                self.results.put(lambda: self.found_update(found))
        threading.Thread(target=worker, daemon=True).start()
        self.root.after(6000, self.poll_results)  # the request times out after 5 s

    def found_update(self, found):
        self.update = found
        self.invalidate()

    def apply_level(self, allow_jump=False):
        self.apply_job = None
        if self.level is None:
            return
        level = self.level = min(self.level, self.settings["volume_limit"])
        if not allow_jump and self.applied_level is not None and level - self.applied_level > ka11.JUMP_GUARD_DB:
            self.confirm_jump(level - self.applied_level)
            return

        def work(dev):
            try:
                dev.set_level(level, allow_jump=allow_jump)
            except ka11.VolumeJumpError as e:  # the dongle was quieter than we thought; ask instead
                return "jump", e.increase_db
            return "ok", dev.get_level()

        def done(result):
            kind, value = result
            if kind == "jump":
                self.confirm_jump(value)
                return
            self.applied_level = value
            if not self.dragging and self.apply_job is None:
                self.level = value
                if value:
                    self.unmuted_level = value
                self.invalidate()
            self.tray.refresh()

        if not self._run(work, done, "Setting volume…"):
            # retry once the current operation finishes
            self.apply_job = self.root.after(200, lambda: self.apply_level(allow_jump))

    def confirm_jump(self, increase_db):
        """Jump guard: ask before raising the volume by more than JUMP_GUARD_DB in one go."""
        self.set_dialog(dict(kind="volume", title=f"Raise the volume by {increase_db} dB?", primary="Raise volume",
                             body="That's a big jump in one go. If you're wearing headphones, take them off "
                                  "or turn it up in smaller steps."))

    def apply_setting(self, attr, value, setter, getter, message):
        """Optimistically show the new value, write it, then show what the dongle reports back."""
        setattr(self, attr, value)
        self.invalidate()

        def work(dev):
            setter(dev, value)
            return getter(dev)

        def done(actual):
            setattr(self, attr, actual)
            self.invalidate()

        self._run(work, done, message,
                  retry=lambda: self.apply_setting(attr, value, setter, getter, message))

    def apply_led(self, mode):
        self.apply_setting("led", mode, ka11.KA11.set_led, ka11.KA11.get_led, "Setting indicator light…")

    def apply_filter(self, index):
        self.apply_setting("filter", index, ka11.KA11.set_filter, ka11.KA11.get_filter, "Setting filter…")

    def apply_uac(self, version):
        self.apply_setting("uac", version, ka11.KA11.set_uac, ka11.KA11.get_uac, "Setting USB audio mode…")

    def apply_restore(self):
        def work(dev):
            dev.restore_defaults()
            return dev.get_led(), dev.get_filter()

        def done(state):
            self.led, self.filter = state
            self.invalidate()

        self._run(work, done, "Restoring defaults…", retry=self.apply_restore)


def log_crashes():
    """The windowed exe has no console, so errors would vanish. Send them, and Python's report of any
    fatal crash, to %APPDATA%\\KA11 Control\\log.txt instead."""
    if sys.stderr is not None:
        return
    import faulthandler
    path = settings.path().with_name("log.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    log = open(path, "a", encoding="utf-8", buffering=1)
    log.write(f"\n--- KA11 Control {__version__} started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    sys.stderr = sys.stdout = log
    faulthandler.enable(log)


def main():
    log_crashes()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp rendering on high-DPI screens
    except (AttributeError, OSError):
        pass
    if not winshell.claim_single_instance():
        return  # the running copy has been asked to show itself
    # Own taskbar identity, so Windows shows this window's icon instead of grouping it under python.exe.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KA11Control")
    root = tk.Tk()
    App(root, start_hidden="--tray" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()
