// Dashboard front end. All numbers come from the backend; this file only draws them.
// Text from portals or the model is always inserted with esc(), never as raw HTML.

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => v == null ? "–" : v.toLocaleString(undefined, { style: "currency", currency: "USD" });
const num = (v) => v == null ? "" : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
const when = (iso) => iso ? new Date(iso).toLocaleString() : "–";

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-PA-Client": "1", ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail
    : (data.detail || []).map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ") || res.statusText);
  return data;
}

// --- tabs ---------------------------------------------------------------------
// Each tab has its own address (/dashboard, /inbox, /accounts, /debug), so a reload,
// a bookmark or the Back button opens the same tab.
const TABS = ["dashboard", "inbox", "accounts", "debug"];
function tabFromPath() {
  const name = location.pathname.replace(/^\/+|\/+$/g, "");
  return TABS.includes(name) ? name : "dashboard";
}
function showTab(name, push) {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === name));
  if (push && location.pathname !== "/" + name) history.pushState({ tab: name }, "", "/" + name);
  ({ dashboard: loadDashboard, inbox: loadInbox, accounts: loadAccounts, debug: loadDebug })[name]();
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab, true)));
window.addEventListener("popstate", () => showTab(tabFromPath(), false));

// --- dashboard ----------------------------------------------------------------
let chart;

// Part-owned accounts (an LLC): count your share, or the whole account. Remembered here.
let basis = "share";
try { basis = localStorage.getItem("basis") === "full" ? "full" : "share"; } catch {}
const setBasisButtons = () => document.querySelectorAll("#basis button")
  .forEach((b) => b.classList.toggle("on", b.dataset.b === basis));
setBasisButtons();
$("#basis").addEventListener("click", (e) => {
  const b = e.target.dataset.b; if (!b || b === basis) return;
  basis = b; try { localStorage.setItem("basis", b); } catch {}
  setBasisButtons(); loadDashboard();
});
const palette = ["#2f6fde", "#e8833a", "#3aa76d", "#a35bd6", "#d6455b", "#2bb3b1", "#8c8c3a", "#5f6b7a"];

function periodStart(p) {
  const d = new Date();
  if (p === "1M") d.setMonth(d.getMonth() - 1);
  else if (p === "3M") d.setMonth(d.getMonth() - 3);
  else if (p === "YTD") return `${d.getFullYear()}-01-01`;
  else if (p === "1Y") d.setFullYear(d.getFullYear() - 1);
  else if (p === "3Y") d.setFullYear(d.getFullYear() - 3);
  else return "";
  return d.toISOString().slice(0, 10);
}

// Chart settings are remembered in this browser, so a reload or another browser tab
// shows the chart the way you left it. Default: the total line only, all dates.
const CHART_KEY = "chartSettings";
function saveChartSettings() {
  const on = document.querySelector("#period button.on");
  const f = { period: on ? on.dataset.p : null, start: $("#start").value, end: $("#end").value,
    granularity: $("#granularity").value, group: $("#group").value, known_on: $("#known-on").value };
  try { localStorage.setItem(CHART_KEY, JSON.stringify(f)); } catch {}
}
function restoreChartSettings() {
  let f = null;
  try { f = JSON.parse(localStorage.getItem(CHART_KEY) || "null"); } catch {}
  f = f || {};
  const pick = (sel, v) => { if (v && [...$(sel).options].some((o) => o.value === v)) $(sel).value = v; };
  pick("#group", f.group || "none");
  pick("#granularity", f.granularity);
  $("#known-on").value = f.known_on || "";
  if (f.period || !f.start) {         // a period button: dates relative to today
    const p = f.period || "All";
    document.querySelectorAll("#period button").forEach((b) => b.classList.toggle("on", b.dataset.p === p));
    $("#start").value = periodStart(p); $("#end").value = "";
  } else {                            // dates you picked
    document.querySelectorAll("#period button").forEach((b) => b.classList.remove("on"));
    $("#start").value = f.start; $("#end").value = f.end || "";
  }
}

$("#period").addEventListener("click", (e) => {
  const p = e.target.dataset.p; if (!p) return;
  document.querySelectorAll("#period button").forEach((b) => b.classList.toggle("on", b === e.target));
  $("#start").value = periodStart(p); $("#end").value = "";
  saveChartSettings();
  loadChart();
});
["#start", "#end", "#granularity", "#group", "#known-on"].forEach((s) => $(s).addEventListener("change", () => {
  if (s === "#start" || s === "#end") document.querySelectorAll("#period button").forEach((b) => b.classList.remove("on"));
  saveChartSettings();
  loadChart();
}));

function clock(hhmm) {           // "20:15" -> "8:15 PM" in the browser's format
  const [h, m] = hhmm.split(":").map(Number);
  return new Date(2000, 0, 1, h, m).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function nextTime(iso) {         // local "YYYY-MM-DDTHH:MM" from the server
  if (!iso) return "–";
  const t = new Date(iso), today = new Date();
  const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
  const day = t.toDateString() === today.toDateString() ? "today"
    : t.toDateString() === tomorrow.toDateString() ? "tomorrow" : t.toLocaleDateString();
  return `${day} ${clock(iso.slice(11, 16))}`;
}
async function loadDaily() {
  const d = await api("/api/daily-refresh"), f = $("#daily-form");
  f.enabled.checked = d.enabled; f.time.value = d.time;
  $("#daily-status").textContent = d.enabled ? `Next: ${nextTime(d.next)}.` : "Off: Refresh runs only when you press it.";
}
$("#daily-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/daily-refresh", { method: "PUT", body: { enabled: f.enabled.checked, time: f.time.value } });
    await loadDaily();
    $("#daily-status").textContent = "Saved. " + $("#daily-status").textContent;
  } catch (err) { $("#daily-status").textContent = err.message; }
});

async function loadSummary() {
  const s = await api("/api/summary");
  const full = basis === "full" && s.has_part_owned;
  $("#basis-wrap").hidden = !s.has_part_owned;
  $("#net-worth").textContent = money(full ? s.net_worth_full : s.net_worth);
  $("#net-worth-note").textContent = (s.latest_retrieval ? `latest data retrieved ${when(s.latest_retrieval)}` : "no data yet")
    + (!s.has_part_owned ? "" : full ? ` · whole accounts; your share ${money(s.net_worth)}`
      : ` · part-owned accounts at your share; whole accounts ${money(s.net_worth_full)}`);
  const d = s.daily_refresh, n = s.last_daily;
  $("#daily-next").textContent = d.enabled ? `next ${nextTime(d.next)}` : "off";
  $("#daily-note").textContent = n ? `last ${when(n.finished_at || n.started_at)}: ${n.summary || n.status}`
    : (d.enabled ? `daily at ${clock(d.time)}; not run yet` : "turn it on under Accounts & logins");
  $("#kill-banner").hidden = !s.kill_switch;
  const badge = $("#inbox-badge");
  badge.hidden = !s.inbox_open; badge.textContent = s.inbox_open;
  const running = s.running.find((r) => r.kind === "refresh");
  if (running) watchRun(running.id);
  else if (s.last_refresh) $("#refresh-status").textContent =
    `last refresh ${when(s.last_refresh.finished_at || s.last_refresh.started_at)}: ${s.last_refresh.summary || s.last_refresh.status}`;
}

async function loadChart() {
  const q = new URLSearchParams();
  if ($("#start").value) q.set("start", $("#start").value);
  if ($("#end").value) q.set("end", $("#end").value);
  if ($("#known-on").value) q.set("known_on", $("#known-on").value);
  const group = $("#group").value;      // "none": the total line only
  q.set("granularity", $("#granularity").value); q.set("group", group === "none" ? "institution" : group);
  q.set("basis", basis);
  const d = await api("/api/series?" + q);
  $("#chart-empty").hidden = d.dates.length > 0;
  const sets = [{ label: "Total", data: d.total, borderColor: getComputedStyle(document.body).color,
    borderWidth: 2.5, pointRadius: 0, tension: 0.15, spanGaps: false }];
  if (group !== "none") Object.entries(d.series).forEach(([name, vals], i) => sets.push({
    label: name, data: vals, borderColor: palette[i % palette.length], borderWidth: 1.5,
    pointRadius: d.dates.length < 40 ? 2 : 0, tension: 0.15, carried: d.carried[name] || [] }));
  if (chart) chart.destroy();
  chart = new Chart($("#chart"), {
    type: "line", data: { labels: d.dates, datasets: sets },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: { y: { ticks: { callback: (v) => "$" + Number(v).toLocaleString() } } },
      plugins: { tooltip: { callbacks: { label: (ctx) => {
        const c = ctx.dataset.carried?.[ctx.dataIndex];
        return `${ctx.dataset.label}: ${money(ctx.raw)}${c ? ` (as of ${c})` : ""}`;
      } } } },
    },
  });
}

async function loadPositions() {
  const rows = await api("/api/positions");
  const tb = $("#positions tbody"); tb.innerHTML = "";
  for (const p of rows) {
    const tr = document.createElement("tr");
    tr.className = p.snapshot_id ? "clickable" : "";
    p.shown = basis === "full" ? p.full_total : p.total;
    const share = !p.share_pct ? "" : `<div class="sub">${basis === "full"
      ? `your ${num(p.share_pct)}%: ${money(p.total)}` : `${num(p.share_pct)}% of ${money(p.full_total)}`}</div>`;
    tr.innerHTML = `<td>${esc(p.account)}${p.owner_name ? `<div class="sub">${esc(p.owner_name)}</div>` : ""}</td>
      <td>${esc(p.login_name || "")}</td>
      <td><span class="pill ${esc(p.status)}">${esc(p.status.replace("_", " "))}</span></td>
      <td class="num">${money(p.shown)}${share}</td><td>${p.as_of_datetime ? when(p.as_of_datetime) : "–"}</td>
      <td>${when(p.retrieved_at)}</td>
      <td>${p.snapshot_id ? `<button class="link" data-src="${p.snapshot_id}">View Source</button>` : ""}</td>`;
    if (p.snapshot_id) tr.addEventListener("click", (e) => {
      if (e.target.dataset.src) return;
      showHoldings(p);
    });
    tr._p = p;
    tb.appendChild(tr);
  }
  const sel = $("#positions [data-f=status]"), cur = sel.value;   // statuses present now
  const statuses = [...new Set(rows.map((p) => p.status))].sort();
  sel.innerHTML = `<option value="">All</option>` + statuses.map((s) =>
    `<option value="${esc(s)}">${esc(s.replace("_", " "))}</option>`).join("");
  sel.value = statuses.includes(cur) ? cur : "";
  sortPositions();
  filterPositions();
}

// Sorting the Accounts table: click a heading for ascending, again for descending, a
// third time for the original order. Empty values always go last. Remembered per browser.
const POS_SORT_KEY = "positionSort";
let posSort = { key: "", dir: 1 };
try { posSort = JSON.parse(localStorage.getItem(POS_SORT_KEY) || "null") || posSort; } catch {}
const POS_SORT_VALUE = {
  account: (p) => p.account, login: (p) => p.login_name, status: (p) => p.status,
  value: (p) => p.shown, asof: (p) => p.as_of_datetime && Date.parse(p.as_of_datetime),
  retrieved: (p) => p.retrieved_at && Date.parse(p.retrieved_at),
};
function sortPositions() {
  const tb = $("#positions tbody");
  const rows = [...tb.querySelectorAll("tr")].filter((tr) => tr._p);
  rows.forEach((tr, i) => { if (tr._order == null) tr._order = i; });
  const get = POS_SORT_VALUE[posSort.key];
  rows.sort((a, b) => {
    if (!get) return a._order - b._order;
    const x = get(a._p), y = get(b._p);
    const ex = x == null || x === "" || Number.isNaN(x), ey = y == null || y === "" || Number.isNaN(y);
    if (ex || ey) return ex && ey ? a._order - b._order : ex ? 1 : -1;   // empty last
    const c = typeof x === "string" ? x.localeCompare(y, undefined, { sensitivity: "base", numeric: true }) : x - y;
    return c * posSort.dir || a._order - b._order;
  });
  rows.forEach((tr) => tb.appendChild(tr));
  document.querySelectorAll("#positions button.sort").forEach((b) => {
    const on = b.dataset.sort === posSort.key;
    const th = b.closest("th");
    if (on) th.setAttribute("aria-sort", posSort.dir > 0 ? "ascending" : "descending");
    else th.removeAttribute("aria-sort");
    b.title = on ? (posSort.dir > 0 ? "Sorted ascending; click for descending" : "Sorted descending; click for the original order")
      : "Sort by this column";
  });
}
document.querySelectorAll("#positions button.sort").forEach((b) => b.addEventListener("click", () => {
  const k = b.dataset.sort;
  posSort = posSort.key !== k ? { key: k, dir: 1 } : posSort.dir > 0 ? { key: k, dir: -1 } : { key: "", dir: 1 };
  try { localStorage.setItem(POS_SORT_KEY, JSON.stringify(posSort)); } catch {}
  sortPositions();
}));

// Column filters on the Accounts table. Value accepts >1000, <0, >=500, =0, 1000..5000
// (a plain number means "at least"). Dates filter by how recent they are.
const POS_FILTER_KEY = "positionFilters";
function valueTest(expr) {
  const t = expr.replace(/[$,\s]/g, "");
  if (!t) return () => true;
  let m = t.match(/^(-?\d*\.?\d+)\.\.(-?\d*\.?\d+)$/);
  if (m) { const a = +m[1], b = +m[2]; return (v) => v != null && v >= Math.min(a, b) && v <= Math.max(a, b); }
  m = t.match(/^(>=|<=|>|<|=)?(-?\d*\.?\d+)$/);
  if (!m) return null;                                   // not understood
  const n = +m[2];
  const cmp = { ">": (v) => v > n, "<": (v) => v < n, ">=": (v) => v >= n, "<=": (v) => v <= n,
                "=": (v) => Math.abs(v - n) < 0.005 }[m[1] || ">="];
  return (v) => v != null && cmp(v);
}
function ageTest(code) {
  if (!code) return () => true;
  const days = +code.slice(1), recent = code[0] === "d";
  return (iso) => {
    if (!iso) return !recent;                            // no date counts as old
    const age = (Date.now() - new Date(iso).getTime()) / 86400000;
    return recent ? age <= days : age > days;
  };
}
function filterPositions() {
  const f = {};
  document.querySelectorAll("#positions [data-f]").forEach((el) => { f[el.dataset.f] = el.value; });
  const vt = valueTest(f.value);
  $("#positions [data-f=value]").classList.toggle("bad", vt === null);
  const asof = ageTest(f.asof), got = ageTest(f.retrieved);
  const has = (hay, needle) => (hay || "").toLowerCase().includes(needle.trim().toLowerCase());
  let shown = 0, all = 0, total = 0;
  for (const tr of document.querySelectorAll("#positions tbody tr")) {
    const p = tr._p; if (!p) continue;
    all++;
    const ok = has(`${p.account} ${p.owner_name || ""}`, f.account) && has(p.login_name, f.login)
      && (!f.status || p.status === f.status) && (vt === null || vt(p.shown))
      && asof(p.as_of_datetime) && got(p.retrieved_at);
    tr.hidden = !ok;
    if (ok) { shown++; total += p.shown || 0; }
  }
  const active = Object.values(f).some((v) => v);
  $("#positions-clear").hidden = !active;
  $("#positions-count").textContent = active ? `${shown} of ${all} accounts` : `${all} accounts`;
  $("#positions-total").textContent = money(total);
  try { localStorage.setItem(POS_FILTER_KEY, JSON.stringify(f)); } catch {}
}
try {
  const f = JSON.parse(localStorage.getItem(POS_FILTER_KEY) || "null") || {};
  document.querySelectorAll("#positions [data-f]").forEach((el) => {
    if (f[el.dataset.f]) {
      if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === f[el.dataset.f]))
        el.add(new Option(f[el.dataset.f], f[el.dataset.f]));   // status list fills in on load
      el.value = f[el.dataset.f];
    }
  });
} catch {}
document.querySelectorAll("#positions [data-f]").forEach((el) => {
  el.addEventListener("input", filterPositions);
  el.addEventListener("change", filterPositions);
});
$("#positions-clear").addEventListener("click", () => {
  document.querySelectorAll("#positions [data-f]").forEach((el) => { el.value = ""; });
  filterPositions();
});

function showHoldings(p) {
  $("#holdings-card").hidden = false;
  $("#holdings-title").textContent = `Holdings: ${p.account} (as of ${when(p.as_of_datetime)})`
    + (p.share_pct ? ` · whole account, as on the portal; your share is ${num(p.share_pct)}%` : "");
  const tb = $("#holdings tbody"); tb.innerHTML = "";
  for (const h of p.holdings) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(h.symbol || "")}</td><td>${esc(h.description)}</td>
      <td class="num">${num(h.quantity)}</td><td class="num">${h.price == null ? "" : money(h.price)}</td>
      <td class="num">${money(h.value)}</td>
      <td>${h.price_as_of ? esc(h.price_as_of) + (h.price_as_of !== (p.as_of_date || "") ? " <span class='muted'>(older)</span>" : "") : esc(p.as_of_date || "")}</td>
      <td><button class="link" data-src="${p.snapshot_id}">View Source</button></td>`;
    tb.appendChild(tr);
  }
}

document.addEventListener("click", (e) => {
  if (e.target.dataset.src) viewSource(`/api/snapshots/${e.target.dataset.src}/evidence`,
    null, Number(e.target.dataset.src));
});

async function viewSource(base, files, snapshotId) {
  let meta = "";
  if (snapshotId) {
    const s = await api(`/api/snapshots/${snapshotId}`);
    files = s.evidence_files;
    meta = `${s.account}, ${s.source}. As of ${when(s.as_of_datetime)}; retrieved ${when(s.retrieved_at)}.`;
  }
  $("#source-title").textContent = "View Source";
  $("#source-meta").textContent = meta;
  const body = $("#source-body"); body.innerHTML = "";
  if (!files || !files.length) { body.innerHTML = "<p>No evidence file was stored for this value.</p>"; }
  for (const f of files || []) {
    const url = `${base}/${encodeURIComponent(f)}`;
    if (/\.(png|jpe?g)$/i.test(f)) {
      body.insertAdjacentHTML("beforeend", `<h3>${esc(f)}</h3><img alt="screenshot" src="${url}">`);
    } else if (/\.pdf$/i.test(f)) {
      body.insertAdjacentHTML("beforeend", `<h3>${esc(f)}</h3><p><a href="${url}" target="_blank" rel="noopener">Open PDF</a></p>`);
    } else {
      const text = await fetch(url).then((r) => r.text());
      const det = document.createElement("details");
      if (/\.txt$/i.test(f)) det.open = true;
      det.innerHTML = `<summary>${esc(f)}</summary><pre></pre>`;
      det.querySelector("pre").textContent = text;
      body.appendChild(det);
    }
  }
  $("#source").showModal();
}
$("#source-close").addEventListener("click", () => $("#source").close());

let watching = null;
async function watchRun(id) {
  if (watching === id) return;
  watching = id;
  syncRefreshButtons();
  while (true) {
    const r = await api(`/api/runs/${id}`);
    $("#refresh-status").textContent = r.status === "running"
      ? `refreshing… ${r.items.filter((i) => i.finished_at).length}/${r.items.length || "?"} logins done`
      : `refresh ${r.status}: ${r.summary || ""}`;
    loadInboxBadge();
    if (r.status !== "running") break;
    await new Promise((res) => setTimeout(res, 2500));
  }
  watching = null;
  syncRefreshButtons();
  loadDashboard();
}

$("#refresh-btn").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (btn.dataset.run) return cancelRun(btn, (t) => { $("#refresh-status").textContent = t; });
  btn.disabled = true;
  try {
    const { run_id } = await api("/api/refresh", { method: "POST" });
    watchRun(run_id);
  } catch (e) { $("#refresh-status").textContent = e.message; syncRefreshButtons(); }
});

// A Refresh button turns into Cancel while its run is going. Cancel stops the run at
// its next check (between pages, while waiting for a code, or before the captured
// pages are read); accounts the model is already reading finish first.
async function cancelRun(btn, report) {
  btn.disabled = true; btn.textContent = "Cancelling…";
  try { await api(`/api/runs/${btn.dataset.run}/cancel`, { method: "POST" }); }
  catch (err) { report(err.message); }
  syncRefreshButtons();
}
function setRunButton(btn, run, idleDisabled, idleTitle) {
  if (run) {
    btn.dataset.run = run.id;
    btn.textContent = run.cancel_requested ? "Cancelling…" : "Cancel";
    btn.disabled = !!run.cancel_requested;
    btn.classList.remove("primary");
    btn.title = run.cancel_requested ? "Stopping at the next safe point"
      : run.kind === "upload" ? "Stop reading statements (the one being read finishes first)" : "Stop this refresh";
  } else {
    delete btn.dataset.run;
    btn.textContent = btn.dataset.idle || "Refresh";
    btn.disabled = idleDisabled;
    btn.classList.add("primary");
    btn.title = idleTitle;
  }
}
$("#kill-off").addEventListener("click", async () => { await api("/api/kill", { method: "POST", body: { on: false } }); loadSummary(); });

function loadDashboard() { loadSummary(); loadChart(); loadPositions(); }

// --- inbox --------------------------------------------------------------------
const KIND = { review: "Rejected twice", held_value: "Held for approval", question: "Question",
  twofa_code: "Verification code needed", playbook_proposal: "Proposed playbook change",
  registry_suggestion: "Registry suggestion", alert: "Alert" };

async function loadInboxBadge() {
  const s = await api("/api/summary");
  const badge = $("#inbox-badge"); badge.hidden = !s.inbox_open; badge.textContent = s.inbox_open;
  if ($("#inbox").classList.contains("active")) loadInbox();
}

async function loadInbox() {
  const items = await api("/api/inbox");
  const list = $("#inbox-list"); list.innerHTML = items.length ? "" : "<p class='muted'>Nothing waiting.</p>";
  for (const it of items) {
    const div = document.createElement("div");
    div.className = "item" + (it.kind === "alert" ? " alert" : "");
    const snap = it.payload?.snapshot;
    const ext = it.payload?.extraction;
    let rows = "";
    if (snap?.holdings) {
      rows = `<table><thead><tr><th>Symbol</th><th>Description</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Value</th><th>As of</th></tr></thead><tbody>` +
        snap.holdings.map((h) => `<tr><td>${esc(h.symbol || "")}</td><td>${esc(h.description)}</td><td class="num">${num(h.quantity)}</td>
        <td class="num">${h.price == null ? "" : money(h.price)}</td><td class="num">${money(h.value)}</td><td>${esc(h.price_as_of || "")}</td></tr>`).join("") +
        `</tbody></table><p class="muted">Total ${money(snap.holdings.reduce((a, h) => a + h.value, 0))}; stated total ${money(snap.statement_total)}; as of ${when(snap.as_of_datetime)}</p>`;
    }
    if (ext?.accounts) {
      rows += `<table><thead><tr><th>Account read</th><th class="num">Holdings</th><th class="num">Sum of holdings</th><th class="num">Stated total</th></tr></thead><tbody>` +
        ext.accounts.map((a) => `<tr><td>${esc(a.account)}${a.account_suffix ? " …" + esc(a.account_suffix) : ""}</td><td class="num">${(a.holdings || []).length}</td>
        <td class="num">${money((a.holdings || []).reduce((s, h) => s + h.value, 0))}</td><td class="num">${money(a.statement_total)}</td></tr>`).join("") +
        `</tbody></table><p class="muted">Page total for all accounts: ${money(ext.grand_total)}</p>`;
    }
    let actions = "";
    if (it.kind === "held_value" && it.payload?.new_account)
      actions = `<button class="primary" data-act="approve">Include account</button><button data-act="reject">Exclude account</button>`;
    else if (it.kind === "held_value" || it.kind === "playbook_proposal" || it.kind === "registry_suggestion")
      actions = `<button class="primary" data-act="approve">Approve</button><button data-act="reject">Reject</button>`;
    else if (it.kind === "twofa_code")
      actions = `<input class="code" inputmode="numeric" autocomplete="one-time-code" placeholder="code"><button class="primary" data-act="answer">Submit code</button><button data-act="reject">Dismiss</button>`;
    else if (it.kind === "review" && it.payload?.extraction && /^The page lists an account ending \w+ that wasn't extracted\./.test(it.reason || "")
             && (it.reason || "").split("; ").every((r) => /^The page lists an account ending \w+ that wasn't extracted\./.test(r)))
      actions = `<button class="primary" data-act="approve" title="The other numbers on the page (e.g. a member or card number) aren't accounts. They're remembered for this login, and the accounts read are checked again and stored or held.">Accept accounts read</button><button data-act="reject">Dismiss</button>`;
    else actions = `<button data-act="reject">Dismiss</button>`;
    if (it.evidence_files?.length) actions += `<button class="link" data-act="source">View Source</button>`;
    const change = it.kind === "playbook_proposal" ? `<pre>${esc(JSON.stringify(it.payload.change, null, 2))}</pre>` : "";
    div.innerHTML = `<div class="kind">${esc(KIND[it.kind] || it.kind)} · ${esc(it.account || it.institution || "")} · ${when(it.created_at)}</div>
      <p>${esc(it.reason)}</p>${change}${rows}<div class="row">${actions}<span class="msg"></span></div>`;
    div.addEventListener("click", async (e) => {
      const act = e.target.dataset.act; if (!act) return;
      const msg = div.querySelector(".msg");
      try {
        if (act === "source") return viewSource(`/api/inbox/${it.id}/evidence`, it.evidence_files);
        if (act === "answer") {
          const code = div.querySelector(".code").value.trim(); if (!code) return;
          await api(`/api/inbox/${it.id}/answer`, { method: "POST", body: { answer: code } });
        } else await api(`/api/inbox/${it.id}/${act}`, { method: "POST" });
        loadInbox(); loadSummary();
      } catch (err) { msg.textContent = err.message; msg.className = "msg err"; }
    });
    list.appendChild(div);
  }
}

// --- accounts & logins --------------------------------------------------------
let editing = null;     // login id being edited
const TYPES = ["brokerage", "retirement", "private_fund", "bank", "loan", "credit_card"];
const STATUSES = [["active", "Included"], ["ignored", "Excluded"], ["pending", "Not decided yet"], ["closed", "Closed"]];

// --- Gmail reader for emailed verification codes ----------------------------------
async function loadEmailReader() {
  const r = await api("/api/email-reader");
  $("#email-reader-status").textContent = r.configured
    ? `Reading codes from ${r.address}.` : "Not set up: emailed codes are asked for in the Inbox.";
  $("#email-reader-remove").hidden = !r.configured;
}
$("#email-reader-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, status = $("#email-reader-status");
  status.textContent = "Checking the Gmail sign-in…";
  try {
    await api("/api/email-reader", { method: "PUT",
      body: { address: f.address.value, app_password: f.app_password.value } });
    f.reset();
  } catch (err) { status.textContent = err.message; return; }
  loadEmailReader();
});
$("#email-reader-remove").addEventListener("click", async () => {
  if (!confirm("Stop reading codes from Gmail? The saved app password is deleted.")) return;
  await api("/api/email-reader", { method: "DELETE" });
  loadEmailReader();
});

async function loadAccounts() {
  loadOwners();
  loadDaily();
  loadEmailReader();
  const logins = await api("/api/logins");
  const tb = $("#login-list tbody"); tb.innerHTML = "";
  for (const lg of logins) {
    const tr = document.createElement("tr");
    const canRefresh = lg.status === "active" && lg.refresh_supported;
    const canUpload = lg.status === "active";
    tr.innerHTML = `<td class="clip" title="${esc(lg.name)}">${esc(lg.name)}</td><td class="clip" title="${esc(lg.institution)}">${esc(lg.institution)}</td><td>${lg.accounts.length}</td>
      <td>${esc(lg.cadence)}</td><td>${lg.refresh_supported ? "Yes" : "No"}</td>
      <td>${when(lg.last_updated)}</td>
      <td class="row"><button data-act="edit">Edit</button>
        ${!lg.refresh_supported
          ? `<button class="primary" data-act="upload" data-idle="Upload statements" data-lid="${lg.id}" data-can="${canUpload ? 1 : 0}" ${canUpload ? "" : "disabled"} title="Upload this month's statement PDFs: one per account or one consolidated">Upload statements</button><input type="file" accept=".pdf,application/pdf" multiple hidden>`
          : `<button class="primary" data-act="refresh" data-lid="${lg.id}" data-can="${canRefresh ? 1 : 0}" ${canRefresh ? "" : "disabled"}>Refresh</button>`}
        <span class="msg ${loginMsg[lg.id]?.err ? "err" : ""}">${esc(loginMsg[lg.id]?.text || "")}</span></td>`;
    tr.dataset.lid = lg.id;
    tr.querySelector("[data-act=edit]").addEventListener("click", () => openForm(lg));
    tr.querySelector("[data-act=refresh]")?.addEventListener("click", (e) => {
      const b = e.currentTarget;
      if (b.dataset.run) cancelRun(b, (t) => setLoginMsg(lg.id, t, true));
      else refreshLogin(lg, b);
    });
    const file = tr.querySelector("input[type=file]");
    tr.querySelector("[data-act=upload]")?.addEventListener("click", (e) => {
      const b = e.currentTarget;
      if (b.dataset.run) cancelRun(b, (t) => setLoginMsg(lg.id, t, true));
      else file.click();
    });
    file?.addEventListener("change", () => {
      const files = [...file.files]; file.value = "";
      if (files.length) uploadStatements(lg, tr.querySelector("[data-act=upload]"), files);
    });
    tb.appendChild(tr);
  }
  if (editing) {
    const lg = logins.find((x) => x.id === editing);
    if (lg && !dirtyRows().length) renderLoginAccounts(lg);   // never drop unsaved edits
    if (lg) loadStatements(lg);
  }
  syncRefreshButtons();
}

// Refreshes run in parallel, one per institution: a login's Refresh button is disabled
// only while that login (or another at the same institution, which shares its browser)
// is being refreshed. Dashboard Refresh is disabled when every due login is busy.
let refreshPoll = null;
const loginMsg = {};        // login id -> {text, err}: survives the list being redrawn
function setLoginMsg(id, text, err = false) {
  loginMsg[id] = { text, err };
  const m = document.querySelector(`#login-list tr[data-lid="${id}"] .msg`);
  if (m) { m.textContent = text; m.className = err ? "msg err" : "msg"; }
}
function applyBusy(s) {
  const busy = new Set(s.busy_login_ids || []);
  const runOf = {};                 // login id -> the running run that covers it
  for (const r of s.running || []) for (const id of JSON.parse(r.login_ids || "[]")) runOf[id] = r;
  document.querySelectorAll("#login-list [data-act=refresh], #login-list [data-act=upload]").forEach((b) => {
    const lid = Number(b.dataset.lid), isBusy = busy.has(lid);
    setRunButton(b, runOf[lid], isBusy || b.dataset.can !== "1",
      isBusy ? "Another login at this institution is being refreshed" : "");
  });
  const dash = (s.running || []).find((r) => r.trigger === "dashboard" || r.trigger === "scheduled");
  setRunButton($("#refresh-btn"), dash, !s.refresh_all_available,
    s.refresh_all_available ? "" : "Every login is already being refreshed");
}
async function syncRefreshButtons() {
  const s = await api("/api/summary");
  applyBusy(s);
  if (s.running.length && !refreshPoll) {
    let last = JSON.stringify(s.busy_login_ids);
    refreshPoll = setInterval(async () => {
      const t = await api("/api/summary");
      applyBusy(t);
      const now = JSON.stringify(t.busy_login_ids);
      if (now !== last && $("#accounts").classList.contains("active")) loadAccounts();  // a run finished
      last = now;
      if (!t.running.length) { clearInterval(refreshPoll); refreshPoll = null; }
    }, 3000);
  }
}

async function refreshLogin(lg, btn) {
  btn.disabled = true;
  try {
    const { run_id } = await api(`/api/logins/${lg.id}/refresh`, { method: "POST" });
    syncRefreshButtons();
    while (true) {
      const r = await api(`/api/runs/${run_id}`);
      if (r.status !== "running") {
        const item = r.items[0];
        setLoginMsg(lg.id, `${r.status}: ${item?.detail || r.summary || ""}`);
        break;
      }
      setLoginMsg(lg.id, "refreshing… (signing in, capturing pages, then reading accounts)");
      await new Promise((res) => setTimeout(res, 2500));
    }
    loadInboxBadge();
  } catch (err) { setLoginMsg(lg.id, err.message, true); }
  syncRefreshButtons();
  if ($("#accounts").classList.contains("active")) loadAccounts();
}

// Upload statements: send each PDF, then read them all in one run (one file at a time).
async function uploadStatements(lg, btn, files) {
  btn.disabled = true;
  const ids = [], notes = [];
  try {
    for (const [n, f] of files.entries()) {
      setLoginMsg(lg.id, `Uploading ${n + 1} of ${files.length}: ${f.name}…`);
      const res = await fetch(`/api/logins/${lg.id}/statements`, {
        method: "POST", body: f,
        headers: { "X-PA-Client": "1", "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(f.name) } });
      const up = await res.json().catch(() => ({}));
      if (!res.ok) { notes.push(`${f.name}: ${up.detail || res.statusText}`); continue; }
      if (up.already_read) {
        notes.push(`${f.name}: already read from ${up.filename === f.name ? "an upload" : esc(up.filename)} on ${when(up.uploaded_at)}; ignored.`);
        continue;
      }
      ids.push(up.id);
    }
    if (!ids.length) { setLoginMsg(lg.id, notes.join(" ") || "Nothing to read."); btn.disabled = false; return; }
    const { run_id } = await api(`/api/logins/${lg.id}/statements/read`, { method: "POST", body: { upload_ids: ids } });
    syncRefreshButtons();
    while (true) {
      const r = await api(`/api/runs/${run_id}`);
      if (r.status !== "running") {
        const detail = r.items.map((i) => i.detail).filter(Boolean).join(" · ");
        setLoginMsg(lg.id, [`${r.status}: ${r.summary || ""}`, detail, ...notes].filter(Boolean).join(" · "),
          r.status !== "done");
        break;
      }
      const done = r.items.filter((i) => i.finished_at).length;
      setLoginMsg(lg.id, `Reading statement ${Math.min(done + 1, ids.length)} of ${ids.length}… (the model reads every account; this takes a few minutes)`);
      await new Promise((res) => setTimeout(res, 3000));
    }
    loadInboxBadge();
  } catch (err) { setLoginMsg(lg.id, err.message, true); }
  syncRefreshButtons();
  if ($("#accounts").classList.contains("active")) loadAccounts();
}

async function loadStatements(lg) {
  const sec = $("#statements-section");
  sec.hidden = !(lg && (lg.access_method === "upload" || lg.statement_kind === "capital"));
  if (sec.hidden) return;
  const tb = $("#statement-list tbody");
  const ups = await api(`/api/logins/${lg.id}/statements`);
  tb.innerHTML = ups.length ? "" : `<tr><td colspan="4" class="muted">None yet.</td></tr>`;
  for (const u of ups) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="wrap">${esc(u.filename)}</td><td>${when(u.uploaded_at)}</td>
      <td class="wrap">${u.outcome ? `<span class="pill ${u.outcome === "stored" || u.outcome === "unchanged" ? "ok" : u.outcome === "held" || u.outcome === "mixed" ? "pending" : "needs_review"}">${esc(u.outcome)}</span> ` : `<span class="muted">not read yet</span>`}
        ${esc((u.detail || "").replace(u.filename + ": ", ""))}</td>
      <td><a href="/api/statements/${u.id}/file" target="_blank" rel="noopener">Open</a></td>`;
    tb.appendChild(tr);
  }
}

function openForm(lg) {
  const n = dirtyRows().length;
  if (n && !confirm(`${n} account change${n > 1 ? "s are" : " is"} not saved. Discard ${n > 1 ? "them" : "it"}?`)) return;
  editing = lg ? lg.id : null;
  const f = $("#login-form"); f.reset();
  if (!lg) f.liability_categories.value = "Loans, Credit cards, Mortgages, Lines of credit";
  $("#login-form-card").hidden = false;
  $("#login-form-title").textContent = lg ? `Edit ${lg.name}` : "Add login";
  $("#login-msg").textContent = "";
  if (lg) for (const el of f.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") el.checked = !!lg[el.name];
    else if (el.name === "holdings_urls") el.value = (lg.holdings_urls || []).join("\n");
    else el.value = lg[el.name] ?? "";
  }
  syncClosedOn();
  $("#cred-section").hidden = !lg;
  const cf = $("#cred-form"); cf.reset(); cf.password.type = "password"; revealed = false;
  $("#cred-msg").textContent = lg?.has_credentials ? "Saved. Use the eye to view them." : "";
  if (lg) { $("#open-browser-cmd").textContent = `.venv/bin/python -m portfolio_agent.cli open-browser ${lg.id}`; renderLoginAccounts(lg); }
  loadStatements(lg);
  $("#login-form-card").scrollIntoView({ behavior: "smooth" });
}

function renderLoginAccounts(lg) {
  const tb = $("#login-accounts tbody"); tb.innerHTML = "";
  if (!lg.accounts.length) tb.innerHTML = `<tr><td colspan="10" class="muted">None yet. ${lg.access_method === "upload"
    ? "Upload a statement; the accounts in it are found automatically."
    : "Press Refresh; accounts are found on the login's holdings page."}</td></tr>`;
  for (const a of lg.accounts) {
    const tr = document.createElement("tr");
    const portal = `${a.portal_name || ""}${a.portal_suffix ? " …" + a.portal_suffix : ""}`;
    tr.innerHTML = `<td><input name="display_name" value="${esc(a.display_name)}" title="${esc(a.display_name)}"></td>
      <td class="clip" title="${esc(portal)}">${esc(portal)}</td>
      <td class="clip" title="${esc(a.category || "")}">${esc(a.category || "")}</td>
      <td class="num" title="${a.balance_as_of ? "As of " + esc(new Date(a.balance_as_of).toLocaleString()) : "No value stored yet"}">${money(a.balance)}</td>
      <td><select name="account_type">${TYPES.map((t) => `<option value="${t}" ${t === a.account_type ? "selected" : ""}>${t.replace("_", " ")}</option>`).join("")}</select></td>
      <td><select name="status">${STATUSES.map(([v, l]) => `<option value="${v}" ${v === a.status ? "selected" : ""}>${l}</option>`).join("")}</select></td>
      <td><input name="is_liability" type="checkbox" ${a.is_liability ? "checked" : ""}></td>
      <td><select name="owner_id" title="An entity you own part of; the account counts at your percentage">${ownerOptions(a.owner_id)}</select></td>
      <td><input name="closed_on" type="date" value="${esc(a.closed_on || "")}"></td>
      <td><span class="msg"></span></td>`;
    const dn = tr.querySelector("[name=display_name]");
    dn.addEventListener("input", () => { dn.title = dn.value; });
    tr.dataset.id = a.id;
    tr.dataset.saved = JSON.stringify(rowValues(tr));
    tr.querySelectorAll("input,select").forEach((el) => el.addEventListener("input", () => markDirty(tr)));
    tr.dataset.status = a.status;
    tr.dataset.balance = a.balance ?? "";
    tb.appendChild(tr);
  }
  filterLoginAccounts();
  updateSaveAll();
}

// Edits to the accounts table are saved together with "Save changes".
function rowValues(tr) {
  const body = {};
  tr.querySelectorAll("input,select").forEach((el) => {
    body[el.name] = el.type === "checkbox" ? el.checked : (el.value || null); });
  return body;
}
function markDirty(tr) {
  const dirty = JSON.stringify(rowValues(tr)) !== tr.dataset.saved;
  tr.classList.toggle("dirty", dirty);
  if (dirty) { const m = tr.querySelector(".msg"); m.textContent = "Changed"; m.className = "msg"; }
  else tr.querySelector(".msg").textContent = "";
  updateSaveAll();
}
const dirtyRows = () => [...document.querySelectorAll("#login-accounts tbody tr.dirty")];
function updateSaveAll() {
  const n = dirtyRows().length;
  for (const b of document.querySelectorAll(".save-accounts")) {
    b.disabled = n === 0;
    b.textContent = n ? `Save changes (${n})` : "Save changes";
  }
  $("#discard-accounts").disabled = n === 0;
}
async function saveAllAccounts() {
  const rows = dirtyRows();
  document.querySelectorAll(".save-accounts").forEach((b) => { b.disabled = true; });
  let ok = 0, failed = 0;
  for (const tr of rows) {          // one at a time, so each row reports its own result
    const body = rowValues(tr), msg = tr.querySelector(".msg");
    try {
      await api(`/api/accounts/${tr.dataset.id}`, { method: "PUT", body });
      tr.dataset.saved = JSON.stringify(body);
      tr.dataset.status = body.status;   // the row stays visible until the filter is next changed
      tr.classList.remove("dirty");
      msg.textContent = "Saved."; msg.className = "msg"; ok++;
    } catch (err) { msg.textContent = err.message; msg.className = "msg err"; failed++; }
  }
  $("#accounts-save-msg").textContent = failed
    ? `${ok} saved, ${failed} not saved: see the rows marked in red.` : `${ok} saved.`;
  $("#accounts-save-msg").className = failed ? "msg err" : "msg";
  updateSaveAll();
  loadOwners();          // its account lists changed
}
document.querySelectorAll(".save-accounts").forEach((b) => b.addEventListener("click", saveAllAccounts));
$("#discard-accounts").addEventListener("click", async () => {
  const lg = (await api("/api/logins")).find((x) => x.id === editing);
  for (const tr of dirtyRows()) tr.classList.remove("dirty");
  $("#accounts-save-msg").textContent = "";
  if (lg) renderLoginAccounts(lg);
});
window.addEventListener("beforeunload", (e) => { if (dirtyRows().length) e.preventDefault(); });

// Filters for the accounts table: by In portfolio? status, and by balance.
// Debts are compared by the amount owed, so "over $1,000" also shows a $5,000 loan.
// The choice is remembered in this browser.
const ACCT_FILTER_KEY = "acctFilters";
function filterLoginAccounts() {
  const show = new Set([...document.querySelectorAll("#acct-filters input[type=checkbox]:checked")].map((c) => c.value));
  const min = parseFloat($("#acct-min").value);
  let shown = 0, total = 0;
  for (const tr of document.querySelectorAll("#login-accounts tbody tr[data-status]")) {
    total++;
    const bal = tr.dataset.balance === "" ? null : Math.abs(parseFloat(tr.dataset.balance));
    const ok = tr.classList.contains("dirty")    // unsaved edits always stay in view
      || (show.has(tr.dataset.status) && (isNaN(min) || (bal != null && bal > min)));
    tr.hidden = !ok;
    if (ok) shown++;
  }
  $("#acct-count").textContent = total ? `${shown} of ${total} accounts` : "";
  try {
    localStorage.setItem(ACCT_FILTER_KEY, JSON.stringify({ show: [...show], min: $("#acct-min").value }));
  } catch {}
}
try {
  const f = JSON.parse(localStorage.getItem(ACCT_FILTER_KEY) || "null");
  if (f) {
    document.querySelectorAll("#acct-filters input[type=checkbox]").forEach((c) => { c.checked = f.show.includes(c.value); });
    $("#acct-min").value = f.min || "";
  }
} catch {}
document.querySelectorAll("#acct-filters input").forEach((el) => el.addEventListener("input", filterLoginAccounts));

// --- part-owned entities (an LLC) ---------------------------------------------
let owners = [];
const todayLocal = () => new Date().toLocaleDateString("en-CA");   // YYYY-MM-DD
function ownerOptions(selected) {
  return `<option value="">You (100%)</option>` + owners.map((o) =>
    `<option value="${o.id}" ${o.id === selected ? "selected" : ""}>${esc(o.name)} (${num(o.pct)}%)</option>`).join("");
}
function refreshOwnerSelects() {     // keeps unsaved choices in the accounts table
  for (const sel of document.querySelectorAll("#login-accounts select[name=owner_id]")) {
    const v = sel.value;
    sel.innerHTML = ownerOptions(v ? +v : null);
    sel.value = owners.some((o) => String(o.id) === v) ? v : "";
  }
}
async function loadOwners() {
  owners = await api("/api/owners");
  const tb = $("#owner-list tbody");
  tb.innerHTML = owners.length ? "" : `<tr><td colspan="5" class="muted">None. Add one below if some accounts belong to an LLC or similar.</td></tr>`;
  for (const o of owners) {
    const byDate = new Map(o.shares.map((x) => [x.effective_from, x]));   // a later entry on a date replaces it
    const changes = [...byDate.values()].map((x) => `${num(x.pct)}% from ${esc(x.effective_from)}`).join("<br>");
    const accts = o.accounts.map((a) => esc(a.display_name)).join("<br>") || "<span class='muted'>none yet</span>";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(o.name)}</td><td class="num">${num(o.pct)}%</td><td>${changes}</td><td>${accts}</td>
      <td><div class="row" style="margin:0"><button data-act="pct">Change %</button>
        <button data-act="rename">Rename</button><button data-act="del">Delete</button></div>
        <form class="controls share-form" hidden>
          <label>New % <input name="pct" type="number" required min="0.01" max="100" step="any" style="width:80px"></label>
          <label>From <input name="effective_from" type="date" required></label>
          <button type="submit" class="primary">Save</button><button type="button" data-act="cancel">Cancel</button></form>
        <span class="msg"></span></td>`;
    const form = tr.querySelector("form"), msg = tr.querySelector(".msg");
    const fail = (err) => { msg.textContent = err.message; msg.className = "msg err"; };
    tr.querySelector("[data-act=pct]").addEventListener("click", () => {
      form.hidden = false; form.pct.value = o.pct; form.effective_from.value = todayLocal(); form.pct.focus(); });
    tr.querySelector("[data-act=cancel]").addEventListener("click", () => { form.hidden = true; });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api(`/api/owners/${o.id}/shares`, { method: "POST",
          body: { pct: +form.pct.value, effective_from: form.effective_from.value } });
        await loadOwners();
      } catch (err) { fail(err); }
    });
    tr.querySelector("[data-act=rename]").addEventListener("click", async () => {
      const name = prompt("New name", o.name);
      if (!name || name.trim() === o.name) return;
      try { await api(`/api/owners/${o.id}`, { method: "PUT", body: { name: name.trim() } }); await loadOwners(); }
      catch (err) { fail(err); }
    });
    tr.querySelector("[data-act=del]").addEventListener("click", async () => {
      if (!confirm(`Delete ${o.name}?`)) return;
      try { await api(`/api/owners/${o.id}`, { method: "DELETE" }); await loadOwners(); }
      catch (err) { fail(err); }
    });
    tb.appendChild(tr);
  }
  refreshOwnerSelects();
}
$("#owner-form").effective_from.value = todayLocal();
$("#owner-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, msg = $("#owner-msg");
  try {
    const o = await api("/api/owners", { method: "POST",
      body: { name: f.name.value.trim(), pct: +f.pct.value, effective_from: f.effective_from.value } });
    f.reset(); f.effective_from.value = todayLocal();
    msg.textContent = `Added ${o.name}. Now pick it under Owned by for its accounts.`; msg.className = "msg";
    await loadOwners();
  } catch (err) { msg.textContent = err.message; msg.className = "msg err"; }
});

$("#new-login").addEventListener("click", () => openForm(null));
// "Closed on" only matters for a closed login.
function syncClosedOn() {
  const f = $("#login-form"), closed = f.status.value === "closed";
  $("#login-closed-on").hidden = !closed;
  if (!closed) f.closed_on.value = "";
}
$("#login-form").status.addEventListener("change", syncClosedOn);

$("#cancel-login").addEventListener("click", () => { $("#login-form-card").hidden = true; editing = null; });

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, body = {};
  for (const el of f.elements) {
    if (!el.name) continue;
    body[el.name] = el.type === "checkbox" ? el.checked : (el.value.trim() || null);
  }
  try {
    const saved = await api(editing ? `/api/logins/${editing}` : "/api/logins",
      { method: editing ? "PUT" : "POST", body });
    openForm(saved); $("#login-msg").textContent = "Saved."; $("#login-msg").className = "msg"; loadAccounts();
  } catch (err) { $("#login-msg").textContent = err.message; $("#login-msg").className = "msg err"; }
});

let revealed = false;
$("#eye").addEventListener("click", async () => {
  const cf = $("#cred-form");
  if (revealed) { cf.password.type = "password"; revealed = false; return; }
  if (!cf.password.value && editing) {
    try {
      const c = await api(`/api/logins/${editing}/credentials`);
      cf.username.value = c.username; cf.password.value = c.password;
    } catch (err) { $("#cred-msg").textContent = err.message; return; }
  }
  cf.password.type = "text"; revealed = true;
});

$("#cred-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const cf = e.target;
  try {
    await api(`/api/logins/${editing}/credentials`, { method: "PUT",
      body: { username: cf.username.value, password: cf.password.value } });
    cf.password.type = "password"; revealed = false;
    $("#cred-msg").textContent = "Saved (encrypted)."; $("#cred-msg").className = "msg";
    loadAccounts();
  } catch (err) { $("#cred-msg").textContent = err.message; $("#cred-msg").className = "msg err"; }
});

// --- debug ---------------------------------------------------------------------
function took(a, b) {
  if (!a) return "";
  const ms = (b ? new Date(b) : new Date()) - new Date(a);
  const sec = Math.round(ms / 1000);
  return (sec >= 60 ? `${Math.floor(sec / 60)}m ` : "") + `${sec % 60}s` + (b ? "" : " so far");
}

async function loadDebug() {
  const runs = await api("/api/runs");
  const tb = $("#run-list tbody"); tb.innerHTML = runs.length ? "" : `<tr><td colspan="7" class="muted">No runs yet.</td></tr>`;
  for (const r of runs) {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    tr.innerHTML = `<td>${r.id}</td><td>${esc(r.kind)}</td><td>${when(r.started_at)}</td>
      <td>${took(r.started_at, r.finished_at)}</td><td>${esc(r.login_names || r.logins)}</td>
      <td><span class="pill ${r.status === "done" ? "ok" : r.status === "running" ? "stale" : r.status === "cancelled" ? "closed" : "needs_review"}">${esc(r.status)}</span></td>
      <td>${esc(r.summary || "")}</td>`;
    tr.addEventListener("click", () => showRun(r.id));
    tb.appendChild(tr);
  }
}
$("#debug-reload").addEventListener("click", loadDebug);

async function showRun(id) {
  const r = await api(`/api/runs/${id}?detail=true`);
  const box = $("#run-detail"); box.hidden = false;
  let html = `<h2>Run ${r.id} · ${esc(r.kind)} · ${esc(r.status)}</h2>
    <p class="muted">Started ${when(r.started_at)} · finished ${when(r.finished_at)} · took ${took(r.started_at, r.finished_at)}<br>${esc(r.summary || "")}</p>`;
  for (const it of r.items) {
    const base = `/api/runs/${r.id}/items/${it.id}/evidence`;
    const files = (it.evidence_files || []).map((f) => {
      const url = `${base}/${encodeURIComponent(f)}`;
      return /\.(png|jpe?g)$/i.test(f)
        ? `<a href="${url}" target="_blank" rel="noopener" class="thumb"><img src="${url}" alt="${esc(f)}"><span>${esc(f)}</span></a>`
        : `<a href="${url}" target="_blank" rel="noopener">${esc(f)}</a>`;
    }).join(" ");
    const steps = (it.trace || []).map((s, n) => {
      const { action, ...rest } = s;
      const info = Object.entries(rest).filter(([k]) => k !== "folder")
        .map(([k, v]) => `${esc(k)}=${esc(typeof v === "object" ? JSON.stringify(v) : v)}`).join(" · ");
      return `<tr><td>${n + 1}</td><td><code>${esc(action)}</code></td><td class="wrap">${info}</td></tr>`;
    }).join("");
    html += `<div class="item"><div class="kind">${esc(it.login_name || "login " + it.login_id)} · ${esc(it.outcome || "running")} · ${took(it.started_at, it.finished_at)}</div>
      <p>${esc(it.detail || "")}</p>
      <h3>Captured pages and files</h3><div class="files">${files || "<span class='muted'>None saved.</span>"}</div>
      <details ${it.outcome === "stored" ? "" : "open"}><summary>Steps (${(it.trace || []).length})</summary>
        <table><thead><tr><th>#</th><th>Step</th><th>Details</th></tr></thead><tbody>${steps}</tbody></table></details></div>`;
  }
  if (r.stored.length) html += `<h3>Values stored</h3><table><thead><tr><th>Account</th><th>As of</th><th class="num">Total</th><th></th></tr></thead><tbody>` +
    r.stored.map((x) => `<tr><td>${esc(x.account)}</td><td>${when(x.as_of_datetime)}</td><td class="num">${money(x.total)}</td>
      <td><button class="link" data-src="${x.id}">View Source</button></td></tr>`).join("") + `</tbody></table>`;
  if (r.inbox.length) html += `<h3>Inbox items from this run</h3><table><thead><tr><th>Kind</th><th>Account</th><th>Status</th><th>Reason</th></tr></thead><tbody>` +
    r.inbox.map((x) => `<tr><td>${esc(KIND[x.kind] || x.kind)}</td><td>${esc(x.account || "")}</td><td>${esc(x.status)}</td><td class="wrap">${esc(x.reason)}</td></tr>`).join("") + `</tbody></table>`;
  box.innerHTML = html;
  box.scrollIntoView({ behavior: "smooth" });
}

restoreChartSettings();
showTab(tabFromPath(), false);   // open the tab named in the address
setInterval(loadInboxBadge, 15000);

// Resizable columns: drag the right edge of a header; double-click it to reset.
// Widths are remembered per table in this browser.
function makeResizable(table) {
  const key = "colw:" + table.id, ths = [...table.querySelectorAll("thead th")];
  const wrap = document.createElement("div"); wrap.className = "table-scroll";
  table.parentNode.insertBefore(wrap, table); wrap.appendChild(table);
  const fit = () => { table.style.width = ths.reduce((n, th) => n + th.offsetWidth, 0) + "px"; };
  const freeze = () => {
    if (table.classList.contains("fixed")) return;
    const w = ths.map((th) => th.offsetWidth);
    ths.forEach((th, i) => { th.style.width = w[i] + "px"; });
    table.classList.add("fixed"); fit();
  };
  const save = () => { try { localStorage.setItem(key, JSON.stringify(ths.map((th) => parseInt(th.style.width) || 0))); } catch {} };
  let saved = null; try { saved = JSON.parse(localStorage.getItem(key) || "null"); } catch {}
  if (Array.isArray(saved) && saved.length === ths.length && saved.every((w) => w > 0)) {
    ths.forEach((th, i) => { th.style.width = saved[i] + "px"; });
    table.classList.add("fixed"); table.style.width = saved.reduce((a, b) => a + b, 0) + "px";
  }
  ths.forEach((th) => {
    const grip = document.createElement("span");
    grip.className = "col-grip"; grip.title = "Drag to resize; double-click to reset";
    th.appendChild(grip);
    grip.addEventListener("pointerdown", (e) => {
      e.preventDefault(); freeze();
      const x0 = e.clientX, w0 = th.offsetWidth;
      grip.setPointerCapture(e.pointerId); grip.classList.add("on");
      const move = (ev) => { th.style.width = Math.max(48, w0 + ev.clientX - x0) + "px"; fit(); };
      const up = () => {
        grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", up);
        grip.classList.remove("on"); save();
      };
      grip.addEventListener("pointermove", move); grip.addEventListener("pointerup", up);
    });
    grip.addEventListener("dblclick", () => {
      ths.forEach((h) => { h.style.width = ""; }); table.style.width = "";
      table.classList.remove("fixed"); try { localStorage.removeItem(key); } catch {}
    });
  });
}
makeResizable($("#login-list"));
makeResizable($("#login-accounts"));
