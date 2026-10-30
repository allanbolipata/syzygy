/* Syzygy basic front end: sample query/filter, matching views, overrides. */
"use strict";

const API = "/v1";

const $ = (sel) => document.querySelector(sel);

/* ---------- helpers ---------- */

function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtDate(v) {
  if (!v) return "";
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? esc(v) : d.toISOString().slice(0, 16).replace("T", " ");
}

/* JSON-valued sample fields (preservation, sample_origin) may be lists or scalars. */
function metaText(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (Array.isArray(v)) return v.map(metaText).join(", ");
  if (typeof v === "object") return esc(JSON.stringify(v));
  return esc(v);
}

function keyList(v) {
  if (!v || !v.length) return "";
  return "<ul class='mismatches'>" + v.map((k) => `<li>${esc(k)}</li>`).join("") + "</ul>";
}

function noteList(v) {
  if (!v || !v.length) return "";
  return "<ul class='notes'>" + v.map((n) => `<li>${esc(n)}</li>`).join("") + "</ul>";
}

let bannerTimer = null;
function banner(kind, html, sticky) {
  const el = $("#banner");
  el.innerHTML = `<div class="msg ${kind}">${html}</div>`;
  if (bannerTimer) clearTimeout(bannerTimer);
  if (!sticky) bannerTimer = setTimeout(() => (el.innerHTML = ""), 8000);
}

async function api(path, opts) {
  const res = await fetch(API + path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) {
    const detail = body && body.detail
      ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail))
      : res.statusText;
    if (res.status === 403) {
      // 403 = authenticated but not allow-listed: send the browser to the
      // login page WITH the explanation (a plain /login shows nothing).
      location.href = "/login?error=" + encodeURIComponent(detail);
    } else if (res.status === 401) {
      location.href = "/login";
    }
    throw new Error(detail);
  }
  return body;
}

/* ---------- view navigation (tabs + back button) ---------- */

/* Navigation state lives in the URL hash, so browser Back/Forward, reloads
 * and shared links all work:
 *   #/samples   #/patients[/<patient__cmo>]   #/requests[/<request_id>]
 *   #/matches[/<sample primary_id>]
 * Every navigation (tab switch, opening/closing a detail panel, opening
 * matches for a sample) pushes a history entry. */

const VIEWS = ["samples", "patients", "requests", "pooled", "pairs", "matches", "try"];
let restoringNav = false;
let PAIRING_LOGICS = [];

function currentViewName() {
  const el = document.querySelector("main.view:not(.hidden)");
  return el ? el.id.replace("view-", "") : null;
}

function currentHash() {
  const view = currentViewName() || "samples";
  let id = null;
  if (view === "patients" && patientsState.patient) id = patientsState.patient.patient__cmo;
  else if (view === "requests" && requestsState.request) id = requestsState.request.request_id;
  else if (view === "matches" && matchesState.sample) id = matchesState.sample.primary_id;
  else if (view === "try" && tryState.sample) id = tryState.sample.primary_id;
  return `#/${view}` + (id ? `/${encodeURIComponent(id)}` : "");
}

/* Push a history entry for the current UI state (no-op while restoring or unchanged). */
function pushNav() {
  if (restoringNav) return;
  const h = currentHash();
  if (h !== location.hash) history.pushState(null, "", h);
}

function showView(name) {
  document.querySelectorAll(".tab[data-tab]").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name)
  );
  document.querySelectorAll("main.view").forEach((v) =>
    v.classList.toggle("hidden", v.id !== `view-${name}`)
  );
  pushNav();
}

function closePatientDetail() {
  patientsState.patient = null;
  $("#p-detail").classList.add("hidden");
}

function closeRequestDetail() {
  requestsState.request = null;
  $("#r-detail").classList.add("hidden");
}

/* Make the UI match location.hash (used on popstate and initial load). */
async function restoreFromHash() {
  const m = location.hash.match(/^#\/([a-z]+)(?:\/(.+))?$/);
  const view = m && VIEWS.includes(m[1]) ? m[1] : "samples";
  const id = m && m[2] ? decodeURIComponent(m[2]) : null;
  restoringNav = true;
  try {
    showView(view);
    if (view === "patients") {
      if (!id) closePatientDetail();
      else if (!patientsState.patient || patientsState.patient.patient__cmo !== id) {
        const rows = await api(`/patient/?q=${encodeURIComponent(id)}&limit=200`);
        const row = rows.find((p) => p.patient__cmo === id);
        if (row) await openPatient(row);
        else banner("error", `Patient ${esc(id)} not found.`, true);
      }
    } else if (view === "requests") {
      if (!id) closeRequestDetail();
      else if (!requestsState.request || requestsState.request.request_id !== id) {
        const rows = await api(`/request/?q=${encodeURIComponent(id)}&limit=200`);
        const row = rows.find((r) => r.request_id === id);
        if (row) await openRequest(row);
        else banner("error", `Request ${esc(id)} not found.`, true);
      }
    } else if (view === "matches" && id) {
      if (!matchesState.sample || matchesState.sample.primary_id !== id) {
        const rows = await api(`/sample/?primary_id=${encodeURIComponent(id)}&limit=50`);
        const row = rows.find((x) => x.primary_id === id);
        if (row) await openMatchesFor(row);
        else banner("error", `Sample ${esc(id)} not found.`, true);
      }
    } else if (view === "try" && id) {
      const rows = await api(`/sample/lookup?q=${encodeURIComponent(id)}&limit=25`);
      const row = rows.find((x) =>
        x.primary_id.toLowerCase() === id.toLowerCase() ||
        x.cmo_sample_name.toLowerCase() === id.toLowerCase()
      );
      if (row) {
        $("#tq-id").value = id;
        tryFillSelect(rows);
        $("#tq-sample").value = row.id;
        await selectTrySample(row);
      } else banner("error", `Sample ${esc(id)} not found.`, true);
    }
  } catch (e) {
    banner("error", `Failed to restore view: ${esc(e.message)}`, true);
  } finally {
    restoringNav = false;
  }
}

window.addEventListener("popstate", restoreFromHash);

document.querySelectorAll(".tab[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => showView(btn.dataset.tab));
});

/* ---------- cross-view navigation (patient__cmo / request_id links) ---------- */

function navigateToPatient(patientId) {
  $("#p-q").value = patientId;
  showView("patients");
  loadPatients().then((results) => {
    if (!results) return;
    const row = results.find((p) => p.patient__cmo === patientId);
    if (row) openPatient(row);
    else banner("error", `No patient found with id ${esc(patientId)}.`, true);
  });
}

function navigateToRequest(requestId) {
  $("#r-q").value = requestId;
  showView("requests");
  loadRequests().then((results) => {
    if (!results) return;
    const row = results.find((r) => r.request_id === requestId);
    if (row) openRequest(row);
    else banner("error", `No request found with id ${esc(requestId)}.`, true);
  });
}

/* ---------- samples view ---------- */

const samplesState = { limit: 50, offset: 0, results: [] };

const SAMPLE_FILTER_IDS = [
  "primary_id", "cmo_sample_name", "patient__cmo", "request_id",
  "sample_type", "gene_panel", "bait_set", "run_mode", "file_group",
];

function samplesParams(offset) {
  const p = new URLSearchParams();
  p.set("limit", samplesState.limit);
  p.set("offset", offset);
  const q = $("#f-q").value.trim();
  if (q) p.set("q", q);
  for (const id of SAMPLE_FILTER_IDS) {
    const v = $(`#f-${id}`).value.trim();
    if (v) p.set(id, v);
  }
  const role = $("#f-tumor_or_normal").value;
  if (role) p.set("tumor_or_normal", role);
  return p.toString();
}

async function loadSamples(offset) {
  try {
    const results = await api(`/sample/?${samplesParams(offset)}`);
    samplesState.results = results;
    samplesState.offset = offset;
    renderSamples();
  } catch (e) {
    banner("error", `Failed to load samples: ${esc(e.message)}`, true);
  }
}

function renderSamples() {
  const rows = samplesState.results;
  const tbody = $("#samples-table tbody");
  tbody.innerHTML = rows.map((s) => `
    <tr class="click" data-id="${s.id}">
      <td>${esc(s.primary_id)}</td>
      <td>${esc(s.cmo_sample_name)}</td>
      <td>${esc(s.patient__cmo || "—")}</td>
      <td>${esc(s.patient__dmp || "—")}</td>
      <td>${esc(s.anon_id || "—")}</td>
      <td>${esc(s.request_id)}</td>
      <td><span class="badge ${s.tumor_or_normal.toLowerCase()}">${esc(s.tumor_or_normal)}</span></td>
      <td>${esc(s.sample_type)}</td>
      <td>${esc(s.gene_panel)}</td>
      <td>${esc(s.bait_set || "")}</td>
      <td>${esc(s.run_mode || "")}</td>
      <td>${esc(s.file_group || "")}</td>
      <td>${metaText(s.preservation)}</td>
      <td>${metaText(s.sample_origin)}</td>
      <td>${fmtDate(s.sequencing_date)}</td>
    </tr>`).join("");
  $("#s-empty").classList.toggle("hidden", rows.length > 0);
  $("#s-count").textContent = rows.length ? `${rows.length} on this page` : "";
  $("#s-prev").disabled = samplesState.offset === 0;
  $("#s-next").disabled = rows.length < samplesState.limit;
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const sample = rows.find((s) => s.id === tr.dataset.id);
      if (sample) openMatchesFor(sample);
    });
  });
}

$("#btn-search").addEventListener("click", () => loadSamples(0));
$("#btn-clear").addEventListener("click", () => {
  $("#f-q").value = "";
  for (const id of SAMPLE_FILTER_IDS) $(`#f-${id}`).value = "";
  $("#f-tumor_or_normal").value = "";
  loadSamples(0);
});
$("#f-limit").addEventListener("change", (e) => {
  samplesState.limit = Number(e.target.value);
  loadSamples(0);
});
$("#s-prev").addEventListener("click", () => loadSamples(Math.max(0, samplesState.offset - samplesState.limit)));
$("#s-next").addEventListener("click", () => loadSamples(samplesState.offset + samplesState.limit));
for (const el of ["#f-q", ...SAMPLE_FILTER_IDS.map((id) => `#f-${id}`)]) {
  $(el).addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadSamples(0);
  });
}

/* ---------- matches & overrides view ---------- */

// Candidates are a bounded page of the strategy's candidate scope:
// the API returns at most OTHERS_PAGE_SIZE rows per request, with
// whole-scope totals alongside (matches DEFAULT_OTHER_MATCHES_LIMIT).
const OTHERS_PAGE_SIZE = 500;
const matchesState = {
  sample: null,
  data: null,
  busy: false,
  candidates: [],
  othersOffset: 0,
  othersTotal: 0,
};

async function loadLogics() {
  try {
    const logics = await api("/pairing-logic/?limit=100");
    $("#m-logic").innerHTML = logics.map((l) =>
      `<option value="${esc(l.name)}"${l.active ? "" : " disabled"}>${esc(l.name)}${l.active ? "" : " (inactive)"}</option>`
    ).join("");
    // Pairs page lists every logic (inactive ones can still own pairings).
    $("#pr-logic").innerHTML = logics.map((l) =>
      `<option value="${esc(l.name)}">${esc(l.name)}${l.active ? "" : " (inactive)"}</option>`
    ).join("");
    PAIRING_LOGICS = logics;
    renderPrGroups();
    // Try Pairing: active logics only (the dry-run endpoint refuses
    // inactive ones), same options as the Matches tab.
    $("#tq-logic").innerHTML = logics.map((l) =>
      `<option value="${esc(l.name)}"${l.active ? "" : " disabled"}>${esc(l.name)}${l.active ? "" : " (inactive)"}</option>`
    ).join("");
    updateDmpTriggerButton();
  } catch (e) {
    banner("error", `Failed to load pairing logics: ${esc(e.message)}`, true);
  }
}

/* Select the pairing logic assigned to the sample's bait set (server-side
   bait_sets table), so the page doesn't default to the first logic. */
async function applyBaitSetLogic(sample, logicSelId) {
  if (!sample || !sample.bait_set) return false;
  try {
    const row = await api(`/bait-sets/lookup?name=${encodeURIComponent(sample.bait_set)}`);
    if (!row) return false;
    const logicSel = $(`#${logicSelId || "m-logic"}`);
    const opt = [...logicSel.options].find((o) => o.value === row.pairing_logic && !o.disabled);
    if (!opt) return false;
    logicSel.value = opt.value;
    return true;
  } catch (e) {
    return false;
  }
}

async function openMatchesFor(sample) {
  matchesState.sample = sample;
  const sel = $("#m-sample");
  sel.innerHTML = `<option value="${sample.id}" selected>${esc(sampleLabel(sample))}</option>`;
  sel.disabled = false;
  $("#m-show").disabled = false;
  // Prefer the bait set's assigned logic, then the logic this sample is
  // currently paired under, when known.
  const byLogic = patientsState.pairings ? patientsState.pairings[sample.id] : null;
  if (await applyBaitSetLogic(sample)) {
    // bait-set logic selected
  } else if (byLogic) {
    const logicSel = $("#m-logic");
    const known = Object.keys(byLogic)[0];
    const opt = [...logicSel.options].find((o) => o.value === known && !o.disabled);
    if (opt) logicSel.value = opt.value;
  }
  showView("matches");
  matchesState.othersOffset = 0;
  showMatches(0);
}

function sampleLabel(s) {
  return `${s.primary_id} — ${s.cmo_sample_name} (${s.patient__cmo || "no patient"}, ${s.tumor_or_normal})`;
}

async function searchMatchesSamples() {
  const q = $("#m-q").value.trim();
  if (!q) return;
  try {
    const results = await api(`/sample/?q=${encodeURIComponent(q)}&limit=25`);
    const sel = $("#m-sample");
    if (!results.length) {
      sel.innerHTML = "";
      sel.disabled = true;
      $("#m-show").disabled = true;
      banner("info", `No samples match "${esc(q)}".`);
      return;
    }
    sel.innerHTML = results.map((s) =>
      `<option value="${s.id}">${esc(sampleLabel(s))}</option>`
    ).join("");
    sel.disabled = false;
    $("#m-show").disabled = false;
    matchesState.candidates = results;
    matchesState.sample = results[0];
    await applyBaitSetLogic(results[0]);
  } catch (e) {
    banner("error", `Search failed: ${esc(e.message)}`, true);
  }
}

async function showMatches(offset) {
  const sample = matchesState.sample;
  const logic = $("#m-logic").value;
  $("#m-dmp-warning").classList.toggle("hidden", !isDmpLogic(logic));
  if (!sample || !logic) return;
  if (matchesState.busy) return;
  if (offset === undefined || typeof offset !== "number") offset = matchesState.othersOffset;
  matchesState.busy = true;
  $("#m-show").disabled = true;
  const params = new URLSearchParams();
  params.set("pairing_logic_name", logic);
  params.set("limit", OTHERS_PAGE_SIZE);
  params.set("offset", offset);
  try {
    const data = await api(`/sample/${sample.id}/possible-matches?${params}`);
    matchesState.data = data;
    matchesState.othersOffset = offset;
    matchesState.othersTotal = data.other_matches_total ?? data.other_matches.length;
    renderMatches(data, logic);
  } catch (e) {
    banner("error", `Failed to load matches: ${esc(e.message)}`, true);
    hideMatchesPanels();
  } finally {
    matchesState.busy = false;
    $("#m-show").disabled = false;
  }
}

function hideMatchesPanels() {
  for (const id of ["m-summary", "m-current-panel", "m-others-panel"]) $(`#${id}`).classList.add("hidden");
}

function renderMatches(data, logic) {
  const s = data.sample;
  $("#m-summary").classList.remove("hidden");
  $("#m-summary").innerHTML = `
    <h2>Sample</h2>
    <dl class="kv">
      <dt>id</dt><dd>${esc(s.id)}</dd>
      <dt>primary_id</dt><dd>${esc(s.primary_id)}</dd>
      <dt>cmo_sample_name</dt><dd>${esc(s.cmo_sample_name)}</dd>
      <dt>patient__cmo</dt><dd>${s.patient__cmo ? `<span class="nav-link" data-nav-patient="${esc(s.patient__cmo)}" title="Go to the patient page">${esc(s.patient__cmo)}</span>` : "—"}</dd>
      <dt>patient__dmp</dt><dd>${esc(s.patient__dmp || "—")}</dd>
      <dt>anon_id</dt><dd>${esc(s.anon_id || "—")}</dd>
      <dt>request_id</dt><dd>${s.request_id ? `<span class="nav-link" data-nav-request="${esc(s.request_id)}" title="Show the samples for this request">${esc(s.request_id)}</span>` : "—"}</dd>
      <dt>role</dt><dd><span class="badge ${s.tumor_or_normal.toLowerCase()}">${esc(s.tumor_or_normal)}</span></dd>
      <dt>sample_type</dt><dd>${esc(s.sample_type)}</dd>
      <dt>gene_panel</dt><dd>${esc(s.gene_panel)}</dd>
      <dt>bait_set</dt><dd>${esc(s.bait_set || "—")}</dd>
      <dt>run_mode</dt><dd>${esc(s.run_mode || "—")}</dd>
      <dt>file_group</dt><dd>${esc(s.file_group || "—")}</dd>
      <dt>preservation</dt><dd>${metaText(s.preservation)}</dd>
      <dt>sample_origin</dt><dd>${metaText(s.sample_origin)}</dd>
      <dt>sequencing_date</dt><dd>${fmtDate(s.sequencing_date)}</dd>
      <dt>errors</dt><dd>${s.errors && s.errors.length ? esc(s.errors.join("; ")) : "—"}</dd>
    </dl>
    <p class="hint">Click patient__cmo / request_id to go to that patient's or request's page.</p>`;

  // Current matches: a tumor has at most one; a normal matches 1-to-many
  // and lists every currently-paired tumor here.
  $("#m-current-panel").classList.remove("hidden");
  const counterpartWord = s.tumor_or_normal === "TUMOR" ? "normal" : "tumor";
  const cmList = data.current_matches || [];
  $("#m-current-title").textContent = cmList.length > 1 ? "Current matches" : "Current match";
  if (!cmList.length) {
    $("#m-current").innerHTML = `<p class="empty">No current ${counterpartWord} match for logic <strong>${esc(logic)}</strong>.</p>`;
  } else {
    $("#m-current").innerHTML = cmList.map((cm) => {
      const c = cm.sample;
      return `
        <div class="current-match">
          <dl class="kv">
            <dt>matched ${counterpartWord}</dt>
            <dd><span class="nav-link" data-sample-id="${esc(c.id)}" title="Open this sample's matches &amp; overrides">${esc(c.primary_id)} — ${esc(c.cmo_sample_name)} (${esc(c.patient__cmo || "no patient")})</span></dd>
            <dt>trigger_type</dt><dd>${esc(cm.trigger_type || "automatic")}</dd>
            <dt>mismatched_keys</dt><dd>${cm.mismatched_keys ? keyList(cm.mismatched_keys) : "—"}</dd>
          </dl>
          ${noteList(cm.notes)}</div>`;
    }).join("");
  }

  // Other matches.
  $("#m-others-panel").classList.remove("hidden");
  const isTumor = s.tumor_or_normal === "TUMOR";
  const rows = data.other_matches;
  const tbody = $("#m-others tbody");
  tbody.innerHTML = rows.map((c) => {
    const cs = c.sample;
    const status = c.matched
      ? '<span class="badge ok">matches all keys</span>'
      : '<span class="badge bad">mismatch</span>';
    const overrideBtn = isTumor
      ? `<button class="small danger" data-override="${cs.id}">Override</button>`
      : "";
    const star = c.would_be_selected ? ' <span class="star" title="strategy cascade would pick this candidate">★</span>' : "";
    return `
      <tr class="${c.would_be_selected ? "current click" : "click"}" data-sample-id="${esc(cs.id)}" title="Open this sample's matches &amp; overrides">
        <td>${esc(cs.primary_id)} — ${esc(cs.cmo_sample_name)}${star}</td>
        <td>${esc(cs.patient__cmo || "—")}</td>
        <td>${esc(cs.patient__dmp || "—")}</td>
        <td><span class="badge tier">${esc(c.tier)}</span></td>
        <td>${status}</td>
        <td>${c.mismatched_keys ? keyList(c.mismatched_keys) : "—"}</td>
        <td>${fmtDate(cs.sequencing_date)}</td>
        <td>${overrideBtn}</td>
      </tr>`;
  }).join("");
  $("#m-others-empty").classList.toggle("hidden", rows.length > 0);
  $("#m-others-empty").textContent =
    rows.length === 0 && matchesState.othersTotal > 0
      ? "No candidates on this page."
      : "No other candidates.";

  // Bounded candidate page: whole-scope summary + pager controls.
  const total = matchesState.othersTotal;
  const offset = matchesState.othersOffset;
  const parts = [];
  if (total > 0) parts.push(rows.length > 0
    ? `showing ${offset + 1}\u2013${offset + rows.length} of ${total} candidates`
    : `${total} candidates in scope, none on this page`);
  if (typeof data.other_matches_matched === "number") parts.push(`${data.other_matches_matched} pass every key`);
  const tierBits = Object.entries(data.other_matches_tiers || {})
    .map(([t, n]) => `${n} ${t.replace(/_/g, " ")}`)
    .join(", ");
  if (tierBits) parts.push(`tiers: ${tierBits}`);
  const breakdownBits = (data.other_matches_breakdown || [])
    .slice(0, 3)
    .map((b) => `${b.count} on ${b.mismatched_keys.join(" + ")}`)
    .join(", ");
  if (breakdownBits) parts.push(`mismatches: ${breakdownBits}`);
  $("#m-others-summary").textContent = parts.join(" \u00b7 ");
  $("#m-others-prev").disabled = offset <= 0;
  $("#m-others-next").disabled = offset + rows.length >= total;
  tbody.querySelectorAll("button[data-override]").forEach((btn) => {
    btn.addEventListener("click", () => submitOverride(btn.dataset.override));
  });
}

$("#m-search").addEventListener("click", searchMatchesSamples);
$("#m-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") searchMatchesSamples();
});
$("#m-show").addEventListener("click", () => showMatches());
$("#m-sample").addEventListener("change", async (e) => {
  const picked = matchesState.candidates.find((s) => s.id === e.target.value);
  if (!picked) return;
  matchesState.sample = picked;
  matchesState.othersOffset = 0;
  await applyBaitSetLogic(picked);
  showMatches(0);
});
$("#m-others-prev").addEventListener("click", () => {
  if (!matchesState.data) return;
  showMatches(Math.max(0, matchesState.othersOffset - OTHERS_PAGE_SIZE));
});
$("#m-others-next").addEventListener("click", () => {
  if (!matchesState.data) return;
  const next = matchesState.othersOffset + OTHERS_PAGE_SIZE;
  if (next < matchesState.othersTotal) showMatches(next);
});

/* Sample panel: click a patient__cmo / request_id value to go to its page. */
$("#m-summary").addEventListener("click", (e) => {
  const el = e.target.closest("[data-nav-patient],[data-nav-request]");
  if (!el) return;
  if (el.dataset.navPatient) navigateToPatient(el.dataset.navPatient);
  else if (el.dataset.navRequest) navigateToRequest(el.dataset.navRequest);
});

/* Current match(es): click the matched sample to open its page. */
$("#m-current").addEventListener("click", (e) => {
  const el = e.target.closest("[data-sample-id]");
  if (!el) return;
  const cmList = (matchesState.data && matchesState.data.current_matches) || [];
  const cm = cmList.find((c) => String(c.sample.id) === el.dataset.sampleId);
  if (cm) openMatchesFor(cm.sample);
});

/* Other potential matches: click a candidate row to open its page
   (but not the Override button). */
$("#m-others").addEventListener("click", (e) => {
  if (e.target.closest("button")) return;
  const tr = e.target.closest("tr[data-sample-id]");
  if (!tr) return;
  const cand = ((matchesState.data && matchesState.data.other_matches) || []).find(
    (c) => String(c.sample.id) === tr.dataset.sampleId
  );
  if (cand) openMatchesFor(cand.sample);
});

function submitOverride(normalId) {
  const data = matchesState.data;
  const strict = $("#m-strict").checked;
  const logic = $("#m-logic").value;
  if (isDmpLogic(logic)) {
    banner("error",
      "The DMP logic is automatic-only: use the “Run DMP pairing” button on " +
      "the Pairs tab, or switch to another logic (e.g. ARGOS) to pin a " +
      "DMP tumor to a non-DMP normal.", true);
    return;
  }
  const payload = {
    tumor_id: data.sample.id,
    normal_id: normalId,
    trigger_type: "override",
    pairing_logic_name: logic,
    strict: strict,
  };
  api("/pairing/trigger", { method: "POST", body: JSON.stringify(payload) })
    .then((p) => {
      const mism = p.mismatched_keys
        ? ` Mismatches recorded: ${p.mismatched_keys.join("; ")}`
        : " All matching keys agree.";
      banner("ok", `Override recorded: ${esc(p.tumor.primary_id)} ↔ ${esc(p.normal.primary_id)}.${mism}`);
      showMatches();
    })
    .catch((e) => banner("error", `Override failed: ${esc(e.message)}`, true));
}

/* ---------- patients view ---------- */

const patientsState = {
  results: [],
  pairings: null, // sample id -> {pairing logic name: [opposite samples]}
  patient: null,
  patientSamples: [],
};

async function loadPatients() {
  try {
    const q = $("#p-q").value.trim();
    const params = new URLSearchParams();
    params.set("limit", 100);
    if (q) params.set("q", q);
    patientsState.results = await api(`/patient/?${params}`);
    renderPatients();
    return patientsState.results;
  } catch (e) {
    banner("error", `Failed to load patients: ${esc(e.message)}`, true);
    return undefined;
  }
}

function renderPatients() {
  const rows = patientsState.results;
  const tbody = $("#patients-table tbody");
  tbody.innerHTML = rows.map((p) => {
    const meta = Object.entries(p.metadata)
      .map(([k, v]) => `${esc(k)}: ${metaText(v)}`).join(", ");
    return `
      <tr class="click" data-pid="${esc(p.patient__cmo)}">
        <td>${esc(p.patient__cmo)}</td>
        <td>${p.sample_count}</td>
        <td>${p.tumor_count}</td>
        <td>${p.normal_count}</td>
        <td>${esc((p.requests || []).join(", "))}</td>
        <td>${meta || "—"}</td>
      </tr>`;
  }).join("");
  $("#p-empty").classList.toggle("hidden", rows.length > 0);
  tbody.querySelectorAll("tr[data-pid]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const row = rows.find((p) => p.patient__cmo === tr.dataset.pid);
      if (row) openPatient(row);
    });
  });
}

/* Sample id -> {logic name: [opposite samples]}, from all latest pairings
   (one request). A tumor lists its current normal(s) (at most one); a
   normal lists every currently-paired tumor (1-to-many). */
async function loadPairingMap(filter = {}) {
  {
    // Scoped to the patient/request on screen and uncapped, so every
    // current match is returned (a global limit dropped older pairings).
    const params = new URLSearchParams({ latest: "true", ...filter });
    const rows = await api(`/pairing/?${params}`);
    const map = {};
    for (const r of rows) {
      const tSide = (map[r.tumor_id] = map[r.tumor_id] || {});
      (tSide[r.pairing_logic] = tSide[r.pairing_logic] || []).push(r.normal);
      const nSide = (map[r.normal_id] = map[r.normal_id] || {});
      (nSide[r.pairing_logic] = nSide[r.pairing_logic] || []).push(r.tumor);
    }
    patientsState.pairings = map;
  }
  return patientsState.pairings;
}

function currentMatchCell(sampleId) {
  const byLogic = (patientsState.pairings || {})[sampleId];
  if (!byLogic || !Object.keys(byLogic).length) return "—";
  return Object.entries(byLogic).flatMap(([logic, samples]) =>
    (samples || []).map((s) =>
      `<div>${esc(logic)}: ${esc(s.primary_id)} <span class="badge ${s.tumor_or_normal.toLowerCase()}">${esc(s.tumor_or_normal)}</span></div>`
    )
  ).join("");
}

async function openPatient(row) {
  patientsState.patient = row;
  $("#p-detail").classList.remove("hidden");
  pushNav();
  $("#p-detail-title").textContent = `Patient ${row.patient__cmo}`;
  $("#p-detail-meta").innerHTML =
    `<dt>samples</dt><dd>${row.sample_count} (${row.tumor_count} tumors, ${row.normal_count} normals)</dd>` +
    `<dt>requests</dt><dd>${esc((row.requests || []).join(", ") || "—")}</dd>` +
    Object.entries(row.metadata).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${metaText(v)}</dd>`).join("");

  const tbody = $("#p-samples tbody");
  tbody.innerHTML = "";
  $("#p-samples-empty").classList.remove("hidden");
  try {
    const [samples] = await Promise.all([
      api(`/sample/?patient__cmo=${encodeURIComponent(row.patient__cmo)}&limit=200`),
      loadPairingMap({ patient__cmo: row.patient__cmo }),
    ]);
    patientsState.patientSamples = samples;
    tbody.innerHTML = samples.map((s) => `
      <tr class="click" data-id="${s.id}">
        <td>${esc(s.primary_id)}</td>
        <td>${esc(s.cmo_sample_name)}</td>
        <td><span class="badge ${s.tumor_or_normal.toLowerCase()}">${esc(s.tumor_or_normal)}</span></td>
        <td>${esc(s.sample_type)}</td>
        <td>${esc(s.request_id || "")}</td>
        <td>${currentMatchCell(s.id)}</td>
      </tr>`).join("");
    $("#p-samples-empty").classList.toggle("hidden", samples.length > 0);
    tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
      tr.addEventListener("click", () => {
        const sample = patientsState.patientSamples.find((s) => s.id === tr.dataset.id);
        if (sample) openMatchesFor(sample);
      });
    });
    $("#p-detail").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    banner("error", `Failed to load patient samples: ${esc(e.message)}`, true);
  }
}

$("#p-search").addEventListener("click", loadPatients);
$("#p-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadPatients();
});
$("#p-clear").addEventListener("click", () => {
  $("#p-q").value = "";
  loadPatients();
});
$("#p-close").addEventListener("click", () => {
  closePatientDetail();
  pushNav();
});

/* ---------- requests view ---------- */

const requestsState = {
  results: [],
  request: null,
  requestSamples: [],
  requestPatients: [],
};

async function loadRequests() {
  try {
    const q = $("#r-q").value.trim();
    const params = new URLSearchParams();
    params.set("limit", 100);
    if (q) params.set("q", q);
    requestsState.results = await api(`/request/?${params}`);
    renderRequests();
    return requestsState.results;
  } catch (e) {
    banner("error", `Failed to load requests: ${esc(e.message)}`, true);
    return undefined;
  }
}

function renderRequests() {
  const rows = requestsState.results;
  const tbody = $("#requests-table tbody");
  tbody.innerHTML = rows.map((r) => `
    <tr class="click" data-rid="${esc(r.request_id)}">
      <td>${esc(r.request_id)}</td>
      <td>${r.sample_count}</td>
      <td>${r.tumor_count}</td>
      <td>${r.normal_count}</td>
      <td>${esc((r.patients || []).join(", "))}</td>
      <td>${esc((r.sample_types || []).join(", "))}</td>
    </tr>`).join("");
  $("#r-empty").classList.toggle("hidden", rows.length > 0);
  tbody.querySelectorAll("tr[data-rid]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const row = rows.find((r) => r.request_id === tr.dataset.rid);
      if (row) openRequest(row);
    });
  });
}

async function openRequest(row) {
  requestsState.request = row;
  $("#r-detail").classList.remove("hidden");
  pushNav();
  $("#r-detail-title").textContent = `Request ${row.request_id}`;
  $("#r-detail-meta").innerHTML =
    `<dt>samples</dt><dd>${row.sample_count} (${row.tumor_count} tumors, ${row.normal_count} normals)</dd>` +
    `<dt>patients</dt><dd>${esc((row.patients || []).join(", ") || "—")}</dd>` +
    `<dt>sample_types</dt><dd>${esc((row.sample_types || []).join(", ") || "—")}</dd>` +
    `<dt>gene_panels</dt><dd>${esc((row.gene_panels || []).join(", ") || "—")}</dd>`;

  const sbody = $("#r-samples tbody");
  const pbody = $("#r-patients tbody");
  sbody.innerHTML = "";
  pbody.innerHTML = "";
  $("#r-samples-empty").classList.remove("hidden");
  $("#r-patients-empty").classList.remove("hidden");
  try {
    const [fetchedSamples] = await Promise.all([
      api(`/sample/?request_id=${encodeURIComponent(row.request_id)}&limit=200`),
      loadPairingMap({ request_id: row.request_id }),
    ]);
    // /sample/ filters request_id as a substring match; keep only the
    // samples belonging to this exact request.
    const samples = (requestsState.requestSamples =
      fetchedSamples.filter((s) => s.request_id === row.request_id));

    // Per-patient counts within this request (non-null patient__cmo only).
    const byPatient = {};
    for (const s of samples) {
      if (!s.patient__cmo) continue;
      const g = (byPatient[s.patient__cmo] =
        byPatient[s.patient__cmo] || { patient__cmo: s.patient__cmo, count: 0, tumor: 0, normal: 0 });
      g.count += 1;
      if (s.tumor_or_normal === "TUMOR") g.tumor += 1;
      else if (s.tumor_or_normal === "NORMAL") g.normal += 1;
    }
    requestsState.requestPatients = Object.keys(byPatient).sort().map((pid) => byPatient[pid]);

    sbody.innerHTML = samples.map((s) => `
      <tr class="click" data-id="${s.id}">
        <td>${esc(s.primary_id)}</td>
        <td>${esc(s.cmo_sample_name)}</td>
        <td><span class="badge ${s.tumor_or_normal.toLowerCase()}">${esc(s.tumor_or_normal)}</span></td>
        <td>${esc(s.sample_type)}</td>
        <td>${esc(s.patient__cmo || "")}</td>
        <td>${currentMatchCell(s.id)}</td>
      </tr>`).join("");
    $("#r-samples-empty").classList.toggle("hidden", samples.length > 0);
    sbody.querySelectorAll("tr[data-id]").forEach((tr) => {
      tr.addEventListener("click", () => {
        const sample = requestsState.requestSamples.find((s) => s.id === tr.dataset.id);
        if (sample) openMatchesFor(sample);
      });
    });

    pbody.innerHTML = requestsState.requestPatients.map((p) => `
      <tr class="click" data-pid="${esc(p.patient__cmo)}">
        <td>${esc(p.patient__cmo)}</td>
        <td>${p.count}</td>
        <td>${p.tumor}</td>
        <td>${p.normal}</td>
      </tr>`).join("");
    $("#r-patients-empty").classList.toggle("hidden", requestsState.requestPatients.length > 0);
    pbody.querySelectorAll("tr[data-pid]").forEach((tr) => {
      tr.addEventListener("click", () => openPatientFromRequest(tr.dataset.pid));
    });

    $("#r-detail").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    banner("error", `Failed to load request samples: ${esc(e.message)}`, true);
  }
}

/* Switch to the Patients tab and open the given patient's detail. */
async function openPatientFromRequest(patientId) {
  showView("patients");
  try {
    const rows = await api(`/patient/?q=${encodeURIComponent(patientId)}&limit=200`);
    const row = rows.find((p) => p.patient__cmo === patientId);
    if (row) openPatient(row);
    else banner("error", `Patient ${esc(patientId)} not found.`, true);
  } catch (e) {
    banner("error", `Failed to open patient: ${esc(e.message)}`, true);
  }
}

$("#r-search").addEventListener("click", loadRequests);
$("#r-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadRequests();
});
$("#r-clear").addEventListener("click", () => {
  $("#r-q").value = "";
  loadRequests();
});
$("#r-close").addEventListener("click", () => {
  closeRequestDetail();
  pushNav();
});

/* ---------- pooled normals view ---------- */

const POOLED_SAMPLE_TYPE = "Pooled Normal";
const pooledState = { limit: 50, offset: 0, results: [] };

const POOLED_FILTER_IDS = ["gene_panel", "bait_set", "run_mode", "file_group"];

function pooledParams(offset) {
  const p = new URLSearchParams();
  p.set("limit", pooledState.limit);
  p.set("offset", offset);
  p.set("tumor_or_normal", "NORMAL");
  p.set("sample_type", POOLED_SAMPLE_TYPE);
  p.set("no_patient", "true");
  const q = $("#pn-q").value.trim();
  if (q) p.set("q", q);
  for (const id of POOLED_FILTER_IDS) {
    const v = $(`#pn-${id}`).value.trim();
    if (v) p.set(id, v);
  }
  return p.toString();
}

async function loadPooled(offset) {
  try {
    const results = await api(`/sample/?${pooledParams(offset)}`);
    pooledState.results = results;
    pooledState.offset = offset;
    renderPooled();
  } catch (e) {
    banner("error", `Failed to load pooled normals: ${esc(e.message)}`, true);
  }
}

function renderPooled() {
  const rows = pooledState.results;
  const tbody = $("#pooled-table tbody");
  tbody.innerHTML = rows.map((s) => `
    <tr class="click" data-id="${s.id}">
      <td>${esc(s.cmo_sample_name)}</td>
      <td>${esc(s.primary_id)}</td>
      <td>${esc(s.request_id || "")}</td>
      <td>${esc(s.sample_type)}</td>
      <td>${esc(s.gene_panel)}</td>
      <td>${esc(s.bait_set || "")}</td>
      <td>${esc(s.run_mode || "")}</td>
      <td>${esc(s.file_group || "")}</td>
      <td>${metaText(s.preservation)}</td>
      <td>${fmtDate(s.sequencing_date)}</td>
    </tr>`).join("");
  $("#pn-empty").classList.toggle("hidden", rows.length > 0);
  $("#pn-count").textContent = rows.length ? `${rows.length} on this page` : "";
  $("#pn-prev").disabled = pooledState.offset === 0;
  $("#pn-next").disabled = rows.length < pooledState.limit;
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const sample = rows.find((s) => s.id === tr.dataset.id);
      if (sample) openMatchesFor(sample);
    });
  });
}

$("#pn-search").addEventListener("click", () => loadPooled(0));
$("#pn-clear").addEventListener("click", () => {
  $("#pn-q").value = "";
  for (const id of POOLED_FILTER_IDS) $(`#pn-${id}`).value = "";
  loadPooled(0);
});
$("#pn-limit").addEventListener("change", (e) => {
  pooledState.limit = Number(e.target.value);
  loadPooled(0);
});
$("#pn-prev").addEventListener("click", () => loadPooled(Math.max(0, pooledState.offset - pooledState.limit)));
$("#pn-next").addEventListener("click", () => loadPooled(pooledState.offset + pooledState.limit));
for (const el of ["#pn-q", ...POOLED_FILTER_IDS.map((id) => `#pn-${id}`)]) {
  $(el).addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadPooled(0);
  });
}

/* ---------- pooled machines view ---------- */

const machinesState = { results: [], editing: null };

async function loadMachines() {
  try {
    machinesState.results = await api("/machines/");
    renderMachines();
  } catch (e) {
    banner("error", `Failed to load machines: ${esc(e.message)}`, true);
  }
}

function renderMachines() {
  const rows = machinesState.results;
  const tbody = $("#pm-table tbody");
  tbody.innerHTML = rows.map((m) => {
    if (machinesState.editing === m.id) {
      return `
        <tr>
          <td>${esc(m.machine_name)}</td>
          <td><input id="pme-class" value="${esc(m.machine_class || "")}"></td>
          <td><input id="pme-type" value="${esc(m.machine_type || "")}"></td>
          <td>${fmtDate(m.modified_date)}</td>
          <td>
            <button class="small primary" data-save-machine="${esc(m.id)}">Save</button>
            <button class="small" data-cancel-machine="${esc(m.id)}">Cancel</button>
          </td>
        </tr>`;
    }
    return `
      <tr>
        <td>${esc(m.machine_name)}</td>
        <td>${esc(m.machine_class)}</td>
        <td>${esc(m.machine_type || "")}</td>
        <td>${fmtDate(m.modified_date)}</td>
        <td><button class="small" data-edit-machine="${esc(m.id)}">Edit</button></td>
      </tr>`;
  }).join("");
  $("#pm-empty").classList.toggle("hidden", rows.length > 0);
  tbody.querySelectorAll("button[data-edit-machine]").forEach((b) =>
    b.addEventListener("click", () => {
      machinesState.editing = b.dataset.editMachine;
      renderMachines();
    })
  );
  tbody.querySelectorAll("button[data-cancel-machine]").forEach((b) =>
    b.addEventListener("click", () => {
      machinesState.editing = null;
      renderMachines();
    })
  );
  tbody.querySelectorAll("button[data-save-machine]").forEach((b) =>
    b.addEventListener("click", () => saveMachine(b.dataset.saveMachine))
  );
}

function saveMachine(id) {
  const row = machinesState.results.find((m) => m.id === id);
  if (!row) return;
  const payload = {
    machine_name: row.machine_name,
    machine_class: $("#pme-class").value.trim(),
    machine_type: $("#pme-type").value.trim() || null,
  };
  api("/machines/", { method: "POST", body: JSON.stringify(payload) })
    .then(() => {
      machinesState.editing = null;
      banner("ok", `Machine ${esc(payload.machine_name)} saved.`);
      return loadMachines();
    })
    .catch((e) => banner("error", `Save failed: ${esc(e.message)}`, true));
}

function addMachine() {
  const payload = {
    machine_name: $("#pm-name").value.trim(),
    machine_class: $("#pm-class").value.trim(),
    machine_type: $("#pm-type").value.trim() || null,
  };
  if (!payload.machine_name || !payload.machine_class) {
    banner("error", "machine_name and machine_class are required.", true);
    return;
  }
  api("/machines/", { method: "POST", body: JSON.stringify(payload) })
    .then((m) => {
      $("#pm-name").value = "";
      $("#pm-class").value = "";
      $("#pm-type").value = "";
      banner("ok", `Machine ${esc(m.machine_name)} saved.`);
      loadMachines();
    })
    .catch((e) => banner("error", `Add failed: ${esc(e.message)}`, true));
}

$("#pm-add").addEventListener("click", addMachine);
for (const id of ["#pm-name", "#pm-class", "#pm-type"]) {
  $(id).addEventListener("keydown", (e) => {
    if (e.key === "Enter") addMachine();
  });
}

/* ---------- pairs-by-logic view (selectable columns) ---------- */

/* Plain-text renderer shared by the on-screen cells and the CSV/TSV
 * export: nulls become empty strings, JSON lists are joined, objects are
 * compacted to JSON. */
function pairText(v) {
  if (v === null || v === undefined || v === "") return "";
  if (Array.isArray(v)) return v.map(pairText).join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function pairDate(v) {
  if (!v) return "";
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? String(v) : d.toISOString().slice(0, 16).replace("T", " ");
}

/* Every column the pairs table can show/export, in canonical order (that
 * order drives the table and the export). The first three are the original
 * columns and form the default selection, so the default view and export
 * are unchanged. ``text`` maps a PairingRead row to the column's value. */
const PAIR_COLUMNS = [
  { key: "tumor_primary_id", header: "TUMOR_SAMPLE", group: "Tumor sample", text: (r) => r.tumor.primary_id },
  { key: "normal_primary_id", header: "NORMAL_SAMPLE", group: "Normal sample", text: (r) => r.normal.primary_id },
  { key: "tumor_bait_set", header: "BAIT_SET", group: "Tumor sample", text: (r) => r.tumor.bait_set || "" },
  { key: "tumor_patient__cmo", header: "TUMOR_PATIENT__CMO", group: "Tumor sample", text: (r) => r.tumor.patient__cmo || "" },
  { key: "tumor_patient__dmp", header: "TUMOR_PATIENT__DMP", group: "Tumor sample", text: (r) => r.tumor.patient__dmp || "" },
  { key: "tumor_anon_id", header: "TUMOR_ANON_ID", group: "Tumor sample", text: (r) => r.tumor.anon_id || "" },
  { key: "tumor_cmo_sample_name", header: "TUMOR_CMO_SAMPLE_NAME", group: "Tumor sample", text: (r) => pairText(r.tumor.cmo_sample_name) },
  { key: "tumor_role", header: "TUMOR_ROLE", group: "Tumor sample", text: (r) => r.tumor.tumor_or_normal || "" },
  { key: "tumor_sample_type", header: "TUMOR_SAMPLE_TYPE", group: "Tumor sample", text: (r) => r.tumor.sample_type || "" },
  { key: "tumor_gene_panel", header: "TUMOR_GENE_PANEL", group: "Tumor sample", text: (r) => r.tumor.gene_panel || "" },
  { key: "tumor_request_id", header: "TUMOR_REQUEST_ID", group: "Tumor sample", text: (r) => r.tumor.request_id || "" },
  { key: "tumor_run_mode", header: "TUMOR_RUN_MODE", group: "Tumor sample", text: (r) => r.tumor.run_mode || "" },
  { key: "tumor_file_group", header: "TUMOR_FILE_GROUP", group: "Tumor sample", text: (r) => r.tumor.file_group || "" },
  { key: "tumor_preservation", header: "TUMOR_PRESERVATION", group: "Tumor sample", text: (r) => pairText(r.tumor.preservation) },
  { key: "tumor_sample_origin", header: "TUMOR_SAMPLE_ORIGIN", group: "Tumor sample", text: (r) => pairText(r.tumor.sample_origin) },
  { key: "tumor_sequencing_date", header: "TUMOR_SEQUENCING_DATE", group: "Tumor sample", text: (r) => pairDate(r.tumor.sequencing_date) },
  { key: "tumor_additional_metadata", header: "TUMOR_ADDITIONAL_METADATA", group: "Tumor sample", text: (r) => pairText(r.tumor.additional_metadata) },
  { key: "tumor_errors", header: "TUMOR_ERRORS", group: "Tumor sample", text: (r) => (r.tumor.errors || []).join("; ") },
  { key: "tumor_id", header: "TUMOR_ID", group: "Tumor sample", text: (r) => r.tumor.id },
  { key: "normal_bait_set", header: "NORMAL_BAIT_SET", group: "Normal sample", text: (r) => r.normal.bait_set || "" },
  { key: "normal_patient__cmo", header: "NORMAL_PATIENT__CMO", group: "Normal sample", text: (r) => r.normal.patient__cmo || "" },
  { key: "normal_patient__dmp", header: "NORMAL_PATIENT__DMP", group: "Normal sample", text: (r) => r.normal.patient__dmp || "" },
  { key: "normal_anon_id", header: "NORMAL_ANON_ID", group: "Normal sample", text: (r) => r.normal.anon_id || "" },
  { key: "normal_cmo_sample_name", header: "NORMAL_CMO_SAMPLE_NAME", group: "Normal sample", text: (r) => pairText(r.normal.cmo_sample_name) },
  { key: "normal_role", header: "NORMAL_ROLE", group: "Normal sample", text: (r) => r.normal.tumor_or_normal || "" },
  { key: "normal_sample_type", header: "NORMAL_SAMPLE_TYPE", group: "Normal sample", text: (r) => r.normal.sample_type || "" },
  { key: "normal_gene_panel", header: "NORMAL_GENE_PANEL", group: "Normal sample", text: (r) => r.normal.gene_panel || "" },
  { key: "normal_request_id", header: "NORMAL_REQUEST_ID", group: "Normal sample", text: (r) => r.normal.request_id || "" },
  { key: "normal_run_mode", header: "NORMAL_RUN_MODE", group: "Normal sample", text: (r) => r.normal.run_mode || "" },
  { key: "normal_file_group", header: "NORMAL_FILE_GROUP", group: "Normal sample", text: (r) => r.normal.file_group || "" },
  { key: "normal_preservation", header: "NORMAL_PRESERVATION", group: "Normal sample", text: (r) => pairText(r.normal.preservation) },
  { key: "normal_sample_origin", header: "NORMAL_SAMPLE_ORIGIN", group: "Normal sample", text: (r) => pairText(r.normal.sample_origin) },
  { key: "normal_sequencing_date", header: "NORMAL_SEQUENCING_DATE", group: "Normal sample", text: (r) => pairDate(r.normal.sequencing_date) },
  { key: "normal_additional_metadata", header: "NORMAL_ADDITIONAL_METADATA", group: "Normal sample", text: (r) => pairText(r.normal.additional_metadata) },
  { key: "normal_errors", header: "NORMAL_ERRORS", group: "Normal sample", text: (r) => (r.normal.errors || []).join("; ") },
  { key: "normal_id", header: "NORMAL_ID", group: "Normal sample", text: (r) => r.normal.id },
  { key: "pairing_logic", header: "PAIRING_LOGIC", group: "Pairing", text: (r) => r.pairing_logic },
  { key: "trigger_type", header: "TRIGGER_TYPE", group: "Pairing", text: (r) => r.trigger_type || "" },
  { key: "mismatched_keys", header: "MISMATCHED_KEYS", group: "Pairing", text: (r) => (r.mismatched_keys || []).join("; ") },
  { key: "created_date", header: "CREATED_DATE", group: "Pairing", text: (r) => pairDate(r.created_date) },
];
const PAIR_DEFAULT_KEYS = ["tumor_primary_id", "normal_primary_id", "tumor_bait_set"];
const PAIR_GROUPS = ["Tumor sample", "Normal sample", "Pairing"];

/* Legacy DMP export columns: the exact headers and order of the beagle DMP
 * filemetadata export, no additions or removals. Values come from the DMP
 * sample record (primary_id / patient__dmp / anon_id) plus its
 * additional_metadata (coverage, project_name). */
const DMP_PLACEHOLDER = "\u2014"; // "—" — columns whose source is not available yet
function dmpMetaText(sample, key) {
  const meta = (sample && typeof sample.additional_metadata === "object" && sample.additional_metadata) || {};
  const v = meta[key];
  return v === null || v === undefined ? "" : String(v);
}
/* Second-to-last / last dash-separated segments of a DMP sample name,
 * e.g. "P-0000001-T01-IM7" -> ["T01", "IM7"]; blank when unparseable. */
function dmpNameParts(name) {
  const parts = String(name || "").split("-");
  if (parts.length < 2) return ["", ""];
  return [parts[parts.length - 2], parts[parts.length - 1]];
}
const DMP_COLUMNS = [
  { key: "tumor_sample", header: "tumor_sample", group: "Tumor", text: (r) => (r.tumor ? r.tumor.primary_id : "") },
  { key: "patient", header: "patient", group: "Tumor", text: (r) => (r.tumor ? r.tumor.patient__dmp || "" : "") },
  { key: "tumor_idx", header: "tumor_idx", group: "Tumor", text: (r) => (r.tumor ? dmpNameParts(r.tumor.primary_id)[0] : "") },
  { key: "tumor_IM_ver", header: "tumor_IM_ver", group: "Tumor", text: (r) => (r.tumor ? dmpNameParts(r.tumor.primary_id)[1] : "") },
  { key: "tumor_bamname", header: "tumor_bamname", group: "Tumor", text: (r) => (r.tumor ? r.tumor.anon_id || "" : "") },
  { key: "tumor_group", header: "tumor_group", group: "Tumor", text: () => DMP_PLACEHOLDER },
  { key: "tumor_matched", header: "tumor_matched", group: "Tumor", text: (r) => (r.tumor ? (r.normal ? "Matched" : "Unmatched") : "") },
  { key: "tumor_bam_exists", header: "tumor_bam_exists", group: "Tumor", text: () => DMP_PLACEHOLDER },
  { key: "tumor_bam_coverage", header: "tumor_bam_coverage", group: "Tumor", text: (r) => dmpMetaText(r.tumor, "coverage") },
  { key: "normal_sample", header: "normal_sample", group: "Normal", text: (r) => (r.normal ? r.normal.primary_id : "") },
  { key: "normal_bamname", header: "normal_bamname", group: "Normal", text: (r) => (r.normal ? (r.normal.anon_id || "") : "") },
  { key: "normal_IM_ver", header: "normal_IM_ver", group: "Normal", text: (r) => (r.normal ? dmpNameParts(r.normal.primary_id)[1] : "") },
  { key: "pool_id", header: "pool_id", group: "Tumor", text: (r) => dmpMetaText(r.tumor, "project_name") },
  { key: "normal_bam_exists", header: "normal_bam_exists", group: "Normal", text: () => DMP_PLACEHOLDER },
  { key: "normal_bam_coverage", header: "normal_bam_coverage", group: "Normal", text: (r) => dmpMetaText(r.normal, "coverage") },
];
/* Where each legacy DMP column comes from (shown in the "About these
 * columns" panel and as tooltips on the column headers). */
const DMP_LEGEND = {
  tumor_sample: "DMP sample name (primary_id), e.g. P-0000001-T01-IM7",
  patient: "patient__dmp: the DMP \u201cP-\u201d patient id",
  tumor_idx: "Sample name, second-to-last dash segment (e.g. T01); blank when unparseable",
  tumor_IM_ver: "Sample name, last dash segment (e.g. IM7); blank when unparseable",
  tumor_bamname: "anon_id: the DMP anonymous sample id",
  tumor_group: "Placeholder: source not yet available",
  tumor_matched: "\u201cMatched\u201d when a normal is paired to this DMP sample, else \u201cUnmatched\u201d",
  tumor_bam_exists: "Placeholder: source not yet available",
  tumor_bam_coverage: "coverage from the DMP record's additional_metadata",
  normal_sample: "Paired normal's sample name (primary_id)",
  normal_bamname: "Paired normal's anon_id",
  normal_IM_ver: "Paired normal's name, last dash segment",
  pool_id: "project_name from the tumor-side DMP record's additional_metadata",
  normal_bam_exists: "Placeholder: source not yet available",
  normal_bam_coverage: "coverage from the paired normal's additional_metadata",
};
const DMP_DEFAULT_KEYS = DMP_COLUMNS.map((c) => c.key);

const pairsState = {
  rows: [],
  logic: null,
  // Per-logic selected column keys (each logic has its own column set and
  // its own selection, so ARGOS choices never affect DMP and vice versa).
  selected: {},
  // Split-panel filter groups that the user collapsed (default: expanded).
  groupCollapsed: {},
  // Display pagination for the pair table. The full row set can be in the
  // hundreds of thousands (the DMP report lists every DMP sample); rendering
  // all of it to the DOM hangs the tab, so only one page is rendered at a
  // time. The download still covers every row, not just the visible page.
  page: 0,
  pageSize: 50,
};

function isDmpLogic(logicName) {
  const l = PAIRING_LOGICS.find((x) => x.name === logicName);
  return !!logicName && (l ? l.strategy_name === "dmp" || logicName.toUpperCase() === "DMP" : logicName.toUpperCase() === "DMP");
}
function columnsForLogic(logicName) {
  return isDmpLogic(logicName) ? DMP_COLUMNS : PAIR_COLUMNS;
}
function defaultSelectionFor(logicName) {
  return new Set(isDmpLogic(logicName) ? DMP_DEFAULT_KEYS : PAIR_DEFAULT_KEYS);
}
function selectionFor(logicName) {
  if (!logicName) return new Set();
  if (!pairsState.selected[logicName]) pairsState.selected[logicName] = defaultSelectionFor(logicName);
  return pairsState.selected[logicName];
}
function activeLogicName() {
  return pairsState.logic || $("#pr-logic").value || null;
}
function selectedPairColumns() {
  const logic = activeLogicName();
  if (!logic) return [];
  return columnsForLogic(logic).filter((c) => selectionFor(logic).has(c.key));
}

/* Groups for the left split-panel filter.
 *  - "side": the active logic's columns, grouped by side (Tumor / Normal /
 *    Pairing for the standard set; Tumor / Normal for DMP).
 *  - "logic": one group per pairing logic (ARGOS, TEMPO, ACCESS, DMP, ...),
 *    each carrying that logic's full column set. */
function pairReportGroups() {
  const mode = $("#pr-groupby").value;
  if (mode === "logic") {
    return PAIRING_LOGICS.map((l) => ({
      key: `logic:${l.name}`,
      label: l.name,
      logic: l.name,
      columns: columnsForLogic(l.name),
    }));
  }
  const logic = activeLogicName();
  if (!logic) return [];
  const bySide = new Map();
  for (const c of columnsForLogic(logic)) {
    if (!bySide.has(c.group)) bySide.set(c.group, []);
    bySide.get(c.group).push(c);
  }
  return [...bySide.entries()].map(([label, columns]) => ({
    key: `side:${logic}:${label}`,
    label,
    logic,
    columns,
  }));
}

/* Render the split-panel: one collapsible group per entry of
 * pairReportGroups(); the group header shows its item count and toggles
 * every column in the group (indeterminate when partially checked). */
function renderPrGroups() {
  const groups = pairReportGroups();
  $("#pr-groups").innerHTML = groups.map((g) => {
    const sel = selectionFor(g.logic);
    const collapsed = !!pairsState.groupCollapsed[g.key];
    const allChecked = g.columns.every((c) => sel.has(c.key));
    return `<div class="pr-group">
      <div class="pr-group-head">
        <label class="pr-group-label">
          <input type="checkbox" data-prgroup="${esc(g.key)}" data-prlogic="${esc(g.logic)}"${allChecked ? " checked" : ""}>
          <span class="pr-group-name">${esc(g.label)}</span>
          <span class="pr-group-count" title="columns in this group">${g.columns.length}</span>
        </label>
        <button type="button" class="linkbtn" data-prcaret="${esc(g.key)}" title="Expand / collapse this group">${collapsed ? "\u25B8" : "\u25BE"}</button>
      </div>
      <div class="pr-group-body${collapsed ? " hidden" : ""}">
        ${g.columns.map((c) =>
          `<label class="col-check"><input type="checkbox" data-prcol="${esc(c.key)}" data-prlogic="${esc(g.logic)}"${sel.has(c.key) ? " checked" : ""}>${esc(c.header)}</label>`
        ).join("")}
      </div>
    </div>`;
  }).join("");
  for (const g of groups) {
    const sel = selectionFor(g.logic);
    const all = g.columns.every((c) => sel.has(c.key));
    const some = g.columns.some((c) => sel.has(c.key));
    const head = $(`#pr-groups input[data-prgroup="${g.key}"]`);
    if (head) head.indeterminate = some && !all;
  }
}

function renderPairsTable() {
  const logic = activeLogicName();
  const dmp = isDmpLogic(logic);
  const cols = selectedPairColumns();
  $("#pairs-table thead").innerHTML = `<tr>${cols.map((c) =>
    `<th${dmp && DMP_LEGEND[c.key] ? ` title="${esc(DMP_LEGEND[c.key])}"` : ""}>${esc(c.header)}</th>`
  ).join("")}</tr>`;
  // Render only the current page (see pairsState.pageSize). The row array
  // keeps the full set in memory for the download.
  const total = pairsState.rows.length;
  const maxPage = Math.max(0, Math.ceil(total / pairsState.pageSize) - 1);
  if (pairsState.page > maxPage) pairsState.page = maxPage;
  const start = pairsState.page * pairsState.pageSize;
  const slice = pairsState.rows.slice(start, start + pairsState.pageSize);
  $("#pairs-table tbody").innerHTML = slice
    .map((r) => `<tr>${cols.map((c) => `<td>${esc(c.text(r))}</td>`).join("")}</tr>`).join("");
  $("#pr-prev").disabled = pairsState.page === 0;
  $("#pr-next").disabled = pairsState.page >= maxPage;
  const matched = pairsState.rows.filter((r) => r.tumor && r.normal).length;
  const empty = total === 0 || cols.length === 0;
  $("#pr-empty").classList.toggle("hidden", !empty);
  const paging = total > pairsState.pageSize
    ? ` (page ${pairsState.page + 1}/${maxPage + 1}: showing ${start + 1}\u2013${start + slice.length} of ${total})`
    : "";
  $("#pr-count").textContent = cols.length === 0
    ? "no columns selected"
    : dmp
      ? `${total} rows: ${matched} matched, ${total - matched} unmatched${paging}`
      : `${total} pair${total === 1 ? "" : "s"}${paging}`;
  $("#pr-download").disabled = empty;
  // Legend (and header tooltips) only for the legacy DMP column set.
  $("#pr-legend").classList.toggle("hidden", !dmp);
  if (dmp) {
    $("#pr-legend-body").innerHTML = DMP_COLUMNS.map((c) =>
      `<tr><td>${esc(c.header)}</td><td>${esc(DMP_LEGEND[c.key] || "")}</td></tr>`
    ).join("");
  }
}

/* Abort controller for the in-flight pair load; a new load (tab click,
 * logic change) cancels the previous one so scans can't pile up on the
 * server. */
let pairsAbort = null;

async function loadPairs() {
  const logic = $("#pr-logic").value;
  if (!logic) return;
  if (pairsAbort) pairsAbort.abort();
  pairsAbort = new AbortController();
  const signal = pairsAbort.signal;
  try {
    pairsState.logic = logic;
    pairsState.rows = await api(`/pairing/?${new URLSearchParams({ latest: "true", pairing_logic_name: logic })}`, { signal });
    if (isDmpLogic(logic)) await appendUnmatchedDmpSamples(signal);
    pairsState.page = 0;
    renderPrGroups();
    renderPairsTable();
  } catch (e) {
    if (e && e.name === "AbortError") return; // superseded by a newer load
    banner("error", `Failed to load pairs: ${esc(e.message)}`, true);
  }
}

/* DMP report: the legacy export lists every DMP sample, so DMP samples
 * without a pairing yet are appended as rows with the opposite-side
 * columns left empty. The unmatched set is computed server-side (one
 * query) — the old client-side diff had to download the entire DMP
 * sample population in pages of 1000. */
async function appendUnmatchedDmpSamples(signal) {
  const unmatched = await api(
    `/pairing/unmatched/?${new URLSearchParams({ pairing_logic_name: pairsState.logic })}`,
    { signal }
  );
  for (const s of unmatched) {
    pairsState.rows.push({
      tumor: s.tumor_or_normal === "TUMOR" ? s : null,
      normal: s.tumor_or_normal === "NORMAL" ? s : null,
      pairing_logic: pairsState.logic,
      trigger_type: null,
      mismatched_keys: [],
      created_date: s.created_date,
    });
  }
}

function downloadPairs() {
  if (!pairsState.logic) return;
  const cols = selectedPairColumns();
  if (cols.length === 0 || pairsState.rows.length === 0) return;
  const tsv = $("#pr-format").value === "tsv";
  const sep = tsv ? "\t" : ",";
  const cell = (v) => (!tsv && /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v.replace(/[\t\r\n]/g, " "));
  const rows = [cols.map((c) => c.header), ...pairsState.rows.map((r) => cols.map((c) => c.text(r)))];
  const text = rows.map((r) => r.map(cell).join(sep)).join("\n") + "\n";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: tsv ? "text/tab-separated-values" : "text/csv" }));
  a.download = `pairs_${pairsState.logic.replace(/[^\w.-]+/g, "_")}.${tsv ? "tsv" : "csv"}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}

/* Automatic DMP pairing: the DMP strategy pairs every unpaired DMP tumor
 * with its DMP normal (same patient__dmp and gene panel). The button is
 * shown only for the active DMP logic; manual pairing of DMP samples
 * happens under the other logics (e.g. ARGOS). */
async function runDmpPairing() {
  const btn = $("#pr-run-dmp");
  btn.disabled = true;
  $("#pr-dmp-status").textContent = "Pairing…";
  try {
    const r = await api("/pairing/trigger-dmp", { method: "POST" });
    $("#pr-dmp-status").textContent =
      `paired ${r.paired} · unpaired ${r.unpaired} · ` +
      `missing patient__dmp ${r.missing_patient__dmp} · already ${r.already_paired}`;
    banner("ok", `DMP pairing complete: ${r.paired} paired, ${r.unpaired} unpaired, ` +
      `${r.missing_patient__dmp} missing patient__dmp`, true);
    await loadPairs();
  } catch (e) {
    banner("error", `DMP pairing failed: ${esc(e.message)}`, true);
  } finally {
    btn.disabled = false;
  }
}

/* Show/hide the DMP trigger button per selected logic (DMP logic only). */
function updateDmpTriggerButton() {
  const logic = $("#pr-logic").value;
  const l = PAIRING_LOGICS.find((x) => x.name === logic);
  const show = isDmpLogic(logic) && !!l && !!l.active;
  $("#pr-run-dmp").classList.toggle("hidden", !show);
  if (!show) $("#pr-dmp-status").textContent = "";
}

$("#pr-logic").addEventListener("change", () => {
  updateDmpTriggerButton();
  loadPairs();
});
$("#pr-run-dmp").addEventListener("click", runDmpPairing);
$("#pr-download").addEventListener("click", downloadPairs);
$("#pr-prev").addEventListener("click", () => {
  if (pairsState.page > 0) {
    pairsState.page--;
    renderPairsTable();
  }
});
$("#pr-next").addEventListener("click", () => {
  const maxPage = Math.max(0, Math.ceil(pairsState.rows.length / pairsState.pageSize) - 1);
  if (pairsState.page < maxPage) {
    pairsState.page++;
    renderPairsTable();
  }
});
document.querySelector('.tab[data-tab="pairs"]').addEventListener("click", loadPairs);
$("#pr-groupby").addEventListener("change", renderPrGroups);
$("#pr-groups").addEventListener("change", (e) => {
  const t = e.target;
  if (!t.dataset) return;
  if (t.dataset.prcol) {
    const sel = selectionFor(t.dataset.prlogic);
    if (t.checked) sel.add(t.dataset.prcol);
    else sel.delete(t.dataset.prcol);
  } else if (t.dataset.prgroup) {
    const g = pairReportGroups().find((x) => x.key === t.dataset.prgroup);
    if (!g) return;
    const sel = selectionFor(g.logic);
    for (const c of g.columns) {
      if (t.checked) sel.add(c.key);
      else sel.delete(c.key);
    }
  } else {
    return;
  }
  renderPairsTable();
  renderPrGroups();
});
$("#pr-groups").addEventListener("click", (e) => {
  const btn = e.target.closest ? e.target.closest("[data-prcaret]") : null;
  if (!btn) return;
  const k = btn.dataset.prcaret;
  pairsState.groupCollapsed[k] = !pairsState.groupCollapsed[k];
  const group = btn.closest(".pr-group");
  const body = group && group.querySelector(".pr-group-body");
  if (body) body.classList.toggle("hidden", pairsState.groupCollapsed[k]);
  btn.textContent = pairsState.groupCollapsed[k] ? "\u25B8" : "\u25BE";
});
$("#pr-panel-collapse").addEventListener("click", () => {
  $("#pr-panel").classList.add("collapsed");
  $("#pr-panel-open").classList.remove("hidden");
});
$("#pr-panel-open").addEventListener("click", () => {
  $("#pr-panel").classList.remove("collapsed");
  $("#pr-panel-open").classList.add("hidden");
});
function setSelectionForActive(keys) {
  const logic = activeLogicName();
  if (!logic) return;
  const sel = selectionFor(logic);
  sel.clear();
  for (const k of keys) sel.add(k);
  renderPrGroups();
  renderPairsTable();
}
$("#pr-cols-all").addEventListener("click", () =>
  setSelectionForActive(columnsForLogic(activeLogicName()).map((c) => c.key)));
$("#pr-cols-none").addEventListener("click", () => setSelectionForActive([]));
$("#pr-cols-default").addEventListener("click", () =>
  setSelectionForActive([...defaultSelectionFor(activeLogicName())]));
renderPrGroups();

/* ---------- try pairing view (dry run) ---------- */

/* Read-only preview: resolve a sample by primary_id / cmo_sample_name,
 * pick a pairing logic, and show what the pairing cascade *would* do.
 * Only GET endpoints are used (sample lookup + possible-matches) — no
 * pairing row is ever created, updated, or overridden. */

const tryState = { sample: null, candidates: [], busy: false };

function tryFillSelect(rows) {
  tryState.candidates = rows;
  const sel = $("#tq-sample");
  sel.innerHTML = rows.map((s) =>
    `<option value="${s.id}">${esc(sampleLabel(s))}</option>`
  ).join("");
  sel.disabled = rows.length === 0;
  $("#tq-run").disabled = rows.length === 0;
}

async function trySearch() {
  const q = $("#tq-id").value.trim();
  if (!q || tryState.busy) return;
  tryState.busy = true;
  $("#tq-lookup").disabled = true;
  try {
    const rows = await api(`/sample/lookup?q=${encodeURIComponent(q)}&limit=25`);
    if (!rows.length) {
      tryState.sample = null;
      tryFillSelect([]);
      $("#tq-summary").classList.add("hidden");
      $("#tq-result").classList.add("hidden");
      banner("info", `No sample matches "${esc(q)}" (primary_id / cmo_sample_name).`);
      return;
    }
    tryFillSelect(rows);
    const first = rows[0];
    $("#tq-sample").value = first.id;
    await selectTrySample(first);
  } catch (e) {
    banner("error", `Sample lookup failed: ${esc(e.message)}`, true);
  } finally {
    tryState.busy = false;
    $("#tq-lookup").disabled = false;
  }
}

async function selectTrySample(sample) {
  tryState.sample = sample;
  $("#tq-run").disabled = false;
  const kv = [
    ["primary_id", esc(sample.primary_id)],
    ["cmo_sample_name", esc(sample.cmo_sample_name)],
    ["patient__cmo", sample.patient__cmo ? esc(sample.patient__cmo) : "—"],
    ["patient__dmp", sample.patient__dmp ? esc(sample.patient__dmp) : "—"],
    ["anon_id", sample.anon_id ? esc(sample.anon_id) : "—"],
    ["request_id", sample.request_id ? esc(sample.request_id) : "—"],
    ["role", `<span class="badge ${sample.tumor_or_normal.toLowerCase()}">${esc(sample.tumor_or_normal)}</span>`],
    ["sample_type", esc(sample.sample_type)],
    ["gene_panel", esc(sample.gene_panel)],
    ["bait_set", esc(sample.bait_set || "—")],
    ["run_mode", esc(sample.run_mode || "—")],
    ["file_group", esc(sample.file_group || "—")],
    ["preservation", metaText(sample.preservation)],
    ["sample_origin", metaText(sample.sample_origin)],
    ["sequencing_date", fmtDate(sample.sequencing_date)],
  ];
  $("#tq-summary-kv").innerHTML = kv.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  $("#tq-summary").classList.remove("hidden");
  pushNav();
  // Preselect the sample's own bait-set logic when known; otherwise
  // keep whatever logic the user already picked.
  await applyBaitSetLogic(sample, "tq-logic");
  renderTryExplainer($("#tq-logic").value);
}

async function runTry() {
  const sample = tryState.sample;
  const logic = $("#tq-logic").value;
  if (!sample || !logic || tryState.busy) return;
  tryState.busy = true;
  $("#tq-run").disabled = true;
  const params = new URLSearchParams();
  params.set("pairing_logic_name", logic);
  params.set("limit", OTHERS_PAGE_SIZE);
  params.set("offset", 0);
  try {
    const data = await api(`/sample/${sample.id}/possible-matches?${params}`);
    renderTryResult(data, logic);
  } catch (e) {
    banner("error", `Dry run failed: ${esc(e.message)}`, true);
  } finally {
    tryState.busy = false;
    $("#tq-run").disabled = false;
  }
}

function renderTryResult(data, logic) {
  const s = data.sample;
  const isTumor = s.tumor_or_normal === "TUMOR";
  const parts = [];

  parts.push(`<p class="hint">Dry run for <strong>${esc(s.primary_id)} — ${esc(s.cmo_sample_name)}</strong> under logic <strong>${esc(logic)}</strong>. Read-only preview: nothing was recorded.</p>`);

  // Pre-existing pairings (informational — not from this dry run).
  const cmList = data.current_matches || [];
  if (cmList.length) {
    parts.push(`<h3>Currently recorded pairings (pre-existing data, not from this run)</h3>`);
    parts.push(cmList.map((cm) => `
      <div class="current-match">
        <dl class="kv">
          <dt>matched ${isTumor ? "normal" : "tumor"}</dt>
          <dd>${esc(cm.sample.primary_id)} — ${esc(cm.sample.cmo_sample_name)} (${esc(cm.sample.patient__cmo || "no patient")})</dd>
          <dt>trigger_type</dt><dd>${esc(cm.trigger_type || "automatic")}</dd>
          <dt>mismatched_keys</dt><dd>${cm.mismatched_keys ? keyList(cm.mismatched_keys) : "—"}</dd>
        </dl>
        ${noteList(cm.notes)}
      </div>`).join(""));
  }

  // What the cascade would do.
  const rows = data.other_matches || [];
  const winner = rows.find((c) => c.would_be_selected);
  if (isTumor) {
    if (winner) {
      parts.push(`<h3>Cascade outcome</h3>
        <p>The cascade would pick <strong>${esc(winner.sample.primary_id)} — ${esc(winner.sample.cmo_sample_name)}</strong> (${esc(winner.sample.patient__cmo || "no patient")}) in the <span class="badge tier">${esc(winner.tier)}</span> tier.</p>`);
    } else {
      parts.push(`<h3>Cascade outcome</h3>
        <p>No candidate would be selected — the sample would stay unpaired under this logic.</p>`);
    }
  } else {
    const matchedN = (typeof data.other_matches_matched === "number") ? data.other_matches_matched : "—";
    parts.push(`<h3>Cascade outcome</h3>
      <p>This is a <strong>${esc(s.tumor_or_normal)}</strong> sample: the cascade has no single winner. ${esc(String(matchedN))} of the in-scope candidates pass every key — those are the samples this logic would consider pairing with it.</p>`);
  }

  // Candidate scope summary.
  const total = (typeof data.other_matches_total === "number") ? data.other_matches_total : rows.length;
  const summaryParts = [];
  summaryParts.push(`${total} candidate${total === 1 ? "" : "s"} in scope`);
  if (typeof data.other_matches_matched === "number") summaryParts.push(`${data.other_matches_matched} pass every key`);
  const tierBits = Object.entries(data.other_matches_tiers || {}).map(([t, n]) => `${n} ${t.replace(/_/g, " ")}`).join(", ");
  if (tierBits) summaryParts.push(`tiers: ${tierBits}`);
  const breakdownBits = (data.other_matches_breakdown || []).slice(0, 3).map((b) => `${b.count} on ${b.mismatched_keys.join(" + ")}`).join(", ");
  if (breakdownBits) summaryParts.push(`mismatches: ${breakdownBits}`);
  parts.push(`<h3>Candidates (${esc(logic)})</h3><p class="hint">${summaryParts.join(" · ")}</p>`);

  if (!rows.length) {
    parts.push(`<p class="empty">No candidates.</p>`);
  } else {
    parts.push(`<table>
      <thead><tr><th>sample</th><th>patient</th><th>patient__dmp</th><th>tier</th><th>status</th><th>mismatched keys</th><th>sequencing date</th></tr></thead>
      <tbody>${rows.map((c) => {
        const cs = c.sample;
        const status = c.matched
          ? '<span class="badge ok">matches all keys</span>'
          : '<span class="badge bad">mismatch</span>';
        const star = c.would_be_selected ? ' <span class="star" title="strategy cascade would pick this candidate">★</span>' : "";
        return `<tr>
          <td>${esc(cs.primary_id)} — ${esc(cs.cmo_sample_name)}${star}</td>
          <td>${esc(cs.patient__cmo || "—")}</td>
          <td>${esc(cs.patient__dmp || "—")}</td>
          <td><span class="badge tier">${esc(c.tier)}</span></td>
          <td>${status}</td>
          <td>${c.mismatched_keys ? keyList(c.mismatched_keys) : "—"}</td>
          <td>${fmtDate(cs.sequencing_date)}</td>
        </tr>`;
      }).join("")}</tbody></table>`);
  }

  $("#tq-result-body").innerHTML = parts.join("");
  $("#tq-result").classList.remove("hidden");
}

/* How each strategy pairs — sourced from pairing_strategies.py (keep the
 * wording in sync with that module). */
const TRY_EXPLAINERS = {
  argos: `
    <h4>Hard matching keys</h4>
    <p>Same <code>patient__cmo</code>, <code>gene_panel</code>, and <code>bait_set</code> (case-insensitive). <code>sample_type</code> is <em>not</em> a key (beagle v2.3.0 semantics).</p>
    <h4>Additional gates (every tier)</h4>
    <ul>
      <li><strong>Run mode</strong>: raw run-mode strings are normalized to buckets (<code>novaseq x</code> → novaseq_x, <code>novaseq</code>, <code>hiseq</code>; any other value must match exactly) and must be equal on both sides.</li>
      <li><strong>Preservation</strong>: each side's (preservation, sample_origin) pair resolves to a category — both fields empty → that side is <em>not considered</em> and never blocks; a present preservation decides (any "ffpe" entry → ffpe, anything else → frozen) and the origin is free unless it names the <em>opposite</em> category (a within-side conflict → unresolvable → blocks); with preservation empty, the origin alone decides ("ffpe", or "fresh or frozen"/"frozen"; any other value is unresolvable → blocks). Two considered sides must agree, and an unresolvable side blocks the pair.</li>
    </ul>
    <h4>Cascade tiers (in order; latest sequencing_date wins within a tier)</h4>
    <ol>
      <li>normals ingested under the same <code>request_id</code>;</li>
      <li>all other normals for the same patient;</li>
      <li>pooled normals (<code>sample_type</code> "Pooled Normal", no patient): <code>gene_panel</code> + <code>bait_set</code> must match, and run mode is resolved through the pooled-machines table (the pool's machine name → machine class; the tumor's run_mode → novaseq/hiseq family; both must resolve and agree).</li>
    </ol>
    <p>DMP samples (<code>sample_type</code> "DMP") are manual-pairing only: the cascade never selects them, and any pair involving one carries the "DMP: manual pairing only" mismatch reason.</p>
  `,
  tempo: `
    <h4>Tiers (in order)</h4>
    <ol>
      <li><strong>Historical pairing</strong>: the tumor's current latest pairing under this logic is retained unconditionally — bait and run-mode checks are bypassed (any mismatches are only noted). There is no fallback: if the historical normal is missing, the tumor stays unpaired.</li>
      <li><strong>Patient-wide</strong>: same <code>patient__cmo</code> (the only hard key); <code>bait_set</code> (case-insensitive) and the run-mode bucket (<code>novaseq</code>/<code>hiseq</code> substrings) must match; the latest <code>sequencing_date</code> wins.</li>
    </ol>
    <p><code>gene_panel</code> and <code>sample_type</code> are not keys; there is no request cascade and no pooled tier.</p>
  `,
  dmp: `
    <h4>Scope</h4>
    <p>Strictly <code>sample_type</code> "DMP" on both sides: LIMS samples, pooled normals, and cross-group DMP↔LIMS candidates are never paired here (those stay manual under the ARGOS logic).</p>
    <h4>Hard matching keys (case-insensitive)</h4>
    <ul>
      <li><code>patient__dmp</code> (the P- patient id): both sides must have one.</li>
      <li><code>gene_panel</code>: a DMP patient can carry both IMPACT and ACCESS samples, so a cross-assay T/N is invalid.</li>
    </ul>
    <p>No run-mode or preservation gates (DMP samples carry no preservation data) and no pooled tier.</p>
    <h4>Cascade</h4>
    <p>Single tier of key-matching DMP normals; the winner is deterministic — the first candidate in <code>primary_id</code> (string) order.</p>
    <p>Automatic DMP pairing runs via the import script's <code>--trigger-dmp</code> flag (or the dashboard); manual pairing still exists and always wins — an existing pairing (including a manual/override one) is returned unchanged on re-runs.</p>
  `,
  default: `
    <h4>Hard matching keys</h4>
    <p>Same <code>patient__cmo</code>, <code>gene_panel</code>, and <code>bait_set</code> (case-insensitive).</p>
    <h4>Cascade (latest sequencing_date wins within a tier)</h4>
    <ol>
      <li>normals ingested under the same <code>request_id</code> (when the tumor has one);</li>
      <li>all other normals for the same patient.</li>
    </ol>
    <p>No run-mode or preservation gates and no pooled tier — the reference implementation (used by ACCESS), not beagle-aligned.</p>
  `,
};

function renderTryExplainer(logicName) {
  const logic = PAIRING_LOGICS.find((l) => l.name === logicName);
  const key = logic ? logic.strategy_name : null;
  const body = TRY_EXPLAINERS[key];
  $("#tq-explain-body").innerHTML = body
    ? body
    : `<p>No explainer available for strategy <code>${esc(key || logicName || "—")}</code> — see <code>pairing_strategies.py</code>.</p>`;
  $("#tq-explain").classList.remove("hidden");
}

$("#tq-lookup").addEventListener("click", trySearch);
$("#tq-id").addEventListener("keydown", (e) => {
  if (e.key === "Enter") trySearch();
});
$("#tq-sample").addEventListener("change", (e) => {
  const picked = tryState.candidates.find((s) => String(s.id) === e.target.value);
  if (picked) selectTrySample(picked);
});
$("#tq-logic").addEventListener("change", (e) => renderTryExplainer(e.target.value));
$("#tq-run").addEventListener("click", runTry);

/* ---------- auth: current user + sign-out ---------- */

/* The browser sends the auth_token cookie automatically (same origin),
 * so /auth/me needs no Authorization header. */
let currentUser = null;

function userDisplay(user) {
  return user.display_name || user.preferred_username || user.email || "";
}

function renderCurrentUser() {
  $("#current-user").textContent = currentUser ? userDisplay(currentUser) : "";
}

async function loadCurrentUser() {
  try {
    currentUser = await api("/auth/me");
    renderCurrentUser();
  } catch (_) {
    /* 401/403 already handled by api(): the user is being sent to /login. */
  }
}

function signOut() {
  window.location = "/auth/logout";
}

$("#logout-btn").addEventListener("click", signOut);

/* ---------- init ---------- */

(async function init() {
  loadCurrentUser();
  await loadLogics();
  if ($("#tq-logic").value) renderTryExplainer($("#tq-logic").value);
  loadPairs();
  loadSamples(0);
  loadPatients();
  loadRequests();
  loadPooled(0);
  loadMachines();
  if (location.hash) restoreFromHash();
  else history.replaceState(null, "", "#/samples");
})();
