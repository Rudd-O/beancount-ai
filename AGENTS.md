# AGENTS.md — beanhand

## Structure

```
beanhand/
├── docs/*.md                                  # general documentation of the program, features, commands, and use
└── docs/specs/…                               # specs for features in development or developed
│── pyproject.toml                             # Python project definition and configuration ifle
│
├── beanhand/structs.py                    # request/response TypedDicts shared by client and server
│
├── beanhand/server/                       # Runs on the VM(s) which have access to receipts and the LLM
│   ├── documents_cli.py                       # beanhand-documents-server entry point: beanhand.* list/fetch/remove subcommands
│   ├── ai_cli.py                              # beanhand-ai-server entry point: beanhand.* LLM subcommands
│   ├── commands/                              # one module per beanhand.* subcommand
│   │   ├── listcmds.py                        # beanhand.ListUningested / beanhand.ListUnassociated
│   │   ├── process.py                         # beanhand.Process
│   │   ├── fetch.py                           # beanhand.Fetch
│   │   ├── associate.py                       # beanhand.HelpAssociateReceipt
│   │   ├── remove.py                          # beanhand.Remove
│   │   └── refine.py                          # beanhand.Refine
│   ├── config.py                              # server-side configuration
│   ├── llm.py                                 # shared LLM/streaming helpers (file_to_image_parts, …)
│   ├── storage.py                             # WebDAV client helpers
│   ├── pdf.py                                 # PDF→PNG conversion for receipt images
│   └── *_PROMPT.md                            # prompts for LLMs
│
└── beanhand/client/                       # Runs on client VM which has Beancount data
    ├── cli.py                                 # beanhand entry point: build_parser() + dispatch table
    ├── commands/                              # one module per beanhand subcommand
    │   ├── listcmds.py                        # beanhand list-uningested / list-unassociated
    │   ├── ingest.py                          # beanhand ingest
    │   ├── importcmd.py                       # beanhand import
    │   ├── associate.py                       # beanhand associate
    │   ├── refine.py                          # beanhand refine
    │   ├── process.py                         # beanhand process
    │   ├── fetch.py                           # beanhand fetch
    │   ├── remove.py                          # beanhand remove
    │   └── organize.py                        # beanhand organize
    ├── config.py                              # client-side configuration
    ├── beancount_loader.py                    # loads Beancount data (queries / candidate contexts)
    ├── beanfiles.py                           # raw Beancount file ops: tx splitting, doc metadata, receipt organization
    ├── server/                                # one client-side accessor per server program
    │   ├── transport.py                       # shared qrexec/subprocess transport (ServerTransport base)
    │   ├── documents.py                       # DocumentsVM: list/fetch/remove receipts + save/preview helpers
    │   └── ai.py                              # AIVM: process/associate/refine + LLM streaming capture
    └── display.py                             # colored unified-diff printing
```

Tox (`tox --current-env`) is the test framework; it runs doctests, pytest, Ruff and MyPy.
You can invoke the entire suite of tests using command `make qa`.  If you are iterating
through code changes, first run `pytest -vv` in the project directory to verify much more
quickly which tests are failing.  When those tests are passing, make use of `make qa`
to catch further problems with the code.

## How to run

**beanhand-documents-server** — runs on the VM that has the receipt files. Needs only the `documents` config section. CLI subcommands:
- `beanhand-documents-server beanhand.ListUningested` / `beanhand.ListUnassociated`  list receipts as JSON
- `beanhand-documents-server beanhand.Fetch <filename>`  writes one JSON metadata line + the raw receipt bytes to stdout
- `beanhand-documents-server beanhand.Remove <filename>`  deletes a receipt

**beanhand-ai-server** — runs on the VM with access to the LLM. Needs only the `ai` config section; it never touches the receipt storage (receipts arrive inline via stdin). CLI subcommands (all argumentless, input on stdin):
- `beanhand-ai-server beanhand.Process`      `{"accounts": [...], "receipt": {...}}` on stdin; processes a receipt via the OpenAI-compatible API (produces JSONL output)
- `beanhand-ai-server beanhand.HelpAssociateReceipt`  first stdin line: the receipt; then the candidate transactions; matches a receipt against candidates
- `beanhand-ai-server beanhand.Refine`       refines a transaction; request arrives as plain JSON on stdin (produces JSONL output)

**beanhand** — runs on the VM with Beancount data. CLI subcommands:
- `beanhand list-uningested` / `list-unassociated`  → print receipt filenames (one per line)
- `beanhand process <file>` → streams LLM response, prints parsed Beancount tx to stdout
- `beanhand refine <file_path> <target>...` → refine one or more transactions using their linked documents; each target is a 1-based line number (N), an inclusive line range (A-B), or an open range to the end of the file (A-end); targets must be strictly ascending and non-overlapping (see docs/specs/Refine multi-range target specification.md)
- `beanhand ingest` / `import <filename>` / `associate` / `fetch` / `remove` / `organize`

Default config: `~/.config/beanhand.json`. All three programs support `--config <path>` and `$BEANHAND_CONFIG`.

**Local testing**: omit a `vm` key from the `documents` and `ai` sections of the
client config (or omit the sections altogether) so the client spawns each server as a subprocess
(arguments hex-encoded just as if the server were running in a separate VM).

## Configuration (`~/.config/beanhand.json`)

All three programs read from the same config file by default `~/.config/beanhand.json`, but each uses only the
sections it needs: the client uses `beancount` plus the `documents` and `ai` role sections (each optionally
carrying a `vm` key, or an explicit `backend: "qubes"` + `vm`, that names a Qubes VM; absent that the role is
co-located and the attribute resolves to `None`), the documents server uses `documents`,
and the AI server uses `ai` (a server's config section is validated only when it is first accessed, so a
documents-server config need not carry an `ai` section and vice versa).

Refer to `README.md` for configuration details and values.

## Server-client transport

For security reasons, the software is split into three parts:

1. The client: runs on the virtual machine dedicated to accounting, where all the
   Beancount files reside.
2. The documents server: runs on the virtual machine that has the receipts.
3. The AI server: runs on the virtual machine that has access to an
   OpenAI-compatible LLM API that will process the receipts and turn them into
   Beancount-formatted transactions.

The two servers may run on the same or on different VMs. The client fetches any
document an AI operation needs from the documents server and relays it to the AI
server over stdin, so the AI server never touches the receipt storage.

- **Same host** (`*_target_vm: null`): client spawns the server via subprocess, passes hex-encoded subcommand + args.
- **Different VM** (qrexec): service endpoint is `<subcommand>+<hex-encoded-args>`. The RPC handler lives at `/etc/qubes/rpc/beanhand.*` on the target VM (the documents server's handlers run `beanhand-documents-server`, the AI server's run `beanhand-ai-server`).

When a server and the client are on the same machine (client's `config.json` says the matching `_target_vm` key is `null`), then
client spawns that server as subprocess and passes subcommand + command line argument directly, albeit encoding
argument as hex before invocation.

When a server is on another VM, qrexec communication is used, and the service call endpoint becomes
the subcommand joined with a plus sign to the hex-encoded argument (if needed by the call).

Client has the ability to send stdin to server, and server can respond via stdout.

## Key files (do not change without checking spec)

- `RECEIPT_CONVERSION_PROMPT.md` — tested LLM prompt for receipt→Beancount conversion. Do not modify without verifying against docs/specs.
- `RECEIPT_INFO_PROMPT.md` — also do not change, it's manually tested.
- `RECEIPT_MATCH_PROMPT.md` — same.  Do not change.

## Gotchas

- Config is a singleton per process: calling `Configuration.load()` a second time returns the first result silently. If testing different configs, use separate processes or `--config`.
- `BeancountConfiguration` takes an exclusive advisory lock on `main_file` as soon as it is instantiated (and `Configuration.load()` instantiates it). The lock is held until `unlock()` or process exit, so data-modifying subcommands queue up. Consequences: `main_file` must exist at config load time (a missing file raises `FileNotFoundError`); tests that build multiple configs pointing at the same file in one process will block; and two concurrent `beanhand` invocations against the same file serialize.
- Server emits JSONL with no buffering delay (flushes every 10 chunks). Over qrexec this can be slow; client handles line-by-line reading.
- `beanhand refine` refines one or more transactions: you can indicate which transactions to refine by referring to a line number (selects the transaction containing it) or a range A-B (selects every transaction that *begins* on a line within the inclusive span); the range may also be written A-end, where `end` means the end of the file; a line number may point at any line *within* a transaction; lines / ranges must be strictly ascending and non-overlapping. It writes the file once, at the end, only if at least one accepted refinement changed it.
