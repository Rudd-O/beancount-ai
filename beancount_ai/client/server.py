import json
import os
import subprocess
import sys
from pathlib import Path
from typing import IO, Any, Literal, cast

from colorama import Fore, Style  # type: ignore

from beancount_ai.client.config import Configuration


class BadJSON(json.decoder.JSONDecodeError):
    def __str__(self) -> str:
        return json.decoder.JSONDecodeError.__str__(self) + "\nText:\n" + (self.doc)


def load_json(s: str | bytes) -> Any:
    try:
        return json.loads(s)
    except json.decoder.JSONDecodeError as e:
        raise BadJSON(e.msg, s if isinstance(s, str) else s.decode("utf-8"), e.pos)


def stream_reasoning_and_capture_output(stdout: IO[bytes]) -> str:
    accumulated: list[str] = []

    reasoning_over = False
    for line in stdout:
        msg = load_json(line)

        if msg.get("finish"):
            break
        elif msg.get("reasoning"):
            sys.stderr.write(Fore.CYAN)
            sys.stderr.write(msg["reasoning"])
            sys.stderr.write(Style.RESET_ALL)
            sys.stderr.flush()
        elif msg.get("output"):
            if not reasoning_over:
                sys.stderr.write("\n")
                sys.stderr.flush()
                reasoning_over = True
            accumulated.append(msg["output"])
        else:
            assert 0, msg

    return "".join(accumulated).strip()


def demarkdownify(llm_output: str) -> str:
    llm_output_lines = llm_output.splitlines(True)
    if llm_output_lines[0].startswith("```"):
        llm_output_lines = llm_output_lines[1:]
    if llm_output_lines[-1].startswith("```"):
        llm_output_lines = llm_output_lines[:-1]
    return "".join(llm_output_lines)


# -- qrexec transport ------------------------------------------------------

# From inside a VM, IPC to another VM uses:
#   qrexec-client-vm <target_vm> <action_name> [rpc_client] [args...]
# Only stdin / stdout are relayed between client and server — the RPC action name
# determines *which* program on the target VM is invoked (registered via dom0 policy).


class RemoteVM:
    def __init__(self, target_vm: str | None):
        self.target_vm = target_vm

    @classmethod
    def from_cfg(cls, cfg: Configuration) -> "RemoteVM":
        return cls(cfg.target_vm)

    def _call(
        self,
        action: str,
        arg: str | None = None,
    ) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """Start a remote process and return its Popen handle (with all streams already connected)."""
        if arg is not None:
            # arguments must be hex
            arg = arg.encode("utf-8").hex()

        # Local fallback for testing: when target_vm is None, invoke bean-ai-server directly.
        if self.target_vm is None:
            cmd = ["bean-ai-server", "--config", str(Configuration.cfg_path)]
            if arg is not None:
                cmd.extend([action, arg])
            else:
                cmd.append(action)
        else:
            if arg is not None:
                action = f"{action}+{arg}"
            cmd = ["qrexec-client-vm", str(self.target_vm), action]

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert proc.stdin is not None
        assert proc.stdout is not None
        return cmd, proc, proc.stdin, proc.stdout

    def list_receipts(
        self, category: Literal["unassociated"] | Literal["uningested"]
    ) -> list[str]:
        """Return receipt filenames from the server.

        Raises on qrexec transport error; prints to stderr and returns ``[]``
        when the JSON cannot be decoded.
        """

        cmd, proc, stdin, stdout = self._call(
            "beanai.List"
            + ("Uningested" if category == "uningested" else "Unassociated")
        )
        stdin.close()

        read_data = stdout.read()
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        data = load_json(read_data)
        receipts = cast(list[str], data["receipts"])
        mm = [os.path.basename(x) for x in receipts]
        if mm != receipts:
            raise Exception(
                f"The document store returned non-base paths when listing receipts: {data['receipts']}"
            )
        return receipts

    def help_associate_receipt(
        self, filename: str
    ) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """
        Calls upon the LLM on the server side to produce a Beancount transaction
        and the main payment account.
        """
        cmd, proc, stdin, stdout = self._call(
            "beanai.HelpAssociateReceipt", arg=filename
        )
        # FIXME caller of this rawdogs it, but the comms logic should be encapsulated in a class later.
        # FIXME these things should be context managers, actually.  Yield the useful stuff,
        # then when the context is exited, if the command failed, raise an error.
        return cmd, proc, stdin, stdout

    def refine(self) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """
        Calls upon the LLM on the server side to produce a Beancount transaction
        and the main payment account.
        """
        cmd, proc, stdin, stdout = self._call("beanai.Refine")
        # FIXME caller of this rawdogs it, but the comms logic should be encapsulated in a class later.
        # FIXME these things should be context managers, actually.  Yield the useful stuff,
        # then when the context is exited, if the command failed, raise an error.
        return cmd, proc, stdin, stdout

    def process_receipt(
        self, filename: str, account_list: list[str]
    ) -> tuple[str, str]:
        """
        Calls upon the LLM on the server side to produce a Beancount transaction
        and the main payment account.
        """
        cmd, proc, stdin, stdout = self._call("beanai.Process", arg=filename)
        acctlist = json.dumps(account_list).encode("utf-8")
        stdin.write(acctlist)
        stdin.close()

        llm_output = stream_reasoning_and_capture_output(stdout)
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        llm_output_original = llm_output

        # Remove Markdown quote formatting from JSON output.
        llm_output = demarkdownify(llm_output)

        # Fish out first account in the payment accounts list.
        data = load_json(llm_output)

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

    def fetch_receipt(self, filename: str) -> bytes:
        cmd, proc, stdin, stdout = self._call("beanai.Fetch", arg=filename)
        stdin.close()

        raw = stdout.read()
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        return raw

    def remove_receipt(self, filename: str) -> None:
        cmd, proc, stdin, _ = self._call("beanai.Remove", arg=filename)
        stdin.close()

        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)


def open_document(dest_path: Path) -> None:
    subprocess.Popen(
        ["xdg-open", str(dest_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def preview_receipt(cfg: Configuration, filename: str, preview_dir: Path) -> None:
    dest_path = preview_dir / filename
    dest_path.write_bytes(RemoteVM.from_cfg(cfg).fetch_receipt(filename))
    open_document(dest_path)
