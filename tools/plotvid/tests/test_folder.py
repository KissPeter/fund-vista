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


def test_rotate_off_keeps_whole_landscape_photo(tmp_path):
    src = _red_top_left_landscape(tmp_path / "land.jpg", (1600, 900))
    out = folder._still(src, (360, 640), tmp_path / "out.jpg", "0")
    with Image.open(out) as img:
        assert img.size == (360, 640)
        # the photo is scaled to 360 px wide, centred, so its top-left corner is at y ~219
        assert img.getpixel((2, 222))[1] < 100  # red marker visible: nothing cropped
        assert img.getpixel((2, 5))[1] > 150  # the blurred backdrop above it, not the photo
