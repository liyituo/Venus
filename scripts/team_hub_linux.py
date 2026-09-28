#!/usr/bin/env python3
"""Linux deployment checks for a Venus Hub behind Tailscale Serve.

This helper deliberately fails closed when Tailscale Serve already has an
unrelated configuration. It never resets Serve/Funnel state or prints raw
Tailscale command output.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.ts\.net$")
BACKEND_URL = "http://127.0.0.1:8001"
BACKEND_PROXY = "http://127.0.0.1:8001"
SERVICE_NAME = "venus-hub.service"


class DeploymentError(RuntimeError):
    """A deployment check failed with a user-readable, sanitized message."""


def _run(argv: list[str], *, timeout: float = 15) -> str:
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise DeploymentError(f"Required command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeploymentError(f"Command timed out: {argv[0]}") from exc
    if result.returncode:
        # Do not echo arbitrary stdout/stderr. Tailscale may include URLs or
        # local configuration details in its diagnostic output.
        raise DeploymentError(f"Command failed: {' '.join(argv[:3])}")
    return result.stdout.strip()


def _json_command(argv: list[str]) -> dict[str, Any]:
    output = _run(argv)
    if not output:
        return {}
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError(
            f"{argv[0]} returned invalid JSON; update Tailscale and retry"
        ) from exc
    if not isinstance(value, dict):
        raise DeploymentError(f"{argv[0]} returned an unexpected JSON shape")
    return value


def _validated_host(value: str) -> str:
    host = value.strip().rstrip(".").lower()
    if not HOST_RE.fullmatch(host) or ".." in host:
        raise DeploymentError("Hub host must be a valid Tailscale MagicDNS name ending in .ts.net")
    return host


def resolve_host(requested: str | None) -> str:
    status = _json_command(["tailscale", "status", "--json"])
    self_status = status.get("Self")
    if not isinstance(self_status, dict):
        raise DeploymentError("Tailscale is not connected; run tailscale up first")
    detected = _validated_host(str(self_status.get("DNSName") or ""))
    if requested:
        chosen = _validated_host(requested)
        if chosen != detected:
            raise DeploymentError(
                f"Hub host must match this Tailscale node; detected {detected}"
            )
        return chosen
    return detected


def _config_owners(document: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield document
    services = document.get("Services")
    if isinstance(services, dict):
        for service in services.values():
            if isinstance(service, dict):
                yield service


def _web_entries(document: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for owner in _config_owners(document):
        web = owner.get("Web")
        if isinstance(web, dict):
            for authority, details in web.items():
                if isinstance(details, dict):
                    yield str(authority).lower(), details


def _authority_for_host(authority: str, host: str) -> bool:
    return authority in (f"{host}:443", f"{host}:443/tcp")


def _proxy_for_root(details: dict[str, Any]) -> str:
    handlers = details.get("Handlers")
    if not isinstance(handlers, dict):
        return ""
    root = handlers.get("/")
    if not isinstance(root, dict):
        return ""
    return str(root.get("Proxy") or "").rstrip("/")


def _has_active_config(document: dict[str, Any]) -> bool:
    for owner in _config_owners(document):
        for key in ("TCP", "Web"):
            value = owner.get(key)
            if isinstance(value, dict) and value:
                return True
    return False


def _funnel_allowed(details: dict[str, Any]) -> bool:
    """Read both the current bool form and older port-map forms."""
    value = details.get("AllowFunnel")
    if value is True:
        return True
    if isinstance(value, dict):
        return any(bool(enabled) for enabled in value.values())
    return False


def serve_plan(host: str) -> str:
    """Return ``already`` or ``configure``; reject conflicts without mutation."""
    serve = _json_command(["tailscale", "serve", "status", "--json"])
    funnel = _json_command(["tailscale", "funnel", "status", "--json"])

    web_entries = list(_web_entries(serve))
    matching = [details for authority, details in web_entries
                if _authority_for_host(authority, host)]
    if any(_proxy_for_root(details) == BACKEND_PROXY for details in matching):
        # An existing mapping is reusable only when it is the *sole* Serve
        # handler. Other paths, hosts or TCP listeners must be reviewed by an
        # operator rather than being silently kept beside the Hub.
        handlers = matching[0].get("Handlers") if len(matching) == 1 else None
        only_hub_route = (len(web_entries) == 1 and len(matching) == 1
                          and isinstance(handlers, dict) and len(handlers) == 1
                          and isinstance(handlers.get("/"), dict)
                          and set(handlers["/"]) == {"Proxy"}
                          and _proxy_for_root(matching[0]) == BACKEND_PROXY)
        tcp_present = any(bool(owner.get("TCP")) for owner in _config_owners(serve))
        if not only_hub_route or tcp_present:
            raise DeploymentError(
                "Tailscale Serve has another host, path or TCP route; review it before continuing"
            )
        if any(_funnel_allowed(owner) for owner in _config_owners(serve)) \
                or any(_funnel_allowed(details) for details in matching):
            raise DeploymentError(
                "The Hub Serve route also allows Funnel; disable Funnel for this host before continuing"
            )
        if any(
            _authority_for_host(authority, host)
            for authority, _ in _web_entries(funnel)
        ):
            raise DeploymentError(
                "Tailscale Funnel currently uses this host on HTTPS port 443; review it before continuing"
            )
        return "already"

    if _has_active_config(serve):
        raise DeploymentError(
            "Tailscale Serve already has another route; no configuration was changed. "
            "Review `tailscale serve status --json` and configure the Hub route manually."
        )
    if any(_authority_for_host(authority, host) for authority, _ in _web_entries(funnel)):
        raise DeploymentError(
            "Tailscale Funnel already uses this host on HTTPS port 443; no configuration was changed"
        )
    return "configure"


def configure_serve(host: str, *, dry_run: bool) -> None:
    plan = serve_plan(host)
    if plan == "already":
        print(f"Tailscale Serve already routes https://{host}/ to {BACKEND_PROXY}.")
        return
    if dry_run:
        print(f"Would configure Tailscale Serve https://{host}/ -> {BACKEND_PROXY}.")
        return
    _run(
        ["tailscale", "serve", "--bg", "--https=443", BACKEND_PROXY],
        timeout=30,
    )
    if serve_plan(host) != "already":
        raise DeploymentError("Tailscale Serve did not confirm the expected Hub route")
    print(f"Configured tailnet-only HTTPS at https://{host}/.")


def _read_public(url: str, host: str, *, local: bool) -> dict[str, Any]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None

    headers = {"Host": host} if local else {}
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise DeploymentError(f"Hub health endpoint returned HTTP {response.status}")
            body = response.read(64 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        label = "loopback" if local else "Tailscale HTTPS"
        raise DeploymentError(f"Hub {label} health request failed") from exc
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DeploymentError("Hub health endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DeploymentError("Hub health endpoint returned an unexpected response")
    if payload.get("ok") is not True or payload.get("serve_host") != host:
        raise DeploymentError("Hub health response does not match the configured Tailscale host")
    if not isinstance(payload.get("initialized"), bool):
        raise DeploymentError("Hub health response is missing its initialization state")
    return payload


def _check_loopback_listener() -> None:
    proc_net = Path("/proc/net/tcp")
    if not proc_net.is_file():
        raise DeploymentError("Cannot verify loopback binding; /proc/net/tcp is unavailable")
    found_loopback = False
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not table.is_file():
            continue
        try:
            lines = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError as exc:
            raise DeploymentError("Cannot read kernel TCP listener table") from exc
        for line in lines:
            columns = line.split()
            if len(columns) < 4 or columns[3] != "0A":
                continue
            local_address, local_port = columns[1].split(":", 1)
            if int(local_port, 16) != 8001:
                continue
            if table.name == "tcp6":
                raise DeploymentError("Port 8001 has an IPv6 listener; expected IPv4 loopback only")
            address = socket.inet_ntoa(bytes.fromhex(local_address)[::-1])
            if address != "127.0.0.1":
                raise DeploymentError(
                    f"Port 8001 is bound to {address}; expected 127.0.0.1 only"
                )
            found_loopback = True
    if not found_loopback:
        raise DeploymentError("No 127.0.0.1:8001 listener was found")


def check_service_and_health(host: str, *, check_systemd: bool, check_https: bool) -> None:
    if check_systemd:
        _run(["systemctl", "--user", "is-active", "--quiet", SERVICE_NAME])
    _check_loopback_listener()
    local_payload = _read_public(
        f"{BACKEND_URL}/api/v1/team/public", host, local=True
    )
    if check_https:
        if serve_plan(host) != "already":
            raise DeploymentError("Tailscale Serve is not configured for the Hub")
        remote_payload = _read_public(
            f"https://{host}/api/v1/team/public", host, local=False
        )
        if remote_payload.get("initialized") != local_payload.get("initialized"):
            raise DeploymentError("Loopback and Tailscale HTTPS health responses differ")
    initialized = "initialized" if local_payload["initialized"] else "not initialized"
    print(f"Hub is healthy on 127.0.0.1:8001 ({initialized}).")
    if check_https:
        print(f"Tailscale Serve HTTPS is healthy: https://{host}/")


def _cmd_resolve(args: argparse.Namespace) -> None:
    print(resolve_host(args.host))


def _cmd_serve_plan(args: argparse.Namespace) -> None:
    host = resolve_host(args.host)
    configure_serve(host, dry_run=args.dry_run)


def _cmd_health(args: argparse.Namespace) -> None:
    host = resolve_host(args.host)
    check_service_and_health(
        host,
        check_systemd=not args.no_systemd,
        check_https=not args.local_only,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure and check a Linux Venus Hub behind Tailscale Serve"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve-host", help="validate the local Tailscale MagicDNS host")
    resolve.add_argument("--host", help="expected Tailscale host; defaults to this node")
    resolve.set_defaults(func=_cmd_resolve)

    serve = subparsers.add_parser("serve", help="safely configure the Hub Serve route")
    serve.add_argument("--host", help="expected Tailscale host; defaults to this node")
    serve.add_argument("--dry-run", action="store_true", help="show the route change without applying it")
    serve.set_defaults(func=_cmd_serve_plan)

    health = subparsers.add_parser("check", help="check systemd, loopback, Serve and Hub health")
    health.add_argument("--host", help="expected Tailscale host; defaults to this node")
    health.add_argument("--local-only", action="store_true", help="skip the HTTPS request through Serve")
    health.add_argument("--no-systemd", action="store_true", help="skip checking the user systemd unit")
    health.set_defaults(func=_cmd_health)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except DeploymentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
