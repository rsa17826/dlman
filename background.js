// background.js (MV3 service worker)
const pendingRequests = new Map() // url -> {headers, method, body, timestamp}
const inFlight = new Set() // download ids already forwarded, to prevent duplicate sends

// URL patterns to NEVER intercept, e.g. "https://claude.ai/*". Editable from
// the popup. Everything not matching one of these is intercepted by default.
let excludedPatterns = []

async function loadExcludedPatterns() {
  const { excludedPatterns: stored } = await chrome.storage.local.get("excludedPatterns")
  excludedPatterns = stored ?? []
}
loadExcludedPatterns()

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.excludedPatterns) {
    excludedPatterns = changes.excludedPatterns.newValue ?? []
  }
})

function isExcludedUrl(url) {
  return excludedPatterns.some((pattern) => {
    // Patterns are simple "scheme://host[:port]/*" or "*://host/*" style
    // origin prefixes -- match on origin prefix, not full glob syntax.
    const prefix = pattern.replace(/\*$/, "")
    return url.startsWith(prefix) || url.includes(prefix.replace(/^\*:\/\//, ""))
  })
}

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    let body = null
    if (details.requestBody) {
      if (details.requestBody.raw) {
        // Concatenate raw byte chunks and base64-encode for JSON transport.
        const bytes = details.requestBody.raw
          .filter((chunk) => chunk.bytes)
          .flatMap((chunk) => Array.from(new Uint8Array(chunk.bytes)))
        body = btoa(String.fromCharCode(...bytes))
      } else if (details.requestBody.formData) {
        body = { formData: details.requestBody.formData }
      }
    }
    const existing = pendingRequests.get(details.url) || {}
    pendingRequests.set(details.url, {
      ...existing,
      method: details.method,
      body,
      timestamp: Date.now(),
    })
  },
  { urls: ["<all_urls>"] },
  ["requestBody"],
)

chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    const existing = pendingRequests.get(details.url) || {}
    pendingRequests.set(details.url, {
      ...existing,
      headers: details.requestHeaders,
      timestamp: Date.now(),
    })
    // prune old entries so this doesn't grow unbounded
    for (const [url, entry] of pendingRequests) {
      if (Date.now() - entry.timestamp > 30000)
        pendingRequests.delete(url)
    }
  },
  { urls: ["<all_urls>"] },
  ["requestHeaders", "extraHeaders"],
)

async function getCookiesFor(url) {
  const cookies = await chrome.cookies.getAll({ url })
  return cookies.map((c) => `${c.name}=${c.value}`).join("; ")
}

// --- Job tracking for the popup UI ---------------------------------------

async function upsertJob(jobId, patch) {
  const { jobs = {} } = await chrome.storage.local.get("jobs")
  jobs[jobId] = { ...jobs[jobId], ...patch, updatedAt: Date.now() }
  await chrome.storage.local.set({ jobs })
}

// --- Native messaging -----------------------------------------------------

let nativePort
function connectNativeHost() {
  if (nativePort) return nativePort
  nativePort = chrome.runtime.connectNative("com.nyix.dlman")
  nativePort.onDisconnect.addListener(() => {
    if (chrome.runtime.lastError)
      console.error(chrome.runtime.lastError.message)
    nativePort = null
  })
  nativePort.onMessage.addListener((msg) => {
    if (!msg.jobId) return
    if (msg.status === "started") {
      upsertJob(msg.jobId, { status: "downloading" })
    } else if (msg.status === "progress") {
      upsertJob(msg.jobId, { status: "downloading", bytesReceived: msg.bytesReceived, totalBytes: msg.totalBytes })
    } else if (msg.status === "done") {
      upsertJob(msg.jobId, { status: "done", path: msg.path, bytes: msg.bytes, mode: msg.mode })
    } else if (msg.status === "cancelled") {
      upsertJob(msg.jobId, { status: "cancelled" })
    } else if (msg.status === "error") {
      upsertJob(msg.jobId, { status: "error", error: msg.error })
    } else if (msg.status === "file_deleted") {
      removeJob(msg.jobId)
    }
  })
  return nativePort
}

function sendToNativeHost(payload) {
  connectNativeHost().postMessage(payload)
}

async function removeJob(jobId) {
  const { jobs = {} } = await chrome.storage.local.get("jobs")
  delete jobs[jobId]
  await chrome.storage.local.set({ jobs })
}

// --- Popup -> background messaging -----------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "cancel") {
    sendToNativeHost({ action: "cancel", jobId: message.jobId })
  } else if (message.action === "delete") {
    ;(async () => {
      const { jobs = {} } = await chrome.storage.local.get("jobs")
      const job = jobs[message.jobId]
      if (job && job.status === "done" && job.path) {
        // Ask the host to remove the actual file; job entry is removed once
        // it confirms via "file_deleted" so we don't lose track of a file
        // deletion that failed.
        sendToNativeHost({ action: "delete_file", jobId: message.jobId, path: job.path })
      } else {
        // Not a completed download with a file on disk (e.g. still queued,
        // already errored, or already cancelled) -- just drop the record.
        if (job && job.status === "downloading") {
          sendToNativeHost({ action: "cancel", jobId: message.jobId })
        }
        await removeJob(message.jobId)
      }
    })()
  }
  return false
})

// --- Download interception --------------------------------------------------

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  const { id, url, filename, mime, finalUrl } = item
  const target = finalUrl || url

  if (target.startsWith("blob:")) return // not fetchable outside the originating page
  if (isExcludedUrl(target)) return // let Chrome handle this download normally
  if (inFlight.has(id)) return
  inFlight.add(id)

  const jobId = crypto.randomUUID()

  // We're aborting this download entirely, so don't call suggest() --
  // there's no filename decision left for Chrome to make.
  ;(async () => {
    const captured = pendingRequests.get(target) ?? pendingRequests.get(url)
    const cookieHeader = await getCookiesFor(target)

    await upsertJob(jobId, {
      url: target,
      filename: filename || target.split("/").pop(),
      status: "queued",
      startedAt: Date.now(),
    })

    await chrome.downloads.cancel(id)
    await chrome.downloads.erase({ id })

    const payload = {
      jobId,
      url: target,
      filename,
      mime,
      method: captured?.method ?? "GET",
      body: captured?.body ?? null,
      headers: captured?.headers ?? [],
      cookies: cookieHeader,
    }

    sendToNativeHost(payload)
  })()
})
