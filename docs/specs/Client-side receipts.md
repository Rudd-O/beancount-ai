# Spec: Client-side local receipts

Status: planned.

Scope note: this spec is **only** about the client-side receipt feature — letting
`beanhand` operate on receipt files that live on the client (the Beancount VM's
filesystem) instead of exclusively on receipts known by name to the documents
server. **No wire-protocol, server-side, transport, or configuration changes
are in scope**: in particular, the config file format and the rules
`Configuration.load` applies to the `documents` / `ai` sections are unchanged
by this spec.

## Problem

The client still cannot operate on a receipt it already holds. Every
receipt-taking subcommand (`process`, `import`, `ingest`,
`associate`, `organize`) resolves its argument as a **documents-store filename**:

- `client/commands/process.py:22` — `documents_vm.fetch_receipt(args.filename)` —
  an argument that is a path to a file on the client's own disk fails unless a
  receipt with the same basename also happens to be in the store.
- `client/commands/organize.py:32` — same unconditional `vm.fetch_receipt(...)`.
- `client/commands/ingest.py:36` and `client/commands/associate.py:58` — arguments
  that are not in `list_receipts(...)` are rejected before any work is done
  ("Receipt ... does not exist on server").
- `preview_receipt` (`client/server/documents.py:91`) fetches the receipt from the
  store a second time even though the caller already holds the bytes.

So the client, which is the one component that *owns* organized receipts under
`<beancount_folder>/<account>/`, cannot feed any of its own files to the LLM
without first pushing them into the documents store.

## Outstanding work (checklist)

1. A `ReceiptRef` resolution abstraction (local vs store) shared by all
   receipt-taking commands (Decision D1).
2. `process`, `import`, `ingest`, `associate`, `organize` accept local file paths
   in addition to store filenames (Decisions D1–D5).
3. The documents server is contacted only when the receipt is actually a store
   receipt: no `Fetch`/`List`/`Remove` for local files (Decisions D2, D4).
4. Post-success cleanup is symmetric by source: store receipts are `Remove`d
   from the documents server; local source files are **moved** into the
   Beancount account folder (the committed copy at `dest` + `os.unlink` of the
   source, original mtime preserved) (Decision D4).
 5. Preview uses the already-loaded bytes instead of re-fetching (Decision D5).
 6. Tests + `make qa` (work item W4).

## Design decisions

### D1 — Selection between local and store: auto-detect by path

An argument naming a file that exists on the client's filesystem is treated as a
**local file** and read directly; anything else is a **store filename** and is
resolved through the documents server, exactly as today.

*Detection rule.* Let `arg` be the argument as received (i.e., already
shell-expanded by the caller's shell). The argument is **local** when, after
`os.path.expanduser`, one of the following holds:

- it contains a path separator (`os.sep`), or is `.` or `..` — i.e. it is
  path-like; **and** `os.path.lexists(path)` is true with
  `Path(path).is_file()` deciding whether it is a usable file (a broken symlink
  or a directory is an error, not a silent fall-through to the store); or
- it is a *bare name* (no separator, not `.`/`..`) whose name, resolved against
  the current working directory, is an existing file **and** whose extension is
  one of `VALID_EXTENSIONS` (`.jpg`, `.jpeg`, `.png`, `.pdf`, already defined in
  `beanhand/structs.py`).

Otherwise the argument is a **store filename**.

Justification of the two-part rule:

- *Relative paths must be respected.* If the user writes `beanhand process
  scans/2026-01-01.jpg` or `beanhand ingest /home/me/scan/x.pdf`, the intent is
  unambiguous — it is a path, and if it does not exist that is an error.
  Silently re-interpreting a broken path as a store name would hide typos.
- *Bare names are the conservative case, and the extension gate keeps them
  conservative.* A bare name with no receipt extension keeps **exactly**
  today's behavior: it goes to the store, and if it is not there the user gets
  the existing "Receipt not found" error — whether or not a same-named file sits
  in the CWD. This matters for backward compatibility: a plain
  `is_file()` rule would flip every same-named CWD file (scripts, notes, anything)
  from the store path to the local path, silently processing the wrong file.
  Restricting the flip to `VALID_EXTENSIONS` receipts means (a) only files that
  could plausibly be the intended receipt are read locally, and (b) any bare name
  that is **not** a plausible local receipt behaves byte-for-byte as it does
  today, so existing store-based workflows cannot be disturbed.

Rejection of alternatives:

- *Explicit flag* (`--local`, `--file`): adds a flag to every command and still
  leaves "which source?" as a second dimension the user must maintain; the
  auto-detect rule covers every realistic case with the arguments the commands
  already accept.
- *Explicit prefix* (`@/path/file`): a new mini-syntax that would have to be
  documented and type-checked per command; for one feature it is not worth the
  surface area, and it breaks copy-paste of plain paths from a file manager.
- *Precedence inversion (store wins when the name is in the store)*: would change
  the behavior of existing bare-name invocations that currently always go to the
  store, violating the backward-compatibility requirement below.

The one remaining collision — a bare receipt-extension name that exists **both**
in the CWD and in the store — resolves to **local**, deterministically, and is
surfaced: before any LLM work, the command prints
`Using local file '<arg>'; the documents store also contains '<filename>'` on
stderr. (A `List` call to learn this costs one extra store round-trip only in
this rare, otherwise ambiguous case; for path-like arguments there is no
ambiguity and no such call is made.) There is no command-line form of a bare
name that forces the store reading it — by D2 the store identity of a
receipt *is* its basename, and any path-like form of that name resolves local —
so the escape is operational: re-run from a directory that does not contain a
same-named file and the same invocation takes the store copy. This is an
accepted, documented trade-off, made recoverable by the notice.

### D2 — A single `ReceiptRef` abstraction

The local/store decision is implemented **once** and shared by
`process`/`import`/`ingest`/`associate`/`organize`, in a new module
`beanhand/client/receipts.py` (a new module keeps `beanfiles.py` focused on
Beancount text and the `server/` package focused on transports):

```python
class Source(Enum):
    LOCAL = "local"
    STORE = "store"

@dataclass(frozen=True)
class ReceiptRef:
    src: Source                    # where the bytes come from
    arg: str                       # verbatim CLI argument
    path: Path                     # LOCAL: the resolved path (expanduser'd); STORE: Path(basename)
    filename: str                  # basename; drives naming, extension, and the AI payload

    def load(self, documents: DocumentsClient) -> FetchedReceipt:
        # LOCAL: read the file; FetchedReceipt(data, st.st_mtime)
        # STORE: documents.fetch_receipt(self.filename)
```

- `filename` is `os.path.expanduser(arg)`'s basename in **both** sources. The
  store path's argument already is a bare basename (the server's
  `beanhand.Fetch` itself `basename`s its argument,
  `beanhand/server/documents/commands/fetch.py:14`), so local and store receipts
  are interchangeable downstream: the destination path is built by
  `predict_receipt_destination_path` from `filename`, the `ReceiptPayload` is
  built from `filename` + bytes, and `save_receipt` writes under `filename`.
  Rationale: using the *full local path* as the payload/destination name would
  leak the client's directory layout into Beancount `document:` metadata and into
  the AI prompt, and would make a store→local round-trip produce different file
  names than a local→local one. Basename-on-both-sides keeps every downstream
  behavior byte-identical between sources.
- `load()` centralizes the one branch point ("get me the bytes + a timestamp to
  preserve"). The local branch reads `path.read_bytes()` + `path.stat().st_mtime`
  and wraps them in a `FetchedReceipt`, so **no downstream code can tell where
  the bytes came from** — the timestamp of a local file is preserved by
  `save_receipt`'s existing `os.utime` call exactly as a store receipt's is.
- A broken symlink is detected at construction time (Decision D1) and reported
  with a clear error; `load()` itself only raises `FileNotFoundError` in the
  race case where the file vanishes between resolution and read, which the
  commands surface before any AI call is made.

### D3 — `process` and `import` take one argument; `ingest` and `associate` keep their argument list

No parser changes are needed: `process FILE`, `import FILE`, `organize FILE DATE
ACCOUNT` already accept a single string, and `ingest [FILE...]` /
`associate [FILE...]` accept a list — only the *semantics* of the strings change
from "store filename(s)" to "store filename(s) or local path(s)". The help text
is updated (work item W2) to say "receipt filename (documents store) or path to a
local file".

Justification: a uniform positional argument minimizes the surface a user must
learn and mixes naturally in one batch (`beanhand ingest store-name.jpg
~/scans/x.pdf`), which is the main real-world use case — one scanner output
folder plus whatever the store still holds.

### D4 — A successful ingest/associate consumes every receipt: local files are moved, store receipts are removed

Post-success, an accepted and committed `ingest` / `associate` removes the
receipt from wherever it came from, so the "process → file → done" contract is
identical for both sources:

| Command | Local receipt | Store receipt | `--no` / skip / `n` |
|---|---|---|---|
| `ingest` | **move** the source file to the organized destination (see below) | `documents.remove_receipt(filename)` (existing behavior, incl. the `ImportResult.rollback()` path when the remove fails) | never |
| `associate` | **move** the source file to the organized destination | `documents.remove_receipt(filename)` (existing behavior) | never |
| `import` | nothing (unchanged — `import` never deletes) | nothing (unchanged — `import` never deletes) | n/a |
| `process` | no file writing at all (read-only command) | idem | n/a |

Mechanics for the local case. `save_receipt(dest, fetched)` today *copies* the
bytes to `dest` and applies `fetched.timestamp` via `os.utime`; for a local
source `fetched.timestamp` is exactly the source file's own mtime (Decision D2,
`load()` records `st.st_mtime`), so the existing copy already preserves the
file's real age at the destination. The move adds nothing but the second half:
after the organized copy is safely written, the source file is unlinked. Net
effect: the source path disappears and the file exists only under
`<beancount_folder>/<account>/`, exactly as a store receipt's lifecycle ends
(bytes present in the account folder, original gone).

Implementation note: this makes the local case structurally identical to the
existing store case, in which `save_receipt` followed by `Remove` is already
effectively a move (a cross-machine transfer whose original side is cleaned up
only after the destination side is safely written). The local move is the same
two-step sequence, in the same position in `do_ingest_one` (after a successful
`imp.commit()`, with the cleanup guarded by exactly the rollback path that
currently guards `remove.run`):

1. `save_receipt(dest, fetched)` (unmodified: writes the bytes, preserves the
   source mtime) — the organized copy now exists.
2. `os.unlink(ref.path)` (the source) — performed in `do_ingest_one` where
   today's `remove.run(...)` call sits, with the same guard: if it fails,
   `imp.rollback()` is called and the error is raised, exactly as a failed
   remote remove today. The rollback removes the newly filed copy and restores
   the ledger to its pre-import state; because the unlink is the *second*
   step, the user's original file is still in place for a clean re-run of the
   same command.

   (We do not use `os.rename()` — `beancount_folder` and the scanner's output
   directory may be on different devices, where `os.rename` fails with
   `EXDEV`. The write-then-unlink sequence is portable and works identically
   within and across filesystems. Note the write cannot clobber the source:
   `predict_receipt_destination_path` names the destination as
   `<date>.` + (description + ` — ` +) basename, so the destination filename is
   *strictly longer* than the source basename and the two paths can never
   coincide, even when the source sits inside the account folder.)

Rollback symmetry. `ImportResult.rollback()` on the local case already does
what is needed: it unlinks `receipt_destination_path` if it exists. Since the
original local file has not yet been removed by the time `commit()` writes the
ledger entry, a rollback in the local case simply deletes the newly filed copy
and leaves the user's original untouched — exactly the safe outcome. The
original removal happens only *after* a successful `commit()`, so there is no
window in which the user's source file is gone and the ledger entry is absent.

Justification: the whole point of the documents store's two folders is that a
successful ingest/associate *consumes* the receipt — the bytes appear in the
account folder and the original leaves its uningested/unassociated queue. A
file the user pointed at with `ingest` is a *new* receipt to be processed, and
the user's expectation after a successful import is "my ledger has it and the
account folder has it", not "a copy of it is also still cluttering my scanner
output folder". Moving (not deleting) the local file preserves the same
one-place-per-receipt invariant the store already enforces, and is fully
reversible in spirit: the bytes live on, just in the right place. `--no` keeps
meaning "no file is modified anywhere": for store receipts it still never issues
`Remove` (existing invariant, now trivially preserved because the remove is
gated on `src is STORE and action == "import"`), and for local receipts the
source file is never touched at all. The local move is gated exactly like the
store remove: `src is LOCAL and action == "import"` for `ingest`, and
`src is LOCAL and accepted and not --no` for `associate`.

### D5 — Preview uses the loaded bytes

`preview_receipt` is refactored from
`preview_receipt(vm: DocumentsClient, filename: str, preview_dir: Path)` to
`preview_receipt(fetched: FetchedReceipt, preview_dir: Path)` (it becomes trivially
testable and one line: `save_receipt(preview_dir / fetched-filename, fetched);
open_document(...)`), and the callers pass the `FetchedReceipt` they already
hold. This removes the second `Fetch` round-trip (the "double fetch") for both
sources; in particular a local receipt's preview no longer touches the store at
all. (Today the `p` answer in the `ingest` / `associate` prompts re-fetches
through `DocumentsClient` even though the per-receipt handler already loaded
the bytes — this is the observable bug the refactor fixes.)

Justification: it is a strict reduction in work and in the number of store
contacts, and it makes the "local receipts make zero store contacts" invariant
true end-to-end (today it would not be: local-file preview would fail with a
store "not found" error).

### D6 — No configuration changes

This spec makes **no** change to the config file format or the validation
rules applied to it. In particular, the client's `documents` and `ai`
sections keep exactly the parsing and required-presence rules
`Configuration.load` applies today
(`beanhand/client/config.py:154–183`), as does the documents server's
storage-backend section. A deployment that wants to use local receipts via
this feature should configure the client the same way it does today (the
`documents` and `ai` sections present, or the legacy `target_vm`), and the
local-file argument handling described above layers on top of that existing
configuration without requiring any edit to it.

Rationale: the local/receipt-store decision is purely a per-argument runtime
choice (Decision D1/D2). It does not depend on what the `documents` section
says, because a local file is never read through the documents server; the
`documents` section only continues to answer the unrelated question "if a
receipt *is* a store name, where do its bytes and lifecycle live?" Keeping
config untouched means no migration, no new validation surface, and no change
to what a valid `beanhand.json` looks like — the single most important
property for a behavior layer like this. A follow-up decision to *relax*
config so that a purely local client can omit `documents` entirely is a
separate concern and is deliberately out of scope here.

Two distinct notions that must not be conflated:

- *client-side local file* (this spec): the client reads a path on its own
  filesystem. No documents server involved; no storage config consulted.
- *documents-server storage backend* (`documents.backend: "local"` or
  `"webdav"` in the **server's** config): the documents *server* reads
  uningested/unassociated folders on its own filesystem or WebDAV store; the
  client still reaches it via `Fetch`/`List`/`Remove` (qrexec, or the
  co-located spawn). This already works and is unchanged here.

## Governing rule

The client holds one `DocumentsClient` (access to the documents server) and one
`AIClient` (access to the AI server). It consults the documents client **iff** a
receipt argument resolves to a *store* source (a `Fetch`; for no-argument
batches, a `List`; and the post-success `Remove`). For a *local* source the
client performs zero documents-server calls: no `List`, no `Fetch`, no `Remove`.
The AI server is consulted in exactly the same situations as today (the LLM
work); its wire protocol does not change and carries the bytes either way.

## Per-command flows

### `beanhand process <file>`

`ref = ReceiptRef.resolve(args.filename)`; `fetched = ref.load(documents)`;
`ai.process_receipt(args.filename, fetched, accounts)` (unchanged call shape —
the `AIClient` already inlines `fetched.data` into a `ReceiptPayload`); print the
transaction + main account as today. For a local receipt the documents server is
not contacted at all (the `DocumentsClient` is only passed to `load`).

### `beanhand import <file>`

`ref = ReceiptRef.resolve(args.filename)`; `fetched = ref.load(documents)`;
`ImportResult(documents, ai, cfg.beancount, ref.filename, fetched)`.

`ImportResult.__init__` changes signature: it receives the **already-loaded**
`FetchedReceipt` instead of fetching it itself (`importcmd.py:60` today). The
`DocumentsClient` parameter is retained (callers pass it; it is no longer used
inside `ImportResult` — dropping it is churn, keeping it keeps the constructor
call sites symmetric with `ingest`'s). Everything else in `ImportResult`
(`diff`/`commit`/`rollback`) is unchanged; `commit` continues to
`save_receipt(self.receipt_destination_path, self.fetched_receipt)` from the same
bytes. `import` never deletes from the store (unchanged) and never touches the
source of a local receipt.

### `beanhand ingest [files...]`

- **No arguments:** the batch list comes from `documents.list_receipts("uningested")`
  and every element is a `ReceiptRef` with `src = STORE`. Unchanged (a directory
  of unreferenced local files is *not* a queue — D2; there is deliberately no
  "list local receipts" mode; see Out of scope).
- **With arguments:** replace the current
  "not in `receipts` → error" pre-check (`ingest.py:36`) with, per argument:
  `ref = ReceiptRef.resolve(fn)`; a `LOCAL` ref whose file does not exist is an
  error *before any LLM call* (fail fast, same UX as today's store pre-check);
  a `STORE` ref is no longer validated against the listing — the fetch during
  processing is the single source of truth (this also removes a latent TOCTOU
  mismatch: today the listing and the fetch could disagree; the error messages
  for a missing store receipt remain the documents server's).
- **Per receipt (`do_ingest_one`):** `fetched = ref.load(documents)` →
  `ImportResult(...)` with those bytes → diff → prompt (`y/n/p/q`) as today;
  the `p` answer previews the **already-loaded** `FetchedReceipt` (D5) and
  re-prompts.
- **Commit:** `imp.commit()` as today. **Post-success cleanup (D4):** if
  `ref.src is STORE` → `remove.run(cfg, ...)` with the existing
  rollback-on-failure (`ImportResult.rollback()`); if `ref.src is LOCAL` →
  **move the source file into place**: `save_receipt` has already written the
  organized copy (with the source's mtime preserved), so all that remains is
  `os.unlink(ref.path)`, with the same rollback-on-failure guard
  (`imp.rollback()`; the user's original survives a failed unlink for a clean
  re-run). `--no` / `n` → no commit, no store remove, no source move, nothing
  written (D4).

### `beanhand associate [files...]`

Same resolution as `ingest` (store `List` when no args; `ReceiptRef` per
argument; local-missing = fail fast; store-missing surfaces from the fetch).
`do_associate_one`: `fetched = ref.load(documents)`; the two-pass
`help_associate_receipt` call is already content-inline on the client side
(`AIClient.help_associate_receipt` builds the `AssociateRequest` from the passed
`FetchedReceipt` — `ai.py:105–127`), so it becomes
`ai.help_associate_receipt(ref.filename, fetched)` with the candidate-write and
match-pass logic untouched. On an accepted match (not `--no`): update document
metadata, `save_receipt` from the **already-loaded** bytes, `write_beancount_file`
—all unchanged; then the post-success cleanup of D4: `ref.src is STORE` →
`doc_vm.remove_receipt(receipt)` (as today, `associate.py:334`); `ref.src is
LOCAL` → the same guarded unlink of `ref.path` as `ingest`. `p` previews the
loaded bytes (D5). Note: `associate`'s store-remove is **not**
gated on an "action" the way `ingest`'s is, because `associate` has no
action/draft distinction — its writes happen iff not `--no` and the match was
confirmed; that is preserved exactly, only the source condition is added.

### `beanhand organize <file> <date> <account>`

`ref = ReceiptRef.resolve(args.filename)`; `fetched = ref.load(documents)`;
`save_receipt(predicted_path, fetched)`; print the destination. No AI call and no
remove, unchanged otherwise. The change is precisely that step 2 stops being an
unconditional store fetch (`organize.py:32`).

### `beanhand fetch <name> <destination>` — unchanged by design

`fetch` is a documents-server *pull* command (copy a stored receipt to a path
the user names); its first argument is a store filename and the user has already
expressed the transport, so there is no local/store ambiguity to resolve.
Supporting a local path as its first argument would reduce to a plain `cp`,
which the shell already provides. Likewise `beanhand remove <name>` operates on
store receipts only (nothing local to remove, per D4), and
`list-uningested` / `list-unassociated` list the store queues (D2: local files
are not a queue). `refine` is unaffected (it already reads client-local linked
documents directly).

## `--no` and write-operation semantics (invariant, extended to local sources)

Under `--no` (and under `n` at the prompts), no file is modified anywhere on any
VM. With local receipts that means: the local source file is never read-modified
(it is only read), no Beancount file is written, no copy is made under
`<beancount_folder>/<account>/`, no source file is moved, and — for a store
receipt in the same batch — no `Remove` is issued. The documents server serves
only `List` (argless batch) and `Fetch` (store source) under `--no`. The AI
server is read-only with respect to storage by construction.

## Edge cases

| Scenario | Behavior |
|---|---|
| Bare receipt-extension name exists in both the CWD and the store | Treated as **local** (D1); a one-line stderr notice names the collision so the user can re-choose deliberately |
| Bare name not in CWD (or non-receipt extension) and not in either store folder | Store path; the documents server's existing "Receipt not found" error (the `fetch` handler tries uningested then unassociated) |
| Path-like argument (`scans/x.jpg`, `/abs/x.pdf`, `./x.jpg`) with no such file | Error at resolution, before any LLM or transport call: "file not found" — *not* a store lookup (D1). Rationale: a path the user typed is a path; reinterpreting a broken path as a store name hides typos |
| Argument that is a directory (or broken symlink) | Error at resolution ("not a regular file" / "broken symlink"), not a fall-through |
| No arguments (batch), argless `ingest`/`associate` | `documents.List*` is called as today, whether the documents server is reached via qubes or a co-located process; there is no local-directory listing (Out of scope) |
| Local receipt with an unsupported extension (a path-like arg, since bare names are restricted to receipt extensions by D1) | Bytes reach the AI server; `file_to_image_parts` (`server/llm.py`) applies its existing defaulting/warning exactly as it does today |
| Mixed local + store arguments in one batch | Each argument resolves independently; per-receipt commit/cleanup is conditional on that receipt's own source (D4); the existing continue-on-error (`--yes`/`--no`) and end-of-run error-summary semantics are unchanged |
| Local file deleted between resolution and `load()` | A `FileNotFoundError` is raised, surfaced as an error for that receipt before any AI call (batch: continue-on-error rules apply) |
| Store `Fetch` fails mid-batch | Propagates as an error for that receipt exactly as today (no local file written, no tx appended, no remove attempted) |
| Same local file passed twice in one `ingest` batch | The first receipt *moves* the file out of the source path (D4); the second argument then fails its `load()` with `FileNotFoundError` — "file not found: <arg> (already processed earlier in this run?)" — and is reported under the normal per-receipt error handling (a batch with `--yes`/`--no` continues; interactive mode raises). This is the expected consequence of the move semantics: a consumed receipt leaves its source path |
| Same store filename passed twice in one `ingest` batch | The first receipt removes it from the store; the second receipt's `Fetch` fails with the documents server's existing "Receipt not found" error — the same observable outcome a store receipt has today, so no new behavior |
| Local source sitting inside the account folder (e.g. scanner directory *is* the account folder) | Works: the destination name always carries the `YYYY-MM-DD.` prefix (+ optional description) prepended to the basename, so it can never equal the source path; the file is simply copied to the prefixed name and the source unlinked (D4) |
 | `--no` with store and local receipts mixed | Reads (`List`/`Fetch`/LLM) may occur; no client file is written and no `Remove` is issued for any receipt (D4) |

## Backward compatibility

- *Wire protocol:* unchanged by this spec; no changes to any existing client or
  server wire format.
 - *Behavior of existing invocations:* a bare name that resolves to no local
   receipt-extension file goes to the store exactly as before, so every existing
   workflow — including the `target_vm`-only and co-located local-testing
   deployments — is byte-for-byte unchanged. The only observable change to an
   existing invocation is the one-line stderr notice in the (rare, previously
   unintended) bare-name collision case (D1). The config file format, and the
   validation `Configuration.load` applies to it, are unchanged by this spec
   (Decision D6).
- *Prompts:* `RECEIPT_CONVERSION_PROMPT.md`, `RECEIPT_INFO_PROMPT.md`,
  `RECEIPT_MATCH_PROMPT.md` are untouched.

## Work items

Items are grouped by concern; "touches" lists concrete files.

### W1. `ReceiptRef` + resolution (new module)

- New `beanhand/client/receipts.py`: the `Source` enum and frozen
  `ReceiptRef` per Decision D2, `ReceiptRef.resolve(arg)` implementing the D1
  rule (expanduser, separator classification, `VALID_EXTENSIONS` bare-name
  gate, lexists/is-file checks returning a specific error for each failure kind),
  `load(documents)`, and the collision-notice helper used when a bare name is
  local while a store listing contains the same name.
- **Touches:** `beanhand/client/receipts.py` (new); `beanhand/structs.py`
  (reuse `VALID_EXTENSIONS`; nothing new needed).

 ### W2. Route the commands

- `process`: resolve → load → existing AI call.
- `importcmd`: `ImportResult` takes the `FetchedReceipt` as a constructor
  argument instead of fetching; `run` resolves + loads.
- `ingest` / `associate`: replace the listing-membership pre-check with
  `ReceiptRef.resolve` per argument (store list only for the argless batch);
  load per receipt; pass loaded bytes into `ImportResult` / the existing
  associate flow; make the store-remove conditional on `src is STORE` (and, for
  `ingest`, on action `import` — already the case); preview from loaded bytes.
- `organize`: resolve → load → `save_receipt`.
- `preview_receipt(vm, filename, preview_dir)` → `preview_receipt(fetched,
  preview_dir)` in `beanhand/client/server/documents.py`; update the two
  prompt-loop call sites.
- Update the argument help strings to "receipt store filename or path to a
   local file" (W2 covers all five parsers).
- **Touches:** `beanhand/client/commands/{process,importcmd,ingest,associate,
  organize}.py`, `beanhand/client/server/documents.py`.

 ### W3. Documentation

- `docs/Commands.md`: per-subcommand argument semantics update ("store filename
  or local path"), the D4 cleanup table (successful `ingest`/`associate`
  consumes the source: store `Remove` or local move), and a worked example of
  processing a local scan. README "Details" section: one paragraph — `beanhand`
  can read receipts straight from the client's filesystem; a successful
  `ingest`/`associate` moves the local file into the account folder (leaving
  exactly one copy), just as it removes a store receipt on success.
- **Touches:** `docs/Commands.md`, `README.md`.

 ### W4. Tests (then `make qa`)

- `beanhand/tests/test_receipt_ref.py` (new): the D1 matrix — path-like existing
  (absolute, relative, `./`), path-like missing, directory, broken symlink,
  bare-name extension-gate both ways, expanduser, collision notice emission,
  `load()` local branch (bytes + mtime preserved) and store branch (mock
  `DocumentsClient.fetch_receipt`).
- Extend `beanhand/tests/test_import_result.py`: constructor now receives a
  `FetchedReceipt`; assert no fetch interaction happens inside `ImportResult`
  (the mock `DocumentsClient` must report zero `fetch_receipt` calls).
- `ingest` / `associate` D4-cleanup tests: with fake documents/AI clients and a
  temp Beancount file — store source + commit → `remove_receipt` called once and
  the source file (local case) is unlinked; local source + commit → the source
  is **moved** (source gone, organized copy present with the original mtime,
  no `remove_receipt` call, no documents-client `fetch`/`list`/`remove` at
  all); `--no` + store source → remove never called, no beancount write;
  `--no` + local source → source file still present and unmodified; failed
  local unlink → `ImportResult.rollback()` invoked (newly filed copy removed,
  ledger truncated back) and source file intact; source inside the account
  folder → destination gets the date-prefixed name, source unlinked; same file
  twice in one batch → first moves, second errors with "file not found";
  mixed batch → per-receipt independence.
 - `organize` / `process` single-receipt tests for the local path (no documents
   client constructed or called) and for a store name (documents client called
   exactly once: the fetch; no remove).
 - No config-behavior changes are expected: existing config-loading tests are
   regression-tested as-is (Decision D6).
 - Then `make qa` (doctests, pytest, Ruff, MyPy).
- **Touches:** `beanhand/tests/` (new `test_receipt_ref.py`, edits to
  `test_import_result.py`, new/extended command tests in the existing doctest /
  pytest layout used by `test_do_refine.py` et al.).

## Out of scope (deferred)

- **Listing / batch-processing of a client-local directory** (e.g. an
  `ingest <folder>` or `--from-folder` mode, or an automatic "local inbox"
  folder the argless batch would merge with the store queue). Shell expansion
  (`beanhand ingest ~/scans/*.jpg *.pdf`) already covers the practical case
  through the argument forms accepted here. If wanted, it is a small addition on
  top of `ReceiptRef` (a `resolve` that yields N refs for a directory) and can
  be its own spec item.
- **Filename-collision handling** in `predict_receipt_destination_path` and
  **dedup-before-fetch** (Roadmap §4): applies equally to local and store
  receipts and is intentionally not mixed into this change.
- **Retry/backoff** on transport calls (Roadmap Nonissues).
- **The `associate` ambiguous-match picker** (Roadmap §5): orthogonal to source
  selection; this spec leaves the current ambiguous-match error path untouched.
- **A client-side documents-store implementation** (client reading the server's
  configured `local`/`webdav` folders without spawning the server): the
  co-located-spawn transport already provides that behavior today; a direct
  in-process client-side store would duplicate `server/documents/backends.py`
  for no security or capability gain and is rejected.
- **Relaxing the config so a purely local client may omit the `documents`
  (or `ai`) section entirely.** `Configuration.load` currently requires those
  sections (absent them, the legacy `target_vm` key must be set); per Decision
  D6 this spec does not change that rule. A user whose receipts are 100%
  client-local can still configure the client as today (e.g. a co-located
  `"documents": {}` section or `target_vm`), and will simply never trigger a
  store operation. Loosening the validation is a separate, small follow-up
  decision and is out of scope here.
