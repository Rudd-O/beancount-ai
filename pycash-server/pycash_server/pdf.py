from typing import Sequence, Tuple

import fitz


def get_pdf_image_pixmap_dpi(pdf_bytes: bytes) -> int | None:
    """Return the highest native DPI among all embedded images in *pdf_bytes*.

    Reads XObject dictionaries from a PDF without rasterizing pages and
    returns the maximum inferred DPI (pixels-per-inch).  Returns None if no
    images could be found.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    best_dpi: int | None = None

    for page in doc:
        images = page.get_images(full=True)

        mediabox = page.mediabox
        width_pts = mediabox[2] - mediabox[0]
        w_inch = width_pts / 72
        if w_inch <= 0:
            continue

        for img in images:
            xobj_width = img[2]  # px from XObject Width key
            if xobj_width <= 0:
                continue

            dpi_x = int(xobj_width / w_inch)
            if best_dpi is None or dpi_x > best_dpi:
                best_dpi = dpi_x

    doc.close()
    return best_dpi


def render_pdf_pages_to_png(
    pdf_bytes: bytes,
) -> Sequence[Tuple[int, float, bytes]]:
    """Render each page of a PDF to PNG bytes at its native embedded DPI.

    Returns a list of ``(page_number_1-based, dpi_used_as_float, png_bytes)``
    tuples sorted by page number.  When multiple pages or images exist the
    highest DPI found on each individual page is used for that page's render.

    Falls back to 300 DPI for any page where no embedded image is detected and
    raises any exception ``fitz.open`` may produce for invalid PDF data.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

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
        # also use a minimum DPI in case of PDFs with no images
        elif dpi < 150:
            dpi = 150

        zoom = dpi / 72
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        png_data = pix.tobytes("png")
        result.append((page_idx + 1, float(dpi), png_data))
        del pix

    doc.close()
    return result


if __name__ == "__main__":
    import sys

    render_pdf_pages_to_png(open(sys.argv[1], "rb").read())
