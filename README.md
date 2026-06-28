# Codex Remote Control

This directory contains a standalone helper script for setting up Codex remote
control on a Linux host and pairing it with the Codex phone app.

## File

- `codex-rc-up-next.py` - one-shot setup and pairing helper.

## What the script does

The script runs a checklist for a Codex CLI remote-control host, starts the
remote-control daemon, enrolls the host with ChatGPT, mints a manual pairing
code, and optionally waits until the phone claims that code.

It uses only the Python standard library. The host still needs:

- Python 3.
- The `codex` CLI on `PATH`.
- A logged-in Codex account, usually from `codex login`.
- Network access to `chatgpt.com`, including WebSocket access.
- The standalone Codex CLI install for the remote-control daemon.

## Usage

Run the helper on the machine you want to control:

```bash
python3 codex-rc-up-next.py
```

The script prints status lines for each check. If setup reaches the pairing
step, it prints a pairing code for the phone app:

```text
PAIRING CODE:  XXXX-XXXX
```

Enter that code in the Codex phone app under the device-pairing flow.

Useful options:

```bash
python3 codex-rc-up-next.py --no-wait
python3 codex-rc-up-next.py --wait 120
CODEX_HOME=/custom/path python3 codex-rc-up-next.py
```

- `--no-wait` prints the pairing code and exits.
- `--wait 120` waits up to 120 seconds for the phone to pair.
- `CODEX_HOME` points the script at a non-default Codex configuration directory.

## Exit Codes

- `0` means pairing succeeded, or `--no-wait` successfully produced a code.
- `1` means a required check failed or pairing timed out.

## Notes

If the daemon cannot start, the script may still produce a pairing code, but the
host can remain offline until `codex remote-control start` succeeds. If the auth
token has expired or been revoked, re-run login with:

```bash
codex login --device-auth
```
