"""PCB Gerber preview (``/v1/pcb/preview``): one uploaded file to an SVG.

Stage 1 minimal slice: a single Gerber (RS-274X) or Excellon drill file is
parsed with ``gerbonara`` and rendered as black artwork. Preview only: no
layer splitting, inks, outline conversion, A4 fitting or stored results.
"""
