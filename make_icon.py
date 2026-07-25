"""Генерирует иконку приложения (.icns) из кода — чтобы не тащить бинарь в репозиторий."""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "build_assets"


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 1024

    # Скруглённый квадрат в стиле macOS
    pad, radius = 92 * s, 225 * s
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=radius,
                        fill=(10, 132, 255, 255))

    # Стилизованная галочка «отклик отправлен»
    w = 62 * s
    d.line([(340 * s, 520 * s), (455 * s, 640 * s), (690 * s, 390 * s)],
           fill=(255, 255, 255, 255), width=int(w), joint="curve")
    return img


def main():
    OUT.mkdir(exist_ok=True)
    iconset = OUT / "icon.iconset"
    iconset.mkdir(exist_ok=True)

    # Набор размеров, который требует iconutil
    for base in (16, 32, 128, 256, 512):
        draw(base).save(iconset / f"icon_{base}x{base}.png")
        draw(base * 2).save(iconset / f"icon_{base}x{base}@2x.png")

    icns = OUT / "icon.icns"
    try:
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                       check=True)
        print(f"✅ Иконка собрана: {icns}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"⚠️ iconutil недоступен ({e}); оставляю только PNG в {iconset}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
