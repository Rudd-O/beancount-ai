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
import tempfile
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


def shorten_fn(folder: str | Path, fn: str):
    # Reduce max path length without affecting the file name extension.
    maxlen = os.pathconf(folder, "PC_NAME_MAX")
    # Sarn, we only handle UTF-8 file systems.  Maybe this would be good to fix in the future.
    while len(fn.encode("utf-8")) > maxlen:
        n, e = os.path.splitext(fn)
        n = n[:-1]
        fn = n + e
    return fn


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


def list_receipts() -> list[str]:
    """Return receipt filenames from the server.

    Raises on qrexec transport error; prints to stderr and returns ``[]``
    when the JSON cannot be decoded.
    """

    cmd, proc, stdin, stdout = _call_remote("pycash.List")
    stdin.close()

    read_data = stdout.read()
    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)

    try:
        data = json.loads(read_data)
        mm = [os.path.basename(x) for x in data["receipts"]]
        assert mm == data["receipts"]
        return data["receipts"]
    except Exception as e:
        print(f"cannot decode server response: {e}", file=sys.stderr)
        return []


def process_receipt(filename: str) -> tuple[str, str]:
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

    # Remove Markdown quote formatting from JSON output.
    llm_output_lines = llm_output.splitlines(True)
    if llm_output_lines[0].startswith("```"):
        llm_output_lines = llm_output_lines[1:]
    if llm_output_lines[-1].startswith("```"):
        llm_output_lines = llm_output_lines[:-1]
    llm_output = "".join(llm_output_lines)

    # Fish out first account in the payment accounts list.
    try:
        data = json.loads(llm_output)
    except json.decoder.JSONDecodeError as e:
        raise Exception(
            f"Failed decoding expected JSON at end of string: {e}\n{llm_output_original}"
        )

    try:
        payment_account = data["payment_accounts"][0]
    except Exception as e:
        raise Exception(
            f"Could not retrieve expense account from LLM output: {e}\n{llm_output_original}"
        )

    try:
        transaction = data["transaction"]
    except Exception as e:
        raise Exception(
            f"Could not retrieve Beancount transaction from LLM output: {e}\n{llm_output_original}"
        )

    return transaction, payment_account


def fetch_receipt(filename: str) -> bytes:
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

    raw = fetch_receipt(filename)

    # Construct the final destination folder.  Account folders reside directly under `beancount_folder`.
    receipt_dir = Path(cfg["beancount_folder"]) / account.replace(":", "/")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    if description:
        fn = transaction_date.strftime("%Y-%m-%d.") + description + " — " + filename
    else:
        fn = transaction_date.strftime("%Y-%m-%d.") + filename

    # No slashes in the file name, please.
    fn.replace("/", "_")

    fn = shorten_fn(receipt_dir, fn)

    receipt_path = receipt_dir / fn
    receipt_path.write_bytes(raw)

    return receipt_path


def do_list(_args: argparse.Namespace) -> None:
    for fname in list_receipts():
        print(fname)


def do_fetch(args: argparse.Namespace) -> None:
    gotten = fetch_receipt(args.filename)
    Path(args.destination).write_bytes(gotten)


def do_remove(args: argparse.Namespace) -> None:
    cmd, proc, stdin, _ = _call_remote("pycash.Remove", arg=args.filename)
    stdin.close()

    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)


def do_process(args: argparse.Namespace) -> None:
    try:
        llm_output, account = process_receipt(args.filename)
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


def _preview_receipt(filename: str, preview_dir: Path) -> None:
    dest_path = preview_dir / filename
    dest_path.write_bytes(fetch_receipt(filename))
    subprocess.Popen(
        ["xdg-open", str(dest_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def do_import(args: argparse.Namespace) -> None:
    cfg = get_cfg()

    beancount_transaction, account = process_receipt(args.filename)
    # Strip headline comments and newlines from the transaction.
    while beancount_transaction.lstrip().startswith(";"):
        beancount_transaction = "".join(
            beancount_transaction.splitlines(True)[1:]
        ).lstrip()

    datestr, reststr = beancount_transaction.split(" ", 1)
    # Take the text after the date, remove the transaction flag and the space next to it,
    # then use the payee and narration to construct a description for the receipt file name.
    # If there is a comment at the end of the line, strip it too.
    reststr = (
        reststr.splitlines()[0][2:]
        .replace('" "', " — ")
        .replace('"', "")
        .split(";")[0]
        .strip()
    )
    # Take the text containing the date, and make a date for the receipt file name.
    transdate = date.strptime(datestr, "%Y-%m-%d")  # type: ignore

    receipt_path = organize_receipt(transdate, args.filename, account, reststr)
    formatted_tx = insert_document_metadata(beancount_transaction, str(receipt_path))

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


def do_ingest(args: argparse.Namespace) -> None:
    receipts = list_receipts()
    if not receipts:
        print("No receipts to ingest.", file=sys.stderr)
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        preview_dir = Path(tmpdir)

        for receipt in receipts:
            action = "skip"
            while True:
                print(f"\nImport '{receipt}'? [y/n/p/q] ", file=sys.stderr, end="")
                try:
                    answer = input().strip().lower()
                except EOFError:
                    return

                if answer == "q":
                    return

                if answer == "p":
                    _preview_receipt(receipt, preview_dir)
                    continue  # re-prompt for the same receipt

                if answer == "y":
                    action = "import"

                break  # leave prompt loop after y or n

            if action != "import":
                continue  # genuinely skip this receipt

            try:
                do_import(argparse.Namespace(filename=receipt))
            except Exception as e:
                print(f"Ingestion failed for '{receipt}': {e}", file=sys.stderr)
                sys.exit(1)

            # Remove from WebDAV only after successful import.
            try:
                do_remove(argparse.Namespace(filename=receipt))
            except subprocess.CalledProcessError as e:
                print(
                    f"Could not remove '{receipt}' from WebDAV (exit {e.returncode})",
                    file=sys.stderr,
                )
                sys.exit(e.returncode)


def do_organize(args: argparse.Namespace) -> None:
    tdate = date.strptime(args.date, "%Y-%m-%d")  # type:ignore
    receipt_path = organize_receipt(tdate, args.filename, args.account)
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

    rm_cmd = sp.add_parser("remove", help="Delete a receipt file from WebDAV")
    rm_cmd.add_argument("filename", help="Filename of the receipt file in receipts_dir")

    org_cmd = sp.add_parser("organize", help="File a receipt under a payment account")
    org_cmd.add_argument(
        "filename", help="Filename of the receipt file in receipts_dir"
    )
    org_cmd.add_argument("date", help="Date to impute to receipt file", type=str)
    org_cmd.add_argument("account", help="Payment account (e.g. Assets:Cash:CHF)")

    imp_cmd = sp.add_parser(
        "import", help="Full import pipeline: LLM → organize → append to Beancount"
    )
    imp_cmd.add_argument(
        "filename", help="Filename of the receipt file in receipts_dir"
    )

    _ing_cmd = sp.add_parser(
        "ingest",
        help="Batch ingest receipts interactively: process → organize → append → remove",
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

    dispatch = {  # type:ignore
        "list": do_list,
        "fetch": do_fetch,
        "import": do_import,
        "ingest": do_ingest,
        "organize": do_organize,
        "process": do_process,
        "remove": do_remove,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
