#!/usr/bin/env python3
"""
codex-pair.py — one-shot Codex remote-control setup + checklist.

Walks the whole host-side flow, prints [ OK ] / [WARN] / [FAIL] for each step
with the error detail when something goes wrong, and at the end prints the
PAIRING CODE you type into the phone.

Pure standard library — works on any Linux VM that has python3 and the Codex CLI.

  python3 codex-pair.py            # checklist, print code, then wait for the phone to pair
  python3 codex-pair.py --no-wait  # just print the code and exit, don't wait
  python3 codex-pair.py --wait 120 # cap the wait at 120s (default: until the code expires)
  python3 codex-pair.py --install-startup # install startup automatically after pairing
  CODEX_HOME=/custom python3 codex-pair.py

Exit 0 if paired (or --no-wait and a code was minted); 1 if a check failed or
pairing timed out.
"""
import argparse
import datetime
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
AUTH_FILE = CODEX_HOME / "auth.json"
INSTALL_ID_FILE = CODEX_HOME / "installation_id"
STANDALONE = CODEX_HOME / "packages" / "standalone" / "current" / "codex"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_SERVICE = SYSTEMD_USER_DIR / "codex-remote-control.service"
CRON_MARKER = "# codex-remote-control autostart"
CRON_LOG = CODEX_HOME / "remote-control-autostart.log"

BASE = "https://chatgpt.com/backend-api"
MODELS = "/codex/models"
ENROLL = "/wham/remote/control/server/enroll"
PAIR = "/wham/remote/control/server/pair"
PAIR_STATUS = "/wham/remote/control/server/pair/status"

INSTALL_CMD = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
RULE = "-" * 56  # divider printed between sections

# Timeouts (seconds) for the slower codex subcommands.
DOCTOR_TIMEOUT = 60
DAEMON_START_TIMEOUT = 70

# Pairing wait loop.
POLL_INTERVAL = 3           # seconds between pair/status polls
HEARTBEAT_INTERVAL = 30     # seconds between "still waiting" messages
EXPIRY_SAFETY_MARGIN = 2    # stop polling this many seconds before the code expires
DEFAULT_WAIT_SECONDS = 600  # fallback wait when the expiry timestamp can't be parsed

# Terminal colour codes (ANSI SGR parameters).
BOLD = "1"
GREEN = "1;32"
YELLOW = "1;33"
RED = "1;31"
CYAN = "1;36"

TTY = sys.stdout.isatty()


def col(text, code):
    """Wrap ``text`` in an ANSI colour code, but only when writing to a TTY."""
    return f"\033[{code}m{text}\033[0m" if TTY else text


TAGS = {
    "OK": col("[ OK ]", GREEN),
    "WARN": col("[WARN]", YELLOW),
    "FAIL": col("[FAIL]", RED),
    "INFO": col("[INFO]", CYAN),
}
FAILED = []


def report(status, label, detail=""):
    """Print a status-tagged line plus any indented detail; track FAIL labels."""
    print(f"{TAGS[status]} {label}")
    for line in str(detail).splitlines():
        if line.strip():
            print("       " + line)
    if status == "FAIL":
        FAILED.append(label)


def cmd_error(out, rc):
    """Human-readable failure detail for a run_cmd/run_codex result."""
    return out.strip() or f"exit {rc}"


_CODEX_VERSION = None


def codex_version():
    # The version is stable for the life of the process, so cache the first real
    # answer instead of re-spawning `codex --version` on every HTTP request
    # (the pairing wait loop polls every few seconds).
    global _CODEX_VERSION
    if _CODEX_VERSION:
        return _CODEX_VERSION
    try:
        out = subprocess.check_output(["codex", "--version"], text=True, timeout=20)
        for tok in out.split():
            if tok[:1].isdigit():
                _CODEX_VERSION = tok.split("+")[0]
                return _CODEX_VERSION
    except Exception:
        pass
    return "0.0.0"


def http(method, path, body=None, bearer=None, account=None, timeout=25):
    """Make a JSON request to the backend; return (status_or_None, parsed_body_or_text)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": "codex-cli/" + codex_version(), "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    if account:
        headers["chatgpt-account-id"] = account
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw)
        except Exception:
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def run_codex(args, timeout=60):
    """Run `codex <args>`; return (returncode_or_None, combined_output)."""
    try:
        p = subprocess.run(["codex"] + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "(timed out)"
    except FileNotFoundError:
        return None, "codex not found on PATH"


def run_cmd(args, timeout=30, input_text=None):
    """Run an arbitrary command; return (returncode_or_None, combined_output)."""
    try:
        p = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "(timed out)"
    except FileNotFoundError:
        return None, f"{args[0]} not found on PATH"


def prompt_yes_no(question):
    """Ask a yes/no question; default No, and auto-No when stdin isn't interactive."""
    if not sys.stdin.isatty():
        report("INFO", "skipping autostart prompt", "stdin is not interactive")
        return False

    while True:
        answer = input(f"{question} [y/N] ").strip().lower()
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True
        print("Please answer yes or no.")


def systemd_unit(codex_bin):
    return f"""[Unit]
Description=Codex remote-control daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=CODEX_HOME={CODEX_HOME}
ExecStart={codex_bin} remote-control start
ExecStop={codex_bin} remote-control stop

[Install]
WantedBy=default.target
"""


def systemd_user_available():
    """Return (available, detail) for the current user's systemd manager."""
    if not shutil.which("systemctl"):
        return False, "systemctl not found"
    rc, out = run_cmd(["systemctl", "--user", "show-environment"], timeout=10)
    if rc == 0:
        return True, "systemd user manager available"
    return False, cmd_error(out, rc)


def pid1_name():
    """Return the lowercased name of PID 1, or "" if it can't be read."""
    try:
        return Path("/proc/1/comm").read_text().strip().lower()
    except Exception:
        return ""


def detect_autostart_provider():
    """Pick an autostart mechanism; return (provider, detail)."""
    systemd_ok, systemd_detail = systemd_user_available()
    if systemd_ok:
        return "systemd-user", systemd_detail
    if Path("/run/systemd/system").exists() or pid1_name() == "systemd":
        if shutil.which("crontab"):
            return ("cron",
                    "systemd detected, but the user manager is unavailable "
                    f"({systemd_detail}); using per-user cron fallback")
        return "none", "systemd detected, but the user manager is unavailable: " + systemd_detail
    if Path("/run/openrc/softlevel").exists() or shutil.which("rc-service"):
        return "openrc", "OpenRC detected; using per-user cron fallback"
    if Path("/etc/rc.local").exists():
        return "cron", "rc.local detected; using per-user cron fallback"
    if shutil.which("crontab"):
        return "cron", "using per-user cron fallback"
    return "none", systemd_detail


def enable_systemd_autostart(codex_bin):
    """Install, enable and start the systemd user service, and enable linger."""
    try:
        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        SYSTEMD_SERVICE.write_text(systemd_unit(codex_bin))
        report("OK", "wrote systemd user service", str(SYSTEMD_SERVICE))
    except Exception as e:
        report("FAIL", "could not write systemd user service", str(e))
        return

    rc, out = run_cmd(["systemctl", "--user", "daemon-reload"])
    if rc == 0:
        report("OK", "systemd user daemon reloaded")
    else:
        report("FAIL", "systemd user daemon-reload failed", cmd_error(out, rc))
        return

    rc, out = run_cmd(["systemctl", "--user", "enable", "--now", SYSTEMD_SERVICE.name], timeout=60)
    if rc == 0:
        report("OK", "autostart enabled and service started")
    else:
        report("FAIL", "systemd enable --now failed", cmd_error(out, rc))
        return

    user = os.environ.get("USER")
    if not user:
        report("WARN", "could not enable linger", "USER is not set")
        return

    rc, out = run_cmd(["loginctl", "enable-linger", user], timeout=30)
    if rc == 0:
        report("OK", "linger enabled for boot-time user service startup")
    else:
        report("WARN", "could not enable linger",
               cmd_error(out, rc) +
               "\nThe service is enabled, but it may only start after you log in.")


def cron_entry(codex_bin):
    """Build the @reboot crontab line that starts the remote-control daemon."""
    command = " ".join([
        f"CODEX_HOME={shlex.quote(str(CODEX_HOME))}",
        shlex.quote(codex_bin),
        "remote-control",
        "start",
        ">>",
        shlex.quote(str(CRON_LOG)),
        "2>&1",
        CRON_MARKER,
    ])
    return f"@reboot {command}"


def enable_cron_autostart(codex_bin, provider_detail):
    """Install (or refresh) an @reboot cron job for the remote-control daemon."""
    if not shutil.which("crontab"):
        report("FAIL", "could not enable autostart", "crontab not found")
        return

    report("INFO", provider_detail)
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    rc, out = run_cmd(["crontab", "-l"], timeout=10)
    if rc == 0:
        current = out.splitlines()
    elif "no crontab" in out.lower():
        current = []
    else:
        report("FAIL", "could not read current crontab", cmd_error(out, rc))
        return

    entry = cron_entry(codex_bin)
    updated = [line for line in current if CRON_MARKER not in line]
    updated.append(entry)
    text = "\n".join(updated).rstrip() + "\n"
    rc, out = run_cmd(["crontab", "-"], timeout=10, input_text=text)
    if rc == 0:
        report("OK", "installed @reboot cron autostart", entry)
        report("INFO", "remote-control startup log", str(CRON_LOG))
    else:
        report("FAIL", "could not install crontab", cmd_error(out, rc))


def enable_autostart():
    """Enable boot-time autostart using the best available provider."""
    codex_bin = shutil.which("codex")
    if not codex_bin:
        report("FAIL", "could not enable autostart", "codex not found on PATH")
        return

    provider, detail = detect_autostart_provider()
    report("INFO", "autostart provider", f"{provider}: {detail}")
    if provider == "systemd-user":
        enable_systemd_autostart(codex_bin)
    elif provider in ("openrc", "cron"):
        enable_cron_autostart(codex_bin, detail)
    else:
        report("FAIL", "could not enable autostart",
               "No usable systemd user manager or crontab was found.")


def maybe_enable_autostart():
    if prompt_yes_no("Enable Codex remote-control autostart?"):
        enable_autostart()


def handle_autostart(auto_enable):
    if auto_enable:
        enable_autostart()
    else:
        maybe_enable_autostart()


def finish(pairing_code):
    """Exit 0 when a pairing code was produced, otherwise print a hint and exit 1."""
    if not pairing_code:
        print(RULE)
        print(col("No pairing code produced — fix the FAIL items above and re-run.", RED))
        sys.exit(1)
    sys.exit(0)


def check_codex_cli():
    """Step 1: confirm the codex CLI is on PATH. Exits on failure."""
    if shutil.which("codex"):
        report("OK", f"codex CLI on PATH (v{codex_version()})")
        return
    report("FAIL", "codex CLI not found on PATH", f"Install it: {INSTALL_CMD}")
    finish(None)


def read_auth_tokens():
    """Step 2: load the access token + account id from auth.json. Exits on failure."""
    try:
        tokens = json.loads(AUTH_FILE.read_text()).get("tokens") or {}
        access, account = tokens.get("access_token"), tokens.get("account_id")
    except FileNotFoundError:
        report("FAIL", f"auth.json not found at {AUTH_FILE}", "Run: codex login")
        finish(None)
    except Exception as e:
        report("FAIL", "auth.json unreadable", str(e))
        finish(None)

    if access and account:
        report("OK", "auth.json present with access token + account id")
        return access, account
    report("FAIL", "auth.json missing access_token/account_id", "Run: codex login")
    finish(None)


def check_live_auth(access, account):
    """Step 3: verify the token works server-side (the real check, not `codex login status`)."""
    status, body = http("GET", MODELS + "?client_version=" + codex_version(),
                        bearer=access, account=account)
    if status == 200:
        report("OK", "auth token is valid server-side (HTTP 200)")
        return
    if status == 401:
        code = body.get("error", {}).get("code") if isinstance(body, dict) else None
        report("FAIL", f"auth token rejected (HTTP 401, {code})",
               "Token revoked/expired. Re-login: codex login --device-auth")
        finish(None)
    if status is None:
        report("FAIL", "could not reach chatgpt.com", str(body))
        finish(None)
    report("WARN", f"unexpected auth check status: HTTP {status}", str(body)[:200])


def check_websocket():
    """Step 4 (advisory): read websocket reachability from `codex doctor`."""
    _, out = run_codex(["doctor"], timeout=DOCTOR_TIMEOUT)
    websocket_line = next((l.strip() for l in out.splitlines() if "websocket" in l.lower()), "")
    if "connected" in websocket_line.lower():
        report("OK", "doctor: websocket connected")
    elif websocket_line:
        report("WARN", "doctor: websocket not connected", websocket_line +
               "\nRemote control needs wss://chatgpt.com — check proxy/VPN/firewall.")
    else:
        report("WARN", "could not read websocket status from `codex doctor`")


def check_standalone_install():
    """Step 5 (advisory): the daemon needs the standalone managed install."""
    if STANDALONE.exists():
        report("OK", "standalone managed install present")
        return
    report("WARN", "standalone managed install missing",
           f"Expected: {STANDALONE}\n"
           "`codex remote-control start` needs it (the npm build can't run the daemon).\n"
           f"Install: {INSTALL_CMD}\n"
           "A pairing code can still be minted, but the host won't be controllable until the daemon runs.")


def start_daemon():
    """Step 6: start the remote-control daemon; return True if it reported connected."""
    _, out = run_codex(["remote-control", "start", "--json"], timeout=DAEMON_START_TIMEOUT)
    try:
        payload = json.loads(next(l for l in out.splitlines() if l.strip().startswith("{")))
    except Exception:
        report("WARN", "daemon did not report 'connected'", out.strip()[:300] or "(no output)")
        return False
    if payload.get("status") == "connected":
        report("OK", f"daemon connected as '{payload.get('serverName')}'")
        return True
    report("WARN", f"daemon status: {payload.get('status')}", out.strip()[:300])
    return False


def resolve_installation_id():
    """Step 7: return the persisted installation id, generating and saving one if absent."""
    try:
        install_id = INSTALL_ID_FILE.read_text().strip()
    except Exception:
        install_id = ""
    if not install_id:
        install_id = str(uuid.uuid4())
        try:
            INSTALL_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
            INSTALL_ID_FILE.write_text(install_id)
        except Exception:
            pass
        report("INFO", "no installation_id found; generated one")
    report("OK", "installation id resolved")
    return install_id


def enroll_host(install_id, access, account):
    """Step 8: enroll the host and return the minted server token. Exits on failure."""
    status, body = http("POST", ENROLL, {
        "installation_id": install_id,
        "name": socket.gethostname(),
        "os": platform.system().lower(),
        "arch": platform.machine(),
        "app_server_version": codex_version(),
    }, bearer=access, account=account)
    if status == 200 and isinstance(body, dict) and body.get("remote_control_token"):
        report("OK", f"enrolled (server_id={body.get('server_id')})")
        return body["remote_control_token"]
    report("FAIL", f"enroll failed (HTTP {status})", str(body)[:300])
    finish(None)


def mint_pairing_code(server_token, account):
    """Step 9: mint a manual pairing code; return (manual, full, expires). Exits on failure."""
    status, body = http("POST", PAIR, {"manual_code": True}, bearer=server_token, account=account)
    if status == 200 and isinstance(body, dict) and body.get("manual_pairing_code"):
        report("OK", "pairing code minted")
        return body["manual_pairing_code"], body.get("pairing_code"), body.get("expires_at")
    report("FAIL", f"pair failed (HTTP {status})", str(body)[:300])
    finish(None)


def print_pairing_summary(manual_code, expires_at, daemon_ok):
    """Print the final banner: any failures, the offline note, and the pairing code."""
    print(RULE)
    if FAILED:
        print(col(f"{len(FAILED)} check(s) failed: " + ", ".join(FAILED), RED))
    if not daemon_ok:
        print(col("NOTE: daemon not connected — host will pair but stay OFFLINE "
                  "until `codex remote-control start` succeeds.", YELLOW))
    print()
    print(col("  PAIRING CODE:  " + manual_code, GREEN))
    print(f"  host (server): {socket.gethostname()}")
    print(f"  expires:       {expires_at}")
    print("  Enter it on the phone: Codex -> pair a device.")
    print()


def pairing_wait_window(explicit_wait, expires_at):
    """Seconds to poll for pairing: an explicit --wait, else until the code expires."""
    if explicit_wait is not None:
        return explicit_wait
    try:
        expiry = datetime.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, int((expiry - now).total_seconds()) - EXPIRY_SAFETY_MARGIN)
    except Exception:
        return DEFAULT_WAIT_SECONDS


def wait_for_pairing(server_token, account, full_code, manual_code, window, install_startup):
    """Poll pair/status until claimed or the window elapses; exits the process either way."""
    print(col(f"Waiting for the phone to pair (up to {window}s)...", CYAN))
    deadline = time.time() + window
    last_response = None
    next_heartbeat = time.time() + HEARTBEAT_INTERVAL
    while time.time() < deadline:
        _, response = http("POST", PAIR_STATUS, {"pairing_code": full_code},
                           bearer=server_token, account=account)
        if isinstance(response, dict) and response.get("claimed") is True:
            print(col("\n✅ Paired! The phone is now linked to this host.", GREEN))
            handle_autostart(install_startup)
            finish(manual_code)
        last_response = response
        if time.time() >= next_heartbeat:
            print(f"  still waiting... ({int(deadline - time.time())}s left)")
            next_heartbeat = time.time() + HEARTBEAT_INTERVAL
        time.sleep(POLL_INTERVAL)

    # Timed out — print the last response so the failure is visible.
    print(col("\n❌ Not paired — code was not entered in time (or pairing failed).", RED))
    print(f"   last pair/status response: {last_response}")
    print("   The code has likely expired — re-run this script to mint a fresh one.")
    sys.exit(1)


def parse_args():
    ap = argparse.ArgumentParser(description="Codex remote-control setup + checklist")
    ap.add_argument("--no-wait", action="store_true",
                    help="just print the code and exit; do not wait for the phone to pair")
    ap.add_argument("--wait", type=int, default=None,
                    help="seconds to wait for pairing (default: until the code expires)")
    ap.add_argument("--install-startup", "--autostart", dest="install_startup",
                    action="store_true",
                    help="install Codex remote-control startup automatically after pairing")
    return ap.parse_args()


def main():
    args = parse_args()

    print(col("Codex remote-control checklist", BOLD))
    print(f"CODEX_HOME = {CODEX_HOME}")
    print(RULE)

    check_codex_cli()
    access, account = read_auth_tokens()
    check_live_auth(access, account)
    check_websocket()
    check_standalone_install()
    daemon_ok = start_daemon()
    install_id = resolve_installation_id()
    server_token = enroll_host(install_id, access, account)
    manual_code, full_code, expires_at = mint_pairing_code(server_token, account)

    print_pairing_summary(manual_code, expires_at, daemon_ok)

    if args.no_wait or not full_code:
        finish(manual_code)

    window = pairing_wait_window(args.wait, expires_at)
    wait_for_pairing(server_token, account, full_code, manual_code, window, args.install_startup)


if __name__ == "__main__":
    main()
