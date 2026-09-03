# Spec: Rendering of PDF documents to images

Status: developed.

## Overview

PDF receipts are converted page-by-page into PNG images via the `render_pdf_pages_to_png()` function in `beancount_ai/server/pdf.py`.  We do this because vision LLMs often simply cannot read PDFs as images; in many instances they attempt to fall back to OCRed or native PDF text, which isn't as precise for reading text (e.g. OCRed text from skewed receipts), and throws away the visual context — the positioning of each of the elements in the page.

The PNG output is what is sent (as base64-encoded image parts) to an OpenAI-compatible LLM for vision-based receipt parsing. This spec describes the rendering pipeline, DPI handling, and safety gates.

## Core types and return value

`render_pdf_pages_to_png(pdf_bytes: bytes) -> Sequence[Tuple[int, float, bytes]]`

Returns a list of triples:
- **Page number** — 1-based index into the PDF pages.
- **DPI used** — the effective dots per inch applied during rendering (float).
- **PNG bytes** — raw PNG-encoded pixel data for that page.

The returned sequence is sorted by page number ascending. When a page contains multiple embedded images, the highest DPI among them is used for that page's render. Pages where no embedded image is detected use fallback rules described below.

## DPI computation per page

For each page, the function:

1. Retrieves embedded images via `page.get_images(full=True)`.
2. Computes a candidate DPI from each image: `xobj_width / page_width_in_inches`, where `page_width_in_inches = (mediabox[2] - mediabox[0]) / 72`.
3. Selects the **highest** candidate across all embedded images on that page.

### Effective DPI computation

Every page goes through these steps:

```python
dpi = None                          # start — no embedded image candidate yet
if dpi is None:
    dpi = 300                       # default if no image candidate found
dpi = max(dpi, 150)                 # minimum floor
dpi = min(dpi, MAX_DPI)             # maximum cap (300)
```

The clamps only activate when there **is** an embedded image candidate whose computed DPI falls outside the range. The effective DPI applied per page is:

| Scenario | DPI applied |
|---|---|
| No embedded image on page | 300 (default) |
| Embedded image, computed DPI in [150, 300] | Native computed DPI (unchanged) |
| Embedded image, computed DPI < 150 | 150 (floor clamp) |
| Embedded image, computed DPI > 300 | 300 (cap clamp — resampling down) |

Only pages whose native DPI exceeds 300 are actually resampled. Pages at or below 300 render at their native resolution regardless of the "effective window" framing. The minimum floor of 150 is rarely triggered in practice (most PDF images with detectable dimensions yield a DPI well above this threshold).

## Rendering mechanics

The DPI is converted to a zoom factor: `zoom = dpi / 72` (MuPDF works with points internally; 1 point = 1/72 inch). A `pymupdf.Matrix(zoom, zoom)` transform scales the page during rendering via `page.get_pixmap(matrix=mat)`. The result is exported to PNG with `pix.tobytes("png")`.

## Safety gates

### Maximum DPI cap (`MAX_DPI`)

**Value:** 300

Rationale: unbounded native-resolution rendering of high-DPI scans can consume gigabytes of RAM and produce enormous base64 payloads. Capping at 300 ensures LLM payloads remain manageable while retaining sufficient image quality for OCR.

### Maximum pages gate (`MAX_PAGES`)

**Value:** 25

Rationale: multi-page PDFs with many high-resolution pages are a denial-of-service vector — both in terms of memory and LLM cost (image parts consume context tokens). If the PDF has more than `MAX_PAGES` pages, the function closes the document and raises `ValueError`:

```
PDF has {N} pages (maximum allowed: 25). Please split the PDF into smaller files.
```

The gate fires immediately after opening the document, before any page is rendered. No partial work is performed for PDFs that exceed this limit.

## Caller contract

`render_pdf_pages_to_png()` does **not** suppress exceptions it catches internally except by design:

- `pymupdf.open()` may raise for invalid/corrupt PDF data — these propagate to the caller (handled in `file_to_image_parts()` in `server/llm.py:61-64` as a stderr `error: ...` message and `sys.exit(1)`).
- `ValueError` from the `max_pages` gate also propagates; it is not caught inside this function.

The caller `file_to_image_parts()` in `server/llm.py:47-64` wraps all PDF page rendering in a `try/except Exception` block that catches errors, emits a stderr error message, and exits with code 1.

## Constants

| Name | Value | Purpose |
|---|---|---|
| `MAX_DPI` | 300 | Upper bound on rendering resolution per page. |
| `MAX_PAGES` | 25 | Maximum number of pages allowed in a PDF. |
| Minimum DPI floor (implicit) | 150 | Lower bound — used when computed DPI falls below this. |

All three constants are module-level and can be overridden by editing the file directly. None are currently configurable via CLI or config file.

## Integration with LLM pipeline

The PNG bytes returned by `render_pdf_pages_to_png()` are base64-encoded and placed into a Chat completion message array as `image_url` parts (with `"detail": "high"`). Each PDF page produces one such part. The full call is:

```python
{
    "type": "image_url",
    "image_url": {
        "url": f"data:image/png;base64,{base64data}",
        "detail": "high",
    },
}
```

Pages are sent in ascending page-number order. A one-page PDF produces a single image part; a multi-page PDF produces one part per page (up to the cap).

## Notes for future work

- Making `MAX_DPI`, `MAX_PAGES`, and the minimum DPI floor configurable via server config would allow users with high-resolution scanners or unusual use cases to adjust the trade-off between payload size and image quality.
- Splitting large PDFs server-side (e.g., into chunks of 25 pages) could make the gate more user-friendly than a hard error.
