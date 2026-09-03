# Roadmap

Generated from code review of all Python files under `beancount_ai`.

---

## 1. Test infrastructure

- **Expand unit-test coverage** — the following test files exist: `test_update_document_metadata.py` (the `update_document_metadata` helper), `test_refine_helpers.py` (`extract_document_paths`, `resolve_local_document_path`), `test_do_refine.py` and `test_do_refine_server.py` (client & server `do_refine` flows), `test_import_result.py` (`ImportResult`), and `test_split_at_transaction_by_line_number.py` (the transaction-splitting helpers, incl. `split_into_transactions_by_range`). Still untested, add tests for:
  - `load_transactions` and `load_transaction_contexts` in `beancount_loader.py`
  - `predict_receipt_destination_path`, `shorten_fn`, and `insert_document_metadata`
  - PDF rendering edge cases (native / 150 DPI floor / 300 DPI cap / the 25-page limit)
  - Config loading for both client and server configs

## 2. Configuration robustness

- **Schema validation** — client `Configuration.load()` (`client/config.py:72`) and server `Configuration.load()` (`server/config.py:96`) read arbitrary keys via `json.load(fh)` and access them by bare dict indexing. A typo or missing field silently produces a `KeyError` at runtime. Add either a pydantic model or explicit key sets.

## 3. Transaction safety

- **Beancount data lock** — functions on the client that attempt to edit data may trample on each other if accidentally invoked in parallel (e.g. from multiple terminals), potentially destroying data.  The mechanism we will use to prevent this (in effect, making multiple data-modification commands queue up one behind the other) is POSIX advisory file locking -- a method on the BeancountConfiguration class should be called that opens the main Beancount file for reading, then locks the file.  The locking should first be grabbed such that, if the lock is already grabbed by another process, a message is printed to standard error indicating that the Beancount data files are locked by another process, and the lock is attempted to be grabbed again, this time to lock indefinitely (hang) until the lock is released.  This method is to be called right at the beginning of any function, or at the latest right before reading any Beancount file.  In addition to this, any Beancount file writes should be flushed to disk, to improve data reliability.  It may even be worthwhile to attempt to grab the lock *right when the BeancountConfiguration object is instantiated* (keeping a reference to the open file alive in the class instance, so it doesn't get garbage-collected).
- **Beancount file backup before editing** — `ImportResult.commit()` (`client/cli.py:923`) appends directly to the ingestion file via raw text manipulation, and `do_refine` / `do_associate_one` write `tx_file.write_text()` directly (`client/cli.py:839`, `client/cli.py:1515`). If the process crashes mid-write or produces malformed output, the ledger is corrupted with no recovery path. Add:
  - Write-to-temp + atomic rename (or `shutil.move` after validation).
  - Pre-edit backup (e.g., append `.bak-YYYYMMDD-HHMMSS`).

## 4. Receipt organization improvements

- **Filename collision handling** in `predict_receipt_destination_path` (`client/cli.py:252`) — if two receipts share a date + description, the second silently overwrites the first. `shorten_fn()` only shortens over-long names; it does not guard against an already-existing path. Add a `Path.exists()` guard that appends a counter suffix (e.g. `receipt.jpg`, `receipt_2.jpg`).
- **Dedup check before fetch** — both `ImportResult.__init__` (`client/cli.py:850`) and `do_associate_one` (`client/cli.py:1268`) download a receipt via `fetch_receipt` even when it may already exist locally (e.g. after an interrupted ingest). Compare file hashes first.
- **Prompt injectivity for accounts** — the account list is currently dynamically injected to `RECEIPT_CONVERSION_PROMPT.md` on the server side, with an account listing coming from the client side, which is read from a static file on disk. This should be dynamically provided by the client, directly from the Beancount data, in a safe manner to minimize injection risks through the AI payload.  An important question to resolve prior to solving this issue is how do we ensure only certain accounts in the tree are included, and another question is how do we extract user-supplied comments for each account, to support the current use of the account list (which includes an account with an optional `#`-prefixed comment at the end).  Can we supply this information using Beancount account open metadata?  If so, then it's worth doing it.

## 5. The associate flow (partial feature)

The `associate` subcommand is implemented but remains partially incomplete:

| Spec section | Status |
|---|---|
| Phase 1 — Receipt date/amount extraction via LLM | **Done** — `HelpAssociateReceipt` processes the receipt with `RECEIPT_INFO_PROMPT.md`. |
| Phase 2 — Beancount candidate loader (`load_transaction_contexts`) | **Done** — used at `client/cli.py:1303` within `do_associate_one`. |
| Phase 3 — Server-side match subcommand | **Partial** — implemented as `beanai.HelpAssociateReceipt`; two-step flow (receipt info first, then candidate matching via stdin). Works. |
| Phase 4 — Interactive ambiguous-match picker | **Not done** — the ranked-list prompt is commented out at `client/cli.py:1346` and never reached; instead an exception is raised when matches are ambiguous (`client/cli.py:1342`). Needs to be un-commented and wired up. |

Additionally:
- **Hard-coded window** — the ±1/+45 day search window in `do_associate_one` (`client/cli.py:1297-1300`) is not configurable. For old receipts, users must edit code or wait for a future `--candidate-days` flag.

## 6. User-selectable local file-based receipt backend

This project originally had a local file-based receipt backend but for expediency reasons moved to WebDAV.  A new configuration backend -- and document sources access code -- supporting local files needs to be implemented and wired into the code.

## 7. General

| Priority | Item |
|---|---|
| High | Beancount data lock |
| High | Beancount file edit safety — backup before edit + atomic write |
| High | Un-comment / wire up the `associate` ambiguous match picker from the spec |
| Medium | Config schema validation (missing keys, empty values) |
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
- **Config singleton reset** — `Configuration.load()` caches at class level (`instance: ClassVar`) with no public reset hook. Testing with multiple configs requires separate processes. Expose `Configuration._reset_instance()` (or similar).  We don't care about configuration resets because the CLI program is a one-shot execution affair.