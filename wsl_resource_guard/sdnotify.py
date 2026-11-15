"""Minimal sd_notify: readiness and watchdog signals to the service manager."""
from __future__ import annotations

import os
import socket


def notify(state: str) -> bool:
    """Send one state line; False when there is no manager socket or it fails.

    Monitoring must not depend on the manager: every failure is ignored.
    """
    address = os.environ.get('NOTIFY_SOCKET', '')
    if not address or address[0] not in '/@':
        return False
    if address[0] == '@':  # Linux abstract namespace
        address = '\0' + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as connection:
            connection.setblocking(False)
            connection.sendto(state.encode('ascii'), address)
    except (OSError, ValueError):
        return False
    return True
