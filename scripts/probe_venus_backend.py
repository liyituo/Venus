"""Return a small exit status for the one-click launcher's backend check.

0: Venus backend is healthy; 1: no response; 2: another or unusable service.
The configured API credential stays inside ApiClient and is never printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from venuschat_v1.api_client import ApiClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    client = ApiClient(base=args.base)
    code, body = client.get("/api/v1/ready", timeout=args.timeout)
    if code == 404:  # Reuse an already-running backend from before this route existed.
        code, body = client.get("/api/v1/health", timeout=max(6.0, args.timeout))
        ready = (isinstance(body, dict) and body.get("ok") is True
                 and bool(body.get("version")))
    else:
        ready = (isinstance(body, dict) and body.get("ok") is True
                 and body.get("service") == "venus-llm")
    if code == 200 and ready:
        return 0
    return 1 if code == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
