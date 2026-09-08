# Spec: Client-side receipts and service role separation

Status: planned.

## Overview

Today a receipt is *only ever* resolved on the server side. Every LLM command
(`beanhand.Process`, `beanhand.HelpAssociateReceipt`) reads its receipt **by
filename** directly from the server's configured storage (the WebDAV or local
`uningested` / `unassociated` folders — see `server/storage.py`), and the storage
commands (`beanhand.List*`, `beanhand.Fetch`, `beanhand.Remove`) do the same. The
client can only *name* a receipt by its server-side filename; it has no way to
hand the backend a receipt it already holds locally.

This is awkward in two ways:

1. The Beancount VM (the client) already holds receipt files — it organizes every
   ingested/associated receipt under `<beancount_folder>/<account>/`. Yet to run
   the LLM on a receipt that is already sitting locally (a fresh scan, a file
   restored from backup, a copy fetched earlier) the user must first push it into
   the server's storage. The client cannot operate on "an arbitrary local receipt."
2. The server conflates two distinct jobs — **storing** receipts and **talking to
   the LLM** — in one program with one config, so the two can never be deployed
   independently. This is the "bunching up of unrelated operations" the spec removes:
   receipts are "forcibly and only ever looked up in the server."

This spec removes both problems. It lets the client operate on **local** receipts
(auto-detected by path) as well as store-stored receipts, and it splits the backend
into two **independent programs** and **independent roles** — a **documents** server
and an **AI** server — that the client reaches through two **Backends** and
orchestrates:

- The **AI role** becomes **content-addressed**: it consumes receipt bytes supplied
  over the wire (base64, over stdin) and never reads storage. This generalizes the
  mechanism `beanhand.Refine` already uses for its linked documents.
- The **documents role** keeps the existing list / fetch / remove surface and is the
  only component that talks to the configured storage backend.
- The **client** is the join point: it reads a local receipt from disk or fetches a
  store receipt from the documents server, and feeds those bytes to the AI role. The
  client also does the post-success cleanup (`store remove`) in the store-receipt case.

The end state is that the documents server, the AI server, and the client are three
distinct roles that can serve independently, orchestrated from the client.

## Architecture Decision Summary

| Decision | Choice |
|---|---|
| Local receipt selection | **Auto-detect by path** — an argument that resolves to an existing local file is read from the client; otherwise it is treated as a document-store-side filename and fetched through the documents Backend |
| Commands routed through the documents Backend | `process`, `import`, `ingest`, and `associate` gain local-receipt support (auto-detect) and use the AI Backend for the LLM. `organize` is **also** a documents-Backend command: it fetches its receipt from the documents Backend whenever the referenced file is not local (and, with this change, reads it locally when it is). (`refine` already works purely on client-local linked documents and is unaffected except for role addressing.) |
| Receipt source for the AI role | **Content-addressed** — the client inlines receipt bytes (base64) over stdin; the AI role has **no storage access** |
| Backend topology | **Two independent programs / two Backends** (documents, AI) plus the client. Each Backend is a transport handle to one program: a qubes VM when the role's backend names a `vm`, or `RemoteVM(None)` (a co-located local subprocess) otherwise. Each role is its own `beanhand-*-server` binary on the server and its own accessor class on the client |
| Role addressing in config | Per-role backend sections `documents` and `ai`: a lone `vm` key (or an explicit `backend: "qubes"` + `vm`) selects the `qubes` backend; otherwise the role is co-located. Legacy top-level `target_vm` is retained and fills in any backend that has no `vm` of its own |

## The three roles

| Role | Program | Runs on | Owns | Provides | Reads storage? | Talks to LLM? |
|---|---|---|---|---|---|---|
| **Client** | `beanhand` | Beancount VM | Beancount data + organized receipts | Orchestration, file edits, user interaction | local disk only | no |
| **Documents** | `beanhand-documents-server` | Receipts VM (or co-located) | Receipt storage (WebDAV / local folders) | `List`, `Fetch`, `Remove` | **yes** | no |
| **AI** | `beanhand-ai-server` | LLM-access VM (or co-located) | LLM API credentials | `Process`, `HelpAssociateReceipt`, `Refine` | **no** | **yes** |

The defining changes:

- **The AI role no longer receives a filename and does not open the store.** It
  receives receipt *content*, inlined by the client.
- **The documents role is its own program** — the single `beanhand-server` binary is
  split into `beanhand-documents-server` and `beanhand-ai-server`, so each can be
  deployed on its own VM with a config that names only its own backend. The documents
  role is the only component that talks to the configured storage backend.
- **The client decides**, per receipt, whether the bytes come from the local disk or
  from the documents server, and always places them in front of the AI.

## Configuration

Configuration fully adopts the notion of **backends**.

### Client (`~/.config/beanhand.json`)

The client config addresses the two server-side roles through two mandatory backend
sections — `documents` (the **documents** role) and `ai` (the **AI** role).

Because different backends must do different things (the documents backend only does
document-store operations, the AI backend only does AI operations), the code must
change so the server command splits into two distinct roles — `beanhand-documents-server`
and `beanhand-ai-server`. This ends the bunching up of unrelated operations in one
server program. Both programs continue to use the same RPC mechanisms (qrexec or local
subprocess) the current server uses, but they are **different programs on the server
side** and are addressed by **different accessor classes on the client side**.  Of course,
the Configuration-related classes on the server also split, because each server command uses
its own section of the file and nothing else.

Unless in the specific case of dealing with a file available in the local file system,
all file operations and AI operations go to the respective backend.

- **`qubes`** — reach the role's VM over qrexec. Selected by a single `vm` key naming
  the target VM (and *only* that key under the section); an explicit
  `"backend": "qubes"` may also be written alongside `vm`.
- **co-located** — spawn the role's server program as a local subprocess on this host.
  The implicit default whenever the backend for the role is other than `qubes`.

The client resolves each section to a single **Backend target**: the VM name for a
`qubes` backend, or `None` for a co-located role. These are exposed as `store_target`
and `ai_target` (each `str | None`) and are what each Backend transport is constructed
from (see "Backends" below).

#### `documents` (documents backend)

- `{ "documents": { "vm": "<name>" } }` → **`qubes`** documents backend; `store_target`
  is `"<name>"` (shorthand: a lone `vm` means qubes).
- `{ "documents": { "backend": "qubes", "vm": "<name>" } }` → **`qubes`** documents
  backend (explicit form); `store_target` is `"<name>"`.
- No `vm` key, or no `documents` key at all → use `local` documents backend;
  `store_target` is `None`. The real documents-store settings (`local`/`webdav`) are
  read from the **server** config by the spawned `beanhand-documents-server`, not by
  the client.

A `qubes` documents backend names only the VM. It is **not legal** to place
storage-backend keys (`local` / `webdav`) under the client's `documents` section when
using `qubes`, and vice versa: whether the store reads a local folder or WebDAV is a
*server-side* detail (see the server section) and never appears in the client config.

Valid configurations for the client's documents section:

* Implied backend `local` (still goes through the server in the case a referenced
file does not exist on the local file system):

```json
{
  "documents": {
    "unassociated_receipts_folder": "/unassociated",
    "uningested_receipts_folder": "/unassociated",
  }
}
```

* Implied backend `qubes` (still goes through the server in the case a referenced
file does not exist on the local file system):

```json
{
  "documents": {
    "vm": "pim-documents",
  }
}
```

* Implied backend `webdav` (still goes through the server in the case a referenced
file does not exist on the local file system):

```json
{
  "documents": {
    "base_url": "https://nextcloud.com/remote.php/dav/User",
    "uningested_receipts_subfolder": "receipt-photos/uningested",
    "unassociated_receipts_subfolder": "receipt-photos/unassociated"
  }
}
```

#### `ai` (AI backend)

- `{ "ai": { "vm": "<name>" } }` → **`qubes`** AI backend (shorthand); `ai_target` is
  `"<name>"`.
- `{ "ai": { "backend": "qubes", "vm": "<name>" } }` → **`qubes`** AI backend (explicit
  form); `ai_target` is `"<name>"`.
- No `vm` key, or no `ai` key → **co-located** AI backend; `ai_target` is `None`.

The real `openai` model/endpoint/credentials are read from the *server* config by the
spawned `beanhand-ai-server`, not by the client.

It is **not legal** to specify AI-backend keys in the client's `ai` section when using
`qubes`, and vice versa (same rule as `documents`: on the client the section names
only the VM, never backend-internal settings).

#### Legacy `target_vm`

If the top-level `target_vm` key is present, it is the fill-in default: any backend
section that does not name its own `vm` is taken to be `qubes` with that VM. An
explicit per-backend `vm` key takes precedence over `target_vm`. This keeps current
single-VM deployments (which set only `target_vm`) working unchanged.

```json
// Today's default — one VM for both roles:
{ "target_vm": "pim", "beancount": { "main_file": "..." } }
//   → store_target="pim", ai_target="pim"

// Separated roles — documents and AI on different VMs:
{ "documents": { "vm": "doc-vm" }, "ai": { "vm": "pim" },
  "beancount": { "main_file": "..." } }
//   → store_target="doc-vm", ai_target="pim"

// Mixed — explicit AI VM, documents falls back to target_vm:
{ "target_vm": "pim", "ai": { "vm": "llm-vm" },
  "beancount": { "main_file": "..." } }
//   → store_target="pim", ai_target="llm-vm"

// Local testing — no backends named; both roles are co-located subprocesses:
{ "beancount": { "main_file": "..." } }
//   → store_target=None, ai_target=None
```

- **Single target (today's default):** only `target_vm` set → both Backends dial it.
- **Co-located roles:** `documents.vm == ai.vm` (or only `target_vm`).
- **Separated roles:** different `vm`s → documents calls go to the documents Backend,
  AI calls to the AI Backend.
- **Local testing:** no `vm` keys anywhere → both Backends are `None`-targeted local
  subprocesses (the existing local-testing mode).

### Server (`~/.config/beanhand.json`, `server/config.py`)

The single server today requires **both** `ai` and `documents` sections. After the
split there are two programs, and each loads and validates only the section it needs:

- `beanhand-documents-server` requires the `documents` section (a real storage backend
  — `local`, folder on disk, or `webdav`). It errors at load if `documents` is absent.
- `beanhand-ai-server` requires the `ai` section (the `openai` backend —
  model/endpoint/credentials). It errors at load if `ai` is absent.
- A section the program does not need may be present but is ignored; a program is
  validated against only its own role, so a config with both sections still works for
  either program.

Neither server section carries a `vm` key: **the server *is* the role**, not a client
dialing out. This is the mirror image of the client's `documents` / `ai` sections,
which carry only the `vm`.

It is **not an error to specify the `beancount` configuration section** in a server
config, but that section is **unused**. The server already does not and must never
attempt any Beancount operations, since the Beancount accounting files are by
definition in the client.

This lets `beanhand-documents-server` run on one VM with a config holding only
`documents`, and `beanhand-ai-server` run on another with a config holding only `ai`.

### Documents server configuration

Valid configurations for the document server's documents section:

* Implied backend `local` (only called when the client can't find the file
  in its local file system):

```json
{
  "documents": {
    "unassociated_receipts_folder": "/unassociated",
    "uningested_receipts_folder": "/unassociated",
  }
}
```

* Implied backend `webdav` (only called when the client can't find the file
  in its local file system):

```json
{
  "documents": {
    "base_url": "https://nextcloud.com/remote.php/dav/User",
    "uningested_receipts_subfolder": "receipt-photos/uningested",
    "unassociated_receipts_subfolder": "receipt-photos/unassociated"
  }
}
```

The `qubes` backend type does not exist on the server — it is meant
to already be a qubes VM that's being called via RPC by the client.

### AI server configuration

Analogous to the section above.

## Client-side receipt reference model

The notion of "a receipt the user pointed at" is unified in one small abstraction so
that `process` / `import` / `ingest` / `associate` / `organize` share the
local-vs-store decision instead of each re-implementing it.

```python
@dataclass(frozen=True)
class ReceiptRef:
    arg: str          # verbatim CLI argument (a store filename or a local path)
    is_local: bool    # True when `arg` resolves to an existing local regular file

    @property
    def filename(self) -> str:   # basename; drives extension/PDF handling + organization

    def load(self, documents) -> FetchedReceipt:
        # local:  read bytes + st_mtime straight from local disk
        # store:  documents.fetch_receipt(self.filename)   (existing store call)
```

- `is_local` is decided by `Path(self.arg).is_file()`. Per the auto-detect decision,
  an argument that is an existing local file is **local** and wins; a bare name that
  is not a local file falls through to a **store-side filename** lookup.
- `load()` is the single branch point for "get me the bytes." Local receipts are read
  with `path.read_bytes()` and `st.st_mtime` (mirroring `FetchedReceipt`), so downstream
  code always sees a `FetchedReceipt` and does not care where the bytes came from.
- The known trade-off of auto-detect is recorded here deliberately: a file whose
  basename exists both locally (in the cwd) **and** in the store is treated as local.
  Users wanting the store copy in that situation can pass a form that is not a local
  path.

A client helper wraps this for the commands:

```python
def load_receipt(ref: ReceiptRef, documents: DocumentsBackend) -> FetchedReceipt
```

### Governing rule — when the client consults a Backend

The client holds exactly two Backends: the **documents Backend** and the **AI
Backend**. The client consults a Backend for any operation that is not on the
client's own Beancount files — **unless** the operation names a file that already
exists locally, in which case the client reads that file directly and no Backend is
involved.

The **documents Backend must be queried** in two situations:

1. the user supplied **no explicit path** — the receipt list itself comes from
   `documents.list_receipts(...)`; and
2. the user-supplied **path is not a local file** — it is a store-side filename to be
   fetched via `documents.fetch_receipt(...)`. (`organize` uses this same rule for its
   non-local-file case.)

This is the "two levels of indirection" the client exercises:

1. **Source** — is this receipt a local file (read directly) or a store filename (go
   to the documents Backend)? Decided by `ReceiptRef` (auto-detect by path).
2. **Transport** — the documents Backend itself is either a remote qubes VM
   (`RemoteVM("<vm>")`) or a co-located subprocess (`RemoteVM(None)`). Decided by the
   `documents` backend config.

In the co-located case the documents Backend is *still* `RemoteVM(None)` — the client
does **not** read the store's folders directly; it spawns a local
`beanhand-documents-server`, exactly as the existing local-testing transport does. The
AI Backend is analogous (`ai` backend → `RemoteVM("<vm>")` or `RemoteVM(None)`).

## Wire protocol and transport

Transport mechanics are unchanged (`client/server.py:RemoteVM._call`): the command
argument is hex-encoded (appended to the subprocess cmd, or joined to the action name
as `<action>+<hex>` for qrexec), and stdin carries plain data. Only the *payload shape*
of the two AI commands changes, and *which Backend the client dials* (and, for
co-located, *which binary it spawns*).

### RPC action names — deliberately unchanged

The qrexec action names (`beanhand.ListUningested`, `beanhand.Process`, …) are kept
as-is. What changes is *which VM a given action is dialed to* (decided per Backend)
and *which program registers it* on the server. This is a deployment detail: the dom0
qrexec policy must allow `beanhand.*` to the VM(s) that actually host the role, and the
`/etc/qubes/rpc/beanhand.*` handlers must dispatch to the matching
`beanhand-documents-server` / `beanhand-ai-server`. Keeping the name surface stable
shields the spec from policy churn.

### Documents commands — unchanged

`beanhand.ListUningested`, `beanhand.ListUnassociated`, `beanhand.Fetch`, and
`beanhand.Remove` keep their current filename (hex-arg) interface and read/write the
configured store. The client now calls them on the **documents Backend**, and the
handler lives in `beanhand-documents-server`.

### AI commands — content-addressed

The receipt bytes move into the stdin payload (base64), reusing the exact structure
`beanhand.Refine` already uses for its linked documents
(`RefineRequestDocument { filepath, data }` in `structs.py`).

New/additional shared wire types (`beanhand/structs.py`):

```python
class ReceiptPayload(TypedDict):
    filename: str      # basename — drives extension/PDF handling and MIME, like file_to_image_parts
    content: str       # base64 of the raw receipt bytes

class ProcessRequest(TypedDict):
    accounts: list[AccountRef]
    receipt: ReceiptPayload
```

- **`beanhand.Process`** (AI): no longer takes a hex filename argument or reads storage.
  The client sends one stdin JSON object, `ProcessRequest`. The handler base64-decodes
  `receipt.content`, calls `file_to_image_parts(receipt.filename, raw)`
  (`server/llm.py`), fills the `{accounts}` placeholder of
  `RECEIPT_CONVERSION_PROMPT.md`, and streams the unchanged `{transaction,
  payment_accounts}` JSONL response. (The prompt file itself is untouched.)

- **`beanhand.HelpAssociateReceipt`** (AI): no longer takes a hex filename argument or
  reads storage. The two-pass flow is preserved, but the receipt arrives on stdin in
  place of the server-side storage read:
  1. **Pass 1 (info):** client writes stdin message `{"receipt": ReceiptPayload}`;
     server base64-decodes, builds image parts, runs `RECEIPT_INFO_PROMPT.md`, and
     streams the `{date, amount}` result. After finishing this pass the server blocks
     on stdin.
  2. **Pass 2 (match):** client writes stdin message `{"candidates": [...]}`; server
     runs `RECEIPT_MATCH_PROMPT.md` and streams the `{matches, ambiguous}` result.

  Keeping one process across both passes means the (possibly large) receipt image is
  transferred **once**, not twice. This also clears the existing `# FIXME split this
  function into two` note — the receipt and the candidates are now symmetric stdin
  inputs rather than a storage read + a stdin read.

- **`beanhand.Refine`** (AI): already content-input (`RefineRequest.documents`);
  unchanged, but addressed to the AI Backend (and now handled by
  `beanhand-ai-server`).

### Backends (two transports, two accessor classes)

A **Backend** is a transport handle to one role. The transport machinery — the
`RemoteVM._call` logic (local subprocess when the target is `None`, qrexec otherwise;
hex-arg encoding; stdin/stdout pipes) — stays shared as the base class `RemoteVM`.
`RemoteVM.__init__(target)` is unchanged; two thin accessor classes add each role's
methods and remember which **program** to spawn when co-located:

```python
# shared transport (local subprocess when target is None, else qrexec)
class RemoteVM:
    server_program: str = "beanhand-server"   # default; overridden per role
    # ... _call() unchanged ...

class DocumentsBackend(RemoteVM):   # server_program = "beanhand-documents-server"
    # list_receipts, fetch_receipt, remove_receipt  (unchanged from today)

class AIBackend(RemoteVM):          # server_program = "beanhand-ai-server"
    # process_receipt, help_associate_receipt, refine
```

The client constructs one of each from its two targets:

```python
documents = DocumentsBackend(cfg.store_target)   # "<vm>" (qubes) or None (co-located)
ai        = AIBackend(cfg.ai_target)             # "<vm>" (qubes) or None (co-located)
```

Method split by role (signatures per the two subsections above):

- **Documents Backend (`documents`)**: `list_receipts`, `fetch_receipt`,
  `remove_receipt` — unchanged from today.
- **AI Backend (`ai`)**:
  - `process_receipt(receipt: ReceiptPayload, account_refs)` — builds the
    `ProcessRequest`, writes it to stdin, streams the response. (Replaces
    `process_receipt(self, filename, account_refs)`; the filename moves inside the
    payload.)
  - `help_associate_receipt(receipt: ReceiptPayload)` — returns the raw `(cmd, proc,
    stdin, stdout)`; the caller writes the receipt message first, then the candidates
    message (mirroring today's candidates-write).
  - `refine()` — unchanged.

The local-subprocess fallback in `RemoteVM._call` changes only in the binary it spawns:
`beanhand-documents-server` for `DocumentsBackend(None)` and `beanhand-ai-server` for
`AIBackend(None)` (today it always spawns `beanhand-server`).

## `--no` and write-operation semantics

`--no` (present today on `ingest`, `associate`, and `refine`) must keep meaning:
**no files are modified anywhere — not on the client, not on the server.**

Concretely, under `--no`:

- The client writes nothing: no Beancount file is appended to or rewritten, and no
  receipt is saved under `<beancount_folder>/<account>/` (the existing
  `action != "import"` short-circuit already prevents `imp.commit()`).
- The client **never issues a `store remove_receipt`** for a store receipt (today's
  `ingest` skips the `remove.run(...)` call when the action is not `import`; this
  behavior is preserved).
- The documents Backend therefore only ever runs its read/list commands
  (`List`/`Fetch`) under `--no` and never its mutating `Remove`; the AI role is
  always read-only with respect to storage (it has none).

This is a hard invariant: a `--no` run may talk to both Backends (to list, fetch, and
run the LLM) but must leave every file on every VM exactly as it found it.

## Per-command flows

### `beanhand process <path>`
Resolve `ReceiptRef`; `fetched = ref.load(documents)`; call
`ai.process_receipt(ReceiptPayload(filename, b64), accounts)`; print the
transaction + main account. For a local receipt there is no documents-server
involvement at all.

### `beanhand import <path>`
`ImportResult.__init__` takes a `ReceiptRef` instead of a filename. It loads the bytes
once (`ref.load`) and uses **those** bytes both for the AI call (inlined) and for
`save_receipt` at commit time — the receipt is no longer fetched a second time by a
server-side storage read. `import` does not delete anything (unchanged): for a store
receipt the store is left alone; for a local receipt there is nothing to remove.

### `beanhand ingest [paths...]`
- With no arguments, the batch list comes from
  `documents.list_receipts("uningested")` (store receipts); each becomes a
  `ReceiptRef` with `is_local=False`.
- With arguments, each argument is resolved via `ReceiptRef`; arguments may be local
  paths or store filenames, and the two may be mixed in one invocation. The existing
  "all store filenames must exist on the store" check applies only to the arguments
  that resolve as store receipts.
- Per-receipt `do_ingest_one`: load bytes (`ref.load`) → `ai.process_receipt`
  (inlined) → diff → prompt (`y/n/p/q`) → on commit, `save_receipt` uses the loaded
  bytes and the tx is appended (unchanged). **Post-success cleanup is conditional:**
  store receipt **and** action is `import` → `documents.remove_receipt(fn)` (with the
  existing rollback-on-failure of `ImportResult`); local receipt → **no** store remove
  (the local file is untouched); `--no`/non-import → **no** store remove (see the
  `--no` section). The `p` preview opens the already-loaded local bytes (no
  `fetch_receipt` round-trip needed) or, for a store receipt, the existing
  `preview_receipt` flow.

### `beanhand associate [paths...]`
- List/resolve receipts exactly as `ingest` (documents list when no args; `ReceiptRef`
  per argument).
- `do_associate_one`: `fetched = ref.load(documents)`; build
  `ReceiptPayload`; `ai.help_associate_receipt(payload)`; read the info pass
  (`{date, amount}`); load Beancount candidates in the `-1/+45` window (unchanged);
  write the candidates message to the same call; read the match pass.
- On an accepted match (and not `--no`): update the transaction's document metadata
  (unchanged), `save_receipt` using the **already-loaded** bytes (no separate
  `fetch_receipt`), and `write_beancount_file` (unchanged). **Post-success cleanup is
  conditional** as in `ingest`: store receipt → `documents.remove_receipt`; local
  receipt → none; `--no` → nothing modified.

### `beanhand organize <filename> <date> <account>`
`organize` **does** use the documents Backend, and with this change it gains the same
auto-detect as the other receipt commands. It resolves a `ReceiptRef` from
`<filename>`: when the file is not local it fetches via the documents Backend
(replacing today's unconditional `vm.fetch_receipt(filename)`); when it **is** local it
reads the bytes from disk (no documents-Backend round-trip). It then `save_receipt`s
the bytes into the predicted account folder and prints the destination — no remove and
no AI call, unchanged otherwise.

### `beanhand refine <file> <targets...>`
Unchanged in behavior. It is the model this spec generalizes: client-local linked
documents are read and inlined, and the call is now simply addressed to the AI Backend
rather than the single `target_vm`.

## A note on the double-fetch this removes

In the current code a store receipt's bytes cross the wire more than once per import:
`ImportResult` calls `fetch_receipt` (to save the file) **and** `process_receipt`
(which, on the server, re-reads the file from storage). In the content-addressed model
the client loads the bytes **once** and reuses them for both the AI call and the local
save, and the documents store is touched (for a store receipt) only by the initial
`fetch` and the final `remove`.

## Edge cases

| Scenario | Behavior |
|---|---|
| Argument is an existing local file whose basename is also in the store | Treated as **local** (auto-detect wins, per the design decision); the store copy is not touched |
| Argument is a bare name that is neither a local file nor in the store | Store-receipt path: the existing "does not exist on server" error (`ingest`/`associate` pre-checks) or a store `Fetch` failure for single-receipt commands — regardless of whether the documents Backend is qubes or co-located |
| No arguments (batch) — documents Backend must be queried | `documents.list_receipts(...)` is called whether the documents Backend is `RemoteVM("<vm>")` (qubes) or `RemoteVM(None)` (co-located subprocess); the client never reads the store's own folders |
| Local receipt has an unsupported extension (anything but `.jpg`/`.jpeg`/`.png`/`.pdf`) | Reaches the AI role, which skips/warns exactly as it does today for unsupported document formats (the extension check in `server/llm.py` / the refine handler) |
| Large PDF over qrexec (base64 over stdin) | Works — `beanhand.Refine` already ships linked documents the same way; note ~33% base64 size overhead and that qrexec relays stdin/stdout per the existing transport |
| Store `fetch` fails (store receipt) | Propagates as today; no local file is written, no tx appended |
| Local file disappears between resolution and read | `load()` raises; surfaced as an error before any AI call |
| Mixed local + store arguments in one batch | Each is resolved independently; per-receipt success/cleanup is conditional on its own source; batch continue-on-error semantics (`--yes`/`--no`) are unchanged |
| `--no` with a store receipt | `List`/`Fetch`/AI may run, but no client file is written and **no `store remove`** is issued; every VM's files are unchanged |
| Only `target_vm` set, or both Backends resolve to the same VM | Identical to today's single-target behavior — both Backends dial the same target |

## Migration and backward compatibility

- **Same-version deployment:** the AI endpoints' payloads change (filename-arg +
  storage-read → inlined content), and the server binary splits into two programs.
  Because client and server ship as one project and are deployed together (subprocess
  or paired VMs), they are always in lockstep; there is no need to support a new
  client against an old server, and no dual-path support is added. The qrexec action
  names are unchanged, so only the per-VM policy registration and RPC handlers need
  the one-time re-point to the two new programs.
- **Config:** existing configs that only set `target_vm` keep working — both Backends
  resolve to that VM, and the auto-detect rule keeps every non-local argument on the
  store path, so existing workflows are byte-for-byte identical. Co-located local
  testing (`None` target for both Backends) is the existing subprocess mode, now
  spawning the role-appropriate binary.
- **Prompts:** `RECEIPT_CONVERSION_PROMPT.md`, `RECEIPT_INFO_PROMPT.md`, and
  `RECEIPT_MATCH_PROMPT.md` are **untouched** (per the "key files" policy). This spec
  changes only how the receipt image reaches the LLM, not the prompts themselves.

## Work items

Phase A delivers the user-facing feature (local receipts + content-addressed AI) with
backward compatibility in place; both Backends may still target one VM or be co-located.
Phase B lands the independent two-program servers and independent addressing. Items are
grouped by concern rather than strict file order; "touches" lists the concrete files.

### A1. Wire contract (`ReceiptPayload`, `ProcessRequest`)
- Add `ReceiptPayload(filename, content)` and `ProcessRequest(accounts, receipt)` to
  `beanhand/structs.py`. Purely additive; no behavior change.
- **Touches:** `beanhand/structs.py`.

### A2. Client config — backend sections → `store_target` / `ai_target`
- Parse the optional `documents` and `ai` sections into two targets with the rules:
  lone `vm` → qubes → that VM name; explicit `backend: "qubes"` + `vm` → qubes → that
  VM name; anything else (no `vm`, no `documents`/`ai` key) → `None` (co-located).
- `target_vm` fills in as the qubes VM for any section lacking its own `vm`.
- Expose `store_target` and `ai_target` (each `str | None`) on `Configuration`.
- **Touches:** `beanhand/client/config.py`.

### A3. Split the client transport into two Backends
- Keep `RemoteVM` as the **shared transport** (`_call`, hex-arg, local-subprocess vs
  qrexec, streams) and add a `server_program` attribute (default `"beanhand-server"`)
  used by the local fallback.
- Add `DocumentsBackend(RemoteVM)` (`server_program = "beanhand-documents-server"`)
  with `list_receipts` / `fetch_receipt` / `remove_receipt`; and `AIBackend(RemoteVM)`
  (`server_program = "beanhand-ai-server"`) with
  `process_receipt(ReceiptPayload, account_refs)`,
  `help_associate_receipt(ReceiptPayload)`, and `refine()`.
- The local fallback spawns the role's `server_program` instead of always
  `beanhand-server`.
- **Touches:** `beanhand/client/server.py`.

### A4. `ReceiptRef` + `load_receipt` + the two-level rule
- Add the frozen `ReceiptRef` dataclass (auto-detect via `Path(arg).is_file()`,
  `filename` basename, `load(documents)` branching local-read vs
  `documents.fetch_receipt`), and `load_receipt(ref, documents) -> FetchedReceipt`.
- Encode the governing rule: consult the documents Backend iff no path was given or the
  path is not a local file; otherwise read local.
- **Touches:** `beanhand/client/server.py` (a new small module is acceptable if it
  keeps `server.py` focused).

### A5. Make AI server commands content-addressed
- `beanhand.Process`: drop the hex filename argument and the storage read; read a
  `ProcessRequest` from stdin; base64-decode `receipt.content`; build image parts from
  the inlined bytes; stream the existing JSONL response.
- `beanhand.HelpAssociateReceipt`: drop the hex filename argument and storage read;
  read the receipt message (pass 1) then the candidates message (pass 2) from stdin;
  build image parts from the inlined bytes. (Resolves the `# FIXME split this function
  into two` note.)
- `beanhand.Refine`: no logic change (already content-input).
- **Touches:** `beanhand/server/commands/process.py`,
  `beanhand/server/commands/associate.py` (`refine` unchanged).

### A6. Route the four client commands through the two Backends
- `process`: `ref.load(documents)` → `ai.process_receipt(payload, accounts)`.
- `import`: `ImportResult` takes a `ReceiptRef`; load once, reuse bytes for the AI
  call and `save_receipt`; no store-delete.
- `ingest`: batch from `documents.list_receipts` when no args; per-argument
  `ReceiptRef`; inlined AI call; **conditional** `documents.remove_receipt` (store
  receipt **and** action `import`); local preview from loaded bytes.
- `associate`: same resolution; inlined two-pass AI call from loaded bytes; conditional
  store remove; `save_receipt` from loaded bytes.
- `organize`: resolve `ReceiptRef`; when the filename is not a local file,
  `ref.load(documents)` fetches from the documents Backend (replacing the current
  unconditional `vm.fetch_receipt(filename)`); when it is local, the bytes are read
  from disk. `save_receipt` into the account folder then proceeds as today (no remove,
  no AI call); the "organized into" path is unchanged.
- **Touches:** `beanhand/client/commands/process.py`, `importcmd.py`, `ingest.py`,
  `associate.py`, `organize.py`.

### A7. Tests (Phase A) then `make qa`
- `ReceiptRef` resolution + `load_receipt` local/store branches; the two Backends
  (qubes vs `RemoteVM(None)`); client/server `process` and `help_associate_receipt`
  flows with inlined content (extend the `test_do_refine_server.py` patterns);
  conditional store-remove in `ingest`/`associate` **including the `--no` invariant**
  (no client write, no store `remove`); `organize` fetches from the documents Backend
  for a non-local filename and reads locally for a local one. Then `make qa`.
- **Touches:** `tests/` (doctests + pytest).

### B1. Split the server into two programs
- Extract two CLIs from `beanhand/server/cli.py`: `beanhand-documents-server`
  (dispatch `beanhand.List*` / `beanhand.Fetch` / `beanhand.Remove`) and
  `beanhand-ai-server` (dispatch `beanhand.Process` /
  `beanhand.HelpAssociateReceipt` / `beanhand.Refine`).
- `beanhand/server/config.py`: validate per program — documents server requires
  `documents`; AI server requires `ai`; a foreign section is tolerated but ignored;
  the `beancount` section, if present, is unused (not an error).
- Add both console-script entrypoints to `pyproject.toml`.
- Keep the client's local fallback and the qrexec action names aligned to these
  programs (policy + `/etc/qubes/rpc/beanhand.*` re-point is deployment, noted here).
- **Touches:** `beanhand/server/cli.py` (split into modules),
  `beanhand/server/config.py`, `pyproject.toml`, and the co-located fallback in
  `beanhand/client/server.py` (spawn the role's binary).

### B2. Independent routing + deployment
- Confirm `documents.vm` / `ai.vm` route store and AI calls to different targets (the
  client already builds two Backends in Phase A).
- Deployment guidance + role-scoped server configs (documents VM config: only
  `documents`; AI VM config: only `ai`), per-role qrexec policy, and a README /
  `docs/Features.md` topology diagram for the separated layout.
- **Touches:** `docs/Commands.md`, `docs/Features.md`, README.

## Out of scope (deferred)

- **`--dry-run`** as a distinct flag for write operations (Roadmap §8); the existing
  `--no` already covers "do the work but change no files," which this spec preserves.
- **Retry/backoff** on transport calls (Roadmap §8 / Nonissues).
- **Ambiguous-match picker** enablement for `associate` (Roadmap §5).
