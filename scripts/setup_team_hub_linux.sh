#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage: scripts/setup_team_hub_linux.sh [--host NODE.ts.net] [--dry-run]

Run as the normal Linux service user from a checkout at ~/venus-hub.
This creates/updates the Python environment, installs a user systemd unit,
starts the local Hub, and configures Tailscale Serve only when no conflicting
Serve/Funnel route exists.
EOF
}

hub_host=""
dry_run=0
while (($#)); do
  case "$1" in
    --host)
      (($# >= 2)) || { usage >&2; exit 2; }
      hub_host="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"
expected_root="$(cd -- "$HOME" && pwd -P)/venus-hub"
if [[ "$repo_root" != "$expected_root" ]]; then
  echo "ERROR: Place the checkout at ~/venus-hub before running this installer." >&2
  exit 1
fi
if [[ "$(id -u)" -eq 0 ]]; then
  echo "ERROR: Run this script as the unprivileged service user, not root." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required." >&2
  exit 1
fi
if ! command -v tailscale >/dev/null 2>&1; then
  echo "ERROR: Install and sign in to Tailscale before configuring the Hub." >&2
  exit 1
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "ERROR: A systemd user manager is unavailable. Log in with this account or enable lingering." >&2
  exit 1
fi

helper="$repo_root/scripts/team_hub_linux.py"
unit_source="$repo_root/scripts/systemd/venus-hub.service"
[[ -f "$helper" && -f "$unit_source" && -f "$repo_root/requirements.txt" ]] || {
  echo "ERROR: Required deployment files are missing from the checkout." >&2
  exit 1
}

host_args=()
if [[ -n "$hub_host" ]]; then
  host_args=(--host "$hub_host")
fi
hub_host="$(python3 "$helper" resolve-host "${host_args[@]}")"
serve_peer="$(tailscale ip -4 | head -n 1)"
if [[ -z "$serve_peer" ]]; then
  echo "ERROR: Cannot detect this node's Tailscale IPv4 address." >&2
  exit 1
fi
python3 "$helper" serve --host "$hub_host" --dry-run >/dev/null

if ((dry_run)); then
  echo "Dry run passed for https://$hub_host/ -> http://127.0.0.1:8001."
  echo "Would install dependencies, write a private host config, enable venus-hub.service, and configure Serve if needed."
  exit 0
fi

venv_python="$repo_root/.venv/bin/python"
if [[ ! -x "$venv_python" ]]; then
  python3 -m venv "$repo_root/.venv"
fi
"$venv_python" -m pip install -r "$repo_root/requirements.txt"

config_dir="$HOME/.config/venus-hub"
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$config_dir" "$unit_dir" "$repo_root/.venus"
chmod 700 "$config_dir" "$repo_root/.venus"
tmp_env="$config_dir/hub.env.tmp.$$"
printf 'VENUS_TEAM_HOST=%s\nVENUS_TEAM_SERVE_PEER=%s\n' "$hub_host" "$serve_peer" > "$tmp_env"
chmod 600 "$tmp_env"
mv -f -- "$tmp_env" "$config_dir/hub.env"
install -m 0644 "$unit_source" "$unit_dir/venus-hub.service"

systemctl --user daemon-reload
systemctl --user enable --now venus-hub.service

ready=0
for _ in {1..40}; do
  if python3 "$helper" check --host "$hub_host" --no-systemd --local-only >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done
if ((ready == 0)); then
  echo "ERROR: Hub did not pass its loopback health check within 20 seconds." >&2
  echo "Inspect with: journalctl --user -u venus-hub.service -n 100 --no-pager" >&2
  exit 1
fi

python3 "$helper" serve --host "$hub_host"
python3 "$helper" check --host "$hub_host"
echo "Hub installed. Restart with: systemctl --user restart venus-hub.service"
if ! loginctl show-user "$USER" --property=Linger --value 2>/dev/null | grep -qx yes; then
  echo "For automatic startup after reboot, an administrator must run: sudo loginctl enable-linger $USER"
fi
