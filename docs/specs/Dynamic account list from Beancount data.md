# Spec: Dynamic account list from Beancount data

Status: proposed.

This is the specification for the Roadmap item **Prompt injectivity for accounts** (`docs/Roadmap.md`).

## Overview

Today the account list offered to the LLM is read by the client from a **static file on disk** (`beancount.account_list_file`, customarily `~/.config/bean-ai.accounts`), which the user has to keep in sync with the ledger by hand (e.g. via `bean-query ... 'SELECT distinct account ORDER BY account;'`). Ingested accounts can drift out of sync, disappear from the file silently, or carry stale per-account hints.

This feature makes the client **derive the account list directly from the Beancount ledger** at run time:

1. The list is computed from the parsed ledger (`beancount.loader`), so it can never drift from the data it is describing.
2. Only accounts **open on the execution date**, or **closed after** it, are included — a closed account is never offered to the LLM to book new expenses against.
3. Which accounts (and subtrees) are offered at all is decided by **opt-in metadata** on the account's `open` directive (`bean-ai-use: "recursive"`), not by the whole account tree.
4. Per-account guidance that previously lived in `#`-suffixed comments in the static file becomes **typed, quoted string metadata** (`bean-ai-rules`), which removes the free-text parsing seam that the old format had.
5. The wire format changes from a JSON array of strings (`["Assets:Cash:CHF", ...]`) to a JSON array of **typed objects** (`[{"name": "Assets:Cash:CHF", "rule": "..."}, ...]`), and the server treats it as **inert data**: it never re-parses comments, never strips content, and injects it verbatim. All trust is established client-side from the user's own ledger.
6. The client sends the same list on the `beanai.Process` *and* `beanai.Refine` paths, so both LLM prompts see an identical account universe.

This feature **removes** the `beancount.account_list_file` config key outright. That is a **breaking change** for existing installs: they must mark their accounts in the ledger (see Migration) — no static-file fallback, on purpose.

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Source of the account list | Parsed `beancount.loader` ledger, read by the client | Never diverges from the data; reuses infrastructure already present in `client/beancount_loader.py` |
| "Open at execution time" | An account qualifies if its `open` date ≤ run date and it has no `close`, or its most recent `close` date > run date | A closed account must never be offered for new postings; an account closed *after* today is still live today |
| Opt-in mechanism | `bean-ai-use: "recursive"` metadata on the account's **`open` directive** | Opt-in (safe default: nothing is offered) rather than opt-out; a single flag on the root of a subtree covers children; only accounts that appear in *some* included subtree are emitted |
| Per-account guidance | `bean-ai-rules` metadata (a string) on the `open` directive; **does not inherit** from ancestors | Keeps the prompt tight: each account gets only guidance written for it; avoids re-emitting the same text 50 times down a subtree |
| Account types | Every account type is eligible (`Assets`, `Liabilities`, `Income`, `Expenses`, plus custom roots) | The prompt already asks for funding accounts as well as expense accounts; no reason to special-case |
| Root account (`Expenses`, bare `Assets`) | Included, subject to the same opt-in and date rules | Users mark whatever subtree roots they want; nothing is hardcoded about tree shape |
| Accounts never `open`ed (only ever referenced in postings) | **Not included**, not offered as a fallback | In Beancount, referencing an unopened account is a *validation error* (`Invalid reference to unknown account`), so such names can only exist in a broken ledger; including them would make the LLM propose postings that beancount itself rejects |
| Unopened sub-accounts inside an included, *open*, recursive subtree (e.g. `Expenses:Food:Bakery` when `Expenses:Food` is open and used) | **Not included** | Same reason: an account that has no `open` directive cannot legally receive postings. If the user wants an account available to the LLM, they must `open` it. (The `bean-ai-use` flag has no meaning on an unopened account — there is no directive to carry metadata on.) |
| Metadata on unopened accounts | Impossible in Beancount | Metadata is only attached to directives (`open`/`close`/transactions); there is no directive for an account that was never opened, so per the previous item there is nothing to annotate, and attempting to "fix" this would produce invalid Beancount |
| Wire format | JSON array of `{"name": ..., "rule"?: ...}` objects (new `AccountRef` struct), replacing the array of strings | Gives a stable, self-describing home for account-level text; removes all comment-parsing from both ends; keeps the server free of any ledger-specific interpretation |
| Prompt filling | Server fills the `{accounts}` placeholder with `json.dumps(account_refs, indent=2)`; prompts gain a short note explaining the field meanings | Identical mechanism today (`json.dumps`), now typed; the note lets the LLM use rules without further explanation |
| `account_list_file` config key | **Removed** (breaking change; the key is ignored with a one-line stderr warning if still present in a config) | A hand-maintained static file is the very source of drift and of the untyped free-text seam this roadmap item exists to remove; keeping a fallback would keep that path alive forever. The migration from the static file to ledger metadata is a one-time edit (see Migration) |
| Loading cost | Reuses `loader.load_file(main_file)`; the load is already performed by the associate flow, and the client holds the advisory lock on the main file for the whole run, so a single load is race-free | Negligible; one parse per command |

An alternative for the opt-in mechanism — `bean-ai-use: "yes"` (self only) vs. `"recursive"` — was considered and **rejected**: "self only" is strictly less useful than opening the account and its children individually, and the extra enum value buys no safety. The single `"recursive"` value keeps the surface minimal.

## Marking accounts in the ledger

### Metadata keys

Both keys are attached to the **`open` directive** of the account, as ordinary metadata lines (four-space indent, quoted string value), which Beancount attaches to the `Open` entry's `meta` map:

- `bean-ai-use` — **opt-in marker.** The only accepted value is `"recursive"`: the account *and all of its descendants that are themselves `open`ed and open (or closed-after-run) on the run date* are included in the account list. Any other value is a validation error (fail-stop, see Edge cases). Accounts with no `bean-ai-use` metadata are not included, *unless* an ancestor marked `"recursive"` includes them (in which case the child's own lack of the key is irrelevant — inclusion is inherited, the marker only needs to exist once, up the tree).
- `bean-ai-rules` — **optional guidance string** for this account only. Shown to the LLM next to the account so it can be steered ("Use for supermarket groceries, including snacks and household consumables"). It does *not* inherit from ancestors. If absent, no guidance is sent for that account.

Example:

```beancount
open Expenses:Food
  bean-ai-use: "recursive"
open Expenses:Food:Groceries
  bean-ai-rules: "Supermarket and grocery runs; includes snacks"
open Expenses:Food:Restaurants
  bean-ai-rules: "Eating out and delivery; not take-away from supermarkets"
open Expenses:Home
  bean-ai-use: "recursive"
open Assets:Cash:CHF
  bean-ai-rules: "Physical cash on hand, Swiss francs"
open Assets:Banks:Main
  bean-ai-use: "recursive"
open Liabilities:Credits:Visa
  bean-ai-rules: "Visa credit card, statement-cycled"
```

In this ledger, a run on a date after all opens and before any close includes: `Expenses:Food`, `Expenses:Food:Groceries`, `Expenses:Food:Restaurants`, `Expenses:Home`, `Assets:Cash:CHF`, `Assets:Banks:Main` (and anything else `open`ed under `Assets:Banks:Main`). `Excluded-by-default` accounts like `Liabilities:Other` are absent unless marked.

### Rules derived from the Beancount model

- A Beancount account receives postings **only if it has an `open` directive** (an unopened reference is a loader validation error). The account universe for the LLM is therefore *by construction* exactly the universe the ledger already accepts postings for.
- An account may be `open`ed, `close`d, `open`ed again. Each `open`/`close` is a separate entry with its own `meta`. The **marker and rules that apply to an account are whatever its *most recent* `open` directive (with date ≤ run date) carries.**
- **Date rules.** An account is *live as of* the run date if:
  - it has an `open` with date ≤ run date, and
  - either it has no `close`, or its latest `close` has date > run date (i.e. it is currently open, and will still be open "at the end of the day" of the run).
  An account whose `open` is in the future is excluded — it does not yet exist.
- `bean-ai-use` **does not inherit**: it must be present on the account's own most recent `open` directive (or on an ancestor's, which is what "recursive" means).
- `bean-ai-rules` **never inherits**, regardless of markers.

## Client-side: `load_live_accounts()`

### Location and signature

A new helper in `beancount_ai/client/beancount_loader.py` (next to `load_transactions` and `load_transaction_contexts`, which it can share the ledger load with):

```python
@dataclass(frozen=True)
class AccountRef:
    """One account offered to the LLM.

    Attributes:
        name: The full account name (e.g. "Expenses:Food:Groceries").
        rule: Optional per-account guidance text (None when the account's
              most recent open directive carries no bean-ai-rules metadata).
    """
    name: str
    rule: str | None


def load_live_accounts(
    main_file: str | Path,
    as_of: date,
) -> list[AccountRef]:
    """Return the list of live accounts from the ledger, for the LLM prompt.

    An account is live if it is open (or was closed after ``as_of``) at
    ``as_of``, and it is (transitively) under an ``open`` directive that
    carries ``bean-ai-use: "recursive"``.  Accounts are returned sorted
    by name.  Each account's ``rule`` is taken from its own most recent
    ``open`` (≤ ``as_of``) carrying ``bean-ai-rules``; it does not inherit.

    Raises on:
      - a parse or validation error in the ledger (propagated);
      - a ``bean-ai-use`` metadata value other than ``"recursive"``
        (ValueError naming the account and the offending value);
      - a non-string ``bean-ai-use`` / ``bean-ai-rules`` value (ValueError).
    """
```

### Algorithm

1. `entries, errors, options = loader.load_file(main_file)`. If `errors` is non-empty, propagate them as a raised error (the ledger must be healthy for us to trust its account graph; a ledger with validation errors can be run by beancount itself, but our account extraction is only defined for a consistent ledger).
2. Walk `entries` collecting, per account name:
   - its `open` entries (date, meta), sorted by date;
   - its `close` entries (date), sorted by date.
3. For each account, compute:
   - `latest_open` — the `open` entry with the greatest date ≤ `as_of`; if none, the account is not live.
   - `latest_close` — the `close` entry with the greatest date ≤ `as_of`; if any, and `latest_close > latest_open`, the account is closed at `as_of` → not live.
   - `use_marker` — `latest_open.meta.get("bean-ai-use")` (validated, see below);
   - `rules` — `latest_open.meta.get("bean-ai-rules")` (validated, see below).
4. **Inclusion test**: an account is included iff it is live *and* there exists an ancestor (the account itself, its parent, its grandparent, … up to but excluding the root) that is live and carries `use_marker == "recursive"` on its own most recent `open` ≤ `as_of`. Note the ancestor must itself be *live* — an account that was already closed before `as_of` cannot "carry" the marker down, because the LLM would be offered live descendants of a dead account, which is incoherent.
5. **Serialization**: return `list[AccountRef]` sorted by `name`.

### Metadata validation (fail-stop)

While walking `open`/`close` entries (step 3):

- If `bean-ai-use` is present and is not a `str`, or is a `str` other than `"recursive"` → raise `ValueError("invalid bean-ai-use value for <account>: <value!r> (only 'recursive' is accepted)")`. (We do not silently ignore a typo: it is more likely a misconfiguration than a harmless extra.)
- If `bean-ai-rules` is present and is not a `str` (e.g. a number or boolean in the ledger — legal Beancount, wrong here) → raise `ValueError("bean-ai-rules for <account> is not a string: <value!r>")`.
- Validation runs on **every** `open`/`close` entry, not just the selected most-recent one, so a typo anywhere in the file surfaces immediately.

### "Live at execution time" — date details

- `as_of` is `date.today()` in the **client's local time zone** — the same clock that drives everything else in the client (receipt file naming, the associate date window, …). The run date is the day on which `bean-ai` was invoked, per the roadmap item.
- Comparison is by `datetime.date` equality/ordering only (Beancount dates carry no time component).
- "Most recent `open`" is the one with the maximum date among opens with `date ≤ as_of`. If there are multiple opens on the same date, the last in file order wins (beancount itself treats later-same-date directives as the effective ones).

### Where it is called

The account list is needed in exactly the three places that currently read `account_list_file`:

- `client/commands/importcmd.py` — `ImportResult.__init__` (used by `bean-ai ingest` and `bean-ai import`)
- `client/commands/process.py` — `run()` (`bean-ai process <file>`)
- `client/commands/refine.py` — `do_refine_one()` (the per-transaction payload)

All three replace `cfg.beancount.account_list_file.read_text().splitlines()` with a call through a new accessor on `BeancountConfiguration` (below), so no command code references the static file at all.

Because `Configuration.load()` already acquires the exclusive advisory lock on `main_file` at startup, the ledger read here sees a stable file — no `FileGuard` re-snapshot is needed for the *read* (write-guarding is a separate roadmap item and out of scope).

## Client-side: `BeancountConfiguration` accessor

`beancount_ai/client/config.py`:

```python
class BeancountConfiguration:
    ...
    def accounts_for_prompt(self, run_date: date | None = None) -> list[AccountRef]:
        """Return the account list to send to the LLM.

        Queries the ledger via ``load_live_accounts(main_file,
        run_date or date.today())``.  Raises the same errors as
        ``load_live_accounts``, and raises ``RuntimeError("the LLM
        would be offered no accounts")`` when the result is empty —
        see Edge cases.
        """
```

- `run_date` is `None` at every current call site (they all mean "today"); the parameter exists so tests can pin the date.

The `account_list_file` attribute is **removed** from `BeancountConfiguration` entirely, along with its constructor argument. `Configuration.load()`:

- no longer reads `data["beancount"]["account_list_file"]`;
- **if the key is still present** in the config JSON, prints exactly one line to stderr — `warning: beancount.account_list_file is no longer used and will be ignored; mark your accounts with bean-ai-use in the ledger` — and continues (a hard failure here would be annoying: the config is not what this feature is about, and failing to *load* the config over a now-dead key would block every subcommand, including ones that never touch accounts).
- The docstring is updated accordingly (see Configuration below).

`AccountRef` is imported into `config.py` from `beancount_loader` (no new module: it is a data class about ledger data, and colocates with the function that produces it).

## Wire protocol

### New struct

A TypedDict shared by client and server is added to `beancount_ai/structs.py`:

```python
class AccountRef(TypedDict, total=False):
    """One account offered to the LLM.

    ``name`` is required; ``rule`` is optional and omitted when the
    account carries no bean-ai-rules guidance.
    """
    name: str
    rule: str
```

(The client-side dataclass of the same shape in `beancount_loader.py` is the value object the client builds and serializes; the TypedDict is the declared shape of the JSON on the wire. Both stay in sync — a test asserts the JSON round-trip.)

### `beanai.Process`

- **Request (stdin):** a JSON array of `{name, rule?}` objects — replacing today's JSON array of plain strings. Example:

  ```json
  [
    {"name": "Assets:Cash:CHF", "rule": "Physical cash on hand, Swiss francs"},
    {"name": "Expenses:Food:Groceries", "rule": "Supermarket and grocery runs; includes snacks"},
    {"name": "Expenses:Food:Restaurants"}
  ]
  ```

- **Server handler change** (`server/commands/process.py`, `_read_accounts_and_close_stdin`):
  - Parse stdin as a JSON **array of objects**, each object an object with a required `name` string and an optional `rule` string. Any other shape (a string element, a missing/empty `name`, a non-string `name`, a non-string `rule`, a non-array top level) → stderr `error: invalid account list input: <why>` and `sys.exit(1)` (the existing fail-stop behavior is preserved, only the shape being validated changes).
  - No `splitlines()`, no first-line taking, no comment stripping: the input is already one object per line, fully typed. Validation is by JSON type, not by text.
  - Pass the validated list straight to the prompt filler.

- The command's single positional argument (the hex-encoded filename) is unchanged.

### `beanai.Refine`

- **Request (stdin):** `RefineRequest["accounts"]` changes type from `list[str]` to `list[AccountRef]` (shape: same JSON array as above). Everything else in `RefineRequest` (`transaction_text`, `documents`) is unchanged.
- **Server handler change** (`server/commands/refine.py`): the type check for `request["accounts"]` changes from "a list of strings" to "a list of `AccountRef` objects" — same validation and fail-stop behavior as `beanai.Process`.

### Serialization into the prompt (both prompts)

- The server fills `{accounts}` with `json.dumps(account_refs, indent=2)` — the *same* call it makes today, just on a list of objects instead of strings.
- A **new short paragraph** is prepended to the account listing in both prompts (`RECEIPT_CONVERSION_PROMPT.md` and `TRANSACTION_REFINEMENT_PROMPT.md`), directly above the `{accounts}` fence, explaining the fields:

  > Each account in the list is an object with a `name` (the account to use in the transaction) and, optionally, a `rule` (guidance from the user on when to use that account).  Prefer accounts whose `rule` best matches the item; when no `rule` applies, pick the account whose name is most specific.  Do not use any account not listed.

- The existing `Do not imagine accounts not listed.` line remains, unchanged.

Because the two prompt files are **frozen by AGENTS.md** ("do not modify without verifying against docs/specs"), this spec *is* the verification: the modification is limited to (a) inserting this paragraph, and (b) nothing else. The placeholder name `{accounts}` and its surrounding fence are untouched. If the frozen-prompt policy requires a human re-test of the prompt before the change, that is a prerequisite to merge (see Prerequisites).

### Client serialization

The client builds `list[AccountRef]` (dicts, so they serialize directly):

```python
def _to_ref_dict(ref: AccountRef) -> dict[str, str]:
    d: dict[str, str] = {"name": ref.name}
    if ref.rule is not None:
        d["rule"] = ref.rule
    return d
```

- For `beanai.Process`: `json.dumps([_to_ref_dict(r) for r in accounts])` written to stdin (replaces the current `json.dumps(account_list)` in `RemoteVM.process_receipt`).
- For `beanai.Refine`: the `accounts` field of the `RefineRequest` payload is `[{"name": ..., "rule": ...?}, ...]` (replaces the current `list[str]`).

The `RemoteVM.process_receipt` signature changes from `list[str]` to `list[AccountRef]` (the dict form is what crosses the wire; the dataclass is kept on the client side for typing the builder). Alternatively `process_receipt` accepts the already-dict list directly — the spec prefers **dicts on the wire boundary** (`list[dict[str, str]]`) to avoid a second TypedDict/dataclass conversion at the transport edge; the `AccountRef` dataclass lives in `beancount_loader` for the builder, and `RemoteVM.process_receipt` takes `list[dict[str, str]]`.

## Server-side changes (summary)

- `server/commands/process.py`:
  - `_read_accounts_and_close_stdin` → `_read_account_refs_and_close_stdin`, validating the new shape (array of `{name, rule?}`).
  - `run()`: `account_text = json.dumps(account_refs, indent=2)` (was the same call on a string list).
- `server/commands/refine.py`:
  - `run()`: the `accounts` type-check and the `json.dumps` gain the new shape/indent.
- `server/RECEIPT_CONVERSION_PROMPT.md`, `server/TRANSACTION_REFINEMENT_PROMPT.md`: insert the field-explanation paragraph above `{accounts}`; nothing else changes.
- **No server reads the ledger. No server ever parses a Beancount file.** The server's job stays: take typed account objects, inject them verbatim as JSON, call the LLM.

## Prompt-side injection properties

The roadmap item's core requirement is that the account payload reaching the LLM be **controlled and safe**. The properties the design guarantees:

1. **Source is the user's own ledger.** The client is the only component that reads Beancount data, and the only component that composes the account list. The server never re-scans the ledger or re-interprets the text. There is no third-party text reaching the prompt through this path.
2. **No free-text re-parsing server-side.** The old path had the client reading a text file line-by-line and the server re-splitting lines and taking the first line; any account line that contained a newline, a quote, or a leading semicolon could have reshaped the JSON. The new path has *no* textual parsing at all on either side of the wire: the client emits typed JSON, the server validates JSON types only.
3. **Typed values.** `name` and `rule` are JSON strings. A non-string value is rejected before the LLM call. A `name` that is not a syntactically valid Beancount account (e.g. contains spaces) *cannot* happen: the name comes from parsed entries, which the beancount parser already validated. This matches the server's general role (do not interpret), and the prompt's own `Do not imagine accounts not listed` instruction is the user-facing safeguard.
4. **Opt-in, not opt-out.** An untouched ledger with no `bean-ai-use` markers produces an *empty* list, which the client treats as an error (see Edge cases: "no live accounts"). There is no way for a misconfiguration to silently offer the entire tree.
5. **Closed accounts are excluded by the date rule**, so the LLM cannot be pointed at an account that no longer accepts postings.

## Configuration

`~/.config/bean-ai.json` — `beancount` section:

| Field | Type | Required | Description |
|---|---|---|---|
| `beancount.main_file` | `Path` | Yes | Path to the main Beancount ledger. (as today) |
| `beancount.ingestion_destination_file` | `Path \| null` | No | As today. |
| `beancount.account_list_file` | — | **Removed** | No longer read. If still present in a config, a one-line stderr warning is printed and the key is ignored (see `BeancountConfiguration` accessor above). Existing configs keep *loading*; the accounts it pointed to are no longer used. |

The README's `bean-ai.accounts` file description is **deleted** and replaced with the "Marking accounts in the ledger" guidance. The parameter table drops the row.

The server's config is **unaffected**: it takes the account list only via stdin, as before.

## Migration (this is a breaking change)

Existing installs have a static `bean-ai.accounts` file (customarily generated with `bean-query ... 'SELECT distinct account ORDER BY account;'` and annotated with `#`-prefixed comments). Migrating to the ledger:

1. **Generate the marker skeleton.** The `bean-query` listing is the starting point for *which* accounts to mark, but it cannot be used as the source of the metadata: `SELECT distinct account` has no notion of which lines are `open` directives, and it will happily include closed accounts (an account's `close` directive does not remove it from `SELECT account` — the account name still appears in past transactions) and, in a healthy ledger, every unopened name is already a parser error anyway. So: take the distinct-account listing, and for each account you want the LLM to see, add/append to its `open` directive in the ledger:

   ```beancount
   bean-ai-use: "recursive"
   ```

   Placing the marker on a subtree root (e.g. `Expenses:Food`) is the usual edit — one line covers the whole family. Do **not** mark a root that already carries one for its children, and do **not** mark accounts you do not want the LLM to use (closed legacy accounts, bank-reconciliation roots like `Assets:Banks:*:Statement` if you keep those out of the prompt, …).
2. **Move the `#` comments into `bean-ai-rules`.** For each `Expenses:Food # supermarket runs` line in the old file, find `Expenses:Food`'s `open` directive in the ledger and add:

   ```beancount
   bean-ai-rules: "supermarket runs"
   ```

   Rules are per-account and do not inherit (see Design decisions).
3. **Remove `beancount.account_list_file`** from `~/.config/bean-ai.json` and delete the `bean-ai.accounts` file. (Leaving the key in place only produces a stderr warning — the file is simply no longer read.)
4. **Sanity-check** with `bean-ai process --dry-run` style verification is out of scope for this feature (Roadmap §7); the minimum check is to run any account-touching command and confirm exactly the right accounts appear in the prompt (the field-explanation paragraph plus the JSON listing make this easy to grep for in a logged LLM request).

The migration is a one-time ledger edit; it is expected to be slightly more typing than the old `bean-query` workflow (one `open` directive per marked subtree), but the result is version-controlled, per-account-typed, and can never drift.

### Compatibility (what actually breaks)

- **Configs:** loading no longer fails on the removed key; it warns. So existing config *files* load, but the accounts they referenced are no longer used — an un-migrated install will hit the "no live accounts" fail-stop on the first account-touching command, which is the intended nudge to migrate.
- **Wire format:** the server no longer accepts the old string-array shape (array of `{name, rule?}` objects only). Client and server ship in the same package and are updated together (see Rollout), so this is not a cross-version problem in supported deployments.
- **Prompt files:** the two frozen prompts gain one explanatory paragraph each (see Prerequisites) — a behavior-neutral change.
- **Tests:** every test that constructs `BeancountConfiguration(account_list_file=...)` or writes an `accounts.txt` is updated (see Testing plan). This is the bulk of the mechanical churn.

## Client-side changes (file map)

| File | Change |
|---|---|
| `beancount_ai/client/beancount_loader.py` | Add `AccountRef` (frozen dataclass) and `load_live_accounts(main_file, as_of)`; the existing `# type: ignore` module header is extended to cover the new imports; the two `loader.load_file` call sites inside `load_transactions`/`load_transaction_contexts` are left as-is (a load-cache reuse across functions is a perf nicety, not required) |
| `beancount_ai/client/config.py` | Remove `account_list_file` from `BeancountConfiguration` (attribute + `__init__` parameter + docstring); new `accounts_for_prompt(run_date=None) -> list[AccountRef]`; `Configuration.load()` stops reading the `account_list_file` key and warns (once, to stderr) if it is still present in the JSON |
| `beancount_ai/client/commands/importcmd.py` | Replace `beancount.account_list_file.read_text().splitlines()` with `beancount.accounts_for_prompt()`; pass the dict list to `vm.process_receipt` |
| `beancount_ai/client/commands/process.py` | Same replacement |
| `beancount_ai/client/commands/refine.py` | Same replacement; the `RefineRequest["accounts"]` value becomes the dict list |
| `beancount_ai/client/server.py` | `RemoteVM.process_receipt(filename, account_refs: list[dict[str, str]])` — the type change is the only edit; the JSON write is unchanged |
| `beancount_ai/structs.py` | Add `AccountRef` TypedDict; `RefineRequest["accounts"]` type annotation changes to `list[AccountRef]` |

## Server-side changes (file map)

| File | Change |
|---|---|
| `beancount_ai/server/commands/process.py` | `_read_accounts_and_close_stdin` → validate `list[{name, rule?}]`; `run()` uses `json.dumps(..., indent=2)` |
| `beancount_ai/server/commands/refine.py` | `accounts` type-check updated to the new shape; `json.dumps(..., indent=2)` |
| `beancount_ai/server/RECEIPT_CONVERSION_PROMPT.md` | Insert the field-explanation paragraph above the `{accounts}` fence (frozen-prompt exception — see Prerequisites) |
| `beancount_ai/server/TRANSACTION_REFINEMENT_PROMPT.md` | Same paragraph |

## Edge cases

### Client-side

| Scenario | Behavior |
|---|---|
| Ledger has parse or validation errors | `load_live_accounts` raises; the command prints the beancount error(s) to stderr and exits 1. (The ledger must be consistent for the account graph to be meaningful.) |
| `bean-ai-use` value is not `"recursive"` (e.g. `"yes"`, `"all"`, a typo) | `ValueError` naming the account and the value; command exits 1. |
| `bean-ai-use` or `bean-ai-rules` is not a string in the ledger | `ValueError` naming the account; command exits 1. |
| No account carries `bean-ai-use: "recursive"` (the ledger is unmarked) | `accounts_for_prompt()` raises `RuntimeError`; the command prints `Error: no accounts marked bean-ai-use: "recursive" in <main_file>; the LLM would be offered no accounts to post to.  Mark the subtrees you want available (see docs), one 'open' line each.` and exits 1. This is fail-stop on purpose (an empty or near-empty list is almost always a misconfiguration — most commonly, an un-migrated install — and offering nothing to the LLM guarantees a garbage transaction). |
| An included ancestor was `close`d before `as_of` | Its descendants are *not* pulled in by its marker (the marker is only carried by live ancestors). The account is simply not in the list; no error. |
| An account is `open`ed after `as_of` (future-dated open) | Not live, not included. Its `bean-ai-use` is not consulted (its most-recent-open ≤ `as_of` doesn't exist). |
| An account is `close`d after `as_of` (but opened before) | Live, included. (The user has closed it in the ledger for a future date; today it is still open.) |
| An account is `open`ed, `close`d, `open`ed again — markers live on different opens | The most recent `open` ≤ `as_of` wins for both `bean-ai-use` and `bean-ai-rules`. A marker on the first open is ignored if the latest open does not carry it. |
| An unopened account appears in postings but has no `open` directive | Not included (see Design decisions). No warning (the ledger itself is invalid and the parse-error case above already stops the run in that situation if beancount flags it; in edge cases where beancount tolerates it, the account simply does not appear). |
| `account_list_file` is still present in the config | One stderr warning at config-load time, then the key is ignored. No further behavior change. |
| Duplicate `open` on the same account and same date | The last in file order wins (beancount's own resolution), so the marker/rules of the later directive is what applies. |
| An account name is a single component (e.g. just `Expenses`, a bare root) | Included on the same rules as any other name; the ancestor walk stops at the root component (the root has no parent, so it can only be included by its own marker). |
| The same physical file is `main_file` *and* also `ingestion_destination_file` | No interaction with this feature; the ledger load sees the whole file including any pending ingestion, which is correct (an account that was just `open`ed in an unmerged ingestion is already real). |
| Two concurrent `bean-ai` runs | The existing main-file lock serializes them; the ledger read happens under the lock, so both see the same account set. No new locking needed. |

### Server-side

| Scenario | Behavior |
|---|---|
| Stdin is not a JSON array of objects | `error: invalid account list input: <reason>` on stderr, exit 1 (unchanged fail-stop; the shape check tightens) |
| An element's `name` is a non-string or empty | Same as above |
| An element's `rule` is present but a non-string | Same as above |
| An element's `rule` is an empty string | Accepted (a user *can* attach an empty `bean-ai-rules` in the ledger; it is validated as a string on the client and passes through; the prompt filler skips empty-string rules at build time, so they never reach the wire) |
| A `name` string that looks like it contains prompt-injection text (quotes, newlines, `Do not...`) | Sent through verbatim. It is inert data: the JSON encoding escapes it, the LLM sees it as an account name, and the prompt instructs it to use only the listed accounts by name. This is the accepted residual risk: the *user's own ledger* is the source, so a hostile ledger is a hostile user, which is out of threat model. Documented here so it is a conscious decision. |
| The LLM outputs a transaction that uses an account not in the list | Existing client-side behavior is unchanged (the refine flow validates structure only; the process flow trusts the LLM). The prompt's `Do not imagine accounts not listed` line is the guard, as today. Out of scope for this feature. |

### Prompt-side

- The `{accounts}` placeholder and its surrounding ```json fence render the `indent=2` JSON of the object array. A 100-account list at ~60 chars/line is ~1200 characters of prompt text — negligible against the prompt's existing length.
- Multiple `rule` strings for the same account are impossible (one `rule` per account per `open`; only the latest open's `rule` is used).
- The field-explanation paragraph is static English text in the prompt; it does not carry user data, so it cannot be an injection vector.

## Testing plan

Unit tests live under `beancount_ai/tests/`, in a new module `test_load_live_accounts.py`, following the style of `test_beancount_lock.py` (real `BeancountConfiguration` against a `tmp_path` ledger):

1. **Marker on the account itself** — an account with `bean-ai-use: "recursive"` on its own `open` (no other markers anywhere) → included with all its live descendants; unmarked siblings of the root are excluded.
2. **Marker on an ancestor** — marker on `Expenses`, account `Expenses:Food:Groceries` (no marker) → included.
3. **Marker on a live ancestor is required** — marker on `Expenses` which is `close`d before `as_of` → none of its live descendants are pulled in.
4. **Date rules** — (a) `open` in the future → excluded; (b) `close` after `as_of` → included; (c) `close` before `as_of` → excluded; (d) reopen after close, `as_of` in the reopened span → included, with the *latest* open's marker/rules.
5. **`bean-ai-rules` is per-account** — rule on `Expenses:Food`, none on `Expenses:Food:Groceries` → the parent's ref has the rule, the child's does not.
6. **Invalid `bean-ai-use` values** — `"yes"`, `"all"`, a number, `"recursive "` (trailing space) → `ValueError` with account name in the message.
7. **Non-string `bean-ai-rules`** — `42`, `true` → `ValueError`.
8. **Empty ledger (no markers)** → `load_live_accounts` returns `[]`; the *command-level* fail-stop is tested separately.
9. **Unopened accounts** — an account appearing only in postings is not in the result.
10. **Sort order** — result is sorted by account name.
11. **`accounts_for_prompt`** — returns `load_live_accounts(main_file, run_date)`; with `run_date=None` the date is pinned to `date.today()` (test with `freezegun`-free stubbing of `date` or by passing an explicit date); an empty result raises `RuntimeError` with the expected message.
12. **Configuration** — `Configuration.load()` with a legacy config JSON still containing `beancount.account_list_file` → loads, prints exactly one stderr warning line containing `account_list_file is no longer used`, and does not fail; without the key → no warning.
13. **JSON round-trip** — `json.dumps` of a ref with/without `rule` produces the exact `{"name": ...}` / `{"name": ..., "rule": ...}` shapes the server validator accepts.

Server-side, in the existing style (`test_do_refine_server.py` uses a `_run(JSON-str)` helper):

14. **`beanai.Process` validator** — accepts the new array-of-objects shape; rejects: bare string array, object with no `name`, object with a non-string `name`, object with a non-string `rule`, non-array top-level. Existing assertions on the reject-path's exit code (1) and stderr prefix (`error:`) are extended.
15. **`beanai.Refine` validator** — same shapes, against `request["accounts"]`.
16. **Prompt filler** — for a small fixture account list, the rendered prompt contains the `indent=2` JSON and the field-explanation paragraph, and no account text is lost or re-escaped beyond what `json.dumps` does.

**Existing-test updates (mechanical churn, but required):** `test_beancount_lock.py`, `test_import_result.py`, and `test_do_refine.py` all construct `BeancountConfiguration(account_list_file=...)` and write an `accounts.txt` file. Since the attribute is removed, each is updated to: (a) construct `BeancountConfiguration` without it, (b) write a minimal ledger with the needed `open` directives + `bean-ai-use: "recursive"` markers in place of `accounts.txt`, and (c) assert on the *derived* account list where they previously asserted on the file's contents (e.g. `test_import_result.py`'s `test_passes_account_list` asserts `{"name": "Expenses:Food"} in ...` instead of `"Expenses:Food" in ...`). `test_do_refine_server.py`'s payload fixtures change from `["Expenses:Food"]` to `[{"name": "Expenses:Food"}]`. No test's scenario changes, only the fixture format.

Doctests or a `--current-env` Ruff/MyPy pass via `make qa` must be green; `pytest -vv` on the new module plus the three updated ones is the fast loop.

## Prerequisites

- Both prompt files are frozen by `AGENTS.md`. This spec's modification to them is limited to **inserting one short explanatory paragraph** above the `{accounts}` fence in each. If the team's frozen-prompt policy requires a manual LLM test of the conversion flow with the modified prompt before merge, that test is a prerequisite to implementation. (The change is additive and does not alter any extraction or output instruction.)

## Rollout / compatibility ordering

- The client and server ship in the same source package (same `pyproject.toml`, same RPM). They are updated **together** in one release. The wire-format change (strings → objects) is therefore not a cross-version problem in the supported deployment: a new client always talks to a new server, and in the same-VM configuration it spawns one; in the split-VM (qrexec) configuration, the operator updates both VMs with the same RPM.
- The old string-array wire shape is **not** accepted by the new server. The breaking nature of this (both the wire format and the config key) is deliberate and accepted; the release notes must call out the ledger-marking migration, and if split-VM operators are found to lag in updating, the fix is to update both VMs, not to revive the old shape.
- The release notes should point at the "Migration" section of this spec (and the corresponding README subsection) as the how-to.

## Out of scope

- **Backup / atomic write of Beancount files before edit** (Roadmap §3) — a separate feature; this spec only *reads* the ledger.
- **The interactive ambiguous-match picker in `associate`** (Roadmap §5) — no change; `associate` does not send an account list to the LLM (its two prompts use receipt info and candidates, not accounts).
- **Retrying `RemoteVM` calls** (Roadmap §7) — no change.
- **A `bean-ai accounts` CLI subcommand** (a read-only "show me what the LLM would see") — a natural follow-up once `load_live_accounts` exists; not part of this change.
- **`bean-ai-use: "yes"` (self-only opt-in)** — rejected (see Design decisions).
- **Inheritance of `bean-ai-rules` from ancestors** — rejected (Design decisions).
- **A per-account `max`/priority weight or a currency constraint on the LLM** — out of scope; a future extension of the `AccountRef` shape.
- **Any change to what the *receipt content itself* contributes to the prompt** (the image parts) — untouched.
- **A one-shot migration *tool*** (`bean-ai migrate-accounts <old-file>` that rewrites the ledger) — the migration is expected to be done by hand per spec; an automatic tool is a natural follow-up but is not required (it would have to edit every `open` directive, which is a Beancount-file-write operation and belongs to the file-edit-safety work, Roadmap §3, first).
- **A `--show-accounts` debug flag** on the account-touching commands (print the derived list to stderr, go on as normal) — nice, but the `bean-ai accounts` follow-up subcommand covers it; not part of this change.

## Open questions (for revision)

1. **Fail-stop on empty list** — the spec chooses to *error* when no account is marked, rather than fall back to the whole tree or to an empty list. An alternative is a one-time warning + proceed with an empty list (the LLM will then be forced to use no listed account, which is nonsense). Keep fail-stop? (The spec's answer is yes; the question is logged so it is a deliberate choice in review.)
2. **Marker propagation through closed ancestors is rejected** — the spec requires the *marking* ancestor itself to be live at `as_of`. A looser rule ("any ancestor ever marked, even if closed now") is simpler to state and would let a user mark a long-closed `Expenses:Old` subtree whose *live* re-opened children they still want. The spec's answer is that this is almost certainly a misconfiguration (marked a closed subtree?) and should be surfaced, not silently honored.
3. **`bean-ai-use` on a `close` directive** — Beancount allows metadata on `close`. The spec reads markers only from `open`. If a user puts a marker on the `close` of an account, it is ignored. Should it instead be a *validation error* (to catch the mistake), or silently ignored (spec's current choice: silently ignored, since `close`-time metadata is more idiosyncratically used)?
4. **Migration aids** — the spec expects the user to do the `bean-ai.accounts`-to-ledger migration by hand (one-time). A one-shot helper command or a `--show-accounts` debug flag would be nice (both recorded as Out of scope). Would the team rather have the helper in this change despite the write-beancount-file coupling?
5. **Multiple `rule` strings per account** — the spec sends one. If the user wants several rules, they concatenate them in the ledger into one string. A `list`-typed `bean-ai-rules` is a possible future extension of `AccountRef`; not in scope.

(End of file)
