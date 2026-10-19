"""Helpers shared by the native host: header cleanup, collision-safe naming,
duplicate detection, and the built-in single-stream downloader.

The built-in downloader only handles requests aria2 can't (POST with a body).
Plain GET downloads go to aria2 -- see aria2_client.py.
"""
import hashlib
import os
import threading
import time

import requests


class DownloadCancelled(Exception):
    pass


# Headers describing the *browser's* transfer state rather than the resource
# being requested. Forwarding them breaks downloads:
#  - accept-encoding: aria2 forwards it verbatim but can't decode br/zstd, so
#    a compressed response would be saved corrupted.
#  - range / if-*: a browser revalidation or media-range request would make
#    the server answer 206/304 instead of the full file.
_SKIPPED_HEADERS = {
    "host", "content-length", "connection", "accept-encoding", "range",
    "if-range", "if-none-match", "if-modified-since", "if-match",
    "if-unmodified-since",
}


def parse_headers(header_list, drop_content_type=False):
    """Convert the captured Chrome webRequest header list [{name, value}, ...]
    into a plain dict, dropping headers that must not be replayed. When we're
    reconstructing a multipart body ourselves (drop_content_type=True), the
    captured Content-Type's boundary won't match the one requests generates
    for our reconstructed body, so it is dropped too and requests sets its own."""
    skip = set(_SKIPPED_HEADERS)
    if drop_content_type:
        skip.add("content-type")
    out = {}
    for h in header_list or []:
        name = h.get("name", "")
        if name.lower() in skip:
            continue
        out[name] = h.get("value", "")
    return out


def build_multipart_files(form_data):
    """Chrome's webRequest.onBeforeRequest gives multipart form fields back
    already parsed into {field_name: [value, ...]}, not raw bytes. Rebuild
    an equivalent multipart body via requests' `files` param (using (None,
    value) tuples forces multipart encoding for plain text fields, since
    requests only uses multipart when `files` is passed). Repeated field
    names become repeated entries, matching how the original form would
    have sent them."""
    entries = []
    for name, values in form_data.items():
        for value in values:
            entries.append((name, (None, value)))
    return entries


def next_available_path(path, taken=()):
    """First of "name.ext", "name_1.ext", "name_2.ext", ... that doesn't exist
    on disk and isn't in `taken` (paths other in-flight downloads have
    reserved but not created yet)."""
    base, ext = os.path.splitext(path)
    candidate = path
    n = 0
    while os.path.exists(candidate) or candidate in taken:
        n += 1
        candidate = f"{base}_{n}{ext}"
    return candidate


def files_identical(path_a, path_b, chunk_size=1024 * 1024):
    """Compare two files' content without loading either fully into memory.
    Cheap size check first, then a streaming hash comparison."""
    if os.path.getsize(path_a) != os.path.getsize(path_b):
        return False

    hash_a = hashlib.sha256()
    hash_b = hashlib.sha256()
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        while True:
            chunk_a = fa.read(chunk_size)
            chunk_b = fb.read(chunk_size)
            if not chunk_a and not chunk_b:
                break
            hash_a.update(chunk_a)
            hash_b.update(chunk_b)
    return hash_a.digest() == hash_b.digest()


class ProgressTracker:
    """Calls progress_cb at most once every `interval` seconds, so a fast local
    download doesn't flood the native messaging pipe with a message per chunk."""

    def __init__(self, total, progress_cb, interval=0.25):
        self.total = total
        self.progress_cb = progress_cb
        self.interval = interval
        self.downloaded = 0
        self.last_report = 0.0
        self.lock = threading.Lock()

    def add(self, n):
        if self.progress_cb is None:
            return
        with self.lock:
            self.downloaded += n
            now = time.monotonic()
            if now - self.last_report >= self.interval:
                self.last_report = now
                self.progress_cb(self.downloaded, self.total)

    def finish(self):
        if self.progress_cb is not None:
            self.progress_cb(self.downloaded, self.total)


def stream_download(url, headers, cookie_header, out_path, method="GET", body=None,
                    files=None, cancel_event=None, progress_cb=None):
    """Single-connection download with any method/body. Used for POST-driven
    downloads (e.g. a form that generates a zip server-side), which can't be
    handed to aria2 and can't be split into ranges because each request may
    regenerate different content."""
    headers = dict(headers)
    # Ask for the bytes as stored: requests only decodes some content
    # encodings, so a compressed response could otherwise be saved corrupted.
    headers["Accept-Encoding"] = "identity"
    if cookie_header:
        headers["Cookie"] = cookie_header

    resp = requests.request(
        method, url, headers=headers, data=body, files=files, stream=True, timeout=60
    )
    try:
        resp.raise_for_status()
        length = resp.headers.get("Content-Length")
        tracker = ProgressTracker(int(length) if length is not None else None, progress_cb)
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled()
                f.write(chunk)
                tracker.add(len(chunk))
        tracker.finish()
    finally:
        resp.close()

    return {"mode": "single_stream", "bytes": os.path.getsize(out_path)}
