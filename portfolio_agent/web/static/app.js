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
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === b.dataset.tab));
  ({ dashboard: loadDashboard, inbox: loadInbox, accounts: loadAccounts, debug: loadDebug })[b.dataset.tab]();
}));

// --- dashboard ----------------------------------------------------------------
let chart;
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

$("#period").addEventListener("click", (e) => {
  const p = e.target.dataset.p; if (!p) return;
  document.querySelectorAll("#period button").forEach((b) => b.classList.toggle("on", b === e.target));
  $("#start").value = periodStart(p); $("#end").value = "";
  loadChart();
});
["#start", "#end", "#granularity", "#group", "#known-on"].forEach((s) => $(s).addEventListener("change", () => {
  if (s === "#start" || s === "#end") document.querySelectorAll("#period button").forEach((b) => b.classList.remove("on"));
  loadChart();
}));

async function loadSummary() {
  const s = await api("/api/summary");
  $("#net-worth").textContent = money(s.net_worth);
  $("#net-worth-note").textContent = s.latest_retrieval ? `latest data retrieved ${when(s.latest_retrieval)}` : "no data yet";
  const n = s.last_nightly;
  $("#last-nightly").textContent = n ? when(n.finished_at || n.started_at) : "never";
  $("#last-nightly-note").textContent = n ? `${n.status}${n.summary ? ": " + n.summary : ""}` : "schedule: see README";
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
  q.set("granularity", $("#granularity").value); q.set("group", $("#group").value);
  const d = await api("/api/series?" + q);
  $("#chart-empty").hidden = d.dates.length > 0;
  const sets = [{ label: "Total", data: d.total, borderColor: getComputedStyle(document.body).color,
    borderWidth: 2.5, pointRadius: 0, tension: 0.15, spanGaps: false }];
  Object.entries(d.series).forEach(([name, vals], i) => sets.push({
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
    tr.innerHTML = `<td>${esc(p.account)}</td><td>${esc(p.login_name || p.institution)}</td>
      <td><span class="pill ${esc(p.status)}">${esc(p.status.replace("_", " "))}</span></td>
      <td class="num">${money(p.total)}</td><td>${p.as_of_datetime ? when(p.as_of_datetime) : "–"}</td>
      <td>${when(p.retrieved_at)}</td>
      <td>${p.snapshot_id ? `<button class="link" data-src="${p.snapshot_id}">View Source</button>` : ""}</td>`;
    if (p.snapshot_id) tr.addEventListener("click", (e) => {
      if (e.target.dataset.src) return;
      showHoldings(p);
    });
    tb.appendChild(tr);
  }
}

function showHoldings(p) {
  $("#holdings-card").hidden = false;
  $("#holdings-title").textContent = `Holdings: ${p.account} (as of ${when(p.as_of_datetime)})`;
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
  $("#refresh-btn").disabled = true;
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
  $("#refresh-btn").disabled = false;
  loadDashboard();
}

$("#refresh-btn").addEventListener("click", async () => {
  try {
    const { run_id } = await api("/api/refresh", { method: "POST" });
    watchRun(run_id);
  } catch (e) { $("#refresh-status").textContent = e.message; }
});
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
const TYPES = ["brokerage", "retirement", "wealth_management", "private_fund", "bank", "loan", "credit_card", "other"];
const STATUSES = [["active", "Included"], ["ignored", "Excluded"], ["pending", "Not decided yet"], ["closed", "Closed"]];

async function loadAccounts() {
  const logins = await api("/api/logins");
  const tb = $("#login-list tbody"); tb.innerHTML = "";
  for (const lg of logins) {
    const tr = document.createElement("tr");
    const canRefresh = lg.status === "active" && lg.access_method === "browser";
    tr.innerHTML = `<td>${esc(lg.name)}</td><td>${esc(lg.institution)}</td><td>${lg.accounts.length}</td>
      <td>${esc(lg.cadence)}</td><td>${lg.has_credentials ? "yes" : "no"}</td>
      <td>${when(lg.last_fetched)}</td>
      <td class="row"><button data-act="edit">Edit</button>
        <button class="primary" data-act="refresh" data-can="${canRefresh ? 1 : 0}" ${canRefresh ? "" : "disabled"}>Refresh</button>
        <span class="msg"></span></td>`;
    tr.querySelector("[data-act=edit]").addEventListener("click", () => openForm(lg));
    tr.querySelector("[data-act=refresh]").addEventListener("click", () =>
      refreshLogin(lg, tr.querySelector("[data-act=refresh]"), tr.querySelector(".msg")));
    tb.appendChild(tr);
  }
  if (editing) {
    const lg = logins.find((x) => x.id === editing);
    if (lg) renderLoginAccounts(lg);
  }
  syncRefreshButtons();
}

// Only one run at a time: while any refresh or nightly run is going, every Refresh
// button is disabled, and they come back when it finishes.
let refreshPoll = null, loginRefreshActive = false;
function setRefreshButtons(busy) {
  document.querySelectorAll("#login-list [data-act=refresh]").forEach((b) => {
    b.disabled = busy || b.dataset.can !== "1";
    b.title = busy ? "A refresh is already running" : "";
  });
  $("#refresh-btn").disabled = busy;
}
async function syncRefreshButtons() {
  const s = await api("/api/summary");
  const busy = s.running.length > 0;
  setRefreshButtons(busy);
  if (busy && !refreshPoll) {
    refreshPoll = setInterval(async () => {
      const t = await api("/api/summary");
      if (!t.running.length) {
        clearInterval(refreshPoll); refreshPoll = null;
        setRefreshButtons(false);
        if ($("#accounts").classList.contains("active") && !loginRefreshActive) loadAccounts();
      }
    }, 3000);
  }
}

async function refreshLogin(lg, btn, msg) {
  msg.className = "msg";
  loginRefreshActive = true;
  setRefreshButtons(true);
  try {
    const { run_id } = await api(`/api/logins/${lg.id}/refresh`, { method: "POST" });
    while (true) {
      const r = await api(`/api/runs/${run_id}`);
      if (r.status !== "running") {
        const item = r.items[0];
        msg.textContent = `${r.status}: ${item?.detail || r.summary || ""}`;
        break;
      }
      msg.textContent = "refreshing… (signing in, capturing pages, then reading accounts)";
      await new Promise((res) => setTimeout(res, 2500));
    }
    loadInboxBadge();
  } catch (err) { msg.textContent = err.message; msg.className = "msg err"; }
  loginRefreshActive = false;
  syncRefreshButtons();
}

function openForm(lg) {
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
  $("#cred-section").hidden = !lg;
  const cf = $("#cred-form"); cf.reset(); cf.password.type = "password"; revealed = false;
  $("#cred-msg").textContent = lg?.has_credentials ? "Saved. Use the eye to view them." : "";
  if (lg) { $("#open-browser-cmd").textContent = `python -m portfolio_agent.cli open-browser ${lg.id}`; renderLoginAccounts(lg); }
  $("#login-form-card").scrollIntoView({ behavior: "smooth" });
}

function renderLoginAccounts(lg) {
  const tb = $("#login-accounts tbody"); tb.innerHTML = "";
  if (!lg.accounts.length) tb.innerHTML = `<tr><td colspan="8" class="muted">None yet. Press Refresh; accounts are found on the login's holdings page.</td></tr>`;
  for (const a of lg.accounts) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><input name="display_name" value="${esc(a.display_name)}"></td>
      <td>${esc(a.portal_name || "")}${a.portal_suffix ? " …" + esc(a.portal_suffix) : ""}</td>
      <td>${esc(a.category || "")}</td>
      <td><select name="account_type">${TYPES.map((t) => `<option value="${t}" ${t === a.account_type ? "selected" : ""}>${t.replace("_", " ")}</option>`).join("")}</select></td>
      <td><select name="status">${STATUSES.map(([v, l]) => `<option value="${v}" ${v === a.status ? "selected" : ""}>${l}</option>`).join("")}</select></td>
      <td><input name="is_liability" type="checkbox" ${a.is_liability ? "checked" : ""}></td>
      <td><input name="closed_on" type="date" value="${esc(a.closed_on || "")}"></td>
      <td><button>Save</button> <span class="msg"></span></td>`;
    tr.querySelector("button").addEventListener("click", async () => {
      const body = {}; tr.querySelectorAll("input,select").forEach((el) => {
        body[el.name] = el.type === "checkbox" ? el.checked : (el.value || null); });
      const msg = tr.querySelector(".msg");
      try { await api(`/api/accounts/${a.id}`, { method: "PUT", body }); msg.textContent = "Saved."; msg.className = "msg"; }
      catch (err) { msg.textContent = err.message; msg.className = "msg err"; }
    });
    tb.appendChild(tr);
  }
}

$("#new-login").addEventListener("click", () => openForm(null));
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
      <td><span class="pill ${r.status === "done" ? "ok" : r.status === "running" ? "stale" : "needs_review"}">${esc(r.status)}</span></td>
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

loadDashboard();
setInterval(loadInboxBadge, 15000);
