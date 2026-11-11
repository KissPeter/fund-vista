"""Animated intro overlay and fading logo on a finished clip.

The intro SVG (a transparent 1080x1920 overlay with CSS keyframes) shows from
``START_S`` to ``END_S``: fade in and slide up, hold, fade out. librsvg and
ffmpeg do not play CSS animation, so the same motion is applied frame by frame
to the SVG (opacity, ``translateY``, the underline's ``stroke-dashoffset``) and
the frames are rendered with librsvg. The logo (bottom right) stays away while
the intro shows, fades in as it leaves, and is hidden again for the outro.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

START_S, END_S = 0.4, 4.0  # intro on screen
FADE_IN_S = FADE_OUT_S = 0.3
UNDERLINE_FROM_S, UNDERLINE_TO_S = 0.3, 1.0  # draw time, from the intro's start
LOGO_FADE_S = 0.9  # starts FADE_OUT_S before the intro ends
FRAME_FPS = 30
LOGO_WIDTH_FRAC, LOGO_MARGIN_FRAC = 0.2, 0.04  # as in render.RenderConfig


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def _static_svg(text: str) -> str:
    """The SVG without its CSS animation."""
    text = re.sub(r"#ov\{animation[^}]*\}\s*#ul\{animation[^}]*\}", "", text)
    text = re.sub(r"@keyframes[^\n]*\n", "", text)
    return re.sub(r"/\* 2\.2.*?\*/", "", text, flags=re.S)


def _frame_svg(static: str, t: float) -> str:
    """The static SVG at ``t`` seconds into the intro."""
    length = END_S - START_S
    if t < FADE_IN_S:
        k = _ease(t / FADE_IN_S)
        opacity, dy = k, 40 * (1 - k)
    elif t <= length - FADE_OUT_S:
        opacity, dy = 1.0, 0.0
    else:
        k = _ease((t - (length - FADE_OUT_S)) / FADE_OUT_S)
        opacity, dy = 1 - k, -20 * k
    drawn = (t - UNDERLINE_FROM_S) / (UNDERLINE_TO_S - UNDERLINE_FROM_S)
    offset = 1.0 - min(max(drawn, 0.0), 1.0)
    out = static.replace('<g id="ov">',
                         f'<g id="ov" opacity="{opacity:.3f}" transform="translate(0 {dy:.2f})">')
    return out.replace('pathLength="1" fill="none"',
                       f'pathLength="1" stroke-dasharray="1" stroke-dashoffset="{offset:.3f}" '
                       'fill="none"')


def add_intro(svg: Path, logo: Path | None, outro_s: float, src: Path, dst: Path) -> Path:
    """``src`` with the intro overlay and the fading logo, written to ``dst``.
    ``outro_s`` is the outro's length: the logo is hidden for it. Audio is copied."""
    probe = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(src)]))
    width = probe["streams"][0]["width"]
    num, den = map(int, probe["streams"][0]["r_frame_rate"].split("/"))
    fps, duration = num / den, float(probe["format"]["duration"])
    static = _static_svg(svg.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="plotvid_intro_") as tmp_name:
        tmp = Path(tmp_name)
        for i in range(round((END_S - START_S) * FRAME_FPS)):
            (tmp / "f.svg").write_text(_frame_svg(static, i / FRAME_FPS), encoding="utf-8")
            subprocess.run(["rsvg-convert", "-w", str(width), "-o", str(tmp / f"f{i:03d}.png"),
                            str(tmp / "f.svg")], check=True)
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src),
               "-framerate", str(FRAME_FPS), "-i", str(tmp / "f%03d.png")]
        fc = (f"[0:v]fps={fps}[b];[1:v]setpts=PTS+{START_S}/TB[o];"
              "[b][o]overlay=eof_action=pass:format=auto[bo]")
        if logo is not None:
            subprocess.run(["rsvg-convert", "-w", str(round(width * LOGO_WIDTH_FRAC)), "-f", "png",
                            "-o", str(tmp / "logo.png"), str(logo)], check=True)
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{duration + 0.5:.3f}",
                    "-i", str(tmp / "logo.png")]
            margin = round(width * LOGO_MARGIN_FRAC)
            fc += (f";[2:v]format=rgba,fade=t=in:st={END_S - FADE_OUT_S}:d={LOGO_FADE_S}:alpha=1[w];"
                   f"[bo][w]overlay=x=W-w-{margin}:y=H-h-{margin}:"
                   f"enable='lt(t,{duration - outro_s:.3f})':shortest=1[v]")
        else:
            fc = fc.replace("[bo]", "[v]")
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
                "-c:a", "copy", str(dst)]
        subprocess.run(cmd, check=True)
    return dst
