"""Direct access, optional passwords/TLS, enrollment and real SSE transport."""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["VENUS_DATA_DIR"] = tempfile.mkdtemp(prefix="venus_direct_test_")
os.environ["VENUS_DISABLE_MCP"] = "1"
os.environ["VENUS_ALLOW_TEST_HOST"] = "1"

from direct_connection import PasswordGate, PASSWORD_HEADER, normalize_origin  # noqa: E402
from venuschat_v1.api_client import ApiClient, ChatStreamWorker  # noqa: E402
from venuschat_v1 import config_store  # noqa: E402
from venuschat_v1.connection_store import save_connection, connection_password  # noqa: E402
from create_venus_certificate import create_certificate  # noqa: E402


def header(password: str) -> dict:
    return {PASSWORD_HEADER: base64.b64encode(password.encode()).decode()}


def test_server() -> None:
    import llm_server as server
    import team_enrollment as enrollment
    from fastapi.testclient import TestClient

    server.DIRECT_MODE = True
    server.ISOLATED = True
    parser = server.build_server_parser()
    assert parser.parse_args([]).port == 8001
    assert parser.parse_args([]).host == "0.0.0.0"
    remote = TestClient(server.app, base_url="http://192.0.2.10:8001",
                        client=("198.51.100.2", 12345))
    local = TestClient(server.app, base_url="http://127.0.0.1:8001",
                       client=("127.0.0.1", 12345))
    assert remote.get("/api/v1/ready").status_code == 200
    assert local.get("/api/v1/ready").status_code == 200
    assert remote.get("/api/v1/ready", headers={"Host": "attacker.example"}).status_code == 403
    assert remote.get("/api/v1/ready", headers={"Origin": "http://192.0.2.10:8002"}).status_code == 403
    assert remote.get("/api/v1/ready", headers={"Origin": "http://attacker.example"}).status_code == 403
    server.DIRECT_ALLOWED_HOSTS = {"venus.example"}
    assert remote.get("/api/v1/ready", headers={"Host": "venus.example:8001"}).status_code == 200
    server.CONNECTION_PASSWORD = PasswordGate("测试密码 abc")
    assert remote.get("/api/v1/ready").status_code == 401
    assert local.get("/api/v1/ready").status_code == 401
    assert remote.get("/api/v1/ready", headers=header("wrong")).status_code == 401
    assert remote.get("/api/v1/ready", headers={PASSWORD_HEADER: "!invalid"}).status_code == 401
    assert remote.get("/api/v1/ready", headers=header("测试密码 abc")).status_code == 200
    gate = PasswordGate("correct")
    for _ in range(10):
        assert gate.check("wrong", "peer") == 401
    assert gate.check("wrong", "peer") == 429
    assert gate.check("correct", "another-peer") == 200

    server.CONNECTION_PASSWORD = PasswordGate()
    server.TEAM_SERVE_MODE = True
    assert remote.get("/api/v1/ready").json()["mode"] == "team"
    assert remote.get("/api/v1/team/members").status_code == 401
    enrollment.ensure_bootstrap_credential()
    bootstrap_headers = {"X-Team-Bootstrap-Token": enrollment.bootstrap_credential_local()}
    payload = {"team_name": "Direct team", "admin_login": "owner",
               "admin_name": "Owner"}
    assert remote.post("/api/v1/team/bootstrap", json=payload, headers=bootstrap_headers).status_code == 403
    response = local.post("/api/v1/team/bootstrap", json=payload, headers=bootstrap_headers)
    assert response.status_code == 200, response.text
    admin_token = response.json()["device_token"]
    admin = {"X-Team-Device-Token": admin_token}
    assert remote.get("/api/v1/team/me", headers=admin).status_code == 200
    # A remote administrator still cannot use the personal configuration API.
    assert remote.get("/api/v1/config", headers=admin).status_code == 403
    assert local.get("/api/v1/config", headers=admin).status_code == 200
    invite = remote.post("/api/v1/team/invites", headers=admin,
                         json={"expected_login": "member"}).json()
    invite_code = invite.get("invite_code") or invite.get("code")
    assert invite_code, invite
    assert remote.post("/api/v1/team/join/preview", json={"invite_code": "fake"},
                       headers={"Tailscale-User-Login": "member"}).status_code == 401
    claim_secret = "s" * 48
    joined = remote.post("/api/v1/team/join-requests", json={
        "invite_code": invite_code, "display_name": "Member", "device_name": "Laptop",
        "claim_secret": claim_secret, "request_id": "a" * 32})
    assert joined.status_code == 200, joined.text
    application_id = joined.json()["application"]["id"]
    status_path = f"/api/v1/team/join-requests/{application_id}"
    assert remote.get(status_path).status_code == 401
    assert remote.get(status_path, headers={"X-Team-Claim-Secret": claim_secret}).status_code == 200
    reviewed = remote.post(f"/api/v1/team/applications/{application_id}/review", headers=admin,
                           json={"decision": "approve"})
    assert reviewed.status_code == 200, reviewed.text
    assert remote.post(status_path + "/claim", json={"claim_secret": "wrong"}).status_code == 401
    claimed = remote.post(status_path + "/claim", json={"claim_secret": claim_secret})
    assert claimed.status_code == 200, claimed.text
    device = claimed.json()
    member = {"X-Team-Device-Token": device["device_token"]}
    assert remote.get("/api/v1/team/me", headers=member).status_code == 200
    assert remote.get("/api/v1/team/members", headers=member).status_code == 403
    assert remote.get("/api/v1/config", headers=member).status_code == 403
    revoked = remote.post(f"/api/v1/team/devices/{device['device_id']}/revoke", headers=admin)
    assert revoked.status_code == 200, revoked.text
    assert remote.get("/api/v1/team/me", headers=member).status_code == 401


def test_transport(directory: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.server.password and self.headers.get(PASSWORD_HEADER) != header(self.server.password)[PASSWORD_HEADER]:
                self.send_response(401)
                self.end_headers()
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/api/v1/ready")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "service": "venus-llm"}).encode())

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.server.password and self.headers.get(PASSWORD_HEADER) != header(self.server.password)[PASSWORD_HEADER]:
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\nevent: done\ndata: {}\n\n')

    cert, key = create_certificate(directory / "tls", [])
    for tls in (False, True):
        for password in ("", "wire password"):
            http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            http.password = password
            if tls:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                http.socket = context.wrap_socket(http.socket, server_side=True)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = f"{'https' if tls else 'http'}://127.0.0.1:{http.server_port}"
            try:
                if tls:
                    assert ApiClient(base).get("/api/v1/ready")[0] == 0
                client = ApiClient(base, password=password, ca_file=str(cert) if tls else "")
                assert client.get("/api/v1/ready")[0] == 200
                assert client.get("/redirect")[0] == 302
                if password:
                    assert ApiClient(base, password="wrong", ca_file=str(cert) if tls else "").get("/api/v1/ready")[0] == 401
                events, errors = [], []
                done = threading.Event()
                worker = ChatStreamWorker(client, on_event=lambda *event: events.append(event),
                                          on_done=done.set, on_error=lambda e: (errors.append(e), done.set()))
                worker.start({"messages": []})
                assert done.wait(5), "SSE did not finish"
                assert not errors, errors
                assert events == [("delta", ("hello", ""))], events
                save_connection(base, password_enabled=bool(password), password=password,
                                ca_file=str(cert) if tls else "")
                assert connection_password(base) == password
                assert ApiClient(base).get("/api/v1/ready")[0] == 200
                assert "wire password" not in config_store.CONFIG_PATH.read_text(encoding="utf-8")
                assert connection_password("http://127.0.0.1:1") == ""
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=3)


def main() -> None:
    assert normalize_origin("localhost") == "http://localhost:8001"
    assert normalize_origin("192.0.2.1", tls=True) == "https://192.0.2.1:8001"
    assert normalize_origin("[::1]:9000") == "http://[::1]:9000"
    for bad in ("http://user:pass@host", "0.0.0.0", "http://host/path", "host:0", "host:65536"):
        try:
            normalize_origin(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    with tempfile.TemporaryDirectory(prefix="venus_connection_") as tmp:
        directory = Path(tmp)
        with patch.object(config_store, "CONFIG_PATH", directory / "config.json"):
            test_transport(directory)
            test_server()
    print("PASS direct local/remote, all TLS/password combinations, SSE, enrollment and revocation")


if __name__ == "__main__":
    main()
