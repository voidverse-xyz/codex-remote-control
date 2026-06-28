# Codex Remote Control

Set up [Codex](https://chatgpt.com/codex) remote control on a Linux machine and
pair it with the Codex phone app — in one command.

Run the helper on the computer you want to control from your phone. It checks
that everything is ready, starts the remote-control service, and prints a
pairing code to type into the app.

## Requirements

- Linux with **Python 3**
- The **`codex` CLI** installed and on your `PATH`
  ```bash
  curl -fsSL https://chatgpt.com/codex/install.sh | sh
  ```
- A logged-in Codex account (`codex login`)
- Internet access to `chatgpt.com` (including WebSocket connections)

No extra Python packages are needed — the script uses only the standard library.

## Quick start

```bash
python3 codex-pair.py
```

You'll see a checklist, and then a pairing code:

```text
PAIRING CODE:  XXXX-XXXX
```

Open the Codex app on your phone, choose **pair a device**, and enter the code.
By default the script waits until the phone pairs, then offers to start Codex
remote control automatically on boot.

## Options

```bash
python3 codex-pair.py --no-wait        # print the code and exit
python3 codex-pair.py --wait 120       # wait up to 120 seconds for pairing
CODEX_HOME=/custom/path python3 codex-pair.py
```

| Option            | What it does                                                        |
| ----------------- | ------------------------------------------------------------------- |
| `--no-wait`       | Print the pairing code and exit instead of waiting for the phone.   |
| `--wait <seconds>`| Wait up to N seconds for pairing (default: until the code expires). |
| `CODEX_HOME`      | Point at a non-default Codex configuration directory.               |

## Start on boot

After pairing, the script can keep your machine controllable across reboots. It
detects how your system starts background services and sets up the best option
automatically:

- **systemd** — installs a user service and enables it (with linger so it starts
  at boot, before you log in).
- **cron** — falls back to an `@reboot` job when a systemd user service isn't
  available.

You'll be asked before anything is installed.

## Exit codes

- `0` — paired successfully, or `--no-wait` produced a code.
- `1` — a required check failed, or pairing timed out.

## Troubleshooting

- **A check failed.** The checklist prints `[FAIL]` with the reason and the exact
  command to fix it. Resolve it and run the script again.
- **The code expired.** Codes are short-lived. Just re-run the script to mint a
  fresh one.
- **Login expired or was revoked.** Sign in again:
  ```bash
  codex login --device-auth
  ```
- **Paired, but the host shows offline.** The pairing succeeded but the
  remote-control service isn't running. Start it manually:
  ```bash
  codex remote-control start
  ```
