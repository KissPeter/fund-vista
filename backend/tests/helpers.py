"""Shared image factories for HTTP tests (Pillow-generated, in-memory)."""

from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageDraw


def png_bytes(width: int = 320, height: int = 240) -> bytes:
    """Deterministic test image: gradient + dark circle + bars (ink for all methods)."""
    img = Image.new("L", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            px[x, y] = int(255 * (x + y) / (width + height))
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        [width * 0.25, height * 0.2, width * 0.75, height * 0.8], fill=30
    )
    draw.rectangle([10, 10, width - 10, 40], fill=200)
    for i in range(5):
        draw.rectangle([10, 60 + i * 20, width - 10, 70 + i * 20], fill=90)
    rgb = img.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, format="PNG")
    return buf.getvalue()


def tiny_png_bytes() -> bytes:
    return png_bytes(20, 20)


def svg_bytes() -> bytes:
    return (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297">'
        b'<path d="M 10 10 L 200 10 L 200 287 L 10 287 Z"/>'
        b'<circle cx="105" cy="150" r="60"/>'
        b"</svg>"
    )


def empty_svg_bytes() -> bytes:
    return b'<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297"></svg>'


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_params(method: str = "hatch") -> dict:
    return {
        "method": method,
        "threshold": 128,
        "blur_radius": 1.0,
        "hatch_pitch_mm": 1.2,
        "contour_simplify": 2,
        "linemerge_tolerance_mm": 0.5,
        "linesimplify_tolerance_mm": 0.1,
        "linesort": True,
        "reloop_tolerance_mm": 0.05,
        "page": {"size": "A4", "orientation": "portrait", "margin_mm": 10},
        "pen": {
            "draw_speed_mm_s": 40,
            "travel_speed_mm_s": 100,
            "pen_lift_s": 0.3,
        },
    }


def upload(http, data: bytes, filename: str = "test.png"):
    return http.post("/v1/images", files={"file": (filename, data, "application/octet-stream")})


# Shared HMAC secret for the session live-server (conftest) and the shop
# integration tests below. Any string works for HMAC; production uses a
# 256-bit value via PENPIXEL_HMAC_SECRET.
TEST_HMAC_SECRET = "test-penpixel-hmac-secret-0123456789abcdef"
