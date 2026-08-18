"""Tests for proxmox_fleet.ws — minimal RFC 6455 WebSocket client (no network)."""

from __future__ import annotations

import pytest

from proxmox_fleet import ws
from proxmox_fleet.ws import WebSocket, WebSocketError


class _FakeSocket:
    """Scripted socket: a byte stream to feed recv(), records sendall()."""

    def __init__(self, recv_bytes: bytes = b"") -> None:
        self._recv = recv_bytes
        self.sent = b""
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        if not self._recv:
            return b""
        out, self._recv = self._recv[:n], self._recv[n:]
        return out

    def close(self) -> None:
        self.closed = True


def _server_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Build an unmasked server→client frame (FIN + opcode + length + payload)."""
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + length.to_bytes(2, "big")
    else:
        header += bytes([127]) + length.to_bytes(8, "big")
    return header + payload


def _parse_client_frame(data: bytes):
    """Parse a client→server frame (must be masked); return (opcode, payload)."""
    assert data[0] & 0x80  # FIN
    opcode = data[0] & 0x0F
    assert data[1] & 0x80  # masked
    length = data[1] & 0x7F
    off = 2
    if length == 126:
        length = int.from_bytes(data[off:off + 2], "big")
        off += 2
    elif length == 127:
        length = int.from_bytes(data[off:off + 8], "big")
        off += 8
    mask = data[off:off + 4]
    off += 4
    payload = data[off:off + length]
    unmasked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, unmasked


def test_send_text_masks_and_encodes() -> None:
    sock = _FakeSocket()
    ws_conn = WebSocket(sock)
    ws_conn.send_text("hello")
    opcode, payload = _parse_client_frame(sock.sent)
    assert opcode == 0x1
    assert payload == b"hello"


def test_recv_message_reads_text_frame() -> None:
    sock = _FakeSocket(_server_frame(b'{"jsonrpc":"2.0","id":1,"result":{}}'))
    assert WebSocket(sock).recv_message() == '{"jsonrpc":"2.0","id":1,"result":{}}'


def test_recv_message_answers_ping_with_pong() -> None:
    sock = _FakeSocket(_server_frame(b"x", opcode=0x9) + _server_frame(b"ok"))
    conn = WebSocket(sock)
    assert conn.recv_message() == "ok"
    opcode, payload = _parse_client_frame(sock.sent)
    assert opcode == 0xA  # pong
    assert payload == b"x"


def test_recv_message_close_raises() -> None:
    sock = _FakeSocket(_server_frame(b"", opcode=0x8))
    with pytest.raises(WebSocketError, match="closed by peer"):
        WebSocket(sock).recv_message()


def test_rpc_returns_result_and_skips_events() -> None:
    event = b'{"jsonrpc":"2.0","msg":"event","method":"system.ping"}'
    response = b'{"jsonrpc":"2.0","id":1,"result":{"status":"AVAILABLE"}}'
    sock = _FakeSocket(_server_frame(event) + _server_frame(response))
    conn = WebSocket(sock)
    assert conn.rpc("update.check_available", []) == {"status": "AVAILABLE"}


def test_rpc_raises_on_error_result() -> None:
    response = b'{"jsonrpc":"2.0","id":1,"error":{"errname":"ENOTFOUND"}}'
    sock = _FakeSocket(_server_frame(response))
    with pytest.raises(WebSocketError, match="update.check_available failed"):
        WebSocket(sock).rpc("update.check_available", [])


def test_connect_rejects_bad_scheme() -> None:
    with pytest.raises(WebSocketError, match="unsupported websocket scheme"):
        ws.connect("http://example.test/api/current")


def test_connect_handshake_failure(monkeypatch) -> None:
    import socket as socket_mod

    sock = _FakeSocket(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    monkeypatch.setattr(socket_mod, "create_connection", lambda *a, **k: sock)
    with pytest.raises(WebSocketError, match="handshake failed"):
        ws.connect("ws://example.test/api/current")


def test_connect_success_buffers_first_frame(monkeypatch) -> None:
    import socket as socket_mod

    frame = _server_frame(b'{"jsonrpc":"2.0","id":1,"result":null}')
    handshake = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: xxxx\r\n"
        b"\r\n"
    )
    sock = _FakeSocket(handshake + frame)
    monkeypatch.setattr(socket_mod, "create_connection", lambda *a, **k: sock)
    conn = ws.connect("ws://example.test/api/current")
    assert conn.rpc("auth.login_with_api_key", ["k"]) is None
    # handshake request was sent, and the first frame arrived in the same packet
    assert b"GET /api/current HTTP/1.1" in sock.sent
    assert b"Sec-WebSocket-Key:" in sock.sent
    assert b"Sec-WebSocket-Version: 13" in sock.sent
