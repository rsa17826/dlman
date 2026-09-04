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

async function renderJobs() {
  const { jobs = {} } = await chrome.storage.local.get("jobs")
  const container = document.getElementById("jobs")
  const entries = Object.entries(jobs).sort(
    (a, b) => (b[1].startedAt ?? 0) - (a[1].startedAt ?? 0),
  )

  if (entries.length === 0) {
    container.innerHTML = '<div class="empty">No downloads yet.</div>'
    return
  }

  container.innerHTML = entries
    .map(([id, job]) => {
      const statusLabel =
        job.status === "downloading" ? "running"
        : job.status === "queued" ? "queued"
        : job.status === "done" ? "completed"
        : job.status === "error" ? "failed"
        : job.status === "cancelled" ? "cancelled"
        : job.status

      let detail
      if (job.status === "downloading") {
        const total = job.totalBytes
        const received = job.bytesReceived ?? 0
        if (total) {
          const pct = Math.min(100, (received / total) * 100)
          detail = `
            <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
            <div class="url">${fmtBytes(received)} / ${fmtBytes(total)} (${pct.toFixed(0)}%)</div>`
        } else {
          // Size unknown (e.g. dynamically generated content) -- show bytes
          // received without a percentage rather than a fake/frozen bar.
          detail = `<div class="url">${fmtBytes(received)} downloaded</div>`
        }
      } else if (job.status === "done") {
        detail = `<div class="url">${job.path ?? ""} (${fmtBytes(job.bytes)})</div>`
      } else if (job.status === "error") {
        detail = `<div class="error-msg">${job.error ?? ""}</div>`
      } else {
        detail = `<div class="url">${job.url ?? ""}</div>`
      }

      const showCancel =
        job.status === "downloading" || job.status === "queued"

      return `
        <div class="job">
          <span class="status ${job.status}">${statusLabel}</span>
          <span class="fname">${job.filename ?? "(unnamed)"}</span>
          ${detail}
          <div class="actions">
            ${showCancel ? `<button class="cancelBtn" data-id="${id}">Cancel</button>` : ""}
            <button class="deleteBtn" data-id="${id}">Delete</button>
          </div>
        </div>`
    })
    .join("")

  container.querySelectorAll(".cancelBtn").forEach((btn) => {
    btn.addEventListener("click", () => {
      chrome.runtime.sendMessage({
        action: "cancel",
        jobId: btn.dataset.id,
      })
    })
  })
  container.querySelectorAll(".deleteBtn").forEach((btn) => {
    btn.addEventListener("click", () => {
      chrome.runtime.sendMessage({
        action: "delete",
        jobId: btn.dataset.id,
      })
    })
  })
}

async function renderExcludeList() {
  const { excludedPatterns = [] } = await chrome.storage.local.get(
    "excludedPatterns",
  )
  const list = document.getElementById("excludeList")
  list.innerHTML = excludedPatterns
    .map(
      (pattern, i) =>
        `<li><span>${pattern}</span><button data-index="${i}" class="removeExclude">x</button></li>`,
    )
    .join("")

  list.querySelectorAll(".removeExclude").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const idx = Number(btn.dataset.index)
      const { excludedPatterns = [] } =
        await chrome.storage.local.get("excludedPatterns")
      excludedPatterns.splice(idx, 1)
      await chrome.storage.local.set({ excludedPatterns })
      renderExcludeList()
    })
  })
}

document
  .getElementById("addExclude")
  .addEventListener("click", async () => {
    const input = document.getElementById("excludeInput")
    const value = input.value.trim()
    if (!value) return
    const { excludedPatterns = [] } = await chrome.storage.local.get(
      "excludedPatterns",
    )
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
