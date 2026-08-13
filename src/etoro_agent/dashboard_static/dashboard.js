"use strict";

const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

function numberValue(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function moneyText(value) { return money.format(numberValue(value)); }
function countText(value) { return integer.format(numberValue(value)); }
function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}
function empty(target, text) { target.replaceChildren(node("div", "empty", text)); }
function tone(value) { return numberValue(value) > 0 ? "positive" : numberValue(value) < 0 ? "negative" : ""; }
function boolText(value) { return value ? "YES" : "NO"; }
function ageText(seconds) {
  if (seconds === null || seconds === undefined) return "unavailable";
  const value = Math.max(0, numberValue(seconds));
  if (value >= 86400) return `${(value / 86400).toFixed(1)} days`;
  if (value >= 3600) return `${(value / 3600).toFixed(1)} hours`;
  return `${Math.round(value / 60)} minutes`;
}
function timestamp(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
}

function metric(label, value, foot, valueTone = "") {
  const card = node("article", "metric");
  card.append(node("span", "label", label), node("strong", `value ${valueTone}`, value), node("span", "foot", foot));
  return card;
}

function keyValues(target, rows) {
  target.replaceChildren(...rows.map(([label, value, valueTone = ""]) => {
    const row = node("div", "key-value");
    row.append(node("span", "", label), node("strong", valueTone, String(value)));
    return row;
  }));
}

function countRows(target, values, emptyText) {
  const entries = Object.entries(values || {}).sort(([left], [right]) => left.localeCompare(right));
  if (!entries.length) return empty(target, emptyText);
  const header = node("div", "table-row header");
  header.append(node("span", "", "State"), node("span", "", "Count"));
  const rows = entries.map(([state, count]) => {
    const row = node("div", "table-row");
    row.append(node("span", "", state.replaceAll("_", " ")), node("strong", "", countText(count)));
    return row;
  });
  target.replaceChildren(header, ...rows);
}

function renderHealth(health) {
  const status = String(health.status || "error").toLowerCase();
  const locked = status === "locked";
  const gateKnown = typeof health.execution_gate_present === "boolean";
  const realKnown = typeof health.real_money === "boolean";
  $("health-summary").textContent = status.toUpperCase();
  $("health-summary").className = `health-pill ${locked || status === "ok" ? "positive" : "negative"}`;
  const checks = [
    ["Execution gate", gateKnown ? (health.execution_gate_present ? "PRESENT" : "ABSENT") : "UNKNOWN", gateKnown && !health.execution_gate_present ? "positive" : "negative"],
    ["REAL money", realKnown ? boolText(health.real_money) : "UNKNOWN", realKnown && !health.real_money ? "positive" : "negative"],
    ["Stale heartbeats", countText((health.stale_heartbeats || []).length), (health.stale_heartbeats || []).length ? "negative" : "positive"],
    ["Failures", countText((health.failures || []).length), (health.failures || []).length ? "negative" : "positive"],
  ];
  $("health-overview").replaceChildren(...checks.map(([label, value, valueTone]) => {
    const item = node("div", "health-item");
    const line = node("div", "health-line");
    line.append(node("span", "health-name", label), node("span", `health-status ${valueTone}`, value));
    item.append(line);
    return item;
  }));
}

function renderPositions(positions) {
  $("position-count").textContent = `${positions.length} records`;
  const target = $("positions-table");
  if (!positions.length) return empty(target, "No DEMO positions. The runtime is flat and locked.");
  const table = node("table", "data-table");
  const head = node("tr");
  ["Symbol", "Side", "Quantity", "Entry", "Mark", "P&L", "Status", "Strategy"].forEach((label) => head.append(node("th", "", label)));
  const thead = node("thead"); thead.append(head);
  const tbody = node("tbody");
  positions.forEach((position) => {
    const row = node("tr");
    const values = [
      position.symbol || "—",
      String(position.side || "—").toUpperCase(),
      String(position.quantity ?? "—"),
      String(position.entry_price ?? "—"),
      String(position.last_mark ?? "—"),
      moneyText(numberValue(position.realized_pnl) + numberValue(position.unrealized_pnl)),
      position.status || "—",
      position.strategy_id || "—",
    ];
    values.forEach((value) => row.append(node("td", "", value)));
    tbody.append(row);
  });
  table.append(thead, tbody); target.replaceChildren(table);
}

function renderCompatibility(items) {
  const target = $("compatibility-table");
  if (!items.length) return empty(target, "No instrument compatibility records.");
  const columns = [
    ["strategy_id", "Strategy"],
    ["symbol", "Symbol"],
    ["status", "Status"],
    ["feasible_amount_min_usd", "Min USD"],
    ["feasible_amount_max_usd", "Max USD"],
    ["feasible_stop_min", "Min stop"],
    ["feasible_stop_max", "Max stop"],
    ["reasons", "Reasons"],
  ];
  const table = node("table", "data-table");
  const head = node("tr"); columns.forEach(([, label]) => head.append(node("th", "", label)));
  const thead = node("thead"); thead.append(head);
  const tbody = node("tbody");
  items.forEach((item) => {
    const row = node("tr");
    columns.forEach(([key]) => {
      const value = Array.isArray(item[key]) ? item[key].join(", ") : item[key];
      row.append(node("td", "", value === undefined || value === null || value === "" ? "—" : String(value)));
    });
    tbody.append(row);
  });
  table.append(thead, tbody); target.replaceChildren(table);
}

function render(snapshot, health) {
  const portfolio = snapshot.portfolio || {};
  const decisions = snapshot.ai_decisions || {};
  const orderStates = snapshot.orders || {};
  const openPositions = numberValue(portfolio.open_positions);
  const totalDecisions = Object.values(decisions).reduce((sum, value) => sum + numberValue(value), 0);
  $("overview-metrics").replaceChildren(
    metric("Trading state", snapshot.trading_state || "UNKNOWN", "PostgreSQL authority", snapshot.trading_state === "LOCKED" ? "warning" : ""),
    metric("Open positions", countText(openPositions), "canonical projection", openPositions ? "warning" : "positive"),
    metric("Realized P&L", moneyText(portfolio.realized_pnl_usd), "DEMO only", tone(portfolio.realized_pnl_usd)),
    metric("Unrealized P&L", moneyText(portfolio.unrealized_pnl_usd), "DEMO only", tone(portfolio.unrealized_pnl_usd)),
    metric("Fills", countText(portfolio.fills), "audit-backed records"),
    metric("AI decisions", countText(totalDecisions), "untrusted until deterministic apply"),
  );
  keyValues($("portfolio"), [
    ["Initial cash", moneyText(portfolio.initial_cash_usd)],
    ["Realized P&L", moneyText(portfolio.realized_pnl_usd), tone(portfolio.realized_pnl_usd)],
    ["Unrealized P&L", moneyText(portfolio.unrealized_pnl_usd), tone(portfolio.unrealized_pnl_usd)],
    ["Fees", moneyText(portfolio.fees_usd)],
    ["Financing", moneyText(portfolio.financing_usd)],
    ["Closed positions", countText(portfolio.closed_positions)],
  ]);
  renderHealth(health);
  countRows($("orders"), orderStates, "No DEMO order records.");
  countRows($("ai-decisions"), decisions, "No AI decisions recorded in this epoch.");
  renderPositions(snapshot.positions || []);
  keyValues($("safety-boundary"), [
    ["Account mode", snapshot.account_mode || "DEMO"],
    ["REAL money", boolText(snapshot.real_money), snapshot.real_money ? "negative" : "positive"],
    ["Execution enabled", typeof health.execution_enabled === "boolean" ? boolText(health.execution_enabled) : "UNKNOWN", health.execution_enabled === false ? "positive" : "negative"],
    ["Execution gate", typeof health.execution_gate_present === "boolean" ? (health.execution_gate_present ? "PRESENT" : "ABSENT") : "UNKNOWN", health.execution_gate_present === false ? "positive" : "negative"],
    ["Trading state", snapshot.trading_state || "UNKNOWN"],
    ["Research epoch", snapshot.research_epoch || "not promoted"],
  ]);
  keyValues($("audit-backup"), [
    ["Event chain", snapshot.audit?.chain_valid ? "VALID" : "INVALID", snapshot.audit?.chain_valid ? "positive" : "negative"],
    ["Events", countText(snapshot.audit?.events)],
    ["Last anchor", health.audit?.last_anchor_at ? timestamp(health.audit.last_anchor_at) : "unavailable"],
    ["Backup age", ageText(health.backup?.age_seconds)],
    ["Off-host age", ageText(health.backup?.offhost_age_seconds)],
    ["Restore drill age", ageText(health.backup?.restore_drill_age_seconds)],
  ]);
  renderCompatibility(snapshot.compatibility || []);
  $("mode-pill").textContent = snapshot.account_mode || "DEMO";
  $("safety-state").textContent = `${snapshot.trading_state || "UNKNOWN"} · READ ONLY`;
  $("halt-title").textContent = snapshot.trading_state === "LOCKED" ? "LOCKED · FAIL-CLOSED" : `${snapshot.trading_state} · DEMO`;
  $("updated-at").textContent = `Updated ${timestamp(snapshot.generated_at)}`;
  $("schema-version").textContent = `Snapshot schema v${snapshot.schema_version}`;
}

function setConnection(state, message) {
  $("stream-dot").className = `status-dot ${state}`;
  $("stream-status").textContent = message;
}

async function refresh() {
  try {
    const [snapshotResponse, healthResponse] = await Promise.all([
      fetch("/api/v2/snapshot", { cache: "no-store", headers: { Accept: "application/json" } }),
      fetch("/healthz", { cache: "no-store", headers: { Accept: "application/json" } }),
    ]);
    if (!snapshotResponse.ok) throw new Error(`snapshot returned ${snapshotResponse.status}`);
    if (!healthResponse.ok && healthResponse.status !== 503) throw new Error(`health returned ${healthResponse.status}`);
    render(await snapshotResponse.json(), await healthResponse.json());
    setConnection("", "Live · read only");
  } catch (error) {
    console.error("Dashboard refresh failed", error);
    setConnection("error", "Dashboard unavailable");
  }
}

function activateRoute() {
  const route = ["overview", "positions", "system"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.dataset.view === route));
  document.querySelectorAll(".nav-item").forEach((link) => link.classList.toggle("active", link.dataset.route === route));
  $("page-title").textContent = { overview: "Overview", positions: "Positions", system: "System" }[route];
  document.body.classList.remove("nav-open");
  $("menu-button").setAttribute("aria-expanded", "false");
}

$("menu-button").addEventListener("click", () => {
  const open = document.body.classList.toggle("nav-open");
  $("menu-button").setAttribute("aria-expanded", String(open));
});
$("sidebar-scrim").addEventListener("click", activateRoute);
window.addEventListener("hashchange", activateRoute);
activateRoute();
refresh();
setInterval(refresh, 15000);
