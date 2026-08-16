# bean-ai

AI-powered receipt ingestion for [Beancount](https://beancount.github.io/) ledgers.

## Overview

`bean-ai` imports scanned or photographed receipts into a Beancount accounting data set and organizes them coherently. Each receipt is processed by an LLM to extract transaction details, converted into a Beancount transaction, and filed under the appropriate account folder with a `document:` metadata tag linking back to the receipt image.  Receipts for existing transactions can also be imported and associated with the `document:` tag to their corresponding transactions.

You'll need an OpenAI-compatible LLM (e.g. Ollama) to be able to use this project.

## Quick Start

**Install**: `pip install .` from this repository is the easiest way.  Alternatives include installing in a virtual environment, or [using pre-built Fedora RPMs](https://repo.rudd-o.com/) which deal with the availability of all required dependencies.

**Configure** — create `~/.config/bean-ai.json` (see [Configuration](#configuration) below) for examples.  You'll need a `documents`, an `ai`, and a `beancount` section.

**Kick the tires**.  To list various kinds of receipts:

```bash
bean-ai list-uningested        # receipts not yet imported
bean-ai list-unassociated      # receipts not yet linked to a transaction
```

**Import uningested receipts interactively**.  To import scanned receipts (and possibly preview them as you go):

```bash
bean-ai ingest
```

**Associate receipts with existing transactions**:

```bash
bean-ai associate
```

Find a reference to all subcommands in the [Commands](docs/Commands.md) documentation.

## Details

### Batch operation

`ingest` and `associate` support flag `--no` which does all the work but never touches your files.  They both also support mode `--yes` as well, which goes ahead and makes all modifications to your Beancount data, importing receipts into your Beancount folder and deleting them from the source.

### Naming convention for receipt files

Imported receipts are saved under `<beancount_folder>/<account_with_slashes>/` with the naming pattern:

```
<YYYY-MM-DD>_<description> — <original_filename>
```

For example, `2026-07-15_Groceries — IMG_1234.jpg`.

## Configuration

Configuration usually lives in a JSON file: `~/.config/bean-ai.json`

### Sample

Your configuration file must include three sections:

* `beancount`: informs `bean-ai` of your Beancount setup
* `documents`: lets `bean-ai` know where to find your receipts.
* `ai`: informs `bean-ai` of your OpenAI-compatible LLM service.

Here is a sample configuration file:

```json
{
  "beancount": {
    "main_file": "/home/user/Documents/Accounting/main.beancount",
    "ingestion_destination_file": "imported.beancount"
  },
  "ai": {
    "api_url": "https://openwebui.example.com/v1",
    "token": "secret-token",
    "model_name": "meta-llama/llama-3.1-8b-instruct"
  },
  "documents": {
    "username": "dav-user",
    "password": "dav-pass",
    "base_url": "https://dav.example.com/files/Accounting",
    "uningested_receipts_subfolder": "receipts/uningested",
    "unassociated_receipts_subfolder": "receipts/unassociated"
  }
}
```

### Parameters

| Field | Type | Description |
|---|---|---|
| `beancount.main_file` | `Path` | Path to your main Beancount ledger file. |
| `beancount.ingestion_destination_file` | `Path \| null` | File to append ingested transactions to (relative to `main_file`). Defaults to `main_file` itself. |
| `ai.api_url` | `str` | Base URL of the OpenAI compatible instance (example for an Open-WebUI instance running on a bare IP: `http://10.240.6.7/api/`). |
| `ai.token` | `str` | API token for authenticating with the AI API. |
| `ai.model_name` | `str` | Model name to use with the AI API. |
| `documents.username` | `str` | WebDAV username for the receipts data source. |
| `documents.password` | `str` | WebDAV password for the receipts data source. |
| `documents.base_url` | `str` | Base URL of the WebDAV server containing receipts. |
| `documents.uningested_receipts_subfolder` | `str` | Subfolder path on the WebDAV server where new receipts are stored. |
| `documents.unassociated_receipts_subfolder` | `str` | Subfolder path on the WebDAV server where existing receipts (to be associated) are stored. |

### Split `bean-ai` — for Qubes OS users

*Section of interest only to Qubes OS users*

This program supports *split operation* -- receipts and AI access in one VM (the server), Beancount files in another VM (the client).  In this mode, `bean-ai` runs on the qube that has your Beancount files, and will talk to `bean-ai-server` (which it normally does by spawning the process locally) through Qrexec communication channels targeting another VM, to obtain receipt data and talk to your LLM.

To enable this mode of operation:

1. Split your configuration so that client VM only has the `beancount` section, and the server VM has the `documents` and `ai` sections.
2. Ensure both client and server VMs have this program installed.  Remember there are [pre-built Fedora RPMs](https://repo.rudd-o.com/) of the `python3-beancount-ai` package and all its dependencies.
3. Deploy the service files in the `qubes-rpc` folder to `/etc/qubes-rpc` of your server VM.  Depending on where `bean-ai-server` is installed, you may have to adjust the paths in those files.  Ensure all service files are executable.  There [pre-built Fedora RPMs](https://repo.rudd-o.com/) named `python3-beancount-ai-qubes-rpc` that will install these files for you.
4. Add a `target_vm` key in the client configuration, naming the server VM.
5. Allow the client VM access to the Qubes RPC services you deployed.  In the following example, the `pim` VM is the server, and the `financial` VM is the client:

```
# You'd put this e.g. in file /etc/qubes/policy.d/99-bean-ai.policy
# of your dom0 in your Qubes OS installation.
beanai.ListUningested * financial pim allow
beanai.ListUnassociated * financial pim allow
beanai.Process * financial pim allow
beanai.Fetch * financial pim allow
beanai.Remove * financial pim allow
beanai.HelpAssociateReceipt * financial pim allow
```

If you did everything right, `bean-ai list-unassociated` should show you your unassociated receipts.

### Overriding configuration

The location for configuration can be overridden with command line argument `--config` or environment variable
`$BEAN_AI_CONFIG`.. Resolution order (first match wins):

1. `--config <path>` CLI flag
2. `$BEAN_AI_CONFIG` environment variable
3. Default `~/.config/bean-ai.json`
