from __future__ import annotations

from PIL import Image, ImageOps

from plotvid import folder


def _red_top_left_landscape(path, size):
    img = Image.new("RGB", size, (255, 255, 255))
    img.paste((255, 0, 0), (0, 0, 30, 30))
    img.save(path)  # no EXIF orientation tag, like the Sony files
    return path


def test_untagged_landscape_photo_turned_like_videos(tmp_path):
    src = _red_top_left_landscape(tmp_path / "land.jpg", (1600, 900))
    out = folder._still(src, (360, 640), tmp_path / "out.jpg", "auto")
    with Image.open(out) as img:
        assert img.size == (360, 640)
        # 90 degrees counter-clockwise: the top-left corner goes to bottom-left
        assert img.getpixel((5, 634))[1] < 100 and img.getpixel((5, 5))[1] > 200  # red, then white


def test_rotate_off_keeps_landscape_photo(tmp_path):
    src = _red_top_left_landscape(tmp_path / "land.jpg", (1600, 900))
    out = folder._still(src, (360, 640), tmp_path / "out.jpg", "0")
    with Image.open(src) as img:
        expected = ImageOps.fit(img.convert("RGB"), (360, 640), method=Image.Resampling.LANCZOS)
    with Image.open(out) as img:
        assert img.size == (360, 640)
        assert list(img.getdata()) == list(expected.getdata())  # no turn, just the fit
