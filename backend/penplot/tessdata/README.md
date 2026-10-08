# tessdata

`eng.traineddata` — English LSTM model, **tessdata_fast** (4,113,088 bytes), from
the Tesseract project (https://github.com/tesseract-ocr/tessdata_fast),
Apache-2.0. Used by `ocr_text.tesserocr_reader` for the technical-drawing OCR
(digits and `°` only, via a character whitelist). Vendored so hosts that only
install Python dependencies (FastAPI Cloud) need no system package or download.
