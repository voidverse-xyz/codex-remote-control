# Codex Linux Pair

Pair a Linux machine with the **ChatGPT Codex app** so you can drive it remotely
from your phone.

## Quick start

Run this on the Linux machine you want to control:

```bash
python3 codex-pair.py
```

It prints a pairing code:

```text
PAIRING CODE:  XXXX-XXXX
```

Open the Codex app, choose **pair a device**, and enter the code. That's it.

## Requirements

- Linux with **Python 3**
- The **`codex` CLI** installed and logged in (`codex login`)
  ```bash
  curl -fsSL https://chatgpt.com/codex/install.sh | sh
  ```
- Internet access to `chatgpt.com` (including WebSocket connections)

No extra Python packages are needed - the script uses only the standard library.

## What the script does

`codex-pair.py` runs the whole setup and prints `[ OK ]` / `[WARN]` / `[FAIL]`
for each step:

1. Checks that the `codex` CLI is on `PATH`.
2. Reads `CODEX_HOME/auth.json` and confirms the token works server-side.
3. Checks WebSocket reachability with `codex doctor`.
4. Verifies the standalone managed Codex install used by the daemon.
5. Starts the remote-control daemon and reports whether it connected.
6. Resolves or creates the local installation id.
7. Enrolls the host and mints a manual pairing code.
8. Waits for the phone to pair, then offers to enable autostart.

If a step fails, it stops and shows the exact command to fix it - resolve it and
run the script again. Warnings do not always stop pairing, but they may explain
why the host pairs and still appears offline.

### Start on boot

After a successful pair, the script asks whether to keep the machine
controllable across reboots (default is **no**, and it asks before installing
anything). Pass `--install-startup` to install it automatically after pairing
instead of prompting. If startup is enabled, it picks the right mechanism
automatically:

- **systemd user service** - writes
  `~/.config/systemd/user/codex-remote-control.service`, enables it, starts it,
  and turns on linger so it can start at boot before you log in.
- **cron** - installs an `@reboot` job when no usable systemd user manager is
  available, including OpenRC-style systems with `crontab`.

Cron startup output is written to
`$CODEX_HOME/remote-control-autostart.log`.

## Options

```bash
python3 codex-pair.py --no-wait        # print the code and exit
python3 codex-pair.py --wait 120       # wait up to 120 seconds for pairing
python3 codex-pair.py --install-startup # install startup automatically after pairing
CODEX_HOME=/custom/path python3 codex-pair.py
```

| Option              | What it does                                                         |
| ------------------- | -------------------------------------------------------------------- |
| `--no-wait`         | Print the pairing code and exit instead of waiting for the phone.    |
| `--wait <seconds>`  | Wait up to N seconds for pairing (default: until the code expires).  |
| `--install-startup` | Enable remote-control startup automatically after successful pairing. |
| `--autostart`       | Alias for `--install-startup`.                                       |
| `CODEX_HOME`        | Point at a non-default Codex configuration directory.                |

> Note: autostart is only offered when the script actually waits for and
> completes a pair, so it is skipped with `--no-wait`, even when
> `--install-startup` is set.

## Exit codes

- `0` - paired successfully, or `--no-wait` produced a code.
- `1` - a required check failed, or pairing timed out.

## Troubleshooting

- **A check failed.** The checklist prints `[FAIL]` with the reason and the exact
  command to fix it. Resolve it and run the script again.
- **The code expired.** Codes are short-lived. Just re-run the script to mint a
  fresh one.
- **Login expired or was revoked.** Sign in again:
  ```bash
  codex login --device-auth
  ```
- **Standalone managed install is missing.** Reinstall the Codex CLI with the
  official installer. The script can still mint a code, but the host will stay
  offline until the daemon can run:
  ```bash
  curl -fsSL https://chatgpt.com/codex/install.sh | sh
  ```
- **Paired, but the host shows offline.** The pairing succeeded but the
  remote-control service isn't running. Start it manually:
  ```bash
  codex remote-control start
  ```
- **Autostart used cron.** Check the startup log:
  ```bash
  tail -n 100 ~/.codex/remote-control-autostart.log
  ```
