"""PCB Gerber kind (``/v1/pcb``): one uploaded Gerber or Excellon file.

- ``POST /v1/pcb/preview`` returns the file as a black SVG (``render.py``).
- ``POST /v1/pcb/render`` plots the enabled sub-layers (pads, tracks, pours,
  drills) on one A4 sheet, outline only, each in its ink (``layers.py``,
  ``sheet.py``, ``router.py``).
- The ink split accepts kind ``pcb`` (``backend/fulfillment``).

Parsing runs in a child process killed at the timeout (``render.run_isolated``).
"""
