# Linux Venus Hub deployment

This guide runs the Hub on Linux as an isolated backend behind Tailscale Serve HTTPS. The backend listens only on `127.0.0.1:8001`; Tailscale Serve provides the tailnet URL and identity headers. Funnel is not used.

The deployment automation is designed for a checkout at `~/venus-hub` and a normal, non-root service user. It uses a systemd user service, so the service account owns its `.venus` data. The Windows launch scripts remain available for Windows Hub deployments.

## Prerequisites

- A Linux host with systemd, Python 3 and `python3-venv`.
- Tailscale 1.52 or later installed, signed in, and connected on this host. MagicDNS must provide this node's `*.ts.net` name.
- Tailscale HTTPS certificates enabled for the tailnet. The Hub setup command will not reset an existing Serve configuration.
- A code checkout at `~/venus-hub`. Use a deployment branch or release revision, and note its Git commit before upgrades.

On Debian or Ubuntu, install the OS dependencies as an administrator, then make sure Tailscale starts at boot:

```sh
sudo apt-get update
sudo apt-get install -y python3 python3-venv git age
sudo systemctl enable --now tailscaled
```

Clone or update the project as the intended service user. Keep the checkout and `.venus` private to this account. Do not put API keys, invitation codes, claim codes, device credentials, or other secrets in Git, shell arguments, or unit files.

For the user service to start without an interactive login after reboot, run the following while signed in as the intended Hub service user (with `sudo` rights). `$USER` must name that service user:

```sh
sudo loginctl enable-linger "$USER"
```

## Install and start

From the service user's checkout:

```sh
cd ~/venus-hub
bash scripts/setup_team_hub_linux.sh --dry-run
bash scripts/setup_team_hub_linux.sh
```

The installer resolves the host from `tailscale status --json`. An optional `--host hub-name.example.ts.net` must exactly match the local node's MagicDNS name. The script creates `.venv` if needed, installs `requirements.txt`, writes the non-secret host setting to `~/.config/venus-hub/hub.env` with mode `0600`, installs the user unit, and starts it.

Before changing Serve, the installer reads machine-readable Serve and Funnel status. The current Tailscale CLI reference documents `tailscale serve --bg --https=443 <target>` and `tailscale serve status --json`; current status JSON exposes Serve configuration with `Web` handlers and can nest entries under `Services`. The helper handles both node-level and nested forms, and fails closed if a different route is present. See the [Tailscale CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve) and the [current Serve status implementation](https://github.com/tailscale/tailscale/blob/main/cmd/tailscale/cli/serve_v2.go).

It accepts the existing route only if HTTPS port 443 already maps the exact root path to `http://127.0.0.1:8001`. If there is no existing Serve configuration, it adds that route with `tailscale serve --bg --https=443 http://127.0.0.1:8001`. If it detects another route or an active Funnel route for this host, it stops and leaves the Tailscale configuration untouched. Review the existing route yourself before making any change.

The route is tailnet-only. Tailscale documents that Serve shares a local service within the tailnet, requires HTTPS to be enabled in the tailnet, and persists a background Serve configuration across reboots and Tailscale restarts. Funnel is the separate public Internet feature. See [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) and the [Serve CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve).

## Health and service control

Run the combined check from the checkout:

```sh
bash scripts/check_team_hub_linux.sh
```

It checks the user systemd service, confirms there is an IPv4 loopback listener on port 8001 and no IPv6 or non-loopback listener on that port, calls `/api/v1/team/public` through loopback, verifies the Serve route, and calls the same health endpoint over HTTPS.

Use systemd for lifecycle control:

```sh
systemctl --user status venus-hub.service
systemctl --user restart venus-hub.service
systemctl --user stop venus-hub.service
systemctl --user start venus-hub.service
journalctl --user -u venus-hub.service -n 100 --no-pager
```

The application log is `.venus/server.log` (with rotation). The systemd unit uses `UMask=0077` and runs with `NoNewPrivileges` and a private temporary directory. Keep the Linux account dedicated to Hub administration; Tailscale Serve provides network access control, while Linux administrators retain host-level access to the files.

## Headless first-time initialization

The Hub does not need a graphical desktop for initial setup. After the service is running and the local health check passes, run this command as the same Linux service user (never with `sudo`):

```sh
cd ~/venus-hub
python3 scripts/bootstrap_team_hub_linux.py
```

Enter the team name, the admin's exact Tailscale login, and the admin display name when prompted. The helper reads the one-time bootstrap credential from the service account's local `secure_store`, verifies that the loopback Hub is still uninitialized and matches the host's MagicDNS name, and posts only to `127.0.0.1`. It does not print the bootstrap credential or the returned device token. It writes the admin device transfer JSON under `~/.local/state/venus-hub/` with mode `0600`.

Transfer the file path printed by the helper over encrypted SSH/SCP to the admin's private Venus terminal. For example, from the private terminal, use OpenSSH's `scp -p` with the printed server path and a destination under the signed-in user's profile. The password or SSH key stays in the SSH client; the token is never an argument. The receiving Venus installation must be on Tailscale, and its signed-in Tailscale login must match the admin login entered during setup.

In the private Venus checkout, run the importer as the same OS user that runs VenusChat:

```sh
python scripts/import_team_admin_device.py
```

Use `.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on Linux if the system Python does not have the project environment. The importer displays the Hub, team, and admin identity for confirmation, verifies the token over HTTPS without following redirects or using a proxy, stores it through Venus' existing secure-store integration (DPAPI on Windows, mode-`0600` file on Linux), and deletes the received transfer file after success. Restart VenusChat and open **Settings → Team & Members** to connect to the saved Hub. After confirming the terminal works, remove the server-side transfer file too; deleting a file entry does not guarantee physical erasure, so keep the server disk encrypted.

If bootstrap reports an unknown request outcome, or the Hub is initialized but no transfer file was written, do not rerun bootstrap. Check `/api/v1/team/public` locally first. The current backend does not expose a remote or headless token reissue/export endpoint; an initialized Hub with no saved transfer file needs the Hub's audited administrator recovery procedure. Protect the service user's `~/.local/state/venus-hub/` and `.venus/secrets.json` as credential-bearing state.

## Existing Serve routes and recovery

Inspect current routes without changing them:

```sh
tailscale serve status --json
tailscale funnel status --json
```

The setup script intentionally does not run `tailscale serve reset`, `tailscale funnel reset`, or `off` against an existing route. If the Hub's `https://<node>.ts.net/` root already serves another application, select a different deployment design or arrange a reviewed route change manually. Do not add the Hub to an existing public Funnel listener.

If a later operator intentionally needs to remove only the Hub mapping, first inspect the current status and use the exact `tailscale serve --https=443 off` operation only when that HTTPS listener is dedicated to the Hub. That command removes the listener configuration on port 443; it can affect other paths sharing the listener. Prefer keeping the Serve route in place during ordinary application stops and restarts.

## Back up before upgrades

Back up while the service is stopped so SQLite is consistent and no project/task files are changing. The `.venus` directory holds the Hub's JSON state, secure-store files, project data and repositories, `project_access.db` for project access, and `worker_calls.db` for Worker calls and votes. Preserve the current application commit alongside the backup.

Example: create a compressed archive and encrypt it to an operator-held `age` public recipient. The public recipient is not a secret; never put an `age` private key or backup passphrase in a command line or log.

```sh
set -o pipefail
backup_dir="$HOME/venus-hub-backups"
mkdir -m 700 -p "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
git -C "$HOME/venus-hub" rev-parse HEAD > "$backup_dir/$stamp.commit"
systemctl --user stop venus-hub.service
tar -C "$HOME/venus-hub" -czf - .venus \
  | age -r 'age1replace-with-your-backup-recipient' \
      > "$backup_dir/$stamp.venus.tar.gz.age"
systemctl --user start venus-hub.service
```

Store the encrypted archive off-host with restrictive permissions and verify that a test copy decrypts and lists the expected files. The unencrypted stream exists only in the pipe; do not write it to a temporary file. Keep the recipient's private key outside this server. Back up any externally configured `VENUS_DATA_DIR` separately if you set one.

Before applying a release or schema change, record the current commit, stop the service, and take this backup. Update the checkout while the service is stopped. Recreate or update the virtual environment as needed. If the release includes the project-access migration, follow the dry-run and apply commands below before starting the service. For a release without a migration, start the service after updating and run the health check.

For the project access metadata migration, run its dry run against this Hub's data directory and inspect its output before applying:

```sh
python3 scripts/migrate_project_access.py --data-dir "$HOME/venus-hub/.venus"
```

After reviewing the dry-run output, keep the service stopped, complete the encrypted backup above if you have not already, and apply with the recorded encrypted backup path:

```sh
systemctl --user stop venus-hub.service
python3 scripts/migrate_project_access.py \
  --data-dir "$HOME/venus-hub/.venus" \
  --apply \
  --backup-path "$backup_dir/$stamp.venus.tar.gz.age"
systemctl --user start venus-hub.service
bash scripts/check_team_hub_linux.sh
```

Use the migration script's own output to confirm what it changed. If rollback is needed, stop the service and restore both the matching application revision and the encrypted `.venus` backup; this includes restoring `project_access.db` to the pre-migration state.

## Restore and roll back

1. Stop the service and preserve the current checkout and data directory for investigation; do not overwrite the only copy.
2. Restore the application checkout to the recorded commit that matches the database schema in the backup.
3. Decrypt the archive and extract `.venus` into `~/venus-hub`. Keep it owned by the service account and set the directory mode to `0700`; ensure secret-bearing files are no more permissive than `0600`.
4. Rebuild the virtual environment from that checkout's `requirements.txt`, start `venus-hub.service`, and run `scripts/check_team_hub_linux.sh`.
5. If the restored Hub starts with an incompatible schema, stop it and restore the matching application revision and data backup together. Do not delete the pre-restore copy until the Hub and a representative project have been checked.

Example restore commands (replace the file name with the selected backup):

```sh
systemctl --user stop venus-hub.service
age -d -i /secure/off-host/age-identity.txt \
  "$HOME/venus-hub-backups/20260927T000000Z.venus.tar.gz.age" \
  | tar -C "$HOME/venus-hub" -xzf -
chmod 700 "$HOME/venus-hub/.venus"
systemctl --user start venus-hub.service
bash "$HOME/venus-hub/scripts/check_team_hub_linux.sh"
```

The `age` identity is read from a protected key file; it is not included in the command text. A restore replaces Hub state, including `project_access.db`, so use it only with the application revision compatible with that backup. Do not mark a migration or dual-terminal acceptance as successful unless those operations were actually run.

## Two-terminal acceptance run

Use two different Tailscale users and two enrolled Venus devices. Record the Hub revision, MagicDNS host, device display codes, project IDs, and pass/fail results without recording claim codes, invite codes, device tokens, or Worker lease tokens.

1. On terminal A, create and claim project A. On terminal B, create and claim project B. Confirm neither terminal can open the other's project, tasks, changes, or audit.
2. A invites B by its display code and user identity into project A. B previews and accepts the one-time invitation. Confirm B can now see A but remains owner of B; replaying the invitation must fail.
3. A creates a project A team Job. Confirm B sees its events and only project A worktree changes. Submit a Git change, have a distinct eligible member review the exact SHA, and verify the merge follows the configured vote threshold.
4. On B, explicitly authorize a dedicated temporary directory for `workspace.list`, `workspace.read`, and `workspace.write`. A submits a read task to B and verifies the project vote and result. Then A submits a write task: with only A and B in project A, it must remain pending because a write requires two distinct non-author approvals. For a complete write test, enroll a third user and device C, invite C into project A, obtain B and C's votes, and verify B displays the path, content preview, and digest before creating a new file. Try a second write to the same path; it must not overwrite.
5. Revoke B's project A device grant while a new Worker call is waiting or leased. Confirm subsequent project A requests and Worker preflight fail, while B's project B access still succeeds. Also verify B's local emergency stop prevents further polling.
6. Restart the Hub and both terminals. Confirm membership, project selection, task history, change reviews and Worker call status recover without crossing projects. Run `bash scripts/check_team_hub_linux.sh` again and inspect the project audit trail.

The repository's automated tests use temporary data and a simulated Serve host. Keep this real run's results with the deployment record before treating the Hub as operational for a team.

## Known deployment limits

- This is a single-node, single-user systemd deployment. It does not provide high availability or a separate privilege boundary from Linux administrators.
- Tailscale Serve keeps the backend on loopback; it does not replace project-level authorization inside the Hub.
- The current Linux secure-store implementation uses file permissions rather than an OS keyring. Use a dedicated service account, inspect effective file permissions, encrypt backups, and rotate credentials after suspected exposure.
- The setup/check scripts can verify the local process and Tailscale route from the Hub. They do not prove remote client enrollment, project authorization behavior, or a two-terminal workflow.
