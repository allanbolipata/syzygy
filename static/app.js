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

async function apiRes(path, opts) {
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
  return { res, body };
}

async function api(path, opts) {
  return (await apiRes(path, opts)).body;
}

/* Total from the X-Total-Count header (listing endpoints), or null when
 * absent (older backend / non-listing calls). */
function totalFromHeaders(res) {
  const raw = res.headers.get("X-Total-Count");
  if (raw === null) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

/* "Showing 1–50 of 1,234" from the pre-pagination total; falls back to
 * the old per-page wording when the header is missing. */
function countLabel(total, rows, offset) {
  if (!rows.length) return "";
  if (total === null) return `${rows.length} on this page`;
  const from = (offset || 0) + 1;
  const to = (offset || 0) + rows.length;
  return `Showing ${from}–${to} of ${total.toLocaleString("en-US")}`;
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

/* ---------- session pairing-logic View (topbar #view-logic) ---------- *
 * Mandatory + exclusive (card t_3e1444f0, spec updates 2026-11-11): the View
 * IS the pairing logic for the pairing tabs (pairs, matches, try) — no
 * per-tab logic dropdowns and no "ALL" state. viewLogic === null means
 * nothing selected yet: the three pairing tabs are gated ("select a View
 * first") while browse tabs stay usable. Persisted in localStorage so the
 * choice survives tab switches, reloads and navigation. Browse tabs
 * (samples/patients/requests/pooled) ignore the View entirely. */
const VIEW_LOGIC_KEY = "syzygy.logic_view";
const PAIRING_VIEWS = ["pairs", "matches", "try"];
const VIEW_GATE_IDS = { pairs: "pairs-gate", matches: "matches-gate", try: "try-gate" };
const VIEW_TAG_IDS = { pairs: "pr-view-label", matches: "m-view-label", try: "tq-view-label" };
let viewLogic = null;

function activeLogics() {
  return PAIRING_LOGICS.filter((l) => l.active);
}

/* Options = ACTIVE logics only. An explicit "Select a logic…" placeholder
 * exists only while nothing is chosen (it is a required-selection prompt,
 * not an ALL state). */
function renderViewSelect() {
  const actives = activeLogics();
  const sel = $("#view-logic");
  if (!actives.length) {
    sel.innerHTML = `<option value="" disabled selected>No active pairing logics</option>`;
    return;
  }
  sel.innerHTML =
    (viewLogic ? "" : `<option value="" selected>Select a logic…</option>`) +
    actives.map((l) => `<option value="${esc(l.name)}">${esc(l.name)}</option>`).join("");
  if (viewLogic) sel.value = viewLogic;
}

function renderViewTags() {
  for (const id of Object.values(VIEW_TAG_IDS)) {
    const el = $(`#${id}`);
    if (el) el.textContent = viewLogic ? `View: ${viewLogic}` : "no View selected";
  }
}

function gatePairingTabs() {
  const has = !!viewLogic;
  document.querySelectorAll(".tab[data-tab]").forEach((b) => {
    if (!PAIRING_VIEWS.includes(b.dataset.tab)) return;
    b.disabled = !has;
    b.title = has ? "" : "Select a pairing logic in the topbar first";
  });
  for (const id of Object.values(VIEW_GATE_IDS)) $(`#${id}`).classList.toggle("hidden", has);
}

function applyViewState() {
  renderViewSelect();
  renderViewTags();
  gatePairingTabs();
}

/* First-visit resolution: saved localStorage value (only if it still names
 * an ACTIVE logic) → server's settings.default_pairing_logic_name (only if
 * it resolves to an active logic) → unselected (pairing tabs gated). The
 * default is not written to localStorage: a first-visit preselection keeps
 * following the server default until the user actively picks one. */
async function resolveInitialViewLogic() {
  let name = null;
  try { name = localStorage.getItem(VIEW_LOGIC_KEY); } catch (_) { /* private mode */ }
  if (name && !activeLogics().some((l) => l.name === name)) name = null;
  if (!name) {
    try {
      const r = await api("/pairing-logic/default");
      const def = r && r.name;
      if (def && activeLogics().some((l) => l.name === def)) name = def;
    } catch (_) { /* endpoint unavailable: leave unselected */ }
  }
  return name;
}

/* User picked a View (topbar change): persist + re-render every pairing tab
 * immediately — no per-tab override exists anywhere. */
function setViewLogic(name) {
  viewLogic = name || null;
  try {
    if (viewLogic) localStorage.setItem(VIEW_LOGIC_KEY, viewLogic);
    else localStorage.removeItem(VIEW_LOGIC_KEY);
  } catch (_) { /* localStorage unavailable: session-scoped only */ }
  applyViewState();
  rerenderPairingTabs();
}

function rerenderPairingTabs() {
  if (!viewLogic) {
    pairsState.logic = null;
    return;
  }
  // Pair Report: fresh full list under the new logic (loadPairs drops the
  // stale search + prefetch; mirrors the old per-tab logic-change sequence).
  pairsState.logic = viewLogic;
  resetPrSearch();
  renderPrGroups();
  renderPrLogicExplainer();
  applyHarmonyViewMode();
  loadPairs();
  // Matches: candidate scope always follows the View's logic.
  renderLogicMismatch("m", matchesState.baitLogic);
  if (matchesState.sample) {
    matchesState.othersOffset = 0;
    showMatches(0);
  }
  // Try: explainer and (if a sample is open) the read-only dry run follow.
  renderTryExplainer(viewLogic);
  renderLogicMismatch("tq", tryState.baitLogic);
  if (tryState.sample) runTry();
}

/* Non-blocking hint when the open sample's bait-set logic differs from the
 * View (spec update 2): matching / dry run still run under the View. */
function renderLogicMismatch(prefix, baitLogic) {
  const el = $(`#${prefix}-logic-mismatch`);
  if (!el) return;
  if (viewLogic && baitLogic && baitLogic.toLowerCase() !== viewLogic.toLowerCase()) {
    el.innerHTML =
      `This sample's bait set belongs to the <strong>${esc(baitLogic)}</strong> logic, ` +
      `but the session View is <strong>${esc(viewLogic)}</strong> — matching and pairing ` +
      `here run under the View's logic.`;
    el.classList.remove("hidden");
  } else {
    el.innerHTML = "";
    el.classList.add("hidden");
  }
}

$("#view-logic").addEventListener("change", (e) => {
  setViewLogic(e.target.value);
  if (!viewLogic && PAIRING_VIEWS.includes(currentViewName())) {
    banner("info", "Select a pairing logic in the topbar View first — the pairing tabs need one.", true);
  }
});

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
  const prev = currentViewName();
  const isPairs = name === "pairs";
  if (prev && prev !== "pairs" && isPairs) {
    // Entering the Pair Report: drop any stale search so the user always
    // lands on the full list for the current logic.
    resetPrSearch();
  }
  if (prev === "pairs" && !isPairs) {
    // Leaving the Pair Report: clear the search too, so returning later
    // doesn't resurrect a filter over freshly loaded rows.
    resetPrSearch();
  }
  document.querySelectorAll(".tab[data-tab]").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name)
  );
  document.querySelectorAll("main.view").forEach((el) =>
    el.classList.toggle("hidden", el.id !== `view-${name}`)
  );
  // Hash-restored or programmatic entry into a gated pairing tab: the gate
  // panel inside the view says it; a banner makes it unmistakable.
  if (PAIRING_VIEWS.includes(name) && !viewLogic) {
    banner("info", "Select a pairing logic in the topbar <strong>View</strong> first — the pairing tabs need one.", true);
  }
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
      const rows = await api(`/sample/lookup?q=${encodeURIComponent(id)}&limit=${SAMPLE_PICK_LIMIT}`);
      const row = rows.find((x) =>
        x.primary_id.toLowerCase() === id.toLowerCase() ||
        x.cmo_sample_name.toLowerCase() === id.toLowerCase()
      );
      if (row) {
        $("#tq-id").value = id;
        tryState.candidates = rows;
        tryState.resultsTotal = null;
        tryRenderResults();
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
  // Reset-then-prefill: a stale Patients advanced filter would AND against
  // the prefilled search and the landing would find no patient row.
  clearPatientQueries();
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
  // Reset-then-prefill (see navigateToPatient): clears the Requests
  // advanced filters before the prefilled search runs.
  clearRequestQueries();
  $("#r-q").value = requestId;
  showView("requests");
  loadRequests().then((results) => {
    if (!results) return;
    const row = results.find((r) => r.request_id === requestId);
    if (row) openRequest(row);
    else banner("error", `No request found with id ${esc(requestId)}.`, true);
  });
}

/* Sample links in the Pair Report go to the Samples tab (card t_d8720219):
 * the landing contract is RESET-THEN-PREFILL — the full Samples query state
 * (simple search, every advanced filter field, the tumor_or_normal select)
 * is cleared first, then #f-q is prefilled with the sample's primary_id and
 * the listing reloads; the exact row is focused (scroll + flash) when it is
 * on the loaded page. Reset first because samplesParams() ANDs #f-q with
 * every advanced field: a stale advanced filter from a previous Samples
 * session used to combine with the prefilled id and land on zero results.
 * Accepts a full sample record (PAIR/DMP tables) or a bare search term
 * (HARMONY rows carry primary_id strings only). No role-select special
 * case survives the reset; the topbar View selector is still never touched. */
function navigateToSampleInSamples(sampleOrTerm) {
  const term = typeof sampleOrTerm === "string"
    ? sampleOrTerm
    : String((sampleOrTerm && sampleOrTerm.primary_id) || "");
  if (!term) return;
  clearSampleQueries();
  $("#f-q").value = term;
  showView("samples");
  loadSamples(0).then(() => focusSampleRow(term));
}

/* Highlight + scroll to the Samples-listing row whose primary_id matches
 * exactly (case-insensitive). When the prefilled search surfaces only
 * partial candidates (no exact row), the prefilled search itself is the
 * fallback focus — no error, matching the "search prefilled (or direct row
 * focus)" contract. */
function focusSampleRow(term) {
  const want = String(term).toLowerCase();
  const row = (samplesState.results || []).find(
    (s) => String(s.primary_id || "").toLowerCase() === want
  );
  if (!row) return;
  const tr = document.querySelector(
    `#samples-table tbody tr[data-id="${CSS.escape(String(row.id))}"]`
  );
  if (!tr) return;
  document.querySelectorAll("#samples-table tbody tr.row-flash")
    .forEach((el) => el.classList.remove("row-flash"));
  tr.classList.add("row-flash");
  tr.scrollIntoView({ block: "center" });
  setTimeout(() => tr.classList.remove("row-flash"), 1600);
}

/* DMP patient ids are conditional links (card t_d8720219): the Patients tab
 * lists patient__cmo groups, so a DMP id counts as a "matching patient" only
 * when some sample carrying that patient__dmp also has a patient__cmo. The
 * Pair Report row usually carries that proof directly (its own sample record
 * has patient__cmo — use it, no query); otherwise ask the Samples listing
 * for a sample with the same patient__dmp. If nothing resolves, the click is
 * a no-op (stay on the Pair Report) with an info banner; the cell's tooltip
 * states the conditional behaviour up front. */
async function navigateToDmpPatient(dmpId, directCmo) {
  if (directCmo) return navigateToPatient(directCmo);
  try {
    const rows = await api(`/sample/?q=${encodeURIComponent(dmpId)}&limit=200`);
    const want = String(dmpId).toLowerCase();
    const hit = (rows || []).find((s) =>
      s.patient__cmo && s.patient__dmp &&
      String(s.patient__dmp).toLowerCase() === want
    );
    if (hit) return navigateToPatient(hit.patient__cmo);
  } catch (_) { /* unreachable listing = treated as no match: no-op below */ }
  banner("info", `No patient record matches DMP patient ${esc(dmpId)} — staying in the Pair Report.`, true);
}

/* ---------- samples view ---------- */

const samplesState = { limit: 50, offset: 0, results: [], total: null };

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
    const { res, body } = await apiRes(`/sample/?${samplesParams(offset)}`);
    samplesState.results = Array.isArray(body) ? body : [];
    samplesState.total = totalFromHeaders(res);
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
  $("#s-count").textContent = countLabel(samplesState.total, rows, samplesState.offset);
  $("#s-prev").disabled = samplesState.offset === 0;
  $("#s-next").disabled =
    samplesState.total !== null
      ? samplesState.offset + rows.length >= samplesState.total
      : rows.length < samplesState.limit;
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const sample = rows.find((s) => s.id === tr.dataset.id);
      if (sample) openMatchesFor(sample);
    });
  });
}

/* Full Samples query reset: every control samplesParams() reads — simple
 * search, advanced filter fields, tumor_or_normal select. Shared by the
 * Clear button and navigateToSampleInSamples (Pair Report links reset the
 * listing before prefilling, so a stale advanced filter can never AND
 * against the prefilled id). Function declaration: hoisted, so the
 * navigation helpers defined earlier may call it. */
function clearSampleQueries() {
  $("#f-q").value = "";
  for (const id of SAMPLE_FILTER_IDS) $(`#f-${id}`).value = "";
  $("#f-tumor_or_normal").value = "";
  updateAdvancedSummary();
}
$("#btn-search").addEventListener("click", () => loadSamples(0));
$("#btn-clear").addEventListener("click", () => {
  clearSampleQueries();
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

/* Samples "Advanced filters": collapsible <details>, closed by default.
 * Collapsing never clears values (fields stay in the DOM inside <details>);
 * when closed, the summary shows how many advanced fields have values,
 * e.g. "Advanced filters (3 active)". Search semantics are untouched —
 * samplesParams() still reads the same #f-* ids regardless of open state. */
const sAdvanced = $("#s-advanced");
const sAdvancedCount = $("#s-advanced-count");
function activeAdvancedFilterCount() {
  let n = 0;
  for (const id of SAMPLE_FILTER_IDS) {
    if ($(`#f-${id}`).value.trim()) n++;
  }
  return n;
}
function updateAdvancedSummary() {
  const n = activeAdvancedFilterCount();
  if (!sAdvanced.open && n > 0) {
    sAdvancedCount.textContent = `(${n} active)`;
    sAdvancedCount.classList.remove("hidden");
  } else {
    sAdvancedCount.textContent = "";
    sAdvancedCount.classList.add("hidden");
  }
}
sAdvanced.addEventListener("toggle", updateAdvancedSummary);
for (const id of SAMPLE_FILTER_IDS) {
  $(`#f-${id}`).addEventListener("input", updateAdvancedSummary);
}
updateAdvancedSummary();

/* ---------- matches & overrides view ---------- */

// Candidates are a bounded page of the strategy's candidate scope:
// the API returns at most OTHERS_PAGE_SIZE rows per request, with
// whole-scope totals alongside (matches DEFAULT_OTHER_MATCHES_LIMIT).
const OTHERS_PAGE_SIZE = 500;
const matchesState = {
  sample: null,
  data: null,
  busy: false,
  results: [], // the current lookup rows shown in the results panel
  resultsTotal: null, // X-Total-Count for those rows (null when absent)
  othersOffset: 0,
  othersTotal: 0,
  baitLogic: null, // the open sample's bait-set logic (mismatch-hint input)
};

async function loadLogics() {
  try {
    // Inactive logics stay out of the View options (the View is active-only
    // and is now the ONLY pairing-logic control; the old per-tab selectors
    // listed inactive logics — that capability is deliberately dropped).
    const logics = await api("/pairing-logic/?limit=100");
    PAIRING_LOGICS = logics;
    renderViewSelect();
  } catch (e) {
    banner("error", `Failed to load pairing logics: ${esc(e.message)}`, true);
  }
}

/* Which pairing logic the sample's bait set is assigned to (server-side
 * bait_sets table), or null when unknown. This is ONLY a hint input now —
 * the topbar View is the single logic control (no per-tab preselection). */
async function lookupBaitSetLogic(sample) {
  if (!sample || !sample.bait_set) return null;
  try {
    const row = await api(`/bait-sets/lookup?name=${encodeURIComponent(sample.bait_set)}`);
    return row ? row.pairing_logic : null;
  } catch (_) {
    return null;
  }
}

/* Shared sample results panel (card t_4f824f62): the Matches & Overrides and
 * Try Pairing search boxes render a clickable results table in the p-detail
 * visual language (panel + click rows) instead of the old dropdowns. Rows are
 * capped at SAMPLE_PICK_LIMIT; a hint appears when the list is truncated.
 * Clicking a row selects that sample for the tab's downstream action. */
const SAMPLE_PICK_LIMIT = 50;

function renderSampleResults(prefix, rows, selectedId, total, onSelect) {
  $(`#${prefix}-results`).classList.remove("hidden");
  const tbody = $(`#${prefix}-results-table tbody`);
  tbody.innerHTML = rows.map((s) => `
    <tr class="click${selectedId != null && String(s.id) === String(selectedId) ? " selected" : ""}" data-id="${esc(s.id)}">
      <td>${esc(s.primary_id)}</td>
      <td>${esc(s.cmo_sample_name)}</td>
      <td>${esc(s.patient__cmo || "—")}</td>
      <td>${esc(s.request_id || "—")}</td>
      <td><span class="badge ${s.tumor_or_normal.toLowerCase()}">${esc(s.tumor_or_normal)}</span></td>
    </tr>`).join("");
  $(`#${prefix}-results-empty`).classList.toggle("hidden", rows.length > 0);
  $(`#${prefix}-results-count`).textContent = countLabel(total, rows, 0);
  const capped = total !== null ? total > rows.length : rows.length >= SAMPLE_PICK_LIMIT;
  $(`#${prefix}-results-more`).classList.toggle("hidden", !capped);
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const sample = rows.find((s) => String(s.id) === tr.dataset.id);
      if (sample) onSelect(sample);
    });
  });
}

/* "selected" summary line above a pairing tab's downstream buttons. */
function renderSelectedBar(prefix, sample) {
  const bar = $(`#${prefix}-selected-bar`);
  if (!sample) {
    bar.classList.add("hidden");
    return;
  }
  $(`#${prefix}-selected-text`).innerHTML = sampleLabel(sample);
  bar.classList.remove("hidden");
}

async function openMatchesFor(sample) {
  matchesState.sample = sample;
  // A sample arriving from another page: the results panel shows it as the
  // selected row — it is exactly how the user reached this tab.
  matchesState.results = [sample];
  matchesState.resultsTotal = 1;
  matchesState.othersOffset = 0;
  // Bait-set logic is only a mismatch hint now; matching runs under the View.
  matchesState.baitLogic = await lookupBaitSetLogic(sample);
  renderLogicMismatch("m", matchesState.baitLogic);
  showView("matches");
  renderSelectedBar("m", sample);
  $("#m-show").disabled = false;
  renderMatchesResults();
  showMatches(0);
}

function sampleLabel(s) {
  return `${s.primary_id} — ${s.cmo_sample_name} (${s.patient__cmo || "no patient"}, ${s.tumor_or_normal})`;
}

function renderMatchesResults() {
  renderSampleResults(
    "m",
    matchesState.results,
    matchesState.sample && matchesState.sample.id,
    matchesState.resultsTotal,
    (sample) => selectMatchesSample(sample)
  );
}

/* Row click / preselect: make this sample the tab's downstream target.
 * run=false selects without re-running the candidate query (the Show matches
 * button is the explicit trigger in that flow); row clicks run immediately,
 * mirroring openMatchesFor from the browse pages. */
async function selectMatchesSample(sample, run = true) {
  matchesState.sample = sample;
  matchesState.othersOffset = 0;
  matchesState.baitLogic = await lookupBaitSetLogic(sample);
  renderLogicMismatch("m", matchesState.baitLogic);
  renderSelectedBar("m", sample);
  $("#m-show").disabled = false;
  renderMatchesResults();
  pushNav();
  if (run) showMatches(0);
  else hideMatchesPanels();
}

async function searchMatchesSamples() {
  const q = $("#m-q").value.trim();
  if (!q) return;
  try {
    const { res, body } = await apiRes(`/sample/?q=${encodeURIComponent(q)}&limit=${SAMPLE_PICK_LIMIT}`);
    const results = Array.isArray(body) ? body : [];
    matchesState.results = results;
    matchesState.resultsTotal = totalFromHeaders(res);
    if (!results.length) {
      matchesState.sample = null;
      renderSelectedBar("m", null);
      $("#m-show").disabled = true;
      renderMatchesResults();
      hideMatchesPanels();
      banner("info", `No samples match "${esc(q)}".`);
      return;
    }
    renderMatchesResults();
    // Keep the current selection when it is among the results; otherwise
    // preselect the top row (the old dropdown's implicit default) — the
    // candidate query itself still only runs on Show matches or a row click.
    if (!matchesState.sample || !results.some((s) => String(s.id) === String(matchesState.sample.id))) {
      await selectMatchesSample(results[0], false);
    }
  } catch (e) {
    banner("error", `Search failed: ${esc(e.message)}`, true);
  }
}

async function showMatches(offset) {
  const sample = matchesState.sample;
  const logic = viewLogic; // the View IS the logic (no per-tab selector)
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
  const allow = $("#m-allow-overrides").checked;
  const logic = viewLogic; // the View IS the logic (no per-tab selector)
  if (isDmpLogic(logic)) {
    banner("error",
      "The DMP logic is automatic-only: overrides are not offered under a " +
      "DMP View. The intended manual workflow is pinning an ARGOS " +
      "(or other-logic) tumor to a same-patient DMP normal — switch the " +
      "topbar View to that logic and override from the tumor's candidate " +
      "list.", true);
    return;
  }
  const payload = {
    tumor_id: data.sample.id,
    normal_id: normalId,
    trigger_type: "override",
    pairing_logic_name: logic,
    strict: !allow,
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
  total: null,
  offset: 0,
  limit: 100,
  pairings: null, // sample id -> {pairing logic name: [opposite samples]}
  patient: null,
  patientSamples: [],
};

const PATIENT_ADVANCED_IDS = ["species", "sex", "oncotree_code", "diagnosis_date", "request_id"];

/* Shared collapsible advanced-filter panel wiring (samples pattern from
 * t_6c3a32bf): collapsing the <details> keeps values in the DOM; a closed
 * panel shows an "(N active)" pill in its summary. */
function bindAdvancedPanel(detailsEl, countEl, fieldSels) {
  function update() {
    let n = 0;
    for (const sel of fieldSels) {
      if ($(sel).value.trim()) n++;
    }
    if (!detailsEl.open && n > 0) {
      countEl.textContent = `(${n} active)`;
      countEl.classList.remove("hidden");
    } else {
      countEl.textContent = "";
      countEl.classList.add("hidden");
    }
  }
  detailsEl.addEventListener("toggle", update);
  for (const sel of fieldSels) {
    $(sel).addEventListener("input", update);
  }
  update();
  return update;
}

const updatePatientAdvancedSummary = bindAdvancedPanel(
  $("#p-advanced"),
  $("#p-advanced-count"),
  PATIENT_ADVANCED_IDS.map((id) => `#p-${id}`)
);

function patientsParams(offset) {
  const p = new URLSearchParams();
  p.set("limit", patientsState.limit);
  p.set("offset", offset);
  const q = $("#p-q").value.trim();
  if (q) p.set("q", q);
  for (const id of PATIENT_ADVANCED_IDS) {
    const v = $(`#p-${id}`).value.trim();
    if (v) p.set(id, v);
  }
  return p.toString();
}

async function loadPatients(offset = 0) {
  try {
    const { res, body } = await apiRes(`/patient/?${patientsParams(offset)}`);
    patientsState.results = Array.isArray(body) ? body : [];
    patientsState.total = totalFromHeaders(res);
    patientsState.offset = offset;
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
  $("#p-count").textContent = countLabel(patientsState.total, rows, patientsState.offset);
  $("#p-prev").disabled = patientsState.offset === 0;
  $("#p-next").disabled = patientsState.total !== null
    ? patientsState.offset + rows.length >= patientsState.total
    : rows.length < patientsState.limit;
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

$("#p-search").addEventListener("click", () => loadPatients(0));
$("#p-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadPatients(0);
});
for (const id of PATIENT_ADVANCED_IDS) {
  $(`#p-${id}`).addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadPatients(0);
  });
}
$("#p-limit").addEventListener("change", (e) => {
  patientsState.limit = Number(e.target.value);
  loadPatients(0);
});
$("#p-prev").addEventListener("click", () =>
  loadPatients(Math.max(0, patientsState.offset - patientsState.limit))
);
$("#p-next").addEventListener("click", () =>
  loadPatients(patientsState.offset + patientsState.limit)
);
/* Full Patients query reset (simple search + advanced fields): shared by
 * the Clear button and navigateToPatient. */
function clearPatientQueries() {
  $("#p-q").value = "";
  for (const id of PATIENT_ADVANCED_IDS) $(`#p-${id}`).value = "";
  updatePatientAdvancedSummary();
}
$("#p-clear").addEventListener("click", () => {
  clearPatientQueries();
  loadPatients(0);
});
$("#p-close").addEventListener("click", () => {
  closePatientDetail();
  pushNav();
});

/* ---------- requests view ---------- */

const requestsState = {
  results: [],
  total: null,
  offset: 0,
  limit: 100,
  request: null,
  requestSamples: [],
  requestPatients: [],
};

const REQUEST_ADVANCED_IDS = ["gene_panel", "sample_type", "bait_set", "patient__cmo"];

const updateRequestAdvancedSummary = bindAdvancedPanel(
  $("#r-advanced"),
  $("#r-advanced-count"),
  REQUEST_ADVANCED_IDS.map((id) => `#r-${id}`)
);

function requestsParams(offset) {
  const p = new URLSearchParams();
  p.set("limit", requestsState.limit);
  p.set("offset", offset);
  const q = $("#r-q").value.trim();
  if (q) p.set("q", q);
  for (const id of REQUEST_ADVANCED_IDS) {
    const v = $(`#r-${id}`).value.trim();
    if (v) p.set(id, v);
  }
  return p.toString();
}

async function loadRequests(offset = 0) {
  try {
    const { res, body } = await apiRes(`/request/?${requestsParams(offset)}`);
    requestsState.results = Array.isArray(body) ? body : [];
    requestsState.total = totalFromHeaders(res);
    requestsState.offset = offset;
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
  $("#r-count").textContent = countLabel(requestsState.total, rows, requestsState.offset);
  $("#r-prev").disabled = requestsState.offset === 0;
  $("#r-next").disabled = requestsState.total !== null
    ? requestsState.offset + rows.length >= requestsState.total
    : rows.length < requestsState.limit;
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

$("#r-search").addEventListener("click", () => loadRequests(0));
$("#r-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadRequests(0);
});
for (const id of REQUEST_ADVANCED_IDS) {
  $(`#r-${id}`).addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadRequests(0);
  });
}
$("#r-limit").addEventListener("change", (e) => {
  requestsState.limit = Number(e.target.value);
  loadRequests(0);
});
$("#r-prev").addEventListener("click", () =>
  loadRequests(Math.max(0, requestsState.offset - requestsState.limit))
);
$("#r-next").addEventListener("click", () =>
  loadRequests(requestsState.offset + requestsState.limit)
);
/* Full Requests query reset (simple search + advanced fields): shared by
 * the Clear button and navigateToRequest. */
function clearRequestQueries() {
  $("#r-q").value = "";
  for (const id of REQUEST_ADVANCED_IDS) $(`#r-${id}`).value = "";
  updateRequestAdvancedSummary();
}
$("#r-clear").addEventListener("click", () => {
  clearRequestQueries();
  loadRequests(0);
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
  { key: "tumor_primary_id", header: "TUMOR_SAMPLE", group: "Tumor sample", text: (r) => r.tumor.primary_id, link: (r) => sampleLink(r.tumor) },
  { key: "normal_primary_id", header: "NORMAL_SAMPLE", group: "Normal sample", text: (r) => r.normal.primary_id, link: (r) => sampleLink(r.normal) },
  { key: "tumor_bait_set", header: "BAIT_SET", group: "Tumor sample", text: (r) => r.tumor.bait_set || "" },
  { key: "tumor_patient__cmo", header: "TUMOR_PATIENT__CMO", group: "Tumor sample", text: (r) => r.tumor.patient__cmo || "", link: (r) => patientLink(r.tumor && r.tumor.patient__cmo) },
  { key: "tumor_patient__dmp", header: "TUMOR_PATIENT__DMP", group: "Tumor sample", text: (r) => r.tumor.patient__dmp || "", link: (r) => dmpPatientLink(r.tumor && r.tumor.patient__dmp, r.tumor && r.tumor.patient__cmo) },
  { key: "tumor_anon_id", header: "TUMOR_ANON_ID", group: "Tumor sample", text: (r) => r.tumor.anon_id || "" },
  { key: "tumor_cmo_sample_name", header: "TUMOR_CMO_SAMPLE_NAME", group: "Tumor sample", text: (r) => pairText(r.tumor.cmo_sample_name), link: (r) => sampleLink(r.tumor) },
  { key: "tumor_role", header: "TUMOR_ROLE", group: "Tumor sample", text: (r) => r.tumor.tumor_or_normal || "" },
  { key: "tumor_sample_type", header: "TUMOR_SAMPLE_TYPE", group: "Tumor sample", text: (r) => r.tumor.sample_type || "" },
  { key: "tumor_gene_panel", header: "TUMOR_GENE_PANEL", group: "Tumor sample", text: (r) => r.tumor.gene_panel || "" },
  { key: "tumor_request_id", header: "TUMOR_REQUEST_ID", group: "Tumor sample", text: (r) => r.tumor.request_id || "", link: (r) => requestLink(r.tumor && r.tumor.request_id) },
  { key: "tumor_run_mode", header: "TUMOR_RUN_MODE", group: "Tumor sample", text: (r) => r.tumor.run_mode || "" },
  { key: "tumor_file_group", header: "TUMOR_FILE_GROUP", group: "Tumor sample", text: (r) => r.tumor.file_group || "" },
  { key: "tumor_preservation", header: "TUMOR_PRESERVATION", group: "Tumor sample", text: (r) => pairText(r.tumor.preservation) },
  { key: "tumor_sample_origin", header: "TUMOR_SAMPLE_ORIGIN", group: "Tumor sample", text: (r) => pairText(r.tumor.sample_origin) },
  { key: "tumor_sequencing_date", header: "TUMOR_SEQUENCING_DATE", group: "Tumor sample", text: (r) => pairDate(r.tumor.sequencing_date) },
  { key: "tumor_additional_metadata", header: "TUMOR_ADDITIONAL_METADATA", group: "Tumor sample", text: (r) => pairText(r.tumor.additional_metadata) },
  { key: "tumor_errors", header: "TUMOR_ERRORS", group: "Tumor sample", text: (r) => (r.tumor.errors || []).join("; ") },
  { key: "tumor_id", header: "TUMOR_ID", group: "Tumor sample", text: (r) => r.tumor.id },
  { key: "normal_bait_set", header: "NORMAL_BAIT_SET", group: "Normal sample", text: (r) => r.normal.bait_set || "" },
  { key: "normal_patient__cmo", header: "NORMAL_PATIENT__CMO", group: "Normal sample", text: (r) => r.normal.patient__cmo || "", link: (r) => patientLink(r.normal && r.normal.patient__cmo) },
  { key: "normal_patient__dmp", header: "NORMAL_PATIENT__DMP", group: "Normal sample", text: (r) => r.normal.patient__dmp || "", link: (r) => dmpPatientLink(r.normal && r.normal.patient__dmp, r.normal && r.normal.patient__cmo) },
  { key: "normal_anon_id", header: "NORMAL_ANON_ID", group: "Normal sample", text: (r) => r.normal.anon_id || "" },
  { key: "normal_cmo_sample_name", header: "NORMAL_CMO_SAMPLE_NAME", group: "Normal sample", text: (r) => pairText(r.normal.cmo_sample_name), link: (r) => sampleLink(r.normal) },
  { key: "normal_role", header: "NORMAL_ROLE", group: "Normal sample", text: (r) => r.normal.tumor_or_normal || "" },
  { key: "normal_sample_type", header: "NORMAL_SAMPLE_TYPE", group: "Normal sample", text: (r) => r.normal.sample_type || "" },
  { key: "normal_gene_panel", header: "NORMAL_GENE_PANEL", group: "Normal sample", text: (r) => r.normal.gene_panel || "" },
  { key: "normal_request_id", header: "NORMAL_REQUEST_ID", group: "Normal sample", text: (r) => r.normal.request_id || "", link: (r) => requestLink(r.normal && r.normal.request_id) },
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

/* DMP pair-report columns: the reference's 15-column pair file (spec
 * docs/dmp_reference_alignment.md, section c). Values are derived from the
 * row's tumor/normal records exactly as dmp_translator.remap_dmp_record +
 * the router's _dmp_pair_cells derive them server-side, so the table
 * matches the CSV/TSV export. Genuinely absent values render the
 * reference's NA sentinel — never a placeholder. */
const DMP_NA = "NA";
/* The reference's name parse: "P-0000001-T01-IM7" -> patient "P-0000001",
 * idx "T01", ver "IM7". */
const DMP_NAME_RE = /(P-\d+)-(\w+)-(\w+)/;
function dmpMeta(sample) {
  return (sample && typeof sample.additional_metadata === "object" && sample.additional_metadata) || {};
}
/* Faithful port of dmp_translator.remap_dmp_record (T2/8): the key fields
 * of a DMP sample record, with the reference's fallbacks. */
function remapDmpRecord(sample) {
  const name = sample ? String(sample.primary_id || "") : "";
  const m = DMP_NAME_RE.exec(name);
  const meta = dmpMeta(sample);
  const pick = (v) => (v === null || v === undefined ? "" : String(v));
  return {
    idx: m ? m[2] : "",
    ver: m ? m[3] : "",
    bamname: pick(meta.bamname) || pick(sample && sample.anon_id),
    group: pick(meta.group),
    coverage: pick(meta.coverage),
    matched: pick(meta.matched_tag),
    pool_id: pick(meta.pool_id) || pick(meta.project_name),
    patient: pick(sample && sample.patient__dmp) || (m ? m[1] : ""),
  };
}
/* The reference's 15 cells for one report row (a port of the router's
 * _dmp_pair_cells, T4/8). The normal side is NA when the tumor is
 * unmatched — except normal_IM_ver, which the backend backfills from
 * the tumor's version. *_bam_exists is resolved server-side from beagle
 * file records for the export only; it is not in the report payload, so
 * the table shows NA for both. */
function dmpCells(r) {
  const t = r.tumor;
  const n = r.normal || null;
  const tr = remapDmpRecord(t);
  const nr = n ? remapDmpRecord(n) : null;
  const na = DMP_NA;
  return {
    tumor_sample: (t && t.primary_id) ? String(t.primary_id) : na,
    patient: tr.patient || na,
    tumor_idx: tr.idx || na,
    tumor_IM_ver: tr.ver || na,
    tumor_bamname: tr.bamname || na,
    tumor_group: tr.group || na,
    tumor_matched: tr.matched || (n ? "Matched" : "Unmatched"),
    tumor_bam_exists: na,
    tumor_bam_coverage: tr.coverage || na,
    normal_sample: (n && n.primary_id) ? String(n.primary_id) : na,
    normal_bamname: nr ? (nr.bamname || na) : na,
    // The reference backfills the tumor's IM_ver whenever the normal's is
    // missing — on unmatched rows AND on paired rows whose normal name
    // carries no version segment (mirrors _dmp_pair_cells' `or`).
    normal_IM_ver: (nr && nr.ver) || tr.ver || na,
    pool_id: tr.pool_id || na,
    normal_bam_exists: na,
    normal_bam_coverage: nr ? (nr.coverage || na) : na,
  };
}
const dmpText = (key) => (r) => dmpCells(r)[key];
const DMP_COLUMNS = [
  { key: "tumor_sample", header: "tumor_sample", group: "Tumor", text: dmpText("tumor_sample"), link: (r) => sampleLink(r.tumor) },
  { key: "patient", header: "patient", group: "Tumor", text: dmpText("patient"), link: (r) => { const v = dmpCells(r).patient; return v === DMP_NA ? null : dmpPatientLink(v, r.tumor && r.tumor.patient__cmo); } },
  { key: "tumor_idx", header: "tumor_idx", group: "Tumor", text: dmpText("tumor_idx") },
  { key: "tumor_IM_ver", header: "tumor_IM_ver", group: "Tumor", text: dmpText("tumor_IM_ver") },
  { key: "tumor_bamname", header: "tumor_bamname", group: "Tumor", text: dmpText("tumor_bamname") },
  { key: "tumor_group", header: "tumor_group", group: "Tumor", text: dmpText("tumor_group") },
  { key: "tumor_matched", header: "tumor_matched", group: "Tumor", text: dmpText("tumor_matched") },
  { key: "tumor_bam_exists", header: "tumor_bam_exists", group: "Tumor", text: dmpText("tumor_bam_exists") },
  { key: "tumor_bam_coverage", header: "tumor_bam_coverage", group: "Tumor", text: dmpText("tumor_bam_coverage") },
  { key: "normal_sample", header: "normal_sample", group: "Normal", text: dmpText("normal_sample"), link: (r) => sampleLink(r.normal) },
  { key: "normal_bamname", header: "normal_bamname", group: "Normal", text: dmpText("normal_bamname") },
  { key: "normal_IM_ver", header: "normal_IM_ver", group: "Normal", text: dmpText("normal_IM_ver") },
  { key: "pool_id", header: "pool_id", group: "Tumor", text: dmpText("pool_id") },
  { key: "normal_bam_exists", header: "normal_bam_exists", group: "Normal", text: dmpText("normal_bam_exists") },
  { key: "normal_bam_coverage", header: "normal_bam_coverage", group: "Normal", text: dmpText("normal_bam_coverage") },
];
/* Where each DMP column comes from (shown in the "About these columns"
 * panel and as tooltips on the column headers). */
const DMP_LEGEND = {
  tumor_sample: "The DMP tumor's sample name (primary_id)",
  patient: "patient__dmp from the DMP record; falls back to the P-\u2026 id parsed from the sample name; links to the patient detail when a patient record matches this DMP id",
  tumor_idx: "Tumor index parsed from the sample name (second-to-last dash segment, e.g. T01); NA when unparseable",
  tumor_IM_ver: "IM version parsed from the sample name (last dash segment, e.g. IM7); NA when unparseable",
  tumor_bamname: "bamname from the DMP record's additional_metadata; falls back to anon_id",
  tumor_group: "group from the DMP record's additional_metadata; NA when the record has none",
  tumor_matched: "matched_tag from the DMP record; otherwise Matched / Unmatched from the pairing",
  tumor_bam_exists: "Whether beagle has a BAM record for the tumor; resolved server-side for the export, NA in the table",
  tumor_bam_coverage: "coverage from the DMP record's additional_metadata; NA when absent",
  normal_sample: "The paired DMP normal's sample name (primary_id); NA when the tumor is unmatched",
  normal_bamname: "The paired normal's bamname (additional_metadata, fallback anon_id); NA when the tumor is unmatched",
  normal_IM_ver: "IM version parsed from the paired normal's name; backfilled from the tumor's version when the normal's is missing; NA otherwise",
  pool_id: "pool_id from the DMP record's additional_metadata; falls back to project_name",
  normal_bam_exists: "Whether beagle has a BAM record for the normal; resolved server-side for the export, NA in the table",
  normal_bam_coverage: "coverage from the paired normal's additional_metadata; NA when absent or the tumor is unmatched",
};
const DMP_DEFAULT_KEYS = DMP_COLUMNS.map((c) => c.key);

/* HARMONY view: the exact 8 columns of GET /pairing/harmony/ (ticket 2/4),
 * in the endpoint's order. HARMONY uses the selectable-columns machinery
 * with its own column set (groups: Normal side, Tumor side, patient +
 * consent) — never the legacy DMP/PAIR column sets. The endpoint orders
 * rows normal-first, then tumor: a normal with several tumors occupies
 * several consecutive rows and repeats its normal-side columns (one row
 * per (normal, tumor) pair, 1-to-many). */
const HARMONY_COLUMNS = [
  { key: "normal_sample", header: "normal_sample", group: "Normal" },
  { key: "tumor_sample", header: "tumor_sample", group: "Tumor" },
  { key: "normal_bamname", header: "normal_bamname", group: "Normal" },
  { key: "normal_bam_path", header: "normal_bam_path", group: "Normal" },
  { key: "tumor_bamname", header: "tumor_bamname", group: "Tumor" },
  { key: "tumor_bam_path", header: "tumor_bam_path", group: "Tumor" },
  { key: "patient", header: "patient", group: "Patient" },
  { key: "consent", header: "consent", group: "Patient" },
];
const HARMONY_DEFAULT_KEYS = HARMONY_COLUMNS.map((c) => c.key);

const pairsState = {
  // The current page of the report only. Pagination is server-side
  // (GET /pairing/report/), so the browser never holds the full set.
  rows: [],
  // Server total row count for the loaded logic (all pages combined).
  total: 0,
  logic: null,
  // Per-logic selected column keys (each logic has its own column set and
  // its own selection, so ARGOS choices never affect DMP and vice versa).
  selected: {},
  // Split-panel filter groups that the user collapsed (default: expanded).
  groupCollapsed: {},
  // Server-side pagination: 0-based index of the page currently displayed.
  // Next/Prev/jump fetch the requested page from /pairing/report/ (see
  // loadReportPage) rather than slicing a full in-memory set. The download
  // is server-side too (GET /pairing/export/) and covers the FULL searched
  // set, not just the current page.
  page: 0,
  pageSize: 50,
  // View-scoped search (#pr-search): sent to the server as the q param on
  // GET /pairing/report/ (and /pairing/export/), so it filters the FULL
  // merged list, not just the loaded page. Stored trimmed + lowercased
  // (the form the server matches in). Cleared on view enter/leave and on
  // logic change so a stale filter never carries into new rows.
  query: "",
  // Server-sourced "Paired" count for the loaded logic (from
  // /pairing/unmatched-count/): the logic's latest rows with both
  // sides present. null until the count lands; #pr-count falls back
  // to the in-memory rows meanwhile (and whenever a search filter
  // narrows the table, which the server count doesn't cover).
  pairedCount: null,
  // DMP only: the report response's reference summary counts
  // ({patients, tn_pairs, unmatched_tumors}) and 5 QC warning buckets
  // (T5/8) — computed server-side over the FULL pair frame, identical
  // on every page and independent of the search query. null for
  // non-DMP logics and until the first DMP page lands.
  dmpSummary: null,
  dmpWarnings: null,
  dmpStatsPending: false,
  // HARMONY only: the counts the harmony endpoint reports for the FULL
  // searched frame ({matched, unmatched, prevNormal}) — a page's rows can
  // never produce them. null until the first harmony page lands.
  harmonyStats: null,
};

/* HARMONY is its own pairing logic and its own normal-oriented view
 * (one row per normal–tumor pair, 8 fixed columns — see HARMONY_COLUMNS).
 * Its seeded row reuses the "dmp" strategy, so every DMP branch must treat
 * it separately; isDmpLogic() below returns false for it. */
function isHarmonyLogic(logicName) {
  return !!logicName && logicName.toUpperCase() === "HARMONY";
}
function isDmpLogic(logicName) {
  if (isHarmonyLogic(logicName)) return false;
  const l = PAIRING_LOGICS.find((x) => x.name === logicName);
  return !!logicName && (l ? l.strategy_name === "dmp" || logicName.toUpperCase() === "DMP" : logicName.toUpperCase() === "DMP");
}
function columnsForLogic(logicName) {
  if (isHarmonyLogic(logicName)) return HARMONY_COLUMNS;
  return isDmpLogic(logicName) ? DMP_COLUMNS : PAIR_COLUMNS;
}
function defaultSelectionFor(logicName) {
  if (isHarmonyLogic(logicName)) return new Set(HARMONY_DEFAULT_KEYS);
  return new Set(isDmpLogic(logicName) ? DMP_DEFAULT_KEYS : PAIR_DEFAULT_KEYS);
}
function selectionFor(logicName) {
  if (!logicName) return new Set();
  if (!pairsState.selected[logicName]) pairsState.selected[logicName] = defaultSelectionFor(logicName);
  return pairsState.selected[logicName];
}
function activeLogicName() {
  return pairsState.logic || viewLogic || null;
}

/* HARMONY view mode: the CSV/TSV format switch doesn't apply — HARMONY's
 * download is always the server-generated CSV. The selectable-columns panel
 * DOES apply: HARMONY has its own column set (HARMONY_COLUMNS, with its own
 * per-logic selection in pairsState.selected). The Download button relabels
 * to "Download CSV". Reversible: switching back to another logic restores
 * that logic's panel (collapsed state preserved). */
function applyHarmonyViewMode() {
  const harmony = isHarmonyLogic(activeLogicName());
  const showOpen = $("#pr-panel").classList.contains("collapsed");
  $("#pr-panel-open").classList.toggle("hidden", !showOpen);
  $("#pr-format").closest("label").classList.toggle("hidden", harmony);
  $("#pr-download").textContent = harmony ? "Download CSV" : "Download";
}
function selectedPairColumns() {
  const logic = activeLogicName();
  if (!logic) return [];
  return columnsForLogic(logic).filter((c) => selectionFor(logic).has(c.key));
}

/* Groups for the left split-panel filter: the selected (active) logic's
 * columns, grouped by side (Tumor / Normal / Pairing for the standard set;
 * Tumor / Normal for DMP; Normal / Tumor / Patient for HARMONY). The panel
 * never enumerates the other logics. */
function pairReportGroups() {
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
          `<label class="col-check"><input type="checkbox" data-prcol="${esc(c.key)}" data-prlogic="${esc(g.logic)}"${sel.has(c.key) ? " checked" : ""}><span class="col-check-label">${esc(c.header)}</span></label>`
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

/* HARMONY cells: the endpoint already resolves display values (em-dash
 * placeholders for missing bam paths, null for the absent side); consent
 * renders "true"/"false"/empty per the endpoint's value. */
function harmonyCellText(r, key) {
  const v = r[key];
  if (key === "consent") return v === null || v === undefined ? "" : (v ? "true" : "false");
  return pairText(v);
}

/* HARMONY identifier columns are clickable like the other Pair Report
 * variants (card t_d8720219). The frame carries primary_id STRINGS for the
 * sample-name columns (no sample records to resolve), so their links pass
 * the name as the Samples-tab search term; the patient column is
 * coalesce(tumor, normal).patient__dmp — a DMP id — so it goes through the
 * same conditional DMP-patient flow (no-op when no patient record matches).
 * The bam-path columns keep their em-dash placeholder rendering untouched. */
function harmonyCellHtml(r, key) {
  const text = harmonyCellText(r, key);
  if (text === "") return "";
  if (text !== "\\u2014" && (key === "normal_sample" || key === "tumor_sample")) {
    // No role hint: the Samples landing resets the query state (including
    // the role select) instead of relaxing it, so the column key carries no
    // extra meaning for navigation.
    return `<span class="nav-link" data-goto-sample="${esc(text)}" title="${esc(PAIR_LINK_TITLES.sample)}">${esc(text)}</span>`;
  }
  if (text !== "\u2014" && key === "patient")
    return `<span class="nav-link" data-nav-dmp-patient="${esc(text)}" title="${esc(PAIR_LINK_TITLES["dmp-patient"])}">${esc(text)}</span>`;
  return esc(text);
}

/* URLSearchParams for GET /pairing/harmony/: the same 1-based page/size and
 * search query as reportPageParams (the harmony endpoint takes the same page
 * contract), minus the report-only ones — pairing_logic_name is implicit
 * (that endpoint IS the HARMONY frame) and there are no DMP stats to defer.
 * One helper so the page fetch and the next-page prefetch send identical
 * params. */
function harmonyPageParams(page, pageSize) {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const q = (pairsState.query || "").trim().toLowerCase();
  if (q) params.set("q", q);
  return params;
}
/* Clickable identifier cells in the Pair Report table (card t_d8720219).
 * A column model may declare an optional ``link`` that resolves a row to a
 * navigation target:
 * - kind "sample"      → the Samples tab: the listing's query state is
 *   reset (simple search, advanced filters, role select) and #f-q is
 *   prefilled with the sample's primary_id, exact row focused. The cell
 *   carries the record id; the #pairs-table handler resolves the full sample
 *   from pairsState.rows (PAIR/DMP rows), and falls back to treating the
 *   value as a search term for the HARMONY table, whose rows carry
 *   primary_id strings only.
 * - kind "patient"     → the Patients tab with the detail panel opened
 *   (navigateToPatient, same as clicking a patient row).
 * - kind "request"     → the Requests tab with the request detail opened.
 * - kind "dmp-patient" → conditional: navigates only when a patient record
 *   matches the DMP id (the row's own patient__cmo if present, else a
 *   Samples-listing lookup); otherwise the click is a no-op with an info
 *   banner. The tooltip states this up front.
 * Cells render as the app's standard nav-link (underlined accent text) only
 * when the target resolves and the cell has a value; otherwise plain text. */
const sampleLink = (s) => (s && s.id != null ? { kind: "sample", id: s.id } : null);
const patientLink = (id) => (id ? { kind: "patient", id } : null);
const requestLink = (id) => (id ? { kind: "request", id } : null);
const dmpPatientLink = (dmpId, cmoId) =>
  (dmpId ? { kind: "dmp-patient", id: dmpId, cmo: cmoId || null } : null);
const PAIR_LINK_TITLES = {
  sample: "Open this sample in the Samples tab",
  patient: "Open this patient\u2019s detail",
  request: "Open this request\u2019s detail",
  "dmp-patient": "Open the matching patient\u2019s detail — only when a patient record matches this DMP id",
};

function pairCellHtml(r, c) {
  const raw = c.text(r);
  const link = c.link ? c.link(r) : null;
  if (!link || pairText(raw) === "") return `<td>${esc(raw)}</td>`;
  const attr = link.kind === "sample" ? "data-goto-sample"
    : link.kind === "patient" ? "data-nav-patient"
    : link.kind === "request" ? "data-nav-request" : "data-nav-dmp-patient";
  const cmo = link.kind === "dmp-patient" && link.cmo
    ? ` data-nav-cmo="${esc(String(link.cmo))}"` : "";
  return `<td><span class="nav-link" ${attr}="${esc(String(link.id))}"${cmo} title="${esc(PAIR_LINK_TITLES[link.kind])}">${esc(raw)}</span></td>`;
}

/* HARMONY: normal-oriented table over the CHECKED HARMONY columns (via
 * selectedPairColumns(), which resolves HARMONY_COLUMNS for this logic).
 * The server
 * returns ONE PAGE of the frame, ordered by normal primary_id then tumor
 * primary_id, so a normal's rows are consecutive: its normal-side columns
 * repeat and a heavier top border marks each new normal, making the 1-to-many
 * obvious at a glance. One-sided (unmatched) rows render the absent side's
 * columns empty, like the DMP unmatched convention. Same pagination / count /
 * empty states as the DMP view, from the same server page contract. */
function renderHarmonyTable() {
  const cols = selectedPairColumns(); // checked subset, endpoint order
  $("#pairs-table thead").innerHTML =
    `<tr>${cols.map((c) => `<th>${esc(c.header)}</th>`).join("")}</tr>`;
  const rows = pairsState.rows; // one server page, not the whole frame
  const total = pairsState.total; // the FULL searched frame's size
  const maxPage = Math.max(0, Math.ceil(total / pairsState.pageSize) - 1);
  if (pairsState.page > maxPage) pairsState.page = maxPage;
  const start = pairsState.page * pairsState.pageSize;
  // Group divider across the page boundary: the endpoint sends the normal of
  // the frame row just before this page (prev_normal_sample), so a normal's
  // group cut in half by paging still reads as a continuation on the next
  // page instead of as a brand-new group.
  const stats = pairsState.harmonyStats;
  const prevPageNormal = start > 0 && stats ? stats.prevNormal : null;
  $("#pairs-table tbody").innerHTML = rows
    .map((r, i) => {
      const prev = i > 0 ? rows[i - 1].normal_sample : prevPageNormal;
      const divider = prev !== null && prev !== r.normal_sample
        ? ` style="border-top:2px solid #c3ccd6"` : "";
      return `<tr>${cols.map((c) =>
        `<td${divider}>${harmonyCellHtml(r, c.key)}</td>`).join("")}</tr>`;
    }).join("");
  $("#pr-prev").disabled = pairsState.page === 0;
  $("#pr-next").disabled = pairsState.page >= maxPage;
  const totalPages = maxPage + 1;
  const pageInput = $("#pr-page");
  pageInput.max = totalPages;
  pageInput.value = pairsState.page + 1;
  $("#pr-page-of").textContent = `of ${totalPages}`;
  pageInput.disabled = total === 0;
  // Matched/unmatched are the endpoint's counts over the FULL searched frame
  // a page cannot see the other pages' one-sided rows. Only before the
  // first harmony response lands do we fall back to this page's own rows.
  const matched = stats
    ? stats.matched
    : rows.filter((r) => r.normal_sample && r.tumor_sample).length;
  const unmatched = stats
    ? stats.unmatched
    : rows.filter((r) => !(r.normal_sample && r.tumor_sample)).length;
  const emptyEl = $("#pr-empty");
  emptyEl.classList.toggle("hidden", total !== 0 && cols.length !== 0);
  emptyEl.textContent = cols.length === 0
    ? "No columns selected — tick columns in the side panel."
    : pairsState.query
      ? "No HARMONY rows match this search."
      : "No HARMONY rows yet \u2014 run pairing first.";
  const paging = total > pairsState.pageSize
    ? ` (page ${pairsState.page + 1}/${maxPage + 1}: showing ${start + 1}\u2013${start + rows.length} of ${total})`
    : "";
  $("#pr-count").textContent =
    `${total} rows: ${matched} matched, ${unmatched} unmatched${paging}`;
  $("#pr-download").disabled = total === 0 || cols.length === 0;
  $("#pr-legend").classList.add("hidden");
}

function renderPairsTable() {
  if (isHarmonyLogic(activeLogicName())) return renderHarmonyTable();
  const logic = activeLogicName();
  const dmp = isDmpLogic(logic);
  const cols = selectedPairColumns();
  $("#pairs-table thead").innerHTML = `<tr>${cols.map((c) =>
    `<th${dmp && DMP_LEGEND[c.key] ? ` title="${esc(DMP_LEGEND[c.key])}"` : ""}>${esc(c.header)}</th>`
  ).join("")}</tr>`;
  // The browser holds only the current server page (pairsState.rows); the
  // page indicator and the row count come from the server total
  // (pairsState.total). A search query filters the FULL merged list
  // server-side (the q param), so rows is already the searched set.
  const rows = pairsState.rows;
  const total = pairsState.total;
  const maxPage = Math.max(0, Math.ceil(total / pairsState.pageSize) - 1);
  const start = pairsState.page * pairsState.pageSize;
  // DMP: an ambiguous tumor legitimately appears on one row per candidate
  // normal — flag the repeated rows with a subtle cue so they read as
  // one tumor fanned out, not as duplicated data.
  const tumorCounts = new Map();
  if (dmp) {
    for (const r of rows) {
      const id = r.tumor && r.tumor.primary_id;
      if (id) tumorCounts.set(id, (tumorCounts.get(id) || 0) + 1);
    }
  }
  $("#pairs-table tbody").innerHTML = rows
    .map((r) => {
      const tid = r.tumor && r.tumor.primary_id;
      const repeat = dmp && tid && (tumorCounts.get(tid) || 0) > 1;
      return `<tr${repeat ? ` class="pr-dmp-repeat" title="Tumor appears on ${tumorCounts.get(tid)} rows: multiple candidate normals (ambiguous match)"` : ""}>${cols.map((c) => pairCellHtml(r, c)).join("")}</tr>`;
    }).join("");
  $("#pr-prev").disabled = pairsState.page === 0;
  $("#pr-next").disabled = pairsState.page >= maxPage;
  // Keep the skip-to-page input in sync with Prev/Next: the input shows
  // the 1-based page; "of N" shows the last page (always >= 1).
  const totalPages = maxPage + 1;
  const pageInput = $("#pr-page");
  pageInput.max = totalPages;
  pageInput.value = pairsState.page + 1;
  $("#pr-page-of").textContent = `of ${totalPages}`;
  pageInput.disabled = total === 0;
  // Matched: the server count (pairsState.pairedCount, from
  // /pairing/unmatched-count/) once it has landed and no search filter
  // narrows the table — until then, and while filtering, fall back to
  // the current page's rows.
  let matched;
  if (pairsState.query) {
    // A search narrows the set server-side, but the reference counts
    // cover the FULL frame — while filtering, use the page's rows.
    matched = rows.filter((r) => r.tumor && r.normal).length;
  } else if (dmp && pairsState.dmpSummary) {
    matched = pairsState.dmpSummary.tn_pairs;
  } else if (pairsState.pairedCount != null) {
    matched = pairsState.pairedCount;
  } else {
    matched = rows.filter((r) => r.tumor && r.normal).length;
  }
  const empty = rows.length === 0 || cols.length === 0;
  const emptyEl = $("#pr-empty");
  emptyEl.classList.toggle("hidden", !empty);
  emptyEl.textContent =
    pairsState.query ? "No pairs match this search." : "No current pairs for this logic.";
  const paging = total > pairsState.pageSize
    ? ` (page ${pairsState.page + 1}/${maxPage + 1}: showing ${start + 1}\u2013${Math.min(start + pairsState.pageSize, total)} of ${total})`
    : "";
  $("#pr-count").textContent = cols.length === 0
    ? "no columns selected"
    : dmp
      ? `${total} rows: ${matched} matched, ${total - matched} unmatched${paging}`
      : `${total} pair${total === 1 ? "" : "s"}${paging}`;
  $("#pr-download").disabled = empty;
  // Legend (and header tooltips) only for the DMP column set.
  $("#pr-legend").classList.toggle("hidden", !dmp);
  if (dmp) {
    $("#pr-legend-body").innerHTML = DMP_COLUMNS.map((c) =>
      `<tr><td>${esc(c.header)}</td><td>${esc(DMP_LEGEND[c.key] || "")}</td></tr>`
    ).join("");
  }
  renderDmpWarnings();
}

/* DMP QC warnings (T5/8): the 5 reference buckets, each expandable,
 * from the report response's warnings field — computed over the FULL
 * pair frame, so the buckets stay constant across pages and searches.
 * Hidden (and cleared) for non-DMP logics. */
const DMP_WARNING_BUCKETS = [
  { key: "matched_tumors_without_normal", label: "Matched tumors without a normal" },
  { key: "tumor_samples_with_multiple_ambiguous_normals", label: "Tumors with multiple ambiguous normals" },
  { key: "samples_with_bams_missing", label: "Samples with BAMs missing" },
  { key: "low_coverage_tumors", label: "Low-coverage tumors" },
  { key: "low_coverage_normals", label: "Low-coverage normals" },
];
/* Buckets list their samples lazily and cumulatively in chunks. The full
 * DMP buckets run to hundreds of thousands of ids (the BAM-missing bucket
 * alone was ~278k), and materializing every one on every page paint is what
 * froze the tab: a closed bucket shows only its count, opening it lists the
 * first chunk, and "show more" extends that same list. */
const DMP_WARNING_CHUNK = 300;
function dmpWarningListHtml(samples, upto) {
  const shown = Math.min(upto, samples.length);
  const rest = samples.length - shown;
  return `<ul class="pr-warn-list">${samples.slice(0, shown).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` +
    (rest > 0
      ? `<button type="button" class="pr-warn-more" data-upto="${shown + DMP_WARNING_CHUNK}">` +
        `Show ${Math.min(rest, DMP_WARNING_CHUNK).toLocaleString()} more (${rest.toLocaleString()} remaining)</button>`
      : "");
}
function renderDmpWarnings() {
  const warnEl = $("#pr-dmp-warnings");
  if (!warnEl) return;
  // Only the DMP pairing space has the reference's warnings to show.
  if (!isDmpLogic(activeLogicName())) {
    warnEl.classList.add("hidden");
    warnEl.innerHTML = "";
    return;
  }
  if (!pairsState.dmpWarnings) {
    // Stats are still in flight (or this logic has none yet).
    if (pairsState.dmpStatsPending) {
      warnEl.classList.remove("hidden");
      warnEl.innerHTML =
        `<p class="pr-dmp-warnings-title">QC warnings <span class="hint">— computing over the full report…</span></p>`;
    } else {
      warnEl.classList.add("hidden");
      warnEl.innerHTML = "";
    }
    return;
  }
  const w = pairsState.dmpWarnings;
  warnEl.classList.remove("hidden");
  warnEl.innerHTML =
    `<p class="pr-dmp-warnings-title">QC warnings <span class="hint">— full report, not just this page</span></p>` +
    DMP_WARNING_BUCKETS.map((b) => {
      const samples = w[b.key] || [];
      return `<details class="pr-warn-bucket" data-warn-key="${b.key}">
  <summary>${esc(b.label)} <span class="pr-warn-count">${samples.length.toLocaleString()}</span></summary>
  <div class="pr-warn-body">${samples.length
    ? `<p class="hint">Expand to list up to ${DMP_WARNING_CHUNK.toLocaleString()} of ${samples.length.toLocaleString()}.</p>`
    : `<p class="hint">None.</p>`}</div>
</details>`;
    }).join("");
  warnEl.querySelectorAll("details.pr-warn-bucket").forEach((det) => {
    const key = det.getAttribute("data-warn-key");
    const body = det.querySelector(".pr-warn-body");
    const bucket = () => (pairsState.dmpWarnings || {})[key] || [];
    let listed = 0;
    det.addEventListener("toggle", () => {
      if (!det.open || listed) return;
      const samples = bucket();
      if (!samples.length) return;
      listed = Math.min(samples.length, DMP_WARNING_CHUNK);
      body.innerHTML = dmpWarningListHtml(samples, listed);
    });
    body.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".pr-warn-more");
      if (!btn) return;
      const samples = bucket();
      listed = Math.min(samples.length, Number(btn.getAttribute("data-upto")));
      body.innerHTML = dmpWarningListHtml(samples, listed);
    });
  });
}

/* DMP summary + QC warnings now come from GET /pairing/report-stats/ (5/8):
 * they describe the full frame and are page/q independent, so pages request
 * include_stats=false and this fetches the numbers in the background,
 * cached per logic for the life of the tab (pairing runs drop the cache).
 * The pair table paints without them. */
let dmpStatsCache = {}; // logic name -> { summary, warnings }
let dmpStatsAbort = null;
async function loadDmpReportStats(logic) {
  if (!isDmpLogic(logic)) return;
  const cached = dmpStatsCache[logic];
  if (cached) {
    pairsState.dmpSummary = cached.summary;
    pairsState.dmpWarnings = cached.warnings;
    pairsState.dmpStatsPending = false;
    if (activeLogicName() === logic) renderPairsTable();
    return;
  }
  pairsState.dmpStatsPending = true;
  if (activeLogicName() === logic) renderDmpWarnings();
  if (dmpStatsAbort) dmpStatsAbort.abort();
  dmpStatsAbort = new AbortController();
  try {
    const r = await api(
      `/pairing/report-stats/?pairing_logic_name=${encodeURIComponent(logic)}`,
      { signal: dmpStatsAbort.signal },
    );
    if (activeLogicName() !== logic) return; // logic changed mid-flight
    dmpStatsCache[logic] = { summary: r.summary || null, warnings: r.warnings || null };
    pairsState.dmpSummary = dmpStatsCache[logic].summary;
    pairsState.dmpWarnings = dmpStatsCache[logic].warnings;
    pairsState.dmpStatsPending = false;
    renderPairsTable(); // repaint the DMP count line/overview with real numbers
  } catch (e) {
    if (e && e.name === "AbortError") return;
    pairsState.dmpStatsPending = false;
    if (activeLogicName() === logic) {
      renderDmpWarnings();
      banner("error", `Failed to load QC warnings: ${esc(e.message)}`, true);
    }
  }
}

/* Abort controller for the in-flight pair load; a new load (tab click,
 * logic change) cancels the previous one so scans can't pile up on the
 * server. */
let pairsAbort = null;

/* Pair Report load indicator (#pr-loading in the toolbar). Shown while the
 * paired rows and, for DMP, the large unmatched set are in flight so a big
 * load doesn't look frozen. Callers guard it with the is-active check so a
 * superseded load never hides the spinner the newer load is showing. */
function showPrLoading(text) {
  const el = $("#pr-loading");
  if (!el) return;
  const label = el.querySelector(".pr-loading-text");
  if (label) label.textContent = text;
  el.classList.remove("hidden");
}
function hidePrLoading() {
  const el = $("#pr-loading");
  if (el) el.classList.add("hidden");
}

/* URLSearchParams for GET /pairing/report/: the 1-based page and size,
 * plus the active free-text search query (trimmed + lowercased, the
 * form the server matches in; omitted when empty). One helper so the
 * page fetch and the next-page prefetch always send identical params. */
function reportPageParams(logic, page, pageSize) {
  const params = new URLSearchParams({
    pairing_logic_name: logic,
    page: String(page),
    page_size: String(pageSize),
    // The DMP summary/QC warnings describe the FULL pair frame and cost a
    // full-frame walk to compute; a page doesn't wait for them (see
    // loadDmpReportStats, which fetches them in the background).
    include_stats: "false",
  });
  const q = (pairsState.query || "").trim().toLowerCase();
  if (q) params.set("q", q);
  return params;
}
async function loadReportPage(page) {
  const logic = pairsState.logic;
  if (!logic) return;
  // HARMONY is server-paginated too, but on its own endpoint with its own
  // row shape (the normal-oriented frame), so its page turns go through
  // loadHarmonyPage instead of the report endpoint.
  if (isHarmonyLogic(logic)) return loadHarmonyPage(page);
  if (pairsAbort) pairsAbort.abort();
  pairsAbort = new AbortController();
  const signal = pairsAbort.signal;
  // True only while THIS load is the active one. A newer load replaces
  // pairsAbort (and thus owns the loading indicator) and aborts this
  // load's signal, so a stale load must not repaint or clear the spinner
  // the newer load is showing.
  const active = () => signal === pairsAbort.signal;
  // Spinner lifecycle invariant: hide any stale indicator at entry, show
  // ours, and hide in `finally` so every exit path is covered.
  hidePrLoading();
  if (active()) showPrLoading(`Loading page ${page + 1}\u2026`);
  try {
    // Serve from the next-page prefetch when it has already landed for
    // this exact logic/query/page; otherwise fetch for real. A failed
    // prefetch resolves to null and falls through to the real fetch,
    // which surfaces the error.
    let data = null;
    try {
      data = await consumePrPrefetch(page);
      if (!data) {
        data = await api(`/pairing/report/?${reportPageParams(logic, page + 1, pairsState.pageSize)}`, { signal });
      }
    } catch (e) {
      if (e && e.name !== "AbortError") {
        hidePrLoading();
        banner("error", `Failed to load pairs: ${esc(e.message)}`, true);
      }
      return;
    }
    // Only the active load may mutate shared state: a superseded load's
    // page must not paint over the newer load's rows.
    if (!active()) return;
    pairsState.rows = data.rows;
    pairsState.total = data.total;
    pairsState.page = page;
    // DMP summary/QC warnings now arrive off the page path
    // (include_stats=false): a response that does carry them still wins.
    if (data.summary) pairsState.dmpSummary = data.summary;
    if (data.warnings) pairsState.dmpWarnings = data.warnings;
    renderPrGroups();
    renderPairsTable();
    if (page === 0) loadPrOverviewStats();
    // The page is on screen: quietly fetch N+1 so "Next" is instant.
    prefetchNextPrPage(page);
    // ...and fetch the DMP QC extras in the background (cached per logic).
    loadDmpReportStats(logic);
  } finally {
    // Invariant: whenever THIS load is still the newest, its spinner is
    // hidden no matter how the body exited — completion, a fetch error,
    // or a superseded early return.
    if (active()) hidePrLoading();
  }
}

/* HARMONY view: server-paginated on its own endpoint, mirroring the DMP
 * report's page flow. GET /pairing/harmony/?page=&page_size=&q= returns ONE
 * page of the normal-oriented frame plus the whole-frame numbers
 * (total/matched/unmatched/prev_normal_sample): the frame is never fetched,
 * filtered or held client-side (it used to be ~169k rows in one response,
 * which is what froze the tab). Same prefetch slot and abort/spinner
 * lifecycle as loadReportPage. */
async function loadHarmonyPage(page) {
  const logic = pairsState.logic;
  if (!logic) return;
  if (pairsAbort) pairsAbort.abort();
  pairsAbort = new AbortController();
  const signal = pairsAbort.signal;
  const active = () => signal === pairsAbort.signal;
  hidePrLoading();
  if (active()) showPrLoading(page === 0 ? "Loading HARMONY pairs\u2026" : `Loading HARMONY page ${page + 1}\u2026`);
  try {
    let data = null;
    try {
      data = await consumePrPrefetch(page);
      if (!data) {
        data = await api(`/pairing/harmony/?${harmonyPageParams(page + 1, pairsState.pageSize)}`, { signal });
      }
    } catch (e) {
      if (e && e.name !== "AbortError") {
        hidePrLoading();
        banner("error", `Failed to load pairs: ${esc(e.message)}`, true);
      }
      return; // superseded, or the fetch failed: either way, nothing to paint
    }
    if (!active()) return;
    // Same state shape as the report path: rows is the loaded PAGE (not the
    // full frame) and total the searched frame's size; the frame-wide counts
    // ride in harmonyStats for the count line and the page-boundary divider.
    pairsState.rows = data.rows;
    pairsState.total = data.total;
    pairsState.page = page;
    pairsState.harmonyStats = {
      matched: data.matched,
      unmatched: data.unmatched,
      prevNormal: data.prev_normal_sample ?? null,
    };
    renderPrGroups();
    renderHarmonyTable();
    if (page === 0) loadPrOverviewStats();
    // The page is on screen: quietly fetch N+1 so "Next" is instant.
    prefetchNextPrPage(page);
  } finally {
    if (active()) hidePrLoading();
  }
}

async function loadPairs() {
  const logic = viewLogic;
  if (!logic) return;
  // A fresh load (init, tab re-entry, logic change, post-pairing refresh)
  // starts at page 1; any cached prefetch belongs to the previous
  // logic/query and is dropped.
  invalidatePrPrefetch();
  pairsState.logic = logic;
  // The previous logic's server count is stale until the new one lands.
  pairsState.pairedCount = null;
  // Same for the DMP extras: the previous logic's summary/warnings must
  // not linger when a new (or non-DMP) logic is selected.
  pairsState.dmpSummary = null;
  pairsState.dmpWarnings = null;
  // Same for the HARMONY frame counts: the previous load's matched/unmatched
  // must not linger over a new logic, and a post-pairing refresh must show
  // the frame's new numbers rather than the old ones.
  pairsState.harmonyStats = null;
  await loadReportPage(0);
}

/* Next-page prefetch: after a page paints, quietly fetch page N+1 and
 * cache it so "Next" renders instantly. One slot, keyed by
 * logic|query|page — a logic change or a search (which call
 * invalidatePrPrefetch) can never serve a stale page, and a settled but
 * superseded result is simply dropped. Prefetch failures are silent: a
 * real page turn re-fetches and surfaces the error. */
let prPrefetch = null; // { key, page, abort, promise, data }
function prPrefetchKey() {
  return `${pairsState.logic}|${pairsState.query}|${pairsState.pageSize}`;
}
function invalidatePrPrefetch() {
  if (prPrefetch) {
    prPrefetch.abort.abort(); // cancel the in-flight fetch, if any
    prPrefetch = null;
  }
}
function prefetchNextPrPage(page) {
  const maxPage = Math.max(0, Math.ceil(pairsState.total / pairsState.pageSize) - 1);
  const next = page + 1;
  if (next > maxPage) return; // already on the last page
  const key = `${prPrefetchKey()}|${next}`;
  if (prPrefetch && prPrefetch.key === key) return; // in flight or cached
  invalidatePrPrefetch();
  const ctrl = new AbortController();
  const entry = { key, page: next, abort: ctrl, data: null, promise: null };
  // HARMONY prefetches from the harmony endpoint (same page contract, its own
  // row shape); every other logic from the report endpoint.
  const url = isHarmonyLogic(pairsState.logic)
    ? `/pairing/harmony/?${harmonyPageParams(next + 1, pairsState.pageSize)}`
    : `/pairing/report/?${reportPageParams(pairsState.logic, next + 1, pairsState.pageSize)}`;
  entry.promise = api(url, { signal: ctrl.signal })
    .then((data) => {
      if (prPrefetch === entry) entry.data = data; // else superseded: drop
      return data;
    })
    .catch(() => null); // silent; a real turn re-fetches
  prPrefetch = entry;
}
async function consumePrPrefetch(page) {
  if (!prPrefetch || prPrefetch.page !== page) return null;
  if (prPrefetch.key !== `${prPrefetchKey()}|${page}`) return null;
  if (prPrefetch.data) return prPrefetch.data;
  return await prPrefetch.promise; // null on silent failure → caller re-fetches
}

/* Export the FULL searched set from the server (GET /pairing/export/), not
 * just the page the browser holds — server-side pagination means the client
 * only ever has one page. The server owns the merge, the search filter (q)
 * and the checked column set; the response streams to disk via a blob. */
async function downloadPairs() {
  const logic = pairsState.logic;
  if (!logic) return;
  // HARMONY: the server-generated CSV (the checked harmony columns, full
  // normal-oriented frame) — see downloadHarmonyCsv. The /pairing/export/
  // path below is the pair-table column model and does not apply to
  // HARMONY's own column set.
  if (isHarmonyLogic(logic)) {
    downloadHarmonyCsv();
    return;
  }
  const cols = selectedPairColumns();
  if (cols.length === 0) return;
  const tsv = $("#pr-format").value === "tsv";
  const params = new URLSearchParams({
    pairing_logic_name: logic,
    fmt: tsv ? "tsv" : "csv",
    columns: cols.map((c) => c.key).join(","),
  });
  const q = (pairsState.query || "").trim().toLowerCase();
  if (q) params.set("q", q);
  const btn = $("#pr-download");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(API + "/pairing/export/?" + params);
    let detail = null;
    if (!res.ok) {
      try { const b = await res.json(); detail = b && b.detail; } catch (_) { /* not JSON */ }
      if (res.status === 401) location.href = "/login";
      else if (res.status === 403) location.href = "/login?error=" + encodeURIComponent(detail || "Not allowed");
      throw new Error((typeof detail === "string" ? detail : JSON.stringify(detail)) || `Export failed: ${res.status}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `pairs_${logic.replace(/[^\w.-]+/g, "_")}.${tsv ? "tsv" : "csv"}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    banner("error", `Export failed: ${esc(e.message)}`, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* HARMONY download: GET /pairing/harmony/?format=csv — the server-generated
 * CSV attachment (ticket 2/4), streamed straight to the browser (a plain
 * anchor navigation — cookie auth, like every other request here; the
 * server's Content-Disposition supplies the filename). Like the report's
 * export, it follows the active SEARCH query instead of the loaded page: the
 * file is the full searched frame, never just what the tab happens to show.
 * The CHECKED harmony columns travel as the `columns` param (endpoint order)
 * so the file contains exactly the columns the table shows; an all-checked
 * selection omits the param and gets the endpoint's default all-8 header. */
function downloadHarmonyCsv() {
  const cols = selectedPairColumns();
  if (cols.length === 0) return;
  const params = new URLSearchParams({ format: "csv" });
  if (pairsState.query) params.set("q", pairsState.query);
  if (cols.length < HARMONY_COLUMNS.length) params.set("columns", cols.map((c) => c.key).join(","));
  const a = document.createElement("a");
  a.href = `${API}/pairing/harmony/?${params}`;
  a.download = "harmony_pairs.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* Overview-header blurb for the selected logic: the strategy's explainer
 * from TRY_EXPLAINERS (same copy as the Try tab), with a short default
 * line naming the logic when the strategy has no entry. */
function renderPrLogicExplainer() {
  const el = $("#pr-logic-explain");
  const logicName = activeLogicName();
  if (!el || !logicName) return;
  const logic = PAIRING_LOGICS.find((l) => l.name === logicName);
  const explainer = logic && TRY_EXPLAINERS[logic.strategy_name];
  el.innerHTML = explainer || `<p>${esc(logicName)}: no explainer available for this logic.</p>`;
}

/* Counts strip for the selected logic (#pr-overview-stats). Non-DMP
 * logics: Paired / Unpaired from the per-logic unmatched-count endpoint
 * (Paired = the logic's latest report rows with both sides present,
 * Unpaired = its unpaired population). DMP: the reference's 3 summary
 * counts from the report response's summary field (computed over the
 * FULL pair frame, independent of page and search). */
async function loadPrOverviewStats() {
  const el = $("#pr-overview-stats");
  const logic = activeLogicName();
  if (!el || !logic) return;
  if (isDmpLogic(logic)) {
    const s = pairsState.dmpSummary;
    el.innerHTML = s
      ? `<span class="pr-stat"># of patients <strong>${s.patients}</strong></span>` +
        `<span class="pr-stat"># of Tumor-Normal pairs <strong>${s.tn_pairs}</strong></span>` +
        `<span class="pr-stat"># of unmatched tumors <strong>${s.unmatched_tumors}</strong></span>`
      : `<span class="pr-stat">summary <strong>…</strong></span>`;
    return;
  }
  // HARMONY is deliberately NOT on the DMP summary path (isDmpLogic()
  // excludes it): the per-logic unmatched-count endpoint already answers
  // for it (Paired = HARMONY's latest rows with both sides present,
  // Unpaired = its unpaired samples), so no client-side count is needed.
  const paint = (pairedHtml, unpairedHtml) => {
    el.innerHTML =
      `<span class="pr-stat">Paired ${pairedHtml}</span>` +
      `<span class="pr-stat">Unpaired ${unpairedHtml}</span>`;
  };
  paint("<strong>…</strong>", "<strong>…</strong>");
  try {
    const r = await api(`/pairing/unmatched-count/?pairing_logic_name=${encodeURIComponent(logic)}`);
    if (activeLogicName() === logic) {
      // The server count also drives #pr-count's "matched" figure.
      pairsState.pairedCount = r.paired;
      renderPairsTable();
      paint(`<strong>${r.paired}</strong>`, `<strong>${r.unpaired}</strong>`);
    }
  } catch (e) {
    if (activeLogicName() === logic) paint("<strong>—</strong>", "<strong>—</strong>");
  }
}

/* The old #pr-logic per-tab change handler is gone: a View change runs the
 * identical sequence via setViewLogic() → rerenderPairingTabs(). */
$("#pr-download").addEventListener("click", downloadPairs);
$("#pr-prev").addEventListener("click", () => {
  if (pairsState.page > 0) loadReportPage(pairsState.page - 1);
});
$("#pr-next").addEventListener("click", () => {
  const maxPage = Math.max(0, Math.ceil(pairsState.total / pairsState.pageSize) - 1);
  if (pairsState.page < maxPage) loadReportPage(pairsState.page + 1);
});
/* Skip-to-page: type a number (1-based) and press Enter, or let the
 * input commit on blur/change. Out-of-range values clamp to
 * [1, totalPages]; renderPairsTable() re-syncs the input and Prev/Next. */
function goPrPage() {
  const totalPages = Math.max(1, Math.ceil(pairsState.total / pairsState.pageSize));
  const target = parseInt($("#pr-page").value, 10);
  if (Number.isNaN(target)) return; // blank input: next render re-syncs it
  const page = Math.min(totalPages - 1, Math.max(0, target - 1));
  // Same page (or a blank/clamped-identical value): just re-sync the input,
  // no server round-trip.
  if (page === pairsState.page) {
    renderPairsTable();
  } else {
    loadReportPage(page);
  }
}
$("#pr-page").addEventListener("change", goPrPage);
$("#pr-page").addEventListener("keydown", (e) => {
  if (e.key === "Enter") goPrPage();
});
/* Clear the Pair Report search box and its state. Called when the view is
 * entered/left (see showView) and when the pairing logic changes, so a
 * stale filter never carries into a new logic's rows. */
function resetPrSearch() {
  const box = $("#pr-search");
  if (box && box.value) box.value = "";
  if (pairsState.query) {
    pairsState.query = "";
    // The query change voids any cached prefetch (its key includes the
    // query). The page index is a SERVER page, not a position in a filtered
    // set, so it is left alone — the currently loaded page stays loaded.
    invalidatePrPrefetch();
  }
}
/* View-scoped search: send the query to the server (the report's and the
 * harmony frame's endpoints both take ?q=)
 * so it filters the FULL merged list, not just the loaded page. Debounced so
 * typing doesn't issue a request per keystroke. A new/changed query resets
 * to page 1 and refetches; #pr-count / #pr-page-of then reflect the server's
 * filtered total (the same renderPairsTable path as any page fetch). */
let prSearchTimer = null;
$("#pr-search").addEventListener("input", () => {
  clearTimeout(prSearchTimer);
  prSearchTimer = setTimeout(() => {
    const q = $("#pr-search").value.trim().toLowerCase();
    if (q === pairsState.query) return; // unchanged: nothing to refetch
    pairsState.query = q;
    invalidatePrPrefetch(); // cached prefetch was fetched for the old query
    loadReportPage(0); // server-side search starts at page 1
  }, 250);
});
document.querySelector('.tab[data-tab="pairs"]').addEventListener("click", loadPairs);
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

/* Identifier links in the Pair Report table (all three column sets):
 * - sample links navigate to the Samples tab (card t_d8720219): the value
 *   is resolved to the full record from pairsState.rows when the frame
 *   carries one (PAIR/DMP rows) and handed to navigateToSampleInSamples;
 *   HARMONY rows carry names only, so the value is used as the search term.
 *   The Samples listing's query state is reset before the prefilled search
 *   runs, so a stale advanced filter never hides the link target.
 *   (Pair Report sample links deliberately land on the Samples listing, not
 *   on Matches & Overrides like row clicks on browse pages do.)
 * - patient/request links reuse the existing cross-view helpers, which
 *   reset the target tab's query state, prefill its search, and open the
 *   detail panel.
 * - DMP patient links resolve first (row's own patient__cmo, else a
 *   Samples-listing lookup) and no-op with a banner when no patient record
 *   matches. */
$("#pairs-table").addEventListener("click", (e) => {
  const el = e.target.closest("[data-goto-sample],[data-nav-patient],[data-nav-request],[data-nav-dmp-patient]");
  if (!el) return;
  if (el.dataset.navPatient != null) return navigateToPatient(el.dataset.navPatient);
  if (el.dataset.navRequest != null) return navigateToRequest(el.dataset.navRequest);
  if (el.dataset.navDmpPatient != null) return navigateToDmpPatient(el.dataset.navDmpPatient, el.dataset.navCmo || null);
  if (el.dataset.gotoSample != null) {
    const v = el.dataset.gotoSample;
    const row = pairsState.rows.find(
      (r) => (r.tumor && String(r.tumor.id) === v) || (r.normal && String(r.normal.id) === v)
    );
    const sample = row && ((row.tumor && String(row.tumor.id) === v ? row.tumor : null)
      || (row.normal && String(row.normal.id) === v ? row.normal : null));
    return navigateToSampleInSamples(sample || v);
  }
});

renderPrGroups();
applyHarmonyViewMode();

/* ---------- try pairing view (dry run) ---------- */

/* Read-only preview: resolve a sample by primary_id / cmo_sample_name,
 * pick a pairing logic, and show what the pairing cascade *would* do.
 * Only GET endpoints are used (sample lookup + possible-matches) — no
 * pairing row is ever created, updated, or overridden. */

const tryState = { sample: null, candidates: [], resultsTotal: null, busy: false, baitLogic: null };

function tryRenderResults() {
  renderSampleResults(
    "tq",
    tryState.candidates,
    tryState.sample && tryState.sample.id,
    tryState.resultsTotal,
    (sample) => selectTrySample(sample)
  );
}

async function trySearch() {
  const q = $("#tq-id").value.trim();
  if (!q || tryState.busy) return;
  tryState.busy = true;
  $("#tq-lookup").disabled = true;
  try {
    const { res, body } = await apiRes(`/sample/lookup?q=${encodeURIComponent(q)}&limit=${SAMPLE_PICK_LIMIT}`);
    const rows = Array.isArray(body) ? body : [];
    tryState.candidates = rows;
    tryState.resultsTotal = totalFromHeaders(res); // lookup sends no X-Total-Count: null
    if (!rows.length) {
      tryState.sample = null;
      renderSelectedBar("tq", null);
      $("#tq-run").disabled = true;
      tryRenderResults();
      $("#tq-summary").classList.add("hidden");
      $("#tq-result").classList.add("hidden");
      banner("info", `No sample matches "${esc(q)}" (primary_id / cmo_sample_name).`);
      return;
    }
    tryRenderResults();
    const first = rows[0];
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
  renderSelectedBar("tq", sample);
  tryRenderResults();
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
  // The dry run always uses the View's logic; the sample's own bait-set
  // logic only feeds the non-blocking mismatch hint (spec update 2).
  tryState.baitLogic = await lookupBaitSetLogic(sample);
  renderLogicMismatch("tq", tryState.baitLogic);
  renderTryExplainer(viewLogic);
}

async function runTry() {
  const sample = tryState.sample;
  const logic = viewLogic; // the View IS the logic (no per-tab selector)
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
// No #tq-logic listener: the dry run always follows the topbar View
// (rerenderPairingTabs re-runs it when the View changes).
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
  // Resolve the session View: saved localStorage choice → server default
  // (if active) → unselected (pairing tabs gated). Don't persist an
  // auto-resolved default — only an explicit user pick is saved, so a
  // first-visit View keeps following the server default until Allan picks.
  viewLogic = await resolveInitialViewLogic();
  applyViewState();
  if (viewLogic) {
    pairsState.logic = viewLogic;
    renderPrGroups();
    renderPrLogicExplainer();
    applyHarmonyViewMode();
    renderTryExplainer(viewLogic);
    loadPairs();
  }
  loadSamples(0);
  loadPatients();
  loadRequests();
  loadPooled(0);
  loadMachines();
  if (location.hash) restoreFromHash();
  else history.replaceState(null, "", "#/samples");
})();
