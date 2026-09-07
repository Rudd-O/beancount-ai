from typing import Sequence, Tuple

import pymupdf

# Suppress error printouts to stdout — they mess with the JSON LLM output.
pymupdf.TOOLS.mupdf_display_errors(False)  # pyright: ignore[reportUnknownMemberType]

# Max DPI for rendering (capped to avoid gigabytes of RAM from high-DPI scans).
MAX_DPI = 300

# Max pages allowed; PDFs exceeding this raise ValueError.
MAX_PAGES = 25


def render_pdf_pages_to_png(
    pdf_bytes: bytes,
) -> Sequence[Tuple[int, float, bytes]]:
    """Render each page of a PDF to PNG bytes at its native embedded DPI.

    Returns a list of ``(page_number_1-based, dpi_used_as_float, png_bytes)``
    tuples sorted by page number.  When multiple pages or images exist the
    highest DPI found on each individual page is used for that page's render.

    Falls back to 300 DPI for any page where no embedded image is detected and
    raises any exception ``pymupdf.open`` may produce for invalid PDF data.

    Raises
    ------
    ValueError
        If the PDF has more than ``MAX_PAGES`` pages.
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    if len(doc) > MAX_PAGES:
        doc.close()
        raise ValueError(
            f"PDF has {len(doc)} pages (maximum allowed: {MAX_PAGES}). "
            "Please split the PDF into smaller files."
        )

    result: list[Tuple[int, float, bytes]] = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_images = page.get_images(full=True)

        mediabox = page.mediabox
        w_inch = (mediabox[2] - mediabox[0]) / 72
        if w_inch <= 0:
            continue

        dpi: float | None = None
        for img in page_images:
            xobj_width = img[2]
            if xobj_width > 0:
                candidate = xobj_width / w_inch
                if dpi is None or candidate > dpi:
                    dpi = candidate

        # default fallback before applying zoom
        if dpi is None:
            dpi = 300
        # clamp to minimum and maximum bounds
        dpi = max(dpi, 150)
        dpi = min(dpi, MAX_DPI)

        zoom = dpi / 72
        mat = pymupdf.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        png_data = pix.tobytes("png")
        result.append((page_idx + 1, float(dpi), png_data))
        del pix

    doc.close()
    return result


if __name__ == "__main__":
    import sys

    render_pdf_pages_to_png(open(sys.argv[1], "rb").read())
