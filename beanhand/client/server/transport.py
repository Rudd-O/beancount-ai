"""Shared transport for the two backend server programs.

The client talks to two separate server programs, which may live on
different VMs:

  * beanhand-documents-server — list / fetch / remove receipts
  * beanhand-ai-server        — talk to the LLM

From inside a VM, IPC to another VM uses:
    qrexec-client-vm <target_vm> <action_name> [rpc_client] [args...]
Only stdin / stdout are relayed between client and server — the RPC action name
determines *which* program on the target VM is invoked (registered via dom0
policy).  When the target is null, the server program is spawned as a local
subprocess.
"""

import subprocess
from typing import IO, ClassVar, TypeVar

from beanhand.client.config import Configuration

_T = TypeVar("_T", bound="ServerTransport")


class ServerTransport:
    """Base class for talking to one server program.

    Subclasses set the ``program`` name of the server binary and the
    ``target_vm_key`` config attribute that names the VM it runs on
    (``null`` meaning: run locally as a subprocess).
    """

    program: ClassVar[str]
    target_vm_key: ClassVar[str]

    def __init__(self, target_vm: str | None) -> None:
        self.target_vm = target_vm

    @classmethod
    def from_cfg(cls: type[_T], cfg: Configuration) -> _T:
        return cls(getattr(cfg, cls.target_vm_key))

    def _call(
        self,
        action: str,
        arg: str | None = None,
    ) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """Start a server process and return its Popen handle (all streams connected)."""
        if arg is not None:
            # arguments must be hex
            arg = arg.encode("utf-8").hex()

        # Local fallback for testing: when target_vm is None, invoke the server
        # program directly as a subprocess.
        if self.target_vm is None:
            cmd = [self.program, "--config", str(Configuration.cfg_path)]
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
