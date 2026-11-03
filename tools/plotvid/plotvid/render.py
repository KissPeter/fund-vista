"""Render a clip plan to a 9:16 H.264/AAC MP4 with one ffmpeg call.

A clip is made of parts:
- the plotting segments, each with the plotter's own sound;
- the final-plot photo, silent;
- the outro, which is an image (silent) or a video (with its own audio,
  silence if it has none).

Each part is normalised inside one ``filter_complex``: the same size, frame
rate, pixel format, SAR, and 48 kHz stereo audio. The parts are then
joined with ``concat`` (``transition: cut``) or an ``xfade`` +
``acrossfade`` chain (``crossfade``). One encode avoids the AAC
priming gaps of stitching separately encoded files.

Geometry: the output is the largest 9:16 crop of the source at native
resolution, with no upscaling. A source that is too tall loses equal
bands at top and bottom; one that is too wide loses equal bands left and
right. With several sources, the smallest crop wins and larger sources are
scaled down to it. Images and outro videos are scaled to cover the output,
then centre-cropped.

Every segment gets a short audio fade (``audio_fade_s``, 20 ms) at both
ends, so cuts do not click.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from plotvid.config import IMAGE_EXTS, RenderConfig

AUDIO_RATE = 48000


def crop_9_16(w: int, h: int) -> tuple[int, int, int, int]:
    """Largest centred 9:16 crop with even sides: (cw, ch, x, y)."""
    if w * 16 > h * 9:  # too wide: keep full height
        ch = h - h % 2
        cw = min(w, ch * 9 // 16)
        cw -= cw % 2
    else:  # too tall (or exact): keep full width
        cw = w - w % 2
        ch = min(h, cw * 16 // 9)
        ch -= ch % 2
    return cw, ch, (w - cw) // 2, (h - ch) // 2


_STANDARD_FPS = (23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94, 60.0, 120.0)


def nice_fps(fps: float) -> float:
    """Snap a phone's variable average rate (vid2: 30.013) to the standard rate
    it was recorded at, if one is within 0.5%. Otherwise keep it as is."""
    best = min(_STANDARD_FPS, key=lambda r: abs(r - fps))
    return best if abs(best - fps) <= 0.005 * best else fps


def output_size(display_sizes: list[tuple[int, int]]) -> tuple[int, int]:
    """Smallest 9:16 crop over all sources; never upscales any of them."""
    crops = [crop_9_16(w, h)[:2] for w, h in display_sizes]
    return min(crops, key=lambda c: c[0])


@dataclass(frozen=True)
class Part:
    kind: str  # "segment" | "image" | "video"
    path: Path
    duration_s: float
    start_s: float = 0.0  # segments and videos: source in-point
    display_size: tuple[int, int] | None = None  # segments: autorotated size
    has_audio: bool = True
    fade_audio: bool = False


def _probe_video(path: Path) -> tuple[float, bool]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)], check=True, capture_output=True, text=True).stdout
    data = json.loads(out)
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return float(data.get("format", {}).get("duration") or 0.0), has_audio


def asset_part(path: Path, seconds: float | None) -> Part:
    if path.suffix.lower() in IMAGE_EXTS:
        if seconds is None:
            raise ValueError(f"{path}: an image needs seconds")
        return Part("image", path, seconds, has_audio=False)
    length, has_audio = _probe_video(path)
    return Part("video", path, min(seconds, length) if seconds else length, has_audio=has_audio)


def _video_chain(p: Part, size: tuple[int, int], fps: float) -> str:
    w, h = size
    if p.kind == "segment":
        assert p.display_size is not None
        cw, ch, x, y = crop_9_16(*p.display_size)
        geo = f"crop={cw}:{ch}:{x}:{y}"
        if (cw, ch) != (w, h):
            geo += f",scale={w}:{h}:flags=lanczos"
    else:
        geo = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
               f"crop={w}:{h}")
    return (f"{geo},fps={fps},format=yuv420p,setsar=1,"
            f"trim=duration={p.duration_s:.6f},setpts=PTS-STARTPTS")


def _audio_chain(p: Part, fade_s: float) -> str:
    chain = (f"aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
             f"apad,atrim=duration={p.duration_s:.6f},asetpts=PTS-STARTPTS")
    if p.fade_audio and fade_s > 0:
        st = max(0.0, p.duration_s - fade_s)
        chain += f",afade=t=in:d={fade_s:.4f},afade=t=out:st={st:.6f}:d={fade_s:.4f}"
    return chain


def build_command(parts: list[Part], size: tuple[int, int], fps: float, cfg: RenderConfig,
                  out: Path) -> list[str]:
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    filters: list[str] = []
    n_inputs = 0
    labels: list[tuple[str, str]] = []
    for k, p in enumerate(parts):
        vi = n_inputs
        if p.kind == "segment":
            cmd += ["-ss", f"{p.start_s:.6f}", "-t", f"{p.duration_s + 0.5:.6f}", "-i", str(p.path)]
        elif p.kind == "image":
            cmd += ["-loop", "1", "-framerate", f"{fps}", "-t", f"{p.duration_s + 0.5:.6f}",
                    "-i", str(p.path)]
        else:
            cmd += ["-ss", f"{p.start_s:.6f}", "-t", f"{p.duration_s + 0.5:.6f}", "-i", str(p.path)]
        n_inputs += 1
        if p.has_audio:
            ai = f"{vi}:a:0"
        else:
            cmd += ["-f", "lavfi", "-t", f"{p.duration_s + 0.5:.6f}",
                    "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
            ai = f"{n_inputs}:a:0"
            n_inputs += 1
        filters.append(f"[{vi}:v:0]{_video_chain(p, size, fps)}[v{k}]")
        filters.append(f"[{ai}]{_audio_chain(p, cfg.audio_fade_s)}[a{k}]")
        labels.append((f"v{k}", f"a{k}"))

    if cfg.transition == "cut" or len(parts) == 1:
        joined = "".join(f"[{v}][{a}]" for v, a in labels)
        filters.append(f"{joined}concat=n={len(parts)}:v=1:a=1[vout][aout]")
    else:
        x = cfg.crossfade_s
        v_prev, a_prev = labels[0]
        elapsed = parts[0].duration_s
        for k in range(1, len(parts)):
            v_next, a_next = labels[k]
            offset = elapsed - x
            vo, ao = ("vout", "aout") if k == len(parts) - 1 else (f"vx{k}", f"ax{k}")
            filters.append(f"[{v_prev}][{v_next}]xfade=transition={cfg.crossfade_type}:duration={x:.4f}:"
                           f"offset={offset:.6f}[{vo}]")
            filters.append(f"[{a_prev}][{a_next}]acrossfade=d={x:.4f}[{ao}]")
            v_prev, a_prev = vo, ao
            elapsed = offset + parts[k].duration_s
    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-pix_fmt", "yuv420p", "-r", f"{fps}",
            "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", str(AUDIO_RATE), "-ac", "2",
            "-movflags", "+faststart", str(out)]
    return cmd


def expected_duration(parts: list[Part], cfg: RenderConfig) -> float:
    total = sum(p.duration_s for p in parts)
    if cfg.transition == "crossfade":
        total -= cfg.crossfade_s * (len(parts) - 1)
    return total


def render(parts: list[Part], size: tuple[int, int], fps: float, cfg: RenderConfig,
           out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(build_command(parts, size, fps, cfg, out), capture_output=True, text=True,
                          check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {out.name}:\n{proc.stderr[-2000:]}")


def extract_frame(path: Path, time_s: float, out: Path) -> None:
    """Full-resolution, autorotated still from a video."""
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{time_s:.3f}",
                    "-i", str(path), "-frames:v", "1", str(out)], check=True, capture_output=True)


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for name in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _centred(draw: ImageDraw.ImageDraw, text: str, y: int, width: int, font, fill) -> int:
    """Draw ``text`` centred, wrapped to ~85% of ``width``; returns the next y."""
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width * 0.85 or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    for ln in lines:
        box = draw.textbbox((0, 0), ln, font=font)
        draw.text(((width - (box[2] - box[0])) / 2, y), ln, font=font, fill=fill)
        y += int((box[3] - box[1]) * 1.4)
    return y


def outro_card(size: tuple[int, int], title: str, text: str, out: Path) -> None:
    """Placeholder outro: plot name and a line of text on a dark card."""
    w, h = size
    img = Image.new("RGB", (w, h), (18, 18, 20))
    draw = ImageDraw.Draw(img)
    big, small = _font(max(16, w // 9)), _font(max(12, w // 18))
    y = _centred(draw, title, int(h * 0.42), w, big, (245, 245, 240))
    draw.line([(w * 0.35, y + h * 0.01), (w * 0.65, y + h * 0.01)], fill=(200, 140, 60),
              width=max(2, w // 200))
    _centred(draw, text, int(y + h * 0.03), w, small, (180, 180, 175))
    img.save(out)
