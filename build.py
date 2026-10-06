"""Build the portable single-file exe: dist/KA11-Control.exe

Needs PyInstaller and Pillow (numpy is only used to regenerate the filter curves and isn't bundled):
    python -m pip install -r requirements.txt
    python build.py
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
ICON = HERE / "build" / "ka11.ico"
FONT_ICONS = r"C:\Windows\Fonts\SegoeIcons.ttf"
ICON_HEADPHONES = "\ue7f6"


def make_icon():
    """White Fluent headphones silhouette on transparency - same artwork as the window icon."""
    size = 256
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).text((size / 2, size / 2), ICON_HEADPHONES, font=ImageFont.truetype(FONT_ICONS, 236),
                              fill=255, anchor="mm", stroke_width=4, stroke_fill=255)
    ImageDraw.floodfill(mask, (0, 0), 128)  # fill the enclosed ear cups
    mask = mask.point(lambda v: 0 if v == 128 else 255)
    img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    img.putalpha(mask)
    ICON.parent.mkdir(exist_ok=True)
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main():
    if not (HERE / "filter_curves_data.py").exists():
        subprocess.run([sys.executable, "filter_shapes.py"], cwd=HERE, check=True)
    make_icon()
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "ka11_control.pyw",
        "--name", "KA11-Control",
        "--onefile", "--windowed", "--noconfirm", "--clean",
        "--icon", str(ICON),
        "--hidden-import", "filter_curves_data",
        "--exclude-module", "numpy",  # curves are precomputed; keeps the exe small
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE / "build"),
    ], cwd=HERE, check=True)
    exe = HERE / "dist" / "KA11-Control.exe"
    print(f"\nBuilt {exe} ({exe.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
