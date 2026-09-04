#!/usr/bin/env python3
"""
Minimal Chrome native messaging host.

Protocol: each message is a 4-byte little-endian length prefix followed by
that many bytes of UTF-8 JSON. Same framing on stdout for replies. Chrome
launches this process per connectNative() call and kills it when the port
disconnects, so this loop should read until EOF (empty read) and exit.
"""

import base64
import json
import os
import struct
import sys
import threading
import traceback
import urllib.parse

from downloader import multithreaded_download, parse_headers, build_multipart_files, DownloadCancelled

LOG_PATH = os.path.expanduser("/tmp/dlman_debug.log")
send_lock = threading.Lock()

registry_lock = threading.Lock()
cancel_events = {} # jobId -> threading.Event, only valid within this process's lifetime


def resolve_filename(msg_filename, url):
  """Chrome's downloadItem.filename is often empty or just the download
  directory at onCreated time -- the real filename isn't assigned until
  onDeterminingFilename, which we never reach because we cancel earlier.
  Fall back to the URL's path component, which is real data from the
  actual request rather than a made-up default."""
  name = os.path.basename(msg_filename or "")
  if name:
    return name

  path = urllib.parse.urlparse(url).path
  name = os.path.basename(urllib.parse.unquote(path))
  if not name:
    raise ValueError(f"could not determine a filename from URL: {url}")

  return name


def log(msg):
  with open(LOG_PATH, "a") as f:
    f.write(f"{msg}\n")


def read_message():
  raw_length = sys.stdin.buffer.read(4)
  if len(raw_length) == 0:
    return None # Chrome closed the pipe

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
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def handle_job(msg):
  job_id = msg.get("jobId")
  cancel_event = threading.Event()
  with registry_lock:
    cancel_events[job_id] = cancel_event

  # msg has: jobId, url, filename, mime, headers (Chrome webRequest format), cookies, method, body
  url = msg["url"]
  method = msg.get("method", "GET")

  raw_body = msg.get("body")
  body = None
  files = None
  if isinstance(raw_body, str):
    body = base64.b64decode(raw_body)
    headers = parse_headers(msg.get("headers"))
  elif isinstance(raw_body, dict) and "formData" in raw_body:
    files = build_multipart_files(raw_body["formData"])
    # The captured Content-Type carries the *original* multipart
    # boundary, which won't match the one requests generates for our
    # reconstructed body -- drop it so requests sets its own.
    headers = parse_headers(msg.get("headers"), drop_content_type=True)
  elif raw_body is None:
    headers = parse_headers(msg.get("headers"))
  else:
    log(f"unrecognized body shape: {raw_body}")
    send_message({"jobId": job_id, "status": "error", "url": url, "error": f"unrecognized request body shape: {type(raw_body)}"})
    with registry_lock:
      cancel_events.pop(job_id, None)

    return

  cookie_header = msg.get("cookies", "")

  try:
    filename = resolve_filename(msg.get("filename"), url)

  except ValueError as e:
    log(f"filename resolution failed: {e}")
    send_message({"jobId": job_id, "status": "error", "url": url, "error": str(e)})
    with registry_lock:
      cancel_events.pop(job_id, None)

    return

  out_path = os.path.join(os.path.expanduser("~/Downloads"), filename)
  log(f"[{job_id}] downloading to {out_path}")
  send_message({"jobId": job_id, "status": "started", "url": url, "path": out_path})

  def report_progress(downloaded, total):
    send_message({"jobId": job_id, "status": "progress", "bytesReceived": downloaded, "totalBytes": total})

  try:
    result = multithreaded_download(
      url,
      headers,
      cookie_header,
      out_path,
      method=method,
      body=body,
      files=files,
      cancel_event=cancel_event,
      progress_cb=report_progress,
    )
    log(f"[{job_id}] download succeeded: {result}")
    send_message({"jobId": job_id, "status": "done", "url": url, "path": out_path, **result})

  except DownloadCancelled:
    log(f"[{job_id}] download cancelled")
    # Remove the partial file rather than leaving a truncated/incomplete
    # download sitting in ~/Downloads.
    try:
      if os.path.exists(out_path):
        os.remove(out_path)


    except OSError as e:
      log(f"[{job_id}] failed to remove partial file after cancel: {e}")

    send_message({"jobId": job_id, "status": "cancelled", "url": url})

  except Exception as e:
    log(f"[{job_id}] download failed:\n{traceback.format_exc()}")
    send_message({"jobId": job_id, "status": "error", "url": url, "error": str(e)})

  finally:
    with registry_lock:
      cancel_events.pop(job_id, None)



def handle_control(msg):
  action = msg.get("action")
  job_id = msg.get("jobId")

  if action == "cancel":
    with registry_lock:
      event = cancel_events.get(job_id)

    if event is not None:
      event.set()
      log(f"[{job_id}] cancel requested")
    else:
      log(f"[{job_id}] cancel requested but no running job found (already finished?)")
      send_message({"jobId": job_id, "status": "error", "error": "job not running, cannot cancel"})

  elif action == "delete_file":
    # Delete the file for a completed download. Path comes from the
    # extension's own record (it received it in the "done" message),
    # not re-derived here, so this only ever deletes exactly what was
    # reported as downloaded.
    path = msg.get("path")
    if not path:
      send_message({"jobId": job_id, "status": "error", "error": "delete_file requires a path"})
      return

    try:
      os.remove(path)
      log(f"[{job_id}] deleted file: {path}")
      send_message({"jobId": job_id, "status": "file_deleted", "path": path})

    except OSError as e:
      log(f"[{job_id}] failed to delete file {path}: {e}")
      send_message({"jobId": job_id, "status": "error", "error": f"could not delete file: {e}"})

  else:
    log(f"unrecognized control action: {action}")
    send_message({"jobId": job_id, "status": "error", "error": f"unrecognized action: {action}"})


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

    log(f"received message: {msg}")

    if "action" in msg:
      # Control message (cancel/delete_file) -- handle inline, it's
      # fast and must not race behind a slow download thread.
      threading.Thread(target=handle_control, args=(msg,), daemon=True).start()
      continue

    # Run each job on its own thread so a slow download doesn't block
    # reading (and starting) the next one -- otherwise only one
    # download could ever be "running" at a time.
    threading.Thread(target=handle_job, args=(msg,), daemon=True).start()


if __name__ == "__main__":
  main()
