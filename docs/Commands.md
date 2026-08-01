
# Commands

## pycash-server (server VM)

### Purpose

Lists available receipt files as a JSON document and processes individual receipts by submitting them to Open-WebUI's LLM.

### Subcommand: `pycash.List`

Returns JSON to stdout:

```json
{"receipts": ["M071225_115439.jpg"], "count": 1}
```

On error (e.g., receipts_ingestion_url not found / down), returns `{"error": "..."}` on stderr and exit code 1.

### Subcommand: `pycash.Process`

Takes a receipt filename as a positional argument, reads it from `receipts_ingestion_url`, base64-encodes the image, embeds the full prompt (`RECEIPT_CONVERSION_PROMPT.md`) + receipt data in a multi-part OpenAI compatible message (text_part + image_part), and submits it to Open-WebUI via the `openwebui-client` library.

Uses an explicit `httpx.Client(verify=<system_ca_bundle>/certifi.where())` to support self-signed/internal CAs. The system CA bundle is determined by `ssl.get_default_verify_paths().cafile` (on RHEL/Fedora this resolves to `/etc/pki/tls/cert.pem`).

Produces JSONL output:
- `{"reasoning": <chunk>}` — LLM chain-of-thought / reasoning tokens streamed in real time as they arrive from the LLM's response stream.
- `{"output": <chunk>}` — final Beancount transaction content, emitted after reasoning has completed.
- `{"finish": <reason>}` (e.g., `"stop"`) — signals completion; client should stop processing and output accumulated result.

The server emits each line immediately via `sys.stdout.write("\n")` to minimize latency over the qrexec pipe. It flushes every 10 chunks (`flush_every % 10 == 0`).

## pycash-client (client VM)

### Purpose

CLI tool that communicates with `pycash-server` (either locally or on another VM via qrexec)
to orchestrate receipt list retrieval and individual receipt processing.
It abstracts away the transport layer entirely from the end user.

### Subcommand: `list**

Invokes `pycash-server pycash.List` via the configured transport, then prints one receipt filename per line to stdout.

### Subcommand: `process`

Taking the name of a receipt, it invokes `pycash-server pycash.Process+<filename>` on the server.
Reads the remote JSONL stream line-by-line, prints reasoning chunks to stderr as they arrive.
Stops processing when either `{"finish": ...}` or `error` is received. Accumulates all
`{"output": ...}` delta chunks and joins them into a single string which is printed to stdout
at the end.
