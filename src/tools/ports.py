"""Zero-dependency Windows ctypes listening TCP port inspector and process killer."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import sys
from dataclasses import dataclass

_AF_INET = 2
_AF_INET6 = 23
_TCP_TABLE_OWNER_PID_LISTENER = 3
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INSUFFICIENT_BUFFER = 122

MIN_DEV_PORT = 1024
EPHEMERAL_START = 49152  # Windows dynamic range: RPC listeners, not dev servers
MAX_PORTS = 12
PROTECTED = "service"  # owned by another user or SYSTEM; killing needs admin


@dataclass(frozen=True)
class PortEntry:
    """A listening TCP port and the process that owns it."""

    port: int
    pid: int
    name: str


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _listener_rows(family: int, row_type: type[ctypes.Structure]) -> list[tuple[int, int]]:
    """Return (port, pid) pairs from iphlpapi.GetExtendedTcpTable for one address family."""
    get_table = ctypes.windll.iphlpapi.GetExtendedTcpTable
    size = wintypes.DWORD(0)
    get_table(None, ctypes.byref(size), False, family, _TCP_TABLE_OWNER_PID_LISTENER, 0)
    for _attempt in range(3):  # the table can grow between the size query and the read
        buf = ctypes.create_string_buffer(size.value)
        ret = get_table(buf, ctypes.byref(size), False, family, _TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret == 0:
            break
        if ret != _ERROR_INSUFFICIENT_BUFFER:
            return []
    else:
        return []
    count = wintypes.DWORD.from_buffer(buf).value
    rows = (row_type * count).from_buffer(buf, ctypes.sizeof(wintypes.DWORD))
    # dwLocalPort holds the port in network byte order in its low 16 bits
    return [(((r.dwLocalPort & 0xFF) << 8) | ((r.dwLocalPort >> 8) & 0xFF), int(r.dwOwningPid)) for r in rows]


def process_name(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return PROTECTED
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return PROTECTED
    finally:
        kernel32.CloseHandle(handle)


def list_listening_ports() -> list[PortEntry]:
    """Listening TCP ports in 1024..49151 (IPv4 + IPv6), one entry per port, sorted, capped."""
    if sys.platform != "win32":
        return []
    owners: dict[int, int] = {}
    for family, row_type in ((_AF_INET, _MIB_TCPROW_OWNER_PID), (_AF_INET6, _MIB_TCP6ROW_OWNER_PID)):
        for port, pid in _listener_rows(family, row_type):
            if MIN_DEV_PORT <= port < EPHEMERAL_START and pid > 4:
                owners.setdefault(port, pid)
    names: dict[int, str] = {}
    entries = []
    for port in sorted(owners)[:MAX_PORTS]:
        pid = owners[port]
        if pid not in names:
            names[pid] = process_name(pid)
        entries.append(PortEntry(port=port, pid=pid, name=names[pid]))
    return entries


def can_kill(pid: int) -> bool:
    """Refuse System Idle (0), System (4) and Paperduck itself."""
    return pid > 4 and pid != os.getpid()


def kill_pid(pid: int) -> bool:
    if sys.platform != "win32" or not can_kill(pid):
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)
