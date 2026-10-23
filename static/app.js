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
    throw new Error(detail);
  }
  return body;
}

/* ---------- view navigation (tabs + back button) ---------- */

/* Navigation state lives in the URL hash, so browser Back/Forward, reloads
 * and shared links all work:
 *   #/samples   #/patients[/<patient_id>]   #/requests[/<request_id>]
 *   #/matches[/<sample primary_id>]
 * Every navigation (tab switch, opening/closing a detail panel, opening
 * matches for a sample) pushes a history entry. */

const VIEWS = ["samples", "patients", "requests", "pooled", "matches"];
let restoringNav = false;

function currentViewName() {
  const el = document.querySelector("main.view:not(.hidden)");
  return el ? el.id.replace("view-", "") : null;
}

function currentHash() {
  const view = currentViewName() || "samples";
  let id = null;
  if (view === "patients" && patientsState.patient) id = patientsState.patient.patient_id;
  else if (view === "requests" && requestsState.request) id = requestsState.request.request_id;
  else if (view === "matches" && matchesState.sample) id = matchesState.sample.primary_id;
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
      else if (!patientsState.patient || patientsState.patient.patient_id !== id) {
        const rows = await api(`/patient/?q=${encodeURIComponent(id)}&limit=200`);
        const row = rows.find((p) => p.patient_id === id);
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

/* ---------- cross-view navigation (patient_id / request_id links) ---------- */

function navigateToPatient(patientId) {
  $("#p-q").value = patientId;
  showView("patients");
  loadPatients().then((results) => {
    if (!results) return;
    const row = results.find((p) => p.patient_id === patientId);
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
  "primary_id", "cmo_sample_name", "patient_id", "request_id",
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
      <td>${esc(s.patient_id || "")}</td>
      <td>${esc(s.request_id || "")}</td>
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
  } catch (e) {
    banner("error", `Failed to load pairing logics: ${esc(e.message)}`, true);
  }
}

/* Select the pairing logic assigned to the sample's bait set (server-side
   bait_sets table), so the page doesn't default to the first logic. */
async function applyBaitSetLogic(sample) {
  if (!sample || !sample.bait_set) return false;
  try {
    const row = await api(`/bait-sets/lookup?name=${encodeURIComponent(sample.bait_set)}`);
    if (!row) return false;
    const logicSel = $("#m-logic");
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
  return `${s.primary_id} — ${s.cmo_sample_name} (${s.patient_id || "no patient"}, ${s.tumor_or_normal})`;
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
      <dt>patient_id</dt><dd>${s.patient_id ? `<span class="nav-link" data-nav-patient="${esc(s.patient_id)}" title="Go to the patient page">${esc(s.patient_id)}</span>` : "—"}</dd>
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
    <p class="hint">Click patient_id / request_id to go to that patient's or request's page.</p>`;

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
            <dd><span class="nav-link" data-sample-id="${esc(c.id)}" title="Open this sample's matches &amp; overrides">${esc(c.primary_id)} — ${esc(c.cmo_sample_name)} (${esc(c.patient_id || "no patient")})</span></dd>
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
        <td>${esc(cs.patient_id || "—")}</td>
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

/* Sample panel: click a patient_id / request_id value to go to its page. */
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
      <tr class="click" data-pid="${esc(p.patient_id)}">
        <td>${esc(p.patient_id)}</td>
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
      const row = rows.find((p) => p.patient_id === tr.dataset.pid);
      if (row) openPatient(row);
    });
  });
}

/* Sample id -> {logic name: [opposite samples]}, from all latest pairings
   (one request). A tumor lists its current normal(s) (at most one); a
   normal lists every currently-paired tumor (1-to-many). */
async function loadPairingMap() {
  if (!patientsState.pairings) {
    const rows = await api("/pairing/?latest=true&limit=500");
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
  $("#p-detail-title").textContent = `Patient ${row.patient_id}`;
  $("#p-detail-meta").innerHTML =
    `<dt>samples</dt><dd>${row.sample_count} (${row.tumor_count} tumors, ${row.normal_count} normals)</dd>` +
    `<dt>requests</dt><dd>${esc((row.requests || []).join(", ") || "—")}</dd>` +
    Object.entries(row.metadata).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${metaText(v)}</dd>`).join("");

  const tbody = $("#p-samples tbody");
  tbody.innerHTML = "";
  $("#p-samples-empty").classList.remove("hidden");
  try {
    const [samples] = await Promise.all([
      api(`/sample/?patient_id=${encodeURIComponent(row.patient_id)}&limit=200`),
      loadPairingMap(),
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
      loadPairingMap(),
    ]);
    // /sample/ filters request_id as a substring match; keep only the
    // samples belonging to this exact request.
    const samples = (requestsState.requestSamples =
      fetchedSamples.filter((s) => s.request_id === row.request_id));

    // Per-patient counts within this request (non-null patient_id only).
    const byPatient = {};
    for (const s of samples) {
      if (!s.patient_id) continue;
      const g = (byPatient[s.patient_id] =
        byPatient[s.patient_id] || { patient_id: s.patient_id, count: 0, tumor: 0, normal: 0 });
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
        <td>${esc(s.patient_id || "")}</td>
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
      <tr class="click" data-pid="${esc(p.patient_id)}">
        <td>${esc(p.patient_id)}</td>
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
    const row = rows.find((p) => p.patient_id === patientId);
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

/* ---------- init ---------- */

(async function init() {
  await loadLogics();
  loadSamples(0);
  loadPatients();
  loadRequests();
  loadPooled(0);
  loadMachines();
  if (location.hash) restoreFromHash();
  else history.replaceState(null, "", "#/samples");
})();
