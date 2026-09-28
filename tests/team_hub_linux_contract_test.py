"""Pure configuration checks for the Linux Tailscale Serve deployment helper."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import team_hub_linux as hub  # noqa: E402


HOST = "venus.example.ts.net"
ROUTE = {"Web": {f"{HOST}:443": {"Handlers": {
    "/": {"Proxy": "http://127.0.0.1:8001"}}}}}


def plan(serve: dict, funnel: dict | None = None) -> str:
    original = hub._json_command
    hub._json_command = lambda argv: (serve if argv[1] == "serve" else (funnel or {}))
    try:
        return hub.serve_plan(HOST)
    finally:
        hub._json_command = original


def denied(serve: dict, funnel: dict | None = None) -> bool:
    try:
        plan(serve, funnel)
    except hub.DeploymentError:
        return True
    return False


def run() -> None:
    assert plan({}) == "configure"
    assert plan(ROUTE) == "already"
    assert plan({"Services": {"svc": ROUTE}}) == "already"
    assert denied({"Web": {f"{HOST}:443": {"Handlers": {
        "/": {"Proxy": "http://127.0.0.1:8002"}}}}})
    assert denied({"Web": {f"{HOST}:443": {"Handlers": {
        "/": {"Proxy": "http://127.0.0.1:8001"},
        "/other": {"Proxy": "http://127.0.0.1:9000"}}}}})
    assert denied({**ROUTE, "TCP": {"22": {"TCPForward": "127.0.0.1:22"}}})
    assert denied({**ROUTE, "AllowFunnel": {f"{HOST}:443": True}})
    assert denied(ROUTE, {"Web": {f"{HOST}:443": {"Handlers": {}}}})
    assert denied(ROUTE, {"Services": {"svc": {
        "Web": {f"{HOST}:443": {"Handlers": {}}}}}})
    print("PASS Serve route, conflict and Funnel fail-closed checks")


if __name__ == "__main__":
    run()
