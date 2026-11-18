"""Нормалізація картинок квесту (DESIGN §8, NFR-6).

* усе перетворюється на JPEG, ширина 1280 px, якість 85;
* картинки раунду «Справжнє чи ШІ» (за префіксом імені) приводяться до **однакового** розміру
  й співвідношення сторін (центр-кроп до 16:9 і 1280×720), щоб розмір чи якість не видавали
  відповідь;
* файли пишуться поруч з оригіналами з розширенням `.jpg`; оригінали не чіпаємо.

Запуск:
    pip install pillow
    python scripts/prepare_images.py                # звіт і конвертація
    python scripts/prepare_images.py --update-yaml  # + замінити .png → .jpg у quest.yaml
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
WIDTH = 1280
UNIFORM_RATIO = (16, 9)
UNIFORM_SIZE = (WIDTH, WIDTH * UNIFORM_RATIO[1] // UNIFORM_RATIO[0])  # 1280x720
QUALITY = 85
UPSCALE_WARN_FACTOR = 1.5  # попереджаємо, лише якщо картинку доводиться збільшувати сильніше
DEFAULT_UNIFORM_PREFIX = "r5_photo_"
SOURCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True)
class Result:
    source: Path
    output: Path
    size_in: tuple[int, int]
    size_out: tuple[int, int]
    uniform: bool
    upscaled: bool  # сильне збільшення (>1,5×): якість могла просісти
    cropped_pct: int  # скільки площі обрізано при кропі (0 для звичайних)


def _to_rgb(image: Image.Image) -> Image.Image:
    """JPEG не має прозорості: підкладаємо білий фон."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return image.convert("RGB")


def _center_crop_to_ratio(image: Image.Image, ratio: tuple[int, int]) -> tuple[Image.Image, int]:
    width, height = image.size
    target = ratio[0] / ratio[1]
    if width / height > target:  # зависоке за шириною → обрізаємо боки
        new_width = round(height * target)
        left = (width - new_width) // 2
        box = (left, 0, left + new_width, height)
    else:  # зашироке за висотою → обрізаємо верх і низ
        new_height = round(width / target)
        top = (height - new_height) // 2
        box = (0, top, width, top + new_height)
    cropped = image.crop(box)
    lost = 100 - round(100 * (cropped.width * cropped.height) / (width * height))
    return cropped, lost


def convert(source: Path, output: Path, *, uniform: bool) -> Result:
    with Image.open(source) as opened:
        image = _to_rgb(opened)
    size_in = image.size
    cropped_pct = 0

    if uniform:
        image, cropped_pct = _center_crop_to_ratio(image, UNIFORM_RATIO)
        upscaled = image.width * UPSCALE_WARN_FACTOR < UNIFORM_SIZE[0]
        image = image.resize(UNIFORM_SIZE, Image.Resampling.LANCZOS)
    else:
        upscaled = False
        if image.width > WIDTH:  # лише зменшуємо: збільшувати звичайні картинки немає сенсу
            height = round(image.height * WIDTH / image.width)
            image = image.resize((WIDTH, height), Image.Resampling.LANCZOS)

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "JPEG", quality=QUALITY, optimize=True)
    return Result(source, output, size_in, image.size, uniform, upscaled, cropped_pct)


def prepare(src: Path, uniform_prefix: str = DEFAULT_UNIFORM_PREFIX) -> list[Result]:
    results = []
    for path in sorted(src.iterdir()):
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        output = path.with_suffix(".jpg")
        if output == path:  # вже .jpg: переписувати поверх оригіналу не будемо
            output = path.with_name(path.stem + "_prepared.jpg")
        results.append(convert(path, output, uniform=path.name.startswith(uniform_prefix)))
    return results


def update_yaml(yaml_path: Path, results: list[Result], content_root: Path) -> int:
    """Замінює `images/x.png` на `images/x.jpg` для сконвертованих файлів. Повертає кількість замін."""
    text = yaml_path.read_text(encoding="utf-8")
    replaced = 0
    for r in results:
        old = r.source.relative_to(content_root).as_posix()
        new = r.output.relative_to(content_root).as_posix()
        if old == new:
            continue
        text, n = re.subn(rf"(?<![\w/]){re.escape(old)}(?![\w.])", new, text)
        replaced += n
    yaml_path.write_text(text, encoding="utf-8")
    return replaced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--src", type=Path, default=ROOT / "content" / "images")
    parser.add_argument("--uniform-prefix", default=DEFAULT_UNIFORM_PREFIX)
    parser.add_argument("--yaml", type=Path, default=ROOT / "content" / "quest.yaml")
    parser.add_argument("--update-yaml", action="store_true", help="замінити .png на .jpg у quest.yaml")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # консоль Windows (cp1251) не знає всіх символів
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not args.src.is_dir():
        print(f"Немає каталогу {args.src}", file=sys.stderr)
        return 1
    results = prepare(args.src, args.uniform_prefix)
    for r in results:
        kind = "єдиний формат" if r.uniform else "звичайна"
        note = ""
        if r.cropped_pct:
            note += f", обрізано {r.cropped_pct}% площі"
        if r.upscaled:
            note += ", УВАГА: джерело менше за цільовий розмір, якість нижча (перевір на око)"
        print(f"{r.source.name}: {r.size_in[0]}x{r.size_in[1]} -> {r.output.name} "
              f"{r.size_out[0]}x{r.size_out[1]} ({kind}{note})")  # fmt: skip
    if not results:
        print("Картинок не знайдено")
    if args.update_yaml:
        n = update_yaml(args.yaml, results, args.src.parent)
        print(f"{args.yaml.name}: замінено шляхів {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
