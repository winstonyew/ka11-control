"""KA11 Control - an unofficial Windows 11 (Fluent) style control panel for the FiiO KA11.

A Settings-style navigation pane with three pages: Sound (volume, digital filter), Device
(indicator light, USB audio mode, restore defaults) and Connection (diagnostics).

The UI is painted with Pillow (supersampled for smooth corners) using the system's light/dark
mode, accent colour, Segoe UI Variable and Segoe Fluent Icons. Rendering is layered so motion
stays smooth: the page is drawn once and cached, and the navigation pane and dialog are cached
layers composited on top, so sliding/fading frames only cost a paste or a blend.
"""
import ctypes
import threading
import time
import tkinter as tk
import winreg

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

import filter_shapes
import ka11

W, H = 380, 444  # logical (96-dpi) window size; every page fits
SS = 3  # supersampling factor
FONT_TEXT = r"C:\Windows\Fonts\SegUIVar.ttf"
FONT_ICONS = r"C:\Windows\Fonts\SegoeIcons.ttf"
ICON_MUTE, ICON_VOL1, ICON_VOL2, ICON_VOL3, ICON_REFRESH = "\ue74f", "\ue993", "\ue994", "\ue995", "\ue72c"
ICON_HEADPHONES, ICON_CHEVRON_DOWN, ICON_COPY = "\ue7f6", "\ue70d", "\ue8c8"
ICON_NAV, ICON_SOUND, ICON_DEVICE, ICON_CONNECTION = "\ue700", "\ue995", "\ue88e", "\ue71b"
PAGES = (("sound", "Sound", ICON_SOUND), ("device", "Device", ICON_DEVICE), ("connection", "Connection", ICON_CONNECTION))
LED_OPTIONS = (("on", "On"), ("off-once", "Off for now"), ("off", "Always off"))
RESTORE_DIALOG = dict(kind="restore", title="Restore default settings?", primary="Restore",
                      body="The indicator light turns on and the filter goes back to Minimum phase fast "
                           "roll-off. Volume and USB audio mode stay as they are.")
TOP_BAR_H = 44
CONTENT_Y = 132  # first card on each page
SLIDER_X0, SLIDER_X1, SLIDER_Y = 70, 344, 190
FILTER_BOX = (32, 270, 348, 304)  # dropdown button on the Sound page
ROW_H = 26
PANE_W, NAV_Y, NAV_ITEM_H = 280, 52, 40
FLYOUT_ITEM_H, FLYOUT_PAD = 36, 4
SHADOW = 24  # room around overlay layers for their soft shadows

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
                    subtle_hover=(255, 255, 255, 15), track=(140, 140, 140), thumb_ring=(69, 69, 69),
                    accent=accent, on_accent=(0, 0, 0), flyout=(44, 44, 44), flyout_stroke=(60, 60, 60),
                    dialog_footer=(32, 32, 32), ok=(108, 203, 95), error=(255, 153, 164), caution=(252, 225, 0))
    return dict(dark=False, bg=(243, 243, 243), card=(251, 251, 251), card_stroke=(229, 229, 229),
                text=(26, 26, 26), text2=(93, 93, 93), text3=(140, 140, 140),
                control=(253, 253, 253), control_hover=(246, 246, 246), control_stroke=(214, 214, 214),
                subtle_hover=(0, 0, 0, 10), track=(135, 135, 135), thumb_ring=(255, 255, 255),
                accent=accent, on_accent=(255, 255, 255), flyout=(249, 249, 249), flyout_stroke=(220, 220, 220),
                dialog_footer=(243, 243, 243), ok=(15, 123, 15), error=(196, 43, 28), caution=(157, 93, 0))


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

    def circle(self, cx, cy, r, fill):
        self.d.ellipse((*self.xy(cx - r, cy - r), *self.xy(cx + r, cy + r)), fill=fill)

    def line(self, points, fill, width):
        self.d.line([self.xy(x, y) for x, y in points], fill=fill, width=round(width * self.k), joint="curve")

    def text(self, x, y, s, size, fill, weight=400, anchor="ls", icons=False):
        self.d.text(self.xy(x, y), s, font=self.font(size, weight, icons), fill=fill, anchor=anchor)

    def text_width(self, s, size, weight=400):
        return self.font(size, weight).getlength(s) / self.k

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


def round_corners(window):
    """Ask DWM for Windows 11 rounded corners (no-op on Windows 10)."""
    window.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)


class FilterFlyout:
    """Fluent-style dropdown list for the digital filter: a borderless popup that slides down and
    fades in, with hover highlights that fade like the main window's."""

    def __init__(self, app):
        self.app, self.hover = app, None
        self.tweens = {}
        self.job = None
        t, s = app.t, app.scale
        x0, _, x1, y1 = FILTER_BOX
        self.width = x1 - x0
        self.height = FLYOUT_PAD * 2 + FLYOUT_ITEM_H * len(ka11.FILTERS)
        self.x = app.root.winfo_rootx() + round(x0 * s)
        self.y = app.root.winfo_rooty() + round((y1 + 4) * s)
        self.win = tk.Toplevel(app.root, bg="#%02x%02x%02x" % t["flyout"])
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
        return i if 0 <= y and i < len(ka11.FILTERS) else None

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
            self.app.apply_filter(i)

    def draw(self):
        t = self.app.t
        p = Painter(self.app.scale, t["flyout"], (0, 0, self.width, self.height))
        p.rect(0, 0, self.width - 0.5, self.height - 0.5, 8, t["flyout"], t["flyout_stroke"])
        for i, name in enumerate(ka11.FILTERS):
            y0 = FLYOUT_PAD + i * FLYOUT_ITEM_H
            selected = i == self.app.filter
            h = self.amount(i, 1.0 if selected or i == self.hover else 0.0)
            if h:
                p.rect(4, y0 + 2, self.width - 4, y0 + FLYOUT_ITEM_H - 2, 4, mix(t["flyout"], t["control_hover"], h))
            if selected:
                p.rect(4, y0 + 10, 7, y0 + FLYOUT_ITEM_H - 10, 1.5, t["accent"])
            p.text(16, y0 + FLYOUT_ITEM_H / 2, name, 13, t["text"], anchor="lm")
            self.app.draw_curve(p, self.width - 76, y0 + 8, self.width - 14, y0 + FLYOUT_ITEM_H - 6,
                                self.app.curves[i], 1.25)
        self.photo = ImageTk.PhotoImage(p.result())
        self.view.config(image=self.photo)

    def destroy(self):
        if self.job:
            self.win.after_cancel(self.job)
        self.win.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.t = system_theme()
        root.title("KA11 Control")
        root.resizable(False, False)
        root.configure(bg="#%02x%02x%02x" % self.t["bg"])
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
        self.flyout = None
        self.level = None
        self.unmuted_level = 25
        self.applied_level = None  # last level confirmed on the dongle; the jump guard compares to it
        self.led = None
        self.filter = None
        self.uac = None
        self.uac_active = None  # mode the dongle was plugged in with; a change applies on replug
        self.status = ("Connecting…", "busy")
        self.info = dict(sample_rate=None, firmware=None, response_ms=None, checked=None, error=None)
        self.hover = None
        self.pressed = None
        self.dragging = False
        self.busy = threading.Lock()
        self.apply_job = None

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

        self.frame()
        self.refresh()

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
        """Window/taskbar icon: a white Fluent headphones silhouette on a transparent background."""
        size = 256
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).text((size / 2, size / 2), ICON_HEADPHONES, font=ImageFont.truetype(FONT_ICONS, 236),
                                  fill=255, anchor="mm", stroke_width=4, stroke_fill=255)
        # Segoe Fluent only has an outline headphones glyph: fill its enclosed ear cups by flooding
        # the outside and treating everything the flood didn't reach as solid.
        ImageDraw.floodfill(mask, (0, 0), 128)
        mask = mask.point(lambda v: 0 if v == 128 else 255)
        img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        img.putalpha(mask)
        self.icons = [ImageTk.PhotoImage(img.resize((s, s), Image.LANCZOS)) for s in (64, 32, 16)]
        self.root.iconphoto(True, *self.icons)

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

    def slider_x(self, level):
        return SLIDER_X0 + (SLIDER_X1 - SLIDER_X0) * level / 50

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
            p.text((x0 + x1) / 2, cy, label, 13, color, weight=600 if sel > 0.5 else 400, anchor="mm")
        return name, x0, y0, x1, y1

    def icon_button(self, p, name, x0, y0, x1, y1, icon, size=16, active=False):
        """Borderless toolbar button (hamburger, refresh) that fades in a subtle fill on hover."""
        t = self.t
        h = max(self.hover_amount(name), 1.0 if active else 0.0)
        if h:
            p.rect(x0, y0, x1, y1, 4, mix(t["bg"], t["control_hover"], h))
        color = t["text2"] if self.pressed == name and self.hover == name else t["text"]
        p.text((x0 + x1) / 2, (y0 + y1) / 2, icon, size, color, anchor="mm", icons=True)
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

    def card_title(self, p, y, title, caption, caption_color=None):
        p.text(32, y + 26, title, 14, self.t["text"])
        p.text(32, y + 46, caption, 12, caption_color or self.t["text2"])

    def detail_rows(self):
        info, (status, kind) = self.info, self.status
        rate = info["sample_rate"]
        return [
            ("Status", "Working…" if kind == "busy" else status),
            ("Sample rate", "—" if rate is None else f"{rate / 1000:g} kHz"),
            ("Firmware", info["firmware"] or "—"),
            ("USB device", f"VID {ka11.VID:04X} · PID {ka11.PID:04X}"),
            ("USB audio mode", "—" if self.uac_active is None else f"UAC {self.uac_active}.0"),
            ("Response time", "—" if info["response_ms"] is None else f"{info['response_ms']:.0f} ms"),
            ("Last checked", info["checked"] or "—"),
            ("Last error", info["error"] or "None"),
        ]

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
        if len(text) > 30:  # the full message is on the Connection page
            text = text[:29] + "…"
        p.circle(x + 3.5, 22, 3.5, {"ok": t["ok"], "busy": t["text3"], "error": t["error"]}[kind])
        p.text(x + 12, 22, text, 12, t["text2"], anchor="lm")
        hits.append(self.icon_button(p, "refresh", W - 44, 4, W - 4, 40, ICON_REFRESH, size=14))

        title = next(label for key, label, _ in PAGES if key == self.page)
        p.text(20, 96, title, 28, t["text"], weight=600)
        hits += {"sound": self.draw_sound, "device": self.draw_device, "connection": self.draw_connection}[self.page](p)
        self.layers["base"] = p.result()
        self.base_hits = hits

    def draw_sound(self, p):
        t, level = self.t, self.level
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
        tx = self.slider_x(level or 0)
        p.rect(SLIDER_X0, SLIDER_Y - 2, SLIDER_X1, SLIDER_Y + 2, 2, t["track"])
        if level:
            p.rect(SLIDER_X0, SLIDER_Y - 2, tx, SLIDER_Y + 2, 2, t["accent"])
        p.circle(tx, SLIDER_Y, 10, t["thumb_ring"])
        # Fluent slider thumb: the inner dot grows on hover and shrinks while dragging.
        inner = self.amount("thumb", 5.0 if self.dragging else 7.0 if self.hover == "slider" else 6.0)
        p.circle(tx, SLIDER_Y, inner, t["accent"])
        hits.append(("slider", SLIDER_X0 - 10, SLIDER_Y - 14, SLIDER_X1 + 10, SLIDER_Y + 14))

        # Digital filter: dropdown plus an illustrative impulse response of the selection
        self.card(p, 232, 384)
        p.text(32, 258, "Digital filter", 14, t["text"])
        p.text(348, 258, "DAC reconstruction filter", 12, t["text2"], anchor="rs")
        x0, y0, x1, y1 = FILTER_BOX
        h = max(self.hover_amount("filter"), 1.0 if self.flyout else 0.0)
        p.rect(x0, y0, x1, y1, 4, mix(t["control"], t["control_hover"], h), t["control_stroke"])
        name = "—" if self.filter is None else ka11.FILTERS[self.filter]
        p.text(x0 + 12, (y0 + y1) / 2, name, 13, t["text"], anchor="lm")
        p.text(x1 - 16, (y0 + y1) / 2, ICON_CHEVRON_DOWN, 10, t["text2"], anchor="mm", icons=True)
        hits.append(("filter", x0, y0, x1, y1))
        if self.filter is not None:
            p.text(348, 322, "Impulse response", 10, t["text3"], anchor="rm")
            self.draw_curve(p, 32, 316, 348, 352, self.curves[self.filter], 1.5)
            p.text(32, 372, filter_shapes.DESCRIPTIONS[self.filter], 12, t["text2"])
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

    def draw_connection(self, p):
        t = self.t
        rows = self.detail_rows()
        y1 = CONTENT_Y + 16 + len(rows) * ROW_H
        self.card(p, CONTENT_Y, y1)
        for i, (label, value) in enumerate(rows):
            y = CONTENT_Y + 8 + ROW_H * i + ROW_H / 2
            p.text(32, y, label, 13, t["text2"], anchor="lm")
            if len(value) > 34:  # full text still goes to the clipboard via Copy details
                value = value[:33] + "…"
            p.text(348, y, value, 13, t["text"], anchor="rm")
        return [self.button(p, "copy", 236, y1 + 12, 364, y1 + 44, "Copy details", icon=ICON_COPY)]

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
        content_h = 24 + 28 + len(body) * 20 + 20
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
        if self.dialog:
            hits = list(self.dialog_hits)
        self.hits = hits

    # ---------- input ----------

    def hit(self, event):
        x, y = event.x / self.scale, event.y / self.scale
        return next((name for name, x0, y0, x1, y1 in self.hits if x0 <= x <= x1 and y0 <= y <= y1), None)

    def level_from_x(self, event):
        x = event.x / self.scale
        return max(0, min(50, round((x - SLIDER_X0) / (SLIDER_X1 - SLIDER_X0) * 50)))

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
        if name == "slider" and self.level is not None:
            self.dragging = True
            self.level = self.level_from_x(event)
        else:
            self.pressed = name
        self.invalidate(self.layer_of(name) or "base")

    def on_drag(self, event):
        if self.dragging:
            self.level = self.level_from_x(event)
            self.invalidate()

    def on_release(self, event):
        if self.dragging:
            # Apply on release rather than on every drag step: each change is several USB writes.
            self.dragging = False
            self.invalidate()
            self.apply_level()
            return
        name, self.pressed = self.pressed, None
        self.invalidate(self.layer_of(name) or "base")
        if not name or name != self.hit(event):
            return
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
                    self.refresh()  # fresh sample rate and response time
            self.set_nav(False)
        elif name == "refresh":
            self.refresh()
        elif name == "copy":
            self.copy_details()
        elif name == "mute" and self.level is not None:
            if self.level:
                self.unmuted_level, self.level = self.level, 0
            else:
                self.level = self.unmuted_level
            self.apply_level(allow_jump=True)  # unmuting returns to a level you already had
        elif name.startswith("led:"):
            self.apply_led(name[4:])
        elif name == "filter" and self.filter is not None:
            self.flyout = FilterFlyout(self)
            self.invalidate()
        elif name.startswith("uac:"):
            self.apply_uac(int(name[4:]))
        elif name == "restore":
            self.set_dialog(RESTORE_DIALOG)
        elif name == "dialog:cancel":
            self.cancel_dialog()
        elif name == "dialog:primary":
            kind = self.dialog["kind"]
            self.set_dialog(None)
            if kind == "restore":
                self.apply_restore()
            elif kind == "volume":
                self.apply_level(allow_jump=True)

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

    def close_flyout(self):
        if self.flyout:
            self.flyout.destroy()
            self.flyout = None
            self.invalidate()

    def nudge(self, step):
        if self.level is None or self.dialog or self.nav_open or self.page != "sound":
            return
        self.level = max(0, min(50, self.level + step))
        self.invalidate()
        if self.apply_job:
            self.root.after_cancel(self.apply_job)
        self.apply_job = self.root.after(400, self.apply_level)

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
                self.root.after(0, self.record, meta, None)
                self.root.after(0, done, result)
                self.root.after(0, self.set_status, "Connected", "ok")
            except BaseException as e:  # SystemExit from ka11 when the dongle is unplugged
                message = str(e) or type(e).__name__
                self.root.after(0, self.record, {}, message)
                self.root.after(0, self.set_status, message, "error")
            finally:
                self.busy.release()

        threading.Thread(target=worker, daemon=True).start()
        return True

    def refresh(self):
        def work(dev):
            return dev.get_level(), dev.get_led(), dev.get_sample_rate(), dev.get_filter(), dev.get_uac()

        def done(state):
            self.level, self.led, self.info["sample_rate"], self.filter, self.uac = state
            self.applied_level = self.level
            if self.uac_active is None:
                self.uac_active = self.uac
            if self.level:
                self.unmuted_level = self.level
            self.invalidate()

        self._run(work, done, "Reading…")

    def copy_details(self):
        text = "KA11 Control - connection details\n" + "\n".join(f"{k}: {v}" for k, v in self.detail_rows())
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("Copied connection details", "ok")

    def apply_level(self, allow_jump=False):
        self.apply_job = None
        level = self.level
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


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp rendering on high-DPI screens
    except (AttributeError, OSError):
        pass
    # Own taskbar identity, so Windows shows this window's icon instead of grouping it under python.exe.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KA11Control")
    root = tk.Tk()
    App(root)
    root.mainloop()
