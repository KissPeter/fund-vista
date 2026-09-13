"""Pen-plot converter package (API v1).

Design notes (why this layout):
- ``schemas`` owns the wire format (what clients see). Nothing else defines it.
- ``store`` owns bytes on disk + TTL. No image math here.
- ``imaging`` owns pixels (Pillow / NumPy / OpenCV). No HTTP here.
- ``methods`` owns the ``contour | centerline | hatch | flow`` strategies
  behind one protocol, so adding a new method means adding one class + one
  registry line.
- ``optimize`` owns the vpype-equivalent cleanup (linemerge / curvesmooth /
  simplify / sort / reloop / layout) in pure Python — no GPL binaries required (see README note
  in ``methods.py`` about Potrace / flow-imager licensing).
- ``pipeline`` orchestrates the stages with per-stage timing logs so a slow
  convert is easy to debug from logs alone.
- ``router`` is thin HTTP glue: validate -> call pipeline -> return schema.
"""

from backend.penplot.pipeline import ConvertResult, run_convert  # noqa: F401
