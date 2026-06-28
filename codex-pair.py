#!/usr/bin/env python3
"""
codex-rc-up.py — one-shot Codex remote-control setup + checklist.

Walks the whole host-side flow, prints [ OK ] / [WARN] / [FAIL] for each step
with the error detail when something goes wrong, and at the end prints the
PAIRING CODE you type into the phone.

Pure standard library — works on any Linux VM that has python3 and the Codex CLI.

  python3 codex-rc-up.py            # checklist, print code, then wait for the phone to pair
  python3 codex-rc-up.py --no-wait  # just print the code and exit, don't wait
  python3 codex-rc-up.py --wait 120 # cap the wait at 120s (default: until the code expires)
  CODEX_HOME=/custom python3 codex-rc-up.py

Exit 0 if paired (or --no-wait and a code was minted); 1 if a check failed or
pairing timed out.
"""
import argparse
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

TTY = sys.stdout.isatty()


def col(s, code):
    return f"\033[{code}m{s}\033[0m" if TTY else s


TAGS = {
    "OK": col("[ OK ]", "1;32"),
    "WARN": col("[WARN]", "1;33"),
    "FAIL": col("[FAIL]", "1;31"),
    "INFO": col("[INFO]", "1;36"),
}
FAILED = []


def report(status, label, detail=""):
    print(f"{TAGS[status]} {label}")
    for line in str(detail).splitlines():
        if line.strip():
            print("       " + line)
    if status == "FAIL":
        FAILED.append(label)


def codex_version():
    try:
        out = subprocess.check_output(["codex", "--version"], text=True, timeout=20)
        for tok in out.split():
            if tok[:1].isdigit():
                return tok.split("+")[0]
    except Exception:
        pass
    return "0.0.0"


def http(method, path, body=None, bearer=None, account=None, timeout=25):
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
    try:
        p = subprocess.run(["codex"] + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "(timed out)"
    except FileNotFoundError:
        return None, "codex not found on PATH"


def run_cmd(args, timeout=30, input_text=None):
    try:
        p = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "(timed out)"
    except FileNotFoundError:
        return None, f"{args[0]} not found on PATH"


def prompt_yes_no(question):
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
    if not shutil.which("systemctl"):
        return False, "systemctl not found"
    rc, out = run_cmd(["systemctl", "--user", "show-environment"], timeout=10)
    if rc == 0:
        return True, "systemd user manager available"
    return False, out.strip() or f"exit {rc}"


def pid1_name():
    try:
        return Path("/proc/1/comm").read_text().strip().lower()
    except Exception:
        return ""


def detect_autostart_provider():
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
    try:
        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        SYSTEMD_SERVICE.write_text(systemd_unit(codex_bin))
        report("OK", f"wrote systemd user service", str(SYSTEMD_SERVICE))
    except Exception as e:
        report("FAIL", "could not write systemd user service", str(e))
        return

    rc, out = run_cmd(["systemctl", "--user", "daemon-reload"])
    if rc == 0:
        report("OK", "systemd user daemon reloaded")
    else:
        report("FAIL", "systemd user daemon-reload failed", out.strip() or f"exit {rc}")
        return

    rc, out = run_cmd(["systemctl", "--user", "enable", "--now", SYSTEMD_SERVICE.name], timeout=60)
    if rc == 0:
        report("OK", "autostart enabled and service started")
    else:
        report("FAIL", "systemd enable --now failed", out.strip() or f"exit {rc}")
        return

    user = os.environ.get("USER")
    if user:
        rc, out = run_cmd(["loginctl", "enable-linger", user], timeout=30)
        if rc == 0:
            report("OK", "linger enabled for boot-time user service startup")
        else:
            report("WARN", "could not enable linger",
                   (out.strip() or f"exit {rc}") +
                   "\nThe service is enabled, but it may only start after you log in.")
    else:
        report("WARN", "could not enable linger", "USER is not set")


def cron_entry(codex_bin):
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
        report("FAIL", "could not read current crontab", out.strip() or f"exit {rc}")
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
        report("FAIL", "could not install crontab", out.strip() or f"exit {rc}")


def enable_autostart():
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


def main():
    ap = argparse.ArgumentParser(description="Codex remote-control setup + checklist")
    ap.add_argument("--no-wait", action="store_true",
                    help="just print the code and exit; do not wait for the phone to pair")
    ap.add_argument("--wait", type=int, default=None,
                    help="seconds to wait for pairing (default: until the code expires)")
    args = ap.parse_args()

    print(col("Codex remote-control checklist", "1"))
    print(f"CODEX_HOME = {CODEX_HOME}")
    print("-" * 56)

    access = account = None
    server_token = None

    # 1. codex CLI present ----------------------------------------------------
    if shutil.which("codex"):
        report("OK", f"codex CLI on PATH (v{codex_version()})")
    else:
        report("FAIL", "codex CLI not found on PATH",
               "Install it: curl -fsSL https://chatgpt.com/codex/install.sh | sh")
        return finish(None)

    # 2. auth.json readable ---------------------------------------------------
    try:
        tokens = (json.loads(AUTH_FILE.read_text()).get("tokens") or {})
        access, account = tokens.get("access_token"), tokens.get("account_id")
        if access and account:
            report("OK", "auth.json present with access token + account id")
        else:
            report("FAIL", "auth.json missing access_token/account_id",
                   "Run: codex login")
            return finish(None)
    except FileNotFoundError:
        report("FAIL", f"auth.json not found at {AUTH_FILE}", "Run: codex login")
        return finish(None)
    except Exception as e:
        report("FAIL", "auth.json unreadable", str(e))
        return finish(None)

    # 3. live auth check (the real one — not `codex login status`) ------------
    status, body = http("GET", MODELS + "?client_version=" + codex_version(),
                        bearer=access, account=account)
    if status == 200:
        report("OK", "auth token is valid server-side (HTTP 200)")
    elif status == 401:
        code = body.get("error", {}).get("code") if isinstance(body, dict) else None
        report("FAIL", f"auth token rejected (HTTP 401, {code})",
               "Token revoked/expired. Re-login: codex login --device-auth")
        return finish(None)
    elif status is None:
        report("FAIL", "could not reach chatgpt.com", str(body))
        return finish(None)
    else:
        report("WARN", f"unexpected auth check status: HTTP {status}", str(body)[:200])

    # 4. websocket reachability (advisory) ------------------------------------
    rc, out = run_codex(["doctor"], timeout=60)
    wsline = next((l.strip() for l in out.splitlines() if "websocket" in l.lower()), "")
    if "connected" in wsline.lower():
        report("OK", "doctor: websocket connected")
    elif wsline:
        report("WARN", "doctor: websocket not connected", wsline +
               "\nRemote control needs wss://chatgpt.com — check proxy/VPN/firewall.")
    else:
        report("WARN", "could not read websocket status from `codex doctor`")

    # 5. standalone managed install (needed for the daemon) -------------------
    if STANDALONE.exists():
        report("OK", "standalone managed install present")
    else:
        report("WARN", "standalone managed install missing",
               f"Expected: {STANDALONE}\n"
               "`codex remote-control start` needs it (the npm build can't run the daemon).\n"
               "Install: curl -fsSL https://chatgpt.com/codex/install.sh | sh\n"
               "A pairing code can still be minted, but the host won't be controllable until the daemon runs.")

    # 6. start the remote-control daemon --------------------------------------
    rc, out = run_codex(["remote-control", "start", "--json"], timeout=70)
    daemon_ok = False
    try:
        j = json.loads(next(l for l in out.splitlines() if l.strip().startswith("{")))
        if j.get("status") == "connected":
            report("OK", f"daemon connected as '{j.get('serverName')}'")
            daemon_ok = True
        else:
            report("WARN", f"daemon status: {j.get('status')}", out.strip()[:300])
    except Exception:
        report("WARN", "daemon did not report 'connected'", out.strip()[:300] or "(no output)")

    # 7. installation id ------------------------------------------------------
    try:
        install_id = INSTALL_ID_FILE.read_text().strip()
    except Exception:
        install_id = str(uuid.uuid4())
        try:
            INSTALL_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
            INSTALL_ID_FILE.write_text(install_id)
        except Exception:
            pass
        report("INFO", "no installation_id found; generated one")
    if install_id:
        report("OK", "installation id resolved")

    # 8. enroll (mint the server token) ---------------------------------------
    status, body = http("POST", ENROLL, {
        "installation_id": install_id,
        "name": socket.gethostname(),
        "os": platform.system().lower(),
        "arch": platform.machine(),
        "app_server_version": codex_version(),
    }, bearer=access, account=account)
    if status == 200 and isinstance(body, dict) and body.get("remote_control_token"):
        server_token = body["remote_control_token"]
        report("OK", f"enrolled (server_id={body.get('server_id')})")
    else:
        report("FAIL", f"enroll failed (HTTP {status})", str(body)[:300])
        return finish(None)

    # 9. mint the pairing code ------------------------------------------------
    status, body = http("POST", PAIR, {"manual_code": True}, bearer=server_token, account=account)
    if status == 200 and isinstance(body, dict) and body.get("manual_pairing_code"):
        manual = body["manual_pairing_code"]
        full = body.get("pairing_code")
        expires = body.get("expires_at")
        report("OK", "pairing code minted")
    else:
        report("FAIL", f"pair failed (HTTP {status})", str(body)[:300])
        return finish(None)

    # ---- final output -------------------------------------------------------
    print("-" * 56)
    if FAILED:
        print(col(f"{len(FAILED)} check(s) failed: " + ", ".join(FAILED), "1;31"))
    if not daemon_ok:
        print(col("NOTE: daemon not connected — host will pair but stay OFFLINE "
                  "until `codex remote-control start` succeeds.", "1;33"))
    print()
    print(col("  PAIRING CODE:  " + manual, "1;32"))
    print(f"  host (server): {socket.gethostname()}")
    print(f"  expires:       {expires}")
    print("  Enter it on the phone: Codex -> pair a device.")
    print()

    if args.no_wait or not full:
        return finish(manual)

    # Wait for the phone to pair (default behaviour).
    import datetime
    if args.wait is not None:
        window = args.wait
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            window = max(0, int((dt - now).total_seconds()) - 2)
        except Exception:
            window = 600

    print(col(f"Waiting for the phone to pair (up to {window}s)...", "1;36"))
    deadline = time.time() + window
    last = None
    next_beat = time.time() + 30
    while time.time() < deadline:
        st, sb = http("POST", PAIR_STATUS, {"pairing_code": full},
                      bearer=server_token, account=account)
        if isinstance(sb, dict) and sb.get("claimed") is True:
            print(col("\n✅ Paired! The phone is now linked to this host.", "1;32"))
            maybe_enable_autostart()
            return finish(manual)
        last = sb
        if time.time() >= next_beat:
            print(f"  still waiting... ({int(deadline - time.time())}s left)")
            next_beat = time.time() + 30
        time.sleep(3)

    # Timed out — print the output so the failure is visible.
    print(col("\n❌ Not paired — code was not entered in time (or pairing failed).", "1;31"))
    print(f"   last pair/status response: {last}")
    print("   The code has likely expired — re-run this script to mint a fresh one.")
    sys.exit(1)


def finish(code):
    if not code:
        print("-" * 56)
        print(col("No pairing code produced — fix the FAIL items above and re-run.", "1;31"))
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
