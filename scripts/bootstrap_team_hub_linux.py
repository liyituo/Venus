#!/usr/bin/env python3
"""Initialize a Linux Hub headlessly and save its admin device token privately.

This script runs as the Hub service user. It reads the local-only bootstrap
credential from secure_store, calls the loopback-only bootstrap API, and never
prints either secret. The returned device token is written to a mode-0600 file
for encrypted transfer to the administrator's private Venus terminal.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import ssl
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_ORIGIN = "http://127.0.0.1:8001"
LOCAL_CA_FILE = ""
PUBLIC_PATH = "/api/v1/team/public"
BOOTSTRAP_PATH = "/api/v1/team/bootstrap"
TRANSFER_DIR = Path.home() / ".local" / "state" / "venus-hub"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(path: str, *, payload: dict[str, Any] | None = None,
             credential: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    password = os.environ.get("VENUS_SERVER_PASSWORD", "")
    if password:
        headers["X-Venus-Password"] = base64.b64encode(password.encode()).decode()
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if credential:
        headers["X-Team-Bootstrap-Token"] = credential
    request = urllib.request.Request(
        LOCAL_ORIGIN + path, data=data, headers=headers,
        method="POST" if payload is not None else "GET",
    )
    handlers = [NoRedirectHandler(), urllib.request.ProxyHandler({})]
    if LOCAL_ORIGIN.startswith("https://"):
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=LOCAL_CA_FILE or None)))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=10) as response:
            status = response.status
            body = response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read(64 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Could not reach the Hub over loopback") from exc
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("The Hub returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("The Hub returned an unexpected response")
    return status, data


def _new_transfer_file() -> tuple[int, Path]:
    TRANSFER_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if TRANSFER_DIR.is_symlink():
        raise RuntimeError("Transfer directory must not be a symbolic link")
    os.chmod(TRANSFER_DIR, 0o700)
    fd, raw_path = tempfile.mkstemp(
        prefix="admin-device-transfer-", suffix=".json", dir=TRANSFER_DIR
    )
    os.chmod(raw_path, 0o600)
    return fd, Path(raw_path)


def _write_transfer(fd: int, payload: dict[str, Any]) -> None:
    stream = os.fdopen(fd, "w", encoding="utf-8")
    with stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    global LOCAL_ORIGIN, LOCAL_CA_FILE
    from direct_connection import normalize_origin
    from urllib.parse import urlsplit
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="", help="客户端访问的服务器地址，默认端口 8001")
    parser.add_argument("--local-base", default=LOCAL_ORIGIN)
    parser.add_argument("--ca-file", default="")
    parser.add_argument("--password", action="store_true")
    args = parser.parse_args()
    LOCAL_ORIGIN = normalize_origin(args.local_base)
    LOCAL_CA_FILE = args.ca_file
    if urlsplit(LOCAL_ORIGIN).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("初始化必须连接 Hub 本机地址")
    if args.password:
        os.environ["VENUS_SERVER_PASSWORD"] = getpass.getpass("Connection password: ")
    if os.name != "posix" or not hasattr(os, "geteuid"):
        print("ERROR: This bootstrap helper is for Linux only.", file=sys.stderr)
        return 1
    if os.geteuid() == 0:
        print("ERROR: Run as the unprivileged Hub service user, not root.", file=sys.stderr)
        return 1

    pending_fd = -1
    pending_path: Path | None = None
    try:
        from team_enrollment import bootstrap_credential_local, normalize_login
        origin = normalize_origin(args.origin or input("Public server IP or URL: "))
        status, public_info = _request(PUBLIC_PATH)
        if status != 200 or public_info.get("ok") is not True:
            raise RuntimeError("The local Hub health endpoint is unavailable")
        if public_info.get("connection_mode") != "direct":
            raise RuntimeError("Start the Hub with --team for direct enrollment")
        if public_info.get("initialized") is True:
            raise RuntimeError("This Hub is already initialized; bootstrap is one-time")
        if public_info.get("initialized") is not False:
            raise RuntimeError("The local Hub did not report an uninitialized state")

        team_name = input("Team name: ").strip()
        admin_login = input("Admin account label: ").strip().casefold()
        admin_name = input("Admin display name: ").strip()
        if not team_name or len(team_name) > 100:
            raise RuntimeError("Team name must be 1–100 characters")
        if not normalize_login(admin_login):
            raise RuntimeError("Enter a valid member account label")
        if not admin_name or len(admin_name) > 80:
            raise RuntimeError("Admin display name must be 1–80 characters")

        credential = bootstrap_credential_local()
        if not credential:
            raise RuntimeError(
                "No local bootstrap credential is available in this service account's secure store"
            )

        pending_fd, pending_path = _new_transfer_file()
        try:
            code, result = _request(
                BOOTSTRAP_PATH,
                payload={
                    "team_name": team_name,
                    "admin_login": admin_login,
                    "admin_name": admin_name,
                    "device_name": "Admin private Venus terminal",
                },
                credential=credential,
            )
        except RuntimeError as exc:
            raise RuntimeError(
                "Bootstrap request outcome is unknown; check the local Hub state and "
                "do not retry until `/api/v1/team/public` confirms it is uninitialized"
            ) from exc
        credential = ""
        if code != 200 or result.get("ok") is not True:
            try:
                _, after = _request(PUBLIC_PATH)
            except RuntimeError:
                after = {}
            if after.get("initialized") is True:
                raise RuntimeError(
                    "Hub is initialized but no admin transfer token was received; "
                    "do not rerun bootstrap, use the Hub's audited recovery process"
                )
            raise RuntimeError(
                f"Hub bootstrap failed with HTTP {code}; no credential value was displayed"
            )

        team = result.get("team")
        user = result.get("user")
        token = str(result.get("device_token") or "")
        device_id = str(result.get("device_id") or "")
        if not isinstance(team, dict) or not isinstance(user, dict) or not token or not device_id:
            raise RuntimeError(
                "Hub initialized but returned incomplete transfer data; do not rerun bootstrap"
            )
        transfer = {
            "format": "venus-team-admin-transfer-v1",
            "origin": origin,
            "team": {
                "team_id": str(team.get("team_id") or ""),
                "team_name": str(team.get("team_name") or team.get("name") or team_name),
            },
            "user": {
                "id": str(user.get("id") or ""),
                "name": str(user.get("name") or admin_name),
                "role": str(user.get("role") or "admin"),
                "tailscale_login": admin_login,
            },
            "device_id": device_id,
            "display_name": admin_name,
            "device_token": token,
        }
        if not transfer["team"]["team_id"] or not transfer["user"]["id"]:
            raise RuntimeError(
                "Hub initialized but returned incomplete identity data; do not rerun bootstrap"
            )
        try:
            _write_transfer(pending_fd, transfer)
            pending_fd = -1
        except OSError as exc:
            raise RuntimeError(
                "Hub initialized but the admin transfer file could not be saved; "
                "do not rerun bootstrap, use the Hub's audited recovery process"
            ) from exc
        token = ""

        print(f"Hub initialized: {transfer['team']['team_name']} ({transfer['team']['team_id']}).")
        print(f"Admin identity: {admin_login}; Hub: {transfer['origin']}.")
        print(f"Private admin device transfer file: {pending_path}")
        print("Transfer this file over encrypted SSH/SCP to the admin's private Venus terminal.")
        print("The file contains the long-lived device token. Do not paste it into chat or logs.")
        pending_path = None
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: Bootstrap helper failed ({type(exc).__name__}); details were suppressed.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    finally:
        if pending_fd >= 0:
            try:
                os.close(pending_fd)
            except OSError:
                pass
        if pending_path is not None:
            pending_path.unlink(missing_ok=True)


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
