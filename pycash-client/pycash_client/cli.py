#!/usr/bin/env python3
"""pycash-client — CLI wrapper that talks to pycash-server on `pim` via qrexec.

Subcommands (mirror those of pycash-server):
    list    Get the remote receipt file list as plain text (one file per line)

Config is read from ~/.config/pycash.json.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict, cast


CONF_DEFAULT = Path.home() / ".config" / "pycash.json"


# -- configuration ---------------------------------------------------------


class Configuration(TypedDict):
    target_vm: str | None


_cfg: Configuration | None = None
_cfg_path: Path | None = None  # which file was actually loaded

_cfg_override: str | None = None  # set by --config before get_cfg() is called


def _get_cfg_path(override: str | None) -> Path:
    """Return the config file path, resolving overrides in order of priority.

    Priority (highest → lowest):
        1. ``--config`` CLI argument
        2. ``PYCASH_CONFIG`` environment variable
        3. Default ``~/.config/pycash.json``
    """
    if override:
        return Path(override)
    env_cfg = os.environ.get("PYCASH_CONFIG")
    if env_cfg:
        return Path(env_cfg)
    return CONF_DEFAULT


def get_cfg() -> Configuration:
    """Load and cache the config from the resolved path.

    If called multiple times, only the *first* invocation's resolution is used;
    subsequent calls return the cached result (prevents a user from accidentally
    reloading with different paths within one process).
    """
    global _cfg, _cfg_path
    if _cfg is not None:
        return _cfg

    fp = _get_cfg_path(_cfg_override)
    with open(fp) as fh:
        _cfg = cast(Configuration, json.load(fh))
    _cfg_path = fp
    return _cfg


# -- qrexec transport ------------------------------------------------------

# From inside a VM, IPC to another VM uses:
#   qrexec-client-vm <target_vm> <action_name> [rpc_client] [args...]
# Only stdin / stdout are relayed between client and server — the RPC action name
# determines *which* program on the target VM is invoked (registered via dom0 policy).


def _call_remote(
    action: str,
    stdin_data: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    cfg = get_cfg()
    target_vm: str | None = cfg["target_vm"]

    # Local fallback for testing: when target_vm is None, invoke pycash-server directly.
    if target_vm is None:
        cmd = ["pycash-server"] + ["--config", str(_cfg_path)] + [action]
        return subprocess.run(cmd, input=stdin_data, capture_output=True)

    cmd = ["qrexec-client-vm", str(target_vm), action]
    return subprocess.run(cmd, input=stdin_data, capture_output=True)


# -- subcommands -----------------------------------------------------------


def do_list(_args: argparse.Namespace) -> None:
    prefix = "pycash"
    action = f"{prefix}.List"

    result = _call_remote(action)
    if result.returncode != 0:
        print(result.stderr.decode(), file=sys.stderr)
        sys.exit(1)

    data = json.loads(result.stdout)
    if "error" in data:
        print(data["error"], file=sys.stderr)
        sys.exit(1)

    for fname in data["receipts"]:
        print(fname)


# -- CLI -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pycash-client",
        description="qrexec client for pycash",
    )
    ap.add_argument(
        "--config",
        "-c",
        default=None,
        dest="conf_path",
        help="Path to the config file; overrides $PYCASH_CONFIG and the default",
    )
    sp = ap.add_subparsers(dest="command")

    sp.add_parser("list", help="Get receipt filenames to import (plain text)")

    return ap


def main() -> None:
    global _cfg_override
    ap = build_parser()
    args = ap.parse_args()

    _cfg_override = args.conf_path  # set it once for downstream calls to get_cfg()
    _cfg = None  # ensure fresh start if already cached (reload from --config path)

    if not args.command:
        ap.print_help(sys.stderr)
        sys.exit(1)

    dispatch = {"list": do_list}
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
