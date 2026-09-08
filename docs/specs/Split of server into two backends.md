# Spec: Split of server into two backends

Status: implemented.

## Overview

Previously, the `beanhand` client command talked to a single server `beanhand-server`, either
locally via a subprocess or remotely via `qrexec` to a single `target_vm`. That meant the
user could not configure two separate VMs, one for document operations (retrieval, erasure)
and one for AI operations.

The server command is now split into two distinct programs — `beanhand-documents-server`
and `beanhand-ai-server` — which the client addresses through two role sections.

Because some AI operations require documents, the responsibility of fetching the required
document from the documents server and then relaying it to the AI server lives on the
client.

## Implementation notes

* The client config addresses the two server roles through two sections,
  `documents` and `ai`. Each *may* name its server's Qubes VM via a `vm` key
  (or an explicit `"backend": "qubes"` alongside `vm`), and the two sections
  may name different VMs. A section lacking a `vm` key (e.g. `"documents": {}`)
  makes the matching server run locally as a subprocess; when a `vm` is present
  the section must carry no other keys. Both sections must still be present in
  the client config *unless* the legacy top-level `target_vm` key is set — which
  is honored as a deprecated catch-all that synthesizes a `{"vm": ..., "backend":
  "qubes"}` address for any role section not already spelled out.
* All three AI subcommands (`beanhand.Process`, `beanhand.HelpAssociateReceipt`,
  and `beanhand.Refine`) take no filename argument and the AI server never reads
  the receipt storage; input travels over stdin. For `Process`, the whole request
  (the account list plus the base64 receipt) is a single JSON object on stdin.
  `HelpAssociateReceipt` is a two-line stdin protocol: line 1 is the receipt,
  line 2 the candidate transaction list (written after the first LLM pass
  completes). `Refine`'s request is a single plain-JSON object.
* A server config section is validated on first access, so a documents-server
  config may omit `ai` entirely and an AI-server config may omit `documents`.