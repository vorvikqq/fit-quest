from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from bot.config import PROJECT_ROOT  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "prepare_images", PROJECT_ROOT / "scripts" / "prepare_images.py"
)
prepare_images = importlib.util.module_from_spec(_spec)
sys.modules["prepare_images"] = prepare_images
_spec.loader.exec_module(prepare_images)


def make(path: Path, size: tuple[int, int], mode="RGB", color=(200, 30, 30)):
    Image.new(mode, size, color).save(path)


@pytest.fixture
def images(tmp_path):
    src = tmp_path / "images"
    src.mkdir()
    return src


def test_regular_image_is_downscaled_to_1280_keeping_ratio(images):
    make(images / "r5_prompt_01.png", (2560, 1280))
    (r,) = prepare_images.prepare(images)
    out = Image.open(r.output)
    assert out.size == (1280, 640) and out.format == "JPEG"
    assert not r.uniform and r.output.name == "r5_prompt_01.jpg"
    assert (images / "r5_prompt_01.png").exists()  # оригінал лишається


def test_small_regular_image_is_not_upscaled(images):
    make(images / "x.png", (400, 300))
    (r,) = prepare_images.prepare(images)
    assert Image.open(r.output).size == (400, 300)


def test_photo_round_images_get_identical_size_and_ratio(images):
    """Справжні фото й ШІ-картинки мають різні вихідні розміри: після обробки відрізнити не можна."""
    make(images / "r5_photo_01.png", (1000, 900))  # майже квадрат
    make(images / "r5_photo_02.png", (1024, 572))  # 16:9, як ШІ-картинки: збільшення лише 1,25×
    make(images / "r5_photo_03.png", (388, 257))  # крихітне
    make(images / "r5_photo_04.png", (3000, 1000))  # дуже широке
    results = prepare_images.prepare(images)
    sizes = {Image.open(r.output).size for r in results}
    assert sizes == {(1280, 720)}
    by_name = {r.source.name: r for r in results}
    assert by_name["r5_photo_01.png"].cropped_pct > 30  # довелося обрізати багато
    assert by_name["r5_photo_03.png"].upscaled  # попередження про слабку якість
    assert not by_name["r5_photo_02.png"].upscaled and by_name["r5_photo_02.png"].cropped_pct <= 1


def test_center_crop_keeps_the_middle(images):
    img = Image.new("RGB", (1600, 900), (0, 0, 255))
    for x in range(700, 900):  # червона смуга по центру
        for y in range(900):
            img.putpixel((x, y), (255, 0, 0))
    img = img.resize((1600, 900))
    wide = Image.new("RGB", (2400, 900), (0, 0, 255))
    wide.paste(img.crop((700, 0, 900, 900)), (1100, 0))  # червоне посередині широкого кадру
    wide.save(images / "r5_photo_01.png")
    (r,) = prepare_images.prepare(images)
    out = Image.open(r.output)
    assert out.getpixel((640, 360))[0] > 200  # центр залишився червоним


def test_transparency_is_flattened_on_white(images):
    make(images / "a.png", (100, 100), mode="RGBA", color=(0, 0, 0, 0))
    (r,) = prepare_images.prepare(images)
    assert Image.open(r.output).getpixel((50, 50)) == (255, 255, 255)


def test_non_image_files_and_existing_jpg_are_handled(images):
    (images / "notes.txt").write_text("x")
    make(images / "b.jpg", (100, 100))
    results = prepare_images.prepare(images)
    assert [r.output.name for r in results] == ["b_prepared.jpg"]  # не переписуємо оригінал
    assert Image.open(images / "b.jpg").size == (100, 100)


def test_update_yaml_replaces_only_converted_paths(images, tmp_path):
    make(images / "r5_photo_01.png", (800, 600))
    make(images / "r5_photo_011.png", (800, 600))
    yaml_path = tmp_path / "quest.yaml"
    yaml_path.write_text(
        "image: images/r5_photo_01.png    # коментар\nimage: images/r5_photo_011.png\nimage: images/other.png\n",
        encoding="utf-8",
    )
    results = prepare_images.prepare(images)
    n = prepare_images.update_yaml(yaml_path, results, tmp_path)
    text = yaml_path.read_text(encoding="utf-8")
    assert n == 2
    assert "images/r5_photo_01.jpg    # коментар" in text and "images/r5_photo_011.jpg" in text
    assert "images/other.png" in text  # чужі шляхи не зачеплено


def test_cli_runs_and_reports(images, capsys, tmp_path):
    make(images / "r5_photo_01.png", (388, 257))
    code = prepare_images.main(["--src", str(images)])
    out = capsys.readouterr().out
    assert code == 0 and "r5_photo_01.png: 388x257 -> r5_photo_01.jpg 1280x720" in out
    assert "УВАГА" in out and "єдиний формат" in out


def test_cli_missing_directory(tmp_path, capsys):
    assert prepare_images.main(["--src", str(tmp_path / "nope")]) == 1


def test_real_images_convert_to_uniform_photo_set(tmp_path):
    """Реальні вихідники проєкту: фото раунду 5.2 стають однаковими, кількість збігається."""
    src = tmp_path / "images"
    src.mkdir()
    for p in (PROJECT_ROOT / "content" / "images").glob("*.png"):
        (src / p.name).write_bytes(p.read_bytes())
    results = prepare_images.prepare(src)
    assert len(results) == 10
    photos = [r for r in results if r.uniform]
    assert len(photos) == 5 and {Image.open(r.output).size for r in photos} == {(1280, 720)}
    assert all(r.output.stat().st_size < 600_000 for r in results)  # JPEG легший за PNG
