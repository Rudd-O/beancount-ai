# Roadmap

Generated from code review of all Python files under `beanhand`.

---

## 1. Test infrastructure

- **Expand unit-test coverage** — the following test files exist: `test_update_document_metadata.py` (the `update_document_metadata` helper), `test_refine_helpers.py` (`extract_document_paths`, `resolve_local_document_path`), `test_do_refine.py` and `test_do_refine_server.py` (client & server `do_refine` flows), `test_import_result.py` (`ImportResult`), `test_beancount_lock.py` (the `BeancountConfiguration` advisory file lock and `write_beancount_file()`), and `test_split_at_transaction_by_line_number.py` (the transaction-splitting helpers, incl. `split_into_transactions_by_range`). Still untested, add tests for:
  - `load_transactions` and `load_transaction_contexts` in `beancount_loader.py`
  - `predict_receipt_destination_path`, `shorten_fn`, and `insert_document_metadata`
  - PDF rendering edge cases (native / 150 DPI floor / 300 DPI cap / the 25-page limit)
  - Config loading for both client and server configs

## 2. Configuration robustness

- **Schema validation** — client `Configuration.load()` (`client/config.py:72`) and server `Configuration.load()` (`server/config.py:96`) read arbitrary keys via `json.load(fh)` and access them by bare dict indexing. A typo or missing field silently produces a `KeyError` at runtime. Add either a pydantic model or explicit key sets.

## 3. Transaction safety

- **Beancount file backup before editing** — `ImportResult.commit()` (`client/commands/importcmd.py:99`) appends directly to the ingestion file via raw text manipulation, and the `refine` / `associate` commands rewrite their target file through `write_beancount_file()` (`client/beanfiles.py`), a whole-file write that flushes and `os.fsync()`s to disk on completion. The per-subcommand advisory lock (implemented; see `BeancountConfiguration.lock()`) keeps concurrent writers from clobbering each other, and the `fsync` guards against a crash losing unsynced data, but there is still no recovery path if the process produces malformed output or is interrupted between `open()` and the write. Add:
  - Write-to-temp + atomic rename (or `shutil.move` after validation).
  - Pre-edit backup (e.g., append `.bak-YYYYMMDD-HHMMSS`).

## 4. Receipt organization improvements

- **Filename collision handling** in `predict_receipt_destination_path` (`client/beanfiles.py:23`) — if two receipts share a date + description, the second silently overwrites the first. `shorten_fn()` only shortens over-long names; it does not guard against an already-existing path. Add a `Path.exists()` guard that appends a counter suffix (e.g. `receipt.jpg`, `receipt_2.jpg`).
- **Dedup check before fetch** — both `ImportResult.__init__` (`client/commands/importcmd.py:26`) and the associate command's per-receipt flow (`client/commands/associate.py:303`) download a receipt via `fetch_receipt` even when it may already exist locally (e.g. after an interrupted ingest). Compare file hashes first.

## 5. The associate flow (partial feature)

The `associate` subcommand is implemented but remains partially incomplete:

| Spec section | Status |
|---|---|
| Phase 1 — Receipt date/amount extraction via LLM | **Done** — `HelpAssociateReceipt` processes the receipt with `RECEIPT_INFO_PROMPT.md`. |
| Phase 2 — Beancount candidate loader (`load_transaction_contexts`) | **Done** — used at `client/commands/associate.py:96` within `do_associate_one`. |
| Phase 3 — Server-side match subcommand | **Done** — implemented as `beanhand.HelpAssociateReceipt`; two-step flow (receipt info first, then candidate matching via stdin). Works. |
| Phase 4 — Interactive ambiguous-match picker | **Not done** — the ranked-list prompt is commented out at `client/commands/associate.py:139-180` and never reached; instead an exception is raised when matches are ambiguous (`client/commands/associate.py:134-137`). Needs to be un-commented and wired up. |

Additionally:
- **Hard-coded window** — the ±1/+45 day search window in `do_associate_one` (`client/commands/associate.py:90-93`) is not configurable. For old receipts, users must edit code or wait for a future `--candidate-days` flag.

## 6. Code quality

- **`beanfiles.py:classify_by_target_spans`** and friends returns lists of lists of lines which identify a transaction by a list of lines.  It would be a good idea to have an actual `TransactionText` class that contains the lines, and that can provide information about the transaction such as the date, and then callers can use an `isinstance()` check instead of checking for a boolean.  The date extraction present in `refine.py` can then fold as a method of that `TransactionText` class.

## 7. General

| Priority | Item |
|---|---|
| Medium | Code quality |
| Medium | Beancount file edit safety — backup before edit + atomic write |
| Medium | Un-comment / wire up the `associate` ambiguous match picker from the spec |
| Medium | Config schema validation (missing keys, empty values) |
| Low | Add `--dry-run` mode for all write operations (current `--no` only shows diff, it does not process) |
| Low | Dedup check before receipt fetch |
| Low | Retry logic — `RemoteVM.fetch_receipt()`, `RemoteVM.remove_receipt()`, and `RemoteVM.list_receipts()` (`client/server.py`) make one attempt each. A transient network failure on the receipts VM causes the entire import to fail. Add a 3-retry loop with exponential backoff using `tenacity` or similar. |

## Nonissues

This is a list of things we will not fix.  Do not remove anything from this list when refactoring this file, unless explicitly asked.  Do not do any work involving these items — they are excluded from consideration as work items deliberately.

- **Reset mechanism for the config singleton** — `Configuration.load()` caches at class level permanently with no public reset hook; testing with multiple configs requires separate processes. Expose `Configuration._reset_instance()` (or similar).
- **Validation of resolved values** — `api_url` should be checked for a trailing `/v1`; `beancount_folder` and `beancount_main_file` should exist at load time; `receipts_username` / `receipts_password` should not be empty. Fail fast with a clear message.
- **Retry logic** — `RemoteVM.fetch_receipt()`, `RemoteVM.remove_receipt()`, and `RemoteVM.list_receipts()` (`client/server.py`) make one attempt each. A transient network failure on the receipts VM causes the entire import to fail. Add a 3-retry loop with exponential backoff using `tenacity` or a simple helper.
- **Pagination for large directories** — `Client.ls("/", detail=True)` assumes all receipts fit in one listing. Most WebDAV implementations don't paginate but it's worth protecting against very large directories (thousands of files) by adding a configurable limit + warning to the server-side list handler.
- **beanhand.json client and server config files shared** — Both `beanhand.json` config schemas share the same file on disk (`~/.config/beanhand.json`). The server reads its fields first, then the client reads its fields. This is fine and is intended behavior.
- **Empty `__init__.py` in `beanhand/`, `client/`, `server/`** — this program is not a library but a program designed to be consumed via the CLI.
- **Config singleton reset** — `Configuration.load()` caches at class level (`instance: ClassVar`) with no public reset hook. Testing with multiple configs requires separate processes. Expose `Configuration._reset_instance()` (or similar).  We don't care about configuration resets because the CLI program is a one-shot execution affair.