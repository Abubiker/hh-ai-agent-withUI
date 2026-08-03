"""Генерирует иконку приложения (.icns) из исходного логотипа (assets/logo.png).

Раньше иконка рисовалась кодом (скруглённый квадрат + галочка), чтобы не
тащить бинарь в репозиторий. Логотип — готовый дизайн (wordmark "AbuHH" на
градиентном фоне, широкий прямоугольник, не квадрат), нарисовать его кодом
нельзя — исходный PNG теперь лежит в assets/ и коммитится как есть. Сюда, в
build_assets/, попадают только сгенерированные .icns/.iconset — сама папка
build_assets/ по-прежнему в .gitignore как результат сборки.

Наивная вставка логотипа целиком (с его собственным фоном) поверх любого
нового квадратного фона даёт заметный шов на границе прямоугольника — два
градиента стыкуются под разными углами. Поэтому здесь буквы вырезаются из
логотипа по цвету (чёрный текст + красный текст, оба контрастны на светлом
фоне), а от исходного фона остаётся только палитра — на неё строится чистый
диагональный градиент под квадратный холст, и буквы садятся на него без шва.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).parent
SOURCE = ROOT / "assets" / "logo.png"
OUT = ROOT / "build_assets"

CORNER_PATCH = 24  # пикселей — усредняем patch угла, а не берём один пиксель


def _corner_color(arr: np.ndarray, x0: int, y0: int) -> tuple[int, int, int]:
    patch = arr[y0:y0 + CORNER_PATCH, x0:x0 + CORNER_PATCH].reshape(-1, 3)
    return tuple(int(v) for v in patch.mean(axis=0))


def _corner_gradient(size: int, tl, tr, bl, br) -> Image.Image:
    small = 64
    v = np.linspace(0, 1, small).reshape(small, 1, 1)
    u = np.linspace(0, 1, small).reshape(1, small, 1)
    tl_a, tr_a, bl_a, br_a = (np.array(c, dtype=float) for c in (tl, tr, bl, br))
    left = tl_a + (bl_a - tl_a) * v
    right = tr_a + (br_a - tr_a) * v
    grad = left + (right - left) * u
    img = Image.fromarray(grad.round().astype(np.uint8), "RGB")
    return img.resize((size, size), Image.BILINEAR)


def _cutout_letters(src: Image.Image) -> Image.Image:
    """Вырезает текст логотипа (чёрные и красные буквы) в RGBA с прозрачным
    фоном — по контрасту с бледным градиентным фоном самого исходника."""
    arr = np.asarray(src, dtype=np.int16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]

    # Чёрный текст: тёмный по всем каналам. Плавный порог 0..120 — мягкий
    # край на антиалиасинге исходника, а не рубленая маска.
    dark = np.clip((120 - np.maximum(np.maximum(r, g), b)) / 120, 0, 1)

    # Красный текст: R сильно выше G и B. Порог высокий и намеренно: тёплые
    # розовые участки самого градиентного фона (верхний правый угол) дают
    # r-g/r-b порядка 60-95 — почти как у текста, но не дотягивают до
    # настоящего красного (190+). Ниже порога — просто фон, не текст.
    redness = np.minimum(r - g, r - b)
    red = np.clip((redness - 140) / 40, 0, 1)

    alpha = np.clip(np.maximum(dark, red), 0, 1)
    out = np.dstack([arr[..., :3].astype(np.uint8), (alpha * 255).astype(np.uint8)])
    return Image.fromarray(out, "RGBA")


def draw(size: int, letters: Image.Image, bg_colors) -> Image.Image:
    s = size / 1024
    tl, tr, bl, br = bg_colors
    bg = _corner_gradient(size, tl, tr, bl, br)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size, size], radius=225 * s, fill=255)
    canvas.paste(bg.convert("RGBA"), (0, 0), mask)

    # Буквы — без искажений, вписаны с равными отступами со всех сторон.
    pad = 150 * s
    max_w, max_h = size - 2 * pad, size - 2 * pad
    scale = min(max_w / letters.width, max_h / letters.height)
    fg = letters.resize((round(letters.width * scale), round(letters.height * scale)),
                        Image.LANCZOS)
    canvas.alpha_composite(fg, ((size - fg.width) // 2, (size - fg.height) // 2))
    return canvas


def main():
    if not SOURCE.exists():
        print(f"❌ Не найден исходный логотип: {SOURCE}")
        return 1

    src = Image.open(SOURCE).convert("RGB")
    arr = np.asarray(src)
    w, h = src.size
    bg_colors = (
        _corner_color(arr, 0, 0),
        _corner_color(arr, w - CORNER_PATCH, 0),
        _corner_color(arr, 0, h - CORNER_PATCH),
        _corner_color(arr, w - CORNER_PATCH, h - CORNER_PATCH),
    )
    letters = _cutout_letters(src)

    OUT.mkdir(exist_ok=True)
    iconset = OUT / "icon.iconset"
    iconset.mkdir(exist_ok=True)

    # Набор размеров, который требует iconutil
    for base in (16, 32, 128, 256, 512):
        draw(base, letters, bg_colors).save(iconset / f"icon_{base}x{base}.png")
        draw(base * 2, letters, bg_colors).save(iconset / f"icon_{base}x{base}@2x.png")

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
