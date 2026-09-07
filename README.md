# Beanhand: from receipts into [Beancount](https://beancount.github.io/) transactions, effortlessly

*Let the clanker think about accounting for you, so you can think of the things you care about.*

`beanhand` is a command-line computer program that assists you with frequent time-consuming tasks, like creating detailed transaction from a receipt, filing receipts with existing transactions, and adding details to transactions based on receipts.  It delegates drudgery like typing or reading receipts to an LLM.  Your data can stay 100% private, if you choose to.  It's open source, free software — you can install and use it on your desktop computer for free.

This program used to be called *Beancount AI*, but that name was neither uniquely identifying nor very good.  Beanhand is a more memorable name — a helping hand for your beans.

## Who is this for?  Is it for *me?*

* Do you dread typing detailed transactions in your ledger by hand, but you'd like them entered in detail anyway?
* Do you have receipts available for some of your transactions?  (The more the merrier.)
* Is importing data into your ledger (e.g. using Beangulp) not saving you as much work as you'd expected?
* Do you have a lot of catching up to do in your accounting?
* Do you ask yourself *how much of that supermarket bill was actually groceries rather than snacks*?
* Are you curious about AI, but worried about unleashing a full agent on your computer, or exposing your financial data to strangers?
* Is the time you can devote to your books limited?

If you answered yes to any of these questions, then **yes, `beanhand` is for you**.

## What is this *not?*

`beanhand` is not a general accounting agent or harness.  It doesn't have open access to your accounting files.  You can't ask it open-ended questions about your ledger, nor ask it to write arbitrary transactions, or scan your ledger in general.

### Do I need to submit my personal info to third parties?  Do I have to "run an AI" on my financial data?

No.  You don't need any cloud, agents, or even a paid AI.  A local AI suffices — a modest 30B model with vision works stellarly.  That said, you can use OpenAI-compatible commercial services if you want.

This program intentionally lives in the region of the assistance spectrum between "manually importing CSV files" and "committing your accounting data to full agentic AI".

## How can this help me?

### Ingest receipts directly into Beancount files

`beanhand` can import (scanned or photographed) receipts into a Beancount file, directly as transactions, and organize those receipts coherently.

The LLM processes your receipt to extract transaction details and convert it into a Beancount transaction.  `beanhand` uses that information to file the receipt under the appropriate account folder, and to write the newly-created transaction (complete with `document:` metadata tag linking back to the filed receipt).

### Associate and organize receipts of existing Beancount transactions

It can automatically associate transactions already in your Beancount files with your receipts.

Each receipt is analyzed by the LLM to determine date / amount, then `beanhand` queries Beancount for matching transactions; the LLM is then directed to identify the correct transaction among the search results.  Finally, `beanhand` files the receipt appropriately, then adds the `document:` tag to the identified transaction, pointing to the filed receipt.

### Refine existing transactions that have documents

It can even help you refine transactions down to the line item.

A transaction you identify (by file name and line number — or a range of lines covering several transactions) will be submitted to the LLM, along with all its associated `document:`s, with instructions to enhance the transaction with all the factual detail present in the documents.  `beanhand` then uses the response of the LLM to rewrite *only* that transaction in your Beancount file.

This works incredibly well after you've imported a bunch of transactions — with the little detail your bank gives you — and you've used `beanhand associate` to add receipts to those transactions.  All those supermarket receipts of yours with many line items turn into rich detail in your ledger, in just a few seconds.

Bug reports, feature requests and pull requests are welcome!

### Run through your accounting tasks real fast

This tooling makes a workflow possible where:

* use your your favorite importers to import structured data (CSV, Quicken or bank statements);
* use `beanhand associate` to add receipts you scanned to the newly-imported data, and organize them;
* enhance the imported and now-documented transactions with lots of detail using `beanhand refine`;
* ingest any receipts corresponding to transactions not imported (e.g. cash) with `beanhand ingest`.

Minimal effort, maximum results — this program plus an importer ought to accomplish 95% of your accounting input into Beancount.  Everything `beanhand` does happens with very little intervention on your part — at best, you'll fix an LLM-made error here and there; in most cases all you need to do is confirm the changes that the AI offers.

All the tools in this program offer batch mode too: you can script them to run periodically, then check on your ledger once in a while to edit transactions and mark them as cleared.

## What do I need in order to use this program?

*Access to an AI:* You'll need an OpenAI-compatible LLM (private like Open-WebUI / Ollama or cloud like OpenAI) and an API key from your LLM service to be able to use this project.  Furthermore, whatever model you use needs to be capable of *vision*.  Note that, if you use a private (non-cloud) model, your Beancount and receipt data will always be 100% private.

*Receipts*: two folders where you'll drop receipts (more on that later).

## Quick Start

**Dependencies**: You will need `xdg-open` from the `xdg-utils` package installed.  This is used when the program offers you to preview a receipt alongside a new or edited transaction.

**Install**: `pip install .` from this repository is the easiest way.  Alternatives include installing in a virtual environment, or [using pre-built Fedora RPMs](https://repo.rudd-o.com/) which deal with the availability of all required dependencies (e.g. `python3-beancount`).  The source contains everything you need to build RPM packages including `rpm` and `deps-fedora` targets.

**Configure** — create `~/.config/beanhand.json` (see [Configuration](#configuration) below) for an example.  You'll need a `documents`, an `ai`, and a `beancount` section.  You also need to mark in your ledger the accounts the AI is allowed to use (see [Marking accounts](#marking-accounts-in-your-ledger)).

**Kick the tires**:

To list the accounts you marked for `beanhand` to know about:

```bash
beanhand list-accounts
```

To list various kinds of receipts:

```bash
beanhand list-uningested        # receipts not yet imported
beanhand list-unassociated      # receipts not yet linked to a transaction
```

**Import uningested receipts interactively**.  To import scanned receipts (and possibly preview them as you go):

```bash
beanhand ingest
```

**Associate receipts with existing transactions**:

```bash
beanhand associate
```

**Refine an existing transaction using its linked documents**:

```bash
# rewrites that transaction on line number 157 based on its linked receipt(s)
beanhand refine Expenses.beancount 157
# or batch refinement: any mix of single line numbers and inclusive line ranges
beanhand refine Expenses.beancount 157-200 408 500-700
# the `end` keyword runs a range to the end of the file
beanhand refine Expenses.beancount 500-end
# --clear additionally sets the flag of every modified transaction to the clear flag (*)
beanhand refine Expenses.beancount 157-200 408 --clear
```

Find a reference to all subcommands in the [Commands](docs/Commands.md) documentation.

## Details

### Feeding receipts to `beanhand`

`beanhand` can obtain receipts from **local folders on your computer**, or
on a **WebDAV** server.  You'll need to configure two distinct folders:

1. An uningested receipts folder; `beanhand ingest` can import (as transactions) and file for you.
2. An unassociated receipts folder; `beanhand associate` can look at them, decide which transaction each belongs to, and file them.

Those folders can be stored:

* In your computer; you manually drop files in them, or use something like Syncthing or Dropbox to feed them from your phone.
* In a WebDAV server such as a Nextcloud instance; upload receipts on the Web or via your phone, and `beanhand` can see them.

### Batch operation

`ingest`, `refine` and `associate` work interactively by default, but they support batch operation too.  They support flag `--no` which does all the work but never touches your files.  They all also support mode `--yes`, which goes ahead and makes all modifications to your Beancount data, importing receipts into your Beancount folder and deleting them from the source.  Any exceptions processing receipts when using these two flags are printed (summarized) as they take place, and they are printed in detail at the end of the run; normally (in interactive mode), an exception interrupts the whole process at the first failure.

### Protecting your Beancount data

`beanhand` guards against two ways your ledger could be damaged:

* **Concurrent invocations.** The moment the configuration is loaded (before any subcommand runs), `beanhand` takes an exclusive advisory lock on your main Beancount file (`beancount.main_file`) and holds it for the duration of the whole subcommand.  If you run `beanhand` in one terminal while another `beanhand` (or any other process holding that lock) is still working, the second one prints a notice to standard error and then waits until the first one is done, instead of the two trampling each other's data.  In effect, data-modifying commands queue up one behind the other.
* **Crash-during-write.** Every Beancount file write is flushed and pushed all the way to disk (`fsync`ed) before `beanhand` moves on, so a crash or power loss cannot leave a half-written ledger.

**Do not independently edit any Beancount file** while any `beanhand` routine that may modify a Beancount file is running.  `beanhand` has no way to locking you out from editing a file while it is doing work on the same file.  If you do edit files before `beanhand` is done with them, you run the risk of corrupting them.

### Naming convention for receipt files

Imported receipts are saved under `<beancount_folder>/<account_with_slashes>/` with the naming pattern:

```
<YYYY-MM-DD>_<description> — <original_filename>
```

For example, `2026-07-15_Groceries — IMG_1234.jpg`.

Timestamps of the receipts are preserved.  The Beancount folder is the folder containing the main Beancount file you configured.

### LLMs tested and known to do great work

* qwen3.6-27b through Ollama / Open-WebUI: excellent results
* qwen3.5:35b-A3B through Ollama / Open-WebUI: very good results

## Configuration

Configuration usually lives in the JSON file `~/.config/beanhand.json`.  `~/.config/bean-ai.json` is a compatibility fallback.

### Sample

Your configuration file must include three sections:

* `beancount`: informs `beanhand` of your Beancount setup.
* `documents`: lets `beanhand` know where to find your receipts.
* `ai`: informs `beanhand` of your OpenAI-compatible LLM service.

The `documents.backend` key selects the backend: `"webdav"` or `"local"`.
When the key is absent, the backend is inferred from the fields present:
if **both** `uningested_receipts_folder` and `unassociated_receipts_folder`
are given, the local backend is used; otherwise the WebDAV backend is used.
Each backend has its own mandatory keys, and a configuration missing any of
them is an error.

Here is a sample configuration file using the local backend:

```json
{
  "beancount": {
    "main_file": "/home/user/Documents/Accounting/main.beancount",
    "ingestion_destination_file": "imported.beancount"
  },
  "ai": {
    "api_url": "http://openwebui.example.com/api",
    "token": "secret-token",
    "model_name": "qwen3.6:35b-a3b"
  },
  "documents": {
    "backend": "local",
    "uningested_receipts_folder": "/home/user/Dropbox/Receipts/uningested",
    "unassociated_receipts_folder": "/home/user/Dropbox/Receipts/unassociated"
  }
}
```

With the local backend, each receipt category lives in its own, independent
directory on the server's filesystem, given as a full path (created on demand
by the server).  `<uningested_receipts_folder>` holds receipts awaiting
conversion into transactions, and `<unassociated_receipts_folder>` holds
receipts awaiting association with an existing transaction.  There is no
shared base directory: the two folders may be placed anywhere, and even in
unrelated locations.  (A shared base folder/URL is a WebDAV-only concept.)

The equivalent WebDAV-based `documents` section — exemplified by the typical
Nextcloud user setup — looks like this:

```json
{
  "documents": {
    "backend": "webdav",
    "username": "John",
    "password": "dav-pass",
    "base_url": "https://nextcloud.server.com/remote.php/dav/John/files/Accounting",
    "uningested_receipts_subfolder": "receipts/uningested",
    "unassociated_receipts_subfolder": "receipts/unassociated"
  }
}
```

#### The `ai` section: OpenAI vs. a self-hosted backend

If you pay OpenAI for your LLM, the `ai` section needs no URL at all — omit
`api_url`, and `beanhand` talks to the OpenAI API directly:

```json
"ai": {
  "token": "sk-...",
  "model_name": "gpt-4o"
}
```

(`model_name` should be a vision-capable model from the [OpenAI model catalog](https://platform.openai.com/docs/models).)

If you run your own OpenAI-compatible backend instead (Open-WebUI, Ollama,
vLLM, ...), you must set `api_url` to its **base** URL — the prefix the client
appends `chat/completions` to, which in Ollama and Open-WebUI includes an `/api`
path:

```json
"ai": {
  "api_url": "http://webui.home/api",
  "token": "secret-token",
  "model_name": "qwen3.6:35b-a3b"
}
```

A trailing slash is allowed but not required, and do not add `/v1` yourself
for Open-WebUI.

### Marking accounts in your ledger

The list of accounts the AI is offered is **derived from your ledger** at run time. An account is offered only if it is open as of the day you invoke `beanhand` (an account closed before that day is never offered), and it is selected — and not excluded — by metadata keys attached to `open` directives.

Three metadata keys can be attached to an `open` directive:

- `beanhand-include: "yes"` — **include this one account** (its live children are *not* pulled in).
- `beanhand-include: "recursively"` — **include this account and every live account beneath it.** Placing this on a subtree root (e.g. `Expenses:Food`) is the usual way to opt a whole family in with a single line. `yes` and `recursively` are the only accepted values; any other value is an error. Closing the account does *not* revoke the marker for its still-open children — the policy keeps propagating downward (the same is true of `beanhand-exclude: "recursively"`).
- `beanhand-exclude: "yes"` / `beanhand-exclude: "recursively"` — **exclude this account** (with `"recursively"`, itself *and* its live descendants) from the list, even where an ancestor's `beanhand-include: "recursively"` would otherwise have selected it. An account that carries its own explicit `beanhand-include` beats an ancestor's `beanhand-exclude: "recursively"`, so you can re-include a specific account inside an excluded subtree.
- `beanhand-rules: "..."` — **optional guidance** shown to the LLM next to this one account (it does not inherit from ancestors), e.g. `"Supermarket and grocery runs; includes snacks"`.

Example:

```beancount
2025-01-01 open Expenses:Food
  beanhand-include: "recursively"
2025-01-01 open Expenses:Food:Groceries
  beanhand-rules: "Supermarket and grocery runs; includes snacks"
2025-01-01 open Expenses:Food:Restaurants
  beanhand-rules: "Eating out and delivery; not take-away from supermarkets"
2025-01-01 open Assets:Cash:CHF
  beanhand-include: "yes"
  beanhand-rules: "Physical cash on hand, Swiss francs"
2025-01-01 open Assets:Cash:CHF:In-limbo
  beanhand-exclude: "recursively"
2025-01-01 open Assets:Banks:Main
  beanhand-include: "recursively"
```

Accounts with no markers are absent by default (opt-in, not opt-out). If *no* account is marked, `beanhand` refuses to run an account-touching command and points you back to this section.

> **Migration (breaking change):** earlier versions of `beanhand` read the account list from a static `beancount.account_list_file` (customarily `~/.config/bean-ai.accounts`), or read metadata keys in the ledger named `bean-ai-include` / `bean-ai-exclude` / `bean-ai-rules`.  This is no longer the case.  If your config still carries it, `beanhand` prints a warning to stderr and ignores it.  Migrating is a one-time ledger edit: for each subtree you want the AI to use, add `beanhand-include: "recursively"` to its root's `open` directive; move any trailing `# ...` comments from the old file into `beanhand-rules` metadata on the matching `open`; and delete the `account_list_file` key and the file.  If you were using `bean-ai-*` metadata tags, rename them to `beanhand-*`.

### Parameters

| Field | Type | Description |
|---|---|---|
| `beancount.main_file` | `Path` | Path to your main Beancount ledger file. Used to read existing transactions and directives which influence `beanhand`'s conduct. |
| `beancount.ingestion_destination_file` | `Path \| null` | File to append ingested transactions to (relative to `main_file`). Defaults to `main_file` itself. |
| `ai.api_url` | `str` | *(optional)* Base URL of your OpenAI-compatible API; the client appends `chat/completions` to it. Omit to use the OpenAI API itself. Open-WebUI examples: `http://webui.home/api`, or `http://10.240.6.7/api` for a bare IP. Ollama: `http://localhost:11434/v1`. |
| `ai.token` | `str` | API token for authenticating with the AI API. |
| `ai.model_name` | `str` | Model name to use with the AI API. Must support vision. |
| `documents.backend` | `str` | Receipt storage backend: `"local"` or `"webdav"`. Optional; when absent, the local ``..._receipts_folder`` fields select `local`, otherwise `webdav`. |
| `documents.uningested_receipts_folder` | `str` | *(local backend)* Full path to the directory where **new** (uningested) receipts are stored. |
| `documents.unassociated_receipts_folder` | `str` | *(local backend)* Full path to the directory where **existing** (unassociated) receipts, to be associated, are stored. |
| `documents.username` | `str` | *(webdav backend)* WebDAV username for the receipts data source. |
| `documents.password` | `str` | *(webdav backend)* WebDAV password for the receipts data source. |
| `documents.base_url` | `str` | *(webdav backend)* Base URL of the WebDAV server containing receipts.  As an example using Nextcloud, the base URL would be `https://nextcloud.example.com/remote.php/dav/files/MyUsername`. |
| `documents.uningested_receipts_subfolder` | `str` | *(webdav backend)* Subfolder path (under `base_url`) where **new** (uningested) receipts are stored. |
| `documents.unassociated_receipts_subfolder` | `str` | *(webdav backend)* Subfolder path (under `base_url`) where **existing** (unassociated) receipts, to be associated, are stored. |

### Split `beanhand` — for Qubes OS users

*Section of interest only to Qubes OS users*

This program supports *split operation* -- receipts and AI access in one VM (the server), Beancount files in another VM (the client).  In this mode, `beanhand` runs on the qube that has your Beancount files, and will talk to `beanhand-server` (which it normally does by spawning the process locally) through Qrexec communication channels targeting another VM, to obtain receipt data and talk to your LLM.

To enable this mode of operation:

1. Split your configuration so that client VM only has the `beancount` section, and the server VM has the `documents` and `ai` sections.  The Beancount ledger stays in the client.
2. Ensure both client and server VMs have this program installed.  Remember there are [pre-built Fedora RPMs](https://repo.rudd-o.com/) of the `python3-beanhand` package and all its dependencies.
3. Deploy the service files in the `qubes-rpc` folder to `/etc/qubes-rpc` of your server VM.  Depending on where `beanhand-server` is installed, you may have to adjust the paths in those files.  Ensure all service files are executable.  There [pre-built Fedora RPMs](https://repo.rudd-o.com/) named `python3-beanhand-qubes-rpc` that will install these files for you.
4. Add a `target_vm` key in the client configuration, naming the server VM.
5. Allow the client VM access to the Qubes RPC services you deployed.  In the following example, the `pim` VM is the server, and the `financial` VM is the client:

```
# You'd put this e.g. in file /etc/qubes/policy.d/99-beanhand.policy
# of your dom0 in your Qubes OS installation.
beanhand.ListUningested * financial pim allow
beanhand.ListUnassociated * financial pim allow
beanhand.Process * financial pim allow
beanhand.Fetch * financial pim allow
beanhand.Remove * financial pim allow
beanhand.HelpAssociateReceipt * financial pim allow
beanhand.Refine * financial pim allow
```

If you did everything right, `beanhand list-unassociated` should show you your unassociated receipts, and everything else will work fine.

### Overriding configuration

The location for configuration can be overridden with command line argument `--config` or environment variable
`$BEANHAND_CONFIG`.. Resolution order (first match wins):

1. `--config <path>` CLI flag
2. `$BEANHAND_CONFIG` environment variable
3. Default `~/.config/beanhand.json`
