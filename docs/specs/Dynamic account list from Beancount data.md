# Spec: Dynamic account list from Beancount data

Status: implemented. This spec documents the behavior that is shipped in the code; it describes *what the program does*, not how it is implemented.

This is the specification for the Roadmap item **Prompt injectivity for accounts** (`docs/Roadmap.md`).

## Overview

Today the account list offered to the LLM could be read by the client from a **static file on disk** (`beancount.account_list_file`, customarily `~/.config/beanhand.accounts`), which the user had to keep in sync with the ledger by hand (e.g. via `bean-query ... 'SELECT distinct account ORDER BY account;'`). Ingested accounts could drift out of sync, be closed in the interim, disappear from the file silently, or carry stale per-account hints.

This feature makes the client **derive the account list directly from the Beancount ledger** at run time.  The feature centers around the use of metadata keys in 

1. The list is computed from the parsed ledger, so it can never drift from the data it is describing.
2. The list is formulated **as of a specific date** — only accounts whose `open` directive equals or precedes that date, and that are not yet `close`d as of it, are considered.
3. The list is further filtered down by metadata directives on the `open` directives of each account:
   - `beanhand-include: "yes"` selects that one account.
   - `beanhand-include: "recursively"` selects the account and all its live subaccounts.
   - `beanhand-exclude: "yes"` blocks that specific account from the list.
   - `beanhand-exclude: "recursively"` blocks the account and all its live subaccounts from the list.
4. Per-account guidance that previously lived in `#`-suffixed comments in the static file becomes **typed, quoted string metadata** (`beanhand-rules`), which removes the free-text parsing seam the old format had.
5. The wire format changes from a JSON array of strings (`["Assets:Cash:CHF", ...]`) to a JSON array of **typed objects** (`[{"name": "Assets:Cash:CHF", "rule": "..."}, ...]`), and the server treats them as **inert data**: it validates that `name` (and, if present, `rule`) are strings with no newlines, never re-parses or strips content, and injects them verbatim otherwise. All trust is established client-side from the user's own ledger.
6. The client sends the same list on the `beanhand.Process` *and* `beanhand.Refine` paths, so both LLM prompts see an identical account universe.

This feature **removes** the `beancount.account_list_file` config key outright. That is a **breaking change** for existing installs: they must mark their accounts in the ledger (see Migration) — there is no static-file fallback, on purpose.

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Source of the account list | The parsed ledger's directives | Never drifts from the data it describes; no second source of truth. |
| "Open at a time" | An account qualifies if its `open` date ≤ the run date, and it has no `close` or its latest `close` date > the run date | A closed account must never be offered for new postings; an account closed *after* the run date is still live on that date. |
| Opt-in mechanism | `beanhand-include` / `beanhand-exclude` metadata on the account's **`open` directive**, with values `"yes"` (just this account) or `"recursively"` (this account and all its live descendants) | Opt-in (safe default: nothing is offered) rather than opt-out; a single `beanhand-include: "recursively"` on a subtree root covers all children; a per-account `"yes"` lets the user pick individual accounts without opting into the whole family; `beanhand-exclude` allows blocking specific accounts (or whole subtrees) back out, and an account's own `beanhand-include` re-includes it even inside an excluded subtree. |
| Per-account guidance | `beanhand-rules` metadata (a string) on the `open` directive; **does not inherit** from ancestors | Keeps the prompt tight: each account gets only guidance written for it; avoids re-emitting the same text many times down a subtree. |
| Account types | Every account type is eligible (`Assets`, `Liabilities`, `Income`, `Expenses`, plus custom roots) | The prompt already asks for funding accounts as well as expense accounts; no reason to special-case. |
| Root account (`Expenses`, bare `Assets`) | Included, subject to the same opt-in and date rules | Users mark whatever subtree roots they want; nothing is hardcoded about tree shape. |
| Accounts never `open`ed (only ever referenced in postings) | **Not included**, not offered as a fallback | In Beancount, referencing an unopened account is a *validation error*, so such names can only exist in a broken ledger; including them would make the LLM propose postings that beancount itself rejects. |
| Unopened sub-accounts inside an included, *open*, recursive subtree (e.g. `Expenses:Food:Bakery` when `Expenses:Food` is open and used) | **Not included** | Same reason: an account that has no `open` directive cannot legally receive postings. If the user wants an account available to the LLM, they must `open` it. (A marker has no meaning on an unopened account — there is no directive to carry metadata on.) |
| Metadata on unopened accounts | Impossible in Beancount | Metadata is only attached to directives (`open`/`close`/transactions); there is no directive for an account that was never opened, so there is nothing to annotate, and attempting to "fix" this would produce invalid Beancount. |
| Wire format | JSON array of `{"name": ..., "rule"?: ...}` objects, replacing the array of strings | Gives a stable, self-describing home for account-level text; removes all comment-parsing from both ends; keeps the server free of any ledger-specific interpretation. |
| Prompt filling | The server fills the `{accounts}` placeholder with the indented JSON of the object list; the prompts carry a short note explaining the field meanings | Same mechanism as before (JSON into the prompt), now typed; the note lets the LLM use rules without further explanation. |
| `account_list_file` config key | **Removed.** If it is still present in a config, loading the configuration fails with an error directing the user to mark accounts in the ledger; the command does not run | A hand-maintained static file is the very source of drift and of the untyped free-text seam this roadmap item exists to remove; keeping a silent fallback would keep that path alive forever. The migration from the static file to ledger metadata is a one-time edit (see Migration). |
| Loading cost | The client parses the ledger with the standard Beancount parser; the load happens once per command, under the main-file lock | Negligible; one parse per command, race-free because the client already holds the exclusive advisory lock on the main file for the whole run. |

## Marking accounts in the ledger

### Metadata keys

All three keys are attached to the **`open` directive** of an account, as ordinary metadata lines (four-space indent, quoted string value), which Beancount attaches to the `Open` entry's `meta` map:

- `beanhand-include` — **opt-in marker.** The only accepted values are `"yes"` (select this account only) and `"recursively"` (select this account and all its live descendants). Any other value is a validation error (fail-stop, see Edge cases) naming the account and the offending value. An account with no `beanhand-include` of its own is selected only if a proper ancestor carries `beanhand-include: "recursively"` (inclusion then inherits down the tree; the marker only needs to exist once, up the tree). Closing the ancestor does *not* stop its marker from reaching the live descendants — the policy survives the parent's closure (see Edge cases).
- `beanhand-exclude` — **opt-out marker.** An account selected by `beanhand-include` is removed from the list if it carries a `beanhand-exclude` (or a proper ancestor carries `beanhand-exclude: "recursively"`, even a closed one). However, an account's **own** `beanhand-include` always beats an exclusion — its own or an ancestor's — so you can re-include one specific account inside a subtree that is otherwise excluded.
- `beanhand-rules` — **optional guidance string** for this account only. Shown to the LLM next to the account so it can be steered ("Use for supermarket groceries, including snacks and household consumables"). It does *not* inherit from ancestors. If absent, empty, or whitespace-only, no guidance is sent for that account.

Example:

```beancount
; Recursively opt the whole food family in.
2025-01-01 open Expenses:Food
  beanhand-include: "recursively"
  beanhand-rules: "Supermarket and grocery runs; includes snacks"
2025-01-01 open Expenses:Food:Restaurants
  beanhand-rules: "Eating out and delivery"

; Opt a funding account in individually.
2025-01-01 open Assets:Cash:CHF
  beanhand-include: "yes"
  beanhand-rules: "Physical cash on hand, Swiss francs"

; Opt a bank subtree in recursively, but carve a reconciliation scratch
; account back out.
2025-01-01 open Assets:Banks:Main
  beanhand-include: "recursively"
2025-01-01 open Assets:Banks:Main:Statement
  beanhand-exclude: "recursively"

; An unmarked account: never offered to the LLM.
2025-01-01 open Liabilities:Other
```

In this ledger, a run on a date after all of these opens and before any close offers: `Expenses:Food`, `Expenses:Food:Restaurants`, `Assets:Cash:CHF`, and `Assets:Banks:Main` (plus any other live account beneath it). It does **not** offer `Assets:Banks:Main:Statement` (carved out), nor `Liabilities:Other` (unmarked).

### Rules derived from the Beancount model

- A Beancount account receives postings **only if it has an `open` directive** (an unopened reference is a loader validation error). The account universe for the LLM is therefore, *by construction*, exactly the universe the ledger already accepts postings for.
- An account may be `open`ed, `close`d, `open`ed again. Each `open`/`close` is a separate entry with its own `meta`. The **markers and rules that apply to an account are whatever its most recent `open` directive (with date ≤ run date) carries.**
- **Date rule.** An account is *live as of* the run date when, considering only its `open` and `close` events on or before the run date, the most recent such event is an `open`. (Equivalently: it has an `open` ≤ the run date, and it has no `close` ≤ the run date that post-dates its latest such `open`.) An account whose `open` is in the future, or that is `close`d on or before the run date, is excluded.
- `beanhand-include` and `beanhand-exclude` **do not inherit** unless their value is `recursively`: the marker must be present on the account's own most recent `open`, or on a proper ancestor's (which is what `recursively` means). A *closed* ancestor **can** carry a `recursively` marker down to its live descendants — closing a parent does not revoke the policy for children that remain open (see Edge cases).
- `beanhand-rules` **never inherits**, regardless of markers.
- **Deprecated `bean-ai-*` aliases.** For one release the pre-rename keys `bean-ai-include`, `bean-ai-exclude`, and `bean-ai-rules` are still accepted and behave identically (and may be combined with the `beanhand-*` keys), but they emit a deprecation warning to stderr on load; rename them to the `beanhand-*` forms. Support is scheduled for removal.

## Deriving the account list

The derivation is a pure function of (the main file, the run date) and yields a **sorted** list of `{name, rule?}` objects. It is fail-stop: any condition that would produce a misleading offer raises an error instead of proceeding.

**Live set.** An account is *live as of* the run date when, considering only `open`/`close` events at or before the run date, the most recent such event is an `open`. Its "selected" `open` is the latest `open` at or before the run date (if several share a date, the last one in file order wins).

**Selection.** An account is *selected* if:

- it carries its own `beanhand-include` (any value), **or**
- a *proper* ancestor carries `beanhand-include: "recursively"` — the ancestor need not itself be live; a closed parent's `recursively` marker still selects its live descendants.

**Exclusion.** An account is *excluded* if:

- it carries its own `beanhand-exclude` (any value), **or**
- a *proper* ancestor carries `beanhand-exclude: "recursively"` — likewise, a closed parent's `recursively` exclusion still reaches its live descendants.

**Final membership.** An account appears in the list only if it is live and selected and *(not excluded, or it carries its own `beanhand-include`)*. In short: an account's own `beanhand-include` wins over any exclusion; otherwise selection and exclusion are evaluated from the account and its proper ancestors, regardless of whether those ancestors are themselves live.

**Guidance.** Each listed account carries a `rule` when its selected `open` has a non-empty `beanhand-rules`; the raw string is sent as-is, without inheritance.

**Output.** The result is sorted by account name and sent as `{"name": ...}` / `{"name": ..., "rule": ...}` objects. An empty result is an error (see Edge cases: "no accounts selected").

### "As of" — the run date

The run date is the day the account list is formulated; comparison is by calendar date only (Beancount dates carry no time). Which date is used depends on the command:

- **`ingest` / `import` / `process`** create a *new* transaction, so the list is derived **as of today** — the same local clock that drives receipt naming, the associate date window, and so on.
- **`refine`** edits an *existing* transaction, so the list is derived **as of that transaction's own date** (read from the date on the transaction's header line). This offers the LLM exactly the accounts that were open when the transaction was made. A targeted transaction whose header date cannot be read is an error.

### Where the list is used

The account list is needed wherever a transaction is produced with the LLM:

- `beanhand process <file>` (`beanhand.Process`) — as of today.
- `beanhand ingest` / `beanhand import <filename>` (the import path behind both) — as of today.
- `beanhand refine <file> <targets>…` (`beanhand.Refine`) — as of the refined transaction's own date.
- `beanhand list-accounts [date]` — read-only; as of the supplied date (default today).

The `associate` flow does **not** offer an account list to the LLM (its prompts use receipt info and candidate transactions), so it is unaffected.

## Wire protocol

### Shape

The account list is a JSON array of objects. Each object has a required `name` (a non-empty string, no newlines) and an optional `rule` (a string, no newlines). No other keys are permitted on an element. Example:

```json
[
  {"name": "Assets:Cash:CHF", "rule": "Physical cash on hand, Swiss francs"},
  {"name": "Expenses:Food:Restaurants", "rule": "Eating out and delivery"},
  {"name": "Expenses:Food"}
]
```

### `beanhand.Process`

- **Request (stdin):** the whole request is a single plain-JSON object `{"accounts": <the list above>, "receipt": {"filename": ..., "content": <base64>}}`; the account list is the `accounts` field (no longer a lone array, and no hex-encoded filename argument — the receipt travels inline in the same object).
- **Server handler:** deserializes the request and validates the `accounts` field's shape via `check_account_refs`: the top level must be an array; each element must be an object; each element must carry a non-empty string `name` with no newlines and, optionally, a string `rule` with no newlines, and no other keys. Any other shape (a bare string element, a missing/empty/non-string `name`, a non-string `rule`, an unknown extra key, a non-array top level) is a fail-stop on stderr and exit 1. There is no line splitting, no first-line taking, and no comment stripping — the input is fully typed and validated by JSON type, not by text.

### `beanhand.Refine`

- **Request (stdin):** the `accounts` field of the plain-JSON request payload changes type from a list of strings to the account list above. Everything else in the request (`transaction_text`, `documents`) is unchanged.
- **Server handler:** the `accounts` field is validated with the same `check_account_refs`; a missing or invalid list is a fail-stop (`error: Invalid request: account list missing or invalid: ...`) and exit 1.

### Serialization into the prompt

- The server fills the `{accounts}` placeholder in each prompt with the list rendered as indented JSON.
- Both the receipt-conversion prompt and the transaction-refinement prompt carry a short, static note directly above the `{accounts}` fence, explaining the fields:

  > Each account in the list is an object with a `name` (the account to use in the transaction) and, optionally, a `rule` (guidance from the user on when to use that account). Prefer accounts whose `rule` best matches the item; when no `rule` applies, pick the account whose name is most specific. Do not use any account not listed.

- The existing `Do not imagine accounts not listed.` line remains unchanged.

The two prompts are **frozen by AGENTS.md** ("do not modify without verifying against docs/specs"). This spec *is* the verification: the change to each is limited to inserting that one explanatory paragraph and nothing else; the `{accounts}` placeholder and its surrounding fence are untouched.

### Client serialization

The client builds the sorted list of `{name, rule?}` objects and hands it to the transport: for `beanhand.Process` it becomes the `accounts` field of the stdin JSON request; for `beanhand.Refine` it becomes the `accounts` field of the request payload. The same derivation also feeds `beanhand list-accounts` (see below).

### `beanhand list-accounts` (read-only inspection)

`beanhand list-accounts [date]` prints exactly the account list the LLM would be offered — one `name` per line, with an indented `  rule: <rule>` line beneath it when the account carries one. The optional `date` positional is the "as of" date (calendary comparison only; defaults to today). It runs the same `load_live_accounts`/`account_refs_or_die` path as the other account-touching commands, so it inherits every fail-stop (unmarked ledger, invalid marker, etc.). This is the "show me what the LLM would see" read-only command that was a follow-up to the feature; it lets users verify their ledger markers without running any LLM.

## Server-side behavior (summary)

- The server never reads the ledger and never parses a Beancount file.
- It takes the typed account objects, validates their shape, and injects them verbatim as indented JSON into the prompt, then calls the LLM.
- The only prompt edits are the field-explanation paragraph in the two affected prompts (see above).

## Prompt-side injection properties

The roadmap item's core requirement is that the account payload reaching the LLM be **controlled and safe**. The properties the design guarantees:

1. **Source is the user's own ledger.** The client is the only component that reads Beancount data and composes the account list. The server never re-scans the ledger or re-interprets the text. No third-party text reaches the prompt through this path.
2. **No free-text re-parsing on either side of the wire.** The old path read a text file line-by-line and the server re-split lines and took the first line; any account line with a newline, a quote, or a leading semicolon could have reshaped the JSON. The new path has *no* textual parsing at all: the client emits typed JSON and the server validates JSON types only.
3. **Typed values.** `name` and `rule` are JSON strings; a non-string value is rejected before the LLM call. A `name` that is not a syntactically valid Beancount account cannot occur: the name comes from parsed entries the Beancount parser already validated. The prompt's own `Do not imagine accounts not listed` instruction is the user-facing safeguard.
4. **Opt-in, not opt-out.** An untouched ledger with no `beanhand-include` / `beanhand-exclude` markers produces an *empty* list, which the client treats as an error. There is no way for a misconfiguration to silently offer the entire tree.
5. **Closed accounts are excluded by the date rule**, so the LLM cannot be pointed at an account that no longer accepts postings.

## Configuration

`~/.config/beanhand.json` — `beancount` section:

| Field | Type | Required | Description |
|---|---|---|---|
| `beancount.main_file` | `Path` | Yes | Path to the main Beancount ledger (as today). |
| `beancount.ingestion_destination_file` | `Path \| null` | No | As today. |
| `beancount.account_list_file` | — | **Removed** | No longer read. If it is still present in a config, **loading the configuration fails** with an error directing the user to mark accounts in the ledger, and the command does not run. The key must be removed as part of migration (see Migration). |

The README's `beanhand.accounts` file description is **deleted** and replaced with the "Marking accounts in the ledger" guidance; the parameter table drops the row.

The server's config is **unaffected**: it receives the account list only via stdin, as before.

## Migration (this is a breaking change)

Existing installs have a static `beanhand.accounts` file (customarily generated with `bean-query ... 'SELECT distinct account ORDER BY account;'` and annotated with `#`-prefixed comments). Migrating to the ledger:

1. **Generate the marker skeleton.** The `bean-query` listing is the starting point for *which* accounts to mark, but it cannot be the source of the metadata: `SELECT distinct account` has no notion of which lines are `open` directives, and it will happily include closed accounts (an account's `close` does not remove it from `SELECT account`) — and in a healthy ledger every unopened name is already a parser error. So: take the distinct-account listing, and for each account you want the LLM to see, add/append to its `open` directive in the ledger:

   ```beancount
   2025-01-01 open Assets:Some-account
    beanhand-include: "recursively"
   ```

   Placing the marker on a subtree root (e.g. `Expenses:Food`) is the usual edit — one line covers the whole family. Do **not** mark a root that already carries one for its children, and do **not** mark accounts you do not want the LLM to use (closed legacy accounts, bank-reconciliation roots like `Assets:Banks:*:Statement` if you keep those out of the prompt, …).
2. **Move the `#` comments into `beanhand-rules`.** For each `Expenses:Food # supermarket runs` line in the old file, find `Expenses:Food`'s `open` directive in the ledger and add:

   ```beancount
   beanhand-rules: "supermarket runs"
   ```

   Rules are per-account and do not inherit (see Design decisions).
3. **Remove `beancount.account_list_file`** from `~/.config/beanhand.json` and delete the `beanhand.accounts` file. Unlike before, leaving the key in place now **aborts the command** (the config fails to load with a migration-oriented error), so it must be removed.
4. **Sanity-check.** A `--dry-run`-style verification is out of scope for this feature (Roadmap §7); the minimum check is to run any account-touching command and confirm exactly the right accounts appear in the prompt (the field-explanation paragraph plus the JSON listing make this easy to grep for in a logged LLM request).

The migration is a one-time ledger edit; it is expected to take slightly more typing than the old `bean-query` workflow (one `open` directive per marked subtree), but the result is version-controlled, per-account-typed, and can never drift.

### Compatibility (what actually breaks)

- **Configs:** loading now *fails* on the removed key (it no longer merely warns). Existing config files therefore stop loading until the key is removed — an un-migrated install is nudged to migrate by the migration-oriented error rather than silently dropping the list.
- **Wire format:** the server no longer accepts the old string-array shape (array of `{name, rule?}` objects only). Client and server ship in the same package and are updated together (see Rollout), so this is not a cross-version problem in supported deployments.
- **Prompts:** the two frozen prompts gain one explanatory paragraph each (see Wire protocol) — a behavior-neutral change.
- **Ledger:** an un-migrated, unmarked ledger produces an empty account list, which every account-touching command rejects with the "no accounts selected" fail-stop (see Edge cases).

## Edge cases

### Client-side

| Scenario | Behavior |
|---|---|
| Ledger has parse or validation errors | The derivation refuses to proceed: the command prints the Beancount error(s) and exits 1. (The account graph is only meaningful for a consistent ledger.) |
| `beanhand-include` / `beanhand-exclude` value is not `"yes"` or `"recursively"` (or is not a string) | Validation error naming the account and the offending value; the command exits 1. |
| `beanhand-rules` is not a string in the ledger | Validation error naming the account; the command exits 1. |
| No account is selected (the ledger is unmarked) | The command prints `Error: no accounts marked beanhand-include: "yes" or "recursively" in <main_file>; the LLM would be offered no accounts to post to.  Mark the accounts or subtrees you want available (see docs) on their 'open' directives.` and exits 1. This is fail-stop on purpose — an empty or near-empty list is almost always a misconfiguration (most commonly an un-migrated install), and offering nothing to the LLM guarantees a garbage transaction. |
| An included ancestor was `close`d on or before the run date | The ancestor itself is not in the list (it is not live), but a `recursively` marker on its selected `open` **still** pulls in its live descendants; the include/exclude policy survives the parent's closure. Closing an account revokes nothing from its still-open children. |
| An account is `open`ed after the run date (future-dated) | Not live, not included; its `beanhand-*` metadata is not consulted. |
| An account is `close`d after the run date (but opened before) | Live, included. (The user closed it for a future date; on the run date it is still open.) |
| An account is `open`ed, `close`d, `open`ed again — markers live on different opens | The most recent `open` at or before the run date wins for `beanhand-include`, `beanhand-exclude`, and `beanhand-rules`; a marker only on an earlier open is ignored. |
| An account carries both its own `beanhand-include` and its own `beanhand-exclude` | Included — the own `beanhand-include` beats the exclusion. |
| An unopened account appears only in postings, with no `open` directive | Not included (see Design decisions); no separate warning — the ledger itself is invalid and the parse-error case already stops the run where Beancount flags it. |
| A `beanhand-*` marker is placed on a `close` directive | Markers are read only from `open` directives; a `close`-side marker is ignored (silently — `close`-time metadata is idiosyncratic, and there is no safe interpretation). |
| Duplicate `open` for the same account on the same date | The last in file order wins (Beancount's own resolution), so the later directive's markers/rules apply. |
| An account name is a single component (e.g. bare `Expenses`) | Included on the same rules as any other name; the ancestor walk stops at the root (a root can only be selected by its own marker). |
| The same physical file is `main_file` *and* `ingestion_destination_file` | No interaction: the ledger load sees the whole file including any pending ingestion, which is correct (an account just `open`ed in an unmerged ingestion is already real). |
| Two concurrent `beanhand` runs | The existing main-file lock serializes them; the ledger read happens under the lock, so both see the same account set. No new locking needed. |
| A targeted transaction for `refine` has no parseable header date | An error is raised for that transaction (the account list must be derived as of its own date). |

### Server-side

| Scenario | Behavior |
|---|---|
| Stdin (or `accounts`) is not a JSON array of objects | `error: ... invalid account list ...` on stderr, exit 1 (fail-stop; the shape check is what tightens). |
| An element's `name` is a non-string, empty, or contains a newline | Same as above. |
| An element's `rule` is present but a non-string or contains a newline | Same as above. |
| An element has any key other than `name` / `rule` | Same as above. |
| An element's `rule` is an empty string | The client omits empty/whitespace-only `beanhand-rules` at build time, so an empty `rule` never normally reaches the wire; if one did, the server accepts (it is a string). |
| A `name` / `rule` containing prompt-injection text (quotes, newlines, `Do not…`) | Newlines are rejected; other characters are sent through verbatim. It is inert data: the JSON encoding escapes it, the LLM sees it as account text, and the prompt instructs it to use only the listed accounts by name. This is the accepted residual risk — the *user's own ledger* is the source, so a hostile ledger is a hostile user, which is outside the threat model. Documented as a conscious decision. |
| The LLM outputs a transaction that uses an account not in the list | Unchanged from today: the process flow trusts the LLM and the refine flow validates structure only; the prompt's `Do not imagine accounts not listed` line is the guard. Out of scope for this feature. |

### Prompt-side

- The `{accounts}` placeholder and its `json` fence render the indented JSON of the object array. A 100-account list at ~60 characters/line is ~1200 characters of prompt text — negligible against the prompt's existing length.
- There is at most one `rule` per account (only the latest `open`'s `beanhand-rules` is used); multiple rule strings for one account are impossible.
- The field-explanation paragraph is static text and carries no user data, so it cannot be an injection vector.

## Behavior verification

The behavior above is covered by tests that pin a run date against a fixture ledger and drive the real commands. In summary, the scenarios that must hold:

- **Marker on the account itself** — `beanhand-include: "recursively"` on its own `open` (no other markers) includes the account plus all its live descendants; unmarked siblings of the root stay out.
- **Marker on an ancestor** — `beanhand-include: "recursively"` on `Expenses` includes an unmarked `Expenses:Food:Groceries`.
- **Self-only include** — `beanhand-include: "yes"` includes just that account and does *not* pull in its children.
- **Closed ancestor still propagates** — a `beanhand-include`/`beanhand-exclude: "recursively"` on an ancestor that is `close`d before the run date still pulls in / blocks its *live* descendants; the closed ancestor itself is not in the list.
- **Exclude** — `beanhand-exclude: "yes"` blocks one selected account; `beanhand-exclude: "recursively"` blocks a selected subtree.
- **Carve-out** — an account inside an `beanhand-exclude: "recursively"` subtree that carries its own `beanhand-include` is re-included.
- **Date rules** — (a) future-dated `open` → excluded; (b) `close` after the run date → included; (c) `close` before the run date → excluded; (d) open→close→reopen with the run date inside the reopened span → included, using the *latest* open's markers/rules.
- **`beanhand-rules` is per-account and not inherited** — a rule on a parent reaches no child; an empty/whitespace rule is omitted from the wire.
- **Invalid values** — a `beanhand-include`/`beanhand-exclude` other than `"yes"`/`"recursively"` (including a non-string, and a trailing-space typo) and a non-string `beanhand-rules` all raise a validation error naming the account and value.
- **Unopened accounts** — an account appearing only in postings is not in the result.
- **Sort order** — the result is sorted by account name.
- **Empty ledger** — the derivation returns nothing and the command-level fail-stop fires with the documented error message.
- **Run date per command** — process/import derive as of today; refine derives as of the targeted transaction's own header date.
- **Wire round-trip** — the serialized `{name}` / `{name, rule}` objects are exactly what the server validator accepts, and the rendered prompt contains the indented JSON plus the field-explanation paragraph, with no account text lost or re-escaped beyond the JSON encoding.
- **Server validation** — the server accepts the array-of-objects shape and rejects a bare string array, a missing/non-string/empty/ newline-bearing `name`, a non-string or newline-bearing `rule`, an unknown extra key, and a non-array top level.
- **Legacy config key** — loading a config that still carries `beancount.account_list_file` fails with the migration-oriented error; a config without it loads normally.

## Rollout / compatibility ordering

- The client and server ship in the same source package (same `pyproject.toml`, same RPM) and are updated **together** in one release. The wire-format change (strings → objects) is therefore not a cross-version problem in a supported deployment: a new client always talks to a new server, and in the same-VM configuration it spawns one; in the split-VM (qrexec) configuration the operator updates both VMs with the same RPM.
- The old string-array wire shape is **not** accepted by the new server. The breaking nature of this (both the wire format and the config key) is deliberate and accepted; the release notes must call out the ledger-marking migration, and if split-VM operators lag in updating, the fix is to update both VMs, not to revive the old shape.
- The release notes should point at the Migration section of this spec (and the corresponding README subsection) as the how-to.

## Out of scope

- **Backup / atomic write of Beancount files before edit** (Roadmap §3) — a separate feature; this feature only *reads* the ledger.
- **The interactive ambiguous-match picker in `associate`** (Roadmap §5) — no change; `associate` does not send an account list to the LLM.
- **Retrying transport calls** (Roadmap §7) — no change.
- **A `beanhand list-accounts` CLI subcommand** (a read-only "show me what the LLM would see") — now implemented (see "Client serialization"); an optional `date` argument scopes the derivation.
- **Inheritance of `beanhand-rules` from ancestors** — rejected (Design decisions).
- **A per-account `max`/priority weight or a currency constraint on the LLM** — out of scope; a future extension of the account-object shape.
- **Any change to what the *receipt content itself* contributes to the prompt** (the image parts) — untouched.
- **A one-shot migration *tool*** (that rewrites the ledger from an old static file) — the migration is done by hand per spec; an automatic tool is a natural follow-up but would have to edit every `open` directive, which is a Beancount-file-write operation and belongs to the file-edit-safety work (Roadmap §3) first.
- **A `--show-accounts` debug flag** on the account-touching commands — the `beanhand list-accounts` follow-up subcommand covers it; not part of this change.
