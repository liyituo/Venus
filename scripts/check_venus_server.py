"""Check a direct server using the same saved TLS/password options as the GUI."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from direct_connection import normalize_origin  # noqa: E402
from venuschat_v1.api_client import ApiClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--ca-file", default=None)
    parser.add_argument("--password-env", default="VENUS_SERVER_PASSWORD")
    args = parser.parse_args()
    try:
        base = normalize_origin(args.base)
        client = ApiClient(base, password=os.environ.get(args.password_env), ca_file=args.ca_file)
        code, result = client.get("/api/v1/ready", timeout=8)
    except (ValueError, OSError) as exc:
        print(f"Connection failed: {type(exc).__name__}")
        return 1
    if code != 200 or result.get("service") != "venus-llm":
        print(f"Connection failed: HTTP {code}; {result.get('detail', '')}")
        return 1
    print(f"Ready: {base}; mode={result.get('mode', 'personal')}; TLS={base.startswith('https://')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
