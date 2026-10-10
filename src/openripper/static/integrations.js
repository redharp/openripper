// Connection secrets stay in password fields only until save/test completes.
let namingJob = null;
let namingRequest = 0;

function integrationSummary(job) {
  if (!job.naming_supported || job.status !== "complete") return "";
  const state = job.integration || {};
  const blocked = ["running", "waiting", "complete", "uncertain"].includes(
    state.status,
  );
  return `<section class="detail-block"><h3>Library naming</h3><p>${escapeHtml(state.message || "Send the finished files to an existing movie or show in Radarr or Sonarr.")}</p>${(state.outputs || []).map((path) => `<p class="path">${escapeHtml(path)}</p>`).join("")}<p class="field-help">Your original rip stays at its saved location. Managed library copies use the server’s naming rules.</p>${state.status === "waiting" ? `<button class="button" data-action="check-names" data-job="${escapeHtml(job.id)}">Check naming status</button>` : ""}${blocked ? "" : `<button class="button" data-action="process-names" data-job="${escapeHtml(job.id)}">Process names</button>`}</section>`;
}

async function loadIntegrationSettings() {
  const root = $("#integration-connections");
  root.textContent = "Loading connections…";
  try {
    const values = await api("/api/integrations");
    root.innerHTML = ["radarr", "sonarr"]
      .map((provider) => {
        const p = values[provider];
        return `<form class="arr-form settings-fields" data-provider="${provider}" autocomplete="off">
        <h3>${provider === "radarr" ? "Radarr · movies" : "Sonarr · TV"}</h3>
        <label class="check-field"><input name="enabled" type="checkbox" ${p.enabled ? "checked" : ""}> Enable ${provider === "radarr" ? "Radarr" : "Sonarr"} and hand off new completed rips</label>
        <label class="field"><span>Server URL</span><input name="url" type="url" value="${escapeHtml(p.url)}" placeholder="http://media-server:${provider === "radarr" ? "7878" : "8989"}"></label>
        <label class="field"><span>API key</span><input name="api_key" type="password" autocomplete="new-password" placeholder="${p.key_configured ? "Key saved · leave blank to keep it" : "From the server’s Settings → General"}"></label>
        <label class="check-field"><input name="clear_api_key" type="checkbox"> Remove saved key</label>
        <details><summary>Shared-folder path mapping</summary><p class="field-help">Only needed when this server sees your files at a different path, such as inside Docker. Both paths must point to the same files.</p>
        <label class="field"><span>Folder on OpenRipper</span><input name="local_path" value="${escapeHtml(p.local_path)}" placeholder="/media/rips"></label>
        <label class="field"><span>Same folder on ${provider === "radarr" ? "Radarr" : "Sonarr"}</span><input name="remote_path" value="${escapeHtml(p.remote_path)}" placeholder="/data/rips"></label></details>
        <p class="field-help">Keys are stored in the private local configuration. Changing the server URL requires entering its key again.</p>
        <div class="arr-actions"><button class="button" type="button" data-arr-test>Test connection</button><button class="button button-primary" type="submit">Save ${provider === "radarr" ? "Radarr" : "Sonarr"}</button></div>
        <p class="arr-feedback field-help" role="status"></p></form>`;
      })
      .join("");
  } catch {
    root.textContent =
      "Integration settings are unavailable. Restart the updated service after active rips finish.";
  }
}

async function saveOrTestConnection(form, test) {
  const feedback = $(".arr-feedback", form);
  const values = Object.fromEntries(new FormData(form));
  values.enabled = form.elements.enabled.checked;
  values.clear_api_key = form.elements.clear_api_key.checked;
  if (!values.api_key) delete values.api_key;
  $$("button", form).forEach((b) => (b.disabled = true));
  feedback.textContent = test ? "Testing connection…" : "Saving connection…";
  feedback.classList.remove("form-error");
  try {
    const result = await api(
      `/api/integrations/${form.dataset.provider}${test ? "/test" : ""}`,
      {
        method: test ? "POST" : "PUT",
        body: JSON.stringify(values),
      },
    );
    if (!test) {
      form.elements.api_key.value = "";
      form.elements.api_key.placeholder = result.key_configured
        ? "Key saved · leave blank to keep it"
        : "No key saved";
      form.elements.clear_api_key.checked = false;
    }
    feedback.textContent = test
      ? `${result.message} · ${result.version}. Save to apply these settings.`
      : "Connection saved. You can process earlier rips from History → Details.";
  } catch (err) {
    feedback.classList.add("form-error");
    feedback.textContent = err.message;
  } finally {
    $$("button", form).forEach((b) => (b.disabled = false));
  }
}

$("#integration-connections").addEventListener("submit", (e) => {
  e.preventDefault();
  saveOrTestConnection(e.target, false);
});
$("#integration-connections").addEventListener("click", (e) => {
  if (e.target.closest("[data-arr-test]"))
    saveOrTestConnection(e.target.closest("form"), true);
});

async function openNaming(jobId) {
  namingJob = await api(`/api/jobs/${jobId}`);
  $("#job-dialog").close();
  $("#naming-heading").textContent = `Process names · ${titleFor(namingJob)}`;
  $("#naming-provider").value =
    namingJob.media_type === "tv" ? "sonarr" : "radarr";
  $("#naming-query").value = "";
  $("#naming-error").textContent = "";
  $("#naming-dialog").showModal();
  await searchNamingLibrary();
}

async function searchNamingLibrary() {
  const request = ++namingRequest;
  $("#naming-target").innerHTML =
    '<option value="">Searching library…</option>';
  $("#naming-files").innerHTML = "";
  $("#naming-submit").disabled = true;
  $("#naming-error").textContent = "";
  try {
    const items = await api(
      `/api/integrations/${$("#naming-provider").value}/library?query=${encodeURIComponent($("#naming-query").value.trim())}`,
    );
    if (request !== namingRequest) return;
    $("#naming-target").innerHTML =
      '<option value="">Choose an existing title…</option>' +
      items
        .map(
          (m) =>
            `<option value="${m.id}">${escapeHtml(m.title)}${m.year ? ` (${m.year})` : ""}</option>`,
        )
        .join("");
    if (!items.length)
      $("#naming-error").textContent =
        "No matching titles. Add the movie or show in Sonarr/Radarr first, or try another search.";
  } catch (err) {
    if (request !== namingRequest) return;
    $("#naming-target").innerHTML =
      '<option value="">Library unavailable</option>';
    $("#naming-error").textContent = err.message;
  }
}

async function loadNamingFiles() {
  const request = ++namingRequest;
  $("#naming-error").textContent = "";
  $("#naming-files").innerHTML = "";
  $("#naming-submit").disabled = true;
  const id = $("#naming-target").value;
  if (!id) return;
  const tv = $("#naming-provider").value === "sonarr";
  try {
    const episodes = tv
      ? await api(`/api/integrations/sonarr/episodes/${id}`)
      : [];
    if (request !== namingRequest) return;
    episodes.sort(
      (a, b) =>
        a.seasonNumber - b.seasonNumber || a.episodeNumber - b.episodeNumber,
    );
    $("#naming-files").innerHTML = namingJob.titles
      .filter((t) => t.ripped_path)
      .map(
        (t, i) =>
          `<div class="naming-file"><label class="check-field"><input type="${tv ? "checkbox" : "radio"}" name="naming-file" value="${t.id}" ${!tv && i === 0 ? "checked" : ""}>${escapeHtml(t.name || `Title ${t.title_index}`)} · ${escapeHtml(formatDuration(t.duration_seconds))}</label>${tv ? `<label class="field"><span>Episode(s) in this file</span><select multiple size="4" data-episodes="${t.id}">${episodes.map((ep) => `<option value="${ep.id}">S${String(ep.seasonNumber).padStart(2, "0")}E${String(ep.episodeNumber).padStart(2, "0")} · ${escapeHtml(ep.title || "")}</option>`).join("")}</select></label>` : ""}</div>`,
      )
      .join("");
    $("#naming-submit").disabled = false;
  } catch (err) {
    if (request === namingRequest) $("#naming-error").textContent = err.message;
  }
}

$("#naming-provider").addEventListener("change", searchNamingLibrary);
$("#naming-search").addEventListener("click", searchNamingLibrary);
$("#naming-query").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    searchNamingLibrary();
  }
});
$("#naming-target").addEventListener("change", loadNamingFiles);
$("#naming-close").addEventListener("click", () => $("#naming-dialog").close());
$("#naming-dialog").addEventListener("close", () => namingRequest++);
$("#naming-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const files = $$("#naming-files input:checked").map((input) => ({
    title_id: Number(input.value),
    episode_ids: Array.from(
      $(`[data-episodes="${input.value}"]`)?.selectedOptions || [],
    ).map((o) => Number(o.value)),
  }));
  if (
    !files.length ||
    ($("#naming-provider").value === "sonarr" &&
      files.some((f) => !f.episode_ids.length))
  ) {
    $("#naming-error").textContent =
      "Choose files and assign episodes to every selected TV file.";
    return;
  }
  $("#naming-submit").disabled = true;
  try {
    await api(`/api/jobs/${namingJob.id}/integration`, {
      method: "POST",
      body: JSON.stringify({
        provider: $("#naming-provider").value,
        target_id: Number($("#naming-target").value),
        files,
      }),
    });
    $("#naming-dialog").close();
    await openJobDetail(namingJob.id);
  } catch (err) {
    $("#naming-error").textContent = err.message;
  } finally {
    $("#naming-submit").disabled = false;
  }
});
