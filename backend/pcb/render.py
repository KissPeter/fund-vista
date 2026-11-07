"""Gerber/Excellon bytes to a black-on-white SVG preview.

Parsing runs in a spawned child process (never in the server process): the
parent sniffs the header, the child parses on a temp file (never a path
derived from the upload name) that is removed in ``finally``, and the parent
kills the child when it runs past ``PARSE_TIMEOUT_S``, so a hostile file
cannot keep a thread busy. ``<image>`` and ``<text>`` elements are stripped
from the output so the preview carries only strokes and fills.
"""

from __future__ import annotations

import asyncio
import importlib
import multiprocessing
import os
import tempfile
import xml.etree.ElementTree as ET

from defusedxml import ElementTree as DefusedET
from gerbonara import ExcellonFile, GerberFile

from backend.penplot.errors import ErrorCode, PenPlotError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
SNIFF_BYTES = 2048
PARSE_TIMEOUT_S = 8.0

SVG_NS = "http://www.w3.org/2000/svg"
# Markers from the first 2 KB: RS-274X (%FS, G04, %MO, G75/G01) or Excellon (M48).
_GERBER_MARKERS = ("%FS", "G04", "%MO", "G75", "G01")
_EXCELLON_MARKER = "M48"
_STRIPPED_TAGS = {"image", "text"}

# Spawn (not fork): the server process runs threads, and a fresh interpreter
# shares no locks or sockets with it.
_CONTEXT = multiprocessing.get_context("spawn")


def _sniff(data: bytes) -> str:
    """Return ``"gerber"`` or ``"excellon"``; raise 422 otherwise."""
    if not data:
        raise PenPlotError(
            status=422, code=ErrorCode.INVALID_PARAMS, message="The uploaded file is empty."
        )
    head = data[:SNIFF_BYTES].decode("utf-8", errors="replace")
    if any(marker in head for marker in _GERBER_MARKERS):
        return "gerber"
    if _EXCELLON_MARKER in head:
        return "excellon"
    raise PenPlotError(
        status=422,
        code=ErrorCode.INVALID_PARAMS,
        message="The file does not look like a Gerber or Excellon drill file.",
    )


def _strip_unsafe(svg: str) -> str:
    """Drop ``<image>`` and ``<text>`` elements from the rendered SVG."""
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    root = DefusedET.fromstring(svg)
    for parent in root.iter():
        for child in list(parent):
            if child.tag.rsplit("}", 1)[-1] in _STRIPPED_TAGS:
                parent.remove(child)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def _render_sync(data: bytes) -> str:
    kind = _sniff(data)
    fd, path = tempfile.mkstemp(suffix=".gbr")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if kind == "excellon":
            svg = str(ExcellonFile.open(path).to_svg(fg="black", bg="white"))
        else:
            svg = str(GerberFile.open(path).to_svg(fg="black", bg="white"))
    finally:
        os.unlink(path)
    return _strip_unsafe(svg)


def _child_main(conn, target: str, data: bytes) -> None:
    """Child entry point: resolve ``module:function`` and send its result."""
    try:
        module_name, _, func_name = target.partition(":")
        func = getattr(importlib.import_module(module_name), func_name)
        message = ("ok", func(data))
    except Exception:  # any child failure is a parse failure for the parent
        message = ("error", None)
    try:
        conn.send(message)
    finally:
        conn.close()


def run_isolated(target: str, data: bytes, timeout_s: float = PARSE_TIMEOUT_S):
    """Run ``target`` (``module:function``) on ``data`` in a child process.

    The child is killed when it has not reported within ``timeout_s``. Any
    timeout, crash or exception becomes a 422 ``PenPlotError``.
    """
    reader, writer = _CONTEXT.Pipe(duplex=False)
    proc = _CONTEXT.Process(target=_child_main, args=(writer, target, data), daemon=True)
    try:
        proc.start()
    except Exception as exc:  # a server fault (for example an unguarded entry module), not bad input
        writer.close()
        reader.close()
        raise PenPlotError(
            status=500,
            code=ErrorCode.PROCESSING_FAILED,
            message="The Gerber parser process could not start.",
        ) from exc
    writer.close()
    try:
        if not reader.poll(timeout_s):
            raise PenPlotError(
                status=422,
                code=ErrorCode.INVALID_PARAMS,
                message=f"The Gerber file could not be parsed within {timeout_s:g} seconds.",
            )
        status, payload = reader.recv()
    except (EOFError, OSError):
        status, payload = "error", None
    finally:
        reader.close()
        if proc.is_alive():
            proc.kill()
        proc.join()
    if status != "ok":
        raise PenPlotError(
            status=422,
            code=ErrorCode.INVALID_PARAMS,
            message="The Gerber file could not be parsed.",
        )
    return payload


async def render_preview_svg(data: bytes) -> str:
    """Render ``data`` to SVG; 422 on bad input, parse failure or timeout."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise PenPlotError(
            status=413,
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message="The Gerber file is larger than 5 MB.",
        )
    _sniff(data)
    return await asyncio.to_thread(run_isolated, "backend.pcb.render:_render_sync", data)
