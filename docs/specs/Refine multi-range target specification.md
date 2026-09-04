# Spec: Multi-range target specification for `bean-ai refine`

Status: in development.

Supersedes the single-range (2-arg) and single-transaction (1-arg) invocation forms described in `docs/specs/Refine existing Beancount transactions.md`.  That document's description of the refine *flow* (LLM pass, diff, prompt, write-once) remains authoritative; this spec replaces only the CLI argument grammar and the block-extraction driver.

## Overview

Extend `bean-ai refine` so it can accept any number of 1-based line-number or line-range tokens after the file path, e.g.:

```sh
bean-ai refine main.bean 1234               # one transaction (same as before)
bean-ai refine main.bean 1234 5678          # two single-transaction targets
bean-ai refine main.bean 123-456 789-1010   # two multi-transaction ranges
bean-ai refine main.bean 1234 789-1010 2000 # mixed, mixed order
```

Each token is independently either:
- a single 1-based line number `N` ("touch the transaction containing line N"), or
- a range `A-B` where `A` and `B` are 1-based line numbers (`A <= B`), meaning "every transaction that *begins* on a line `L` with `A <= L <= B`". (Per the existing single-range semantics, the boundary is on the *start* line of the transaction, not its end, so a transaction whose body extends past `B` is still included whole.)

The tokens may be interleaved in any order.  The resulting refinement run refines the **union** of all transactions selected by any token, in file order (i.e. ascending start-line order), each refined exactly once.

## Rationale

Today a user who wants to fix a handful of unrelated transactions in the same file has to either (a) widen one range so much that it also refines transactions they did not mean to touch, or (b) run `refine` multiple times back-to-back, re-prompting and re-reading the file each time.  Allowing a list of independent targets makes the natural workflow a single command.

## Argument grammar

New grammar (the previous 2-positional-arg form is removed, not retained as a fallback):

```
bean-ai refine <file_path> <target>+ [--yes | --no] [--clear]

<target> ::= <line_no> | <line_no>-<line_no>
```

- `<line_no>` is a positive integer (1-based).
- A `<target>` is either a single `<line_no>`, or a `<line_no>-<line_no>` pair where `-` is a literal hyphen/minus (no whitespace allowed inside the token).
- The parser accepts **one or more** `<target>` tokens (previously: exactly one or two line-number positionals).
- The old `[last_line_number]` optional positional is gone; the `A-B` form replaces it.

Parsing notes:
- argparse `nargs="+"` is used for the `<target>+` position with a `type=` callable that parses one token and returns the tuple `(start_1based, end_1based)` where `end_1based == start_1based` for single-token cases.  The callable raises `argparse.ArgumentTypeError` (with an explanatory message) on malformed tokens.
- A token such as `5-` / `-5` / `5-3` (inverted), `0`, negative, or containing non-digit characters must be rejected with a clear message, not silently coerced.
- The command still requires at least one `<target>` and at most as many as are needed for the given file (no artificial cap).

## Range validation

After all tokens are parsed into `(start, end)` pairs (1-based, inclusive), the client validates them **before** any file access.  Violations raise `ValueError` (caught in `run()` and printed to stderr with non-zero exit, matching the style used for `split_into_transactions_by_range` validation today).

Rules:

1. **Non-overlapping.** No two tokens' selected line spans may intersect.  This means for any two tokens (i, j) with `i != j`, their selected *line intervals* are disjoint, i.e. `max(end_i, start_j) ... min(start_i, end_j)` is empty.  Concretely, if we sort the tokens `(s1,e1), (s2,e2), ...` by `start`, we require `e_k < s_{k+1}` for consecutive pairs.  Note: because the semantic is "transaction *starts*" within the interval, adjacent intervals like `1-500` and `501-1000` are allowed (they are disjoint: `500 < 501`).
2. **Strictly ascending.** The tokens, taken in the order supplied by the user, must already be sorted by start-line: for consecutive tokens `(s_k, e_k)` and `(s_{k+1}, e_{k+1})` the user must supply them such that `s_k < s_{k+1}`.  (The non-overlap rule alone already forces ascending starts *if the user happens to provide them in that order*; the explicit strict-ascending check makes the intent visible and yields a friendlier error message for e.g. `5678-9012 1234`.)
3. **In-bounds.** Every token's `start` and `end` must satisfy `1 <= start <= end <= N` where `N` is the number of lines in the file.  This bound is checked *after* the file is read (the file may be empty, missing, etc.; the missing-file check is unchanged).

Error messages:
- Overlap / descending-order example: `Error: target ranges are not strictly ascending and non-overlapping: 1-500 400-900 (range #2 begins before range #1 ends)`.
- Out-of-bounds example: `Error: target range 5-12 out of file bounds (file has 9 lines)`.

The validation step is a pure function of the parsed token list and the file's line count.  It is unit-tested independently of the file-reading logic.

## Dedup / union semantics

The **set of transactions to refine** is the union of:

- For a single-line token `N`: the transaction containing line `N` (walk-back semantics, as today).
- For a range token `A-B`: every transaction that *begins* on a line `L` with `A <= L <= B` (walk-back semantics on the lower bound: if `A` is inside a transaction, that transaction's true start line wins and it is included whole).

The set of transactions to refine is deduplicated: if two tokens select the same transaction, it is refined exactly once.  The dedup is by transaction start-line index (the canonical key for a transaction in the file-order classification the classifier produces).

Because of rule 1 (non-overlapping line spans), two tokens can only select the *same* transaction in one narrow case: when the transaction's body straddles the boundary between two tokens.  For example, transactions starting at line 498, 502 (in a 1000-line file): token `1-500` selects the tx starting at 498 and 502 (both begin inside `[1, 500]`? no — 502 > 500, so only the 498 tx).  A more realistic collision: token `490-500` and `501-510` — if a transaction begins at line 498 with body extending to line 505, the first range selects it (start 498 in `[490,500]`); no second range selects it, because "begins in range" is the criterion and 498 ∉ `[501,510]`.  So a collision requires a transaction whose start line equals exactly the token boundary AND the tokens are contiguous — which the non-overlap rule prevents (contiguous `A-B` and `B+1-C` do not overlap).  If (contrary to the validation rules above) such a collision did happen anyway, the dedup step is a belt-and-braces: it removes duplicates and keeps the *earliest* (first-appearing) token's classification, so the transaction is refined exactly once.

## Block extraction (new driver function)

Replace the single call to `split_into_transactions_by_range(all_lines, first - 1, last - 1)` in `run()` with a new helper (see "File and function changes" below) that:

- Accepts `all_lines: list[str]` and `targets: list[tuple[int, int]]` (0-based, inclusive, already normalized to file order — after the validation step).
- Classifies the file once into the same `(is_transaction, lines)` tuple format as `split_into_transactions_by_range`, but flags **every transaction whose start-line index falls within the union of the given spans** (plus: any single-line token that lands inside a transaction flags that transaction via walk-back).
- Returns the classification list so the existing `blocks`-based loop in `do_refine_one` (and the diff/write machinery) is unchanged.

Implementation is a thin composition of the base classifier, not a rewrite:
1. Run `split_into_transactions_by_range` over the whole file with `start_line=0` and `end_line=len(all_lines)-1` *once*.  That yields the file-partitioning into `(is_transaction, lineno, lines)` — every transaction in the file appears, and each transaction's start-line index is given directly by the group's `lineno`.
2. The base classifier already emits **exactly one transaction per `True` group**, even for two adjacent transactions with no blank line between them: a transaction-header line (a Beancount date beginning a `*`/`!` transaction) always ends the previous block and starts a new one (see `test_adjacent_txs_no_separator_get_own_groups`).  No re-splitting step is required — a single-line target pointing into the second of two adjacent transactions flags only that one.
3. Flag each transaction: a transaction is selected iff its start-line index (`lineno`) falls in any target span, or any line of it does (walk-back).  Emit the list of `(is_transaction, lineno, lines)` tuples with `is_transaction=True` only for selected transactions; non-selected transactions become `False` groups (their lines are still carried over so `collapse_blocks_into_lines` round-trips byte-for-byte).

`classify_by_target_spans` therefore inherits from the base classifier its invariant that **every `True` group is exactly one transaction**.  This also fixes a latent issue in today's single-range refinement, where a range covering two adjacent unseparated transactions used to refine them as one merged block; under the new driver each is refined independently.

This keeps the existing guarantee: the file is reassembled by concatenating all groups, and every line not belonging to a selected transaction is carried over byte-for-byte.

## Interaction with `--yes` / `--no` / `--clear` / prompt

Unchanged: `run()` still loops over the flagged blocks in file order, calls `do_refine_one` for each, and prompts the user per transaction.  The prompts, `--yes`, `--no`, `--clear`, and the single write-at-the-end semantics all remain exactly as documented in the existing spec.  The only observable difference is how many flagged transactions there are (potentially many, from many tokens), and the ordering is the file's start-line order (not the order the user typed the tokens in).

## Exit codes & error surface

- Unchanged for the existing categories (missing file, malformed LLM output, missing document, server failure, parse error, prompt EOF, etc.).
- New: argument-parse failures (invalid token text, zero/empty target list) abort at argparse level (exit code 2, argparse's normal stderr).
- New: range validation failures (overlap, not ascending, out-of-bounds, empty file) abort before any file mutation, print the `ValueError` message to stderr, and exit 1 — matching the existing style for line-range validation.

## CLI example sessions

```sh
$ bean-ai refine Documents/Accounting/00-beancount.bean 1234 5678
Refining transaction '2026-01-21 * "Foo"' ...   (whatever tx starts in 1234)
Apply refined transaction to Documents/Accounting/00-beancount.bean? [y/n/p/q]
...
Refining transaction '2026-03-03 * "Bar"' ...   (whatever tx starts in 5678)
Apply refined transaction to Documents/Accounting/00-beancount.bean? [y/n/p/q]
...
Updated transactions in Documents/Accounting/00-beancount.bean

$ bean-ai refine Documents/Accounting/00-beancount.bean 123-456 789-1000
...refines every tx beginning on lines 123..456 and 789..1000, in file order...

$ bean-ai refine Documents/Accounting/00-beancount.bean 1234 789-1012
Error: target ranges are not strictly ascending and non-overlapping: 1234 789-1012
       (token #2 begins before token #1 does)
```

## File and function changes

| File | Change |
|---|---|
| `beancount_ai/client/beanfiles.py` | Modify `split_into_transactions_by_range` so it emits **exactly one transaction per `True` group**: instead of grouping the per-line flags with `itertools.groupby()` (which merged consecutive unseparated transactions into a single group), the fold into blocks now starts a new block at every transaction header, so two adjacent transactions with no blank line between them become separate `(True, lineno, lines)` blocks.  This fixes the latent merged-block issue described in "Block extraction" and makes `classify_by_target_spans` a trivial remap.  Add `classify_by_target_spans(tx_lines: list[str], spans: list[tuple[int, int]]) -> list[tuple[bool, int, list[str]]]` (0-based inclusive) alongside it: run the base classifier once over the whole file and re-flag each `True` group by span intersection (walk-back).  Add doctests covering: single single-line token; single range token; multiple disjoint single-line tokens; multiple disjoint range tokens; mixed; walk-back when a single line lands mid-tx; dedup when two spans both select the same tx (via a contrived example). |
| `beancount_ai/client/commands/refine.py` | Replace the two positional args (`first_line_number`, `last_line_number`) with one `nargs="+"` positional `targets` whose `type=` parses a token into `(start_1, end_1)` (1-based, `end >= start`).  Add `validate_target_ranges(targets: list[tuple[int,int]], n_lines: int) -> list[tuple[int,int]]` that enforces rules 1-3 and raises `ValueError`.  In `run()`: after reading the file, call `validate_target_ranges` (catch `ValueError` → stderr + exit 1, same style as the existing `split_into_transactions_by_range` error path), substitute the new block-extraction call for `classify_by_target_spans(all_lines, spans_0based)`, and drive the existing `do_refine_one` loop over the resulting flagged blocks.  Update `subcommand_parser` and the help string. |
| `beancount_ai/tests/test_refine_targets.py` | New test module for the token parser and `validate_target_ranges` (accept/Reject matrix for each failure mode). |
| `beancount_ai/tests/test_do_refine.py` | Update `argparse.Namespace(...)` constructions: replace `first_line_number` / `last_line_number` attributes with `targets=[(...)]` (1-based pairs). |
| `beancount_ai/tests/test_split_at_transaction_by_line_number.py` | Add doctest/unit tests for `classify_by_target_spans` mirroring the existing `split_into_transactions_by_range` test shape. |
| `docs/specs/Refine existing Beancount transactions.md` | Add a pointer at the top: "The target-specification grammar described here is superseded by `docs/specs/Refine multi-range target specification.md`.  See that document for the CLI argument format; the rest of this document (flow, prompt, server-side protocol) still applies." |

## Backwards compatibility

None is preserved.  The old `bean-ai refine <file> <n>` and `bean-ai refine <file> <n> <m>` invocations are rejected by the new parser (a bare single line number still happens to be accepted as `<target> == <line_no>`, so `bean-ai refine f 42` keeps working; but a 3-arg form like `bean-ai refine f 42 200` — previously "range 42..200" — is now rejected because `200` is a *second single target* meaning tx at line 200, which is a different meaning.  This is an acceptable, documented break given there is a single user and no scripts currently use this command.)

## Open questions

- None; range semantics (boundary on start line, walk-back, dedup, file-order refinement) are inherited from the existing single-range spec to minimize behavior change.

## Implementation order (proposed)

1. Modify `split_into_transactions_by_range` to emit one transaction per `True` group (replace the `itertools.groupby` fold), then add `classify_by_target_spans` + doctests in `beanfiles.py`.
2. `validate_target_ranges` + arg-parser token callable in `client/commands/refine.py`.
3. Rewire `run()` to the new flow; update `subcommand_parser`.
4. Update `test_do_refine.py` `Namespace(...)` to the new `targets` attribute; add `test_refine_targets.py`.
5. Update `test_split_at_transaction_by_line_number.py` for `classify_by_target_spans`.
6. Update `docs/specs/Refine existing Beancount transactions.md` with the supersession pointer, and `AGENTS.md` (the subcommand synopsis for `refine`) if the command signature is quoted there.
7. `make qa`.
