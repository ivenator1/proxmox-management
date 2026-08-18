"""Minimal RFC 6455 WebSocket client (stdlib only) — manager-local IO.

Used by the TrueNAS JSON-RPC API adapter (``wss://<host>/api/current``),
which is the supported TrueNAS SCALE API going forward (the REST API is
deprecated since 25.04 and removed in 26). Kept small and synchronous:

- one connection, JSON-RPC 2.0 request/response calls via :meth:`WebSocket.rpc`;
- TLS with an optional ``verify`` switch (self-signed appliance certs);
- handles fragmentation, ping/pong keepalives, close frames, and the
  client-side frame masking RFC 6455 requires.

Only stdlib (``socket``/``ssl``/``struct``) — no new runtime dependency.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
from typing import Any, List, Optional
from urllib.parse import urlsplit

_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    """A WebSocket-level failure (handshake, framing, JSON-RPC error, close)."""


def _mask(payload: bytes, mask: bytes) -> bytes:
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class WebSocket:
    """A single client WebSocket connection with JSON-RPC helpers."""

    def __init__(self, sock: Any) -> None:
        self._sock = sock
        self._buffer = b""
        self._next_id = 1

    # -- low-level frame IO --------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        buf = self._buffer
        self._buffer = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise WebSocketError("websocket closed by peer")
            buf += chunk
        if len(buf) > n:
            self._buffer = buf[n:]
            buf = buf[:n]
        return buf

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])  # FIN + opcode
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        self._sock.sendall(bytes(header) + _mask(payload, mask))

    def _recv_frame(self) -> "tuple[int, bytes]":
        """Read one frame; returns (opcode, unmasked payload)."""
        hdr = self._recv_exact(2)
        fin = bool(hdr[0] & 0x80)
        opcode = hdr[0] & 0x0F
        masked = bool(hdr[1] & 0x80)
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = _mask(payload, mask)
        if not fin:
            raise WebSocketError("unexpected fragmented frame from server")
        return opcode, payload

    def recv_message(self) -> str:
        """Read one complete text message; control frames are handled inline."""
        chunks: List[bytes] = []
        while True:
            opcode, payload = self._recv_frame()
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                raise WebSocketError("websocket closed by peer")
            if opcode == _OP_TEXT:
                chunks.append(payload)
                return b"".join(chunks).decode("utf-8", errors="replace")
            if opcode == _OP_BINARY:
                raise WebSocketError("unexpected binary frame from server")

    # -- JSON-RPC ------------------------------------------------------------

    def rpc(self, method: str, params: Any = None) -> Any:
        """Call a JSON-RPC method and return its result (skipping events)."""
        msg_id = self._next_id
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params if params is not None else [],
        }
        self.send_text(json.dumps(request))
        while True:
            try:
                data = json.loads(self.recv_message())
            except ValueError:
                continue  # non-JSON frame (keepalive/junk) — keep reading
            if not isinstance(data, dict) or data.get("id") != msg_id:
                continue  # a pushed event, not our response
            error = data.get("error")
            if error:
                raise WebSocketError(f"TrueNAS {method} failed: {error}")
            return data.get("result")

    def send_text(self, text: str) -> None:
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def close(self) -> None:
        try:
            self._send_frame(_OP_CLOSE, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def connect(
    url: str,
    *,
    timeout: float = 30.0,
    verify: bool = True,
) -> WebSocket:
    """Open a WebSocket connection, performing the RFC 6455 handshake.

    Raises :class:`WebSocketError` on handshake failure, or the underlying
    ``OSError``/``ssl.SSLCertVerificationError`` on connection/TLS problems
    (so callers can classify unreachable vs error exactly like HTTP).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise WebSocketError(f"unsupported websocket scheme: {parts.scheme!r}")
    if parts.scheme == "wss":
        host: Optional[str] = parts.hostname
        port = parts.port or 443
    else:
        host = parts.hostname
        port = parts.port or 80
    if not host:
        raise WebSocketError(f"websocket URL has no host: {url!r}")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if parts.scheme == "wss":
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: proxmox-fleet/1.0\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("utf-8"))

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("no response to websocket handshake")
            response += chunk
        head, _, rest = response.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in b" " + status_line + b" ":
            raise WebSocketError(
                f"websocket handshake failed: {status_line.decode('utf-8', 'replace')}"
            )

        conn = WebSocket(sock)
        conn._buffer = rest  # the server may have sent the first frame already
        return conn
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise
