#!/usr/bin/env python3
"""
Native messaging host for the download-manager extension.

Protocol: each message is a 4-byte little-endian length prefix followed by that
many bytes of UTF-8 JSON, in both directions.

Two engines:
  * aria2   -- plain GET downloads. Handed to a detached aria2c daemon, so they
               keep running if the browser (and this process) goes away. If the
               browser was closed before one finished, the extension sends a
               "hello" when it next starts and we pick the download back up.
  * python  -- anything with a request body (POST). Streamed by this process,
               so it stops if the browser closes. Written to "<name>.part" and
               renamed only on success, so an interrupted download never looks
               complete.
"""
import base64
import json
import os
import struct
import sys
import threading
import time
import traceback
import urllib.parse

import requests

import aria2_client
from downloader import (
    DownloadCancelled, build_multipart_files, files_identical, next_available_path,
    parse_headers, stream_download,
)

DOWNLOAD_DIR = os.path.expanduser("~/Downloads")
LOG_PATH = os.path.expanduser("~/dlman_debug.log")

send_lock = threading.Lock()
registry_lock = threading.Lock()
python_cancel = {}     # jobId -> Event, built-in jobs running in this process
aria2_jobs = {}        # jobId -> gid, aria2 jobs this process is monitoring
reserved_paths = set() # target paths chosen by in-flight jobs but not created yet

ARIA2_STATUS_KEYS = ["status", "completedLength", "totalLength", "downloadSpeed",
                     "errorCode", "errorMessage"]


def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write(f"{msg}\n")


def describe(msg):
    """Log-safe summary of an incoming message. Headers, cookies and request
    bodies are deliberately left out (they hold credentials), as is the URL's
    query string."""
    url = msg.get("url")
    if url:
        url = urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()
    return f"{msg.get('action') or 'job'} jobId={msg.get('jobId')} method={msg.get('method')} url={url}"


def read_message():
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) == 0:
        return None  # Chrome closed the pipe
    if len(raw_length) < 4:
        raise ValueError(f"truncated length prefix: got {len(raw_length)} bytes")
    length = struct.unpack("<I", raw_length)[0]
    data = sys.stdin.buffer.read(length)
    if len(data) < length:
        raise ValueError(f"truncated message body: expected {length}, got {len(data)}")
    return json.loads(data.decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj).encode("utf-8")
    with send_lock:
        try:
            sys.stdout.buffer.write(struct.pack("<I", len(data)))
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            # The browser has gone away; nobody is listening any more.
            log(f"browser pipe closed, dropped '{obj.get('status')}' for {obj.get('jobId')}")


def reply(job_id, status, **fields):
    send_message({"jobId": job_id, "status": status, **fields})


def spawn(job_id, fn, *args):
    """Run fn on its own thread; an unexpected exception is logged and shown
    on the job instead of killing the thread silently."""
    def run():
        try:
            fn(*args)
        except Exception as e:
            log(f"[{job_id}] unhandled error:\n{traceback.format_exc()}")
            reply(job_id, "error", error=f"{type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()


def resolve_filename(msg_filename, url):
    """Chrome's downloadItem.filename comes from Content-Disposition. Fall back
    to the URL's path component if it's empty."""
    name = os.path.basename(msg_filename or "")
    if name in ("", ".", ".."):
        path = urllib.parse.urlparse(url).path
        name = os.path.basename(urllib.parse.unquote(path))
    if name in ("", ".", ".."):
        raise ValueError(f"could not determine a filename from URL: {url}")
    return name


def require_in_downloads(path):
    """Resolve `path` and refuse anything outside the downloads directory, so a
    bad record can never make us touch other files."""
    real = os.path.realpath(path)
    root = os.path.realpath(DOWNLOAD_DIR)
    if os.path.commonpath([real, root]) != root:
        raise ValueError(f"refusing to touch {path}: outside {DOWNLOAD_DIR}")
    return real


def reserve_path(desired):
    with registry_lock:
        path = next_available_path(desired, taken=reserved_paths)
        reserved_paths.add(path)
        return path


def release_path(path):
    with registry_lock:
        reserved_paths.discard(path)


def remove_if_exists(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def finalize_download(out_path, desired_path):
    """If we had to use a "_1"-style name because desired_path already existed,
    check whether the new file is just a duplicate of it. Identical: drop the
    old copy and take over the clean name. Different: keep both."""
    if out_path == desired_path:
        return out_path
    with registry_lock:
        in_flight = desired_path in reserved_paths
    in_flight = in_flight or os.path.exists(desired_path + ".aria2")
    if in_flight or not os.path.exists(desired_path):
        return out_path  # nothing finished there to compare against
    if files_identical(desired_path, out_path):
        log(f"{out_path} is identical to existing {desired_path}, replacing it")
        os.remove(desired_path)
        os.replace(out_path, desired_path)
        return desired_path
    log(f"{out_path} differs from existing {desired_path}, keeping both")
    return out_path


# --- aria2 engine ---------------------------------------------------------

def finish_from_disk(job_id, out_path, desired_path, expected_total):
    """aria2 no longer knows this download (daemon restarted, or its result
    list was pruned). aria2 deletes the "<file>.aria2" control file when a
    download completes, so decide from what's on disk."""
    complete = os.path.exists(out_path) and not os.path.exists(out_path + ".aria2")
    if complete and expected_total is not None and os.path.getsize(out_path) != expected_total:
        complete = False
    if not complete:
        remove_if_exists(out_path)
        remove_if_exists(out_path + ".aria2")
        reply(job_id, "error", error="interrupted: aria2 lost this download before it finished")
        return
    final = finalize_download(out_path, desired_path)
    reply(job_id, "done", engine="aria2", path=final, bytes=os.path.getsize(final), mode="aria2")


def monitor_aria2(job_id, rpc, gid, out_path, desired_path, expected_total=None):
    failures = 0
    while True:
        try:
            st = rpc.tell_status(gid, ARIA2_STATUS_KEYS)
        except aria2_client.Aria2GidNotFound:
            finish_from_disk(job_id, out_path, desired_path, expected_total)
            return
        except requests.RequestException as e:
            failures += 1
            if failures >= 5:
                reply(job_id, "error", error=f"lost contact with aria2: {e}")
                return
            time.sleep(1)
            continue
        failures = 0

        status = st["status"]
        completed = int(st["completedLength"])
        total = int(st["totalLength"]) or None
        if total:
            expected_total = total

        if status == "active":
            reply(job_id, "progress", engine="aria2", bytesReceived=completed,
                  totalBytes=total, speed=int(st["downloadSpeed"]))
        elif status == "waiting":
            reply(job_id, "queued", engine="aria2")
        elif status == "complete":
            final = finalize_download(out_path, desired_path)
            rpc.remove_download_result(gid)
            log(f"[{job_id}] aria2 download complete: {final}")
            reply(job_id, "done", engine="aria2", path=final,
                  bytes=os.path.getsize(final), mode="aria2")
            return
        elif status == "error":
            remove_if_exists(out_path)
            remove_if_exists(out_path + ".aria2")
            rpc.remove_download_result(gid)
            reply(job_id, "error", error=f"aria2 error {st.get('errorCode')}: {st.get('errorMessage')}")
            return
        elif status == "removed":
            remove_if_exists(out_path)
            remove_if_exists(out_path + ".aria2")
            rpc.remove_download_result(gid)
            reply(job_id, "cancelled")
            return
        time.sleep(0.5)


def run_aria2_job(job_id, url, headers, cookie_header, out_path, desired_path):
    try:
        rpc = aria2_client.ensure_daemon()
        options = aria2_client.build_options(
            os.path.dirname(out_path), os.path.basename(out_path), headers, cookie_header)
        gid = rpc.add_uri(url, options)
        with registry_lock:
            aria2_jobs[job_id] = gid
        log(f"[{job_id}] handed to aria2 as {gid}: {out_path}")
        reply(job_id, "started", engine="aria2", gid=gid, path=out_path, desiredPath=desired_path)
        monitor_aria2(job_id, rpc, gid, out_path, desired_path)
    except aria2_client.Aria2Error as e:
        log(f"[{job_id}] aria2 failed: {e}")
        reply(job_id, "error", error=f"aria2: {e}")
    finally:
        with registry_lock:
            aria2_jobs.pop(job_id, None)
        release_path(out_path)


def adopt_job(rec):
    """Handle one unfinished job the extension remembers from before this host
    started (browser restart, or this host being restarted)."""
    job_id = rec["jobId"]
    gid, path, desired = rec.get("gid"), rec.get("path"), rec.get("desiredPath")

    if rec.get("engine") != "aria2" or not (gid and path and desired):
        # Built-in downloads stop when the browser (or this host) does. Clear
        # their leftover .part file.
        if path:
            try:
                remove_if_exists(require_in_downloads(path + ".part"))
            except ValueError as e:
                log(f"[{job_id}] {e}")
        reply(job_id, "error",
              error="interrupted: the built-in downloader stopped before this finished "
                    "(it stops when the browser closes)")
        return

    path = require_in_downloads(path)
    desired = require_in_downloads(desired)
    with registry_lock:
        aria2_jobs[job_id] = gid
        reserved_paths.add(path)
    try:
        rpc = aria2_client.connect_existing()
        if rpc is None:
            finish_from_disk(job_id, path, desired, rec.get("totalBytes"))
        else:
            log(f"[{job_id}] reattached to aria2 download {gid}")
            monitor_aria2(job_id, rpc, gid, path, desired, rec.get("totalBytes"))
    finally:
        with registry_lock:
            aria2_jobs.pop(job_id, None)
        release_path(path)


# --- built-in engine --------------------------------------------------------

def run_python_job(job_id, url, method, headers, cookie_header, body, files, out_path, desired_path):
    cancel_event = threading.Event()
    with registry_lock:
        python_cancel[job_id] = cancel_event
    part_path = out_path + ".part"
    log(f"[{job_id}] built-in download to {out_path}")
    reply(job_id, "started", engine="python", path=out_path, desiredPath=desired_path)

    def report_progress(downloaded, total):
        reply(job_id, "progress", engine="python", bytesReceived=downloaded, totalBytes=total)

    try:
        result = stream_download(url, headers, cookie_header, part_path, method=method,
                                 body=body, files=files, cancel_event=cancel_event,
                                 progress_cb=report_progress)
        os.replace(part_path, out_path)
        final = finalize_download(out_path, desired_path)
        log(f"[{job_id}] built-in download complete: {final}")
        reply(job_id, "done", engine="python", path=final, **result)
    except DownloadCancelled:
        remove_if_exists(part_path)
        log(f"[{job_id}] cancelled")
        reply(job_id, "cancelled")
    except Exception as e:
        remove_if_exists(part_path)
        log(f"[{job_id}] failed:\n{traceback.format_exc()}")
        reply(job_id, "error", error=str(e))
    finally:
        with registry_lock:
            python_cancel.pop(job_id, None)
        release_path(out_path)


# --- message handlers -------------------------------------------------------

def handle_job(msg):
    job_id = msg["jobId"]
    url = msg["url"]
    method = msg.get("method", "GET")
    cookie_header = msg.get("cookies", "")

    raw_body = msg.get("body")
    body = None
    files = None
    drop_content_type = False
    if isinstance(raw_body, str):
        body = base64.b64decode(raw_body)
    elif isinstance(raw_body, dict) and "formData" in raw_body:
        files = build_multipart_files(raw_body["formData"])
        # The captured Content-Type carries the *original* multipart boundary,
        # which won't match the one requests generates for our body.
        drop_content_type = True
    elif raw_body is not None:
        raise ValueError(f"unrecognized request body shape: {type(raw_body)}")
    headers = parse_headers(msg.get("headers"), drop_content_type=drop_content_type)

    filename = resolve_filename(msg.get("filename"), url)
    desired_path = os.path.join(DOWNLOAD_DIR, filename)
    out_path = reserve_path(desired_path)

    # aria2 can't send a request body, so only plain GETs go to it.
    if method == "GET":
        run_aria2_job(job_id, url, headers, cookie_header, out_path, desired_path)
    else:
        run_python_job(job_id, url, method, headers, cookie_header, body, files,
                       out_path, desired_path)


def handle_hello(msg):
    for rec in msg.get("jobs", []):
        spawn(rec["jobId"], adopt_job, rec)


def handle_control(msg):
    action = msg["action"]
    job_id = msg.get("jobId")

    if action == "cancel":
        with registry_lock:
            event = python_cancel.get(job_id)
            gid = aria2_jobs.get(job_id)
        if event is not None:
            event.set()
        elif gid is not None:
            rpc = aria2_client.connect_existing()
            if rpc is None:
                reply(job_id, "control_error", error="aria2 is not reachable, cannot cancel")
                return
            try:
                # The monitor thread sees status "removed", deletes the partial
                # file and reports "cancelled".
                rpc.force_remove(gid)
            except aria2_client.Aria2GidNotFound:
                log(f"[{job_id}] cancel: aria2 already finished with {gid}")
        else:
            reply(job_id, "control_error", error="job is not running, cannot cancel")

    elif action == "delete_file":
        path = require_in_downloads(msg["path"])
        try:
            os.remove(path)
        except OSError as e:
            log(f"[{job_id}] could not delete {path}: {e}")
            reply(job_id, "control_error", error=f"could not delete file: {e}")
            return
        log(f"[{job_id}] deleted file: {path}")
        reply(job_id, "file_deleted", path=path)

    else:
        raise ValueError(f"unrecognized control action: {action}")


def main():
    log("=== native host started ===")
    while True:
        try:
            msg = read_message()
        except Exception:
            log(f"read_message failed:\n{traceback.format_exc()}")
            break

        if msg is None:
            log("stdin closed, exiting")
            break

        log(f"received {describe(msg)}")
        action = msg.get("action")
        if action == "hello":
            threading.Thread(target=handle_hello, args=(msg,), daemon=True).start()
        elif action:
            spawn(msg.get("jobId"), handle_control, msg)
        else:
            spawn(msg.get("jobId"), handle_job, msg)


if __name__ == "__main__":
    main()
