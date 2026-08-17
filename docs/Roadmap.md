# Roadmap

Generated from code review of all Python files under `beancount_ai`.

---

## 1. Test infrastructure

- **Expand unit-test coverage** — only one test file exists (`test_update_document_metadata.py`) covering the `update_document_metadata` helper. All other modules have zero tests. Add tests for:
  - `load_transactions` and `load_transaction_contexts` in `beancount_loader.py`
  - `predict_receipt_destination_path`, `shorten_fn`, and `insert_document_metadata`
  - PDF rendering edge cases at native / min / max DPI (once caps are implemented)
  - Config loading for both client and server configs

## 2. Configuration robustness

- **Schema validation** — client `Configuration.load()` (`client/config.py:82`) and server `Configuration.load()` (`server/config.py:109`) read arbitrary keys via `json.load(fh)` and access them by bare dict indexing. A typo or missing field silently produces a `KeyError` at runtime. Add either a pydantic model or explicit key sets.
- **Config singleton reset** — `Configuration.load()` caches at class level (`instance: ClassVar`) with no public reset hook. Testing with multiple configs requires separate processes. Expose `Configuration._reset_instance()` (or similar).

## 3. Transaction safety

- **Beancount file backup before editing** — `ImportResult.commit()` appends directly to the ingestion file via raw text manipulation, and `do_associate` writes `tx_file.write_text()` at one point. If the process crashes mid-write or produces malformed output, the ledger is corrupted with no recovery path. Add:
  - Write-to-temp + atomic rename (or `shutil.move` after validation).
  - Pre-edit backup (e.g., append `.bak-YYYYMMDD-HHMMSS`).

## 4. Receipt organization improvements

- **Filename collision handling** in `predict_receipt_destination_path` (`client/cli.py:226`) — if two receipts share a date + description, the second silently overwrites the first. Add a `Path.exists()` guard that appends a counter suffix (e.g., `receipt.jpg`, `receipt_2.jpg`).
- **Dedup check before fetch** — both `ImportResult.__init__` (`client/cli.py:368`) and `associate_one` (`client/cli.py:968`) download a receipt via `fetch_receipt` even when it may already exist locally (e.g., after an interrupted ingest). Compare file hashes first.
- **Prompt injectivity** — the account list is currently dynamically injected to `RECEIPT_CONVERSION_PROMPT.md` on the server side, with an account listing coming from the client side, which is read from a static file on disk. This should be dynamically provided by the client, directly from the Beancount data, in a safe manner to minimize injection risks through the AI payload.  An important question to resolve prior to solving this issue is how do we ensure only certain accounts in the tree are included, and another question is how do we extract user-supplied comments for each account, to support the current use of the account list (which includes an account with an optional `#`-prefixed comment at the end).  Can we supply this information using Beancount account open metadata?  If so, then it's worth doing it.

## 5. PDF rendering enhancements (`pdf.py`)

| Status | Item |
|---|---|
| Done (partial) | Minimum DPI floor of 150 is implemented (line 47). Default fallback for pages without embedded images is 300 DPI (line 44). |
| **TODO** | **`max_dpi` cap** — no upper bound exists. A multi-page high-DPI scan can still render every page at full native resolution, consuming gigabytes of RAM. Add a fixed `max_dpi` constant (e.g., 300) to limit rendering. |
| **TODO** | **`max_pages` gate** — no page count check exists. A multi-page high-DPI scan should trigger a warning or error if pages exceed a threshold (e.g., 25). Suggest PDF splitting on the server side. |
| **TODO** | **Resampling** — pages rendered above their natural DPI produce huge base64 payloads that slow down LLM calls and increase costs. Cap rendering resolution using the `max_dpi` constant once implemented. |

## 6. The associate flow (partial feature)

The `associate` subcommand is implemented but remains partially incomplete:

| Spec section | Status |
|---|---|
| Phase 1 — Receipt date/amount extraction via LLM | **Done** — `HelpAssociateReceipt` processes the receipt with `RECEIPT_INFO_PROMPT.md`. |
| Phase 2 — Beancount candidate loader (`load_transaction_contexts`) | **Done** — used at `client/cli.py:776-778` within `associate_one`. |
| Phase 3 — Server-side match subcommand | **Partial** — implemented as `beanai.HelpAssociateReceipt`; two-step flow (receipt info first, then candidate matching via stdin). Works. |
| Phase 4 — Interactive ambiguous-match picker | **Not done** — the ranked-list prompt is commented out at `client/cli.py:819-861` and never reached; instead an exception is raised when matches are ambiguous (`client/cli.py:815-817`). Needs to be un-commented and wired up. |

Additionally:
- **Hard-coded window** — the ±1/+45 day search window in `associate_one` (`client/cli.py:769-773`) is not configurable. For old receipts, users must edit code or wait for a future `--candidate-days` flag.

## 7. General

| Priority | Item |
|---|---|
| High | Beancount file edit safety — backup before edit + atomic write |
| High | Un-comment / wire up the `associate` ambiguous match picker from the spec |
| Medium | Config schema validation (missing keys, empty values) |
| Medium | PDF max DPI cap and max_pages gate (`pdf.py`) |
| Medium | Fix duplicate SSL verification blocks on the server (see FIXME at `server/cli.py:327`) |
| Low | Add `--dry-run` mode for all write operations (current `--no` only shows diff, it does not process) |
| Low | Dedup check before receipt fetch |
| Low | Retry logic — `do_fetch`, `do_remove`, and `list_receipts` make one attempt each. A transient network failure on the receipts VM causes the entire import to fail. Add a 3-retry loop with exponential backoff using `tenacity` or similar. |

## Nonissues

This is a list of things we will not fix.  Do not remove anything from this list when refactoring this file, unless explicitly asked.  Do not do any work involving these items — they are excluded from consideration as work items deliberately.

- **Reset mechanism for the config singleton** — `Configuration.load()` caches at class level permanently with no public reset hook; testing with multiple configs requires separate processes. Expose `Configuration._reset_instance()` (or similar).
- **Validation of resolved values** — `api_url` should be checked for a trailing `/v1`; `beancount_folder` and `beancount_main_file` should exist at load time; `receipts_username` / `receipts_password` should not be empty. Fail fast with a clear message.
- **Retry logic** — `do_fetch`, `do_remove`, and `list_receipts` make one attempt each. A transient network failure on the receipts VM causes the entire import to fail. Add a 3-retry loop with exponential backoff using `tenacity` or a simple helper.
- **Pagination for large directories** — `Client.ls("/", detail=True)` assumes all receipts fit in one listing. Most WebDAV implementations don't paginate but it's worth protecting against very large directories (thousands of files) by adding a configurable limit + warning to the server-side list handler.
- **Bean-ai.json client and server config files shared** — Both `bean-ai.json` config schemas share the same file on disk (`~/.config/bean-ai.json`). The server reads its fields first, then the client reads its fields. This is fine and is intended behavior.
- **Empty `__init__.py` in `beancount_ai/`, `client/`, `server/`** — this program is not a library but a program designed to be consumed via the CLI.
