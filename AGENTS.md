# AGENTS.md — beancount-ai

## Structure

```
beancount-ai/
├── docs/*.md                                  # general documentation of the program, features, commands, and use
└── docs/specs/…                               # specs for features in development or developed
│── pyproject.toml                             # Python project definition and configuration ifle
│
├── beancount_ai/server/                       # Runs on server VM which has access to receipts and LLM
│   └── cli.py                                 # beanai.List + beanai.Process subcommands
│   └── pdf.py                                 # PDF→PNG conversion for receipt images
│   └── *_PROMPT.md                            # prompts for LLMs
│
└── beancount_ai/client/                       # Runs on client VM which has Beancount data
    └── cli.py                                 # list + process subcommands → calls server via qrexec/local
    └── beancount_loader.py                    # loads Beancount data
    └── report.py                              # small temporary module to produce reports on modifications of Beancount files
```

Tox (`tox --current-env`) is the test framework, and it runs Ruff, MyPy and pytest.
You can invoke the entire suite of tests using command `make qa`.  If you are iterating
through code changes, first run `pytest -vv` in the project directory to verify much more
quickly which tests are failing.  When those tests are passing, make use of `make qa`
to catch further problems with the code.

## How to run

**bean-ai-server** — runs on the VM that has receipt files + LLM access. CLI subcommands:
- `bean-ai-server beanai.List -d <receipts-dir>`  lists receipts as JSON
- `bean-ai-server beanai.Process <filename>`        processes one receipt via OpenAI-compatible API (produces JSONL output)

**bean-ai** — runs on the VM with Beancount data. CLI subcommands:
- `bean-ai list`        → prints receipt filenames (one per line)
- `bean-ai process <file>` → streams LLM response, prints parsed Beancount tx to stdout

Default config: `~/.config/bean-ai.json`. Both clients also support `--config <path>` and `$BEAN_AI_CONFIG`.

**Local testing**: set `"target_vm": null` in client config so the client spawns the server as a subprocess
(arguments hex-encoded just as if the server were running in a separate VM).

## Configuration (`~/.config/bean-ai.json`)

Both programs read from the same config file by default `~/.config/bean-ai.json` (but see below for more).

Refer to `README.md` for configuration details and values.

## Server-client transport

For security reasons, the software is split into two parts:

1. The client: runs on the virtual machine dedicated to accounting, where all the
   Beancount files reside.
2. The server: runs on the virtual machine that has the receipts, and also access
   to an OpenAI-compatible LLM API that will process the receipts and turn them into
   Beancount-formatted transactions.

- **Same host** (`target_vm: null`): client spawns server via subprocess, passes hex-encoded subcommand + args.
- **Different VM** (qrexec): service endpoint is `<subcommand>+<hex-encoded-args>`. The RPC handler lives at `/etc/qubes/rpc/pycharm-importer` on the server VM.

When server and client are on the same machine (client's `config.json` says `target_vm: null`), then
client spawns server as subprocess and passes subcommand + command line argument directly, albeit encoding
argument as hex before invocation.

When server is on another VM, qrexec communication is used, and the service call endpoint becomes
the subcommand joined with a plus sign to the hex-encoded argument (if needed by the call).

Client has the ability to send stdin to server, and server can respond via stdout.

## Key files (do not change without checking spec)

- `RECEIPT_CONVERSION_PROMPT.md` — tested LLM prompt for receipt→Beancount conversion. Do not modify without verifying against docs/specs.
- `RECEIPT_INFO_PROMPT.md` — also do not change, it's manually tested.
- `RECEIPT_MATCH_PROMPT.md` — same.  Do not change.

## Gotchas

- Config is a singleton per process: calling `Configuration.load()` a second time returns the first result silently. If testing different configs, use separate processes or `--config`.
- Server emits JSONL with no buffering delay (flushes every 10 chunks). Over qrexec this can be slow; client handles line-by-line reading.
- The server CLI accepts `-d` for receipts directory override only for the `List` subcommand when running directly.
