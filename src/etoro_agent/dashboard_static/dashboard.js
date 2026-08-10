"use strict";

const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const appState = { snapshot: null, trades: [], reviews: [], proposals: [], usage: null, strategyFilter: "all", routeToken: 0 };

function numeric(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}
function moneyText(value) { return money.format(numeric(value)); }
function signedMoney(value) {
  const amount = numeric(value);
  return `${amount > 0 ? "+" : ""}${money.format(amount)}`;
}
function percentText(value, fraction = false) {
  const amount = numeric(value) * (fraction ? 100 : 1);
  return `${amount.toFixed(2)}%`;
}
function pnlClass(value) { return numeric(value) > 0 ? "positive" : numeric(value) < 0 ? "negative" : ""; }
function shortTime(value, includeSeconds = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return date.toLocaleString([], {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
    ...(includeSeconds ? { second: "2-digit" } : {}),
  });
}
function durationText(value, start, end) {
  if (value !== undefined && value !== null && value !== "") {
    if (typeof value === "string" && /[a-z:]/i.test(value)) return value;
    const seconds = numeric(value);
    if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)}d`;
    if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}h`;
    return `${Math.max(0, Math.round(seconds / 60))}m`;
  }
  const elapsed = new Date(end).valueOf() - new Date(start).valueOf();
  return Number.isFinite(elapsed) && elapsed >= 0 ? durationText(elapsed / 1000) : "—";
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function empty(target, text) { target.replaceChildren(node("div", "empty", text)); }
function badge(status) {
  return node("span", `badge ${String(status || "unknown").toLowerCase()}`, String(status || "unknown").replaceAll("_", " "));
}
function jsonDetails(value, label = "Details") {
  const wrapper = document.createElement("details");
  wrapper.append(node("summary", "", label), node("pre", "", JSON.stringify(value, null, 2)));
  return wrapper;
}
function asArray(payload, keys = []) {
  if (Array.isArray(payload)) return payload;
  for (const key of [...keys, "items", "data", "results"]) if (Array.isArray(payload?.[key])) return payload[key];
  return [];
}
function firstValue(source, keys, fallback = null) {
  for (const key of keys) if (source?.[key] !== undefined && source[key] !== null) return source[key];
  return fallback;
}
function optionalNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value); return Number.isFinite(parsed) ? parsed : null;
}

async function getJson(path, optional = false) {
  try {
    const response = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) throw new Error(`${path} returned ${response.status}`);
    return await response.json();
  } catch (error) {
    if (!optional) throw error;
    console.info("Optional dashboard endpoint unavailable", path, error.message);
    return null;
  }
}
async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-ETORO-CSRF": "1", Accept: "application/json" },
    body: JSON.stringify(payload), cache: "no-store",
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
    envelope_hash: approval.envelope_hash, confirmation: entered,
  });
  await initialLoad();
}

function metricCard(label, value, foot, tone = "") {
  const card = node("article", "metric");
  card.append(node("span", "label", label), node("strong", `value ${tone}`, value), node("span", "foot", foot));
  return card;
}
function renderMetricGrid(target, metrics) {
  target.replaceChildren(...metrics.map((item) => metricCard(...item)));
}

function renderOverview(snapshot) {
  const overview = snapshot.overview || {};
  const kill = snapshot.kill_switch || {};
  renderMetricGrid($("overview"), [
    ["AI master NAV", moneyText(overview.master_equity_usd), "single $1,000 budget simulation", pnlClass(overview.master_daily_pnl_usd)],
    ["AI master today", signedMoney(overview.master_daily_pnl_usd), snapshot.master?.position ? `position ${snapshot.master.position.symbol || "open"}` : "no open position", pnlClass(overview.master_daily_pnl_usd)],
    ["Sol queue", String(overview.ai_pending || 0), snapshot.ai?.latest ? `${String(snapshot.ai.latest.state || "latest").toLowerCase()} · ${snapshot.ai.latest.packet_id || "packet"}` : "waiting for candidate", overview.ai_pending ? "warning" : ""],
    ["Research capital", moneyText(overview.shadow_capital_usd), `${overview.strategy_count || 0} isolated ledgers`],
    ["Daily P&L", signedMoney(overview.daily_pnl_usd), "realized + unrealized", pnlClass(overview.daily_pnl_usd)],
    ["Kill state", kill.active ? "HALTED" : "READY", kill.active ? "new orders blocked" : "deterministic gate ready", kill.active ? "negative" : "positive"],
  ]);
  $("halt-banner").classList.toggle("hidden", !kill.active);
  renderOverviewStrategies(snapshot.strategies || []);
}

function sortedStrategies(strategies) {
  return [...strategies].sort((a, b) => {
    if (Boolean(a.top3) !== Boolean(b.top3)) return a.top3 ? -1 : 1;
    if (a.rank && b.rank) return numeric(a.rank) - numeric(b.rank);
    return String(a.name || a.id).localeCompare(String(b.name || b.id));
  });
}
function hasPosition(strategy) {
  const explicitState = `${strategy.position_state || ""} ${strategy.status || ""}`.toLowerCase();
  return Boolean(strategy.position || strategy.has_position || strategy.open_position || strategy.positions?.length || /position.open|open.position|\blong\b|\bshort\b/.test(explicitState) || appState.trades.some((trade) => trade.strategy_id === strategy.id && trade.status === "open"));
}
function renderOverviewStrategies(strategies) {
  const target = $("overview-strategies");
  const cards = sortedStrategies(strategies).slice(0, 5);
  if (!cards.length) return empty(target, "No strategies available.");
  target.replaceChildren(...cards.map((strategy) => {
    const link = node("a", "mini-strategy");
    link.href = `#strategies/${encodeURIComponent(strategy.id)}`;
    link.append(node("strong", "", strategy.name || strategy.id), node("span", "", strategy.rank ? `#${strategy.rank}` : "unranked"), node("strong", pnlClass(strategy.total_pnl_usd), signedMoney(strategy.total_pnl_usd)));
    return link;
  }));
}
function renderStrategies(strategies) {
  let cards = sortedStrategies(strategies);
  if (appState.strategyFilter === "top3") cards = cards.filter((item) => item.top3);
  if (appState.strategyFilter === "positions") cards = cards.filter(hasPosition);
  const target = $("strategies");
  if (!cards.length) return empty(target, "No strategies match this filter.");
  target.replaceChildren(...cards.map((strategy) => {
    const card = node("a", `strategy-card ${strategy.top3 ? "top3" : ""}`);
    card.href = `#strategies/${encodeURIComponent(strategy.id)}`;
    const head = node("div", "strategy-head");
    const title = node("div");
    title.append(node("div", "strategy-name", strategy.name || strategy.id), node("div", "strategy-family", strategy.family || "strategy"));
    head.append(title, node("span", "rank", strategy.rank ? `#${strategy.rank}` : "—"));
    const stats = node("div", "strategy-stats");
    [["Today", signedMoney(strategy.daily_pnl_usd), pnlClass(strategy.daily_pnl_usd)], ["Drawdown", percentText(strategy.drawdown_fraction, true), ""], ["Trades", String(strategy.trades || 0), ""]].forEach(([label, value, tone]) => {
      const item = node("div"); item.append(node("span", "", label), node("strong", tone, value)); stats.append(item);
    });
    const signal = String(strategy.signal_state || strategy.last_signal || strategy.status || "waiting").replaceAll("_", " ");
    const position = hasPosition(strategy) ? "position open" : "no position";
    card.append(head, node("div", "strategy-nav", moneyText(strategy.nav_usd)), stats, node("div", `status-tag ${hasPosition(strategy) ? "position" : ""}`, `${signal} · ${position}`));
    return card;
  }));
}

function renderPnl(pnl = {}) {
  const rows = pnl.daily || [];
  const table = $("pnl-table");
  if (!rows.length) empty(table, "No daily P&L records yet.");
  else {
    const header = node("div", "table-row header");
    ["Day", "Realized", "Unrealized", "Equity"].forEach((label) => header.append(node("span", "", label)));
    const body = rows.slice(-6).reverse().map((row) => {
      const item = node("div", "table-row");
      item.append(node("span", "", row.day), node("span", pnlClass(row.realized_usd), signedMoney(row.realized_usd)), node("span", pnlClass(row.unrealized_usd), signedMoney(row.unrealized_usd)), node("span", "", moneyText(row.equity_usd)));
      return item;
    });
    table.replaceChildren(header, ...body);
  }
  $("equity-chart-note").textContent = rows.length ? `${rows.length} daily records` : "Waiting for records";
  drawLineChart($("pnl-chart"), rows.map((row) => numeric(row.equity_usd)), { color: "#5367e8", fill: "rgba(83,103,232,.10)", zeroWhenEmpty: true });
}

function canvasContext(canvas) {
  if (!canvas) return null;
  const box = canvas.getBoundingClientRect();
  const width = Math.max(1, box.width || canvas.parentElement?.clientWidth || 320);
  const height = Math.max(1, box.height || canvas.parentElement?.clientHeight || 190);
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * ratio); canvas.height = Math.floor(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio); ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}
function drawGrid(ctx, width, height) {
  ctx.strokeStyle = "#e8edf3"; ctx.lineWidth = 1;
  for (let line = 0; line < 5; line += 1) {
    const y = 12 + ((height - 24) * line / 4);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
}
function drawLineChart(canvas, input, options = {}) {
  const context = canvasContext(canvas); if (!context) return;
  const { ctx, width, height } = context; drawGrid(ctx, width, height);
  const values = input.map((value) => numeric(value));
  if (!values.length) return;
  const low = Math.min(...values); const high = Math.max(...values);
  const spread = Math.max(high - low, Math.abs(high) * .005, 1);
  const points = values.map((value, index) => ({ x: values.length === 1 ? width / 2 : 5 + ((width - 10) * index / (values.length - 1)), y: 12 + ((height - 26) * (high - value + spread * .08) / (spread * 1.16)) }));
  if (options.fill && points.length > 1) {
    ctx.beginPath(); ctx.moveTo(points[0].x, height - 10); points.forEach((point) => ctx.lineTo(point.x, point.y)); ctx.lineTo(points.at(-1).x, height - 10); ctx.closePath(); ctx.fillStyle = options.fill; ctx.fill();
  }
  ctx.beginPath(); points.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
  ctx.strokeStyle = options.color || "#5367e8"; ctx.lineWidth = 2.2; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.stroke();
  points.forEach((point) => { ctx.beginPath(); ctx.arc(point.x, point.y, 2.5, 0, Math.PI * 2); ctx.fillStyle = options.color || "#5367e8"; ctx.fill(); });
}
function drawBarChart(canvas, input, options = {}) {
  const context = canvasContext(canvas); if (!context) return;
  const { ctx, width, height } = context; drawGrid(ctx, width, height);
  const values = input.map((value) => numeric(value)); if (!values.length) return;
  const max = Math.max(...values.map(Math.abs), 1); const mid = options.signed ? height / 2 : height - 14;
  const slot = (width - 12) / values.length; const barWidth = Math.max(3, slot * .58);
  if (options.signed) { ctx.strokeStyle = "#cad4df"; ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(width, mid); ctx.stroke(); }
  values.forEach((value, index) => {
    const scaled = Math.abs(value) / max * (options.signed ? height * .39 : height - 28);
    const x = 6 + slot * index + (slot - barWidth) / 2; const y = options.signed ? (value >= 0 ? mid - scaled : mid) : mid - scaled;
    ctx.fillStyle = value < 0 ? "#d44255" : (options.colors?.[index] || options.color || "#5367e8");
    ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(x, y, barWidth, Math.max(1, scaled), 3); else ctx.rect(x, y, barWidth, Math.max(1, scaled)); ctx.fill();
  });
}
function drawDonutChart(canvas, segments) {
  const context = canvasContext(canvas); if (!context) return;
  const { ctx, width, height } = context; const total = segments.reduce((sum, item) => sum + Math.max(0, numeric(item.value)), 0);
  const centerX = width / 2; const centerY = height / 2; const radius = Math.min(width, height) * .32;
  if (!total) { ctx.strokeStyle = "#e8edf3"; ctx.lineWidth = 18; ctx.beginPath(); ctx.arc(centerX, centerY, radius, 0, Math.PI * 2); ctx.stroke(); return; }
  let angle = -Math.PI / 2;
  segments.forEach((segment, index) => { const slice = Math.max(0, numeric(segment.value)) / total * Math.PI * 2; ctx.strokeStyle = segment.color || ["#5367e8", "#128966", "#e9a336", "#2a78c7", "#d44255"][index % 5]; ctx.lineWidth = 18; ctx.beginPath(); ctx.arc(centerX, centerY, radius, angle, angle + slice - .025); ctx.stroke(); angle += slice; });
  ctx.fillStyle = "#172b3c"; ctx.font = "700 16px -apple-system, sans-serif"; ctx.textAlign = "center"; ctx.fillText(integer.format(total), centerX, centerY + 5);
}

function renderHealth(health = { status: "degraded", checks: [] }) {
  $("health-summary").textContent = String(health.status || "degraded").toUpperCase();
  $("health-summary").className = `health-pill ${health.status === "ok" ? "positive" : health.status === "halted" ? "negative" : "warning"}`;
  const checks = health.checks || [];
  if (!checks.length) return empty($("health"), "No health checks reported.");
  $("health").replaceChildren(...checks.map((check) => {
    const item = node("div", "health-item"); const line = node("div", "health-line");
    const tone = check.status === "ok" || check.status === "healthy" ? "positive" : check.status === "halted" || check.status === "error" ? "negative" : "warning";
    line.append(node("span", "health-name", check.name), node("span", `health-status ${tone}`, check.status));
    item.append(line, node("span", "health-detail", typeof check.detail === "string" ? check.detail : JSON.stringify(check.detail)));
    return item;
  }));
}
function renderOrders(orders = []) {
  const target = $("orders"); if (!orders.length) return empty(target, "No DEMO order lifecycle events.");
  target.replaceChildren(...orders.map((order) => {
    const item = node("article", "order"); const head = node("div", "order-head");
    head.append(node("span", "proposal", order.proposal_id), badge(order.status));
    const meta = node("div", "order-meta"); [order.symbol || "no symbol", order.strategy_id || "unattributed", shortTime(order.updated_at)].forEach((text) => meta.append(node("span", "", text)));
    item.append(head, meta, jsonDetails(order.lifecycle || [], `${(order.lifecycle || []).length} lifecycle events`)); return item;
  }));
}
function renderApprovals(approvals = []) {
  const target = $("approvals"); if (!approvals.length) return empty(target, "No authorization records. Sealed Sol DEMO orders may use standing policy.");
  target.replaceChildren(...approvals.map((approval) => {
    const item = node("article", "approval"); const head = node("div", "approval-head");
    head.append(node("span", "proposal", approval.proposal_id), badge(approval.status)); item.append(head, jsonDetails(approval.request, "Exact DEMO request"));
    if (approval.status === "awaiting_owner" && approval.envelope_hash) {
      const button = node("button", "approve-button", "Approve this DEMO order"); button.type = "button";
      button.addEventListener("click", () => approveProposal(approval).catch((error) => window.alert(error.message))); item.append(button);
    }
    return item;
  }));
}
function activityRows(activity = []) {
  return activity.map((event) => {
    const row = node("div", "activity-row");
    row.append(node("span", "activity-time", shortTime(event.ts).split(", ").pop()), node("span", "activity-type", event.event_type || "event"), node("span", "activity-payload", JSON.stringify(event.payload || {})), node("span", "activity-hash", String(event.event_hash || "").slice(0, 10)));
    row.title = JSON.stringify(event.payload || {}, null, 2); return row;
  });
}
function renderActivity(activity = [], audit = {}) {
  $("audit-summary").textContent = audit.readable ? `${audit.events_loaded || 0} recent · ${audit.latest_event_hash ? audit.latest_event_hash.slice(0, 8) : "no hash"}` : "Audit unavailable";
  for (const id of ["activity", "overview-activity"]) {
    const target = $(id); if (!activity.length) empty(target, "No activity events recorded."); else target.replaceChildren(...activityRows(id === "overview-activity" ? activity.slice(0, 7) : activity));
  }
}

function normalizeTrade(raw, index = 0) {
  const entryTime = firstValue(raw, ["entry_at", "entry_time", "opened_at", "open_time", "created_at"]);
  const exitTime = firstValue(raw, ["exit_at", "exit_time", "closed_at", "close_time"]);
  const fees = optionalNumber(firstValue(raw, ["fees_usd", "fee_usd", "commission_usd"]));
  const spread = optionalNumber(firstValue(raw, ["spread_cost_usd", "spread_usd"]));
  const slippage = optionalNumber(firstValue(raw, ["slippage_usd", "slippage_cost_usd"]));
  const financing = optionalNumber(firstValue(raw, ["financing_usd", "overnight_fee_usd"]));
  const explicitCost = firstValue(raw, ["cost_usd", "costs_usd", "total_cost_usd"]);
  const fillTimeline = asArray(raw.fills).map((fill) => ({
    ts: firstValue(fill, ["timestamp", "ts"]),
    event: `${String(firstValue(fill, ["role"], "fill")).toUpperCase()} · ${String(firstValue(fill, ["side"], "")).toUpperCase()} ${firstValue(fill, ["units"], "—")} @ ${firstValue(fill, ["price"], "—")}`,
  }));
  return {
    ...raw,
    trade_id: String(firstValue(raw, ["trade_id", "id", "round_trip_id", "proposal_id"], `trade-${index + 1}`)),
    strategy_id: String(firstValue(raw, ["strategy_id", "strategy", "portfolio_id"], "unattributed")),
    strategy_name: firstValue(raw, ["strategy_name", "name"], null),
    symbol: String(firstValue(raw, ["symbol", "instrument", "ticker"], "—")),
    side: String(firstValue(raw, ["side", "direction", "action"], "—")).toUpperCase(),
    entry_time: entryTime,
    exit_time: exitTime,
    entry_price: firstValue(raw, ["entry_price", "entry_average_price", "open_price", "price_in"]),
    exit_price: firstValue(raw, ["exit_price", "exit_average_price", "close_price", "price_out"]),
    notional_usd: firstValue(raw, ["notional_usd", "entry_notional_usd", "value_usd", "amount_usd", "notional", "amount"]),
    quantity: firstValue(raw, ["quantity", "entry_units", "units", "size"]),
    gross_pnl_usd: firstValue(raw, ["gross_pnl_usd", "gross_pnl", "pnl_before_costs_usd"]),
    net_pnl_usd: firstValue(raw, ["net_pnl_usd", "pnl_usd", "realized_pnl_usd", "pnl"], 0),
    cost_usd: explicitCost === null ? [fees, spread, slippage, financing].filter((value) => value !== null).reduce((sum, value) => sum + value, 0) : numeric(explicitCost),
    fees_usd: fees, spread_cost_usd: spread, slippage_usd: slippage, financing_usd: financing,
    duration: durationText(firstValue(raw, ["duration_seconds", "holding_seconds", "duration"]), entryTime, exitTime),
    status: String(firstValue(raw, ["status", "state"], exitTime ? "closed" : "open")).toLowerCase(),
    exit_reason: firstValue(raw, ["exit_reason", "close_reason", "reason"], "—"),
    review: firstValue(raw, ["review", "ai_review", "minimax_review"]),
    timeline: asArray(firstValue(raw, ["timeline", "lifecycle", "events"], fillTimeline)),
    pricing_quality: firstValue(raw, ["pricing_quality"], "PAPER_SIMULATED_NEXT_QUOTE"),
  };
}
function fallbackTrades(snapshot, strategyId = null) {
  const source = (snapshot.orders || []).filter((order) => !strategyId || order.strategy_id === strategyId);
  return source.map((order, index) => normalizeTrade({ ...order, trade_id: order.proposal_id, entry_at: order.updated_at, exit_reason: "Execution lifecycle; round-trip endpoint unavailable" }, index));
}
async function loadStrategyTrades(strategyId) {
  const items = await loadPaged(`/api/strategies/${encodeURIComponent(strategyId)}/trades`, "trades");
  return (items.length ? items : fallbackTrades(appState.snapshot || {}, strategyId)).map(normalizeTrade);
}
async function loadAllTrades() {
  let items = await loadPaged("/api/trades", "trades");
  if (!items.length && appState.snapshot?.strategies?.length) {
    const groups = await Promise.all(appState.snapshot.strategies.map((strategy) => loadStrategyTrades(strategy.id)));
    items = groups.flat();
  }
  const deduped = new Map(); items.map(normalizeTrade).forEach((trade) => deduped.set(trade.trade_id, trade));
  appState.trades = [...deduped.values()].sort((a, b) => String(b.exit_time || b.entry_time || "").localeCompare(String(a.exit_time || a.entry_time || "")));
  return appState.trades;
}

async function loadPaged(path, key, pageSize = 100) {
  const items = [];
  for (let offset = 0; offset < 10000; offset += pageSize) {
    const payload = await getJson(`${path}${path.includes("?") ? "&" : "?"}limit=${pageSize}&offset=${offset}`, true);
    if (!payload) break;
    const page = asArray(payload, [key]); items.push(...page);
    if (page.length < pageSize || items.length >= numeric(payload.total, items.length)) break;
  }
  return items;
}

function tradeTable(trades, { includeStrategy = true } = {}) {
  const wrap = node("div");
  if (!trades.length) { empty(wrap, "No reconstructed round-trip trades yet. Execution events remain available under Risk & System."); return wrap; }
  const table = node("table", "data-table"); const thead = node("thead"); const head = node("tr");
  const columns = [...(includeStrategy ? ["Strategy"] : []), "Symbol", "Side", "Entry", "Exit", "Value", "Cost", "Net P&L", "Duration", "Status"];
  columns.forEach((label) => head.append(node("th", ["Value", "Cost", "Net P&L"].includes(label) ? "numeric" : "", label))); thead.append(head);
  const tbody = node("tbody");
  trades.forEach((trade) => {
    const row = node("tr", "trade-row"); row.tabIndex = 0; row.dataset.tradeId = trade.trade_id;
    const values = [...(includeStrategy ? [trade.strategy_name || trade.strategy_id] : []), trade.symbol, trade.side, trade.entry_price === null ? "—" : number.format(numeric(trade.entry_price)), trade.exit_price === null ? "—" : number.format(numeric(trade.exit_price)), trade.notional_usd === null ? "—" : moneyText(trade.notional_usd), moneyText(trade.cost_usd), signedMoney(trade.net_pnl_usd), trade.duration];
    values.forEach((value, index) => { const numericColumn = index >= values.length - 5 && index <= values.length - 3; row.append(node("td", `${numericColumn ? "numeric " : ""}${index === values.length - 3 ? pnlClass(trade.net_pnl_usd) : ""}`, value)); });
    const statusCell = node("td"); statusCell.append(badge(trade.status)); row.append(statusCell);
    const detailRow = node("tr", "trade-detail-row hidden"); const detailCell = node("td"); detailCell.colSpan = columns.length; detailCell.append(buildTradeDetail(trade)); detailRow.append(detailCell);
    const toggle = () => { detailRow.classList.toggle("hidden"); if (!detailRow.classList.contains("hidden")) hydrateTradeDetail(trade, detailCell); };
    row.addEventListener("click", toggle); row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } });
    tbody.append(row, detailRow);
  });
  table.append(thead, tbody); wrap.append(table); return wrap;
}
function buildTradeDetail(trade) {
  const detail = node("div", "trade-detail");
  const lifecycle = node("div"); lifecycle.append(node("h4", "", "Lifecycle")); const timeline = node("div", "timeline-mini");
  const events = trade.timeline.length ? trade.timeline : [{ event: "Opened", ts: trade.entry_time }, ...(trade.exit_time ? [{ event: `Closed · ${trade.exit_reason}`, ts: trade.exit_time }] : [])];
  events.forEach((event) => timeline.append(node("div", "", `${shortTime(event.ts)} · ${event.event || event.type || event.state || "event"}`))); lifecycle.append(timeline);
  const review = node("div"); review.append(node("h4", "", "AI review"));
  if (trade.review) review.append(node("p", "", typeof trade.review === "string" ? trade.review : firstValue(trade.review, ["summary", "feedback", "verdict"], JSON.stringify(trade.review))));
  else review.append(node("p", "", "No MiniMax-M3 review attached yet."));
  const knownCost = (label, value) => `${label} ${value === null ? "unavailable" : moneyText(value)}`;
  review.append(node("p", "", `Gross ${trade.gross_pnl_usd === null ? "—" : signedMoney(trade.gross_pnl_usd)} · ${knownCost("spread", trade.spread_cost_usd)} · ${knownCost("slippage", trade.slippage_usd)} · ${knownCost("fees", trade.fees_usd)}`));
  review.append(node("p", "review-meta", `Pricing: ${String(trade.pricing_quality || "estimated").replaceAll("_", " ").toLowerCase()}`));
  detail.append(lifecycle, review); return detail;
}
async function hydrateTradeDetail(trade, target) {
  if (target.dataset.loaded === "true") return;
  const payload = await getJson(`/api/trades/${encodeURIComponent(trade.trade_id)}`, true); if (!payload) return;
  const merged = normalizeTrade({ ...trade, ...(payload.trade || payload) }); target.replaceChildren(buildTradeDetail(merged)); target.dataset.loaded = "true";
}

async function renderStrategyDetail(strategyId, token) {
  const fallback = appState.snapshot?.strategies?.find((item) => item.id === strategyId) || { id: strategyId, name: strategyId, family: "strategy" };
  const payload = await getJson(`/api/strategies/${encodeURIComponent(strategyId)}`, true);
  if (token !== appState.routeToken) return;
  const body = payload?.strategy || payload || {};
  const strategy = { ...fallback, ...(body.card || {}), ...body, ...(body.metrics || {}) };
  const position = body.position || body.positions?.[0] || strategy.position || null;
  $("strategy-detail-title").textContent = strategy.name || strategy.id;
  $("strategy-detail-family").textContent = String(strategy.family || "strategy").toUpperCase();
  $("strategy-detail-subtitle").textContent = strategy.description || `${strategy.symbol || strategy.instrument || "Multi-asset"} · ${strategy.timeframe || strategy.interval || "configured cadence"}`;
  $("strategy-version").textContent = `version ${strategy.version || strategy.strategy_version || "current"}`;
  $("strategy-detail-state").replaceWith(Object.assign(badge(strategy.status || "waiting"), { id: "strategy-detail-state" }));
  renderMetricGrid($("strategy-detail-metrics"), [
    ["NAV", moneyText(firstValue(strategy, ["nav_usd", "equity_usd"], 1000)), "isolated shadow ledger", pnlClass(strategy.total_pnl_usd)],
    ["Total P&L", signedMoney(strategy.total_pnl_usd), "net result", pnlClass(strategy.total_pnl_usd)],
    ["Today", signedMoney(strategy.daily_pnl_usd), "realized + unrealized", pnlClass(strategy.daily_pnl_usd)],
    ["Drawdown", percentText(strategy.drawdown_fraction, true), "peak to current", numeric(strategy.drawdown_fraction) > .08 ? "negative" : ""],
    ["Win rate", strategy.win_rate == null && strategy.win_rate_fraction == null ? "—" : percentText(firstValue(strategy, ["win_rate", "win_rate_fraction"], 0), numeric(firstValue(strategy, ["win_rate", "win_rate_fraction"], 0)) <= 1), `${strategy.wins || 0} wins`],
    ["Profit factor", strategy.profit_factor == null ? "—" : number.format(numeric(strategy.profit_factor)), `${strategy.trades || 0} trades`],
  ]);
  const current = $("strategy-current-state"); current.replaceChildren();
  [["Signal state", firstValue(strategy, ["signal_state", "last_signal", "status"], "waiting")], ["Position state", hasPosition(strategy) ? "OPEN" : "FLAT"], ["Symbol", position?.symbol || strategy.symbol || "—"], ["Units", position?.units || position?.quantity || "—"], ["Research epoch", strategy.research_epoch || "—"], ["Promotion", strategy.eligible_for_promotion === false ? "excluded" : "eligible"]].forEach(([label, value]) => { const row = node("div", "key-value"); row.append(node("span", "", label), node("strong", "", String(value).replaceAll("_", " "))); current.append(row); });
  const curve = asArray(firstValue(strategy, ["equity_curve", "daily_pnl", "performance"], []));
  const values = curve.map((point) => typeof point === "number" ? point : numeric(firstValue(point, ["equity_usd", "nav_usd", "equity", "value"]))).filter(Number.isFinite);
  drawLineChart($("strategy-equity-chart"), values.length ? values : [numeric(strategy.nav_usd, 1000)], { color: "#128966", fill: "rgba(18,137,102,.10)" });
  const trades = await loadStrategyTrades(strategyId); if (token !== appState.routeToken) return;
  $("strategy-trades-summary").textContent = `${trades.length} records`;
  $("strategy-trades").replaceChildren(tradeTable(trades, { includeStrategy: false }));
}

function tradeMatchesFilters(trade) {
  const query = $("trade-search").value.trim().toLowerCase(); const result = $("trade-result-filter").value;
  if (query && !`${trade.symbol} ${trade.strategy_id} ${trade.strategy_name || ""}`.toLowerCase().includes(query)) return false;
  if (result === "win" && numeric(trade.net_pnl_usd) <= 0) return false;
  if (result === "loss" && numeric(trade.net_pnl_usd) >= 0) return false;
  if (result === "open" && trade.status !== "open") return false;
  return true;
}
function renderTrades() {
  const trades = appState.trades.filter(tradeMatchesFilters); const closed = appState.trades.filter((trade) => trade.status !== "open");
  const wins = closed.filter((trade) => numeric(trade.net_pnl_usd) > 0); const costs = closed.reduce((sum, trade) => sum + numeric(trade.cost_usd), 0); const net = closed.reduce((sum, trade) => sum + numeric(trade.net_pnl_usd), 0);
  renderMetricGrid($("trade-summary"), [["Completed", integer.format(closed.length), `${appState.trades.length - closed.length} open`], ["Win rate", closed.length ? percentText(wins.length / closed.length, true) : "—", `${wins.length} winners`], ["Net P&L", signedMoney(net), "after costs", pnlClass(net)], ["Trading costs", moneyText(costs), "spread + fees + slippage"], ["Avg. result", closed.length ? signedMoney(net / closed.length) : "—", "per completed trade", pnlClass(net)], ["Displayed", integer.format(trades.length), "current filters"]]);
  $("trade-count").textContent = `${trades.length} of ${appState.trades.length} records`; $("trades-table").replaceChildren(tradeTable(trades));
  drawBarChart($("distribution-chart"), closed.slice(0, 24).reverse().map((trade) => trade.net_pnl_usd), { signed: true });
  const costGroups = [closed.reduce((sum, trade) => sum + trade.spread_cost_usd, 0), closed.reduce((sum, trade) => sum + trade.slippage_usd, 0), closed.reduce((sum, trade) => sum + trade.fees_usd + trade.financing_usd, 0)];
  drawBarChart($("cost-chart"), costGroups, { colors: ["#5367e8", "#e9a336", "#2a78c7"] });
  const exposure = new Map(); appState.trades.filter((trade) => trade.status === "open").forEach((trade) => exposure.set(trade.symbol, (exposure.get(trade.symbol) || 0) + numeric(trade.notional_usd)));
  if (!exposure.size) appState.trades.forEach((trade) => exposure.set(trade.symbol, (exposure.get(trade.symbol) || 0) + Math.abs(numeric(trade.notional_usd))));
  drawDonutChart($("exposure-chart"), [...exposure.entries()].map(([label, value]) => ({ label, value })));
}

function normalizeReview(raw, index, kind = "trade_review") {
  const content = raw.review || raw.proposal || {};
  return { ...raw, kind, review_id: firstValue(raw, ["review_id", "proposal_id", "id"], `review-${index + 1}`), trade_id: firstValue(raw, ["trade_id", "round_trip_id"]), strategy_id: firstValue(raw, ["strategy_id", "strategy"], "unattributed"), model: firstValue(raw, ["model", "model_id"], "MiniMax-M3"), created_at: firstValue(raw, ["created_at", "ts", "reviewed_at"]), verdict: String(firstValue(content, ["verdict", "classification", "status"], firstValue(raw, ["verdict", "state", "status"], kind === "proposal" ? "research_only" : "reviewed"))).replaceAll("_", " "), summary: firstValue(content, ["summary", "feedback", "analysis", "rationale", "thesis", "objective"], firstValue(raw, ["summary", "feedback"], "Review completed; open the structured record for details.")), confidence: firstValue(content, ["confidence", "score"], firstValue(raw, ["confidence", "score"])), tags: asArray(firstValue(content, ["tags", "findings", "issues", "experiments", "suggested_experiments", "evidence"], firstValue(raw, ["tags", "findings", "issues"], []))) };
}
async function loadReviews() {
  const payload = await getJson("/api/reviews?limit=100", true); let reviews = asArray(payload, ["reviews"]);
  appState.proposals = asArray(payload?.proposals).map((item, index) => normalizeReview(item, index, "proposal"));
  if (!reviews.length) reviews = (appState.snapshot?.activity || []).filter((event) => /review|minimax|strategy_change_proposal/i.test(event.event_type || "")).map((event) => ({ ...event.payload, created_at: event.ts, review_id: event.event_hash }));
  appState.reviews = reviews.map((item, index) => normalizeReview(item, index)); return appState.reviews;
}
function renderReviews() {
  const completed = appState.reviews.length; const actionable = appState.reviews.filter((review) => /bad|improve|action|deviation/i.test(`${review.verdict} ${review.summary}`)).length; const latest = appState.reviews[0];
  renderMetricGrid($("review-summary"), [["Reviews", integer.format(completed), "one per completed round trip"], ["Actionable", integer.format(actionable), "requires experiment"], ["Coverage", appState.trades.length ? percentText(completed / Math.max(1, appState.trades.filter((item) => item.status !== "open").length), true) : "—", "closed trades reviewed"], ["Reviewer", latest?.model || "MiniMax-M3", "stateless post-trade"], ["Last review", latest ? shortTime(latest.created_at) : "—", "audit timestamp"], ["Authority", "NONE", "analysis cannot execute"]]);
  const combined = [...appState.proposals, ...appState.reviews].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const target = $("reviews-list"); if (!combined.length) return empty(target, "No post-trade AI reviews yet. MiniMax-M3 runs only after a round trip closes.");
  target.replaceChildren(...combined.map((review) => {
    const card = node("article", "review-card"); const head = node("div", "review-head"); const title = node("div");
    title.append(node("h3", "", review.trade_id ? `Trade ${review.trade_id}` : `Strategy proposal · ${review.strategy_id}`)); const meta = node("div", "review-meta"); [review.model, review.strategy_id, shortTime(review.created_at)].forEach((value) => meta.append(node("span", "", value || "—"))); title.append(meta); head.append(title, badge(review.verdict));
    card.append(head, node("p", "review-body", typeof review.summary === "string" ? review.summary : JSON.stringify(review.summary)));
    const tags = node("div", "review-tags"); review.tags.slice(0, 6).forEach((tag) => tags.append(node("span", "", typeof tag === "string" ? tag : firstValue(tag, ["label", "finding", "message"], JSON.stringify(tag))))); if (review.confidence !== null) tags.append(node("span", "", `confidence ${number.format(numeric(review.confidence))}`)); card.append(tags); return card;
  }));
}

async function loadUsage() {
  const payload = await getJson("/api/ai/usage", true);
  if (payload) { appState.usage = payload; return payload; }
  const events = (appState.snapshot?.activity || []).filter((event) => /ai_|sol_|minimax|llm/i.test(event.event_type || ""));
  appState.usage = { totals: { calls: events.length, input_tokens: null, output_tokens: null, errors: events.filter((event) => /error|fail/i.test(event.event_type)).length }, runs: events.map((event) => ({ created_at: event.ts, model: /minimax/i.test(event.event_type) ? "MiniMax-M3" : "gpt-5.6-sol", status: event.event_type, run_id: event.event_hash })), daily: [], budgets: [] };
  return appState.usage;
}
function renderUsage() {
  const usage = appState.usage || {}; const totals = usage.totals || usage.summary || {}; const runs = asArray(usage.recent || usage.runs || usage, ["recent", "runs"]); const daily = asArray(usage.daily || [], ["daily"]); let budgets = asArray(usage.budgets || [], ["budgets"]);
  const sumDaily = (field) => daily.reduce((sum, item) => sum + numeric(item[field]), 0);
  const input = firstValue(totals, ["input_tokens", "prompt_tokens"], daily.length ? sumDaily("input_tokens") : null); const output = firstValue(totals, ["output_tokens", "completion_tokens"], daily.length ? sumDaily("output_tokens") : null); const cache = firstValue(totals, ["cache_read_tokens", "cached_tokens"], daily.length ? sumDaily("cache_read_tokens") : null); const latency = firstValue(totals, ["average_latency_ms", "avg_latency_ms"], daily.length ? daily.reduce((sum, item) => sum + numeric(item.average_latency_ms), 0) / daily.length : null);
  const today = new Date().toISOString().slice(0, 10);
  budgets = budgets.map((budget) => ({ ...budget, used: daily.filter((item) => item.day === today && String(item.model) === String(budget.model)).reduce((sum, item) => sum + numeric(item.runs), 0) }));
  renderMetricGrid($("usage-summary"), [["Calls", integer.format(firstValue(totals, ["calls", "total_calls", "runs"], runs.length)), "recorded model runs"], ["Input tokens", input === null ? "Unavailable" : integer.format(input), "exact when provider exposes it"], ["Output tokens", output === null ? "Unavailable" : integer.format(output), "exact when provider exposes it"], ["Cache read", cache === null ? "Unavailable" : integer.format(cache), "provider telemetry"], ["Avg. latency", latency === null ? "—" : `${integer.format(latency)} ms`, "completed calls"], ["Errors", integer.format(firstValue(totals, ["errors", "failed_calls"], 0)), "failures and quota events", numeric(firstValue(totals, ["errors", "failed_calls"], 0)) ? "negative" : ""]]);
  const budgetTarget = $("usage-budgets");
  if (!budgets.length) empty(budgetTarget, "No model budgets reported.");
  else budgetTarget.replaceChildren(...budgets.map((budget) => { const used = numeric(firstValue(budget, ["used", "calls", "used_calls"])); const limit = optionalNumber(firstValue(budget, ["limit", "daily_limit", "max_calls"])); const item = node("div", "budget-item"); const head = node("div", "budget-head"); head.append(node("strong", "", budget.model || budget.provider || "model"), node("span", "", limit === null ? `${used} calls · provider quota` : `${used} / ${limit} calls`)); const track = node("div", "budget-track"); const fill = node("i"); fill.style.width = limit === null ? "0%" : `${Math.min(100, used / Math.max(1, limit) * 100)}%`; track.append(fill); item.append(head, track); return item; }));
  const callsByDay = daily.length ? daily.map((item) => numeric(firstValue(item, ["calls", "total_calls", "runs"]))) : [runs.length]; drawBarChart($("usage-chart"), callsByDay, { color: "#5367e8" });
  $("usage-runs").replaceChildren(simpleRunsTable(runs));
}
function simpleRunsTable(runs) {
  const wrap = node("div"); if (!runs.length) { empty(wrap, "No model runs recorded for this epoch."); return wrap; }
  const table = node("table", "data-table"); const head = node("tr"); ["Time", "Model", "Purpose", "Tokens", "Latency", "Status", "Run hash"].forEach((label) => head.append(node("th", "", label))); const thead = node("thead"); thead.append(head); const body = node("tbody");
  runs.slice(0, 100).forEach((run) => {
    const row = node("tr");
    const tokens = numeric(run.input_tokens) + numeric(run.output_tokens);
    [
      shortTime(firstValue(run, ["created_at", "ts", "started_at"])),
      firstValue(run, ["model", "model_id"], "—"),
      firstValue(run, ["purpose", "kind", "event_type"], "decision"),
      tokens ? integer.format(tokens) : "—",
      run.latency_ms ? `${integer.format(run.latency_ms)} ms` : "—",
      firstValue(run, ["status", "state"], "completed"),
      String(firstValue(run, ["input_hash", "run_id", "id"], "—")).slice(0, 12),
    ].forEach((value) => row.append(node("td", "", value)));
    body.append(row);
  });
  table.append(thead, body); wrap.append(table); return wrap;
}

function renderSnapshot(snapshot) {
  appState.snapshot = snapshot;
  renderOverview(snapshot); renderStrategies(snapshot.strategies || []); renderPnl(snapshot.pnl); renderHealth(snapshot.health); renderOrders(snapshot.orders); renderApprovals(snapshot.approvals); renderActivity(snapshot.activity, snapshot.audit);
  $("updated-at").textContent = `Updated ${shortTime(snapshot.generated_at, true)}`;
  const count = numeric(snapshot.overview?.strategy_count); $("strategy-summary").textContent = `${count} × ${moneyText(count ? numeric(snapshot.overview.shadow_capital_usd) / count : 0)} shadow capital`;
  $("schema-version").textContent = `Snapshot schema v${snapshot.schema_version}`;
}

function routeParts() {
  const raw = window.location.hash.replace(/^#\/?/, "") || "overview"; return raw.split("/").filter(Boolean).map(decodeURIComponent);
}
async function activateRoute() {
  const parts = routeParts(); const strategyId = parts[0] === "strategies" && parts[1] ? parts[1] : null; const route = strategyId ? "strategy-detail" : ["overview", "strategies", "trades", "reviews", "risk", "usage"].includes(parts[0]) ? parts[0] : "overview"; const token = ++appState.routeToken;
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.dataset.view === route));
  document.querySelectorAll(".nav-item").forEach((link) => link.classList.toggle("active", link.dataset.route === (strategyId ? "strategies" : route)));
  const titles = { overview: "Overview", strategies: "Strategies", "strategy-detail": "Strategy detail", trades: "Trades", reviews: "AI Reviews", risk: "Risk & System", usage: "AI Usage" }; $("page-title").textContent = titles[route];
  closeNavigation();
  if (route === "strategy-detail") await renderStrategyDetail(strategyId, token);
  if (route === "trades") { if (!appState.trades.length) await loadAllTrades(); if (token === appState.routeToken) renderTrades(); }
  if (route === "reviews") { await Promise.all([appState.trades.length ? Promise.resolve() : loadAllTrades(), loadReviews()]); if (token === appState.routeToken) renderReviews(); }
  if (route === "usage") { await loadUsage(); if (token === appState.routeToken) renderUsage(); }
  if (route === "overview") renderPnl(appState.snapshot?.pnl);
}
function closeNavigation() { document.body.classList.remove("nav-open"); $("menu-button").setAttribute("aria-expanded", "false"); }

async function initialLoad() { const snapshot = await getJson("/api/snapshot"); renderSnapshot(snapshot); await activateRoute(); }
function setConnection(state, message) { $("stream-dot").className = `status-dot ${state}`; $("stream-status").textContent = message; }
function connectStream() {
  const stream = new EventSource("/api/events");
  stream.addEventListener("open", () => setConnection("", "Live stream"));
  stream.addEventListener("snapshot", (event) => { try { renderSnapshot(JSON.parse(event.data)); setConnection("", "Live stream"); } catch (error) { console.error("Invalid dashboard snapshot", error); setConnection("error", "Invalid snapshot"); } });
  stream.addEventListener("error", () => setConnection("pending", "Reconnecting"));
}

document.querySelectorAll("[data-strategy-filter]").forEach((button) => button.addEventListener("click", async () => {
  appState.strategyFilter = button.dataset.strategyFilter;
  document.querySelectorAll("[data-strategy-filter]").forEach((item) => item.classList.toggle("active", item === button));
  if (appState.strategyFilter === "positions" && !appState.trades.length) await loadAllTrades();
  renderStrategies(appState.snapshot?.strategies || []);
}));
$("trade-search").addEventListener("input", renderTrades); $("trade-result-filter").addEventListener("change", renderTrades);
$("menu-button").addEventListener("click", () => { const open = document.body.classList.toggle("nav-open"); $("menu-button").setAttribute("aria-expanded", String(open)); });
$("sidebar-scrim").addEventListener("click", closeNavigation);
window.addEventListener("hashchange", () => activateRoute().catch((error) => console.error("Route failed", error)));
window.addEventListener("resize", () => {
  if (window.innerWidth > 760) closeNavigation();
  const route = routeParts()[0]; if (route === "overview") renderPnl(appState.snapshot?.pnl); if (route === "trades" && appState.trades.length) renderTrades(); if (route === "usage" && appState.usage) renderUsage();
});
$("kill-button").addEventListener("click", () => { if (!window.confirm("Lock all new orders now?")) return; postJson("/api/control/kill", { reason: "dashboard manual kill" }).then(initialLoad).catch((error) => window.alert(error.message)); });
$("resume-button").addEventListener("click", () => { const confirmation = window.prompt("Type RESUME_DEMO. Resume fails if audit or reconciliation is unsafe."); if (confirmation !== "RESUME_DEMO") return; postJson("/api/control/resume", { confirmation }).then(initialLoad).catch((error) => window.alert(error.message)); });

initialLoad().then(connectStream).catch((error) => { console.error("Dashboard unavailable", error); setConnection("error", "Dashboard unavailable"); });
