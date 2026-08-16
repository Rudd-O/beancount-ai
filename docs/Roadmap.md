# Roadmap

Generated from code review of all Python files under `beancount_ai`.

---

## 1. Test infrastructure

- **Expand unit-test coverage** — most modules have zero tests aside from the one metadata helper.

## 2. Package / dependency management

| Current state | Issue | Suggested fix |
|---|---|---|
| No `__init__.py` exports defined | Public API surface is undocumented to consumers importing the package programmatically | Add `__all__ = [...]` in each module's `__init__.py` |

## 3. Configuration robustness

- **Schema validation of `bean-ai.json`** — `Configuration.load()` reads arbitrary keys (`json.load(fh)`) and accesses them by index without checking for missing / extra keys. If a field is renamed upstream, a typo silently produces a `KeyError`. Add either a pydantic model or a `set`-based key check.

## 4. Transaction safety

- **Beancount file backup before editing** — `do_associate` and `ImportResult.commit()` write directly to `.bean` files via raw text manipulation. If the process crashes mid-write or produces malformed output, the ledger is corrupted with no way to recover. Add:
  - Write-to-temp + atomic rename (or `shutil.move` after validation).
  - Pre-edit snapshot backup (e.g. append `.bak-YYYYMMDD-HHMMSS`).

## 6. Receipt organization improvements

- **Filename collision handling** in `predict_receipt_destination_path` — if two receipts arrive on consecutive days with the same date + description, the second overwrites the first silently. Add a `Path.exists()` guard that appends a counter suffix (`receipt.jpg`, `receipt_2.jpg`).
- **Dedup check before fetch** — `ImportResult.__init__` and `associate` both call `fetch_receipt` to download a receipt even though it may already exist locally (e.g. if the ingest was interrupted). Compare file hashes first.
- **Account list** — Currently `ingest` uses a hard-coded accounts list in `RECEIPT_CONVERSION_PROMPT.md`.  This should be dynamically provided by the client, in a safe manner so as to minimize jailbreaks with the AI.

## 7. PDF rendering enhancements (`pdf.py`)

- **Concurrency gate** — `render_pdf_pages_to_png` opens every page at native DPI in one process; a multi-page high-DPI scan can consume gigabytes of RAM. Add:
  - A fixed `max_dpi` constant (set to 300) to cap per-page resolution.
  - A `max_pages` constant (set to 25) with a warning/error if exceeded, suggesting PDF page splitting on the server side.
- **Image resampling** — pages rendered well above their natural DPI produce huge base64 payloads that slow down the LLM call and increase costs.  Use the `max_dpi` constant to cap rendering of very high resolution pages to that reasonable amount.

## 7. The associate flow (partial feature)

The `associate` subcommand is implemented but the user-interaction path for ambiguous matches is entirely commented out (lines 846-886). The spec (`docs/specs/Associating receipts with transactions.md`) defines three pieces still missing:

| Spec section | Status |
|---|---|
| Phase 2 — Beancount candidate loader (`beancount_loader.py` exists but is not called from `associate`) | Partially done via `load_transaction_contexts`. Works. |
| Phase 3 — Server-side `beanai.MatchCandidates` subcommand | Implemented as `beanai.HelpAssociateReceipt`. Works. |
| Phase 4 — Interactive ambiguous-match picker (present ranked list, user selects) | Commented out in client code. **Needs to be un-commented and wired up.** |

Additionally:
- The spec proposes `import_source:` metadata key for preserving old `document:` values. The current code uses `document:`, `document2:`, `document3:` keys instead, where old entries are renumbered (`document2:`, `document3:`, …). This is a divergence from the spec that should either be reconciled or documented as intentional.
- Add a dedicated flag (`--candidate-days`) to override the hard-coded ±1/+45 day window (cli.py:799-801) so users can widen the search for old receipts without editing code.

## 8. General

| Priority | Item |
|---|---|
| High | Beancount file edit safety — backup before edit + atomic write |
| High | Un-comment / wire up the `associate` ambiguous match picker from the spec |
| Medium | Config schema validation (missing keys, empty values) + config singleton reset |
| Medium | Fix duplicate SSL verification blocks on the server |
| Low | Add `--dry-run` mode for all write operations |
| Low | PDF DPI cap to prevent excessive memory usage on multi-page receipts |

## Nonissues

This is a list of things we will not fix.

- **Reset mechanism for the config singleton** — `Configuration.load()` caches at class level permanently with no public reset hook; testing with multiple configs requires separate processes. Expose `Configuration._reset_instance()` (or similar).
- **Validation of resolved values** — `api_url` should be checked for a trailing `/v1`; `beancount_folder` and `beancount_main_file` should exist at load time; `receipts_username` / `receipts_password` should not be empty. Fail fast with a clear message.
- **Retry logic** — `do_fetch`, `do_remove`, and `list_receipts` make one attempt each. A transient network failure on the receipts VM causes the entire import to fail. Add a 3-retry loop with exponential backoff using `tenacity` or a simple helper.
- **Pagination for large directories** — `Client.ls("/", detail=True)` assumes all receipts fit in one listing. Most WebDAV implementations don't paginate but it's worth protecting against very large directories (thousands of files) by adding a configurable limit + warning to the server-side list handler.
- **Bean-ai.json client and server config files shared** — Both `bean-ai.json` config schemas share the same file on disk (`~/.config/bean-ai.json`). The server reads its fields first, then the client reads its fields. This is fine and is intended behavior.
