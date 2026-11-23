"use strict";
/* Daily Setter & Closer Performance Report — dashboard (no build step). */

const $ = (sel, el = document) => el.querySelector(sel);
const view = () => $("#view");
let ME = null;
let STATUS = null;

// ------------------------------------------------------------------ helpers
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 0) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
let TTD = 1; // decimals shown for TT; set from the server
const fmtTT = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: TTD, maximumFractionDigits: TTD }));
const fmtTime = (iso) => iso ? new Date(iso).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit", timeZone: "Asia/Kolkata" }) : "—";
const fmtDT = (iso) => iso ? new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit", timeZone: "Asia/Kolkata" }) : "—";
const fmtDay = (d) => new Date(d + "T00:00:00").toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
const todayIST = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
const isAdmin = () => ME && ME.role === "admin";

function toast(msg, ms = 4000) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts,
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json", ...(opts.headers || {}) } : opts.headers });
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Please sign in."); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

const statusPill = (kind, text) => `<span class="pill ${kind}">${esc(text)}</span>`;
const callingPill = (s, msg) => ({
  ok: statusPill("ok", "FreJun data processed"),
  stale: statusPill("warn", "No new FreJun CSV since last report"),
  missing: statusPill("bad", "FreJun CSV missing"),
  invalid: statusPill("bad", "FreJun CSV invalid"),
}[s] || statusPill("neutral", s || "—"));
const bookingPill = (s) => ({
  ok: statusPill("ok", "HubSpot connected · data retrieved"),
  failed: statusPill("bad", "HubSpot retrieval failed"),
  not_configured: statusPill("warn", "HubSpot not connected"),
}[s] || statusPill("neutral", s || "—"));
const overallPill = (s) => ({ complete: statusPill("ok", "Report complete"), partial: statusPill("warn", "Partial report"), failed: statusPill("bad", "Automation failed") }[s] || "");

// ------------------------------------------------------------------ auth
function showLogin() { $("#shell").classList.add("hidden"); $("#login").classList.remove("hidden"); }
function showShell() {
  $("#login").classList.add("hidden"); $("#shell").classList.remove("hidden");
  $("#whoName").textContent = `${ME.name} · ${ME.role}`;
}
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    ME = await api("/api/login", { method: "POST", body: JSON.stringify({ name: f.get("name"), password: f.get("password") }) });
    $("#loginError").textContent = ""; showShell(); route();
  } catch (err) { $("#loginError").textContent = err.message; }
});
$("#logoutBtn").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); ME = null; showLogin(); });

// ------------------------------------------------------------------ router
const ROUTES = {
  "": renderReportHome, report: renderReportById, history: renderHistory, compare: renderCompare,
  trend: renderTrend, uploads: renderUploads, roster: renderRoster, logs: renderLogs, settings: renderSettings,
};
async function route() {
  if (!ME) return;
  const [name, ...args] = location.hash.replace(/^#\/?/, "").split("/");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active",
    a.dataset.route === (name || "report") || (name === "" && a.dataset.route === "report")));
  view().innerHTML = `<p class="muted">Loading…</p>`;
  try { await (ROUTES[name] || renderReportHome)(...args); }
  catch (err) { view().innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; }
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ report view
async function renderReportHome() {
  STATUS = await api("/api/status");
  TTD = STATUS.tt_decimals;
  const latest = await api("/api/reports/latest");
  if (!latest) return renderEmptyToday();
  return renderReport(latest);
}
async function renderReportById(id) { STATUS = STATUS || await api("/api/status"); renderReport(await api(`/api/reports/${id}`)); }

function renderEmptyToday() {
  const next = STATUS.next_runs && STATUS.next_runs[0];
  view().innerHTML = `
    <div class="page-head"><div><h1>Daily Setter &amp; Closer Performance Report</h1>
      <div class="meta"><span>${esc(fmtDay(STATUS.today))}</span></div></div></div>
    <div class="card empty"><h2>No reports yet</h2>
      <p>The first report is generated automatically${next ? ` at <b>${esc(fmtDT(next))}</b>` : ""}.
      Upload today's FreJun CSV before then so Calling Inputs can be filled.</p>
      <div class="row" style="justify-content:center">
        <a class="btn primary" href="#/uploads">Upload FreJun CSV</a>
        ${isAdmin() ? `<button class="btn" id="genNow">Generate a report now</button>` : ""}
      </div></div>`;
  const g = $("#genNow"); if (g) g.onclick = () => openGenerate();
}

async function renderReport(snap) {
  if (snap.tt_decimals !== undefined) TTD = snap.tt_decimals;
  const day = await api(`/api/reports?day=${snap.report_date}`);
  const c = snap.calling || {}, b = snap.booking || {};
  const ct = (c.totals || {}).setters_and_closers || {};
  const bt = b.totals || {};
  const callingOK = ["ok", "stale"].includes(snap.calling_status);
  const bookingOK = snap.booking_status === "ok";
  const posted = (snap.deliveries || []).filter((d) => d.status === "sent");

  const tabs = day.cycles.map((cy) => {
    const cur = cy.versions[0];
    const active = cy.hour === snap.cycle_hour;
    return cur ? `<a class="tab ${active ? "active" : ""}" href="#/report/${cur.id}"><span class="dot ${cur.status}"></span>${esc(cy.label)}</a>`
      : `<span class="tab" style="opacity:.55"><span class="dot"></span>${esc(cy.label)}</span>`;
  }).join("");
  const versions = (day.cycles.find((x) => x.hour === snap.cycle_hour) || { versions: [] }).versions;

  view().innerHTML = `
    <div class="page-head">
      <div>
        <h1>Daily Setter &amp; Closer Performance Report</h1>
        <div class="meta">
          <span>${esc(fmtDay(snap.report_date))}</span>
          <span>Reporting cycle: <b>till ${esc(snap.cycle_label)}</b></span>
          <span>Last updated: <b>${esc(fmtDT(snap.created_at))}</b></span>
          <span>Version ${snap.version}${versions.length > 1 ? ` of ${versions.length}` : ""} · ${esc(snap.trigger)}</span>
        </div>
      </div>
      <div class="row">
        <a class="btn" href="/api/reports/${snap.id}/download?format=xlsx">Download Excel</a>
        <a class="btn" href="/api/reports/${snap.id}/download?format=csv">CSV</a>
        ${isAdmin() ? `<button class="btn" id="regen">Re-run this cycle</button>
          ${STATUS && STATUS.slack_configured ? `<button class="btn" id="postSlack">${posted.length ? "Send correction to Slack" : "Post to Slack"}</button>` : ""}` : ""}
      </div>
    </div>
    <div class="tabs">${tabs}${versions.length > 1 ? `<select id="verSel" class="tab" style="padding:5px 10px">${versions.map((v) =>
      `<option value="${v.id}" ${v.id === snap.id ? "selected" : ""}>v${v.version} · ${esc(fmtTime(v.created_at))} · ${esc(v.trigger)}</option>`).join("")}</select>` : ""}</div>

    <div class="pills">
      ${overallPill(snap.status)} ${callingPill(snap.calling_status)} ${bookingPill(snap.booking_status)}
      ${c.source ? statusPill("info", `CSV uploaded · exported ${fmtTime(c.source.exported_at)}`) : ""}
      ${posted.length ? statusPill("ok", `Posted to Slack ${fmtTime(posted[posted.length - 1].sent_at)}`) : statusPill("neutral", "Not posted to Slack")}
    </div>
    ${snap.calling_message ? `<div class="notice ${snap.calling_status === "stale" ? "warn" : "bad"}"><b>Calling Inputs:</b> ${esc(snap.calling_message)}</div>` : ""}
    ${snap.booking_message ? `<div class="notice bad"><b>Booking Inputs:</b> ${esc(snap.booking_message)}</div>` : ""}

    <div class="kpis">
      <div class="kpi group-label">Calling performance · setters + closers</div>
      ${kpi("Total calls", callingOK ? num(ct.calls) : "—")}
      ${kpi("Total talk time", callingOK ? fmtTT(ct.tt_minutes) : "—", "minutes")}
      ${kpi("Connected calls", callingOK ? num(ct.connected) : "—")}
      ${kpi("Unique calls", callingOK ? num(ct.unique) : "—")}
      <div class="kpi group-label">Booking performance</div>
      ${kpi("Total bookings", bookingOK ? num(bt.bookings) : "—", "Meeting Booking - Daily")}
      ${kpi("Total CCs", bookingOK ? num(bt.cc) : "—", "CCs - today")}
    </div>

    <div class="grid two">
      <section class="card"><h2>Section A · Calling Inputs</h2>${callingOK ? callingTable(c) : `<div class="empty">No calling data for this report.</div>`}
        ${callingOK ? callingFooter(c) : ""}</section>
      <section class="card"><h2>Section B · Booking Inputs</h2>${bookingOK ? bookingTable(b) : `<div class="empty">No booking data for this report.</div>`}
        ${bookingOK ? bookingFooter(b, snap) : ""}</section>
    </div>
    ${issuesBlock(snap.issues)}`;

  const vs = $("#verSel"); if (vs) vs.onchange = () => (location.hash = `#/report/${vs.value}`);
  const rg = $("#regen"); if (rg) rg.onclick = () => openGenerate(snap.report_date, snap.cycle_hour);
  const ps = $("#postSlack"); if (ps) ps.onclick = async () => {
    if (!confirm(posted.length ? "This cycle was already posted. Send this version as a correction?" : "Post this report to Slack?")) return;
    try { const r = await api(`/api/reports/${snap.id}/deliver`, { method: "POST" }); toast(r.status === "sent" ? "Posted to Slack." : `Slack: ${r.detail}`); route(); }
    catch (e) { toast(e.message); }
  };
  view().querySelectorAll("[data-trace]").forEach((el) => el.onclick = () => showTrace(snap, el.dataset.trace, el.dataset.kind));
}

const kpi = (label, value, sub = "") => `<div class="kpi"><div class="label">${esc(label)}</div><div class="value">${value}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`;

function callingTable(c) {
  const head = `<tr><th>Setter Name</th><th class="num">Calls</th><th class="num sorted">TT (in min)</th><th class="num">Connected</th><th class="num">Unique Calls</th></tr>`;
  let body = "";
  for (const key of ["setter", "closer", "unclassified"]) {
    const sec = c.sections[key];
    if (!sec.rows.length) continue;
    body += `<tr class="section"><td colspan="5">${esc(sec.title)}${key === "unclassified" ? " — assign on the Roster page" : ""}</td></tr>`;
    body += sec.rows.map((r) => `<tr><td class="name"><span class="rank">${r.rank}</span>${esc(r.name)}${r.flags.length ? `<span class="flag" title="${esc(r.flags.join("; "))}">⚠ check</span>` : ""}</td>
      <td class="num">${num(r.calls)}</td><td class="num"><b>${fmtTT(r.tt_minutes)}</b></td><td class="num">${num(r.connected)}</td><td class="num">${num(r.unique)}</td></tr>`).join("");
    const t = sec.totals;
    body += `<tr class="total"><td>Total ${esc(sec.title)}</td><td class="num">${num(t.calls)}</td><td class="num">${fmtTT(t.tt_minutes)}</td><td class="num">${num(t.connected)}</td><td class="num">${num(t.unique)}</td></tr>`;
  }
  const g = c.totals.setters_and_closers;
  body += `<tr class="grand"><td>Setters + Closers</td><td class="num">${num(g.calls)}</td><td class="num">${fmtTT(g.tt_minutes)}</td><td class="num">${num(g.connected)}</td><td class="num">${num(g.unique)}</td></tr>`;
  return `<div class="table-wrap"><table>${head}${body}</table></div>`;
}

function callingFooter(c) {
  const hidden = c.hidden.no_activity_role_not_set.length;
  return `<p class="muted small" style="margin-top:10px">Source: <a href="/api/uploads/${c.source.upload_id}/raw">${esc(c.source.filename)}</a>
    · exported ${esc(fmtDT(c.source.exported_at))} · ${esc(c.source.tt_rule)}
    ${hidden ? `· ${hidden} people with no calls and no role are hidden.` : ""}</p>`;
}

function bookingTable(b) {
  const head = `<tr><th>Setter Name</th><th class="num sorted">Bookings</th><th class="num">CC</th></tr>`;
  const rows = b.rows.map((r) => {
    const flags = [r.zero_filled ? `<span class="flag" style="color:var(--muted)" title="On the roster as a setter; HubSpot returned nothing for them today.">0 in HubSpot</span>` : "",
      !r.name_found ? `<span class="flag">name missing in HubSpot</span>` : "",
      r.role === "unclassified" && r.hubspot_value ? `<span class="flag" title="Not matched to the roster">not on roster</span>` : ""].join("");
    return `<tr><td class="name"><span class="rank">${r.rank}</span>${esc(r.name)}${flags ? `<span class="flags">${flags}</span>` : ""}</td>
    <td class="num"><b class="${r.bookings ? "clickable" : ""}" ${r.bookings ? `data-trace="${esc(r.hubspot_value || r.name)}" data-kind="bookings"` : ""}>${num(r.bookings)}</b></td>
    <td class="num"><span class="${r.cc ? "clickable" : ""}" ${r.cc ? `data-trace="${esc(r.hubspot_value || r.name)}" data-kind="cc"` : ""}>${num(r.cc)}</span></td></tr>`;
  }).join("");
  const t = b.totals;
  return `<div class="table-wrap"><table>${head}${rows}<tr class="grand"><td>Total</td><td class="num">${num(t.bookings)}</td><td class="num">${num(t.cc)}</td></tr></table></div>`;
}

function bookingFooter(b, snap) {
  const m = b.meta, t = b.totals;
  const note = t.bookings !== t.bookings_row_sum ? ` Total counts each contact once; rows add up to ${t.bookings_row_sum} because a contact can sit under two setters.` : "";
  return `<p class="muted small" style="margin-top:10px">Source: HubSpot, counted ${esc(fmtTime(m.window_start))}–${esc(fmtTime(m.window_end))} IST ·
    Bookings = ${esc(m.source_reports.bookings)} · CC = ${esc(m.source_reports.cc)} · Click a number to see the deals behind it.${esc(note)}</p>`;
}

function showTrace(snap, key, kind) {
  const r = snap.booking.rows.find((x) => (x.hubspot_value || x.name) === key);
  if (!r) return;
  const portal = snap.hubspot_portal_id;
  const ids = kind === "cc" ? r.cc_deal_ids : r.booking_deal_ids;
  const links = ids.map((id) => `<a target="_blank" rel="noopener" href="https://app.hubspot.com/contacts/${esc(portal)}/record/0-3/${esc(id)}">Deal ${esc(id)}</a>`).join("<br>");
  const contacts = kind === "bookings" ? `<h3 style="margin-top:12px">Contacts counted (${r.booking_contact_ids.length})</h3><div class="trace">${r.booking_contact_ids.map((id) =>
    `<a target="_blank" rel="noopener" href="https://app.hubspot.com/contacts/${esc(portal)}/record/0-1/${esc(id)}">Contact ${esc(id)}</a>`).join("<br>")}</div>` : "";
  modal(`<h2>${esc(r.name)} · ${kind === "cc" ? "CC" : "Bookings"}</h2>
    <p class="muted">These HubSpot records make up the number shown.</p>
    <h3>Deals (${ids.length})</h3><div class="trace">${links || "—"}</div>${contacts}`);
}

function issuesBlock(issues) {
  if (!issues || !issues.length) return "";
  return `<section class="card" style="margin-top:16px"><details ${issues.some((i) => i.level !== "info") ? "open" : ""}>
    <summary>Checks and warnings (${issues.length})</summary>
    <ul class="issues">${issues.map((i) => `<li class="${esc(i.level)}"><b>${esc(i.module)}:</b> ${esc(i.message)}</li>`).join("")}</ul></details></section>`;
}

function modal(html) {
  const back = document.createElement("div"); back.className = "modal-back";
  back.innerHTML = `<div class="modal">${html}<div class="row" style="margin-top:16px;justify-content:flex-end"><button class="btn" data-close>Close</button></div></div>`;
  back.onclick = (e) => { if (e.target === back || e.target.dataset.close !== undefined) back.remove(); };
  document.body.appendChild(back);
  return back;
}

function openGenerate(dateStr, hour) {
  const opts = (STATUS ? STATUS.cycles : []).map((c) => `<option value="${c.hour}" ${c.hour === hour ? "selected" : ""}>${esc(c.label)}</option>`).join("");
  const m = modal(`<h2>Generate a report</h2>
    <p class="muted">Makes a new version for that cycle. Earlier versions stay in history. HubSpot is counted up to the cycle time (or now, if earlier).</p>
    <div class="grid" style="grid-template-columns:1fr 1fr">
      <label>Date<input type="date" id="gDate" value="${esc(dateStr || todayIST())}" max="${todayIST()}"></label>
      <label>Cycle<select id="gHour">${opts}</select></label></div>
    ${STATUS && STATUS.slack_configured ? `<label style="display:flex;gap:8px;align-items:center;margin-top:12px;font-weight:400"><input type="checkbox" id="gPost"> Also post it to Slack</label>` : ""}
    <div class="row" style="margin-top:14px"><button class="btn primary" id="gGo">Generate</button></div>`);
  $("#gGo", m).onclick = async () => {
    $("#gGo", m).disabled = true; $("#gGo", m).textContent = "Working…";
    try {
      const s = await api("/api/reports/generate", { method: "POST", body: JSON.stringify({
        date: $("#gDate", m).value, hour: Number($("#gHour", m).value), post_to_slack: !!($("#gPost", m) || {}).checked }) });
      m.remove(); location.hash = `#/report/${s.id}`; toast(`Report v${s.version} generated (${s.status}).`);
    } catch (e) { toast(e.message); $("#gGo", m).disabled = false; $("#gGo", m).textContent = "Generate"; }
  };
}

// ------------------------------------------------------------------ history
async function renderHistory(dateArg) {
  STATUS = STATUS || await api("/api/status");
  const d = dateArg || todayIST();
  const [day, dates] = await Promise.all([api(`/api/reports?day=${d}`), api("/api/dates")]);
  view().innerHTML = `
    <div class="page-head"><div><h1>History</h1><div class="meta">Every cycle is stored separately; re-runs add versions and never replace earlier reports.</div></div>
      <div class="row"><input type="date" id="hDate" value="${esc(d)}" max="${todayIST()}"><a class="btn" href="/api/days/${esc(d)}/download">Download the day (Excel)</a></div></div>
    <section class="card"><h2>${esc(fmtDay(d))}${day.working_day ? "" : " · not a working day"}</h2>
      <table><tr><th>Cycle</th><th>Versions</th><th>Status</th><th>Slack</th><th></th></tr>
      ${day.cycles.map((cy) => {
        const cur = cy.versions[0];
        return `<tr><td><b>${esc(cy.label)}</b></td>
          <td>${cy.versions.length ? cy.versions.map((v) => `<a href="#/report/${v.id}">v${v.version}</a> <span class="muted small">${esc(fmtTime(v.created_at))} · ${esc(v.trigger)}</span>`).join("<br>") : `<span class="muted">No report</span>`}</td>
          <td>${cur ? overallPill(cur.status) : ""}</td>
          <td>${cy.versions.some((v) => v.posted) ? statusPill("ok", "Posted") : statusPill("neutral", "—")}</td>
          <td>${cur ? `<a class="btn sm" href="/api/reports/${cur.id}/download?format=xlsx">Excel</a>` : ""}</td></tr>`;
      }).join("")}</table>
      <div class="row" style="margin-top:12px"><a class="btn sm" href="#/compare/${esc(d)}">Compare cycles</a><a class="btn sm" href="#/trend/${esc(d)}">Through the day</a></div>
    </section>
    <section class="card" style="margin-top:16px"><h2>Dates with reports</h2>
      <div class="row">${dates.length ? dates.map((x) => `<a class="tab ${x === d ? "active" : ""}" href="#/history/${x}">${esc(new Date(x + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short", weekday: "short" }))}</a>`).join("") : `<span class="muted">None yet.</span>`}</div></section>`;
  $("#hDate").onchange = (e) => (location.hash = `#/history/${e.target.value}`);
}

// ------------------------------------------------------------------ compare
async function renderCompare(dateArg, fromH, toH) {
  STATUS = STATUS || await api("/api/status");
  const d = dateArg || todayIST();
  const day = await api(`/api/reports?day=${d}`);
  const avail = day.cycles.filter((c) => c.versions.length);
  const hours = day.cycles.map((c) => c.hour);
  const pairs = hours.slice(0, -1).map((h, i) => [h, hours[i + 1]]);
  let a = Number(fromH), b = Number(toH);
  if (!a || !b) {
    const usable = pairs.filter(([x, y]) => avail.some((c) => c.hour === x) && avail.some((c) => c.hour === y));
    [a, b] = usable.length ? usable[usable.length - 1] : pairs[0] || [0, 0];
  }
  const ca = day.cycles.find((c) => c.hour === a), cb = day.cycles.find((c) => c.hour === b);
  const label = (h) => (day.cycles.find((c) => c.hour === h) || {}).label;
  let body = `<div class="card empty">Both cycles need a report to compare. ${!avail.length ? "There are no reports on this date." : ""}</div>`;
  if (ca && cb && ca.versions.length && cb.versions.length) {
    const cmp = await api(`/api/compare?a=${ca.versions[0].id}&b=${cb.versions[0].id}`);
    body = compareBody(cmp);
  }
  view().innerHTML = `
    <div class="page-head"><div><h1>Compare cycles</h1><div class="meta">${esc(fmtDay(d))}</div></div>
      <input type="date" id="cDate" value="${esc(d)}" max="${todayIST()}"></div>
    <div class="tabs">${pairs.map(([x, y]) => `<a class="tab ${x === a && y === b ? "active" : ""}" href="#/compare/${d}/${x}/${y}">${esc(label(x))} vs ${esc(label(y))}</a>`).join("")}
      <a class="tab ${a === hours[0] && b === hours[hours.length - 1] ? "active" : ""}" href="#/compare/${d}/${hours[0]}/${hours[hours.length - 1]}">${esc(label(hours[0]))} vs ${esc(label(hours[hours.length - 1]))}</a></div>
    ${body}`;
  $("#cDate").onchange = (e) => (location.hash = `#/compare/${e.target.value}`);
}

const delta = (v, d = 0) => v === null || v === undefined ? `<span class="zero">—</span>` : v > 0 ? `<span class="pos">+${num(v, d)}</span>` : v < 0 ? `<span class="neg">${num(v, d)}</span>` : `<span class="zero">0</span>`;
function compareBody(cmp) {
  const t = cmp.totals;
  const tk = (k, label, d = 0) => kpi(label, `${t[k].to === null ? "—" : num(t[k].to, d)}`, `${t[k].from === null ? "—" : num(t[k].from, d)} → change ${t[k].delta === null ? "—" : (t[k].delta > 0 ? "+" : "") + num(t[k].delta, d)}`);
  const warn = [cmp.from, cmp.to].filter((x) => x.calling_status !== "ok" || x.booking_status !== "ok")
    .map((x) => `${x.label}: calling ${x.calling_status}, booking ${x.booking_status}`);
  return `${warn.length ? `<div class="notice warn">Some data was missing or stale in these reports, so changes may be understated. ${esc(warn.join(" · "))}</div>` : ""}
    <p class="muted">From <b>${esc(cmp.from.label)}</b> to <b>${esc(cmp.to.label)}</b>.</p>
    <div class="kpis">${tk("calls", "Calls")}${tk("tt_minutes", "Talk time (min)", TTD)}${tk("connected", "Connected")}${tk("unique", "Unique")}${tk("bookings", "Bookings")}${tk("cc", "CC")}</div>
    <div class="grid two even">
      <section class="card"><h2>Calling change (sorted by TT gained)</h2><div class="table-wrap"><table>
        <tr><th>Name</th><th class="num">Calls</th><th class="num">TT (min)</th><th class="num">TT +/-</th><th class="num">Connected +/-</th><th class="num">Unique +/-</th></tr>
        ${cmp.calling.map((r) => `<tr><td class="name">${esc(r.name)} <span class="muted small">${esc(r.role)}</span></td><td class="num">${delta(r.calls.delta)}</td>
          <td class="num">${fmtTT(r.tt_minutes.from)} → <b>${fmtTT(r.tt_minutes.to)}</b></td><td class="num">${delta(r.tt_minutes.delta, TTD)}</td>
          <td class="num">${delta(r.connected.delta)}</td><td class="num">${delta(r.unique.delta)}</td></tr>`).join("")}
      </table></div></section>
      <section class="card"><h2>Booking change (sorted by bookings gained)</h2><div class="table-wrap"><table>
        <tr><th>Setter Name</th><th class="num">Bookings</th><th class="num">+/-</th><th class="num">CC</th><th class="num">+/-</th></tr>
        ${cmp.booking.map((r) => `<tr><td>${esc(r.name)}</td><td class="num">${num(r.bookings.from)} → <b>${num(r.bookings.to)}</b></td><td class="num">${delta(r.bookings.delta)}</td>
          <td class="num">${num(r.cc.from)} → <b>${num(r.cc.to)}</b></td><td class="num">${delta(r.cc.delta)}</td></tr>`).join("")}
      </table></div></section></div>`;
}

// ------------------------------------------------------------------ trend
function spark(values) {
  const v = values.map((x) => (x === null || x === undefined ? null : Number(x)));
  const known = v.filter((x) => x !== null);
  if (known.length < 2) return "";
  const max = Math.max(...known, 1), w = 70, h = 20, step = w / (v.length - 1);
  const pts = v.map((x, i) => x === null ? null : `${(i * step).toFixed(1)},${(h - (x / max) * (h - 2) - 1).toFixed(1)}`).filter(Boolean).join(" ");
  return `<svg class="spark" width="${w}" height="${h}"><polyline points="${pts}" fill="none" stroke="#1f3a5f" stroke-width="1.6"/></svg>`;
}
async function renderTrend(dateArg) {
  const d = dateArg || todayIST();
  const t = await api(`/api/timeline?day=${d}`);
  const hs = t.cycles.map((c) => c.hour);
  const cell = (obj, h, dec = 0) => obj[h] === undefined ? `<span class="zero">—</span>` : num(obj[h], dec);
  const table = (title, key, dec, filter) => {
    const people = t.people.filter(filter).filter((p) => Object.keys(p[key]).length)
      .sort((a, b) => (b[key][hs[hs.length - 1]] ?? -1) - (a[key][hs[hs.length - 1]] ?? -1) || Math.max(...Object.values(b[key])) - Math.max(...Object.values(a[key])));
    return `<section class="card"><h2>${esc(title)}</h2>${people.length ? `<div class="table-wrap"><table>
      <tr><th>Name</th>${t.cycles.map((c) => `<th class="num">${esc(c.label)}</th>`).join("")}<th>Trend</th></tr>
      ${people.map((p) => `<tr><td>${esc(p.name)} <span class="muted small">${esc(p.role)}</span></td>${hs.map((h) => `<td class="num">${cell(p[key], h, dec)}</td>`).join("")}
        <td>${spark(hs.map((h) => p[key][h]))}</td></tr>`).join("")}</table></div>` : `<div class="empty">No data yet.</div>`}</section>`;
  };
  view().innerHTML = `
    <div class="page-head"><div><h1>Through the day</h1><div class="meta">${esc(fmtDay(d))} · cumulative values at each reporting time</div></div>
      <input type="date" id="tDate" value="${esc(d)}" max="${todayIST()}"></div>
    <div class="grid">
      ${table("Talk time (min) by cycle", "tt", TTD, (p) => true)}
      ${table("Bookings by cycle", "bookings", 0, (p) => true)}
      ${table("CC by cycle", "cc", 0, (p) => true)}
    </div>`;
  $("#tDate").onchange = (e) => (location.hash = `#/trend/${e.target.value}`);
}

// ------------------------------------------------------------------ uploads
async function renderUploads() {
  const ups = await api("/api/uploads");
  view().innerHTML = `
    <div class="page-head"><div><h1>FreJun uploads</h1>
      <div class="meta">Upload the "users-table" CSV exported from FreJun. Keep FreJun's file name: the export time is read from it.</div></div></div>
    ${isAdmin() ? `<section class="card" style="margin-bottom:16px">
      <label class="drop" id="drop"><b>Drop the FreJun CSV here</b><br><span class="muted">or click to choose the file</span><input type="file" id="file" accept=".csv,text/csv"></label>
      <details style="margin-top:10px"><summary>File was renamed? Set its export time</summary>
        <label style="max-width:300px;margin-top:8px">Exported at (IST)<input type="datetime-local" id="expAt"></label></details>
      <div id="upResult"></div></section>` : `<div class="notice info">Viewers can't upload. Ask an admin.</div>`}
    <section class="card"><h2>Recent uploads</h2>${ups.length ? `<div class="table-wrap"><table>
      <tr><th>File</th><th>For date</th><th>Exported</th><th>Uploaded</th><th class="num">People</th><th>Check</th></tr>
      ${ups.map((u) => `<tr><td><a href="/api/uploads/${u.id}/raw">${esc(u.filename)}</a></td><td>${esc(u.report_date)}</td>
        <td>${esc(fmtDT(u.exported_at))} <span class="muted small">(${esc(u.exported_at_source.replace("_", " "))})</span></td>
        <td>${esc(fmtDT(u.uploaded_at))} <span class="muted small">${esc(u.uploaded_by)}</span></td><td class="num">${u.people}</td>
        <td>${u.valid ? statusPill("ok", "Valid") : statusPill("bad", "Rejected")} ${u.issues.filter((i) => i.level !== "info").length ? `<span class="muted small">${u.issues.filter((i) => i.level !== "info").length} notes</span>` : ""}</td></tr>`).join("")}
    </table></div>` : `<div class="empty">No uploads yet.</div>`}</section>`;
  if (!isAdmin()) return;
  const drop = $("#drop"), file = $("#file");
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && doUpload(e.dataTransfer.files[0]));
  file.onchange = () => file.files[0] && doUpload(file.files[0]);
}

async function doUpload(f) {
  const fd = new FormData(); fd.append("file", f);
  const exp = $("#expAt").value; if (exp) fd.append("exported_at", exp + ":00+05:30");
  $("#upResult").innerHTML = `<p class="muted">Checking ${esc(f.name)}…</p>`;
  try {
    const u = await api("/api/uploads", { method: "POST", body: fd });
    const map = Object.entries(u.column_map).map(([k, v]) => `<tr><td>${esc({ name: "Setter Name", email: "Email", calls: "Calls", tt: "TT (in min)", connected: "Connected", unique: "Unique Calls" }[k] || k)}</td><td>← "${esc(v)}"</td></tr>`).join("");
    $("#upResult").innerHTML = `<div class="notice ${u.valid ? "info" : "bad"}" style="margin-top:12px">
      <b>${u.valid ? (u.duplicate_file ? "This exact file was already uploaded." : "CSV accepted.") : "CSV rejected."}</b>
      ${u.valid ? `${u.people} people · for ${esc(fmtDay(u.report_date))} · exported ${esc(fmtTime(u.exported_at))} · talk time format: ${esc(u.tt_format)}` : ""}
      ${u.issues.length ? `<ul class="issues">${u.issues.map((i) => `<li class="${esc(i.level)}">${esc(i.message)}</li>`).join("")}</ul>` : ""}</div>
      ${u.valid ? `<details><summary>Column mapping used</summary><table style="max-width:420px">${map}</table></details>` : ""}`;
    if (u.valid) toast("CSV uploaded. It will be used in the next report, or re-run a cycle now.");
    setTimeout(() => renderUploadsList(), 50);
  } catch (e) { $("#upResult").innerHTML = `<div class="notice bad" style="margin-top:12px">${esc(e.message)}</div>`; }
}
async function renderUploadsList() { const keep = $("#upResult").innerHTML; await renderUploads(); if ($("#upResult")) $("#upResult").innerHTML = keep; }

// ------------------------------------------------------------------ roster
async function renderRoster() {
  const people = await api("/api/roster");
  const roleSel = (p) => `<select data-id="${p.id}" data-field="role" ${isAdmin() ? "" : "disabled"}>${["setter", "closer", "unclassified"].map((r) =>
    `<option value="${r}" ${p.role === r ? "selected" : ""}>${r === "unclassified" ? "role not set" : r}</option>`).join("")}</select>`;
  const counts = (r) => people.filter((p) => p.role === r && p.active).length;
  view().innerHTML = `
    <div class="page-head"><div><h1>Roster</h1><div class="meta">Decides who is a setter or a closer. Report names always come from FreJun and HubSpot, not from here.
      People are matched by email first. New people in a CSV are added here automatically with no role.</div></div></div>
    <div class="pills">${statusPill("info", `${counts("setter")} setters`)} ${statusPill("info", `${counts("closer")} closers`)}
      ${counts("unclassified") ? statusPill("warn", `${counts("unclassified")} without a role`) : ""}</div>
    <section class="card"><div class="table-wrap"><table>
      <tr><th>Name</th><th>Email</th><th>Role</th><th>Team</th><th>HubSpot ID</th><th>Show in reports</th></tr>
      ${people.map((p) => `<tr style="${p.active ? "" : "opacity:.55"}"><td>${esc(p.name)}${p.notes ? `<div class="muted small">${esc(p.notes)}</div>` : ""}</td>
        <td class="small">${esc(p.email || "—")}</td><td>${roleSel(p)}</td>
        <td><input data-id="${p.id}" data-field="team" value="${esc(p.team)}" style="width:130px" ${isAdmin() ? "" : "disabled"}></td>
        <td class="small muted">${esc(p.hubspot_owner_id || "—")}</td>
        <td><input type="checkbox" data-id="${p.id}" data-field="active" ${p.active ? "checked" : ""} ${isAdmin() ? "" : "disabled"}></td></tr>`).join("")}
    </table></div></section>`;
  view().querySelectorAll("[data-field]").forEach((el) => el.onchange = async () => {
    const val = el.type === "checkbox" ? el.checked : el.value;
    try { await api(`/api/roster/${el.dataset.id}`, { method: "PATCH", body: JSON.stringify({ [el.dataset.field]: val }) }); toast("Saved. Applies to reports generated from now on."); }
    catch (e) { toast(e.message); }
  });
}

// ------------------------------------------------------------------ logs
async function renderLogs(level) {
  const rows = await api(`/api/logs${level ? `?level=${level}` : ""}`);
  view().innerHTML = `
    <div class="page-head"><div><h1>Activity log</h1><div class="meta">Every run, upload, retry, Slack post and error, with timestamps (IST).</div></div>
      <div class="tabs">${["", "error", "warning", "info"].map((l) => `<a class="tab ${l === (level || "") ? "active" : ""}" href="#/logs/${l}">${l || "all"}</a>`).join("")}</div></div>
    <section class="card">${rows.length ? rows.map((e) => `<div class="log-row"><span class="muted">${esc(fmtDT(e.at))}</span>
      <span class="lvl ${esc(e.level)}">${esc(e.level)}</span><span class="muted">${esc(e.module)}</span><span>${esc(e.message)}</span></div>`).join("") : `<div class="empty">Nothing logged.</div>`}</section>`;
}

// ------------------------------------------------------------------ settings / status
async function renderSettings() {
  STATUS = await api("/api/status");
  const s = STATUS;
  view().innerHTML = `
    <div class="page-head"><div><h1>Status &amp; setup</h1><div class="meta">${esc(fmtDT(s.now))} IST · ${s.working_day ? "working day" : "not a working day"}</div></div></div>
    <div class="grid two">
      <section class="card"><h2>Today's cycles</h2><table>
        <tr><th>Cycle</th><th>Report</th><th>Calling</th><th>Booking</th><th>Slack</th></tr>
        ${s.cycles.map((c) => `<tr><td><b>${esc(c.label)}</b></td>
          <td>${c.snapshot_id ? `<a href="#/report/${c.snapshot_id}">${esc(c.status)}</a>` : c.due ? statusPill("bad", "missing") : `<span class="muted">upcoming</span>`}</td>
          <td>${esc(c.calling_status || "—")}</td><td>${esc(c.booking_status || "—")}</td><td>${c.posted ? "posted" : "—"}</td></tr>`).join("")}
      </table>
      <p class="muted small">Schedule: ${esc(s.working_days)} · ${esc(s.timezone)} · waits up to ${s.csv_wait_minutes} min for a new CSV.<br>
        Next runs: ${s.next_runs.length ? s.next_runs.map((t) => esc(fmtDT(t))).join(", ") : "scheduler off"}</p></section>
      <section class="card"><h2>Connections</h2>
        <div class="pills" style="flex-direction:column;align-items:flex-start">
          ${s.hubspot_configured ? statusPill("ok", "HubSpot token set") : statusPill("bad", "HubSpot not connected (HUBSPOT_TOKEN)")}
          ${s.slack_configured ? statusPill("ok", `Slack set up · auto-post ${s.auto_deliver ? "on" : "off"}`) : statusPill("warn", "Slack not set up (optional)")}
          ${s.scheduler_enabled ? statusPill("ok", "Scheduler on") : statusPill("warn", "Scheduler off")}
          ${s.last_upload ? statusPill(s.last_upload.valid ? "info" : "bad", `Last CSV: ${s.last_upload.filename} · ${fmtDT(s.last_upload.uploaded_at)}`) : statusPill("warn", "No CSV uploaded yet")}
        </div>
        ${isAdmin() ? `<div class="row"><button class="btn" id="hsTest">Test HubSpot connection</button><button class="btn" id="genBtn">Generate a report now</button></div><p id="hsResult" class="small"></p>` : ""}
      </section>
      <section class="card"><h2>Needs attention</h2>
        ${s.unclassified.length ? `<p>${s.unclassified.length} people have no role: ${esc(s.unclassified.slice(0, 12).join(", "))}${s.unclassified.length > 12 ? "…" : ""}. <a href="#/roster">Set roles</a> (only people with calls are shown in reports).</p>` : `<p class="muted">Everyone on the roster has a role.</p>`}
        ${s.recent_errors.length ? `<h3>Recent errors</h3>${s.recent_errors.map((e) => `<div class="log-row"><span class="muted">${esc(fmtDT(e.at))}</span><span class="lvl error">error</span><span class="muted">${esc(e.module)}</span><span>${esc(e.message)}</span></div>`).join("")}` : `<p class="muted">No recent errors.</p>`}
      </section>
    </div>`;
  const t = $("#hsTest"); if (t) t.onclick = async () => {
    t.disabled = true; $("#hsResult").textContent = "Testing…";
    try { const r = await api("/api/hubspot/test", { method: "POST" }); $("#hsResult").innerHTML = r.ok ? `<span class="pos">${esc(r.message)}</span>` : `<span class="neg">${esc(r.message)}</span>`; }
    catch (e) { $("#hsResult").textContent = e.message; } t.disabled = false;
  };
  const g = $("#genBtn"); if (g) g.onclick = () => openGenerate();
}

// ------------------------------------------------------------------ boot
(async function boot() {
  try { ME = await api("/api/me"); showShell(); route(); } catch { showLogin(); }
})();
