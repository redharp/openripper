// A disc track is never assumed to be a broadcast episode number.
let episodeLookupGeneration = 0;
let episodeCatalog = [];
let episodeShows = [];

function setupEpisodeReview(job) {
  episodeLookupGeneration++;
  episodeCatalog = [];
  episodeShows = [];
  $("#episode-query").value = job.title || "";
  $("#episode-show-field").hidden = true;
  $("#episode-show").innerHTML = '<option value="">Choose a show</option>';
  $("#episode-lookup-status").textContent = "Sonarr is optional. Manual matching works without a connection.";
  $("#episode-files").innerHTML = (job.titles || []).filter(t => t.ripped_path).map(t => {
    const saved = (job.episode_assignments || []).find(a => a.title_id === t.id) || {};
    return `<fieldset class="episode-file" data-episode-file="${t.id}"><legend><label><input type="checkbox" data-episode-include ${t.selected ? "checked" : ""} /> Title ${t.title_index} · ${escapeHtml(formatDuration(t.duration_seconds))}</label></legend>
      <p class="path">${escapeHtml(t.ripped_path.split(/[\\/]/).pop())}</p>
      <label class="field episode-catalog-field" hidden><span>Episode from Sonarr</span><select data-episode-catalog><option value="">Choose an episode</option></select></label>
      <div class="episode-fields">
        <label class="field"><span>Season</span><input data-episode-season type="number" min="0" max="999" value="${saved.season ?? job.season ?? ""}" /></label>
        <label class="field"><span>Episode</span><input data-episode-number type="number" min="1" max="9999" value="${saved.episode ?? ""}" /></label>
        <label class="field"><span>Through <i>optional</i></span><input data-episode-end type="number" min="1" max="9999" value="${saved.episode_end && saved.episode_end !== saved.episode ? saved.episode_end : ""}" /></label>
        <label class="field episode-name-field"><span>Episode title <i>optional</i></span><input data-episode-name maxlength="180" value="${escapeHtml(saved.episode_title || "")}" /></label>
      </div><p class="field-help" data-episode-preview></p>
    </fieldset>`;
  }).join("");
  updateEpisodeReview();
}

function updateEpisodeReview() {
  const tv = $('input[name="media_type"]:checked').value === "tv";
  const matching = tv && !appState.reviewBeforeRip;
  $("#episode-matching").hidden = !matching;
  $("#review-titles").closest("fieldset").hidden = matching;
  $("#review-season").closest("label").hidden = matching;
  $("#review-heading").textContent = matching ? "Match episodes" : appState.reviewBeforeRip ? "Preview this rip" : "Name this rip";
  $("#review-description").textContent = matching
    ? "Ripping is finished. Confirm the show and match each selected file to its actual episode. You can close this screen and return later."
    : appState.reviewBeforeRip ? "Choose which titles to copy. TV episodes will wait for matching after ripping." : "Confirm the name and selected files to publish this rip.";
  $("#publish-button").textContent = appState.reviewBeforeRip ? "Start selected titles" : matching ? "Publish matched episodes" : "Publish to library";
  for (const row of $$("[data-episode-file]")) {
    const selected = $(`input[name="selected_title"][value="${row.dataset.episodeFile}"]`).checked;
    row.dataset.selected = String(selected);
    $('[data-episode-include]', row).checked = selected;
    for (const input of $$('input:not([data-episode-include]), select', row)) input.disabled = !matching || !selected;
    $('.episode-fields', row).hidden = !selected;
    for (const input of $$('[data-episode-season], [data-episode-number]', row)) input.required = matching && selected;
    const season = $('[data-episode-season]', row).value;
    const episode = $('[data-episode-number]', row).value;
    const end = $('[data-episode-end]', row).value;
    const title = $('#review-title').value.trim();
    $('[data-episode-preview]', row).textContent = season !== "" && episode !== ""
      ? `Assigned to ${title || "Show"} · S${season.padStart(2, "0")}E${episode.padStart(2, "0")}${end ? `–E${end.padStart(2, "0")}` : ""}. Your TV filename template applies when publishing.`
      : "Needs an episode assignment. Track order may differ from episode order.";
  }
}

function collectEpisodeAssignments() {
  return $$("[data-episode-file]").filter(row => row.dataset.selected === "true").map(row => ({
    title_id: Number(row.dataset.episodeFile),
    season: Number($('[data-episode-season]', row).value),
    episode: Number($('[data-episode-number]', row).value),
    episode_end: $('[data-episode-end]', row).value ? Number($('[data-episode-end]', row).value) : null,
    episode_title: $('[data-episode-name]', row).value.trim(),
  }));
}

$("#review-form").addEventListener("input", event => {
  if (event.target.id === "review-season") {
    for (const input of $$("[data-episode-season]")) if (!input.value) input.value = event.target.value;
  }
  if (event.target.matches('[data-episode-include]')) {
    const id = event.target.closest('[data-episode-file]').dataset.episodeFile;
    $(`input[name="selected_title"][value="${id}"]`).checked = event.target.checked;
  }
  updateEpisodeReview();
});

$("#episode-search-button").addEventListener("click", async () => {
  const generation = ++episodeLookupGeneration;
  const status = $("#episode-lookup-status");
  status.textContent = "Searching Sonarr’s existing shows…";
  $("#episode-show-field").hidden = true;
  episodeCatalog = [];
  for (const field of $$(".episode-catalog-field")) field.hidden = true;
  try {
    const shows = await api(`/api/integrations/sonarr/library?query=${encodeURIComponent($("#episode-query").value.trim())}`);
    if (generation !== episodeLookupGeneration) return;
    episodeShows = shows;
    $("#episode-show").innerHTML = '<option value="">Choose a show</option>' + shows.map(show => `<option value="${show.id}">${escapeHtml(show.title)}${show.year ? ` (${show.year})` : ""}</option>`).join("");
    $("#episode-show-field").hidden = !shows.length;
    status.textContent = shows.length ? "Choose the correct show, then assign an episode to each file." : "No matching shows in Sonarr. Try another name or enter details manually.";
  } catch (error) {
    if (generation === episodeLookupGeneration) status.textContent = `${error.message}. You can still match episodes manually.`;
  }
});

$("#episode-show").addEventListener("change", async event => {
  const generation = ++episodeLookupGeneration;
  const show = episodeShows.find(s => String(s.id) === event.target.value);
  episodeCatalog = [];
  for (const field of $$(".episode-catalog-field")) field.hidden = true;
  if (!show) return;
  $("#episode-lookup-status").textContent = "Loading episodes…";
  try {
    const episodes = await api(`/api/integrations/sonarr/episodes/${show.id}`);
    if (generation !== episodeLookupGeneration) return;
    episodeCatalog = episodes.sort((a,b) => a.seasonNumber - b.seasonNumber || a.episodeNumber - b.episodeNumber);
    for (const row of $$("[data-episode-file]")) {
      for (const input of $$('[data-episode-number], [data-episode-end], [data-episode-name]', row)) input.value = "";
    }
    $("#review-title").value = show.title;
    $("#review-year").value = show.year || "";
    const options = '<option value="">Choose an episode</option>' + episodeCatalog.map(ep => `<option value="${ep.id}">S${String(ep.seasonNumber).padStart(2,"0")}E${String(ep.episodeNumber).padStart(2,"0")} · ${escapeHtml(ep.title)}</option>`).join("");
    for (const select of $$("[data-episode-catalog]")) { select.innerHTML = options; select.closest("label").hidden = !episodes.length; }
    $("#episode-lookup-status").textContent = episodes.length ? "Episode list loaded. Confirm which episode is in each file." : "No episodes found. Enter their details manually.";
    updateEpisodeReview();
  } catch (error) {
    if (generation === episodeLookupGeneration) $("#episode-lookup-status").textContent = `${error.message}. Manual matching is still available.`;
  }
});

$("#episode-files").addEventListener("change", event => {
  if (!event.target.matches("[data-episode-catalog]")) return;
  const episode = episodeCatalog.find(e => String(e.id) === event.target.value);
  if (!episode) return;
  const row = event.target.closest("[data-episode-file]");
  $('[data-episode-season]', row).value = episode.seasonNumber;
  $('[data-episode-number]', row).value = episode.episodeNumber;
  $('[data-episode-end]', row).value = "";
  $('[data-episode-name]', row).value = episode.title || "";
  updateEpisodeReview();
});
