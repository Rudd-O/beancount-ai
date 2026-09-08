# Spec: Split of server into two backends

Status: planned.

## Overview

Today, the `beanhand` client command talks to a single server `beanhand-server`, either
locally via `subprocess.run` or remotely via `qrexec` to a different `target_vm`.

This has the effect that the user of the program cannot configure two separate VMs,
one for document operations (retrieval, erasure) and one for AI operations.

The code must change so the server command splits into two distinct commands
— `beanhand-documents-server` and `beanhand-ai-server`.

