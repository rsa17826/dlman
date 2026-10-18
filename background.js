// background.js (MV3 service worker)
const HOST_NAME = "com.nyix.dlman"

// One per service-worker lifetime. The native host -- and every download the
// built-in engine is running inside it -- lives and dies with this worker, so
// an unfinished job stamped with a different instance belongs to a previous
// browser session.
const INSTANCE = crypto.randomUUID()
const ACTIVE = new Set(["queued", "downloading"])

const pendingRequests = new Map() // url -> {headers, method, body, timestamp}
const inFlight = new Set() // download ids already forwarded, to prevent duplicate sends

// URL patterns to NEVER intercept, e.g. "https://claude.ai/*". Editable from
// the popup. Everything not matching one of these is intercepted by default.
let excludedPatterns = []

async function loadExcludedPatterns() {
  const { excludedPatterns: stored } = await chrome.storage.local.get(
    "excludedPatterns",
  )
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
    return (
      url.startsWith(prefix) ||
      url.includes(prefix.replace(/^\*:\/\//, ""))
    )
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

// --- Job records (what the popup shows) -------------------------------------

// Every change is a read-modify-write of one storage key, so changes are run
// one at a time. Unserialized, two jobs reporting progress at once overwrite
// each other and a finished job can be left stuck on "downloading".
let jobsQueue = Promise.resolve()
function mutateJobs(fn) {
  const run = jobsQueue.then(async () => {
    const { jobs = {} } = await chrome.storage.local.get("jobs")
    if (fn(jobs) === false) return // nothing changed
    await chrome.storage.local.set({ jobs })
  })
  jobsQueue = run.catch(() => {}) // keep the queue moving; callers still see the rejection via `run`
  return run
}

function createJob(jobId, fields) {
  return mutateJobs((jobs) => {
    jobs[jobId] = { ...fields, updatedAt: Date.now() }
  })
}

// Only ever updates an existing record: a late message from the host for a
// job the user already deleted must not bring it back as a nameless ghost.
function updateJob(jobId, patch, { onlyIfActive = false } = {}) {
  return mutateJobs((jobs) => {
    const job = jobs[jobId]
    if (!job) return false
    if (onlyIfActive && !ACTIVE.has(job.status)) return false
    jobs[jobId] = { ...job, ...patch, updatedAt: Date.now() }
  })
}

function removeJob(jobId) {
  return mutateJobs((jobs) => {
    delete jobs[jobId]
  })
}

// --- Native messaging -----------------------------------------------------

let nativePort
let reconnects = 0

function connectNativeHost() {
  if (nativePort) return nativePort
  const port = chrome.runtime.connectNative(HOST_NAME)
  nativePort = port
  port.onMessage.addListener(onHostMessage)
  port.onDisconnect.addListener(() => onHostDisconnect(port))
  return port
}

function sendToNativeHost(payload) {
  connectNativeHost().postMessage(payload)
}

function onHostMessage(msg) {
  reconnects = 0 // the host is alive and talking
  if (!msg.jobId) return
  switch (msg.status) {
    case "started":
      updateJob(
        msg.jobId,
        {
          status: "downloading",
          engine: msg.engine,
          gid: msg.gid,
          path: msg.path,
          desiredPath: msg.desiredPath,
        },
        { onlyIfActive: true },
      )
      break
    case "queued":
      updateJob(
        msg.jobId,
        { status: "queued", engine: msg.engine },
        { onlyIfActive: true },
      )
      break
    case "progress":
      updateJob(
        msg.jobId,
        {
          status: "downloading",
          engine: msg.engine,
          bytesReceived: msg.bytesReceived,
          totalBytes: msg.totalBytes,
          speed: msg.speed,
        },
        { onlyIfActive: true },
      )
      break
    case "done":
      updateJob(msg.jobId, {
        status: "done",
        engine: msg.engine,
        path: msg.path,
        bytes: msg.bytes,
        mode: msg.mode,
      })
      break
    case "cancelled":
      updateJob(msg.jobId, { status: "cancelled" })
      break
    case "error":
      updateJob(msg.jobId, { status: "error", error: msg.error })
      break
    case "control_error":
      // A cancel/delete didn't work. Say so, but don't change the job's state.
      console.warn("host could not complete request:", msg.error)
      updateJob(msg.jobId, { notice: msg.error })
      break
    case "file_deleted":
      removeJob(msg.jobId)
      break
  }
}

// Tell the host about unfinished jobs it may need to pick back up: aria2
// downloads keep running without a browser, so a fresh host reattaches to
// them; anything else is reported as interrupted.
function sendHello(entries) {
  sendToNativeHost({
    action: "hello",
    jobs: entries.map(([jobId, j]) => ({
      jobId,
      engine: j.engine,
      gid: j.gid,
      path: j.path,
      desiredPath: j.desiredPath,
      totalBytes: j.totalBytes,
    })),
  })
}

async function resumeStaleJobs() {
  const { jobs = {} } = await chrome.storage.local.get("jobs")
  const stale = Object.entries(jobs).filter(
    ([, j]) => ACTIVE.has(j.status) && j.instance !== INSTANCE,
  )
  if (stale.length === 0) return
  sendHello(stale)
  // They're ours now, so a later host crash is handled like any other.
  for (const [jobId] of stale)
    await updateJob(jobId, { instance: INSTANCE })
}
resumeStaleJobs()

async function onHostDisconnect(port) {
  const reason = chrome.runtime.lastError?.message // must be read before any await
  if (reason) console.error("native host disconnected:", reason)
  if (nativePort === port) nativePort = null

  const { jobs = {} } = await chrome.storage.local.get("jobs")
  const active = Object.entries(jobs).filter(
    ([, j]) => ACTIVE.has(j.status) && j.instance === INSTANCE,
  )

  // Built-in downloads ran inside the host and died with it.
  for (const [jobId, j] of active) {
    if (j.engine !== "aria2") {
      await updateJob(jobId, {
        status: "error",
        error: "the download host exited while this was running",
      })
    }
  }
  if (active.length === 0) return

  // Start a new host and tell it about everything that was running: it
  // reattaches to the aria2 downloads (still running in the daemon) and
  // removes the half-written files the built-in ones left behind. Give up if
  // hosts keep dying right away.
  if (++reconnects > 3) {
    for (const [jobId, j] of active) {
      if (j.engine === "aria2") {
        await updateJob(jobId, {
          status: "error",
          error: "lost contact with the download host",
        })
      }
    }
    return
  }
  sendHello(active)
}

// --- Popup -> background messaging -----------------------------------------

chrome.runtime.onMessage.addListener((message) => {
  if (message.action === "cancel") {
    sendToNativeHost({ action: "cancel", jobId: message.jobId })
  } else if (message.action === "delete") {
    ;(async () => {
      const { jobs = {} } = await chrome.storage.local.get("jobs")
      const job = jobs[message.jobId]
      if (job && job.status === "done" && job.path) {
        // Ask the host to remove the actual file; the record is removed once
        // it confirms via "file_deleted", so a failed delete isn't forgotten.
        sendToNativeHost({
          action: "delete_file",
          jobId: message.jobId,
          path: job.path,
        })
        return
      }
      // Not a finished download with a file on disk. Stop it if it's still
      // running; the host cleans up its partial file.
      if (job && ACTIVE.has(job.status)) {
        sendToNativeHost({ action: "cancel", jobId: message.jobId })
      }
      await removeJob(message.jobId)
    })()
  }
  return false
})

// --- Download interception --------------------------------------------------

chrome.downloads.onDeterminingFilename.addListener(
  (item, suggest) => {
    const { id, url, filename, mime, finalUrl } = item
    const target = finalUrl || url

    // blob:, data:, filesystem: etc. only exist inside the page that made them;
    // nothing outside the browser can fetch them.
    if (!/^https?:/i.test(target)) return
    if (isExcludedUrl(target)) return // let Chrome handle this download normally
    if (inFlight.has(id)) return
    inFlight.add(id)

    const jobId = crypto.randomUUID()

    // We're aborting this download entirely, so don't call suggest() --
    // there's no filename decision left for Chrome to make.
    ;(async () => {
      const captured =
        pendingRequests.get(target) ?? pendingRequests.get(url)
      const cookieHeader = await getCookiesFor(target)

      await createJob(jobId, {
        url: target,
        filename: filename || target.split("/").pop(),
        status: "queued",
        startedAt: Date.now(),
        instance: INSTANCE,
      })

      await chrome.downloads.cancel(id)
      await chrome.downloads.erase({ id })

      sendToNativeHost({
        jobId,
        url: target,
        filename,
        mime,
        method: captured?.method ?? "GET",
        body: captured?.body ?? null,
        headers: captured?.headers ?? [],
        cookies: cookieHeader,
      })
    })()
  },
)
