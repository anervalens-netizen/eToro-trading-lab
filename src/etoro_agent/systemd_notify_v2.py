from __future__ import annotations

import os
import socket


def notify_systemd(message: str) -> bool:
    """Send a best-effort sd_notify datagram without adding a runtime dependency."""

    address = os.getenv("NOTIFY_SOCKET", "")
    if not address:
        return False
    if address.startswith("@"):  # Linux abstract namespace notation used by systemd.
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.connect(address)
            client.sendall(message.encode("utf-8"))
    except OSError:
        return False
    return True


def ready() -> bool:
    return notify_systemd("READY=1")


def watchdog() -> bool:
    return notify_systemd("WATCHDOG=1")
