#!/usr/bin/env python3
"""Import a headless Hub admin device transfer into this Venus terminal."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import urllib.error
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
os.umask(0o077)





class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_transfer(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Choose a regular transfer file, not a symbolic link")
    if path.stat().st_size > 64 * 1024:
        raise ValueError("Transfer file is unexpectedly large")
    if os.name == "posix":
        os.chmod(path, 0o600)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("format") != "venus-team-admin-transfer-v1":
        raise ValueError("Unrecognized Venus admin transfer format")
    origin = str(value.get("origin") or "")
    from venuschat_v1.config_store import normalize_team_origin
    origin = normalize_team_origin(origin)
    team = value.get("team")
    user = value.get("user")
    if not isinstance(team, dict) or not isinstance(user, dict):
        raise ValueError("Transfer is missing team or user details")
    if (not team.get("team_id") or not team.get("team_name")
            or not user.get("id") or user.get("role") != "admin"
            or not value.get("device_id") or not value.get("device_token")):
        raise ValueError("Transfer is missing admin device information")
    value["origin"] = origin
    return value


def _verify_remote_device(origin: str, token: str) -> tuple[int, dict]:
    from venuschat_v1.api_client import ApiClient
    client = ApiClient(origin, token=token, token_header="X-Team-Device-Token",
                       use_default_token=False, deny_redirects=True,
                       password=os.environ.get("VENUS_SERVER_PASSWORD"),
                       ca_file=os.environ.get("VENUS_TLS_CA_FILE"))
    return client.get("/api/v1/team/me", timeout=12)


def main() -> int:
    print("Import the admin device token from the Hub's private transfer file.")
    raw_path = input("Transfer file path: ").strip().strip('"')
    path = Path(raw_path).expanduser()
    try:
        transfer = _read_transfer(path)
        team = transfer["team"]
        user = transfer["user"]
        print()
        print(f"Hub: {transfer['origin']}")
        print(f"Team: {team['team_name']} ({team['team_id']})")
        print(f"Admin: {user.get('name') or ''} <{user.get('tailscale_login') or ''}>")
        if input("Import this admin device into this Venus installation? [y/N] ").strip().casefold() != "y":
            print("Cancelled; the transfer file was kept.")
            return 1

        from venuschat_v1.config_store import llm_base, save_team_connection

        token = str(transfer["device_token"])
        status, identity = _verify_remote_device(transfer["origin"], token)
        returned_user = identity.get("user") if isinstance(identity, dict) else None
        returned_team = identity.get("team") if isinstance(identity, dict) else None
        returned_device = identity.get("device") if isinstance(identity, dict) else None
        if (status != 200 or not isinstance(returned_user, dict)
                or not isinstance(returned_team, dict)
                or not isinstance(returned_device, dict)
                or returned_user.get("id") != user.get("id")
                or returned_user.get("role") != "admin"
                or returned_team.get("team_id") != team.get("team_id")
                or returned_device.get("id") != transfer.get("device_id")):
            print("ERROR: Hub rejected the device token or connection password. No credentials were imported.", file=sys.stderr)
            return 1

        if "VENUS_SERVER_PASSWORD" in os.environ or "VENUS_TLS_CA_FILE" in os.environ:
            from venuschat_v1.connection_store import save_connection
            password = os.environ.get("VENUS_SERVER_PASSWORD", "")
            save_connection(transfer["origin"], password_enabled=bool(password), password=password,
                            ca_file=os.environ.get("VENUS_TLS_CA_FILE", ""))
        save_team_connection({
            "origin": transfer["origin"],
            "team_id": str(team["team_id"]),
            "team_name": str(team["team_name"]),
            "user_id": str(user["id"]),
            "device_id": str(transfer["device_id"]),
            "display_name": str(transfer.get("display_name") or user.get("name") or "Hub admin"),
            "status": "joined",
            "previous_llm_base": llm_base(),
        }, device_token=token)
        token = ""
        try:
            path.unlink()
        except OSError:
            print("WARNING: Imported successfully, but remove the temporary transfer file manually.", file=sys.stderr)
        print("Admin device imported into this Venus installation's secure store.")
        print("Restart VenusChat and open Settings → Team & Members to connect to the saved Hub.")
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: Import failed ({type(exc).__name__}); details were suppressed.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nCancelled; no credentials were imported.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
