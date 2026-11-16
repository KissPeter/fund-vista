from __future__ import annotations

from PIL import Image

from plotvid import folder


def _red_top_left(path, size):
    img = Image.new("RGB", size, (255, 255, 255))
    img.paste((255, 0, 0), (0, 0, 30, 30))
    img.save(path)
    return path


def test_landscape_photo_turned_for_portrait_frame(tmp_path):
    src = _red_top_left(tmp_path / "land.jpg", (1600, 900))
    out = folder._still(src, (360, 640), tmp_path / "out.jpg")
    with Image.open(out) as img:
        assert img.size == (360, 640)
        # 90 degrees counter-clockwise: the top-left corner goes to bottom-left
        assert img.getpixel((5, 634))[1] < 100 and img.getpixel((5, 5))[1] > 200


def test_portrait_photo_not_turned(tmp_path):
    src = _red_top_left(tmp_path / "port.jpg", (900, 1600))
    out = folder._still(src, (360, 640), tmp_path / "out.jpg")
    with Image.open(out) as img:
        assert img.getpixel((5, 5))[1] < 100  # top-left stays top-left


def test_landscape_frame_keeps_landscape_photo(tmp_path):
    src = _red_top_left(tmp_path / "land.jpg", (1600, 900))
    out = folder._still(src, (640, 360), tmp_path / "out.jpg")
    with Image.open(out) as img:
        assert img.size == (640, 360)
        assert img.getpixel((5, 5))[1] < 100  # not turned


def test_pick_shots_keeps_all_when_within_limit():
    import random
    shots = list(range(3))
    assert folder._pick_shots(shots, 3, random.Random(1)) == shots
    assert folder._pick_shots(shots[:2], 3, random.Random(1)) == [0, 1]


def test_pick_shots_samples_limit_in_order_and_varies():
    import random
    shots = list(range(10))
    rng = random.Random(42)
    picks = [folder._pick_shots(shots, 3, rng) for _ in range(20)]
    assert all(len(p) == 3 and p == sorted(p) and len(set(p)) == 3 for p in picks)
    assert len({tuple(p) for p in picks}) > 1
