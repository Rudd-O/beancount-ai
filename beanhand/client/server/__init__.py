"""Client-side transport modules, one per backend server program.

* :mod:`beanhand.client.server.documents` — the documents server
  (list / fetch / remove receipts).
* :mod:`beanhand.client.server.ai` — the AI server (LLM calls).
* :mod:`beanhand.client.server.transport` — the shared qrexec / subprocess
  plumbing.

Import what's needed directly.  This file contains no references to
the aforementioned modules.
"""
