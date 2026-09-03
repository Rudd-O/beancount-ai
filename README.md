# Beancount AI: AI-powered receipt ingestion for [Beancount](https://beancount.github.io/) ledgers

## Overview

`bean-ai` helps you manage your Beancount accounting data through AI —local or cloud, your choice— in several ways:

* It can import (scanned or photographed) receipts into a Beancount file and organize them coherently.
  * The LLM processes your receipt to extract transaction details and convert it into a Beancount transaction.  `bean-ai` uses that information to file the receipt under the appropriate account folder, and to write the newly-created transaction (complete with `document:` metadata tag linking back to the filed receipt).
* It can automatically associate transactions already in your Beancount files with your receipts.
  * Each receipt is analyzed by the LLM to determine date / amount, then `bean-ai` queries Beancount for matching transactions; the LLM is then directed to identify the correct transaction among the search results.  Finally, `bean-ai` files the receipt appropriately, then adds the `document:` tag to the identified transaction, pointing to the filed receipt.
* It can even help you refine transactions down to the line item.
  * A transaction you identify (by file name and line number — or a range of lines covering several transactions) will be submitted to the LLM, along with all its associated `document:`s, with instructions to enhance the transaction with all the factual detail present in the documents.  `bean-ai` then uses the response of the LLM to rewrite *only* that transaction in your Beancount file.

This lets you have a comprehensive AI-assisted workflow where:

* you continue to use your your favorite importers to import data like bank statements;
* you can quickly add any receipts you scanned to the newly-imported data;
* you can use those added receipts to enhance the imported transactions with lots of detail;
* you can ingest any receipts corresponding to transactions not imported (e.g. cash);
* all of this happens with very little intervention on your part — at best, you'll fix an LLM-made error here and there.

This program imposes no dependency on cloud at all.  Furthermore, you do not need a harness like Codex or OpenClaw; you don't need MCP or any similar complication to use `bean-ai` either; the LLM is never given free / open access to your accounting data — it only ever sees the information that the current task requires, and it isn't allowed to touch anything else.  You *don't* need a frontier model for this — modest 30B parameter models do very well!

*AI use:* You'll need an OpenAI-compatible LLM (private like Open-WebUI / Ollama or cloud like OpenAI) and an API key from your LLM service to be able to use this project.  Furthermore, whatever model you use needs to be capable of *vision*.  Note that, if you use a local (non-cloud) model, your Beancount and receipt data will always be 100% private.

*Receipt source*: in the current iteration of this project, the receipts storage backend only supports WebDAV -- in a future release, local files will be supported as well.

Bug reports, feature requests and pull requests are welcome!

## Quick Start

**Dependencies**: You will need `xdg-open` from the `xdg-utils` package, and your machine will need access to an LLM via Open-WebUI.

**Install**: `pip install .` from this repository is the easiest way.  Alternatives include installing in a virtual environment, or [using pre-built Fedora RPMs](https://repo.rudd-o.com/) which deal with the availability of all required dependencies.  The source contains everything you need to build RPM packages including `rpm` and `deps-fedora` targets, but at least one package is not in Fedora (`python3-openwebui-client`) and is only available in the link on this paragraph.

**Configure** — create `~/.config/bean-ai.json` and `~/.config/bean-ai.accounts` (see [Configuration](#configuration) below) for examples.  You'll need a `documents`, an `ai`, and a `beancount` section.

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

**Refine an existing transaction using its linked documents**:

```bash
# rewrites that transaction on line number 157 based on its linked receipt(s)
bean-ai refine Expenses.beancount 157 # <file_path> <first_line_number>
# or batch refinement, rewrites multiple transactions between two lines
bean-ai refine Expenses.beancount 157 408 # <file_path> <first_line_number> <last_line_number>
# --clear additionally sets the flag of every modified transaction to the clear flag (*)
bean-ai refine Expenses.beancount 157 408 --clear
```

Find a reference to all subcommands in the [Commands](docs/Commands.md) documentation.

## Details

### Batch operation

`ingest`, `refine` and `associate` work interactively by default, but they support batch operation too.  They support flag `--no` which does all the work but never touches your files.  They all also support mode `--yes`, which goes ahead and makes all modifications to your Beancount data, importing receipts into your Beancount folder and deleting them from the source.  Any exceptions processing receipts when using these two flags are printed (summarized) as they take place, and they are printed in detail at the end of the run; normally (in interactive mode), an exception interrupts the whole process at the first failure.

### Protecting your Beancount data

`bean-ai` guards against two ways your ledger could be damaged:

* **Concurrent invocations.** The moment the configuration is loaded (before any subcommand runs), `bean-ai` takes an exclusive advisory lock on your main Beancount file (`beancount.main_file`) and holds it for the duration of the whole subcommand.  If you run `bean-ai` in one terminal while another `bean-ai` (or any other process holding that lock) is still working, the second one prints a notice to standard error and then waits until the first one is done, instead of the two trampling each other's data.  In effect, data-modifying commands queue up one behind the other.
* **Crash-during-write.** Every Beancount file write is flushed and pushed all the way to disk (`fsync`ed) before `bean-ai` moves on, so a crash or power loss cannot leave a half-written ledger.

### Naming convention for receipt files

Imported receipts are saved under `<beancount_folder>/<account_with_slashes>/` with the naming pattern:

```
<YYYY-MM-DD>_<description> — <original_filename>
```

For example, `2026-07-15_Groceries — IMG_1234.jpg`.

### LLMs tested and known to do great work

* qwen3.6-27b through Ollama / Open-WebUI: excellent results
* qwen3.5:35b-A3B through Ollama / Open-WebUI: very good results

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
    "ingestion_destination_file": "imported.beancount",
    "account_list_file": "/home/user/.config/bean-ai.accounts"
  },
  "ai": {
    "api_url": "https://openwebui.example.com/v1",
    "token": "secret-token",
    "model_name": "qwen3.6:35b-a3b"
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

You also need a `bean-ai.accounts` (customarily saved to `~/.config`) with all your expense, liability and asset accounts used in your day-to-day transactions.  A good starting point to make this listing should be:

```sh
bean-query Documents/Accounting/00-beancount.bean 'SELECT distinct account ORDER BY account;'
```

You can append a comment with a space, and a hash sign, and another space to each account, to tune in which circumstances the LLM should consider using that specific account.

### Parameters

| Field | Type | Description |
|---|---|---|
| `beancount.main_file` | `Path` | Path to your main Beancount ledger file. |
| `beancount.ingestion_destination_file` | `Path \| null` | File to append ingested transactions to (relative to `main_file`). Defaults to `main_file` itself. |
| `beancount.account_list_file` | `Path` | File containing the list of accounts to be considered to make transactions when ingesting receipts. |
| `ai.api_url` | `str` | Base URL of the OpenAI compatible instance (example for an Open-WebUI instance running on a bare IP: `http://10.240.6.7/api/`). |
| `ai.token` | `str` | API token for authenticating with the AI API. |
| `ai.model_name` | `str` | Model name to use with the AI API. Must support vision. |
| `documents.username` | `str` | WebDAV username for the receipts data source. |
| `documents.password` | `str` | WebDAV password for the receipts data source. |
| `documents.base_url` | `str` | Base URL of the WebDAV server containing receipts.  As an example using Nextcloud, the base URL would be `https://nextcloud.example.com/remote.php/dav/files/MyUsername`. |
| `documents.uningested_receipts_subfolder` | `str` | Subfolder path on the WebDAV server where new receipts are stored. |
| `documents.unassociated_receipts_subfolder` | `str` | Subfolder path on the WebDAV server where existing receipts (to be associated) are stored. |

### Split `bean-ai` — for Qubes OS users

*Section of interest only to Qubes OS users*

This program supports *split operation* -- receipts and AI access in one VM (the server), Beancount files in another VM (the client).  In this mode, `bean-ai` runs on the qube that has your Beancount files, and will talk to `bean-ai-server` (which it normally does by spawning the process locally) through Qrexec communication channels targeting another VM, to obtain receipt data and talk to your LLM.

To enable this mode of operation:

1. Split your configuration so that client VM only has the `beancount` section, and the server VM has the `documents` and `ai` sections.  The `bean-ai.accounts` file stays in the client.
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
beanai.Refine * financial pim allow
```

If you did everything right, `bean-ai list-unassociated` should show you your unassociated receipts, and everything else will work fine.

### Overriding configuration

The location for configuration can be overridden with command line argument `--config` or environment variable
`$BEAN_AI_CONFIG`.. Resolution order (first match wins):

1. `--config <path>` CLI flag
2. `$BEAN_AI_CONFIG` environment variable
3. Default `~/.config/bean-ai.json`
