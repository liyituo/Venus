#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"
server_python="$repo_root/.venv/bin/python"
[[ -x "$server_python" ]] || { echo 'Create .venv and install requirements.txt first.' >&2; exit 1; }
exec "$server_python" "$repo_root/src/llm_server.py" "$@"
