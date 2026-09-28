"""Focused contracts for persistent installation identity and old Hub support."""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1.api_client import ApiClient, installation_code_confirmation
from venuschat_v1.terminal_identity import (
    _format_random_bytes,
    _lock_file,
    get_installation_code,
)


def _read_installation_code(path: str) -> str:
    return get_installation_code(path)


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="venus-installation-code-") as folder:
        target = Path(folder) / "installation_code.txt"
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=6, mp_context=multiprocessing.get_context("spawn")) as pool:
            process_codes = list(pool.map(_read_installation_code,
                                          [str(target)] * 12))
        assert len(set(process_codes)) == 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(lambda _index: get_installation_code(target), range(48)))
        assert len(set(codes)) == 1
        code = codes[0]
        assert code.startswith("VI-") and len(code[3:].replace("-", "")) == 26
        assert get_installation_code(target) == code
        assert target.read_text(encoding="ascii").strip() == code
        assert not list(Path(folder).glob("installation_code.txt.tmp-*"))

        second = get_installation_code(Path(folder) / "other" / "installation_code.txt")
        assert second != code

    deterministic = _format_random_bytes(bytes(range(16)))
    assert deterministic.startswith("VI-")
    assert all(char in "0123456789ABCDEFGHJKMNPQRSTVWXYZ-" for char in deterministic[3:])

    assert installation_code_confirmation(
        {"application": {"id": "old-hub-ignored-code"}}, code) == ("", False)
    assert installation_code_confirmation(
        {"application": {"installation_code": code}}, code) == ("bound", True)
    assert installation_code_confirmation(
        {"installation_code_binding": "bound"}, code) == ("bound", True)
    assert installation_code_confirmation(
        {"installation_code_binding": "legacy"}, code) == ("legacy", False)
    assert installation_code_confirmation(
        {"installation_code": "VI-OTHER-CODE"}, code) == ("", False)

    with tempfile.TemporaryDirectory(prefix="venus-installation-lock-") as folder:
        lock_path = Path(folder) / "held.lock"
        if os.name == "nt":
            import msvcrt

            locker = patch.object(msvcrt, "locking", side_effect=OSError("lock held"))
        else:
            import fcntl

            locker = patch.object(fcntl, "flock", side_effect=BlockingIOError("lock held"))
        with locker, patch("venuschat_v1.terminal_identity.time.monotonic",
                           side_effect=(1.0, 5.0)):
            try:
                _lock_file(lock_path)
            except TimeoutError:
                pass
            else:
                raise AssertionError("Installation identity lock must time out")

    client = ApiClient("https://hub.example.ts.net", use_default_token=False)
    client.post = Mock(return_value=(200, {"application": {"id": "app_1"}}))
    status, _data, sent = client.post_with_optional_installation_code(
        "/api/v1/team/join-requests",
        {"invite_code": "once", "installation_code": code}, timeout=8)
    assert status == 200 and sent
    client.post.assert_called_once_with(
        "/api/v1/team/join-requests",
        {"invite_code": "once", "installation_code": code}, timeout=8)

    client.post = Mock(side_effect=(
        (422, {"detail": [{"loc": ["body", "installation_code"],
                            "type": "extra_forbidden"}]}),
        (200, {"application": {"id": "legacy"}}),
    ))
    status, _data, sent = client.post_with_optional_installation_code(
        "/api/v1/team/join-requests",
        {"invite_code": "once", "installation_code": code}, timeout=8)
    assert status == 200 and not sent
    assert client.post.call_args_list[1].args == (
        "/api/v1/team/join-requests", {"invite_code": "once"})

    client.post = Mock(return_value=(409, {"detail": "installation code mismatch"}))
    status, _data, sent = client.post_with_optional_installation_code(
        "/api/v1/team/join-requests/app_1/claim",
        {"claim_secret": "temporary", "installation_code": code})
    assert status == 409 and sent
    client.post.assert_called_once()

    print("PASS Venus installation identity contract")


if __name__ == "__main__":
    run()
