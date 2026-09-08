"""Client-side access to the documents server (beanhand-documents-server).

This is the program that talks about receipts: listing, fetching and removing
them.  It runs on the VM named by the ``documents.vm`` config key, or locally
when that key is absent.
"""

import os
import subprocess
from pathlib import Path
from typing import ClassVar, Literal, cast

from beanhand.client.server.transport import ServerTransport
from beanhand.structs import FetchedReceipt, load_json


class DocumentsClient(ServerTransport):
    """Relay client subcommands to the documents server."""

    program: ClassVar[str] = "beanhand-documents-server"
    target_vm_key: ClassVar[str] = "documents_target_vm"

    def list_receipts(
        self, category: Literal["unassociated"] | Literal["uningested"]
    ) -> list[str]:
        """Return receipt filenames from the documents server.

        Raises on qrexec transport error; prints to stderr and returns ``[]``
        when the JSON cannot be decoded.
        """

        cmd, proc, stdin, stdout = self._call(
            "beanhand.List"
            + ("Uningested" if category == "uningested" else "Unassociated"),
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

    def fetch_receipt(self, filename: str) -> FetchedReceipt:
        cmd, proc, stdin, stdout = self._call("beanhand.Fetch", arg=filename)
        stdin.close()

        try:
            return FetchedReceipt.load(stdout)
        finally:
            ret = proc.wait()
            if ret != 0:
                raise subprocess.CalledProcessError(ret, cmd)

    def remove_receipt(self, filename: str) -> None:
        cmd, proc, stdin, _ = self._call("beanhand.Remove", arg=filename)
        stdin.close()

        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)


def save_receipt(path: Path, fetched: FetchedReceipt) -> None:
    """Write *fetched* to *path*, preserving the server-side modification time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fetched.data)
    # os.utime with a (atime, mtime) tuple sets both; here we want the file to
    # appear as if it was last modified at the timestamp the server reported.
    os.utime(path, (fetched.timestamp, fetched.timestamp))


def open_document(dest_path: Path) -> None:
    subprocess.Popen(
        ["xdg-open", str(dest_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def preview_receipt(vm: DocumentsClient, filename: str, preview_dir: Path) -> None:
    dest_path = preview_dir / filename
    save_receipt(dest_path, vm.fetch_receipt(filename))
    open_document(dest_path)
