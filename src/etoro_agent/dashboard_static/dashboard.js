"use strict";

const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

function numeric(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function moneyText(value) { return money.format(numeric(value)); }
function signedMoney(value) {
  const amount = numeric(value);
  return `${amount > 0 ? "+" : ""}${money.format(amount)}`;
}
function pnlClass(value) { return numeric(value) > 0 ? "positive" : numeric(value) < 0 ? "negative" : ""; }
function shortTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function empty(target, text) {
  target.replaceChildren(node("div", "empty", text));
}
function badge(status) { return node("span", `badge ${status || ""}`, String(status || "unknown").replaceAll("_", " ")); }
function details(value, label = "Details") {
  const wrapper = document.createElement("details");
  wrapper.append(node("summary", "", label));
  wrapper.append(node("pre", "", JSON.stringify(value, null, 2)));
  return wrapper;
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-ETORO-CSRF": "1", Accept: "application/json" },
    body: JSON.stringify(payload),
    cache: "no-store",
  });
  const result = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
  return result;
}

async function approveProposal(approval) {
  const suffix = String(approval.envelope_hash || "").slice(-8);
  const phrase = `APPROVE ${approval.proposal_id} ${suffix}`;
  const entered = window.prompt(`Exact DEMO request is shown below. Type:\n${phrase}`);
  if (entered !== phrase) return;
  await postJson(`/api/approvals/${encodeURIComponent(approval.proposal_id)}`, {
    envelope_hash: approval.envelope_hash,
    confirmation: entered,
  });
  await initialLoad();
}

function renderOverview(snapshot) {
  const overview = snapshot.overview;
  const kill = snapshot.kill_switch;
  const metrics = [
    ["AI master NAV", moneyText(overview.master_equity_usd), "single real-budget simulation", pnlClass(overview.master_daily_pnl_usd)],
    ["AI master today", signedMoney(overview.master_daily_pnl_usd), snapshot.master.position ? `position ${snapshot.master.position.symbol}` : "no open position", pnlClass(overview.master_daily_pnl_usd)],
    ["Sol queue", String(overview.ai_pending || 0), snapshot.ai.latest ? `${snapshot.ai.latest.state.toLowerCase()} · ${snapshot.ai.latest.packet_id}` : "waiting for first candidate", overview.ai_pending ? "warning" : ""],
    ["Research capital", moneyText(overview.shadow_capital_usd), `${overview.strategy_count} isolated hypothesis ledgers`],
    ["Daily P&L", signedMoney(overview.daily_pnl_usd), "realized + unrealized", pnlClass(overview.daily_pnl_usd)],
    ["Strategies", String(overview.strategy_count), `${overview.top3_count} currently ranked top 3`],
    ["Pending approvals", String(overview.pending_approvals), "individual owner approval only", overview.pending_approvals ? "warning" : ""],
    ["Kill state", kill.active ? "HALTED" : "READY", kill.active ? "new orders blocked" : "executor gate inactive", kill.active ? "negative" : "positive"],
    ["Audit events", number.format(overview.audit_events), snapshot.audit.readable ? "store connected" : "store unavailable", snapshot.audit.readable ? "" : "negative"],
  ];
  const target = $("overview");
  target.replaceChildren(...metrics.map(([label, value, foot, tone]) => {
    const card = node("article", "metric");
    card.append(node("span", "label", label), node("strong", `value ${tone || ""}`, value), node("span", "foot", foot));
    return card;
  }));
  $("halt-banner").classList.toggle("hidden", !kill.active);
}

function renderStrategies(strategies) {
  const cards = [...strategies].sort((a, b) => {
    if (a.top3 !== b.top3) return a.top3 ? -1 : 1;
    if (a.rank && b.rank) return numeric(a.rank) - numeric(b.rank);
    return a.name.localeCompare(b.name);
  });
  $("strategies").replaceChildren(...cards.map((strategy) => {
    const card = node("article", `strategy-card ${strategy.top3 ? "top3" : ""}`);
    const head = node("div", "strategy-head");
    const title = node("div");
    title.append(node("div", "strategy-name", strategy.name), node("div", "strategy-family", strategy.family));
    head.append(title, node("span", "rank", strategy.rank ? `#${strategy.rank}` : "—"));
    const nav = node("div", "strategy-nav", moneyText(strategy.nav_usd));
    const stats = node("div", "strategy-stats");
    [["Today", signedMoney(strategy.daily_pnl_usd), pnlClass(strategy.daily_pnl_usd)], ["Drawdown", `${(numeric(strategy.drawdown_fraction) * 100).toFixed(2)}%`, ""], ["Trades", String(strategy.trades || 0), ""]].forEach(([label, value, tone]) => {
      const item = node("div");
      item.append(node("span", "", label), node("strong", tone, value));
      stats.append(item);
    });
    card.append(head, nav, stats, node("div", "status-tag", String(strategy.status || "unknown").replaceAll("_", " ")));
    return card;
  }));
}

function renderPnl(pnl) {
  const rows = pnl.daily || [];
  const table = $("pnl-table");
  if (!rows.length) {
    empty(table, "No daily P&L records yet. Collector remains fail-closed.");
  } else {
    const header = node("div", "table-row header");
    ["Day", "Realized", "Unrealized", "Equity"].forEach((label) => header.append(node("span", "", label)));
    const body = rows.slice(-5).reverse().map((row) => {
      const item = node("div", "table-row");
      item.append(
        node("span", "", row.day),
        node("span", pnlClass(row.realized_usd), signedMoney(row.realized_usd)),
        node("span", pnlClass(row.unrealized_usd), signedMoney(row.unrealized_usd)),
        node("span", "", moneyText(row.equity_usd)),
      );
      return item;
    });
    table.replaceChildren(header, ...body);
  }
  drawPnlChart(rows);
}

function drawPnlChart(rows) {
  const canvas = $("pnl-chart");
  const box = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(box.width * ratio));
  canvas.height = Math.max(1, Math.floor(box.height * ratio));
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const width = box.width;
  const height = box.height;
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = "#222c37";
  ctx.lineWidth = 1;
  for (let line = 0; line < 4; line += 1) {
    const y = 12 + ((height - 24) * line / 3);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
  if (!rows.length) return;
  const values = rows.map((row) => numeric(row.equity_usd));
  const low = Math.min(...values);
  const high = Math.max(...values);
  const spread = Math.max(high - low, Math.abs(high) * 0.005, 1);
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = values.length === 1 ? width / 2 : 4 + ((width - 8) * index / (values.length - 1));
    const y = 10 + ((height - 20) * (high - value + spread * 0.08) / (spread * 1.16));
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = "#49e6a2";
  ctx.lineWidth = 2;
  ctx.shadowColor = "rgba(73,230,162,.35)";
  ctx.shadowBlur = 9;
  ctx.stroke();
  ctx.shadowBlur = 0;
}

function renderHealth(health) {
  $("health-summary").textContent = String(health.status).toUpperCase();
  $("health-summary").className = health.status === "ok" ? "positive" : health.status === "halted" ? "negative" : "warning";
  $("health").replaceChildren(...health.checks.map((check) => {
    const item = node("div", "health-item");
    const line = node("div", "health-line");
    line.append(node("span", "health-name", check.name), node("span", `health-status ${check.status === "ok" ? "positive" : check.status === "halted" || check.status === "error" ? "negative" : "warning"}`, check.status));
    const detail = typeof check.detail === "string" ? check.detail : JSON.stringify(check.detail);
    item.append(line, node("span", "health-detail", detail));
    return item;
  }));
}

function renderOrders(orders) {
  const target = $("orders");
  if (!orders.length) return empty(target, "No order lifecycle events. DEMO executor has not submitted an order.");
  target.replaceChildren(...orders.map((order) => {
    const item = node("article", "order");
    const head = node("div", "order-head");
    head.append(node("span", "proposal", order.proposal_id), badge(order.status));
    const meta = node("div", "order-meta");
    [order.symbol || "no symbol", order.strategy_id || "unattributed", shortTime(order.updated_at)].forEach((text) => meta.append(node("span", "", text)));
    item.append(head, meta, details(order.lifecycle, `${order.lifecycle.length} lifecycle events`));
    return item;
  }));
}

function renderApprovals(approvals) {
  const target = $("approvals");
  if (!approvals.length) return empty(target, "No approval requests. Writes require a separate one-time owner action.");
  target.replaceChildren(...approvals.map((approval) => {
    const item = node("article", "approval");
    const head = node("div", "approval-head");
    head.append(node("span", "proposal", approval.proposal_id), badge(approval.status));
    item.append(head, details(approval.request, "Exact DEMO request"));
    if (approval.status === "awaiting_owner" && approval.envelope_hash) {
      const button = node("button", "approve-button", "APPROVE THIS DEMO ORDER");
      button.type = "button";
      button.addEventListener("click", () => approveProposal(approval).catch((error) => window.alert(error.message)));
      item.append(button);
    }
    return item;
  }));
}

function renderActivity(activity, audit) {
  $("audit-summary").textContent = audit.readable ? `${audit.events_loaded} recent events · ${audit.latest_event_hash ? audit.latest_event_hash.slice(0, 10) : "no hash"}` : "Audit store unavailable";
  const target = $("activity");
  if (!activity.length) return empty(target, "No activity events recorded.");
  target.replaceChildren(...activity.map((event) => {
    const row = node("div", "activity-row");
    row.append(
      node("span", "activity-time", shortTime(event.ts).split(", ").pop()),
      node("span", "activity-type", event.event_type),
      node("span", "activity-payload", JSON.stringify(event.payload)),
      node("span", "activity-hash", event.event_hash.slice(0, 10)),
    );
    row.title = JSON.stringify(event.payload, null, 2);
    return row;
  }));
}

function render(snapshot) {
  renderOverview(snapshot);
  renderStrategies(snapshot.strategies);
  renderPnl(snapshot.pnl);
  renderHealth(snapshot.health);
  renderOrders(snapshot.orders);
  renderApprovals(snapshot.approvals);
  renderActivity(snapshot.activity, snapshot.audit);
  $("updated-at").textContent = `Updated ${shortTime(snapshot.generated_at)}`;
  $("strategy-summary").textContent = `${snapshot.overview.strategy_count} × ${moneyText(numeric(snapshot.overview.shadow_capital_usd) / snapshot.overview.strategy_count)} shadow capital`;
  $("schema-version").textContent = `Snapshot schema v${snapshot.schema_version}`;
}

async function initialLoad() {
  const response = await fetch("/api/snapshot", { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!response.ok) throw new Error(`snapshot returned ${response.status}`);
  render(await response.json());
}

function setConnection(state, message) {
  $("stream-dot").className = `status-dot ${state}`;
  $("stream-status").textContent = message;
}

function connectStream() {
  const stream = new EventSource("/api/events");
  stream.addEventListener("open", () => setConnection("", "Live stream"));
  stream.addEventListener("snapshot", (event) => {
    try { render(JSON.parse(event.data)); setConnection("", "Live stream"); }
    catch (error) { console.error("Invalid dashboard snapshot", error); setConnection("error", "Invalid snapshot"); }
  });
  stream.addEventListener("error", () => setConnection("pending", "Reconnecting"));
}

initialLoad()
  .then(connectStream)
  .catch((error) => {
    console.error("Dashboard unavailable", error);
    setConnection("error", "Dashboard unavailable");
  });

$("kill-button").addEventListener("click", () => {
  if (!window.confirm("Lock all new orders now?")) return;
  postJson("/api/control/kill", { reason: "dashboard manual kill" })
    .then(initialLoad)
    .catch((error) => window.alert(error.message));
});

$("resume-button").addEventListener("click", () => {
  const confirmation = window.prompt("Type RESUME_DEMO. Resume fails if audit or reconciliation is unsafe.");
  if (confirmation !== "RESUME_DEMO") return;
  postJson("/api/control/resume", { confirmation })
    .then(initialLoad)
    .catch((error) => window.alert(error.message));
});

window.addEventListener("resize", () => {
  // The latest P&L series is refreshed by the stream; this avoids retaining operational data globally.
  const chart = $("pnl-chart");
  const context = chart.getContext("2d");
  context.clearRect(0, 0, chart.width, chart.height);
});
