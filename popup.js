const ACTIVE = new Set(["queued", "downloading"])

// Filenames and URLs come from remote servers; never put them in innerHTML raw.
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c])
}

function fmtBytes(n) {
  if (n == null) return ""
  const units = ["B", "KB", "MB", "GB"]
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(1)} ${units[i]}`
}

// aria2 runs in a background daemon that keeps going without the browser. The
// built-in engine runs inside the native host, which stops with the browser.
function engineTag(job) {
  if (job.engine === "aria2") {
    return '<span class="engine aria2" title="Downloaded by the aria2 background daemon">aria2</span>'
  }
  if (job.engine === "python") {
    return '<span class="engine python" title="Downloaded by the built-in downloader (used for POST requests)">built-in</span>'
  }
  return ""
}

function closeHint(job) {
  if (!ACTIVE.has(job.status)) return ""
  if (job.engine === "aria2") {
    return '<div class="closehint ok">✓ Safe to close the browser — aria2 keeps downloading</div>'
  }
  if (job.engine === "python") {
    return '<div class="closehint warn">⚠ Keep the browser open — this download stops if it closes</div>'
  }
  return ""
}

function renderSummary(jobs) {
  const active = jobs.filter((j) => ACTIVE.has(j.status))
  const needBrowser = active.filter((j) => j.engine !== "aria2")
  const el = document.getElementById("closeSummary")
  if (active.length === 0) {
    el.className = "summary hidden"
    el.textContent = ""
  } else if (needBrowser.length > 0) {
    el.className = "summary warn"
    el.textContent = `⚠ ${needBrowser.length} download${needBrowser.length > 1 ? "s" : ""} need${needBrowser.length > 1 ? "" : "s"} the browser open`
  } else {
    el.className = "summary ok"
    el.textContent = "✓ Safe to close the browser — all downloads continue in the background"
  }
}

async function renderJobs() {
  const { jobs = {} } = await chrome.storage.local.get("jobs")
  const container = document.getElementById("jobs")
  const entries = Object.entries(jobs).sort(
    (a, b) => (b[1].startedAt ?? 0) - (a[1].startedAt ?? 0),
  )

  renderSummary(entries.map(([, j]) => j))

  if (entries.length === 0) {
    container.innerHTML = '<div class="empty">No downloads yet.</div>'
    return
  }

  container.innerHTML = entries
    .map(([id, job]) => {
      const statusLabel =
        job.status === "downloading" ? "running" :
        job.status === "queued" ? "queued" :
        job.status === "done" ? "completed" :
        job.status === "error" ? "failed" :
        job.status === "cancelled" ? "cancelled" : job.status

      let detail
      if (job.status === "downloading") {
        const total = job.totalBytes
        const received = job.bytesReceived ?? 0
        const speed = job.speed ? ` · ${fmtBytes(job.speed)}/s` : ""
        if (total) {
          const pct = Math.min(100, (received / total) * 100)
          detail = `
            <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
            <div class="url">${fmtBytes(received)} / ${fmtBytes(total)} (${pct.toFixed(0)}%)${speed}</div>`
        } else {
          // Size unknown (e.g. dynamically generated content) -- show bytes
          // received without a percentage rather than a fake/frozen bar.
          detail = `<div class="url">${fmtBytes(received)} downloaded${speed}</div>`
        }
      } else if (job.status === "done") {
        detail = `<div class="url">${esc(job.path)} (${fmtBytes(job.bytes)})</div>`
      } else if (job.status === "error") {
        detail = `<div class="error-msg">${esc(job.error)}</div>`
      } else {
        detail = `<div class="url">${esc(job.url)}</div>`
      }

      const notice = job.notice ? `<div class="error-msg">${esc(job.notice)}</div>` : ""
      const showCancel = ACTIVE.has(job.status)

      return `
        <div class="job">
          <span class="status ${esc(job.status)}">${esc(statusLabel)}</span>
          ${engineTag(job)}
          <span class="fname">${esc(job.filename ?? "(unnamed)")}</span>
          ${detail}
          ${closeHint(job)}
          ${notice}
          <div class="actions">
            ${showCancel ? `<button class="cancelBtn" data-id="${esc(id)}">Cancel</button>` : ""}
            <button class="deleteBtn" data-id="${esc(id)}">Delete</button>
          </div>
        </div>`
    })
    .join("")

  container.querySelectorAll(".cancelBtn").forEach((btn) => {
    btn.addEventListener("click", () => {
      chrome.runtime.sendMessage({ action: "cancel", jobId: btn.dataset.id })
    })
  })
  container.querySelectorAll(".deleteBtn").forEach((btn) => {
    btn.addEventListener("click", () => {
      chrome.runtime.sendMessage({ action: "delete", jobId: btn.dataset.id })
    })
  })
}

async function renderExcludeList() {
  const { excludedPatterns = [] } = await chrome.storage.local.get("excludedPatterns")
  const list = document.getElementById("excludeList")
  list.innerHTML = excludedPatterns
    .map(
      (pattern, i) =>
        `<li><span>${esc(pattern)}</span><button data-index="${i}" class="removeExclude">x</button></li>`,
    )
    .join("")

  list.querySelectorAll(".removeExclude").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const idx = Number(btn.dataset.index)
      const { excludedPatterns = [] } = await chrome.storage.local.get("excludedPatterns")
      excludedPatterns.splice(idx, 1)
      await chrome.storage.local.set({ excludedPatterns })
      renderExcludeList()
    })
  })
}

document.getElementById("addExclude").addEventListener("click", async () => {
  const input = document.getElementById("excludeInput")
  const value = input.value.trim()
  if (!value) return
  const { excludedPatterns = [] } = await chrome.storage.local.get("excludedPatterns")
  if (!excludedPatterns.includes(value)) {
    excludedPatterns.push(value)
    await chrome.storage.local.set({ excludedPatterns })
  }
  input.value = ""
  renderExcludeList()
})

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return
  if (changes.jobs) renderJobs()
  if (changes.excludedPatterns) renderExcludeList()
})

renderJobs()
renderExcludeList()
