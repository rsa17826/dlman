"""Client for a shared aria2c daemon that outlives the native host.

The daemon is started detached (its own session), so when the browser closes
and Chrome kills the native host, downloads handed to aria2 keep running. Its
RPC port and secret live in a 0600 state file so any later host process --
including one started by a different browser -- can reattach.
"""
import fcntl
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time

import requests

RPC_TIMEOUT = 10


class Aria2Error(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Aria2GidNotFound(Aria2Error):
    """aria2 has no record of this download (daemon restarted, or the result
    list was pruned)."""


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.path.join(base, "dlman")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _state_file():
    return os.path.join(state_dir(), "aria2.json")


class Aria2Rpc:
    def __init__(self, port, secret):
        self.url = f"http://127.0.0.1:{port}/jsonrpc"
        self.secret = secret

    def call(self, method, *params, timeout=RPC_TIMEOUT):
        payload = {
            "jsonrpc": "2.0",
            "id": "dlman",
            "method": method,
            "params": [f"token:{self.secret}", *params],
        }
        resp = requests.post(self.url, json=payload, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            raise Aria2Error(f"aria2 returned a non-JSON response (HTTP {resp.status_code})")
        if "error" in data:
            err = data["error"]
            message = err.get("message", "unknown aria2 error")
            if re.search(r"GID .* is not found", message):
                raise Aria2GidNotFound(message, err.get("code"))
            raise Aria2Error(message, err.get("code"))
        return data["result"]

    def get_version(self, timeout=RPC_TIMEOUT):
        return self.call("aria2.getVersion", timeout=timeout)

    def add_uri(self, uri, options):
        return self.call("aria2.addUri", [uri], options)

    def tell_status(self, gid, keys):
        return self.call("aria2.tellStatus", gid, keys)

    def force_remove(self, gid):
        return self.call("aria2.forceRemove", gid)

    def remove_download_result(self, gid):
        return self.call("aria2.removeDownloadResult", gid)


def find_binary():
    path = os.environ.get("DLMAN_ARIA2C") or shutil.which("aria2c")
    if not path:
        raise Aria2Error(
            "aria2c not found: put it on the native host's PATH or set DLMAN_ARIA2C"
        )
    return path


def connect_existing():
    """RPC handle for the daemon recorded in the state file, or None when no
    live daemon answers there (never started, killed, or the port was reused
    by something that doesn't know our secret)."""
    try:
        with open(_state_file()) as f:
            info = json.load(f)
    except FileNotFoundError:
        return None
    rpc = Aria2Rpc(info["port"], info["secret"])
    try:
        rpc.get_version(timeout=2)
    except (requests.RequestException, Aria2Error):
        return None
    return rpc


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_private(path, text):
    """Write a file only the current user can read (it holds the RPC secret)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _spawn_daemon():
    d = state_dir()
    port = _free_port()
    secret = secrets.token_hex(16)

    # Options go in a private config file rather than argv, where `ps` would
    # show the secret to other local users. --conf-path also means the user's
    # own ~/.aria2/aria2.conf is not picked up.
    conf = os.path.join(d, "aria2.conf")
    _write_private(conf, "\n".join([
        "enable-rpc=true",
        f"rpc-listen-port={port}",
        f"rpc-secret={secret}",
        "rpc-listen-all=false",
        "rpc-allow-origin-all=false",
        "max-concurrent-downloads=16",
        "console-log-level=warn",
        "summary-interval=0",
    ]) + "\n")

    log_path = os.path.join(d, "aria2.log")
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [find_binary(), f"--conf-path={conf}"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )

    rpc = Aria2Rpc(port, secret)
    deadline = time.monotonic() + 8
    while True:
        if proc.poll() is not None:
            raise Aria2Error(f"aria2c exited immediately (code {proc.returncode}); see {log_path}")
        try:
            rpc.get_version(timeout=1)
            break
        except requests.RequestException:
            pass  # not accepting connections yet; retry until the deadline
        if time.monotonic() > deadline:
            proc.kill()
            raise Aria2Error(f"aria2c did not accept RPC connections within 8s; see {log_path}")
        time.sleep(0.1)

    _write_private(_state_file(), json.dumps({"port": port, "secret": secret, "pid": proc.pid}))
    return rpc


def ensure_daemon():
    """RPC handle for the shared daemon, starting it if needed. The lock stops
    two hosts (e.g. two browsers) from each spawning their own."""
    with open(os.path.join(state_dir(), "aria2.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return connect_existing() or _spawn_daemon()


def build_options(download_dir, out_name, headers, cookie_header):
    """addUri options for one download. `headers` is the cleaned dict from
    parse_headers; aria2 replaces its built-in User-Agent/Referer when they
    appear here, so they are passed through as ordinary headers."""
    if cookie_header:
        headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}
    header_lines = [f"{k}: {v}" for k, v in headers.items()]
    if cookie_header:
        header_lines.append(f"Cookie: {cookie_header}")

    return {
        "dir": download_dir,
        "out": out_name,
        "header": header_lines,
        "split": "8",
        "max-connection-per-server": "8",
        "min-split-size": "1M",
        # We pick the final name (name_1, name_2, ...) ourselves.
        "allow-overwrite": "false",
        "auto-file-renaming": "false",
        "file-allocation": "none",
        "max-tries": "5",
        "retry-wait": "2",
        "connect-timeout": "15",
        "timeout": "60",
    }
