import os
import threading
import time
import requests


class DownloadCancelled(Exception):
  pass


def parse_headers(header_list, drop_content_type=False):
  """Convert the captured Chrome webRequest header list [{name, value}, ...]
  into a plain dict, skipping ones that requests sets itself or that
  Chrome won't let us reuse anyway (e.g. Host, Content-Length). When we're
  reconstructing a multipart body ourselves (drop_content_type=True), the
  captured Content-Type's boundary won't match the one requests generates
  for our reconstructed body, so it must be dropped and let requests set
  its own."""
  skip = {"host", "content-length", "connection"}
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


def probe(url, headers):
  """HEAD request to determine size and whether range requests are supported.
  Some servers lie about Accept-Ranges or omit it but still honor Range, so
  this is a hint, not a guarantee -- the real check happens on first chunk
  request via the returned status code."""
  resp = requests.head(url, headers=headers, allow_redirects=True, timeout=15)
  resp.raise_for_status()
  size = resp.headers.get("Content-Length")
  accepts_ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
  return (int(size) if size is not None else None), accepts_ranges, resp.url


class ProgressTracker:
  """Aggregates byte counts across threads and calls progress_cb at most
  once every `interval` seconds, so a fast local download doesn't flood
  the native messaging pipe with a message per 256KB chunk."""

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



def download_range(url, headers, start, end, out_path, errors, index, cancel_event, tracker):
  """Fetch bytes [start, end] inclusive and write them at the correct offset.
  Raises into `errors[index]` rather than swallowing, so a failed chunk is
  visible instead of silently producing a truncated file."""
  expected_len = end - start + 1
  range_headers = dict(headers)
  range_headers["Range"] = f"bytes={start}-{end}"
  resp = None
  try:
    resp = requests.get(url, headers=range_headers, stream=True, timeout=30)
    if resp.status_code != 206:
      raise RuntimeError(f"expected 206 Partial Content, got {resp.status_code} for range {start}-{end}")

    # Some servers return 206 but ignore Range and send the whole file
    # anyway. Content-Range on a real partial response looks like
    # "bytes start-end/total" -- verify it matches what we asked for.
    content_range = resp.headers.get("Content-Range", "")
    expected_prefix = f"bytes {start}-{end}/"
    if not content_range.startswith(expected_prefix):
      raise RuntimeError(f"server ignored Range request: asked for {start}-{end}, got Content-Range: {content_range!r}")

    content_length = resp.headers.get("Content-Length")
    if content_length is not None and int(content_length) != expected_len:
      raise RuntimeError(f"response length {content_length} != requested range length {expected_len} for {start}-{end}")

    with open(out_path, "r+b") as f:
      f.seek(start)
      written = 0
      for chunk in resp.iter_content(chunk_size=1024 * 256):
        if cancel_event.is_set():
          raise DownloadCancelled()

        remaining = expected_len - written
        if remaining <= 0:
          break

        if len(chunk) > remaining:
          # Hard cap: never write past this thread's assigned
          # window even if the server sent extra bytes.
          chunk = chunk[:remaining]

        f.write(chunk)
        written += len(chunk)
        tracker.add(len(chunk))

      if not cancel_event.is_set() and written != expected_len:
        raise RuntimeError(f"wrote {written} bytes, expected {expected_len} for range {start}-{end}")



  except Exception as e:
    errors[index] = e

  finally:
    if resp is not None:
      resp.close()



def download_single_stream(url, headers, out_path, method="GET", body=None, files=None, cancel_event=None, tracker=None):
  """Handles plain GET downloads, POST-with-raw-body downloads, and
  POST-with-reconstructed-multipart downloads (files= builds the
  multipart body and its matching Content-Type/boundary itself)."""
  resp = requests.request(method, url, headers=headers, data=body, files=files, stream=True, timeout=60)
  try:
    resp.raise_for_status()
    with open(out_path, "wb") as f:
      for chunk in resp.iter_content(chunk_size=1024 * 256):
        if cancel_event is not None and cancel_event.is_set():
          raise DownloadCancelled()

        f.write(chunk)
        if tracker is not None:
          tracker.add(len(chunk))




  finally:
    resp.close()


def multithreaded_download(url, headers, cookie_header, out_path, method="GET", body=None, files=None, num_threads=8, cancel_event=None, progress_cb=None):
  if cancel_event is None:
    cancel_event = threading.Event()

  if cookie_header:
    headers = dict(headers)
    headers["Cookie"] = cookie_header

  if method != "GET":
    # Non-GET (e.g. POST-driven dynamic zip generation) can't be safely
    # split into parallel range requests -- each request may produce
    # different content, so multiple requests would race and corrupt
    # the output rather than fetch slices of one consistent file.
    tracker = ProgressTracker(total=None, progress_cb=progress_cb)
    download_single_stream(url, headers, out_path, method=method, body=body, files=files, cancel_event=cancel_event, tracker=tracker)
    if cancel_event.is_set():
      raise DownloadCancelled()

    tracker.finish()
    return {"mode": "single_stream_non_get", "bytes": os.path.getsize(out_path)}

  size, accepts_ranges, final_url = probe(url, headers)

  if size is None or not accepts_ranges or size < num_threads * 1024 * 1024:
    # Unknown size, no range support, or too small to bother splitting.
    tracker = ProgressTracker(total=size, progress_cb=progress_cb)
    download_single_stream(final_url, headers, out_path, cancel_event=cancel_event, tracker=tracker)
    if cancel_event.is_set():
      raise DownloadCancelled()

    tracker.finish()
    return {"mode": "single_stream", "bytes": os.path.getsize(out_path)}

  # Preallocate the file so each thread can seek+write its own region.
  with open(out_path, "wb") as f:
    f.truncate(size)

  chunk_size = size // num_threads
  ranges = []
  for i in range(num_threads):
    start = i * chunk_size
    end = size - 1 if i == num_threads - 1 else start + chunk_size - 1
    ranges.append((start, end))

  tracker = ProgressTracker(total=size, progress_cb=progress_cb)
  errors = [None] * num_threads
  threads = []
  for i, (start, end) in enumerate(ranges):
    t = threading.Thread(
      target=download_range,
      args=(final_url, headers, start, end, out_path, errors, i, cancel_event, tracker),
    )
    t.start()
    threads.append(t)

  for t in threads:
    t.join()

  if cancel_event.is_set():
    raise DownloadCancelled()

  failures = [e for e in errors if e is not None and not isinstance(e, DownloadCancelled)]
  if failures:
    # Surface the real failure instead of reporting False success on a
    # partially-written file.
    raise RuntimeError(f"{len(failures)}/{num_threads} chunks failed: {failures[0]}")

  tracker.finish()
  actual_size = os.path.getsize(out_path)
  if actual_size != size:
    raise RuntimeError(f"size mismatch after download: expected {size}, got {actual_size}")

  return {"mode": "multithreaded", "threads": num_threads, "bytes": actual_size}
