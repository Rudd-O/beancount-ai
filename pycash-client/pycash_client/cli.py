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
from datetime import date
from pathlib import Path
from typing import TypedDict, cast, IO


CONF_DEFAULT = Path.home() / ".config" / "pycash.json"


# -- configuration ---------------------------------------------------------


class Configuration(TypedDict):
    target_vm: str | None
    beancount_folder: str
    beancount_main_file: str
    beancount_transaction_destination_file: str


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
) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
    """Start a remote process and return its Popen handle (with all streams already connected)."""
    cfg = get_cfg()
    target_vm: str | None = cfg["target_vm"]

    if arg is not None:
        # arguments must be hex
        arg = arg.encode("utf-8").hex()

    # Local fallback for testing: when target_vm is None, invoke pycash-server directly.
    if target_vm is None:
        cmd = ["pycash-server", "--config", str(_cfg_path)]
        if arg is not None:
            cmd.extend([action, arg])
        else:
            cmd.append(action)
    else:
        if arg is not None:
            action = f"{action}+{arg}"
        cmd = ["qrexec-client-vm", str(target_vm), action]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin is not None
    assert proc.stdout is not None
    return cmd, proc, proc.stdin, proc.stdout


# -- subcommands -----------------------------------------------------------


def do_list(_args: argparse.Namespace) -> None:
    _, proc, stdin, stdout = _call_remote("pycash.List")
    stdin.close()

    try:
        data = json.load(stdout)
        if data.get("error"):
            print(data["error"], file=sys.stderr)
        else:
            for fname in data["receipts"]:
                print(fname)
    except Exception as e:
        print(f"cannot decode server response: {e}", file=sys.stderr)

    sys.exit(proc.wait())


def process(filename: str) -> tuple[str, str]:
    """
    Calls upon the LLM on the server side to produce a Beancount transaction
    and the main payment account.
    """
    cmd, proc, stdin, stdout = _call_remote("pycash.Process", arg=filename)
    stdin.close()

    accumulated: list[str] = []

    reasoning_over = False
    for line in stdout:
        msg = json.loads(line)

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
        else:
            assert 0, msg

    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)

    llm_output = "".join(accumulated).strip()
    llm_output_original = llm_output

    # Fish out the last line...
    llm_output_lines = llm_output.splitlines(True)
    last_line = llm_output_lines[-1]
    # ...then rejoin the prior lines.
    llm_output = "".join(llm_output_lines[:-1]).strip()

    # Remove Markdown quote formatting from Beancount transaction.
    llm_output_lines = llm_output.splitlines(True)
    if llm_output_lines[0].startswith("```"):
        llm_output_lines = llm_output_lines[1:]
    if llm_output_lines[-1].startswith("```"):
        llm_output_lines = llm_output_lines[:-1]
    llm_output = "".join(llm_output_lines)

    # Fish out first account in the payment accounts list.
    try:
        account = json.loads(last_line)[0]
    except json.decoder.JSONDecodeError:
        raise Exception(
            f"Failed decoding expected JSON at end of string:\n{llm_output_original}"
        )

    return llm_output, account


def fetch(filename: str) -> bytes:
    cmd, proc, stdin, stdout = _call_remote("pycash.Fetch", arg=filename)
    stdin.close()

    raw = stdout.read()
    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)

    return raw


def organize_receipt(
    transaction_date: date,
    filename: str,
    account: str,
    description: str | None = None,
) -> Path:
    """
    Organize a receipt file into an account folder.

    For receipts to be recognized as documents in Beancount, their filename has
    the requirement that it must begin with a date in Y-m-d format.  Hence
    the requisite transaction date at the beginning of the file name.
    """
    cfg = get_cfg()

    raw = fetch(filename)

    # Construct the final destination folder.  Account folders reside directly under `beancount_folder`.
    receipt_dir = Path(cfg["beancount_folder"]) / account.replace(":", "/")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    if description:
        fn = transaction_date.strftime("%Y-%m-%d.") + description + " — " + filename
    else:
        fn = transaction_date.strftime("%Y-%m-%d.") + filename
    receipt_path = receipt_dir / fn
    receipt_path.write_bytes(raw)

    return receipt_path


def do_fetch(args: argparse.Namespace) -> None:
    Path(args.destination).write_bytes(fetch(args.filename))


def do_process(args: argparse.Namespace) -> None:
    try:
        llm_output, account = process(args.filename)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

    print(llm_output)
    print(f"Main account: {account}")


def insert_document_metadata(transaction_text: str, file_path: str) -> str:
    lines = transaction_text.splitlines(True)
    if not lines or lines[0].strip().startswith("#"):
        return transaction_text
    stripped = lines[1].lstrip()
    indent = lines[1][: len(lines[1]) - len(stripped)]
    lines.insert(1, '{}document: "{}"\n'.format(indent, file_path.replace('"', '\\"')))
    return "".join(lines)


def do_import(args: argparse.Namespace) -> None:
    cfg = get_cfg()

    llm_output, account = process(args.filename)
    datestr, reststr = llm_output.split(" ", 1)
    # Take the text after the date, remove the transaction flag and the space next to it,
    # then use the payee and narration to construct a description for the receipt file name.
    reststr = reststr.splitlines()[0][2:].replace('" "', " — ").replace('"', "")
    # Take the text containing the date, and make a date for the receipt file name.
    transdate = date.strptime(datestr, "%Y-%m-%d")

    receipt_path = organize_receipt(transdate, args.filename, account, reststr)
    formatted_tx = insert_document_metadata(llm_output, str(receipt_path))

    dest = Path(cfg["beancount_folder"]) / os.path.basename(
        cfg["beancount_transaction_destination_file"]
    )
    with open(dest, "a") as f:
        f.write("\n; {} imported by pycash.\n".format(args.filename))
        f.write(formatted_tx.rstrip("\n"))
        if not formatted_tx.endswith("\n"):
            f.write("\n")

    print(
        f"The transaction has been imported to {dest} and the receipt has been filed under {receipt_path}",
        file=sys.stderr,
    )


def do_organize(args: argparse.Namespace) -> None:
    receipt_path = organize_receipt(args.filename, args.account)
    print("The file has been organized into", str(receipt_path), file=sys.stderr)


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

    fetch_cmd = sp.add_parser("fetch", help="Fetch a receipt file from the server")
    fetch_cmd.add_argument(
        "filename", help="Filename of the receipt file in receipts_dir"
    )
    fetch_cmd.add_argument("destination", help="Local path to save the retrieved file")

    org_cmd = sp.add_parser("organize", help="File a receipt under a payment account")
    org_cmd.add_argument(
        "filename", help="Filename of the receipt file in receipts_dir"
    )
    org_cmd.add_argument("account", help="Payment account (e.g. Assets:Cash:CHF)")

    imp_cmd = sp.add_parser(
        "import", help="Full import pipeline: LLM → organize → append to Beancount"
    )
    imp_cmd.add_argument(
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

    dispatch = {
        "list": do_list,
        "fetch": do_fetch,
        "import": do_import,
        "organize": do_organize,
        "process": do_process,
    }  # type:ignore
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
