"""Third-party licence inventory: every vendored item has a licence text or a
documented origin, and THIRD_PARTY_NOTICES covers deps + copyright lines.

Issue #58 (3/7). Pure filesystem/content assertions — no server, no image
math. Fails if a font/background/notice file is added without its licence
paperwork (that is the point: the test is the checklist).
"""

from __future__ import annotations

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel: str) -> str:
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _exists(rel: str) -> str:
    path = os.path.join(REPO_ROOT, rel)
    assert os.path.isfile(path), f"missing vendored file: {rel}"
    return path


# --- font binaries -----------------------------------------------------------

PENPLOT_FONTS = (
    "Znikoslsvginot8-GOB3y.ttf",
    "Excalifont-Regular.ttf",
    "ComicShanns-Regular.ttf",
    "Nunito-Regular.ttf",
)

WORDMARK_FONTS = ("Poppins-Bold.ttf",)


def test_penplot_font_binaries_present():
    for name in PENPLOT_FONTS:
        _exists(f"backend/penplot/fonts/{name}")


def test_wordmark_font_binaries_present():
    for name in WORDMARK_FONTS:
        _exists(f"backend/wordmark/fonts/{name}")


# --- licence texts next to the fonts -----------------------------------------

def test_ofl_text_vendored_in_both_font_dirs():
    for rel in ("backend/penplot/fonts/OFL-1.1.txt", "backend/wordmark/fonts/OFL-1.1.txt"):
        text = _read(rel)
        assert "SIL OPEN FONT LICENSE" in text
        assert "Version 1.1" in text
        assert "26 February 2007" in text
        assert "AS IS" in text


def test_mit_text_vendored_for_comic_shanns():
    # Comic Shanns is the only non-OFL face: its MIT grant must be on disk.
    text = _read("backend/penplot/fonts/MIT.txt")
    assert "MIT License" in text
    assert "Shannon Miwa" in text
    assert "Permission is hereby granted" in text


def test_penplot_fonts_readme_documents_every_face():
    text = _read("backend/penplot/fonts/README.md")
    for name in PENPLOT_FONTS:
        assert name in text, name
    assert "OFL-1.1.txt" in text
    # The non-OFL exception must be called out explicitly (issue #58).
    assert "Comic Shanns" in text and "NOT OFL" in text
    assert "MIT.txt" in text
    assert "Excalidraw" in text  # Excalifont provenance
    assert "gluk" in text  # ZnikoSL provenance


def test_wordmark_fonts_readme_documents_poppins():
    text = _read("backend/wordmark/fonts/README.md")
    assert "Poppins-Bold.ttf" in text
    assert "Poppins Project Authors" in text
    assert "OFL-1.1.txt" in text


# --- central inventory ---------------------------------------------------------

def test_vendored_inventory_covers_fonts_backgrounds_hershey_tessdata():
    text = _read("backend/THIRD_PARTY_VENDORED.md")
    for name in (*PENPLOT_FONTS, *WORDMARK_FONTS):
        assert name in text, name
    # Textures: origin recorded, unknown licence flagged (replacement undecided).
    assert "dark-texture.jpg" in text and "light-texture.jpg" in text
    assert "UNKNOWN" in text
    # Hershey sources collected from the code comments.
    assert "python-hershey" in text
    assert "hersheytextjs" in text
    assert "Hershey" in text
    # Tessdata model.
    assert "eng.traineddata" in text
    assert "tessdata_fast" in text
    assert "Apache" in text
    # D1-D4 recorded, Potrace/vpype named as server-side-only.
    assert "Potrace" in text and "vpype" in text


# --- THIRD_PARTY_NOTICES --------------------------------------------------------

EXPECTED_DEP_NAMES = (
    "fastapi", "uvicorn", "httpx", "brotli", "redis",
    "pydantic", "pillow", "numpy", "opencv", "python-multipart",
    "defusedxml", "fonttools", "skia-pathops", "jinja2",
    "tesserocr", "pytesseract", "pytest",
)

EXPECTED_COPYRIGHTS = (
    "gluk",
    "Excalidraw",
    "Shannon Miwa",
    "Nunito",
    "Poppins",
)


def test_notices_file_exists_and_covers_dependencies():
    text = _read("backend/THIRD_PARTY_NOTICES.md")
    for dep in EXPECTED_DEP_NAMES:
        assert dep in text.lower(), dep
    for holder in EXPECTED_COPYRIGHTS:
        assert holder in text, holder
    # Non-OFL exception + unknown-texture flag must survive in the notices.
    assert "NOT OFL" in text or "non-OFL" in text
    assert "UNKNOWN" in text
    # D1 exception documented as excluded-by-design, not as a dependency.
    assert "Potrace" in text


def test_code_attribution_constants_agree_with_docs():
    from backend.penplot import labels
    from backend.penplot import svgfont
    from backend.wordmark import schemas as wordmark_schemas

    assert "OFL" in labels.ATTRIBUTION_OUTLINE
    assert "MIT" in labels.ATTRIBUTION_OUTLINE  # Comic Shanns callout
    assert "OFL" in svgfont.ATTRIBUTION_SINGLESTROKE
    assert "OFL" in wordmark_schemas.ATTRIBUTION
    notices = _read("backend/THIRD_PARTY_NOTICES.md")
    assert "Shannon Miwa" in notices
    assert "Shannon Miwa" in labels.ATTRIBUTION_OUTLINE


# --- backgrounds on disk match the registry ------------------------------------

def test_background_registry_files_exist_on_disk():
    from backend.penplot import backgrounds

    for bid, entry in backgrounds.BACKGROUNDS.items():
        if not entry["filename"]:
            continue
        _exists(f"backend/penplot/backgrounds/{entry['filename']}")
        assert entry["source"].startswith("https://"), bid
