const appState = {
  overview: { drives: [], active_jobs: [], history: [], totals: {} },
  socket: null,
  reconnectTimer: null,
  reconnectAttempts: 0,
  socketGeneration: 0,
  pollTimer: null,

  feedbackTimer: null,

  lastSnapshotAt: 0,

  writeObservations: new Map(),

  activityTimer: null,
  activityInFlight: false,
  activityPending: false,
  lastActivityHydration: 0,
  activityJobs: new Map(),
  detailJobId: null,
  detailRequest: 0,
  destroyed: false,
  preferences: null,
  view: "queue",
  knownStatuses: new Map(),
  pendingTypes: new Map(),
  alertReady: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

const COMPATIBILITY_LABELS = {
  ready: "4K UHD ready",
  needs_firmware: "UHD needs firmware",
  standard: "Blu-ray & DVD",
  check_failed: "Check failed",
  unknown: "Checking…",
};

function humanStatus(status) {
  if (status === "awaiting_start") return "Ready to start";
  return String(status || "unknown").replaceAll("_", " ");
}

function formatDuration(seconds) {
  if (!seconds) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function formatBytes(bytes) {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

function relativeTime(value) {
  if (!value) return "—";
  const date = utcDate(value);
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const ranges = [
    ["year", 31536000],
    ["month", 2592000],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  for (const [unit, amount] of ranges) {
    if (Math.abs(seconds) >= amount)
      return formatter.format(Math.round(seconds / amount), unit);
  }
  return formatter.format(seconds, "second");
}

function elapsedTime(value) {
  if (!value) return "Waiting to start";
  const seconds = Math.max(
    0,
    Math.round((Date.now() - utcDate(value).getTime()) / 1000),
  );
  if (seconds < 60) return `${seconds}s elapsed`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m elapsed` : `${minutes}m elapsed`;
}

function utcDate(value) {
  const text = String(value).replace(" ", "T");
  return new Date(/[zZ]$|[+-]\d{2}:\d{2}$/.test(text) ? text : `${text}Z`);
}

function titleFor(job) {
  const title = job.title || job.disc_name || "Unknown disc";
  const readable = title.includes("_")
    ? title
        .toLowerCase()
        .replaceAll("_", " ")
        .replace(/\b\w/g, (c) => c.toUpperCase())
    : title;
  return `${readable}${job.year ? ` (${job.year})` : ""}`;
}
function driveFor(job) {
  return appState.overview.drives.find((drive) => drive.id === job.drive_id);
}
function finalFolder(job) {
  if (job.destination_folder || job.final_path)
    return job.destination_folder || job.final_path;
  // Older running services don't expose destination_folder. Support their
  // exact default disc layout without interrupting a rip to upgrade them.
  const prefs = appState.preferences;
  if (
    prefs?.output_mode !== "disc" ||
    prefs.disc_folder_template !== "{disc} - {job_id}" ||
    !job.stage_path
  )
    return "";
  const suffix = new RegExp(`[\\\\/]\\.openripper-staging[\\\\/]${job.id}$`);
  if (!suffix.test(job.stage_path)) return "";
  const disc =
    String(job.disc_name || "")
      .replace(/[<>:"/\\\\|?*\u0000-\u001f]/g, " - ")
      .replace(/(?:\s*-\s*){2,}/g, " - ")
      .replace(/\s+/g, " ")
      .replace(/^[ .\-_]+|[ .\-_]+$/g, "")
      .slice(0, 180) || "Disc";
  const separator = job.stage_path.includes("\\") ? "\\" : "/";
  return `${job.stage_path.replace(suffix, "")}${separator}${disc.slice(0, 100)} - ${job.id}`;
}
function ripLocation(job) {
  const title = job.telemetry?.title_name;
  const copying = title
    ? `<div><dt>${job.telemetry?.phase === "encoding" ? "Encoding" : "Copying"}</dt><dd>${escapeHtml(title)}</dd></div>`
    : "";
  const folder = finalFolder(job);
  const destination = folder
    ? `<div><dt>Final folder</dt><dd class="path">${escapeHtml(folder)}</dd></div>`
    : `<div><dt>Final folder</dt><dd>Determined after title selection</dd></div>`;
  return `<dl class="rip-location">${copying}${destination}</dl>`;
}
function driveLabel(job) {
  return driveFor(job)?.device || "Disconnected drive";
}
function driveUpgrade(drive) {
  if (!drive.online || drive.uhd_status !== "needs_firmware") return "";
  return `<p class="drive-upgrade">4K UHD needs firmware.<br><a href="https://trev.bot/" target="_blank" rel="noopener noreferrer">Check flashing support at trev.bot</a></p>`;
}
function shortDrive(drive) {
  return drive.name
    .replace(/^BD-RE\s+/, "")
    .replace(/\s+[\d.]+\s+\S+$/, "")
    .replace(/\s+/g, " ");
}
function problemCopy(text) {
  if (/LibreDrive compatible|AACS|KEY EXCHANGE/.test(text))
    return [
      "MakeMKV could not open this disc",
      "The drive reported a disc protection or compatibility error. Check this disc in MakeMKV before retrying.",
    ];
  if (/activation|evaluation|shareware/i.test(text))
    return [
      "MakeMKV needs activation",
      "Activate MakeMKV on this server, then retry with the original disc inserted.",
    ];
  if (/Permission|Access is denied/i.test(text))
    return [
      "Destination access denied",
      "Check that the server account can write to this folder or share.",
    ];
  if (/overwrite/i.test(text))
    return [
      "Output already exists",
      "Existing files were preserved. Check the destination and staged files before retrying.",
    ];
  if (/No space|disk full/i.test(text))
    return [
      "Destination is full",
      "Free space on the destination before retrying.",
    ];
  if (/Interrupted by service restart/i.test(text))
    return [
      "Interrupted by a restart",
      "Reinsert the same disc and retry. Partial files remain in staging.",
    ];
  return [
    String(text || "The rip did not finish").split("\n")[0],
    "Open details for the full diagnostics before retrying.",
  ];
}
function conciseEvent(event) {
  return event.level === "error"
    ? problemCopy(event.message)[0]
    : event.message.split("\n")[0];
}
function jobFeedback(job) {
  const t = job.telemetry || {};
  let eta = t.eta_seconds;
  if (
    eta == null &&
    t.bytes_per_second == null &&
    !(t.seconds_since_write >= 60) &&
    job.status === "ripping" &&
    job.progress > 0.01 &&
    job.started_at
  ) {
    eta =
      (((Date.now() - utcDate(job.started_at).getTime()) / 1000) *
        (1 - job.progress)) /
      job.progress;
  }
  return {
    speed:
      t.bytes_per_second == null
        ? "Not available yet"
        : t.bytes_per_second === 0 ? "0 B/s" : `${formatBytes(t.bytes_per_second)}/s`,
    eta:
      t.seconds_since_write >= 60 ? "Waiting for data" : eta != null && job.status === "ripping"
        ? `~${formatDuration(Math.max(60, eta))}`
        : "Calculating…",
    bytes:
      t.bytes_written != null
        ? `${formatWritten(t.bytes_written)} / ${formatBytes(t.bytes_expected)}`
        : elapsedTime(job.started_at || job.created_at),
    slow: t.seconds_since_write >= 60,
  };
}

function formatWritten(bytes) {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : formatBytes(bytes);
}

function observeWrite(previous, telemetry, now) {
  if (telemetry.bytes_written == null) return previous;

  if (!previous || telemetry.bytes_written > previous.bytes) {
    return {
      bytes: telemetry.bytes_written,

      at:
        telemetry.seconds_since_write != null
          ? now - telemetry.seconds_since_write * 1000
          : previous
            ? now
            : null,
    };
  }

  return previous;
}

function transferActivity(job, snapshotAge) {
  const age = Math.max(0, Math.floor(snapshotAge));

  if (age >= 15)
    return {
      kind: "stale",

      label: "Updates delayed",

      detail: `Last update ${age}s ago · rip status unconfirmed`,
    };

  const t = job.telemetry || {};

  if (job.status === "scanning")
    return {
      kind: "working",

      label: "Reading disc",

      detail: `Server updated ${age}s ago · waiting for title information`,
    };

  if (t.phase === "encoding")
    return {
      kind: "working",

      label: "Encoding",

      detail: `Server updated ${age}s ago`,
    };

  if (job.status !== "ripping")
    return {
      kind: "working",

      label: "Finishing files",

      detail: `Server updated ${age}s ago`,
    };

  if (t.seconds_since_write == null || !t.bytes_written)
    return {
      kind: "waiting",

      label: "Waiting for data",

      detail: `Server updated ${age}s ago · no measured write yet`,
    };

  const since = Math.floor(t.seconds_since_write + age);

  if (since >= 60)
    return {
      kind: "stalled",

      label: `No new data for ${since}s`,

      detail: "The drive may be retrying a read. Check details.",
    };

  if (since > 10)
    return {
      kind: "waiting",

      label: "Waiting for data",

      detail: `Last write ${since}s ago · server updated ${age}s ago`,
    };

  return {
    kind: "writing",

    label: "Receiving data",

    detail: `Last write ${since < 2 ? "just now" : `${since}s ago`} · server updated ${age}s ago`,
  };
}

function transferMarkup(job) {
  if (!["ripping", "scanning", "publishing"].includes(job.status)) return "";

  return `<div class="transfer-feedback"><span class="transfer-signal" aria-hidden="true"><i></i><i></i><i></i><i></i></span><strong data-transfer-label></strong><span data-transfer-detail></span></div>`;
}

function updateTransferFeedback() {
  const age = (Date.now() - appState.lastSnapshotAt) / 1000;

  for (const row of $$(".job-row[data-job]")) {
    const job = appState.overview.active_jobs.find(
      (item) => item.id === row.dataset.job,
    );

    const label = $("[data-transfer-label]", row);

    if (!job || !label) continue;

    const activity = transferActivity(job, age);

    row.dataset.activity = activity.kind;

    label.textContent = activity.label;

    $("[data-transfer-detail]", row).textContent = activity.detail;

    const bounds = row.getBoundingClientRect();

    row.dataset.animating = String(
      activity.kind === "writing" &&
        document.visibilityState === "visible" &&
        bounds.bottom > 0 &&
        bounds.top < innerHeight,
    );

    if (activity.kind === "stale") {
      for (const metric of $$("[data-live-estimate]", row))
        metric.textContent = "Awaiting update";
    }
  }

  if (age >= 15 && appState.lastSnapshotAt) {
    setConnection(
      "offline",

      "Updates delayed",

      `No fresh status for ${Math.floor(age)} seconds`,
    );
  }
}

function startNowButton(job) {
  const supported = appState.overview.runtime?.slot_override_supported;

  return `<button class="mini-button" data-action="start-now" data-job="${escapeHtml(job.id)}" ${supported ? 'title="Start this job now, even when all slots are in use"' : 'disabled title="Available after the service is updated; running rips are unaffected"'}>Rip now</button>`;
}

function statusCopy(job, progress) {
  const messages = {
    scanning:
      "Reading titles from the disc. MakeMKV does not report a scan percentage.",
    queued: "Waiting for a free drive slot",
    ripping: progress ? `Ripping · ${progress}%` : "Starting rip",
    publishing: "Moving files into your library",
    needs_review: "Review the selected titles or naming to continue.",
  };
  return messages[job.status] || humanStatus(job.status);
}

function badge(status) {
  return `<span class="status-pill ${escapeHtml(status)}">${escapeHtml(humanStatus(status))}</span>`;
}

function connectedOverview(data) {
  return {
    ...data,
    drives: data.drives.filter((drive) => drive.online !== false),
  };
}

function renderMetrics(data) {
  const running = data.active_jobs.filter((j) => j.status === "ripping").length;
  const waiting = data.active_jobs.filter((j) => ["queued", "awaiting_start"].includes(j.status)).length;
  const scanning = data.active_jobs.filter(
    (j) => j.status === "scanning",
  ).length;
  const encoding = data.active_jobs.filter(
    (j) => j.telemetry?.phase === "encoding",
  ).length;
  $("#queue-count").textContent = data.active_jobs.length;
  $("#queue-summary").textContent =
    `${running} ripping · ${waiting} waiting${encoding ? ` · ${encoding} encoding` : ""}${scanning ? ` · ${scanning} ${data.runtime ? "scanning" : "waiting / scanning"}` : ""}`;
  const limit =
    data.runtime?.max_concurrent_rips ??
    appState.preferences?.max_concurrent_rips;
  $("#capacity-button").textContent = limit
    ? `${data.runtime?.slots_in_use ?? running} / ${limit} slots in use`
    : "Concurrent rips";

  if (data.runtime?.slot_overrides_active) {
    $("#capacity-button").textContent =
      `${data.runtime.slots_in_use} active · ${data.runtime.slot_overrides_active} manual`;
  }

  $("#queue-note").textContent = limit
    ? data.runtime?.slot_overrides_active
      ? `Auto-start limit: ${limit} · manual starts can exceed it`
      : `Up to ${limit} ${limit === 1 ? "job" : "jobs"} at once`
    : "Detailed queue states need a service restart";
}
function renderDrives(data) {
  $("#drive-count").textContent = data.drives.length;
  $("#drive-summary").textContent = data.drives.length
    ? `${data.drives.filter((d) => !d.disc_name).length} empty · ${data.drives.filter((d) => d.disc_name).length} with a disc`
    : "No drives detected";
  $("#drive-grid").innerHTML = data.drives.length
    ? data.drives
        .map((drive) => {
          const job = data.active_jobs.find((j) => j.drive_id === drive.id);
          const active =
            job &&
            [
              "awaiting_start",
              "queued",
              "scanning",
              "ripping",
              "publishing",
              "needs_review",
            ].includes(job.status);

          return `<article class="drive-row" data-drive="${escapeHtml(drive.id)}"><div class="drive-heading"><strong>Drive ${escapeHtml(drive.device || drive.disc_index)}</strong><span class="state-pill ${escapeHtml(job?.status || "")}">${escapeHtml(job ? humanStatus(job.status) : drive.disc_name ? "Disc loaded" : "Empty")}</span></div><span class="drive-name">${escapeHtml(shortDrive(drive))}</span>${driveUpgrade(drive)}<details><summary>Drive details</summary><p>${escapeHtml(drive.name)}</p><p>${escapeHtml(drive.firmware_message || COMPATIBILITY_LABELS[drive.uhd_status] || "Compatibility not checked")}</p></details><div class="drive-actions">${job?.status === "queued" ? startNowButton(job) : ""}${!active ? `${drive.disc_name ? `<button class="text-button" data-action="rip" data-drive="${escapeHtml(drive.id)}">Rip now</button>` : ""}<button class="text-button" data-action="eject" data-drive="${escapeHtml(drive.id)}">Open tray</button>` : ""}</div></article>`;
        })
        .join("")
    : `<p class="muted">Connect an optical drive, then refresh. MakeMKV must be able to see it.</p>`;
}
function pendingDiscMarkup(job) {
  const choice = appState.pendingTypes.get(job.id) || "auto";
  const type = choice === "auto" ? job.media_type : choice;
  const p = appState.preferences || {};
  const override = p.drive_paths?.[job.drive_id];
  const root = override ? override.replace(/[\\/]+$/, "") + (override.includes("\\") ? "\\" : "/") + (type === "tv" ? "TV" : "Movies") : type === "tv" ? p.tv_root : p.movie_root;
  return `<article class="pending-row"><div><h3>${escapeHtml(titleFor(job))}</h3><p>Drive ${escapeHtml(driveLabel(job))} · Waiting for you to start</p><p class="path" data-pending-path="${escapeHtml(job.id)}">${escapeHtml(root || "Set a destination in Configuration")}</p></div><div class="job-actions"><label class="field"><span>Save as</span><select data-pending-type="${escapeHtml(job.id)}" aria-label="Media type for ${escapeHtml(titleFor(job))}"><option value="auto" ${choice === "auto" ? "selected" : ""}>${job.media_type === "tv" ? "Auto · TV show" : "Auto · Detect from disc"}</option><option value="movie" ${choice === "movie" ? "selected" : ""}>Movie</option><option value="tv" ${choice === "tv" ? "selected" : ""}>TV show</option></select></label><button class="button button-primary" data-action="start-pending" data-job="${escapeHtml(job.id)}">Start</button><button class="text-button" data-action="cancel" data-job="${escapeHtml(job.id)}">Remove</button></div></article>`;
}

document.addEventListener("change", (event) => {
  const id = event.target.dataset.pendingType;
  if (!id) return;
  appState.pendingTypes.set(id, event.target.value);
  const job = appState.overview.active_jobs.find(j => j.id === id);
  if (job) {
    const template = document.createElement("template");
    template.innerHTML = pendingDiscMarkup(job);
    event.target.closest(".pending-row").querySelector("[data-pending-path]").textContent = template.content.querySelector("[data-pending-path]").textContent;
  }
});

function renderQueue(data) {
  $$("[data-action='clear-failed']").forEach(button => button.disabled = !data.runtime?.queue_controls_supported || !data.totals.failed);
  $("#service-notice").hidden = Boolean(data.runtime?.queue_controls_supported);
  const sorted = [...data.active_jobs].sort(
    (a, b) => utcDate(a.created_at) - utcDate(b.created_at),
  );
  const waiting = sorted.filter((j) => j.status === "queued");
  const pending = sorted.filter((j) => j.status === "awaiting_start");
  const working = sorted.filter((j) => !["queued", "awaiting_start"].includes(j.status));
  const detail = (job) =>
    `<button class="mini-button" data-action="job-detail" data-job="${escapeHtml(job.id)}" aria-label="Details for ${escapeHtml(titleFor(job))}">Details</button>`;
  $("#queue").innerHTML = !sorted.length
    ? `<div class="empty-state"><strong>Ready for the next disc</strong><p>${appState.preferences?.auto_rip === false ? "Automatic ripping is off. Loaded discs appear here with a Start button." : "Insert a DVD or Blu-ray. Its scan and rip will appear here automatically."}</p></div>`
    : `${pending.length ? `<div class="group-heading"><strong>Ready to start</strong><span>Media type is detected automatically</span></div><div class="waiting-list">${pending.map(pendingDiscMarkup).join("")}</div>` : ""}${
        working.length
          ? `<div class="group-heading"><strong>In progress</strong><span>${working.length} jobs</span></div><div class="running-list">${working
              .map((job) => {
                const progress = Math.max(
                  0,
                  Math.min(100, (job.progress || 0) * 100),
                );
                const f = jobFeedback(job);
                const isRip = job.status === "ripping";
                const encoding = job.telemetry?.phase === "encoding";

                return `<article class="job-row" data-job="${escapeHtml(job.id)}"><div class="job-top"><div class="job-title">${badge(encoding ? "encoding" : job.status)}<h3>${escapeHtml(titleFor(job))}</h3><p>Drive ${escapeHtml(driveLabel(job))}${job.telemetry?.title_count ? ` · Title ${job.telemetry.title_number} of ${job.telemetry.title_count}` : ""}</p></div>${isRip ? `<div class="job-percent">${progress.toFixed(1)}<small>%</small></div>` : ""}</div>${isRip || job.status === "scanning" ? `<div class="progress-track ${job.status === "scanning" ? "indeterminate" : ""}" role="progressbar" aria-label="${escapeHtml(titleFor(job))}" ${isRip ? `aria-valuenow="${progress.toFixed(1)}" aria-valuemin="0" aria-valuemax="100"` : 'aria-valuetext="Reading disc"'} style="--progress:${progress / 100}"><span></span></div>` : ""}${transferMarkup(job)}${ripLocation(job)}<div class="job-bottom">${isRip ? `<div class="job-measures"><div><strong>${escapeHtml(f.bytes)}</strong>${job.telemetry?.bytes_written != null ? "Copied" : "Time ripping"}</div><div><strong data-live-estimate>${escapeHtml(f.speed)}</strong>Write speed</div><div><strong data-live-estimate>${escapeHtml(f.eta)}</strong>Estimated remaining</div></div>` : `<p class="job-note">${escapeHtml(!data.runtime && job.status === "scanning" ? "Waiting for capacity or reading the disc. This service version cannot distinguish the two." : encoding ? `Encoding title ${job.telemetry.title_number} of ${job.telemetry.title_count} · ${Math.round((job.telemetry.encode_progress || 0) * 100)}%` : statusCopy(job, progress))}</p>`}<div class="job-actions">${detail(job)}${job.status === "needs_review" ? `<button class="mini-button" data-action="review" data-job="${escapeHtml(job.id)}">Review titles</button>` : `<button class="text-button" data-action="cancel" data-job="${escapeHtml(job.id)}">Cancel</button>`}</div></div>${f.slow ? '<p class="job-note warning">No new data written for over a minute. The drive may be retrying a read; check details.</p>' : ""}</article>`;
              })
              .join("")}</div>`
          : ""
      }${waiting.length ? `<div class="group-heading"><strong>Waiting</strong><span>Starts as slots open</span></div><div class="waiting-list">${waiting.map((job, index) => `<article class="waiting-row"><span class="queue-position">${index + 1}</span><div><h3>${escapeHtml(titleFor(job))}</h3><p>Drive ${escapeHtml(driveLabel(job))} · Waiting for a free slot</p></div><div class="job-actions">${startNowButton(job)}${detail(job)}<button class="text-button" data-action="cancel" data-job="${escapeHtml(job.id)}">Remove</button></div></article>`).join("")}</div>` : ""}`;

  const failures = data.history
    .filter((j) => j.status === "failed")
    .slice(0, 2);
  $("#attention-section").hidden = !failures.length;
  $("#attention-list").innerHTML = failures
    .map(
      (job) =>
        `<article class="result-row"><div>${badge("failed")}<h3>${escapeHtml(titleFor(job))}</h3><p>${escapeHtml(problemCopy(job.error)[0])} · ${escapeHtml(relativeTime(job.completed_at))}</p></div><div class="job-actions">${detail(job)}<button class="text-button" data-action="dismiss-failed" ${data.runtime?.queue_controls_supported ? "" : "disabled"} data-job="${escapeHtml(job.id)}" aria-label="Clear failure for ${escapeHtml(titleFor(job))}">Clear</button></div></article>`,
    )
    .join("");
  const completed = data.history
    .filter((j) => j.status === "complete")
    .slice(0, 3);
  $("#completed-list").innerHTML = completed.length
    ? completed
        .map(
          (job) =>
            `<article class="result-row">${appState.preferences?.artwork_enabled ? `<img class="poster-thumb" src="/api/jobs/${escapeHtml(job.id)}/artwork" alt="" loading="lazy" onerror="this.hidden=true">` : ""}<div>${badge("complete")}<h3>${escapeHtml(titleFor(job))}</h3><span class="path">${escapeHtml(job.final_path)}</span>${job.integration ? `<p class="field-help">${escapeHtml(job.integration.message)}</p>` : ""}</div>${detail(job)}</article>`,
        )
        .join("")
    : `<p class="muted">No completed rips yet. Saved files and their locations will appear here.</p>`;
}

function renderActivity() {
  const list = $("#activity-log");
  const details = [...appState.activityJobs.values()];
  const events = details
    .flatMap((job) =>
      (job.events || []).map((event) => ({
        ...event,
        jobId: job.id,
        jobTitle: titleFor(job),
      })),
    )
    .sort((left, right) => utcDate(right.created_at) - utcDate(left.created_at))
    .slice(0, 30);
  $("#activity-note").textContent = events.length
    ? `${events.length} recent event${events.length === 1 ? "" : "s"} · select one for details`
    : "No activity yet";
  if (!events.length) {
    list.innerHTML = `<li class="activity-empty">No activity yet. Jobs show up here as they run.</li>`;
    return;
  }
  list.innerHTML = events
    .map(
      (event) => `
        <li>
          <button class="activity-item ${escapeHtml(event.level || "info")}" data-action="job-detail" data-job="${escapeHtml(event.jobId)}">
            <span class="activity-dot" aria-hidden="true"></span>
            <span class="activity-copy">
              <strong>${escapeHtml(conciseEvent(event))}</strong>
              <small>${escapeHtml(event.jobTitle)} · ${escapeHtml(relativeTime(event.created_at))}</small>
            </span>
          </button>
        </li>`,
    )
    .join("");
}

async function hydrateActivity() {
  appState.activityTimer = null;
  if (appState.activityInFlight) {
    appState.activityPending = true;
    return;
  }
  appState.activityInFlight = true;
  appState.lastActivityHydration = Date.now();
  const jobs = [
    ...appState.overview.active_jobs,
    ...appState.overview.history.slice(0, 8),
    ...appState.overview.history.filter(
      (job) => job.id === appState.detailJobId,
    ),
  ].filter(
    (job, index, all) => all.findIndex((item) => item.id === job.id) === index,
  );
  try {
    const details = await Promise.all(
      jobs.map((job) =>
        api(`/api/jobs/${job.id}`).catch(
          () => appState.activityJobs.get(job.id) || null,
        ),
      ),
    );
    appState.activityJobs = new Map(
      details.filter(Boolean).map((detail) => [detail.id, detail]),
    );
    renderActivity();
    if (appState.detailJobId) {
      const detail = appState.activityJobs.get(appState.detailJobId);
      if (detail) renderJobDetail(detail);
    }
  } finally {
    appState.activityInFlight = false;
    if (appState.activityPending) {
      appState.activityPending = false;
      scheduleActivityHydration();
    }
  }
}

function scheduleActivityHydration() {
  if (appState.activityTimer || appState.destroyed) return;
  const delay = Math.max(
    250,
    2000 - (Date.now() - appState.lastActivityHydration),
  );
  appState.activityTimer = setTimeout(hydrateActivity, delay);
}

function renderHistory(data) {
  const filter = $("#history-filter").value,
    query = $("#history-search").value.toLowerCase();
  const jobs = data.history.filter(
    (j) =>
      (filter === "all" || j.status === filter) &&
      `${titleFor(j)} ${j.disc_name}`.toLowerCase().includes(query),
  );
  $("#history-body").innerHTML = jobs.length
    ? jobs
        .map(
          (job) =>
            `<tr><td><strong>${escapeHtml(titleFor(job))}</strong><small>${escapeHtml(job.disc_name)}</small></td><td>${badge(job.status)}${job.integration ? `<small>${escapeHtml(job.integration.message)}</small>` : ""}</td><td>${job.status === "complete" ? `<span class="path">${escapeHtml(job.final_path)}</span>` : escapeHtml(job.status === "failed" ? problemCopy(job.error)[0] : job.status === "cancelled" ? "Stopped · any partial files remain in staging" : "Awaiting review")}</td><td>${escapeHtml(relativeTime(job.completed_at || job.created_at))}</td><td><button class="mini-button" data-action="job-detail" data-job="${escapeHtml(job.id)}">Details</button></td></tr>`,
        )
        .join("")
    : '<tr><td colspan="5">No matching jobs.</td></tr>';
}

function render(data) {
  appState.lastSnapshotAt = Date.now();

  const ids = new Set(data.active_jobs.map((job) => job.id));

  for (const id of appState.writeObservations.keys())
    if (!ids.has(id)) appState.writeObservations.delete(id);

  for (const job of data.active_jobs) {
    const telemetry = job.telemetry || {};

    const observed = observeWrite(
      appState.writeObservations.get(job.id),
      telemetry,
      appState.lastSnapshotAt,
    );

    if (observed) {
      appState.writeObservations.set(job.id, observed);

      if (observed.at != null)
        job.telemetry = {
          ...telemetry,
          seconds_since_write: Math.max(
            telemetry.seconds_since_write || 0,
            (appState.lastSnapshotAt - observed.at) / 1000,
          ),
        };
    }
  }

  const focused = document.activeElement;
  const restore =
    focused?.dataset.action && (focused.dataset.job || focused.dataset.drive)
      ? {
          action: focused.dataset.action,
          job: focused.dataset.job,
          drive: focused.dataset.drive,
        }
      : null;
  const expandedDrives = [
    ...document.querySelectorAll("#drive-grid details[open]"),
  ].map((el) => el.closest(".drive-row")?.dataset.drive);
  const connected = connectedOverview(data);
  notifyChanges(connected);
  appState.overview = connected;
  renderMetrics(connected);
  renderDrives(connected);
  renderQueue(connected);

  updateTransferFeedback();

  renderHistory(connected);
  expandedDrives.forEach((id) =>
    document
      .querySelector(`#drive-grid [data-drive="${CSS.escape(id)}"] details`)
      ?.setAttribute("open", ""),
  );
  if (restore && !focused.isConnected) {
    const selector = `[data-action="${CSS.escape(restore.action)}"]${restore.job ? `[data-job="${CSS.escape(restore.job)}"]` : `[data-drive="${CSS.escape(restore.drive)}"]`}`;
    document.querySelector(selector)?.focus({ preventScroll: true });
  }
  scheduleActivityHydration();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      message = Array.isArray(payload.detail)
        ? payload.detail.map((item) => item.msg).join("; ")
        : payload.detail || message;
    } catch {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function setConnection(state, text, detail = "") {
  const element = $("#socket-state");
  element.className = `connection ${state}`;
  $("span", element).textContent = text;
  element.title = detail || text;
  $("#connection-alert").hidden = state !== "offline";
}

function connect() {
  if (appState.destroyed || !navigator.onLine) {
    setConnection("offline", "Offline", "This browser is offline");
    return;
  }
  clearTimeout(appState.reconnectTimer);
  const generation = ++appState.socketGeneration;
  if (appState.socket && appState.socket.readyState < WebSocket.CLOSING) {
    appState.socket.close();
  }
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/api/ws`);
  appState.socket = socket;
  setConnection("syncing", "Syncing", "Connecting…");
  socket.addEventListener("open", () => {
    if (generation !== appState.socketGeneration) return;
    appState.reconnectAttempts = 0;
    setConnection("online", "Live", "Live");
  });
  socket.addEventListener("message", (event) => {
    if (generation !== appState.socketGeneration) return;
    try {
      render(JSON.parse(event.data));
      setConnection(
        "online",
        "Live",
        `Last update ${new Date().toLocaleTimeString()}`,
      );
    } catch {
      setConnection(
        "syncing",
        "Syncing",
        "Live updates failed. Refreshing every few seconds instead.",
      );
    }
  });
  socket.addEventListener("close", () => {
    if (generation !== appState.socketGeneration || appState.destroyed) return;
    appState.socket = null;
    appState.reconnectAttempts += 1;
    setConnection(
      "syncing",
      "Polling",
      "Live updates paused. Refreshing every few seconds instead.",
    );
    refresh().catch(() =>
      setConnection("offline", "Offline", "Can’t reach OpenRipper"),
    );
    const delay = Math.min(
      15000,
      1200 * 2 ** Math.min(appState.reconnectAttempts, 4),
    );
    appState.reconnectTimer = setTimeout(connect, delay);
  });
  socket.addEventListener("error", () => socket.close());
}

let toastTimer;
function toast(message, type = "") {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    element.className = "toast";
  }, 3500);
}

async function refresh() {
  const overview = await api("/api/overview");
  render(overview);

  if (appState.socket?.readyState === WebSocket.OPEN)
    setConnection("online", "Live");

  if (!appState.socket || appState.socket.readyState !== WebSocket.OPEN) {
    setConnection(
      "syncing",
      "Polling",
      `Last refresh ${new Date().toLocaleTimeString()}`,
    );
  }
}

function renderJobDetail(job) {
  if (document.querySelector("#job-dialog-body details[open]")) return;
  const f = jobFeedback(job),
    problem = problemCopy(job.error);
  const selected = (job.titles || []).filter((t) => t.selected);
  const liveJob = appState.overview.active_jobs.find(j => j.id === job.id);
  const destination = job.final_path || finalFolder({...liveJob, ...job, destination_folder: job.destination_folder || liveJob?.destination_folder});
  $("#job-dialog-title").textContent = titleFor(job);
  $("#job-dialog-body").innerHTML =
    `${badge(job.status)}<div class="detail-stats"><div><span>Drive</span><strong>${escapeHtml(driveLabel(job))}</strong></div><div><span>Progress</span><strong>${job.status === "queued" ? "Waiting for a slot" : job.status === "scanning" ? "Reading disc" : `${((job.progress || 0) * 100).toFixed(1)}%`}</strong></div><div><span>${job.completed_at ? "Finished" : "Time ripping"}</span><strong>${escapeHtml(job.completed_at ? relativeTime(job.completed_at) : elapsedTime(job.started_at))}</strong></div></div>${job.error ? `<div class="error-summary"><strong>${escapeHtml(problem[0])}</strong><p>${escapeHtml(problem[1])}</p></div>` : ""}${job.status === "ripping" ? `<div class="detail-stats"><div><span>Write speed</span><strong>${escapeHtml(f.speed)}</strong></div><div><span>Estimated remaining</span><strong>${escapeHtml(f.eta)}</strong></div><div><span>Written</span><strong>${escapeHtml(f.bytes)}</strong></div></div>` : ""}<section class="detail-block"><h3>${job.final_path ? "Saved location" : "Final destination"}</h3><p class="path">${escapeHtml(destination || "Determined after the disc scan")}</p>${job.stage_path && !job.final_path ? `<details><summary>Temporary staging location</summary><p class="path">${escapeHtml(job.stage_path)}</p></details>` : ""}</section>${f.slow ? `<p class="job-note warning">${escapeHtml(job.telemetry?.read_warning || "No new data written for over a minute. The drive may be retrying a read.")}</p>` : ""}<section class="detail-block"><h3>Selected titles · ${selected.length}</h3><div class="detail-list">${selected.length ? selected.map((t) => `<div><strong>${escapeHtml(t.name || `Title ${t.title_index}`)}</strong><small>Title ${t.title_index} · ${escapeHtml(t.playlist || "Source unavailable")} · ${escapeHtml(formatDuration(t.duration_seconds))} · ${escapeHtml(formatBytes(t.size_bytes))}${t.ripped_path ? " · saved" : ""}</small></div>`).join("") : `<p class="detail-empty">${job.status === "queued" ? "Title selection begins when a slot opens." : job.status === "failed" ? "The disc scan did not return usable titles." : "Titles appear after the disc scan finishes."}</p>`}</div></section>${typeof integrationSummary === "function" ? integrationSummary(job) : ""}<section class="detail-block"><h3>Recent events</h3><div class="detail-list">${(
      job.events || []
    )
      .slice(0, 8)
      .map(
        (e) =>
          `<div><strong>${escapeHtml(conciseEvent(e))}</strong><small>${escapeHtml(relativeTime(e.created_at))}</small></div>`,
      )
      .join(
        "",
      )}</div></section>${job.error ? `<details class="detail-block"><summary>Full diagnostic output</summary><pre class="diagnostics">${escapeHtml(job.error)}</pre></details>` : ""}<div class="dialog-actions">${["failed", "cancelled"].includes(job.status) ? `<button class="button" data-action="retry" data-job="${escapeHtml(job.id)}">Retry with original disc</button>` : job.status === "needs_review" ? `<button class="button button-primary" data-action="review" data-job="${escapeHtml(job.id)}">Review titles</button>` : ["ripping", "scanning", "queued"].includes(job.status) ? `<button class="button button-danger" data-action="cancel" data-job="${escapeHtml(job.id)}">Cancel rip</button>` : ""}<button class="button" data-action="close-job">Close</button></div>`;
}

async function openJobDetail(jobId) {
  appState.detailJobId = jobId;
  const request = ++appState.detailRequest;
  $("#job-dialog-title").textContent = "Loading…";
  $("#job-dialog-body").innerHTML = `<p class="dialog-copy">Loading job…</p>`;
  const dialog = $("#job-dialog");
  if (!dialog.open) dialog.showModal();
  const job = await api(`/api/jobs/${jobId}`);
  if (request !== appState.detailRequest || appState.detailJobId !== jobId)
    return;
  appState.activityJobs.set(job.id, job);
  renderJobDetail(job);
}

async function openReview(jobId) {
  const job = await api(`/api/jobs/${jobId}`);
  const beforeRip = !(job.titles || []).some((t) => t.ripped_path);
  appState.reviewBeforeRip = beforeRip;
  $("#publish-button").disabled = false;
  $("#review-heading").textContent = beforeRip
    ? "Preview this rip"
    : "Name this rip";
  $("#review-description").textContent = beforeRip
    ? "Choose which titles to copy and check the name. No video has been ripped yet."
    : "The rip is safe in staging. Confirm its name and selected titles to publish it.";
  $("#publish-button").textContent = beforeRip
    ? "Start selected titles"
    : "Publish to library";
  $("#review-job-id").value = job.id;
  $("#review-title").value = job.title || "";
  $("#review-year").value = job.year || "";
  $("#review-edition").value = job.edition || "";
  $("#review-season").value = job.season ?? "";
  $("#review-episode").value = job.episode_start ?? "";
  const type = job.media_type === "tv" ? "tv" : "movie";
  $(`input[name="media_type"][value="${type}"]`).checked = true;
  $("#review-form").classList.toggle("tv-mode", type === "tv");
  $("#review-error").textContent = "";
  $("#review-titles").innerHTML = job.titles
    .map(
      (title) => `
        <label class="title-option">
          <input type="checkbox" name="selected_title" value="${title.id}" ${title.selected ? "checked" : ""} />
          <strong>${escapeHtml(title.name || `Title ${title.title_index}`)}</strong>
          <span>Title ${title.title_index} · ${escapeHtml(title.playlist || "Source unavailable")} · ${formatDuration(title.duration_seconds)} · ${formatBytes(title.size_bytes)}${title.duplicate_of != null ? ` · Same content as title ${title.duplicate_of}` : ""}</span>
        </label>`,
    )
    .join("");
  $("#review-dialog").showModal();
}

async function performAction(action, target) {
  if (action === "refresh-presets") { await loadHandbrakePresets(); return; }
  if (action === "start-pending") {
    const job = appState.overview.active_jobs.find(j => j.id === target.dataset.job);
    if (!job) return;
    await api(`/api/jobs/${job.id}/start`, {method: "POST", body: JSON.stringify({media_type: appState.pendingTypes.get(job.id) || "auto"})});
    appState.pendingTypes.delete(job.id);
    toast("Started. This disc will rip when a slot is available.");
    await refresh(); return;
  }
  if (action === "clear-failed" || action === "dismiss-failed") {
    const url = action === "clear-failed" ? "/api/jobs/failed/clear" : `/api/jobs/${target.dataset.job}/dismiss`;
    const result = await api(url, {method: "POST"});
    toast(`${result.cleared} failed ${result.cleared === 1 ? "entry" : "entries"} cleared. Partial files kept.`);
    await refresh(); return;
  }
  if (action === "check-names") {
    await api(`/api/jobs/${target.dataset.job}/integration/check`, {
      method: "POST",
    });
    await openJobDetail(target.dataset.job);
    return;
  }
  if (action === "process-names") {
    await openNaming(target.dataset.job);
    return;
  }
  if (action === "job-detail") {
    await openJobDetail(target.dataset.job);
    return;
  }
  if (action === "close-job") {
    appState.detailJobId = null;
    appState.detailRequest += 1;
    $("#job-dialog").close();
    return;
  }
  if (action === "review") {
    appState.detailJobId = null;
    if ($("#job-dialog").open) $("#job-dialog").close();
    await openReview(target.dataset.job);
    return;
  }

  if (action === "start-now") {
    await api(`/api/jobs/${target.dataset.job}/start-now`, { method: "POST" });

    toast("Starting this job now. Your saved slot limit is unchanged.");
  } else if (action === "rip") {
    await api(`/api/drives/${target.dataset.drive}/rip`, {
      method: "POST",
      body: "{}",
    });
    toast("Rip queued.");
  } else if (action === "eject") {
    await api(`/api/drives/${target.dataset.drive}/eject`, {
      method: "POST",
      body: "{}",
    });
    toast("Tray opened.");
  } else if (action === "retry") {
    await api(`/api/jobs/${target.dataset.job}/retry`, {
      method: "POST",
      body: "{}",
    });
    toast("Retry queued.");
  } else if (action === "cancel") {
    const pending = appState.overview.active_jobs.find(j => j.id === target.dataset.job)?.status === "awaiting_start";
    if (!pending && !(await confirmCancel())) return;
    await api(`/api/jobs/${target.dataset.job}/cancel`, {
      method: "POST",
      body: "{}",
    });
    toast("Cancelling…");
  }
  await refresh();
}

document.addEventListener("click", async (event) => {
  const target =
    event.target.closest("[data-action]") ||
    (event.target.closest(".job-card[data-job]")
      ? {
          dataset: {
            action: "job-detail",
            job: event.target.closest(".job-card[data-job]").dataset.job,
          },
        }
      : null);
  if (!target) return;
  target.disabled = true;
  try {
    await performAction(target.dataset.action, target);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    target.disabled = false;
  }
});

$("#scan-button").addEventListener("click", async () => {
  const button = $("#scan-button");
  button.disabled = true;
  button.textContent = "Scanning…";
  try {
    await api("/api/poll", { method: "POST", body: "{}" });
    await refresh();
    toast("Drive scan complete.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Refresh";
  }
});

$$('input[name="media_type"]').forEach((input) => {
  input.addEventListener("change", () => {
    $("#review-form").classList.toggle(
      "tv-mode",
      input.value === "tv" && input.checked,
    );
  });
});

$("#review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.submitter?.value === "cancel") {
    $("#review-dialog").close();
    return;
  }
  const button = $("#publish-button");
  button.disabled = true;
  button.textContent = appState.reviewBeforeRip ? "Queueing…" : "Publishing…";
  $("#review-error").textContent = "";
  const type = $('input[name="media_type"]:checked').value;
  const payload = {
    media_type: type,
    title: $("#review-title").value.trim(),
    year: $("#review-year").value ? Number($("#review-year").value) : null,
    season: type === "tv" && $("#review-season").value !== "" ? Number($("#review-season").value) : null,
    episode_start:
      type === "tv" && $("#review-episode").value !== "" ? Number($("#review-episode").value) : null,
    edition: type === "movie" ? $("#review-edition").value.trim() : "",
    selected_title_ids: $$('input[name="selected_title"]:checked').map(
      (input) => Number(input.value),
    ),
  };
  if (!payload.selected_title_ids.length) {
    $("#review-error").textContent = "Select at least one title to publish.";
    button.disabled = false;
    button.textContent = appState.reviewBeforeRip
      ? "Start selected titles"
      : "Publish to library";
    return;
  }
  try {
    await api(`/api/jobs/${$("#review-job-id").value}/metadata`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    $("#review-dialog").close();
    toast(
      appState.reviewBeforeRip
        ? "Approved titles queued for ripping."
        : "Published to your library.",
    );
    await refresh();
  } catch (error) {
    $("#review-error").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = appState.reviewBeforeRip
      ? "Start selected titles"
      : "Publish to library";
  }
});

async function boot() {
  try {
    const [health, overview, preferences] = await Promise.all([
      api("/healthz"),
      api("/api/overview"),
      api("/api/settings"),
    ]);
    renderPreferences(preferences);
    $("#simulation-notice").hidden = !health.simulation;
    $("#footer-library").textContent = health.simulation
      ? `SIMULATION / ${health.library_root}`
      : `LIBRARY / ${health.library_root}`;
    render(overview);
  } catch (error) {
    toast(`OpenRipper couldn’t load: ${error.message}`, "error");
  }
  connect();

  appState.feedbackTimer = setInterval(updateTransferFeedback, 1000);

  appState.pollTimer = setInterval(() => {
    if (document.visibilityState === "visible") {
      refresh().catch(() => {
        if (!appState.socket || appState.socket.readyState !== WebSocket.OPEN) {
          setConnection("offline", "Offline", "Can’t reach OpenRipper");
        }
      });
    }
  }, 5000);
}

document.addEventListener("visibilitychange", () => {
  updateTransferFeedback();

  if (document.visibilityState !== "visible") return;
  refresh().catch(() => {});
  if (!appState.socket || appState.socket.readyState >= WebSocket.CLOSING)
    connect();
});

window.addEventListener("online", connect);
window.addEventListener("offline", () => {
  clearTimeout(appState.reconnectTimer);
  setConnection("offline", "Offline", "This browser is offline");
});

window.addEventListener("pagehide", () => {
  appState.destroyed = true;
  clearTimeout(appState.reconnectTimer);
  clearTimeout(appState.activityTimer);
  clearInterval(appState.pollTimer);

  clearInterval(appState.feedbackTimer);

  appState.socket?.close();
});

$("#job-dialog").addEventListener("close", () => {
  appState.detailJobId = null;
  appState.detailRequest += 1;
});

boot();

function renderPreferences(preferences) {
  appState.preferences = preferences;
  $("#destination-path").textContent = preferences.movie_root || preferences.library_root;
  $("#destination-tv-path").textContent = preferences.tv_root || "Configure a TV destination";
  $("#automation-status").textContent = preferences.auto_rip
    ? "Automatic ripping on"
    : "Automatic ripping off";
  $("#automation-status").classList.toggle("paused", !preferences.auto_rip);
  $("#output-description").textContent =
    preferences.output_mode === "disc"
      ? "One folder per disc. Saves automatically."
      : "Media library naming. Uncertain matches need review.";
  $("#eject-description").textContent = preferences.eject_on_success
    ? "Eject after a successful rip."
    : "Keep the disc loaded after ripping.";
  $("#service-notice").hidden = Boolean(appState.overview.runtime?.queue_controls_supported);
}
async function showView(view) {
  appState.view = view;
  $$(".view").forEach((el) => (el.hidden = el.id !== `view-${view}`));
  $$(".nav-button").forEach((el) => {
    el.classList.toggle("selected", el.dataset.view === view);
    if (el.dataset.view === view) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  document.title = `OpenRipper · ${view === "queue" ? "Rip queue" : view.charAt(0).toUpperCase() + view.slice(1)}`;
  if (view === "settings") await loadSettings();
}
async function loadSettings() {
  const p = await api("/api/settings");
  renderPreferences(p);
  $("#settings-destination").value = p.library_root;
  $("#settings-auto").checked = p.auto_rip;
  $("#settings-eject").checked = p.eject_on_success;
  $("#settings-output").value = p.output_mode;
  $("#settings-selection").value = p.rip_mode;
  $("#settings-slots").value = p.max_concurrent_rips ?? 2;
  const busy = appState.overview.active_jobs.some((j) =>
    ["queued", "ripping", "scanning", "publishing", "needs_review"].includes(
      j.status,
    ),
  );
  $("#settings-protected").disabled = busy;
  $("#settings-lock-note").textContent = busy
    ? "Locked while jobs are active. Queue limits and automatic ripping can still change."
    : "These settings apply to new jobs.";
  $("#settings-slots").disabled = p.max_concurrent_rips == null;
  $("#settings-save").disabled = busy && p.max_concurrent_rips == null;
  $("#settings-error").textContent = "";
  $("#settings-feedback").textContent = "";
  if (typeof loadAdvancedSettings === "function") loadAdvancedSettings(p, busy);
  await loadIntegrationSettings();
}
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-view]");
  if (el) showView(el.dataset.view).catch((err) => toast(err.message, "error"));
});
$("#history-search").addEventListener("input", () =>
  renderHistory(appState.overview),
);
$("#history-filter").addEventListener("change", () =>
  renderHistory(appState.overview),
);
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const b = $("#settings-save");
  b.disabled = true;
  b.textContent = "Saving…";
  $("#settings-error").textContent = "";
  try {
    const values = {
      library_root: $("#settings-destination").value.trim(),
      auto_rip: $("#settings-auto").checked,
      eject_on_success: $("#settings-eject").checked,
      output_mode: $("#settings-output").value,
      rip_mode: $("#settings-selection").value,
    };
    if (appState.preferences.max_concurrent_rips != null)
      values.max_concurrent_rips = Number($("#settings-slots").value);
    if (typeof advancedValues === "function")
      Object.assign(values, advancedValues());
    const p = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify(values),
    });
    renderPreferences(p);
    $("#settings-feedback").textContent = "Settings saved.";
    await refresh();
  } catch (err) {
    $("#settings-error").textContent = err.message;
  } finally {
    b.disabled = false;
    b.textContent = "Save settings";
  }
});
function confirmCancel() {
  return new Promise((resolve) => {
    const d = $("#cancel-dialog");
    d.returnValue = "keep";
    d.addEventListener("close", () => resolve(d.returnValue === "cancel"), {
      once: true,
    });
    d.showModal();
  });
}
function notifyChanges(data) {
  for (const job of [...data.active_jobs, ...data.history]) {
    const prior = appState.knownStatuses.get(job.id);
    if (
      prior &&
      prior !== job.status &&
      ["complete", "failed", "needs_review"].includes(job.status)
    ) {
      const mode = localStorage.getItem("openripper-alerts") || "muted";
      if (mode !== "off")
        toast(
          `${titleFor(job)}: ${humanStatus(job.status)}`,
          job.status === "failed" ? "error" : "",
        );
      if (mode === "ding") playDing();
    }
    appState.knownStatuses.set(job.id, job.status);
  }
}
function playDing() {
  try {
    const c = appState.audioContext;
    if (!c || c.state !== "running") return;
    const o = c.createOscillator(),
      g = c.createGain();
    o.connect(g);
    g.connect(c.destination);
    o.frequency.value = 660;
    g.gain.setValueAtTime(0.12, c.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + 0.65);
    o.start();
    o.stop(c.currentTime + 0.65);
  } catch {}
}

async function loadHandbrakePresets() {
  const status = $("#preset-status");
  status.textContent = "Loading presets…";
  try {
    const result = await api("/api/handbrake/presets");
    if (!result.available) { status.textContent = result.message; return; }
    const select = $("#cfg-handbrake_preset");
    const selected = select.value || appState.preferences?.handbrake_preset || "Fast 1080p30";
    const groups = new Map();
    for (const preset of result.presets) {
      if (!groups.has(preset.group)) groups.set(preset.group, []);
      groups.get(preset.group).push(preset.name);
    }
    select.innerHTML = [...groups].map(([group, names]) => `<optgroup label="${escapeHtml(group)}">${names.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")}</optgroup>`).join("");
    if (!result.presets.some(p => p.name === selected)) select.add(new Option(`${selected} (saved; unavailable)`, selected));
    select.value = selected;
    status.textContent = result.message;
  } catch {
    status.textContent = "Presets unavailable. Check the saved CLI path. If the service was updated, restart it after active rips finish. Your saved preset is unchanged.";
  }
}

function loadAdvancedSettings(p, busy) {
  const preset = $("#cfg-handbrake_preset");
  if (![...preset.options].some(o => o.value === p.handbrake_preset)) preset.add(new Option(p.handbrake_preset || "Fast 1080p30", p.handbrake_preset || "Fast 1080p30"));
  loadHandbrakePresets();
  const supported = p.disc_folder_template != null;
  $$("[data-setting]").forEach((input) => {
    let value = p[input.dataset.setting];
    if (input.dataset.setting === "min_title_seconds")
      value = (value ?? 1200) / 60;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
  });
  $$(".advanced-protected").forEach((el) => (el.disabled = busy || !supported));
  const known = new Map(
    appState.overview.drives.map((d) => [
      d.id,
      `Drive ${d.device} · ${shortDrive(d)}`,
    ]),
  );
  Object.keys(p.drive_paths || {}).forEach((id) => {
    if (!known.has(id)) known.set(id, `Disconnected drive · ${id}`);
  });
  $("#drive-settings").innerHTML =
    '<legend class="sr-only">Per-drive destinations</legend>' +
    [...known]
      .map(
        ([id, label]) =>
          `<label class="field"><span>${escapeHtml(label)}</span><input data-drive-path="${escapeHtml(id)}" value="${escapeHtml(p.drive_paths?.[id] || "")}" placeholder="Use default destination"></label>`,
      )
      .join("");
  $("#alert-mode").value = localStorage.getItem("openripper-alerts") || "muted";
  api("/api/capabilities")
    .then((c) => {
      $("#artwork-status").textContent = c.tmdb_configured
        ? "TMDB token configured. Only confident title/year matches receive a poster."
        : "TMDB token is not configured. Add it on the server before enabling artwork.";
      $("#handbrake-status").textContent = c.handbrake_available
        ? "Configured HandBrake CLI found on this server."
        : "Configured HandBrake CLI not found. Install it separately, then set the executable path.";
    })
    .catch(() => {
      $("#handbrake-status").textContent =
        "Restart the service after current rips finish to use the new integration settings.";
    });
  updateNamingPreview();
}
function advancedValues() {
  if (appState.preferences.disc_folder_template == null) return {};
  const result = {};
  $$("[data-setting]").forEach((input) => {
    result[input.dataset.setting] =
      input.type === "checkbox"
        ? input.checked
        : input.type === "number"
          ? Number(input.value)
          : input.value.trim();
  });
  result.min_title_seconds *= 60;
  result.drive_paths = Object.fromEntries(
    $$("[data-drive-path]")
      .filter((i) => i.value.trim())
      .map((i) => [i.dataset.drivePath, i.value.trim()]),
  );
  return result;
}
function updateNamingPreview() {
  const values = {
    title: "Example Movie",
    year: 2026,
    year_suffix: " (2026)",
    edition_suffix: "",
    disc: "EXAMPLE_DISC",
    job_id: "unique-job-id",
    season: "01",
    episode: "02",
    episode_suffix: "",
    title_index: "00",
  };
  const template = $("#cfg-movie_name_template").value;
  $("#naming-preview").textContent =
    `Example: ${template.replace(/\{([^}:]+)(?::[^}]+)?\}/g, (all, key) => values[key] ?? all)}.mkv`;
}
$("#cfg-movie_name_template").addEventListener("input", updateNamingPreview);
$("#alert-mode").addEventListener("change", async () => {
  const mode = $("#alert-mode").value;
  localStorage.setItem("openripper-alerts", mode);
  if (mode === "ding") await enableAudio();
  $("#alert-feedback").textContent = "Alert preference saved on this device.";
});
async function enableAudio() {
  const Audio = window.AudioContext || window.webkitAudioContext;
  if (!Audio) {
    $("#alert-feedback").textContent =
      "This browser does not support audio alerts.";
    return;
  }
  appState.audioContext ??= new Audio();
  await appState.audioContext.resume();
}
$("#test-alert").addEventListener("click", async () => {
  await enableAudio();
  playDing();
  $("#alert-feedback").textContent =
    "Test sound played. Keep this dashboard open to receive alerts.";
});
document.addEventListener(
  "pointerdown",
  () => {
    if (localStorage.getItem("openripper-alerts") === "ding")
      enableAudio().catch(() => {});
  },
  { once: true },
);
