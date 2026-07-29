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
from typing import TypedDict, cast, IO


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
    arg: str | None = None,
) -> tuple[subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
    """Start a remote process and return its Popen handle (with all streams already connected)."""
    cfg = get_cfg()
    target_vm: str | None = cfg["target_vm"]

    # Local fallback for testing: when target_vm is None, invoke pycash-server directly.
    if target_vm is None:
        cmd = ["pycash-server", "--config", str(_cfg_path)]
        if arg is not None:
            cmd.extend([action, arg])
        else:
            cmd.append(action)
    else:
        cmd = ["qrexec-client-vm", str(target_vm), action]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin is not None
    assert proc.stdout is not None
    return proc, proc.stdin, proc.stdout


# -- subcommands -----------------------------------------------------------


def do_list(_args: argparse.Namespace) -> None:
    proc, stdin, stdout = _call_remote("pycash.List")
    stdin.close()

    data = json.load(stdout)
    if data.get("error"):
        print(data["error"], file=sys.stderr)
    else:
        for fname in data["receipts"]:
            print(fname)

    sys.exit(proc.wait())


def do_process(args: argparse.Namespace) -> None:
    proc, stdin, stdout = _call_remote("pycash.Process", arg=args.filename)
    stdin.close()

    accumulated: list[str] = []

    for line in stdout:
        msg = json.loads(line)

        reasoning_over = False
        if err := msg.get("error"):
            print(err, file=sys.stderr)
            break
        elif msg.get("finish"):
            break
        elif msg.get("reasoning"):
            sys.stderr.write(msg["reasoning"])
            sys.stderr.flush()
        elif msg.get("output"):
            if not reasoning_over:
                sys.stderr.write("\n")
                sys.stderr.flush()
                reasoning_over = True
            accumulated.append(msg["output"])

    ret = proc.wait()
    if ret != 0:
        sys.exit(ret)

    print("".join(accumulated))


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

    process_cmd = sp.add_parser(
        "process", help="Process a receipt image via Open-WebUI"
    )
    process_cmd.add_argument(
        "filename", help="Filename of the receipt file in receipts_dir"
    )

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

    dispatch = {"list": do_list, "process": do_process}  # type:ignore
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
