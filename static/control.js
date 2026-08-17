const API = "/api/v1";
const state = { user: null, csrf: "", section: "overview", cache: new Map(), settlementFilter: "all", positionFilters: {}, investigation: { page: 1, period: "day", accountId: "", search: "" }, indexDesk: { fundId: "", draftFundId: "", draft: [], dirty: false } };
const sections = [
  ["overview", "Overview"], ["engine", "FCX Engine"], ["positions", "Live Positions"],
  ["operations", "Companies & Securities"], ["indexes", "Index Settings"], ["leverage", "Leverage Settings"], ["promotions", "Account Promotions"],
  ["accounts", "Ravenhood Accounts"], ["banking", "FCX Banking"], ["investigations", "FEC Investigations"], ["audit", "Audit / Activity"],
  ["health", "System Health"], ["onboarding", "FEC Onboarding"], ["communities", "Community Registry"], ["connections", "API Connections"],
];
const sectionMeta = {
  overview: ["Command", "⌂"], engine: ["Command", "◉"], positions: ["Command", "↗"],
  operations: ["Market Operations", "◆"], indexes: ["Market Operations", "▦"], leverage: ["Market Operations", "△"], promotions: ["Market Operations", "%"],
  accounts: ["Regulatory", "◎"], banking: ["Regulatory", "$"], investigations: ["Regulatory", "⌕"], audit: ["Regulatory", "≡"],
  health: ["Administration", "✦"], onboarding: ["Administration", "+"], communities: ["Administration", "◇"], connections: ["Administration", "⇄"],
};

function currentRoles() { return new Set((state.user?.roles || []).map(role => String(role).toLowerCase())); }
function isDeveloper() { const roles = currentRoles(); return roles.has("super_admin") || roles.has("developer") || roles.has("commissioner"); }
function visibleSections() {
  if (isDeveloper()) return sections;
  const roles = currentRoles();
  if (roles.has("fec_admin")) return sections.filter(([key]) => !["communities", "connections"].includes(key));
  if (roles.has("fec_investigator")) return sections.filter(([key]) => ["overview", "positions", "accounts", "banking", "investigations", "health", "audit"].includes(key));
  if (roles.has("fcx_admin")) return sections.filter(([key]) => ["overview", "positions", "accounts", "banking", "audit", "health", "communities", "connections"].includes(key));
  return sections.filter(([key]) => ["overview", "health"].includes(key));
}

function accountRoles(row) {
  if (Array.isArray(row?.roles_json)) return row.roles_json.map(String);
  try { const parsed=JSON.parse(String(row?.roles_json||"[]")); return Array.isArray(parsed)?parsed.map(String):[]; } catch { return []; }
}

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = value => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(value || 0));
const stamp = value => value ? new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "Never";
const yes = value => value === true || value === 1 || value === "1" || value === "true";
const safeJson = value => typeof value === "string" ? value : JSON.stringify(value ?? {});

function notice(message, bad = false) {
  const box = $("#notice");
  box.textContent = message;
  box.classList.toggle("bad", bad);
  box.hidden = !message;
  if (message) setTimeout(() => { if (box.textContent === message) box.hidden = true; }, 7000);
}

async function request(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  if (options.method && options.method !== "GET" && state.csrf) headers["X-FCX-CSRF"] = state.csrf;
  const response = await fetch(API + path, { credentials: "same-origin", ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function renderNav() {
  const available = visibleSections();
  if (!available.some(([key]) => key === state.section)) state.section = available[0]?.[0] || "health";
  let group = "";
  $("#nav").innerHTML = available.map(([key, label]) => {
    const [nextGroup, icon] = sectionMeta[key] || ["Administration", "•"];
    const heading = nextGroup !== group ? `<p class="nav-group">${esc(nextGroup)}</p>` : "";
    group = nextGroup;
    return `${heading}<button class="nav-button ${key === state.section ? "active" : ""}" data-section="${key}" data-icon="${esc(icon)}">${esc(label)}</button>`;
  }).join("");
  $("#nav").querySelectorAll("button").forEach(button => button.addEventListener("click", () => { document.querySelector("#workspace")?.classList.remove("nav-open"); $("#mobile-scrim").hidden = true; openSection(button.dataset.section); }));
}

async function openSection(section, force = false) {
  state.section = section;
  renderNav();
  const label = sections.find(item => item[0] === section)?.[1] || "Control";
  $("#section-title").textContent = label;
  $("#section-kicker").textContent = ["investigations", "promotions"].includes(section) ? "FEDERAL EXCHANGE COMMISSION" : "RAVENHOOD EXCHANGE OPERATIONS";
  const content = $("#content");
  content.innerHTML = `<div class="empty">Synchronizing ${esc(label)}…</div>`;
  try {
    if (force) state.cache.clear();
    const renderer = renderers[section] || renderOverview;
    content.innerHTML = await renderer();
    bindActions();
  } catch (error) {
    content.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    if (/login|required|session/i.test(error.message)) showLogin();
  }
}

async function cached(key, path) {
  if (!state.cache.has(key)) state.cache.set(key, request(path));
  return state.cache.get(key);
}

function metrics(items) {
  return `<div class="metric-strip">${items.map(item => `<div class="metric"><small>${esc(item[0])}</small><strong class="${item[3] || ""}">${item[4] ? String(item[1]) : esc(item[1])}</strong><em>${esc(item[2] || "")}</em></div>`).join("")}</div>`;
}

function sectionHead(kicker, title, description = "") {
  return `<div class="section-head"><div><p class="eyebrow">${esc(kicker)}</p><h3>${esc(title)}</h3><p>${esc(description)}</p></div></div>`;
}

function bars(values) {
  const nums = values.map(Number); const max = Math.max(1, ...nums);
  return `<div class="mini-chart">${nums.map((value, index) => `<i style="height:${Math.max(6, value / max * 100)}%;opacity:${.4 + index / Math.max(1, nums.length) * .6}"></i>`).join("")}</div>`;
}

const number = value => Number(value || 0).toLocaleString("en-US", { maximumFractionDigits: 4 });
const percent = value => `${Number(value || 0).toFixed(2)}%`;
const fieldValue = value => esc(value === null || value === undefined ? "" : value);
const rowsOrEmpty = (rows, render, message = "No records in this view") => rows?.length ? rows.map(render).join("") : `<tr><td colspan="20"><div class="empty compact">${esc(message)}</div></td></tr>`;
const selectOptions = (rows, value = "id", label = row => `${row.ticker} · ${row.name}`) => (rows || []).map(row => `<option value="${esc(row[value])}">${esc(label(row))}</option>`).join("");
function dataTable(headers, rows, render, empty = "No records in this view") {
  return `<div class="panel wide table-wrap"><table><thead><tr>${headers.map(header => `<th>${esc(header)}</th>`).join("")}</tr></thead><tbody>${rowsOrEmpty(rows, render, empty)}</tbody></table></div>`;
}
function pnl(value) { const amount = Number(value || 0); return `<span class="amount ${amount >= 0 ? "up" : "down"}">${money(amount)}</span>`; }
function linkedIdentity(row) {
  return `<strong>${esc(row.display_name || row.ravenhood_account_id || row.account_id || "Ravenhood account")}</strong><small>${esc(row.ravenhood_account_id || row.account_id || "")}${row.bohemia_identity_id ? ` · BI ${esc(row.bohemia_identity_id)}` : " · Unlinked"}${row.communities ? ` · ${esc(row.communities)}` : ""}</small>`;
}

async function renderOverview() {
  const data = await cached("overview", "/admin/overview");
  const totals = data.totals || {};
  const connected = (data.communities || []).filter(item => item.connection_enabled && !item.suspended).length;
  return `<section class="hero"><span class="status-chip">Central exchange online</span><h3>One FCX.<br>Independent communities.</h3><p>Market operations, Ravenhood identities, community permissions, regulatory cases, and settlement references live here—not inside either CAD.</p></section>
    ${metrics([
      ["Active accounts", totals.active_accounts || 0, "Shared Ravenhood identities"],
      ["Connected communities", connected, `${(data.communities || []).length} registered`],
      ["Market capitalization", money(totals.market_cap), "FCX operating securities"],
      ["Securities", totals.securities || 0, "Active on the exchange"],
      ["Settlement value", money(totals.settled_value), `${totals.settlements || 0} references`],
      ["Open FEC cases", totals.open_cases || 0, "Regulatory workload", totals.open_cases ? "down" : "up"],
    ])}
    ${sectionHead("OPERATING PICTURE", "Community and control signal", "Every community remains separately permissioned and separately settled.")}
    <div class="grid">
      <article class="panel"><h4>Connected communities</h4><p>Connection health and market access are independent for every CAD.</p>${communitySummary(data.communities || [])}</article>
      <article class="panel"><h4>Control activity</h4><p>Latest privileged operations recorded by the central audit ledger.</p>${auditSummary(data.recent_actions || [])}</article>
    </div>`;
}

function communitySummary(rows) {
  if (!rows.length) return `<div class="empty">No communities registered</div>`;
  return rows.map(row => `<div class="community-card"><div class="community-title"><div><h4>${esc(row.community_name)}</h4><small>${esc(row.community_id)}</small></div><span class="status-chip ${row.suspended ? "danger" : row.connection_enabled ? "" : "warn"}">${row.suspended ? "Suspended" : row.connection_enabled ? "Connected" : "Disabled"}</span></div><div class="bar"><span style="width:${row.last_seen_at ? 100 : 12}%"></span></div><p class="muted">Last seen ${esc(stamp(row.last_seen_at))}</p></div>`).join("");
}

function auditSummary(rows) {
  if (!rows.length) return `<div class="empty">No privileged actions recorded</div>`;
  return rows.slice(0, 8).map(row => `<div class="audit-entry"><time>${esc(stamp(row.created_at))}</time><strong>${esc(row.action)}</strong><span>${esc(row.target_type)} ${esc(row.target_id)}</span></div>`).join("");
}

async function marketData() { return cached("market", "/admin/market"); }

async function renderEngine() {
  const response = await cached("engine", "/admin/engine/snapshot");
  const data = response.engine || response;
  const settings = data.settings || {};
  const counts = data.counts || {};
  const capital = data.capital || {};
  const stateInfo = data.state || {};
  const deployment = data.deployment || {};
  const readiness = deployment.readiness || {};
  const latestDeployment = deployment.latest || {};
  const marketOps = data.market_operations || {};
  const cycles = data.cycles || [];
  const securities = data.securities || [];
  const personalities = data.personalities || [];
  const sectors = data.sectors || [];
  const corporateActions = data.corporate_actions || [];
  const engineAudit = data.audit || [];
  const watchlist = data.watchlist || [];
  const engineHalts = data.halts || [];
  const settingGroups = [
    ["Engine & population", [["enabled","Enabled","bool"],["speed","Speed","text"],["random_seed","Random seed","number"],["population","Simulated investors","number"],["total_capital","Deployed capital","number"],["price_floor","Price floor","number"],["execution_budget_per_tick","Executions per tick","number"]]],
    ["Movement limits", [["minute_cap_percent","1 minute cap %","number"],["five_minute_cap_percent","5 minute cap %","number"],["thirty_minute_cap_percent","30 minute cap %","number"],["human_priority_percent","Resident priority %","number"],["max_order_percent","Maximum order %","number"],["panic_participation_percent","Panic participation %","number"]]],
    ["Liquidity & events", [["market_maker_spread_percent","Maker spread %","number"],["market_maker_depth_multiplier","Depth multiplier","number"],["events_enabled","Events enabled","bool"],["event_probability_percent","Event chance %","number"],["sentiment_sensitivity","Sentiment sensitivity","number"]]],
    ["Circuit breakers", [["halts_enabled","Halts enabled","bool"],["halt_risk_threshold","Halt risk threshold","number"],["circuit_breaker_10m_percent","10m breaker %","number"],["circuit_breaker_30m_percent","30m breaker %","number"],["circuit_breaker_10m_duration_minutes","10m halt duration","number"],["circuit_breaker_30m_duration_minutes","30m halt duration","number"]]],
    ["Surveillance", [["abnormal_volume_float_percent","Abnormal float %","number"],["flow_concentration_percent","Flow concentration %","number"],["rapid_round_trip_percent","Rapid round-trip %","number"],["wash_round_trip_percent","Wash pattern %","number"],["coordinated_flow_imbalance_percent","Coordinated imbalance %","number"],["coordinated_flow_min_participants","Minimum participants","number"]]],
    ["Corporate risk", [["bankruptcy_enabled","Bankruptcy engine","bool"],["delisting_enabled","Delisting engine","bool"],["short_selling_enabled","Short selling","bool"],["bankruptcy_watch_threshold","Watch threshold","number"],["bankruptcy_ch11_threshold","Chapter 11 threshold","number"],["bankruptcy_ch7_threshold","Chapter 7 threshold","number"],["bankruptcy_ch7_loss_cycles","Loss cycles","number"],["delisting_price_floor","Delisting floor","number"]]],
    ["IPO uncertainty", [["ipo_uncertainty_enabled","IPO uncertainty","bool"],["ipo_uncertainty_days","Uncertainty days","number"],["ipo_uncertainty_max_multiplier","Maximum multiplier","number"]]],
  ];
  const settingHtml = settingGroups.map(([title, fields]) => `<fieldset class="control-group"><legend>${esc(title)}</legend><div class="form-grid dense">${fields.map(([key,label,type]) => type === "bool" ? `<label>${esc(label)}<select name="${key}"><option value="true" ${yes(settings[key]) ? "selected" : ""}>Enabled</option><option value="false" ${!yes(settings[key]) ? "selected" : ""}>Disabled</option></select></label>` : `<label>${esc(label)}<input name="${key}" type="${type}" step="any" value="${fieldValue(settings[key])}"></label>`).join("")}</div></fieldset>`).join("");
  const readinessRows = [
    ["Operating listings", readiness.operating_listings, readiness.target_listings],
    ["FCXS constituents", readiness.fcxs_constituents, 8],
    ["FCXV constituents", readiness.fcxv_constituents, 6],
    ["Engine investors", readiness.investors, readiness.target_investors],
  ];
  return `<section class="hero engine-hero"><span class="status-chip ${yes(settings.kill_switch) ? "danger" : ""}">${yes(settings.kill_switch) ? "Kill switch active" : yes(settings.enabled) ? "Autonomous engine online" : "Engine paused"}</span><h3>The complete FCX<br>operating engine.</h3><p>Population, capital, cycles, liquidity, surveillance, corporate actions, event pressure, circuit breakers, and every autonomous market rule are owned here.</p></section>
    ${metrics([["Engine state", yes(settings.enabled) ? "Running" : "Paused", String(stateInfo.status || stateInfo.state || "Authoritative")],["Population", number(counts.investors || settings.population), "Persistent simulated investors"],["Deployed capital", money(capital.deployed_capital || capital.total_capital || settings.total_capital), "FCX engine liquidity"],["Operating listings", number(counts.operating_listings), `${number(securities.length)} modeled securities`],["Open risk flags", number(counts.open_flags), `${number(counts.active_halts)} engine/FEC halts`],["Launch readiness", readiness.ready ? "Ready" : "Attention", `${number(readiness.operating_listings)} / ${number(readiness.target_listings)} listings`, readiness.ready ? "up" : "down"]])}
    ${sectionHead("LIVE MARKET OPERATIONS", "Execution heartbeat", "Resident flow and autonomous engine activity are measured independently without exposing private account identities.")}
    ${metrics([["Resident orders / min", number(marketOps.resident_trades_last_minute), money(marketOps.resident_volume_last_minute)],["Largest resident order", money(marketOps.largest_resident_trade_last_minute), "Last rolling minute"],["Engine fills / min", number(marketOps.engine_executions_last_minute), money(marketOps.engine_volume_last_minute)],["FCX market cap", money(marketOps.total_market_cap), "Active listed securities"],["Long positions", number(counts.positions), "Engine investor network"],["Short positions", number(counts.short_positions), "Engine investor network"]])}
    ${sectionHead("ENGINE COMMAND", "Autonomous control room", "Manual cycles and emergency controls are audited immediately.")}
    <div class="command-deck">
      <button class="engine-command primary" data-command="cycle" data-cycle="minute">Run 1-minute cycle</button><button class="engine-command" data-command="cycle" data-cycle="five_minute">Run 5-minute cycle</button><button class="engine-command" data-command="cycle" data-cycle="thirty_minute">Run 30-minute cycle</button><button class="engine-command" data-command="pause">Pause engine</button><button class="engine-command" data-command="resume">Resume engine</button><button class="engine-command danger-action" data-command="kill" data-active="${yes(settings.kill_switch) ? "false" : "true"}">${yes(settings.kill_switch) ? "Release kill switch" : "Engage kill switch"}</button>
    </div>
    <div class="grid operator-grid">
      <article class="panel third"><h4>Population seed</h4><p>Add any missing engine investors, or perform an explicitly authorized replacement.</p><form id="engine-seed-form" class="form-grid"><label>Mode<select name="replace"><option value="false">Reconcile missing investors</option><option value="true">Replace engine population</option></select></label><label>Replacement authorization<input name="confirmation" placeholder="RESEED FCX only when replacing"></label><button class="primary full">Seed population</button></form></article>
      <article class="panel third"><h4>Sandbox projection</h4><p>Run a deterministic test without replacing live resident ledgers.</p><form id="engine-sandbox-form" class="form-grid"><label>Days<input name="days" type="number" min="1" max="365" value="30"></label><label>Seed<input name="seed" type="number" min="1" value="${fieldValue(settings.random_seed || 1)}"></label><button class="primary full">Run sandbox</button></form></article>
      <article class="panel third"><h4>Ticker intervention</h4><p>Pause or resume one engine-managed security.</p><form id="ticker-control-form" class="form-grid"><label class="full">Security<select name="ticker">${selectOptions(securities,"ticker")}</select></label><button name="action" value="pause">Pause ticker</button><button name="action" value="resume">Resume ticker</button></form></article>
      <article class="panel"><h4>Stock split</h4><p>Apply an audited numerator-to-denominator split.</p><form id="split-form" class="form-grid"><label>Ticker<select name="ticker">${selectOptions(securities,"ticker")}</select></label><label>Numerator<input name="numerator" type="number" min="0.01" max="1000" step="0.01" value="2"></label><label>Denominator<input name="denominator" type="number" min="0.01" max="1000" step="0.01" value="1"></label><label>Authorization<input name="confirmation" value="APPLY SPLIT" required></label><label class="full">Rationale<textarea name="rationale" required>Authorized FCX corporate action.</textarea></label><button class="primary full">Apply split</button></form></article>
      <article class="panel"><h4>Dividend declaration</h4><p>Distribute a per-share payment through the engine ledger.</p><form id="dividend-form" class="form-grid"><label>Ticker<select name="ticker">${selectOptions(securities,"ticker")}</select></label><label>Per share<input name="amount_per_share" type="number" min="0.0001" step="0.0001" value="0.25"></label><label>Authorization<input name="confirmation" value="DECLARE DIVIDEND" required></label><label class="full">Rationale<textarea name="rationale" required>Authorized FCX dividend declaration.</textarea></label><button class="primary full">Declare dividend</button></form></article>
    </div>
    ${sectionHead("DEPLOYMENT CONTROL", "Launch readiness and persistent state", "Pre-flight totals are read directly from FCX. Deployment history remains visible after every restart.")}
    <div class="grid engine-readiness">
      <article class="panel"><h4>${readiness.ready ? "Exchange ready" : "Pre-flight attention required"}</h4><p>${readiness.ready ? "All minimum launch thresholds are satisfied." : "One or more operating thresholds are below the FCX launch target."}</p>${readinessRows.map(([label,current,target]) => { const ratio = Math.min(100, Number(current || 0) / Math.max(1, Number(target || 1)) * 100); return `<div class="readiness-row"><span>${esc(label)}</span><strong>${number(current)} / ${number(target)}</strong><div class="bar"><span style="width:${ratio}%"></span></div></div>`; }).join("")}<div class="readiness-row"><span>Fund accounts / units</span><strong>${number(readiness.fund_accounts)} / ${number(readiness.fund_units)}</strong></div></article>
      <article class="panel"><h4>Latest deployment record</h4><p>Persistent FCX deployment ledger.</p>${latestDeployment.id ? `<dl class="state-list"><div><dt>Status</dt><dd>${esc(latestDeployment.status)}</dd></div><div><dt>Listings</dt><dd>${number(latestDeployment.listings_before)} -> ${number(latestDeployment.listings_after)}</dd></div><div><dt>Created</dt><dd>${esc(stamp(latestDeployment.created_at))}</dd></div><div><dt>Completed</dt><dd>${esc(stamp(latestDeployment.completed_at))}</dd></div><div><dt>Operator</dt><dd>${esc(latestDeployment.deployed_by || "System")}</dd></div></dl><pre class="json-readout">${esc(JSON.stringify(latestDeployment.details || {}, null, 2))}</pre>` : `<div class="empty compact">No completed FCX deployment recorded</div>`}</article>
    </div>
    ${sectionHead("ENGINE CONFIGURATION", "Every autonomous rule", "Saving updates the standalone FCX engine; no CAD setting is used.")}
    <form id="engine-settings-form" class="engine-settings">${settingHtml}
      <fieldset class="control-group full-span"><legend>Automation schedule and participation</legend><div class="form-grid">
        <label class="full">Cycle intervals in seconds (JSON)<textarea name="intervals_json" spellcheck="false">${esc(JSON.stringify(settings.intervals || {}, null, 2))}</textarea><small>Minute, five-minute, fifteen-minute, thirty-minute, hourly, six-hour, and daily workers.</small></label>
        <label class="full">Investor personality distribution (JSON)<textarea name="distribution_json" spellcheck="false">${esc(JSON.stringify(settings.distribution || {}, null, 2))}</textarea><small>Weights are normalized to 100 percent by the FCX engine.</small></label>
        <label>Paused personalities<input name="paused_personalities_csv" value="${esc((settings.paused_personalities || []).join(", "))}" placeholder="panic, speculator"></label>
        <label>Paused tickers<input name="paused_tickers_csv" value="${esc((settings.paused_tickers || []).join(", "))}" placeholder="FCXS, FCXV"></label>
      </div></fieldset>
      <button class="primary save-engine" type="submit">Save complete engine configuration</button>
    </form>
    ${sectionHead("ENGINE INTELLIGENCE", "Live operating telemetry", "Cycles, risks, events, liquidity, sectors, and simulated investor activity remain visible after every run.")}
    <div class="grid">
      <article class="panel">${sectionHead("CYCLE LEDGER", "Recent cycles")}${dataTable(["Started","Cycle","Status","Listings","Executions","Volume","Runtime"], cycles.slice(0,40), row => `<tr><td>${esc(stamp(row.started_at))}</td><td><strong>${esc(row.cycle_key)}</strong><small>${esc(row.cycle_token)}</small></td><td>${esc(row.status)}</td><td>${number(row.securities_moved)}</td><td>${number(row.trades_executed)}</td><td>${money(row.volume)}</td><td>${number(row.duration_ms)} ms</td></tr>`)}</article>
      <article class="panel">${sectionHead("RISK FLAGS", "Open surveillance signals")}${dataTable(["Security","Type","Severity","Evidence","Last seen"], data.risk_flags || [], row => `<tr><td><strong>${esc(row.ticker || row.security_id)}</strong><small>${esc(row.name)}</small></td><td>${esc(row.flag_type)}</td><td>${esc(row.severity)}</td><td><code>${esc(safeJson(row.evidence_json))}</code></td><td>${esc(stamp(row.last_seen_at))}</td></tr>`)}</article>
      <article class="panel">${sectionHead("EVENT PRESSURE", "Market events")}${dataTable(["Event","Sentiment","Revenue","Volatility","Window","State"], data.events || [], row => `<tr><td><strong>${esc(row.title || row.event_type)}</strong><small>${esc(row.event_type)}</small></td><td>${percent(row.sentiment_impact)}</td><td>${percent(row.revenue_impact)}</td><td>${percent(row.volatility_impact)}</td><td>${stamp(row.starts_at)}<br><small>to ${stamp(row.ends_at)}</small></td><td>${esc(row.status)}</td></tr>`)}</article>
      <article class="panel">${sectionHead("LIQUIDITY", "Market-maker depth")}${dataTable(["Ticker","Market","Bid / Ask","Depth","Spread","Providers","Updated"], data.liquidity || [], row => `<tr><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${money(row.price)}</td><td>${money(row.bid_price)} / ${money(row.ask_price)}</td><td>${number(row.bid_depth)} / ${number(row.ask_depth)}</td><td>${percent(row.spread_percent)}</td><td>${number(row.provider_count)}</td><td>${esc(stamp(row.updated_at))}</td></tr>`)}</article>
      <article class="panel">${sectionHead("PERSONALITY BOOK", "Engine investor composition")}${dataTable(["Personality","Investors","Cash","Realized P&L","State"], personalities, row => `<tr><td><strong>${esc(row.personality)}</strong></td><td>${number(row.investors)}</td><td>${money(row.cash)}</td><td>${pnl(row.realized_pnl)}</td><td>${(settings.paused_personalities || []).includes(row.personality) ? "Paused" : "Active"}</td></tr>`)}</article>
      <article class="panel">${sectionHead("SECTOR STATE", "Sentiment and event pressure")}${dataTable(["Sector","Sentiment","Performance","Volatility","Event impact","Updated"], sectors, row => `<tr><td><strong>${esc(row.sector)}</strong></td><td>${percent(row.sentiment)}</td><td>${percent(row.performance)}</td><td>${percent(row.volatility)}</td><td>${percent(row.event_impact)}</td><td>${esc(stamp(row.updated_at))}</td></tr>`)}</article>
      <article class="panel wide">${sectionHead("CORPORATE ACTIONS", "Splits and dividends")}${dataTable(["Created","Security","Action","Ratio / Amount","Resident shares","Engine shares","Cash total","Status"], corporateActions, row => `<tr><td>${esc(stamp(row.created_at))}</td><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${esc(row.action_type)}</td><td>${row.action_type === "split" ? `${number(row.ratio_numerator)} : ${number(row.ratio_denominator)}` : money(row.amount_per_share)}</td><td>${number(row.eligible_resident_shares)}</td><td>${number(row.eligible_npc_shares)}</td><td>${money(row.total_cash_amount)}</td><td>${esc(row.status)}</td></tr>`)}</article>
      <article class="panel wide">${sectionHead("INVESTOR NETWORK", "Leading simulated investors")}${dataTable(["Investor","Personality","Cash","Gross equity","Realized P&L"], data.investor_leaders || [], row => `<tr><td><strong>${esc(row.name || row.id)}</strong></td><td>${esc(row.personality)}</td><td>${money(row.cash_balance)}</td><td>${money(row.gross_equity)}</td><td>${pnl(row.realized_pnl)}</td></tr>`)}</article>
      <article class="panel wide">${sectionHead("ENGINE EXECUTION AUDIT", "Latest autonomous decisions")}${dataTable(["Time","Cycle","Investor","Personality","Security","Action","Shares","Price","Notional","Confidence","Risk"], engineAudit, row => `<tr><td>${esc(stamp(row.created_at))}</td><td>${esc(row.cycle_id)}</td><td>${esc(row.investor_id)}</td><td>${esc(row.personality)}</td><td><strong>${esc(row.ticker || row.security_id)}</strong></td><td>${esc(row.action)}</td><td>${number(row.shares)}</td><td>${money(row.price)}</td><td>${money(row.notional)}</td><td>${percent(Number(row.confidence || 0) * (Number(row.confidence || 0) <= 1 ? 100 : 1))}</td><td>${percent(row.risk_score)}</td></tr>`)}</article>
      <article class="panel">${sectionHead("CORPORATE WATCHLIST", "Distress and bankruptcy model")}${dataTable(["Security","Price","Lifecycle","Fundamental","Risk","Bankruptcy","Loss cycles"], watchlist, row => `<tr><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${money(row.price)}</td><td>${esc(row.lifecycle_status)}</td><td>${esc(row.status)}</td><td>${percent(row.risk_score)}</td><td>${percent(row.bankruptcy_risk)}</td><td>${number(row.consecutive_losses)}</td></tr>`)}</article>
      <article class="panel">${sectionHead("ACTIVE ENGINE HALTS", "Circuit-breaker register")}${dataTable(["Security","Reason","Halted","Resume","State"], engineHalts, row => `<tr><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${esc(row.reason_label || row.reason_code)}</td><td>${esc(stamp(row.halted_at))}</td><td>${esc(stamp(row.automatic_resume_at))}</td><td>${esc(row.status)}</td></tr>`)}</article>
      <article class="panel wide">${sectionHead("FUNDAMENTALS", "Security model state")}${dataTable(["Ticker","Price","Fair value","Fundamental","Sentiment","Risk","Bankruptcy","Status"], securities, row => `<tr><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${money(row.price)}</td><td>${money(row.fair_value)}</td><td>${percent(row.fundamental_score)}</td><td>${percent(row.company_sentiment)}</td><td>${percent(row.risk_score)}</td><td>${percent(row.bankruptcy_risk)}</td><td>${esc(row.status)}</td></tr>`)}</article>
    </div>`;
}

async function renderOperations() {
  const data = await cached("operations", "/admin/operations");
  const activePrograms = (data.programs || []).filter(row => ["active","scheduled"].includes(String(row.status).toLowerCase()));
  const activeHalts = (data.halts || []).filter(row => row.status === "active");
  const activeDelistings = (data.delistings || []).filter(row => row.status === "active");
  return `${sectionHead("FCX OPERATIONS", "Scheduled movement and listing authority", "Move one security, a selected group, or an index; preserve final prices; halt, delist, resume, and relist from one desk.")}
    ${metrics([["Operating securities", number((data.securities || []).filter(row => yes(row.active)).length), `${number(data.securities?.length)} registered`],["Active programs", number(activePrograms.length), `${number(data.programs?.length)} retained in history`],["Active halts", number(activeHalts.length), "FEC restrictions live"],["Delisted", number(activeDelistings.length), "Preserved off-exchange records"],["Index funds", number(data.funds?.length), "FCXS / FCXV state"],["Price authority", "Online", "Immediate or future programs"]])}
    <div class="grid operator-grid">
      <article class="panel"><h4>Schedule movement</h4><p>Select multiple securities. The current price and calculated target remain visible before execution.</p><form id="price-program-form" class="form-grid"><label class="full">Securities<select name="security_ids" multiple size="10" required>${selectOptions(data.securities)}</select></label><label class="full">RP event<input name="event_name" placeholder="Documented market event" required></label><label>Percentage change<input name="percent_change" type="number" step="0.01" min="-99.99" max="1000" required></label><label>Duration (minutes)<input name="duration_minutes" type="number" min="1" max="43200" value="60" required></label><label class="full">Start time (blank = now)<input name="starts_at" type="datetime-local"></label><div id="program-preview" class="price-preview full">Select securities to preview current and target prices.</div><button class="primary full">Launch price program</button></form></article>
      <article class="panel"><h4>FEC market intervention</h4><form id="halt-form" class="form-grid"><label class="full">Securities<select name="security_ids" multiple size="6" required>${selectOptions(data.securities)}</select></label><label>Reason code<input name="reason_code" value="FEC_REVIEW" required></label><label>Reason label<input name="reason_label" value="FEC market review" required></label><label class="full">Public notice<textarea name="public_notice" required></textarea></label><label>Case reference<input name="case_reference"></label><label>Automatic resume<input name="automatic_resume_at" type="datetime-local"></label><button class="danger-action full">Halt selected securities</button></form><div class="panel-actions"><button id="resume-all-halts">Resume all active halts</button></div></article>
      <article class="panel"><h4>Delist security</h4><form id="delist-form" class="form-grid"><label class="full">Security<select name="security_id">${selectOptions((data.securities || []).filter(row => !row.delisted))}</select></label><label>Reason code<input name="reason_code" value="FEC_LISTING_ACTION" required></label><label>Reason label<input name="reason_label" value="FEC listing action" required></label><label class="full">Public notice<textarea name="public_notice" required></textarea></label><label class="full">Case reference<input name="case_reference"></label><button class="danger-action full">Halt and delist</button></form></article>
      <article class="panel"><div class="panel-heading"><div><h4>Index register</h4><p>Live fund structures and constituent counts.</p></div><button class="open-section" data-section="indexes" type="button">Open Index Settings</button></div>${dataTable(["Fund","Ticker","Price","Constituents","Rebalanced"], data.funds || [], row => `<tr><td><strong>${esc(row.fund_key)}</strong></td><td>${esc(row.ticker)}</td><td>${money(row.price)}</td><td>${number(row.constituent_count)}</td><td>${esc(stamp(row.last_rebalanced_at || row.updated_at))}</td></tr>`)}</article>
    </div>
    ${sectionHead("PROGRAM LEDGER", "Active, scheduled, and historical movements")}
    ${dataTable(["Security","Event","Move","Start","Target","Window","State","Action"], data.programs || [], row => `<tr><td><strong>${esc(row.ticker)}</strong></td><td>${esc(row.event_name)}</td><td class="${Number(row.percent_change)>=0?"up":"down"}">${percent(row.percent_change)}</td><td>${money(row.start_price)}</td><td>${money(row.target_price)}</td><td>${stamp(row.starts_at)}<br><small>to ${stamp(row.ends_at)}</small></td><td>${esc(row.status)}</td><td>${["active","scheduled"].includes(String(row.status).toLowerCase()) ? `<button class="stop-program" data-id="${row.id}">Stop & keep price</button>` : "—"}</td></tr>`) }
    ${sectionHead("FEC RESTRICTIONS", "Security halts")}
    ${dataTable(["Security","Reason","Case","Halted","State","Action"], data.halts || [], row => `<tr><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${esc(row.reason_label || row.reason_code)}</td><td>${esc(row.case_reference || "—")}</td><td>${esc(stamp(row.halted_at))}</td><td>${esc(row.status)}</td><td>${row.status === "active" ? `<button class="resume-halt" data-id="${row.id}">Resume</button>` : "—"}</td></tr>`) }
    ${sectionHead("LISTING REGISTER", "Delisted and relisted securities")}
    ${dataTable(["Security","Basis","Notice","Delisted","State","Action"], data.delistings || [], row => `<tr><td><strong>${esc(row.ticker)}</strong></td><td>${esc(row.reason_label || row.reason_code)}</td><td>${esc(row.public_notice)}</td><td>${esc(stamp(row.delisted_at))}</td><td>${esc(row.status)}</td><td>${row.status === "active" ? `<button class="relist-security" data-id="${row.id}">Relist on FCX</button>` : "—"}</td></tr>`)}`;
}

async function renderMarket() {
  const data = await marketData();
  const active = data.securities.filter(row => yes(row.active));
  const cap = active.reduce((sum, row) => sum + Number(row.price || 0) * Number(row.issued_shares || 0), 0);
  return `<section class="hero"><span class="status-chip">Authoritative market core</span><h3>Exchange state,<br>without a CAD dependency.</h3><p>Global securities, order flow, and market configuration are centralized in FCX Control.</p></section>
    ${metrics([["Operating securities", active.length, `${data.securities.length} records`], ["Market cap", money(cap), "Calculated from issued shares"], ["Recent orders", data.recent_orders.length, "Latest 100"], ["Order states", data.order_counts.length, "Distinct lifecycle states"], ["Settings", data.settings.length, "Central controls"], ["Engine", "Isolated", "Dedicated FCX database", "up"]])}
    ${sectionHead("MARKET TAPE", "Operating securities", "The shared price source consumed by every connected community.")}
    ${securitiesTable(data.securities)}`;
}

function securitiesTable(rows) {
  if (!rows.length) return `<div class="empty">No FCX securities have been created</div>`;
  return `<div class="panel wide table-wrap"><table><thead><tr><th>Ticker</th><th>Issuer</th><th>Price</th><th>Issued shares</th><th>Market cap</th><th>Status</th></tr></thead><tbody>${rows.map(row => `<tr><td><strong>${esc(row.ticker)}</strong></td><td>${esc(row.company_name || row.name || "—")}</td><td class="amount">${money(row.price)}</td><td class="amount">${esc(Number(row.issued_shares || 0).toLocaleString())}</td><td class="amount">${money(Number(row.price || 0) * Number(row.issued_shares || 0))}</td><td><span class="status-chip ${yes(row.active) ? "" : "danger"}">${yes(row.active) ? "Active" : "Inactive"}</span></td></tr>`).join("")}</tbody></table></div>`;
}

async function renderCompanies() {
  const data = await marketData();
  return `${sectionHead("ISSUER REGISTER", "Companies on FCX", "Central issuer visibility without access to either CAD's private records.")}${securitiesTable(data.securities)}`;
}

async function renderSecurities() {
  const data = await marketData(); return `${sectionHead("SECURITY MASTER", "Shared listings", "Every CAD sees the same ticker, price, and trading state.")}${securitiesTable(data.securities)}`;
}

async function renderOrders() {
  const data = await marketData();
  return `${sectionHead("ORDER SURVEILLANCE", "Recent exchange orders", "Central order records are linked to Ravenhood accounts, not CAD accounts.")}${ordersTable(data.recent_orders)}`;
}

async function renderTrades() {
  const data = await marketData();
  const closed = data.recent_orders.filter(row => ["filled", "settled", "completed"].includes(String(row.status).toLowerCase()));
  return `${sectionHead("CONSOLIDATED TAPE", "Completed exchange activity", "A central read-only view of completed order flow.")}${ordersTable(closed)}`;
}

function ordersTable(rows) {
  if (!rows.length) return `<div class="empty">No matching orders recorded</div>`;
  return `<div class="panel wide table-wrap"><table><thead><tr><th>Order</th><th>Account</th><th>Security</th><th>Side</th><th>Quantity</th><th>Price</th><th>Status</th><th>Created</th></tr></thead><tbody>${rows.map(row => `<tr><td>${esc(row.order_id || row.id)}</td><td>${esc(row.display_name || "Ravenhood account")}</td><td><strong>${esc(row.ticker)}</strong></td><td>${esc(row.side || row.action || "—")}</td><td class="amount">${esc(row.quantity || 0)}</td><td class="amount">${money(row.price || row.limit_price)}</td><td>${esc(row.status)}</td><td>${esc(stamp(row.created_at))}</td></tr>`).join("")}</tbody></table></div>`;
}

async function renderAccounts() {
  const data = await cached("accounts", "/admin/accounts?limit=500");
  return `${sectionHead("RAVENHOOD IDENTITY", "Cross-community exchange accounts", "Community links are visible; private CAD records and balances are not.")}${metrics([["Accounts", data.accounts.length, "Central identities"], ["Linked communities", data.accounts.reduce((sum, row) => sum + Number(row.community_count || 0), 0), "Verified links"], ["Brokerage cash", money(data.accounts.reduce((sum, row) => sum + Number(row.cash_balance || 0), 0)), "FCX-owned ledger only"], ["Holdings", money(data.accounts.reduce((sum, row) => sum + Number(row.holdings_value || 0), 0)), "Marked value"]])}<div class="panel wide table-wrap"><table><thead><tr><th>Account</th><th>Bohemia identity</th><th>Communities</th><th>Trading</th><th>Brokerage cash</th><th>Holdings</th></tr></thead><tbody>${data.accounts.map(row => `<tr><td><strong>${esc(row.display_name || row.account_id)}</strong><small>${esc(row.account_id)}</small></td><td>${esc(row.bohemia_identity_id || "Not verified")}</td><td>${esc(row.community_count)}</td><td>${esc(row.trading_status || row.status)}</td><td class="amount">${money(row.cash_balance)}</td><td class="amount">${money(row.holdings_value)}</td></tr>`).join("")}</tbody></table></div>`;
}

async function renderInvestigationsLegacy() {
  const data = await cached("investigations", "/admin/investigations?limit=500");
  const open = data.investigations.filter(row => row.status !== "closed");
  return `${sectionHead("FEC CASEWORK", "Investigation docket", "Cases operate on FCX-owned data and preserve community source without exposing CAD records.")}${metrics([["Open docket", open.length, "Active review"], ["Critical", open.filter(row => row.priority === "critical").length, "Immediate attention"], ["Restricted", open.filter(row => row.status === "restricted").length, "Account controls"], ["Closed", data.investigations.length - open.length, "Historical cases"]])}<div class="grid"><article class="panel third"><h4>Open a case</h4><p>Use the Ravenhood account ID shown in Accounts.</p><form id="case-form" class="form-grid"><label class="full">Account ID<input name="account_id" required></label><label class="full">Summary<textarea name="summary" minlength="10" required></textarea></label><label>Priority<select name="priority"><option>normal</option><option>low</option><option>high</option><option>critical</option></select></label><button class="primary" type="submit">Open investigation</button></form></article><article class="panel wide table-wrap"><table><thead><tr><th>Case</th><th>Account</th><th>Priority</th><th>Status</th><th>Summary</th><th>Updated</th></tr></thead><tbody>${data.investigations.map(row => `<tr><td><strong>${esc(row.case_id)}</strong></td><td>${esc(row.display_name || row.account_id)}<small>${esc(row.bohemia_identity_id || "")}</small></td><td>${esc(row.priority)}</td><td>${esc(row.status)}</td><td>${esc(row.summary)}</td><td>${esc(stamp(row.updated_at))}</td></tr>`).join("")}</tbody></table></article></div>`;
}

async function renderCommunities() {
  const data = await cached("communities", "/admin/communities");
  return `${sectionHead("CONNECTION REGISTRY", "Independent community control", "Disabling one CAD never changes another CAD's connection.")}<div class="grid"><article class="panel third"><h4>Register community</h4><p>Bridge secrets remain Railway environment references, never browser values.</p><form id="community-form" class="form-grid"><label class="full">Community ID<input name="community_id" pattern="[a-z0-9_-]+" required></label><label class="full">Community name<input name="community_name" required></label><label class="full">Bank bridge URL<input name="bank_bridge_url" type="url" required></label><label class="full">Secret environment name<input name="bank_secret_env" placeholder="CAD2_BANK_BRIDGE_SECRET" required></label><button class="primary full" type="submit">Register community</button></form></article><article class="panel wide">${data.communities.map(communityCard).join("") || `<div class="empty">No communities registered</div>`}</article></div>`;
}

function communityCard(row) {
  const controls = [["connection_enabled", "Connection"], ["trading_enabled", "Trading"], ["buy_enabled", "Buying"], ["sell_enabled", "Selling"], ["account_creation_enabled", "Accounts"], ["suspended", "Suspended"]];
  return `<div class="community-card"><div class="community-title"><div><h4>${esc(row.community_name)}</h4><small>${esc(row.community_id)} · ${esc(row.bank_bridge_url)}</small></div><span class="status-chip ${row.suspended ? "danger" : row.connection_enabled ? "" : "warn"}">${row.suspended ? "Suspended" : row.connection_enabled ? "Connected" : "Disabled"}</span></div><div class="toggle-row">${controls.map(([field, label]) => { const on = yes(row[field]); return `<button class="community-toggle ${on ? "on" : "off"}" data-community="${esc(row.community_id)}" data-field="${field}" data-value="${on ? "0" : "1"}">${label}: ${on ? "On" : "Off"}</button>`; }).join("")}</div><p class="muted">Last API contact: ${esc(stamp(row.last_seen_at))}</p></div>`;
}

async function renderConnections() {
  const [communitiesData, credentialsData] = await Promise.all([cached("communities", "/admin/communities"), cached("credentials", "/admin/credentials")]);
  return `${sectionHead("API SECURITY", "Community credentials", "Secrets are returned once, hashed at rest, and independently revocable.")}<div class="grid"><article class="panel third"><h4>Generate connection</h4><form id="credential-form" class="form-grid"><label class="full">Community<select name="community_id" required>${communitiesData.communities.map(row => `<option value="${esc(row.community_id)}">${esc(row.community_name)}</option>`).join("")}</select></label><label class="full">Scopes<input name="scopes" value="market:read,account:link,trade:write,settlement:write"></label><button class="primary full" type="submit">Generate credential</button></form><div id="credential-secret"></div></article><article class="panel wide table-wrap"><table><thead><tr><th>Credential</th><th>Community</th><th>Scopes</th><th>Status</th><th>Last used</th><th></th></tr></thead><tbody>${credentialsData.credentials.map(row => `<tr><td><strong>${esc(row.credential_id)}</strong></td><td>${esc(row.community_id)}</td><td>${esc(safeJson(row.scopes_json))}</td><td>${row.active ? "Active" : "Revoked"}</td><td>${esc(stamp(row.last_used_at))}</td><td>${row.active ? `<button class="revoke-credential danger-action" data-id="${esc(row.credential_id)}">Revoke</button>` : ""}</td></tr>`).join("")}</tbody></table></article></div>`;
}

async function renderLeverageLegacy() {
  const data = await marketData(); const settings = Object.fromEntries(data.settings.map(row => [row.setting_key, row.setting_value]));
  return `${sectionHead("RISK ENGINE", "Leverage administration", "Central limits apply consistently to every connected CAD.")}<div class="grid"><article class="panel"><h4>Current risk posture</h4><p>Leverage is ${yes(settings.leverage_enabled) ? "enabled" : "disabled"}; the configured ceiling is ${esc(settings.max_leverage || "not set")}x.</p>${bars([Number(settings.max_leverage || 1), 10, 25, 50, 100])}</article><article class="panel"><h4>Change leverage controls</h4><form id="leverage-form" class="form-grid"><label>Enabled<select name="leverage_enabled"><option value="true" ${yes(settings.leverage_enabled) ? "selected" : ""}>Enabled</option><option value="false" ${!yes(settings.leverage_enabled) ? "selected" : ""}>Disabled</option></select></label><label>Maximum leverage<input type="number" name="max_leverage" min="1" max="200" step="1" value="${esc(settings.max_leverage || 5)}"></label><button class="primary full" type="submit">Save risk controls</button></form></article></div>`;
}

async function renderPromotions() {
  const data = await cached("promotions", "/admin/fec/promotions");
  const campaigns = data.campaigns || [], redemptions = data.redemptions || [], securities = data.securities || [];
  const active = campaigns.filter(row => yes(row.active)).length;
  const reward = row => row.reward_type === "cash" ? money(row.cash_amount) : row.reward_type === "stock" ? `${number(row.share_quantity)} ${esc(row.ticker || "shares")}` : `${number(row.bundle_size)} random stocks × ${number(row.share_quantity)}`;
  return `${sectionHead("ACCOUNT PROMOTIONS", "Promotional Campaigns", "Issue and control portfolio rewards centrally for every connected CAD community. CAD owners cannot create these codes.")}
    ${metrics([["Active campaigns", active, `${campaigns.length} total`], ["Total claims", campaigns.reduce((sum,row)=>sum+Number(row.redemption_count||0),0), "Cross-community"], ["Authority", "FEC ONLY", "Central issuance", "up"]])}
    <div class="grid two"><form id="promotion-form" class="panel"><h4>Issue a promotional campaign</h4>
      <label>Campaign name<input name="campaign_name" maxlength="120" required></label><label>Custom code<input name="custom_code" maxlength="32" placeholder="Generated securely when blank"></label>
      <label>Reward type<select name="reward_type" id="promotion-reward"><option value="cash">Cash buying power</option><option value="stock">Selected stock</option><option value="random_bundle">Starter portfolio</option></select></label>
      <label data-promotion-cash>Cash amount<input name="cash_amount" type="number" min="0.01" step="0.01" value="5000"></label>
      <label data-promotion-stock hidden>Security<select name="security_id"><option value="">Select security</option>${selectOptions(securities)}</select></label>
      <label data-promotion-shares hidden>Shares per security<input name="share_quantity" type="number" min="0.000001" step="0.000001" value="1"></label>
      <label data-promotion-bundle hidden>Portfolio size<select name="bundle_size"><option value="3">3 stocks</option><option value="5">5 stocks</option><option value="9">9 stocks</option></select></label>
      <label>Redemption limit<input name="max_redemptions" type="number" min="1" max="100000" value="100" required></label><label>Expires after (days)<input name="expiry_days" type="number" min="1" max="365" value="30" required></label>
      <button type="submit">Issue promotional code</button><div id="promotion-secret"></div></form>
      <article class="panel"><h4>Recent redemptions</h4>${redemptions.map(row=>`<div class="audit-entry"><time>${esc(stamp(row.redeemed_at))}</time><strong>${esc(row.campaign_name)}</strong><span>${esc(row.display_name)} · ${esc(row.community_id || "FCX")}<br><small>${esc(row.reward_summary)}</small></span></div>`).join("")||`<div class="empty">No codes redeemed.</div>`}</article></div>
    ${dataTable(["Campaign","Reward","Claims","Expires","Control"],campaigns,row=>`<tr><td><strong>${esc(row.campaign_name)}</strong><small><code>${esc(row.code_plain)}</code> · ${esc(row.created_by_name)}</small></td><td>${reward(row)}</td><td>${number(row.redemption_count)} / ${number(row.max_redemptions)}</td><td>${esc(stamp(row.expires_at))}</td><td><button class="promotion-status" data-id="${row.id}" data-active="${yes(row.active)?0:1}">${yes(row.active)?"Pause":"Resume"}</button>${Number(row.redemption_count||0)?"":` <button class="promotion-delete danger" data-id="${row.id}">Delete</button>`}</td></tr>`,"No promotional campaigns issued.")}`;
}

async function renderLeverage() {
  const data = await cached("leverage", "/admin/leverage");
  const settings = Object.fromEntries((data.settings || []).map(row => [row.setting_key, row.setting_value]));
  const stats = data.stats || {};
  const open = data.open_positions || [];
  const closed = data.closed_positions || [];
  const requests = data.requests || [];
  return `${sectionHead("FCX MARGIN AUTHORITY", "Leverage command and exposure ledger", "Configure global and listing-level limits, monitor every open resident position, and retain the complete closed-position and order-request history.")}
    ${metrics([["Open positions", number(stats.open_positions || open.length), `${number(stats.closed_positions || closed.length)} historical`],["Locked collateral", money(stats.locked_collateral), "Resident equity committed"],["Open exposure", money(stats.open_exposure), "Marked leveraged notional"],["Realized P&L", pnl(stats.realized_pnl), "Closed-position ledger", "", true],["Order requests", number(requests.length), "Latest 1,000 retained"],["Identity coverage", number(open.filter(row => row.bohemia_identity_id).length), `${number(open.length)} open positions`]])}
    <div class="grid operator-grid">
      <article class="panel third"><h4>Global leverage policy</h4><p>Control the exchange-wide ceiling, collateral maintenance, position count, and maximum resident notional.</p><form id="leverage-form" class="form-grid"><label>Leverage<select name="leverage_enabled"><option value="true" ${yes(settings.leverage_enabled) ? "selected" : ""}>Enabled</option><option value="false" ${!yes(settings.leverage_enabled) ? "selected" : ""}>Disabled</option></select></label><label>Maximum leverage<input type="number" name="max_leverage" min="1" max="200" step="1" value="${fieldValue(settings.max_leverage || 5)}"></label><label>Margin trading<select name="margin_enabled"><option value="true" ${yes(settings.market_margin_enabled) ? "selected" : ""}>Enabled</option><option value="false" ${!yes(settings.market_margin_enabled) ? "selected" : ""}>Disabled</option></select></label><label>Maintenance %<input type="number" name="margin_maintenance_percent" min="5" max="80" step="0.01" value="${fieldValue(settings.market_margin_maintenance_percent || 20)}"></label><label>Open positions / account<input type="number" name="margin_max_open_positions" min="1" max="25" step="1" value="${fieldValue(settings.market_margin_max_open_positions || 5)}"></label><label>Maximum account notional<input type="number" name="margin_max_account_notional" min="100" max="1000000000" step="1" value="${fieldValue(settings.market_margin_max_account_notional || 10000000)}"></label><button class="primary full" type="submit">Save global policy</button></form></article>
      <article class="panel wide"><h4>Listing-level leverage controls</h4>${dataTable(["Security","Price","Margin","Maximum","Lifecycle","Action"], data.securities || [], row => `<tr><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${money(row.price)}</td><td><select class="security-margin-enabled" data-id="${row.id}"><option value="true" ${yes(row.margin_enabled) ? "selected" : ""}>Enabled</option><option value="false" ${!yes(row.margin_enabled) ? "selected" : ""}>Disabled</option></select></td><td><input class="security-margin-max compact-input" data-id="${row.id}" type="number" min="1" max="200" step="1" value="${fieldValue(row.margin_max_leverage || settings.max_leverage || 5)}"></td><td>${esc(row.lifecycle_status || (yes(row.active) ? "active" : "inactive"))}</td><td><button class="save-security-margin" data-id="${row.id}">Save</button></td></tr>`)}</article>
    </div>
    ${sectionHead("LIVE POSITION MONITOR", "Open resident exposure", "Names, community links, Bohemia identities, collateral, exposure, liquidation boundaries, and active FEC restrictions remain visible together.")}
    ${dataTable(["Resident / identity","Security","Side / leverage","Collateral","Exposure","Entry / mark","Liquidation","Live P&L","Opened","Restriction"], open, row => `<tr><td>${linkedIdentity(row)}</td><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td><strong>${esc(String(row.side || "").toUpperCase())} / ${number(row.leverage)}x</strong></td><td>${money(row.collateral)}</td><td>${money(row.entry_notional || row.exposure)}</td><td>${money(row.entry_price)}<small>${money(row.mark_price)}</small></td><td>${money(row.liquidation_price)}</td><td>${pnl(row.unrealized_pnl || row.live_pnl)}</td><td>${esc(stamp(row.opened_at))}</td><td>${row.restriction_scope ? `<span class="status-chip danger">${esc(row.restriction_scope)}</span>` : `<span class="status-chip">Clear</span>`}</td></tr>`)}
    ${sectionHead("POSITION HISTORY", "Closed, liquidated, and cancelled exposure", "Records never disappear when a position closes.")}
    ${dataTable(["Resident / identity","Security","Side / leverage","Collateral","Entry","Exit","Realized P&L","Outcome","Opened","Closed"], closed, row => `<tr><td>${linkedIdentity(row)}</td><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${esc(String(row.side || "").toUpperCase())} / ${number(row.leverage)}x</td><td>${money(row.collateral)}</td><td>${money(row.entry_price)}</td><td>${money(row.exit_price || row.close_price)}</td><td>${pnl(row.realized_pnl)}</td><td>${esc(row.status || row.close_reason)}</td><td>${esc(stamp(row.opened_at))}</td><td>${esc(stamp(row.closed_at))}</td></tr>`)}
    ${sectionHead("MARGIN ORDER LEDGER", "Past and present leverage requests", "Every submitted request is retained with resident identity and terminal state.")}
    ${dataTable(["Resident / identity","Security","Side","Collateral","Leverage","Requested price","State","Submitted","Resolved"], requests, row => `<tr><td>${linkedIdentity(row)}</td><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${esc(String(row.side || row.direction || "").toUpperCase())}</td><td>${money(row.collateral)}</td><td>${number(row.leverage)}x</td><td>${money(row.requested_price || row.entry_price)}</td><td>${esc(row.status)}</td><td>${esc(stamp(row.created_at || row.requested_at))}</td><td>${esc(stamp(row.resolved_at || row.updated_at))}</td></tr>`)} `;
}

function settlementLifecycle(rawState) {
  const value = String(rawState || "").toUpperCase();
  if (value === "CREATED") return "pending";
  if (["BANK_AUTHORIZED", "BANK_DEBITED", "BANK_CREDITED", "ORDER_EXECUTED"].includes(value)) return "claimed";
  if (value === "SETTLED") return "completed";
  if (value === "FAILED") return "failed";
  if (value === "REVERSED") return "cancelled";
  return "pending";
}

function settlementDirection(row) {
  return String(row.operation || "").toLowerCase() === "credit"
    ? { label: "Withdraw to game bank", short: "FCX TO GAME", css: "withdrawal" }
    : { label: "Deposit to FCX wallet", short: "GAME TO FCX", css: "deposit" };
}

async function renderBanking() {
  const data = await cached("admin-settlements", "/admin/settlements?limit=1000");
  const canAdmin = !currentRoles().has("fec_investigator");
  const rows = data.settlements || [];
  const lifecycleCounts = { all: rows.length, pending: 0, claimed: 0, completed: 0, failed: 0, cancelled: 0 };
  rows.forEach(row => { lifecycleCounts[settlementLifecycle(row.state)] += 1; });
  const recoverable = rows.filter(row => ["CREATED", "BANK_AUTHORIZED", "FAILED"].includes(String(row.state || "").toUpperCase())).length;
  const filters = [
    ["all", "All commands"], ["pending", "Pending"], ["claimed", "Claimed"],
    ["completed", "Completed"], ["failed", "Failed"], ["cancelled", "Cancelled"],
  ];
  return `${sectionHead("FCX BANKING OPERATIONS", "Game-bank transfer command center", "Ravenhood wallet funding remains separate from the read-only Arma bank snapshot. CAD services transport commands through the existing Bank Bridge; FCX owns wallet settlement state.")}
    <div class="settlement-filter-grid">${filters.map(([key, label]) => `<button class="settlement-filter ${state.settlementFilter === key ? "active" : ""}" data-filter="${key}"><small>${esc(label)}</small><strong>${number(lifecycleCounts[key] || 0)}</strong></button>`).join("")}</div>
    <div class="settlement-toolbar">
      <div><strong>${number(recoverable)} recoverable</strong><small>Pending, authorized, or failed commands may be cancelled safely. Claimed money movements are refreshed instead.</small></div>
      <button id="settlement-refresh-all">Refresh records</button>
      ${canAdmin ? `<button id="settlement-bulk-cancel" class="danger-action" ${recoverable ? "" : "disabled"}>Cancel all recoverable</button>` : ""}
    </div>
    <div class="table-wrap settlement-ledger"><table><thead><tr><th>Created</th><th>Resident / account / community</th><th>Direction</th><th>Amount</th><th>Lifecycle</th><th>Bridge detail</th><th>Actions</th></tr></thead><tbody>
      ${rows.map(row => {
        const lifecycle = settlementLifecycle(row.state);
        const direction = settlementDirection(row);
        const rawState = String(row.state || "").toUpperCase();
        const retryable = canAdmin && ["CREATED", "BANK_AUTHORIZED", "FAILED"].includes(rawState);
        const refreshable = canAdmin && !["SETTLED", "REVERSED"].includes(rawState);
        const failure = row.failure_message || row.failure_code || "";
        return `<tr data-settlement-row data-lifecycle="${lifecycle}" data-settlement-id="${esc(row.settlement_id)}"><td>${esc(stamp(row.created_at))}<small>${esc(row.settlement_id)}</small></td><td><strong>${esc(row.display_name || "Resident name unavailable")}</strong><small>${esc(row.account_id || row.community_user_id || "Unlinked account")} · ${esc(row.communities || row.community_id || "Unknown community")}</small></td><td><span class="settlement-direction ${direction.css}">${esc(direction.short)}</span><small>${esc(direction.label)}</small></td><td><strong>${money(row.amount)}</strong><small>${esc(row.currency || "FC")}</small></td><td><span class="settlement-state ${lifecycle}">${esc(lifecycle)}</span><small>${esc(rawState)}</small></td><td>${row.bank_reference ? `<strong>${esc(row.bank_reference)}</strong>` : "—"}${failure ? `<small class="failure-detail">${esc(failure)}</small>` : `<small>${esc(row.order_reference || "Awaiting bridge reference")}</small>`}</td><td><div class="settlement-actions">${refreshable ? `<button class="settlement-refresh" data-id="${esc(row.settlement_id)}">Refresh</button>` : ""}${retryable ? `<button class="settlement-retry" data-id="${esc(row.settlement_id)}">Retry</button><button class="settlement-cancel danger-action" data-id="${esc(row.settlement_id)}">Cancel</button>` : ""}</div></td></tr>`;
      }).join("") || `<tr><td colspan="7"><div class="empty compact">No FCX wallet commands have been submitted.</div></td></tr>`}
    </tbody></table></div>`;
}

async function renderInvestigations() {
  const data = await cached("fec-workspace", "/admin/fec/workspace");
  const accounts = data.accounts || [];
  const cases = data.investigations || [];
  const restrictions = data.restrictions || [];
  const executions = data.executions || [];
  const systemTrades = data.system_trades || [];
  const settlements = data.settlements || [];
  const holdings = data.holdings || [];
  const pnlWindows = data.pnl_windows || {};
  const assetPool = data.asset_pool || { balance: 0 };
  const assetLedger = data.asset_ledger || [];
  const ipoReviews = data.ipo_reviews || [];
  const equityResets = data.equity_resets || [];
  const shareResets = data.share_resets || [];
  const activeRestrictions = restrictions.filter(row => row.status === "active");
  const openCases = cases.filter(row => row.status !== "closed");
  const totalEquity = accounts.reduce((sum, row) => sum + Number(row.cash_balance || 0) + Number(row.holdings_value || 0), 0);
  const identityCount = accounts.filter(row => row.bohemia_identity_id).length;
  const accountOptions = accounts.map(row => `<option value="${row.market_account_id}">${esc(row.display_name || row.account_id)} · ${esc(row.bohemia_identity_id || "unlinked")} · ${money(Number(row.cash_balance || 0) + Number(row.holdings_value || 0))}</option>`).join("");
  const caseOptions = accounts.map(row => `<option value="${esc(row.account_id)}">${esc(row.display_name || row.account_id)} · ${esc(row.bohemia_identity_id || "unlinked")}</option>`).join("");
  return `${sectionHead("FEC MARKET INTEGRITY DIVISION", "Investigation, identity, and enforcement command", "One authoritative workspace for linked residents, account restrictions, cases, executions, market-maker flow, settlements, issuer state, security halts, and listing actions.")}
    ${metrics([["Ravenhood accounts", number(accounts.length), `${number(identityCount)} linked identities`],["Resident equity", money(totalEquity), "Cash plus marked holdings"],["Open investigations", number(openCases.length), `${number(cases.length)} retained cases`],["Active restrictions", number(activeRestrictions.length), `${number(restrictions.length)} historical orders`],["Resident executions", number(executions.length), "Latest consolidated tape"],["FCX settlements", number(settlements.length), "Latest settlement records"]])}
    <div class="grid operator-grid">
      <article class="panel"><h4>Restrict a Ravenhood account</h4><p>Lock the complete account, share trading only, or leverage only. The identity link and case reference remain attached.</p><form id="account-restriction-form" class="form-grid"><label class="full">Resident account<select name="account_id" required>${accountOptions}</select></label><label>Scope<select name="scope"><option value="full">Full trading lock</option><option value="equity">Share trading only</option><option value="leverage">Leverage only</option></select></label><label>Case reference<input name="case_reference" placeholder="FEC-CASE"></label><label class="full">Reason<textarea name="reason" required placeholder="Document the suspicious behavior and authority for restriction."></textarea></label><button class="danger-action full">Apply restriction</button></form></article>
      <article class="panel"><h4>Open financial investigation</h4><p>Create a retained FEC case against the central Ravenhood account.</p><form id="case-form" class="form-grid"><label class="full">Resident<select name="account_id" required>${caseOptions}</select></label><label>Priority<select name="priority"><option value="normal">Normal</option><option value="low">Low</option><option value="high">High</option><option value="critical">Critical</option></select></label><label class="full">Summary<textarea name="summary" required></textarea></label><button class="primary full">Open investigation</button></form></article>
    </div>
    ${sectionHead("IDENTITY REGISTER", "Linked Ravenhood accounts", "Account names, FCX equity, community memberships, Bohemia identity IDs, and current FEC restrictions are correlated here.")}
    ${dataTable(["Resident / identity","Ravenhood account","Cash","Holdings","Total equity","Trading state","Restriction"], accounts, row => `<tr><td>${linkedIdentity(row)}</td><td><strong>${esc(row.account_id)}</strong><small>Market ${esc(row.market_account_id || "not opened")}</small></td><td>${money(row.cash_balance)}</td><td>${money(row.holdings_value)}</td><td>${money(Number(row.cash_balance || 0)+Number(row.holdings_value || 0))}</td><td>${esc(row.trading_status || row.status)}</td><td>${row.restriction_scope ? `<span class="status-chip danger">${esc(row.restriction_scope)}</span><small>${esc(row.restriction_case || row.restriction_reason)}</small>` : `<span class="status-chip">Clear</span>`}</td></tr>`)}
    ${sectionHead("ENFORCEMENT REGISTER", "Account restriction history", "Active orders may be released without deleting their historical record.")}
    ${dataTable(["Resident / identity","Scope","Reason","Case","Issued by","Issued","State","Action"], restrictions, row => `<tr><td>${linkedIdentity(row)}</td><td><strong>${esc(row.scope)}</strong></td><td>${esc(row.reason)}</td><td>${esc(row.case_reference || "—")}</td><td>${esc(row.created_by_name)}</td><td>${esc(stamp(row.created_at))}</td><td>${esc(row.status)}</td><td>${row.status === "active" ? `<button class="release-restriction" data-id="${row.id}">Unlock</button>` : `Released ${esc(stamp(row.released_at))}`}</td></tr>`)}
    ${sectionHead("CASE DOCKET", "Open and historical investigations")}
    ${dataTable(["Case","Resident / identity","Priority","Summary","Status","Opened","Updated"], cases, row => `<tr><td><strong>${esc(row.case_id)}</strong></td><td>${linkedIdentity(row)}</td><td>${esc(row.priority)}</td><td>${esc(row.summary)}</td><td>${esc(row.status)}</td><td>${esc(stamp(row.opened_at || row.created_at))}</td><td>${esc(stamp(row.updated_at))}</td></tr>`)}
    ${sectionHead("RESIDENTIAL PROFIT & LOSS", "Marked FCX exposure by time window", "Realized margin, unrealized margin, equity movement, turnover, and resident leaders remain available for 12 hours, one day, and one week.")}
    <div class="grid">${Object.entries(pnlWindows).map(([window, report]) => `<article class="panel third"><span class="eyebrow">${esc(window.toUpperCase())}</span><h4>${pnl((report.summary || {}).net_pnl)}</h4><p>${number((report.summary || {}).executions)} executions · ${money((report.summary || {}).turnover)} turnover</p><dl class="dense-stats"><div><dt>Realized margin</dt><dd>${pnl((report.summary || {}).realized_margin)}</dd></div><div><dt>Unrealized margin</dt><dd>${pnl((report.summary || {}).unrealized_margin)}</dd></div><div><dt>Equity movement</dt><dd>${pnl((report.summary || {}).equity_unrealized)}</dd></div><div><dt>Fees</dt><dd>${money((report.summary || {}).fees)}</dd></div></dl>${(report.leaders || []).slice(0,5).map(row => `<div class="rank-row"><span>${linkedIdentity(row)}</span><strong>${pnl(row.net_pnl)}</strong></div>`).join("") || `<div class="empty">No resident movement</div>`}</article>`).join("")}</div>
    ${sectionHead("PORTFOLIO INTELLIGENCE", "Resident securities and cost basis", "Every recorded holding is correlated with its resident, linked identity, current quote, market value, and unrealized result.")}
    ${dataTable(["Resident / identity","Security","Quantity","Average cost","Current price","Market value","Unrealized P&L"], holdings, row => `<tr><td>${linkedIdentity(row)}</td><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${number(row.quantity,6)}</td><td>${money(row.average_cost || row.avg_cost)}</td><td>${money(row.price)}</td><td>${money(row.market_value)}</td><td>${pnl(row.unrealized_pnl)}</td></tr>`)}
    ${sectionHead("FEC ASSET CUSTODY", "Seizure and disposition control", "Held FCX cash remains auditable until it is returned to a cleared resident, removed from circulation, or reinvested across active market capitalization.")}
    ${metrics([["Custody pool", money(assetPool.balance), `Updated ${stamp(assetPool.updated_at)}`],["Custody actions", number(assetLedger.length), "Retained ledger"],["IPO reviews", number(ipoReviews.length), "Pending and completed filings"],["Equity resets", number(equityResets.length), "Developer-controlled"],["Share resets", number(shareResets.length), "Developer-controlled"]])}
    <div class="grid operator-grid">
      <article class="panel"><h4>Place resident cash in custody</h4><form id="fec-seizure-form" class="form-grid"><label class="full">Resident account<select name="account_id" required>${accountOptions}</select></label><label>Amount<input type="number" name="amount" min="0.01" max="50000000000" step="0.01" required></label><label>Case reference<input name="case_reference" required></label><label class="full">Legal basis / evidence<textarea name="reason" minlength="10" required></textarea></label><label>Typed authorization<input name="authorization" placeholder="SEIZE" required></label><button class="danger-action full">Execute custody order</button></form></article>
      <article class="panel"><h4>Resolve held assets</h4><form id="fec-disposition-form" class="form-grid"><label>Disposition<select name="disposition"><option value="return">Return to cleared resident</option><option value="reinvest">Reinvest by market cap</option><option value="forfeit">Permanent forfeiture</option></select></label><label>Amount<input type="number" name="amount" min="0.01" max="50000000000" step="0.01" required></label><label class="full">Return account<select name="target_account_id"><option value="">Not applicable</option>${accountOptions}</select></label><label>Case reference<input name="case_reference" required></label><label class="full">Final order / rationale<textarea name="reason" minlength="10" required></textarea></label><label>Typed authorization<input name="authorization" placeholder="FORECLOSE" required></label><button class="primary full">Execute disposition</button></form></article>
    </div>
    ${dataTable(["Time","Action","Amount","Pool after","Resident","Case","Reason","Officer"], assetLedger, row => `<tr><td>${esc(stamp(row.created_at))}</td><td><strong>${esc(row.event_type)}</strong></td><td>${money(row.amount)}</td><td>${money(row.pool_balance_after)}</td><td>${esc(row.target_name || row.target_account_id || "Market-wide")}</td><td>${esc(row.case_reference)}</td><td>${esc(row.reason)}</td><td>${esc(row.created_by_name)}</td></tr>`)}
    ${sectionHead("PRIMARY MARKET REVIEW", "IPO approval docket", "FEC approval state, issuer ownership, capitalization, release schedule, and review notes remain attached to the filing.")}
    ${dataTable(["Issuer","Ticker","Owner","Capitalization","Release","State","Review note","Decision"], ipoReviews, row => `<tr><td><strong>${esc(row.company_name)}</strong></td><td>${esc(row.ticker || "Pending")}</td><td>${esc(row.owner_name || row.owner_account_id)}</td><td>${money(row.target_market_cap || row.current_market_cap)}</td><td>${esc(stamp(row.scheduled_at))}</td><td>${esc(row.review_status || row.status)}</td><td>${esc(row.review_note || "—")}</td><td><div class="inline-actions"><button class="ipo-decision" data-id="${row.id}" data-decision="approved">Approve</button><button class="ipo-decision" data-id="${row.id}" data-decision="needs_changes">Changes</button><button class="ipo-decision danger-action" data-id="${row.id}" data-decision="rejected">Reject</button></div></td></tr>`)}
    ${state.user?.roles?.includes("developer") ? `${sectionHead("DESTRUCTIVE MARKET MAINTENANCE", "Developer-only resets", "These controls preserve an audit record but permanently remove resident FCX cash or holdings.")}<div class="grid"><article class="panel"><form id="fec-equity-reset-form" class="form-grid"><label class="full">Reason<textarea name="reason" minlength="10" required></textarea></label><label class="full">Confirmation<input name="confirmation" placeholder="WIPE FCX EQUITY" required></label><button class="danger-action full">Wipe all FCX equity cash</button></form></article><article class="panel"><form id="fec-share-reset-form" class="form-grid"><label class="full">Reason<textarea name="reason" minlength="10" required></textarea></label><label class="full">Confirmation<input name="confirmation" placeholder="WIPE FCX SHARES" required></label><button class="danger-action full">Wipe all resident shares</button></form></article></div>${dataTable(["Type","Affected accounts","Removed","Reason","Officer","Time"], [...equityResets.map(row => ({...row, type:"Equity cash", removed:row.removed_amount})), ...shareResets.map(row => ({...row, type:"Shares", removed:row.removed_shares}))].sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at))), row => `<tr><td>${esc(row.type)}</td><td>${number(row.affected_accounts)}</td><td>${row.type === "Equity cash" ? money(row.removed) : number(row.removed,6)}</td><td>${esc(row.reason)}</td><td>${esc(row.created_by_name)}</td><td>${esc(stamp(row.created_at))}</td></tr>`)}` : ""}
    ${sectionHead("CONSOLIDATED TRADE TAPE", "All resident executions", "Sort and investigate resident purchases and sales with community and identity context.")}
    ${dataTable(["Executed","Resident / identity","Security","Side","Quantity","Price","Gross","Fee"], executions, row => `<tr><td>${esc(stamp(row.created_at))}</td><td>${linkedIdentity(row)}</td><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td class="${String(row.side).toLowerCase().includes("buy")?"up":"down"}">${esc(row.side)}</td><td>${number(row.quantity,4)}</td><td>${money(row.unit_price)}</td><td>${money(row.gross_amount)}</td><td>${money(row.fee_amount)}</td></tr>`)}
    <div class="grid">
      <article class="panel wide">${sectionHead("BROKERAGE TAPE", "FCX system and market-maker executions")}${dataTable(["Time","Security","Side","Quantity","Price","Notional","Source"], systemTrades, row => `<tr><td>${esc(stamp(row.created_at || row.executed_at))}</td><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${esc(row.side)}</td><td>${number(row.quantity,4)}</td><td>${money(row.price || row.unit_price)}</td><td>${money(row.notional || row.gross_amount)}</td><td>${esc(row.source || row.actor_type || "Brokerage account")}</td></tr>`)}</article>
      <article class="panel">${sectionHead("SETTLEMENT CONTROL", "FCX settlement records")}${dataTable(["Community","Account","Amount","State","Created","Completed"], settlements, row => `<tr><td>${esc(row.community_id)}</td><td>${esc(row.account_id || row.ravenhood_account_id)}</td><td>${money(row.amount)}</td><td>${esc(row.status)}</td><td>${esc(stamp(row.created_at))}</td><td>${esc(stamp(row.completed_at || row.updated_at))}</td></tr>`)}</article>
      <article class="panel wide">${sectionHead("ISSUER SURVEILLANCE", "Resident and FCX company register")}${dataTable(["Issuer","Ticker","Owner","Price","Market cap","Lifecycle","Updated"], data.issuers || [], row => `<tr><td><strong>${esc(row.company_name || row.name)}</strong></td><td>${esc(row.ticker)}</td><td>${esc(row.owner_name || row.owner_account_id || "FCX")}</td><td>${money(row.price)}</td><td>${money(row.current_market_cap)}</td><td>${esc(row.lifecycle_status || row.status)}</td><td>${esc(stamp(row.updated_at))}</td></tr>`)}</article>
      <article class="panel">${sectionHead("SECURITY ENFORCEMENT", "Active trading halts")}${dataTable(["Security","Reason","Case","Since"], data.halts || [], row => `<tr><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${esc(row.reason_label || row.reason_code)}</td><td>${esc(row.case_reference || "—")}</td><td>${esc(stamp(row.halted_at))}</td></tr>`)}</article>
      <article class="panel">${sectionHead("LISTING ACTIONS", "Delisting history")}${dataTable(["Security","Reason","State","Time"], data.delistings || [], row => `<tr><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name)}</small></td><td>${esc(row.reason_label || row.reason_code)}</td><td>${esc(row.status)}</td><td>${esc(stamp(row.delisted_at))}</td></tr>`)}</article>
    </div>`;
}

async function renderSettings() {
  const data = await marketData(); const settings = Object.fromEntries(data.settings.map(row => [row.setting_key, row.setting_value]));
  const controls = [["market_open", "Market open"], ["buy_enabled", "Buying enabled"], ["sell_enabled", "Selling enabled"], ["account_creation_enabled", "Account creation"], ["maintenance_mode", "Maintenance mode"]];
  const sessionMode = settings.market_manual_override || "schedule";
  return `${sectionHead("GLOBAL CONTROLS", "Market settings", "The authoritative FCX session, fee, access, FCXV, and market-maker controls. These values are stored only by FCX Control.")}
    ${metrics([["Session mode", String(sessionMode).toUpperCase(), `${settings.market_schedule_open_time || "09:30"} - ${settings.market_schedule_close_time || "16:00"}`],["FCXV overnight", yes(settings.market_fcxv_24h_enabled) ? "Open" : "Scheduled", "Independent volatility session"],["Trade fee", `${number(settings.market_trade_fee_percent || 0.25,2)}%`, "Executed equity orders"],["Transfer fee", `${number(settings.market_transfer_fee_percent || 1.5,2)}%`, "Ravenhood transfers"],["Margin ceiling", `${number(settings.max_leverage || 5)}x`, `${number(settings.market_margin_maintenance_percent || 20,2)}% maintenance`],["Liquidity hunts", yes(settings.market_liquidation_hunts_enabled) ? "Armed" : "Off", `${money(settings.market_liquidation_hunt_threshold || 100000000)} threshold`]])}
    <div class="grid">${controls.map(([key, label]) => `<article class="panel third"><h4>${label}</h4><p>Current state: ${yes(settings[key]) ? "enabled" : "disabled"}</p><button class="market-toggle ${yes(settings[key]) ? "on" : "off"}" data-field="${key}" data-value="${yes(settings[key]) ? "0" : "1"}">${yes(settings[key]) ? "Disable" : "Enable"}</button></article>`).join("")}</div>
    ${sectionHead("SESSION AUTHORITY", "Exchange hours and FCXV access", "Run the regular exchange on its New York schedule, force an immediate open or close, and independently permit FCXV around the clock.")}
    <div class="grid operator-grid">
      <article class="panel"><form id="market-session-form" class="form-grid"><label>Session authority<select name="manual_override"><option value="schedule" ${sessionMode === "schedule" ? "selected" : ""}>Automatic schedule</option><option value="open" ${sessionMode === "open" ? "selected" : ""}>Force market open</option><option value="closed" ${sessionMode === "closed" ? "selected" : ""}>Force market closed</option></select></label><label>Timezone<input name="schedule_timezone" value="${fieldValue(settings.market_schedule_timezone || "America/New_York")}"></label><label>Opening time<input type="time" name="schedule_open_time" value="${fieldValue(settings.market_schedule_open_time || "09:30")}"></label><label>Closing time<input type="time" name="schedule_close_time" value="${fieldValue(settings.market_schedule_close_time || "16:00")}"></label><label>Weekend trading<select name="weekends_enabled"><option value="true" ${yes(settings.market_weekends_enabled) ? "selected" : ""}>Enabled</option><option value="false" ${!yes(settings.market_weekends_enabled) ? "selected" : ""}>Disabled</option></select></label><label>FCXV 24-hour trading<select name="fcxv_24h_enabled"><option value="true" ${yes(settings.market_fcxv_24h_enabled) ? "selected" : ""}>Enabled</option><option value="false" ${!yes(settings.market_fcxv_24h_enabled) ? "selected" : ""}>Follow exchange hours</option></select></label><button class="primary full">Save session controls</button></form></article>
      <article class="panel"><form id="market-fees-form" class="form-grid"><label>Trade fee %<input type="number" name="trade_fee_percent" min="0" max="10" step="0.01" value="${fieldValue(settings.market_trade_fee_percent || 0.25)}"></label><label>Transfer fee %<input type="number" name="transfer_fee_percent" min="0" max="25" step="0.01" value="${fieldValue(settings.market_transfer_fee_percent || 1.5)}"></label><button class="primary full">Save fee schedule</button></form><div class="control-note"><strong>Authoritative fee policy</strong><p>These rates apply when FCX executes the underlying operation. Connected CADs only display the resulting FCX data.</p></div></article>
    </div>
    ${sectionHead("CONTROLLED LIQUIDITY PRESSURE", "Market-maker liquidation hunts", "Configure audited pressure cycles against qualifying highly capitalized resident accounts without exposing CAD data.")}
    <article class="panel wide"><form id="hunt-form" class="form-grid"><label>Hunts<select name="liquidation_hunts_enabled"><option value="true" ${yes(settings.market_liquidation_hunts_enabled) ? "selected" : ""}>Enabled</option><option value="false" ${!yes(settings.market_liquidation_hunts_enabled) ? "selected" : ""}>Disabled</option></select></label><label>Buying-power threshold<input type="number" name="liquidation_hunt_threshold" min="0" step="1" value="${fieldValue(settings.market_liquidation_hunt_threshold || 100000000)}"></label><label>Trigger probability %<input type="number" name="liquidation_hunt_probability_percent" min="0" max="100" step="0.01" value="${fieldValue(settings.market_liquidation_hunt_probability_percent || 25)}"></label><label>Intensity<select name="liquidation_hunt_intensity"><option value="light" ${settings.market_liquidation_hunt_intensity === "light" ? "selected" : ""}>Light</option><option value="balanced" ${settings.market_liquidation_hunt_intensity === "balanced" ? "selected" : ""}>Balanced</option><option value="aggressive" ${settings.market_liquidation_hunt_intensity === "aggressive" ? "selected" : ""}>Aggressive</option><option value="extreme" ${settings.market_liquidation_hunt_intensity === "extreme" ? "selected" : ""}>Extreme</option></select></label><label>Maximum move %<input type="number" name="liquidation_hunt_max_move_percent" min="0.01" max="1000" step="0.01" value="${fieldValue(settings.market_liquidation_hunt_max_move_percent || 2)}"></label><label>Cooldown minutes<input type="number" name="liquidation_hunt_cooldown_minutes" min="1" max="10080" step="1" value="${fieldValue(settings.market_liquidation_hunt_cooldown_minutes || 60)}"></label><label>Market hours only<select name="liquidation_hunt_market_hours_only"><option value="true" ${yes(settings.market_liquidation_hunt_market_hours_only) ? "selected" : ""}>Yes</option><option value="false" ${!yes(settings.market_liquidation_hunt_market_hours_only) ? "selected" : ""}>No</option></select></label><button class="primary full">Save liquidity controls</button></form></article>`;
}

async function renderAudit() {
  const data = await cached("audit", "/admin/audit?limit=500");
  return `${sectionHead("IMMUTABLE OPERATIONS", "Privileged action ledger", "Credential, community, market, investigation, and settlement interventions are recorded here.")}<div class="panel wide">${data.audit.map(row => `<div class="audit-entry"><time>${esc(stamp(row.created_at))}</time><strong>${esc(row.action)}</strong><span>${esc(row.actor_role || row.actor_type)} → ${esc(row.target_type)} ${esc(row.target_id)}<br><small>${esc(row.reason || "No reason recorded")}</small></span></div>`).join("") || `<div class="empty">No audit records</div>`}</div>`;
}

async function renderHealth() {
  const [health, overview] = await Promise.all([request("/health"), cached("overview", "/admin/overview")]);
  return `<section class="hero"><span class="status-chip">Service healthy</span><h3>FCX is answering<br>for itself.</h3><p>The exchange database, authenticated control API, and standalone PWA are independent from CAD 1 and CAD 2.</p></section>${metrics([["Database", health.database, "Dedicated FCX connection", "up"], ["Communities", health.counts.communities, "Registry rows"], ["Pending settlements", health.counts.pending_settlements, "Awaiting terminal state"], ["Open cases", health.counts.open_investigations, "FEC docket"], ["Service", health.service, "API v1"], ["CAD dependency", "None", `${overview.communities.length} API clients`, "up"]])}`;
}

async function renderCommandOverview() {
  const canAdmin = isDeveloper() || currentRoles().has("fec_admin");
  const [data, positions, operations] = await Promise.all([cached("overview", "/admin/overview"), cached("live-summary", "/admin/live-positions?limit=12"), canAdmin ? cached("operations", "/admin/operations") : Promise.resolve({securities:[]})]);
  const settings = data.settings || {}, engine = data.engine || {}, engineSettings = engine.settings || {};
  const marketOpen = yes(settings.market_open), maintenance = yes(settings.maintenance_mode), trading = marketOpen && !maintenance;
  const buying = yes(settings.buy_enabled ?? "1"), selling = yes(settings.sell_enabled ?? "1"), accountCreation = yes(settings.account_creation_enabled ?? "1");
  const engineRunning = yes(engineSettings.enabled);
  const communities = data.communities || [];
  const recentPositions = positions.positions || [];
  const exposure = recentPositions.reduce((sum,row)=>sum+Number(row.current_value||row.entry_notional||0),0);
  const serviceHealth = engine.error ? "Degraded" : "Operational";
  const controlTile = (label,stateText,detail,button) => `<div class="control-tile"><span><strong>${esc(label)}</strong><small>${esc(stateText)} · ${esc(detail)}</small></span>${button}</div>`;
  const emergency = canAdmin ? `<div class="emergency-grid">
    ${controlTile("FCX Engine",engineRunning?"Running":"Halted","Autonomous execution",`<button class="emergency-command ${engineRunning?"danger-action":""}" data-command="${engineRunning?"pause":"resume"}">${engineRunning?"Halt":"Resume"}</button>`)}
    ${controlTile("FCX Market",maintenance?"Restricted":marketOpen?"Open":"Closed",maintenance?"Maintenance mode is blocking orders":"All exchange trading",`<button class="market-state ${marketOpen?"danger-action":""}" data-field="manual_override" data-value="${marketOpen?"closed":"open"}">${marketOpen?"Close Market":"Open Market"}</button>`)}
    ${controlTile("Maintenance Mode",maintenance?"Active":"Off",maintenance?"All resident orders are restricted":"Normal order processing",`<button class="market-state ${maintenance?"":"danger-action"}" data-field="maintenance_mode" data-value="${maintenance?"false":"true"}" data-confirm="${maintenance?"":"ENTER FCX MAINTENANCE MODE?\n\nAll resident stock orders will be restricted until maintenance mode is exited. This action will be audit logged."}">${maintenance?"Exit Maintenance":"Enter Maintenance"}</button>`)}
    ${controlTile("Buying",buying?"Enabled":"Disabled","Resident buy orders",`<button class="market-state" data-field="buy_enabled" data-value="${buying?"false":"true"}">${buying?"Disable":"Enable"}</button>`)}
    ${controlTile("Selling",selling?"Enabled":"Disabled","Resident sell orders",`<button class="market-state" data-field="sell_enabled" data-value="${selling?"false":"true"}">${selling?"Disable":"Enable"}</button>`)}
    ${controlTile("Account Creation",accountCreation?"Enabled":"Disabled","New Ravenhood account access",`<button class="market-state" data-field="account_creation_enabled" data-value="${accountCreation?"false":"true"}">${accountCreation?"Disable":"Enable"}</button>`)}
  </div>` : `<div class="control-note"><strong>Read-only regulatory access</strong><p>Emergency and market controls require FEC Administrator authorization. Current state remains visible.</p></div>`;
  const activity = (data.recent_actions||[]).slice(0,6).map(row=>`<div class="activity-item"><i></i><span><strong>${esc(row.action)}</strong><small>${esc(row.actor_role||row.actor_type)} · ${esc(row.target_type||"FCX")}</small></span><time>${esc(stamp(row.created_at))}</time></div>`).join("")||`<div class="empty compact">No recent FEC actions</div>`;
  const alerts = (data.recent_alerts||[]).slice(0,5).map(row=>`<div class="activity-item"><i class="down"></i><span><strong>${esc(row.title||row.alert_type||"Risk alert")}</strong><small>${esc(row.severity||"Open")} · ${esc(row.target||"Market")}</small></span><time>${esc(stamp(row.created_at))}</time></div>`).join("")||`<div class="empty compact">No active market alerts</div>`;
  const tradeActivity = (data.recent_trades||[]).slice(0,8).map(row=>`<div class="activity-item"><i class="${String(row.side).toLowerCase()==="sell"?"down":"up"}"></i><span><strong>${esc(String(row.side||"trade").toUpperCase())} ${number(row.quantity)} ${esc(row.ticker)}</strong><small>${esc(row.display_name||row.account_id)} · ${money(row.gross_amount)}</small></span><time>${esc(stamp(row.created_at))}</time></div>`).join("")||`<div class="empty compact">No recent exchange trades</div>`;
  return `<div class="command-header"><div><p class="eyebrow">FCX COMMAND CENTER</p><h3>Operational overview</h3><p>Live market authority, resident exposure, community connectivity, risk signals, and FEC activity from the central FCX control plane.</p></div><div class="command-time"><strong>${esc(new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}))}</strong><small>Last synchronized now</small></div></div>
    ${metrics([["Market",maintenance?"MAINTENANCE":marketOpen?"OPEN":"CLOSED",maintenance?"Orders restricted":settings.market_manual_override||"Scheduled authority",trading?"up":"down"],["FCX Engine",engineRunning?"RUNNING":"HALTED",engine.state?.status||"Autonomous engine",engineRunning?"up":"down"],["CAD Connections",communities.filter(c=>c.connection_enabled&&!c.suspended).length,`${communities.length} registered`],["Live Positions",recentPositions.length,money(exposure)],["Active Companies",data.totals?.securities||0,money(data.totals?.market_cap)],["System Health",serviceHealth,engine.error?"Engine status unavailable":"Core services responding",engine.error?"down":"up"]])}
    <div class="overview-layout"><div class="overview-main">
      <article class="panel emergency-panel"><div class="panel-heading"><div><p class="eyebrow">EMERGENCY AUTHORITY</p><h4>Engine, market, and maintenance controls</h4><p>Every transition requires server authorization and creates an immutable audit entry.</p></div><span class="status-chip ${trading&&engineRunning?"":"danger"}">${trading&&engineRunning?"Normal operations":maintenance?"Maintenance restriction active":"Intervention active"}</span></div>${emergency}</article>
      <article class="panel"><div class="panel-heading"><div><h4>Live Positions Monitor</h4><p>Highest-priority open resident leverage and liquidation exposure.</p></div><button class="open-section" data-section="positions">View all positions</button></div>${positionsTable(recentPositions)}</article>
      <div class="grid"><article class="panel"><div class="panel-heading"><div><h4>Recent FEC activity</h4><p>Latest privileged actions from the central audit ledger.</p></div><button class="open-section" data-section="audit">Open audit</button></div><div class="activity-list">${activity}</div></article><article class="panel"><div class="panel-heading"><div><h4>Recent alerts</h4><p>Open market-integrity and engine risk signals.</p></div><span class="status-chip ${(data.recent_alerts||[]).length?"warn":""}">${(data.recent_alerts||[]).length} open</span></div><div class="activity-list">${alerts}</div></article></div>
      <article class="panel"><div class="panel-heading"><div><h4>Trading activity</h4><p>${number(data.totals?.trades_last_hour)} trades · ${money(data.totals?.volume_last_hour)} volume in the last hour.</p></div><button class="open-section" data-section="operations">Market operations</button></div><div class="activity-list">${tradeActivity}</div></article>
    </div><aside class="overview-side">
      <article class="panel"><div class="panel-heading"><div><h4>Community connections</h4><p>CAD boundaries remain independently enforced.</p></div><button class="open-section" data-section="health">Diagnostics</button></div><div class="community-health-card">${communities.map(c=>`<div class="community-health-row"><header><span><strong>${esc(c.community_name)}</strong><small>${esc(c.community_id)} · ${esc(stamp(c.last_seen_at))}</small></span><span class="status-chip ${c.suspended||!c.connection_enabled?"danger":""}">${c.suspended?"Suspended":c.connection_enabled?"Connected":"Offline"}</span></header></div>`).join("")||`<div class="empty compact">No communities registered</div>`}</div></article>
      <article class="panel"><div class="panel-heading"><div><h4>Market configuration</h4><p>Authoritative exchange settings.</p></div></div><dl class="state-list">${[["Session",String(settings.market_manual_override||"schedule").toUpperCase()],["Market",marketOpen?"Open":"Closed"],["Maintenance",maintenance?"Active — orders restricted":"Off"],["Trading",trading?"Enabled":"Restricted"],["Buying",buying?"Enabled":"Disabled"],["Selling",selling?"Enabled":"Disabled"],["Account creation",accountCreation?"Enabled":"Disabled"],["Trade fee",`${settings.market_trade_fee_percent||0}%`],["Transfer fee",`${settings.market_transfer_fee_percent||0}%`]].map(([k,v])=>`<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl></article>
      <article class="panel"><div class="panel-heading"><div><h4>Quick actions</h4><p>Focused workflows open without leaving Overview.</p></div></div><div class="quick-actions"><button id="open-movement-modal" ${canAdmin?"":"disabled"}>Move a security</button><button class="open-section" data-section="operations">Companies</button><button class="open-section" data-section="accounts">Find account</button><button class="open-section" data-section="investigations">Investigate</button></div></article>
    </aside></div>
    <dialog id="movement-modal"><div class="modal-head"><div><p class="eyebrow">MARKET MOVEMENT</p><h3>Schedule controlled movement</h3><p>Review the security, direction, target change, duration, and reason before execution.</p></div><button class="modal-close" type="button" aria-label="Close">×</button></div><form id="quick-movement-form"><div class="modal-body form-grid"><label class="full">Security<select name="security_ids" required><option value="">Choose an active security</option>${selectOptions(operations.securities||[])}</select></label><label>Direction<select id="movement-direction"><option value="up">Up</option><option value="down">Down</option></select></label><label>Target movement %<input name="percent_change" type="number" min="0.01" max="1000" step="0.01" required></label><label>Duration minutes<input name="duration_minutes" type="number" min="1" max="43200" value="60" required></label><label class="full">FEC reason<input name="event_name" minlength="3" required placeholder="Document the authorized market event"></label></div><div class="modal-actions"><button class="modal-close" type="button">Cancel</button><button class="primary" ${canAdmin?"":"disabled"}>Review and execute</button></div></form></dialog>`;
}

function positionsTable(rows) { return dataTable(["Account / community","Symbol","Position","Quantity","Entry / current","Value","P/L","Leverage","Liquidation","Age"], rows, row=>`<tr><td><strong>${esc(row.display_name||row.account_id)}</strong><small>${esc(row.communities||"Unlinked")}</small></td><td><strong>${esc(row.ticker)}</strong><small>${esc(row.name||"")}</small></td><td>${esc(String(row.side||row.direction||"").toUpperCase())}</td><td>${number(row.quantity)}</td><td>${money(row.entry_price)}<small>${money(row.current_price||row.mark_price)}</small></td><td>${money(row.current_value||Number(row.quantity||0)*Number(row.current_price||row.mark_price||0))}</td><td>${pnl(row.unrealized_pnl)}</td><td>${number(row.leverage)}x</td><td>${money(row.liquidation_price)}</td><td>${row.age_seconds!=null?`${number(Number(row.age_seconds||0)/3600)}h`:esc(stamp(row.opened_at))}</td></tr>`, "No Active Leverage"); }

async function renderLivePositions() {
  const params = new URLSearchParams({...state.positionFilters,limit:"1000"});
  const data = await request(`/admin/live-positions?${params}`), rows=data.positions||[], f=state.positionFilters;
  const longCount=rows.filter(r=>r.side==="long").length, shortCount=rows.filter(r=>r.side==="short").length, profitable=rows.filter(r=>Number(r.unrealized_pnl)>=0).length;
  return `${sectionHead("LIVE POSITIONS", "Resident market exposure", "Monitor the authoritative open leverage ledger with account, community, direction, performance, and liquidation context.")}${metrics([["Open positions",rows.length,"Current filtered result"],["Long",longCount,"Directional exposure"],["Short",shortCount,"Directional exposure"],["Profitable",profitable,"Unrealized gain"],["Losing",rows.length-profitable,"Unrealized loss"],["Current value",money(rows.reduce((s,r)=>s+Number(r.current_value||0),0)),"Marked exposure"]])}<form id="position-filter-form" class="panel form-grid"><label>Community<input name="community" value="${fieldValue(f.community||"")}" placeholder="CAD community"></label><label>Stock symbol<input name="ticker" value="${fieldValue(f.ticker||"")}" placeholder="FCX ticker"></label><label>Account or resident<input name="account" value="${fieldValue(f.account||"")}" placeholder="Name or account ID"></label><label>Position<select name="side"><option value="">Long and short</option><option value="long" ${f.side==="long"?"selected":""}>Long only</option><option value="short" ${f.side==="short"?"selected":""}>Short only</option></select></label><label>Performance<select name="outcome"><option value="">Profitable and losing</option><option value="profitable" ${f.outcome==="profitable"?"selected":""}>Profitable</option><option value="losing" ${f.outcome==="losing"?"selected":""}>Losing</option></select></label><div class="button-row"><button class="primary">Apply filters</button><button type="button" id="clear-position-filters">Clear</button></div></form>${positionsTable(rows)}`;
}

async function renderExpandableAccounts() {
  const data=await cached("accounts","/admin/accounts?limit=500");
  return `${sectionHead("RAVENHOOD ACCOUNTS", "Expandable resident profiles", "Open an account to lazily load trading statistics, holdings, recent activity, and active leverage.")}${metrics([["Accounts",data.accounts.length,"Central identities"],["Brokerage cash",money(data.accounts.reduce((s,r)=>s+Number(r.cash_balance||0),0)),"FCX ledger"],["Holdings",money(data.accounts.reduce((s,r)=>s+Number(r.holdings_value||0),0)),"Marked value"]])}<div class="account-stack">${data.accounts.map(r=>`<details class="panel account-card" data-account="${esc(r.account_id)}"><summary><span><strong>${esc(r.display_name||r.account_id)}</strong><small>${esc(r.account_id)} · ${esc(r.communities||"Unlinked")}</small></span><span>${money(Number(r.cash_balance||0)+Number(r.holdings_value||0))}</span></summary><div class="account-detail empty">Open to load profile…</div></details>`).join("")||`<div class="empty">No Ravenhood accounts</div>`}</div>`;
}

async function renderSystemHealth() {
  const data=await request("/admin/system-health"); const icon=s=>s==="healthy"?"🟢":s==="warning"?"🟡":"🔴";
  return `${sectionHead("SYSTEM HEALTH", "Real FCX diagnostics", `Checked ${stamp(data.checked_at)}. Indicators are backed by current database, engine, service, authentication, and CAD connection state.`)}<div class="grid">${(data.services||[]).map(s=>`<article class="panel health-card"><h4>${icon(s.status)} ${esc(s.name)}</h4><p>${esc(s.detail)}</p></article>`).join("")}</div>${sectionHead("COMMUNITY SYSTEM HEALTH", "Independent CAD connections", "Each community retains its own connection and trading state.")}<div class="grid">${(data.communities||[]).map(c=>`<article class="panel"><h4>${icon(c.health)} ${esc(c.community_name)}</h4><p>${c.connected?"Connected":"Unavailable / stale"} · last contact ${esc(stamp(c.last_seen_at))}</p><dl class="state-list">${[["Connection",c.connection_enabled],["Trading",c.trading_enabled],["Buying",c.buy_enabled],["Selling",c.sell_enabled],["Accounts",c.account_creation_enabled],["Suspended",c.suspended]].map(([k,v])=>`<div><dt>${k}</dt><dd>${yes(v)?"On":"Off"}</dd></div>`).join("")}</dl></article>`).join("")}</div>`;
}

async function renderOnboarding() {
  const data=await request("/admin/fec/investigators");
  const catalog=data.role_catalog||[],assignable=new Set(data.assignable_roles||[]),accounts=data.accounts||data.investigators||[];
  const catalogByKey=new Map(catalog.map(role=>[role.key,role]));
  const roleCards=`<div class="role-catalog">${catalog.map(role=>`<article class="role-card"><div><span class="status-chip">${esc(role.label)}</span><strong>${esc(role.access)}</strong></div><p>${esc(role.description)}</p><small>${assignable.has(role.key)?"You may assign this role":"View-only role information"}</small></article>`).join("")}</div>`;
  const createPanel=assignable.size?`<form id="control-account-form" class="panel form-grid"><div class="full"><p class="eyebrow">AUTHORIZED PROVISIONING</p><h4>Create Control Account</h4><p>Select the role before issuing credentials. Permissions are enforced by the API, not only by this screen.</p></div><label class="full">Username<input name="username" minlength="3" maxlength="120" required></label><label class="full">Access role<select name="role" required>${catalog.filter(role=>assignable.has(role.key)).map(role=>`<option value="${esc(role.key)}">${esc(role.label)} - ${esc(role.access)}</option>`).join("")}</select></label><label>Password<input name="password" type="password" minlength="12" required></label><label>Confirm password<input name="confirm_password" type="password" minlength="12" required></label><button class="primary full">Create Control Account</button></form>`:`<article class="panel control-note"><strong>Account creation requires elevated authority</strong><p>You can review the role model and current accounts, but only a Developer/Super Admin may assign administrative roles. FEC Administrators may create Investigators.</p></article>`;
  const policyPanel=`<article class="panel"><p class="eyebrow">PROVISIONING GUARDRAILS</p><h4>Role hierarchy is enforced server-side</h4><dl class="state-list"><div><dt>Administrative roles</dt><dd>Commissioner / Developer</dd></div><div><dt>Investigator accounts</dt><dd>FEC Administrator</dd></div><div><dt>Password storage</dt><dd>PBKDF2 hashed</dd></div><div><dt>Access changes</dt><dd>Audit logged</dd></div><div><dt>Deleted accounts</dt><dd>History retained</dd></div></dl></article>`;
  const accountTable=dataTable(["Username","Assigned role","Access scope","Status","Created","Last login","Created by","Actions"],accounts,r=>{const keys=accountRoles(r),role=catalogByKey.get(keys[0])||{label:keys[0]||"Unassigned",access:"Custom access"},manageable=keys.some(key=>assignable.has(key));return `<tr><td><strong>${esc(r.display_name)}</strong><small>${esc(r.email)}</small></td><td><strong>${esc(role.label)}</strong><small>${esc(keys.join(", "))}</small></td><td>${esc(role.access)}</td><td>${r.deleted_at?"Deleted":yes(r.active)?"Active":"Revoked"}</td><td>${esc(stamp(r.created_at))}</td><td>${esc(stamp(r.last_login_at))}</td><td>${esc(r.created_by_name||"System")}</td><td>${r.deleted_at?"Retained for audit":manageable?`<button class="control-account-access" data-id="${r.id}" data-active="${yes(r.active)?0:1}">${yes(r.active)?"Revoke":"Restore"}</button> <button class="control-account-delete danger-action" data-id="${r.id}">Delete</button>`:`<span class="muted">Protected by role hierarchy</span>`}</td></tr>`;});
  return `${sectionHead("FEC ONBOARDING", "Role-based control accounts", "Create appropriately scoped FCX personnel accounts, understand exactly what each role can view, and preserve historical audit ownership when access changes.")}${roleCards}<div class="grid operator-grid">${createPanel}${policyPanel}<article class="panel wide"><div class="panel-heading"><div><h4>Current Control Accounts</h4><p>Managed Commissioner, FEC Administrator, FCX Administrator, and Investigator identities.</p></div><span class="status-chip">${number(accounts.length)} accounts</span></div>${accountTable}</article></div>`;
}

async function renderEngineEnhanced() {
  const [html,ops]=await Promise.all([renderEngine(),cached("operations","/admin/operations")]);
  const selector=`${sectionHead("MULTI-STOCK MOVEMENT", "Move selected securities", "Select one, several, or every eligible company and execute one audited movement program without opening securities individually.")}<form id="engine-movement-form" class="panel form-grid"><label class="full">Eligible active securities <span id="engine-selected-count">0 selected</span></label><div class="security-checklist full">${(ops.securities||[]).map(row=>`<label class="security-option"><input class="security-checkbox" type="checkbox" value="${row.id}"><span><strong>${esc(row.ticker)} · ${esc(row.name)}</strong><small>${money(row.price)} current price</small></span></label>`).join("")||`<div class="empty compact">No eligible active securities</div>`}</div><div class="button-row full"><button type="button" id="select-all-engine">Select all eligible</button><button type="button" id="clear-all-engine">Clear selection</button></div><label>Target movement %<input name="percent_change" type="number" min="-99.99" max="1000" step="0.01" required placeholder="Negative values move down"></label><label>Duration minutes<input name="duration_minutes" type="number" min="1" max="43200" value="60"></label><label class="full">FEC reason<input name="event_name" minlength="3" required placeholder="Document the authorized market event"></label><button class="primary full">Review selected movement</button></form>`;
  return html.replace(sectionHead("DEPLOYMENT CONTROL", "Launch readiness and persistent state", "Pre-flight totals are read directly from FCX. Deployment history remains visible after every restart."),selector+sectionHead("DEPLOYMENT CONTROL", "Launch readiness and persistent state", "Pre-flight totals are read directly from FCX. Deployment history remains visible after every restart."));
}

async function renderLeverageEnhanced() {
  const [html,data]=await Promise.all([renderLeverage(),cached("leverage","/admin/leverage")]); const s=Object.fromEntries((data.settings||[]).map(r=>[r.setting_key,r.setting_value]));
  return `${html}${sectionHead("MARKET MAKER", "Liquidation Hunts", "The existing audited liquidation-pressure policy now lives with leverage configuration.")}<form id="hunt-form" class="panel form-grid"><label>Hunts<select name="liquidation_hunts_enabled"><option value="true" ${yes(s.market_liquidation_hunts_enabled)?"selected":""}>Enabled</option><option value="false" ${!yes(s.market_liquidation_hunts_enabled)?"selected":""}>Disabled</option></select></label><label>Exposure threshold<input name="liquidation_hunt_threshold" type="number" min="0" value="${fieldValue(s.market_liquidation_hunt_threshold||100000000)}"></label><label>Probability %<input name="liquidation_hunt_probability_percent" type="number" min="0" max="100" step=".01" value="${fieldValue(s.market_liquidation_hunt_probability_percent||25)}"></label><label>Intensity<select name="liquidation_hunt_intensity">${["light","balanced","aggressive","extreme"].map(v=>`<option ${s.market_liquidation_hunt_intensity===v?"selected":""}>${v}</option>`).join("")}</select></label><label>Maximum move %<input name="liquidation_hunt_max_move_percent" type="number" min=".01" max="1000" step=".01" value="${fieldValue(s.market_liquidation_hunt_max_move_percent||2)}"></label><label>Cooldown minutes<input name="liquidation_hunt_cooldown_minutes" type="number" min="1" value="${fieldValue(s.market_liquidation_hunt_cooldown_minutes||60)}"></label><label>Market hours only<select name="liquidation_hunt_market_hours_only"><option value="true" ${yes(s.market_liquidation_hunt_market_hours_only)?"selected":""}>Yes</option><option value="false" ${!yes(s.market_liquidation_hunt_market_hours_only)?"selected":""}>No</option></select></label><button class="primary full">Save liquidation-hunt policy</button></form>`;
}

async function renderInvestigationsEnhanced() {
  const accounts=await cached("accounts","/admin/accounts?limit=500"),investigator=currentRoles().has("fec_investigator");
  return `${sectionHead("FEC INVESTIGATIONS", "Complete resident financial history", "Select a Ravenhood account to open its authoritative identity, active leverage, period P/L analytics, and server-paginated transaction history.")}<form id="investigation-account-form" class="panel form-grid"><label class="full">Resident / Ravenhood account<select name="account_id" required><option value="">Choose an account to investigate</option>${(accounts.accounts||[]).map(row=>`<option value="${esc(row.account_id)}" ${state.investigation.accountId===row.account_id?"selected":""}>${esc(row.display_name||row.account_id)} — ${esc(row.account_id)} · ${esc(row.communities||"Unlinked")}</option>`).join("")}</select></label><label>Analysis period<select name="period"><option value="day" ${state.investigation.period==="day"?"selected":""}>DAY</option><option value="week" ${state.investigation.period==="week"?"selected":""}>WEEK</option><option value="month" ${state.investigation.period==="month"?"selected":""}>MONTH</option><option value="year" ${state.investigation.period==="year"?"selected":""}>YEAR</option></select></label><label>History search<input name="search" value="${fieldValue(state.investigation.search)}" placeholder="Symbol or transaction type"></label><button class="primary full">Open financial profile</button></form><div id="investigation-account-result">${state.investigation.accountId?`<div class="empty">Loading account investigation…</div>`:`<div class="empty">Select a resident account to begin an investigation.</div>`}</div>${investigator?"":`<details class="legacy-tools"><summary>Advanced FEC casework, restrictions, custody, issuers, and reset tools</summary><div id="advanced-investigation-tools" class="legacy-tools-content"><button id="load-advanced-investigations">Load advanced regulatory workspace</button></div></details>`}`;
}

async function renderProfessionalAccounts() {
  const data=await cached("accounts","/admin/accounts?limit=500"),accounts=data.accounts||[];
  return `${sectionHead("RAVENHOOD ACCOUNTS", "Resident account directory", "Search compact account records and expand only the resident profile you need. Trading statistics, activity, holdings, and leverage load on demand.")}${metrics([["Accounts",accounts.length,"Central identities"],["Linked communities",accounts.reduce((s,r)=>s+Number(r.community_count||0),0),"Verified relationships"],["Brokerage cash",money(accounts.reduce((s,r)=>s+Number(r.cash_balance||0),0)),"FCX ledger"],["Holdings",money(accounts.reduce((s,r)=>s+Number(r.holdings_value||0),0)),"Marked value"]])}<div class="panel wide"><div class="panel-heading"><div><h4>Account directory</h4><p>Search by resident, Ravenhood ID, Bohemia identity, or community.</p></div><input id="account-directory-search" type="search" placeholder="Search accounts" aria-label="Search Ravenhood accounts"></div><div class="account-stack">${accounts.map(r=>`<details class="panel account-card" data-account="${esc(r.account_id)}" data-search="${esc(`${r.display_name} ${r.account_id} ${r.bohemia_identity_id} ${r.communities}`.toLowerCase())}"><summary><span><strong>${esc(r.display_name||r.account_id)}</strong><small>${esc(r.account_id)} · ${esc(r.communities||"Unlinked")} · ${esc(r.trading_status||r.status)}</small></span><span>${money(Number(r.cash_balance||0)+Number(r.holdings_value||0))} portfolio value</span></summary><div class="account-detail empty">Open to load the complete resident profile…</div></details>`).join("")||`<div class="empty">No Ravenhood accounts</div>`}</div></div>`;
}

async function renderIndexes() {
  const data=await cached("indexes","/admin/indexes"),funds=data.funds||[],eligible=data.eligible_securities||[];
  let selected=funds.find(fund=>String(fund.id)===String(state.indexDesk.fundId))||funds[0]||null;
  if(selected)state.indexDesk.fundId=String(selected.id);
  if(selected&&(!state.indexDesk.dirty||state.indexDesk.draftFundId!==String(selected.id))){state.indexDesk.draftFundId=String(selected.id);state.indexDesk.draft=(selected.members||[]).map(member=>Number(member.security_id));state.indexDesk.dirty=false;}
  const securitiesById=new Map(eligible.map(security=>[Number(security.id),security]));
  const draft=selected?state.indexDesk.draft.filter(id=>securitiesById.has(Number(id))):[];
  const draftRows=draft.map(id=>securitiesById.get(Number(id))).filter(Boolean),draftSet=new Set(draft.map(Number));
  const available=eligible.filter(security=>!draftSet.has(Number(security.id)));
  const totalCap=draftRows.reduce((sum,row)=>sum+Number(row.market_cap||Number(row.price||0)*Number(row.issued_shares||0)),0);
  const memberCard=(row,index)=>{const cap=Number(row.market_cap||Number(row.price||0)*Number(row.issued_shares||0)),weight=totalCap>0?cap/totalCap*100:draftRows.length?100/draftRows.length:0;return `<article class="index-security-card" draggable="true" data-index-security="${row.id}" data-index-source="members" data-search="${esc(`${row.ticker} ${row.name} ${row.sector}`.toLowerCase())}"><span class="drag-handle" aria-hidden="true">⋮⋮</span><b>${String(index+1).padStart(2,"0")}</b><div><strong>${esc(row.ticker)} · ${esc(row.name)}</strong><small>${esc(row.sector||"General")} · ${money(cap)} market cap</small></div><span class="index-weight">${number(weight,2)}%</span><button class="index-remove" data-id="${row.id}" type="button" aria-label="Remove ${esc(row.ticker)}">Remove</button></article>`;};
  const poolCard=row=>`<article class="index-security-card compact" draggable="true" data-index-security="${row.id}" data-index-source="pool" data-search="${esc(`${row.ticker} ${row.name} ${row.sector}`.toLowerCase())}"><span class="drag-handle" aria-hidden="true">⋮⋮</span><div><strong>${esc(row.ticker)} · ${esc(row.name)}</strong><small>${esc(row.sector||"General")} · ${money(row.market_cap)} market cap</small></div><button class="index-add" data-id="${row.id}" type="button" aria-label="Add ${esc(row.ticker)}">Add</button></article>`;
  const registry=`<aside class="panel index-registry"><div class="panel-heading"><div><p class="eyebrow">INDEX REGISTER</p><h4>${funds.length} active structures</h4><p>Select a fund to edit its live constituent basket.</p></div></div><div class="index-list">${funds.map(fund=>`<button class="index-select ${selected&&Number(selected.id)===Number(fund.id)?"active":""}" data-id="${fund.id}" type="button"><span><strong>${esc(fund.ticker)}</strong><small>${esc(fund.display_name)}</small></span><b>${number((fund.members||[]).length)}</b></button>`).join("")||`<div class="empty compact">No indexes created</div>`}</div><button id="rebalance-all-indexes" class="primary" type="button" ${funds.length?"":"disabled"}>Restructure all by market cap</button><details class="index-create"><summary>Form a new index</summary><form id="index-create-form" class="form-grid"><label>Public ticker<input name="ticker" maxlength="12" placeholder="FCXM" required></label><label>Internal key<input name="fund_key" maxlength="40" placeholder="MARKET_10" required></label><label class="full">Display name<input name="display_name" maxlength="120" placeholder="FCX Market 10" required></label><label>Target companies<input name="target_size" type="number" min="1" max="50" value="10" required></label><label>Starting NAV<input name="base_nav" type="number" min="1" step=".01" value="100" required></label><label>Management fee %<input name="management_fee_percent" type="number" min="0" max="10" step=".01" value="0"></label><label class="check-row"><input name="auto_populate" type="checkbox" checked> Populate by market cap</label><label class="full">Description<textarea name="description" maxlength="1000">FCX market-cap weighted index.</textarea></label><button class="primary full">Create index</button></form></details></aside>`;
  const builder=selected?`<section class="panel index-builder"><div class="panel-heading"><div><p class="eyebrow">COMPOSITION BUILDER</p><h4>${esc(selected.ticker)} · ${esc(selected.display_name)}</h4><p>Drag companies between the eligible pool and this index. Saving preserves the current fund NAV and recalculates weights from live market capitalization.</p></div><span class="status-chip ${state.indexDesk.dirty?"warn":""}">${state.indexDesk.dirty?"UNSAVED DRAFT":"LIVE COMPOSITION"}</span></div><div class="index-summary">${[["Fund price",money(selected.price)],["Constituents",draftRows.length],["Basket cap",money(totalCap)],["Target size",selected.target_size],["Last restructured",stamp(selected.last_rebalanced_at)]].map(([label,value])=>`<span><small>${esc(label)}</small><strong>${esc(value)}</strong></span>`).join("")}</div><div class="index-builder-actions"><button id="rebalance-selected-index" type="button">Automate ${esc(selected.ticker)} by market cap</button><input id="index-change-reason" value="Index composition updated by authorized FEC operator" aria-label="Index change reason"><button id="save-index-composition" class="primary" type="button" ${state.indexDesk.dirty?"":"disabled"}>Save composition</button></div><div class="index-composition-grid"><section><header><div><small>LIVE BASKET DRAFT</small><h4>${draftRows.length} constituents</h4></div><span>Drop stocks here to add</span></header><div class="index-drop-zone" data-index-drop="members">${draftRows.map(memberCard).join("")||`<div class="empty compact">Drag eligible stocks here to build this index.</div>`}</div></section><section><header><div><small>ELIGIBLE COMPANY POOL</small><h4>${available.length} available</h4></div><input id="index-security-search" type="search" placeholder="Search ticker or company"></header><div class="index-drop-zone pool" data-index-drop="pool">${available.map(poolCard).join("")||`<div class="empty compact">Every eligible company is currently in this index.</div>`}</div></section></div></section>`:`<section class="panel index-builder"><div class="empty">Create the first FCX index to open the composition builder.</div></section>`;
  return `${sectionHead("INDEX AUTHORITY", "Index Settings", "Form FCX indexes, control their constituent baskets by drag-and-drop, and run an audited market-cap restructure from current prices and issued shares.")}${metrics([["Indexes",funds.length,"Enabled FCX funds"],["Eligible companies",eligible.length,"Active operational securities"],["Eligible market cap",money(data.eligible_market_cap),"Current price × issued shares"],["Weighting","MARKET CAP","Exact normalized weights"],["Engine pricing","CONNECTED","Existing constituent NAV service","up"]])}<div class="index-command-layout">${registry}${builder}</div>`;
}

async function renderProfessionalHealth() {
  const [data,overview]=await Promise.all([request("/admin/system-health"),cached("overview","/admin/overview")]);
  const icon=status=>status==="healthy"?"●":status==="warning"?"▲":"■";
  const tone=status=>status==="healthy"?"up":status==="warning"?"":"down";
  const communityName=(row,index)=>index===0?`CAD 1 — ${row.community_name}`:index===1?`CAD 2 — ${row.community_name}`:row.community_name;
  const settings=overview.settings||{},marketOpen=yes(settings.market_open),maintenance=yes(settings.maintenance_mode),canAdmin=isDeveloper()||currentRoles().has("fec_admin");
  const recovery=`<article class="panel emergency-panel"><div class="panel-heading"><div><p class="eyebrow">MARKET RECOVERY</p><h4>${maintenance?"Maintenance restriction active":marketOpen?"Market access is open":"Market access is closed"}</h4><p>${maintenance?"Resident stock orders are currently blocked by FCX maintenance mode.":"Use the authoritative controls below to recover market access or inspect security-level restrictions."}</p></div><span class="status-chip ${maintenance||!marketOpen?"danger":""}">${maintenance?"RESTRICTED":marketOpen?"OPEN":"CLOSED"}</span></div>${canAdmin?`<div class="command-deck"><button class="market-state ${marketOpen?"danger-action":""}" data-field="manual_override" data-value="${marketOpen?"closed":"open"}" data-refresh-section="health">${marketOpen?"Close Market":"Open Market"}</button><button class="market-state ${maintenance?"":"danger-action"}" data-field="maintenance_mode" data-value="${maintenance?"false":"true"}" data-refresh-section="health" data-confirm="${maintenance?"":"ENTER FCX MAINTENANCE MODE?\n\nAll resident stock orders will be restricted until maintenance mode is exited. This action will be audit logged."}">${maintenance?"Exit Maintenance":"Enter Maintenance"}</button><button class="open-section" data-section="operations">Open Security Restrictions</button><button class="open-section" data-section="overview">All Market Controls</button></div>`:`<div class="control-note"><strong>Administrator action required</strong><p>FEC Investigator access is read-only. Contact an FEC Administrator to change market or maintenance state.</p></div>`}</article>`;
  return `${recovery}${sectionHead("SYSTEM HEALTH", "Infrastructure diagnostics", `Real operational checks from the FCX API, database, engines, authentication, and community connection registry. Last checked ${stamp(data.checked_at)}.`)}<div class="health-grid">${(data.services||[]).map(service=>`<article class="health-indicator"><span class="${tone(service.status)}">${icon(service.status)}</span><strong>${esc(service.name)}</strong><small>${esc(service.detail)}</small></article>`).join("")||`<div class="empty">No service diagnostics available</div>`}</div>${sectionHead("COMMUNITY SYSTEM HEALTH", "Independent CAD connections", "CAD 1 and CAD 2 remain separately permissioned. Connectivity is derived from recent authenticated API activity and current backend configuration.")}<div class="grid">${(data.communities||[]).map((c,index)=>`<article class="panel"><div class="panel-heading"><div><h4>${esc(communityName(c,index))}</h4><p>${esc(c.community_id)} · last contact ${esc(stamp(c.last_seen_at))}</p></div><span class="status-chip ${c.connected?"":"danger"}">${c.connected?"Connected":"Unavailable"}</span></div><dl class="state-list">${[["Connection",c.connection_enabled,!c.connection_enabled],["Trading",c.trading_enabled,!c.trading_enabled],["Buying",c.buy_enabled,!c.buy_enabled],["Selling",c.sell_enabled,!c.sell_enabled],["Accounts",c.account_creation_enabled,!c.account_creation_enabled],["Suspended",c.suspended,c.suspended]].map(([label,value,bad])=>`<div><dt>${esc(label)}</dt><dd class="${bad?"down":"up"}">${yes(value)?"On":"Off"} ${bad?"!":"✓"}</dd></div>`).join("")}</dl></article>`).join("")||`<div class="empty">No connected communities registered</div>`}</div>`;
}

const renderers = { overview: renderCommandOverview, engine: renderEngineEnhanced, positions: renderLivePositions, operations: renderOperations, indexes: renderIndexes, accounts: renderProfessionalAccounts, promotions: renderPromotions, banking: renderBanking, investigations: renderInvestigationsEnhanced, communities: renderCommunities, connections: renderConnections, leverage: renderLeverageEnhanced, onboarding: renderOnboarding, audit: renderAudit, health: renderProfessionalHealth };

function formBody(form) { return Object.fromEntries(new FormData(form)); }
function optionalNumber(value) { return value === "" || value === null || value === undefined ? null : Number(value); }
function isoFromLocal(value) { return value ? new Date(value).toISOString() : null; }
async function mutation(path, method, body, message, section = state.section) {
  try {
    await request(path, { method, ...(body === undefined ? {} : { body }) });
    state.cache.clear();
    if (message) notice(message);
    await openSection(section, true);
  } catch (error) { notice(error.message, true); }
}

function bindActions() {
  document.querySelectorAll(".open-section").forEach(button=>button.addEventListener("click",()=>openSection(button.dataset.section)));
  document.querySelectorAll(".index-select").forEach(button=>button.addEventListener("click",()=>{
    state.indexDesk={fundId:String(button.dataset.id),draftFundId:"",draft:[],dirty:false};
    openSection("indexes",true);
  }));
  const updateIndexDraft=(securityId,add)=>{
    const id=Number(securityId),without=state.indexDesk.draft.map(Number).filter(value=>value!==id);
    state.indexDesk.draft=add?[...without,id]:without;
    state.indexDesk.dirty=true;
    openSection("indexes",true);
  };
  document.querySelectorAll(".index-add").forEach(button=>button.addEventListener("click",()=>updateIndexDraft(button.dataset.id,true)));
  document.querySelectorAll(".index-remove").forEach(button=>button.addEventListener("click",()=>updateIndexDraft(button.dataset.id,false)));
  document.querySelectorAll("[data-index-security]").forEach(card=>{
    card.addEventListener("dragstart",event=>{event.dataTransfer.effectAllowed="move";event.dataTransfer.setData("text/plain",String(card.dataset.indexSecurity));card.classList.add("dragging");});
    card.addEventListener("dragend",()=>card.classList.remove("dragging"));
  });
  document.querySelectorAll("[data-index-drop]").forEach(zone=>{
    zone.addEventListener("dragover",event=>{event.preventDefault();event.dataTransfer.dropEffect="move";zone.classList.add("drag-over");});
    zone.addEventListener("dragleave",event=>{if(!zone.contains(event.relatedTarget))zone.classList.remove("drag-over");});
    zone.addEventListener("drop",event=>{event.preventDefault();zone.classList.remove("drag-over");const id=event.dataTransfer.getData("text/plain");if(id)updateIndexDraft(id,zone.dataset.indexDrop==="members");});
  });
  $("#index-security-search")?.addEventListener("input",event=>{const query=event.currentTarget.value.trim().toLowerCase();document.querySelectorAll('[data-index-drop="pool"] [data-index-security]').forEach(card=>card.hidden=query&&!String(card.dataset.search||"").includes(query));});
  $("#index-create-form")?.addEventListener("submit",async event=>{
    event.preventDefault();const raw=formBody(event.currentTarget),body={...raw,target_size:Number(raw.target_size),base_nav:Number(raw.base_nav),management_fee_percent:Number(raw.management_fee_percent||0),auto_populate:event.currentTarget.elements.auto_populate.checked};
    if(!confirm(`CREATE ${String(raw.ticker||"").toUpperCase()} INDEX?\n\nThe fund will be registered as a live FCX security${body.auto_populate?" and populated with the largest eligible companies by current market cap":""}. This action will be audit logged.`))return;
    try{const created=await request("/admin/indexes",{method:"POST",body});state.indexDesk={fundId:String(created.created_index_id||""),draftFundId:"",draft:[],dirty:false};state.cache.clear();notice("New FCX index created.");await openSection("indexes",true);}catch(error){notice(error.message,true);}
  });
  $("#save-index-composition")?.addEventListener("click",async()=>{
    const id=state.indexDesk.fundId,reason=$("#index-change-reason")?.value.trim()||"Index composition updated by authorized FEC operator";
    if(!id)return;
    if(!confirm(`SAVE INDEX COMPOSITION?\n\nThis will replace the live basket with ${state.indexDesk.draft.length} selected constituent(s), recalculate market-cap weights, preserve the current fund NAV, and create an audit record.`))return;
    try{await request(`/admin/indexes/${encodeURIComponent(id)}/members`,{method:"PUT",body:{security_ids:state.indexDesk.draft.map(Number),reason}});state.indexDesk.dirty=false;state.cache.clear();notice("Index composition saved and reweighted.");await openSection("indexes",true);}catch(error){notice(error.message,true);}
  });
  const restructureIndexes=async(fundIds,label)=>{
    if(!confirm(`RESTRUCTURE ${label} BY MARKET CAP?\n\nEach selected index's current basket will be replaced with the highest-market-cap eligible companies up to its target size. Current fund NAVs will be preserved and every change will be audit logged.`))return;
    try{await request("/admin/indexes/rebalance",{method:"POST",body:{fund_ids:fundIds,reason:"Automated market-cap restructure approved by FEC operator"}});state.indexDesk.dirty=false;state.cache.clear();notice(`${label} restructured from current market capitalization.`);await openSection("indexes",true);}catch(error){notice(error.message,true);}
  };
  $("#rebalance-selected-index")?.addEventListener("click",()=>restructureIndexes([Number(state.indexDesk.fundId)],"selected index"));
  $("#rebalance-all-indexes")?.addEventListener("click",()=>restructureIndexes([],"all indexes"));
  const movementModal=$("#movement-modal");
  $("#open-movement-modal")?.addEventListener("click",()=>movementModal?.showModal());
  movementModal?.querySelectorAll(".modal-close").forEach(button=>button.addEventListener("click",()=>movementModal.close()));
  document.querySelectorAll(".emergency-command").forEach(button=>button.addEventListener("click",async()=>{const command=button.dataset.command;if(!confirm(`${command==="pause"?"HALT":"RESUME"} FCX ENGINE?\n\nThis changes autonomous market execution immediately and will be audit logged.`))return;await mutation(`/admin/engine/${command}`,"POST",undefined,`FCX engine ${command}d.`,"overview");}));
  document.querySelectorAll(".market-state").forEach(button=>button.addEventListener("click",async()=>{
    const value=button.dataset.value;
    const enteringMaintenance=button.dataset.field==="maintenance_mode"&&value==="true";
    const dangerous=value==="closed"||(value==="false"&&button.dataset.field!=="maintenance_mode")||enteringMaintenance;
    const warning=button.dataset.confirm||`CONFIRM MARKET CONTROL\n\nThis will change ${button.textContent.trim().toLowerCase()} immediately for connected communities and will be audit logged.`;
    if(dangerous&&!confirm(warning))return;
    const parsed=value==="true"?true:value==="false"?false:value;
    await mutation("/admin/market/settings","PATCH",{[button.dataset.field]:parsed},"Market control updated.",button.dataset.refreshSection||"overview");
  }));
  const movementSubmit=(selector,target)=>$(selector)?.addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget,raw=formBody(form),checks=Array.from(form.querySelectorAll(".security-checkbox:checked"));const select=form.elements.security_ids;const security_ids=checks.length?checks.map(c=>Number(c.value)):select?(select.multiple?Array.from(select.selectedOptions).map(o=>Number(o.value)):[Number(select.value)]):[];let movement=Number(raw.percent_change);if(selector==="#quick-movement-form"&&$("#movement-direction")?.value==="down")movement=-Math.abs(movement);if(!security_ids.length)return notice("Select at least one eligible security.",true);if(!confirm(`EXECUTE STOCK MOVEMENT?\n\n${security_ids.length} security(s) will move ${movement}% over ${raw.duration_minutes} minutes. The final market prices remain in effect and this action will be audit logged.`))return;movementModal?.close();await mutation("/admin/operations/programs","POST",{security_ids,event_name:raw.event_name,percent_change:movement,duration_minutes:Number(raw.duration_minutes),starts_at:null},"Audited stock movement initiated.",target);});
  movementSubmit("#quick-movement-form","overview"); movementSubmit("#engine-movement-form","engine");
  const engineChecks=Array.from(document.querySelectorAll("#engine-movement-form .security-checkbox")); const updateSelected=()=>{if($("#engine-selected-count"))$("#engine-selected-count").textContent=`${engineChecks.filter(c=>c.checked).length} selected`;}; engineChecks.forEach(c=>c.addEventListener("change",updateSelected)); $("#select-all-engine")?.addEventListener("click",()=>{engineChecks.forEach(c=>c.checked=true);updateSelected();}); $("#clear-all-engine")?.addEventListener("click",()=>{engineChecks.forEach(c=>c.checked=false);updateSelected();}); updateSelected();
  $("#position-filter-form")?.addEventListener("submit",event=>{event.preventDefault();state.positionFilters=formBody(event.currentTarget);openSection("positions",true);});
  $("#clear-position-filters")?.addEventListener("click",()=>{state.positionFilters={};openSection("positions",true);});
  $("#account-directory-search")?.addEventListener("input",event=>{const query=event.currentTarget.value.trim().toLowerCase();document.querySelectorAll("details.account-card").forEach(card=>card.hidden=query&&!String(card.dataset.search||"").includes(query));});
  document.querySelectorAll("details.account-card").forEach(card=>card.addEventListener("toggle",async()=>{if(!card.open||card.dataset.loaded)return;const target=card.querySelector(".account-detail");try{const d=await request(`/admin/accounts/${encodeURIComponent(card.dataset.account)}/profile`),a=d.account,s=d.stats||{};target.classList.remove("empty");target.innerHTML=`${metrics([["Portfolio",money(Number(a.cash_balance||0)+(d.holdings||[]).reduce((x,h)=>x+Number(h.current_value||0),0)),"Cash + holdings"],["Cash",money(a.cash_balance),a.trading_status],["Buys",number(s.total_buys),money(s.buy_volume)],["Sells",number(s.total_sells),money(s.sell_volume)],["Realized P/L",pnl(s.realized_pnl),`${number(s.closed_positions)} closed`,"",true],["Unrealized P/L",pnl((d.holdings||[]).reduce((x,h)=>x+Number(h.unrealized_pnl||0),0)+(d.active_leverage||[]).reduce((x,p)=>x+Number(p.unrealized_pnl||0),0)),`${number(s.open_positions)} open`,"",true]])}<h4>ACTIVE LEVERAGE</h4>${positionsTable((d.active_leverage||[]).map(p=>({...p,display_name:a.display_name,account_id:a.account_id,communities:a.communities,current_value:Number(p.quantity||0)*Number(p.current_price||0)})))}${dataTable(["Time","Symbol","Type","Quantity","Price","Value"],d.recent_activity||[],r=>`<tr><td>${esc(stamp(r.created_at))}</td><td>${esc(r.ticker)}</td><td>${esc(r.side)}</td><td>${number(r.quantity)}</td><td>${money(r.unit_price)}</td><td>${money(r.gross_amount)}</td></tr>`)}`;card.dataset.loaded="1";}catch(e){target.textContent=e.message;}}));
  $("#control-account-form")?.addEventListener("submit",async event=>{event.preventDefault();const body=formBody(event.currentTarget),label=event.currentTarget.elements.role.selectedOptions[0]?.textContent||"control account";if(!confirm(`CREATE ${label.toUpperCase()}?\n\nThis account will receive the server-enforced access shown for the selected role. The action will be audit logged.`))return;await mutation("/admin/fec/investigators","POST",body,"FCX control account created.","onboarding");});
  const runInvestigation=async(page=1)=>{const form=$("#investigation-account-form"),target=$("#investigation-account-result");if(!form||!target)return;const raw=formBody(form);if(!raw.account_id)return;state.investigation={accountId:raw.account_id,period:raw.period||"day",search:raw.search||"",page};target.innerHTML=`<div class="empty">Loading authoritative account history…</div>`;try{const id=encodeURIComponent(raw.account_id),query=new URLSearchParams({page:String(page),page_size:"50",search:raw.search||"",sort:"desc"});const [h,a]=await Promise.all([request(`/admin/investigations/accounts/${id}/history?${query}`),request(`/admin/investigations/accounts/${id}/analytics?period=${raw.period||"day"}`)]),x=a.analytics||{},p=h.pagination||{};target.innerHTML=`<article class="panel wide"><div class="panel-heading"><div><p class="eyebrow">ACCOUNT IDENTITY</p><h4>${esc(h.account.display_name||h.account.account_id)}</h4><p>${esc(h.account.account_id)} · ${esc(h.account.communities||"Unlinked")} · ${esc(h.account.status)}</p></div><span class="status-chip">${esc(h.account.status||"Active")}</span></div>${metrics([["Cash balance",money(h.account.cash_balance),"FCX ledger"],["Realized P/L",pnl(x.realized_pnl),String(raw.period).toUpperCase(),"",true],["Unrealized P/L",pnl(x.unrealized_pnl),"Open exposure","",true],["Combined P/L",pnl(x.combined_pnl),"Period result","",true],["Buy volume",money(x.buy_volume),`${number(x.buys)} buys`],["Sell volume",money(x.sell_volume),`${number(x.sells)} sells`],["Trades",number(x.trades),`${number(x.winning_trades)} wins / ${number(x.losing_trades)} losses`]])}</article>${sectionHead("ACTIVE LEVERAGE","Current account exposure","Open leveraged positions are shown immediately beneath the linked Ravenhood identity.")}${positionsTable((h.active_leverage||[]).map(position=>({...position,display_name:h.account.display_name,account_id:h.account.account_id,communities:h.account.communities})))}${sectionHead("TRANSACTION HISTORY","Complete available trading ledger",`${number(p.total)} records · server-filtered and paginated`)}${dataTable(["Timestamp","Symbol","Transaction","Direction","Quantity","Execution price","Value","Leverage","Realized P/L"],h.history||[],r=>`<tr><td>${esc(stamp(r.occurred_at))}</td><td><strong>${esc(r.ticker)}</strong></td><td>${esc(r.transaction_type)}</td><td>${esc(r.direction)}</td><td>${number(r.quantity)}</td><td>${money(r.price)}</td><td>${money(r.value)}</td><td>${r.leverage?`${number(r.leverage)}x`:"—"}</td><td>${r.realized_pnl==null?"—":pnl(r.realized_pnl)}</td></tr>`)}<div class="button-row"><button id="investigation-prev" ${page<=1?"disabled":""}>Previous</button><span class="muted">Page ${number(p.page)} of ${number(p.pages)}</span><button id="investigation-next" ${page>=Number(p.pages||1)?"disabled":""}>Next</button></div>`;$("#investigation-prev")?.addEventListener("click",()=>runInvestigation(page-1));$("#investigation-next")?.addEventListener("click",()=>runInvestigation(page+1));}catch(error){target.innerHTML=`<div class="empty">${esc(error.message)}</div>`;}};
  $("#investigation-account-form")?.addEventListener("submit",event=>{event.preventDefault();event.stopImmediatePropagation();runInvestigation(1);});
  $("#load-advanced-investigations")?.addEventListener("click",async()=>{const target=$("#advanced-investigation-tools");target.innerHTML=`<div class="empty">Loading advanced regulatory workspace…</div>`;try{target.innerHTML=await renderInvestigations();bindActions();}catch(error){target.innerHTML=`<div class="empty">${esc(error.message)}</div>`;}});
  $("#investigation-account-form")?.addEventListener("submit",async event=>{event.preventDefault();const raw=formBody(event.currentTarget),id=encodeURIComponent(raw.account_id),target=$("#investigation-account-result");target.innerHTML=`<div class="empty">Loading complete FCX history…</div>`;try{const [h,a]=await Promise.all([request(`/admin/investigations/accounts/${id}/history?page=1&page_size=50&search=${encodeURIComponent(raw.search||"")}`),request(`/admin/investigations/accounts/${id}/analytics?period=${raw.period}`)]),x=a.analytics||{};target.innerHTML=`${metrics([["Realized P/L",pnl(x.realized_pnl),raw.period,"",true],["Unrealized P/L",pnl(x.unrealized_pnl),"Open positions","",true],["Combined P/L",pnl(x.combined_pnl),"Total","",true],["Buy volume",money(x.buy_volume),`${number(x.buys)} buys`],["Sell volume",money(x.sell_volume),`${number(x.sells)} sells`],["Trades",number(x.trades),`${number(x.winning_trades)} wins / ${number(x.losing_trades)} losses`]])}<article class="panel"><h4>${esc(h.account.display_name)} · ${esc(h.account.account_id)}</h4><p>${esc(h.account.communities||"Unlinked")} · ${esc(h.account.status)}</p><h4>ACTIVE LEVERAGE</h4>${positionsTable((h.active_leverage||[]).map(p=>({...p,display_name:h.account.display_name,account_id:h.account.account_id,communities:h.account.communities,current_value:Number(p.quantity||0)*Number(p.current_price||0)})))}</article>${dataTable(["Timestamp","Symbol","Transaction","Direction","Quantity","Price","Value","Leverage","Realized P/L"],h.history||[],r=>`<tr><td>${esc(stamp(r.occurred_at))}</td><td>${esc(r.ticker)}</td><td>${esc(r.transaction_type)}</td><td>${esc(r.direction)}</td><td>${number(r.quantity)}</td><td>${money(r.price)}</td><td>${money(r.value)}</td><td>${r.leverage?`${number(r.leverage)}x`:"—"}</td><td>${r.realized_pnl==null?"—":pnl(r.realized_pnl)}</td></tr>`)}<p class="muted">Page ${h.pagination.page} of ${h.pagination.pages} · ${number(h.pagination.total)} records</p>`;}catch(e){target.innerHTML=`<div class="empty">${esc(e.message)}</div>`;}});
  document.querySelectorAll(".control-account-access").forEach(b=>b.addEventListener("click",async()=>{const active=b.dataset.active==="1",reason=prompt(`Reason to ${active?"restore":"revoke"} access:`)||"Authorized control-account action";if(!active&&!confirm("Revoke this account's authentication and all active sessions?"))return;await mutation(`/admin/fec/investigators/${b.dataset.id}`,"PATCH",{active,reason},"Control-account access updated.","onboarding");}));
  document.querySelectorAll(".control-account-delete").forEach(b=>b.addEventListener("click",async()=>{if(!confirm("DELETE CONTROL ACCOUNT?\n\nLogin access will be permanently removed. Historical audit actions will be retained."))return;await mutation(`/admin/fec/investigators/${b.dataset.id}`,"DELETE",undefined,"Control account soft-deleted; audit history retained.","onboarding");}));
  const promotionReward = $("#promotion-reward");
  const syncPromotionFields = () => { const reward = promotionReward?.value; if ($('[data-promotion-cash]')) $('[data-promotion-cash]').hidden = reward !== "cash"; if ($('[data-promotion-stock]')) $('[data-promotion-stock]').hidden = reward !== "stock"; if ($('[data-promotion-shares]')) $('[data-promotion-shares]').hidden = reward === "cash"; if ($('[data-promotion-bundle]')) $('[data-promotion-bundle]').hidden = reward !== "random_bundle"; };
  promotionReward?.addEventListener("change", syncPromotionFields); syncPromotionFields();
  $("#promotion-form")?.addEventListener("submit", async event => { event.preventDefault(); const raw=formBody(event.currentTarget); const body={...raw,cash_amount:Number(raw.cash_amount||0),security_id:optionalNumber(raw.security_id),share_quantity:Number(raw.share_quantity||0),bundle_size:Number(raw.bundle_size||0),max_redemptions:Number(raw.max_redemptions),expiry_days:Number(raw.expiry_days)}; try { const issued=await request("/admin/fec/promotions",{method:"POST",body}); state.cache.clear(); $("#promotion-secret").innerHTML=`<div class="secret"><strong>Copy this code now</strong><br>${esc(issued.code)}</div>`; notice("FEC promotional campaign issued."); } catch(error){notice(error.message,true);} });
  document.querySelectorAll(".promotion-status").forEach(button=>button.addEventListener("click",()=>mutation(`/admin/fec/promotions/${button.dataset.id}`,"PATCH",{active:button.dataset.active==="1"},"Campaign status updated.","promotions")));
  document.querySelectorAll(".promotion-delete").forEach(button=>button.addEventListener("click",()=>{if(confirm("Delete this unused promotional campaign?"))mutation(`/admin/fec/promotions/${button.dataset.id}`,"DELETE",undefined,"Campaign deleted.","promotions");}));
  const applySettlementFilter = filter => {
    state.settlementFilter = filter || "all";
    document.querySelectorAll(".settlement-filter").forEach(button => button.classList.toggle("active", button.dataset.filter === state.settlementFilter));
    document.querySelectorAll("[data-settlement-row]").forEach(row => {
      row.hidden = state.settlementFilter !== "all" && row.dataset.lifecycle !== state.settlementFilter;
    });
  };
  document.querySelectorAll(".settlement-filter").forEach(button => button.addEventListener("click", () => applySettlementFilter(button.dataset.filter)));
  applySettlementFilter(state.settlementFilter);
  $("#settlement-refresh-all")?.addEventListener("click", async () => {
    state.cache.delete("admin-settlements");
    await openSection("banking", true);
  });
  $("#settlement-bulk-cancel")?.addEventListener("click", async () => {
    if (!confirm("Cancel every recoverable pending, authorized, or failed FCX wallet transfer? Claimed bank movements will not be cancelled.")) return;
    await mutation("/admin/settlements/bulk-cancel", "POST", { reason: "Bulk-cancelled by authorized FEC operator" }, "Recoverable wallet transfers cancelled.", "banking");
  });
  document.querySelectorAll(".settlement-refresh").forEach(button => button.addEventListener("click", () => mutation(`/admin/settlements/${encodeURIComponent(button.dataset.id)}/refresh`, "POST", {}, "Settlement refreshed from its CAD transport.", "banking")));
  document.querySelectorAll(".settlement-retry").forEach(button => button.addEventListener("click", () => mutation(`/admin/settlements/${encodeURIComponent(button.dataset.id)}/retry`, "POST", {}, "Settlement made available for Bank Bridge retry.", "banking")));
  document.querySelectorAll(".settlement-cancel").forEach(button => button.addEventListener("click", async () => {
    if (!confirm("Cancel this recoverable FCX wallet transfer?")) return;
    await mutation(`/admin/settlements/${encodeURIComponent(button.dataset.id)}/cancel`, "POST", { reason: "Cancelled by authorized FEC operator" }, "Settlement cancelled and any reserved FCX funds restored.", "banking");
  }));
  $("#community-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { await request("/admin/communities", { method: "POST", body: Object.fromEntries(form) }); state.cache.clear(); notice("Community registered. Generate its API credential next."); await openSection("communities", true); } catch (error) { notice(error.message, true); }
  });
  document.querySelectorAll(".community-toggle").forEach(button => button.addEventListener("click", async () => {
    try { await request(`/admin/communities/${encodeURIComponent(button.dataset.community)}`, { method: "PATCH", body: { [button.dataset.field]: button.dataset.value === "1" } }); state.cache.clear(); await openSection("communities", true); } catch (error) { notice(error.message, true); }
  }));
  $("#credential-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = new FormData(event.currentTarget); const community = form.get("community_id"); const scopes = String(form.get("scopes") || "").split(",").map(item => item.trim()).filter(Boolean);
    try { const data = await request(`/admin/communities/${encodeURIComponent(community)}/credentials`, { method: "POST", body: { scopes } }); $("#credential-secret").innerHTML = `<div class="secret"><strong>Copy this once</strong><br>${esc(data.credential)}</div>`; state.cache.delete("credentials"); } catch (error) { notice(error.message, true); }
  });
  document.querySelectorAll(".revoke-credential").forEach(button => button.addEventListener("click", async () => {
    if (!confirm(`Revoke ${button.dataset.id}? The assigned CAD will immediately lose FCX access.`)) return;
    try { await request(`/admin/credentials/${encodeURIComponent(button.dataset.id)}/revoke`, { method: "POST" }); state.cache.clear(); await openSection("connections", true); } catch (error) { notice(error.message, true); }
  }));
  $("#case-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const body = Object.fromEntries(new FormData(event.currentTarget));
    try { const data = await request("/admin/investigations", { method: "POST", body }); state.cache.clear(); notice(`Investigation ${data.case_id} opened.`); await openSection("investigations", true); } catch (error) { notice(error.message, true); }
  });
  $("#leverage-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = new FormData(event.currentTarget); const body = { leverage_enabled: form.get("leverage_enabled") === "true", max_leverage: Number(form.get("max_leverage")), margin_enabled: form.get("margin_enabled") === "true", margin_maintenance_percent: Number(form.get("margin_maintenance_percent")), margin_max_open_positions: Number(form.get("margin_max_open_positions")), margin_max_account_notional: Number(form.get("margin_max_account_notional")) };
    try { await request("/admin/market/settings", { method: "PATCH", body }); state.cache.clear(); notice("Leverage controls saved."); await openSection("leverage", true); } catch (error) { notice(error.message, true); }
  });
  document.querySelectorAll(".market-toggle").forEach(button => button.addEventListener("click", async () => {
    try { await request("/admin/market/settings", { method: "PATCH", body: { [button.dataset.field]: button.dataset.value === "1" } }); state.cache.clear(); await openSection("settings", true); } catch (error) { notice(error.message, true); }
  }));
  $("#market-session-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = new FormData(event.currentTarget); const body = { manual_override: form.get("manual_override"), schedule_timezone: form.get("schedule_timezone"), schedule_open_time: form.get("schedule_open_time"), schedule_close_time: form.get("schedule_close_time"), weekends_enabled: form.get("weekends_enabled") === "true", fcxv_24h_enabled: form.get("fcxv_24h_enabled") === "true" };
    await mutation("/admin/market/settings", "PATCH", body, "Exchange session controls saved.", "settings");
  });
  $("#market-fees-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = new FormData(event.currentTarget); const body = { trade_fee_percent: Number(form.get("trade_fee_percent")), transfer_fee_percent: Number(form.get("transfer_fee_percent")) };
    await mutation("/admin/market/settings", "PATCH", body, "FCX fee schedule saved.", "settings");
  });
  $("#hunt-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = new FormData(event.currentTarget); const body = { liquidation_hunts_enabled: form.get("liquidation_hunts_enabled") === "true", liquidation_hunt_threshold: Number(form.get("liquidation_hunt_threshold")), liquidation_hunt_probability_percent: Number(form.get("liquidation_hunt_probability_percent")), liquidation_hunt_intensity: form.get("liquidation_hunt_intensity"), liquidation_hunt_max_move_percent: Number(form.get("liquidation_hunt_max_move_percent")), liquidation_hunt_cooldown_minutes: Number(form.get("liquidation_hunt_cooldown_minutes")), liquidation_hunt_market_hours_only: form.get("liquidation_hunt_market_hours_only") === "true" };
    await mutation("/admin/market/settings", "PATCH", body, "Controlled-liquidity policy saved.", "leverage");
  });

  document.querySelectorAll(".engine-command").forEach(button => button.addEventListener("click", async () => {
    const command = button.dataset.command;
    if (command === "cycle") return mutation("/admin/engine/cycle", "POST", { cycle: button.dataset.cycle }, `${button.textContent.trim()} completed.`, "engine");
    if (command === "kill") return mutation("/admin/engine/kill-switch", "POST", { active: button.dataset.active === "true" }, "Engine kill switch updated.", "engine");
    return mutation(`/admin/engine/${command}`, "POST", undefined, `Engine ${command} command completed.`, "engine");
  }));
  $("#engine-settings-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const raw = formBody(event.currentTarget);
    const booleanKeys = new Set(["enabled", "kill_switch", "events_enabled", "halts_enabled", "bankruptcy_enabled", "delisting_enabled", "short_selling_enabled", "ipo_uncertainty_enabled"]);
    const structuredKeys = new Set(["intervals_json", "distribution_json", "paused_personalities_csv", "paused_tickers_csv"]);
    const body = {};
    Object.entries(raw).forEach(([key, value]) => {
      if (value === "" || structuredKeys.has(key)) return;
      body[key] = booleanKeys.has(key) ? value === "true" : key === "speed" ? value : Number(value);
    });
    try {
      body.intervals = JSON.parse(raw.intervals_json || "{}");
      body.distribution = JSON.parse(raw.distribution_json || "{}");
    } catch (_error) {
      notice("Cycle intervals and investor distribution must be valid JSON.", true);
      return;
    }
    body.paused_personalities = String(raw.paused_personalities_csv || "").split(",").map(value => value.trim()).filter(Boolean);
    body.paused_tickers = String(raw.paused_tickers_csv || "").split(",").map(value => value.trim().toUpperCase()).filter(Boolean);
    await mutation("/admin/engine/settings", "PATCH", body, "Autonomous engine settings saved.", "engine");
  });
  $("#engine-seed-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    await mutation("/admin/engine/seed", "POST", { replace: raw.replace === "true", confirmation: raw.confirmation || "" }, "Engine investor population reconciled.", "engine");
  });
  $("#engine-sandbox-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    await mutation("/admin/engine/sandbox", "POST", { days: Number(raw.days), seed: optionalNumber(raw.seed) }, "Sandbox simulation completed.", "engine");
  });
  $("#ticker-control-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget); const action = event.submitter?.value || "pause";
    await mutation(`/admin/engine/ticker/${encodeURIComponent(raw.ticker)}/${action}`, "POST", undefined, `${raw.ticker} ${action} command completed.`, "engine");
  });
  $("#split-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    await mutation("/admin/engine/corporate-actions/split", "POST", { ticker: raw.ticker, numerator: Number(raw.numerator), denominator: Number(raw.denominator), rationale: raw.rationale, confirmation: raw.confirmation }, "Stock split applied.", "engine");
  });
  $("#dividend-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    await mutation("/admin/engine/corporate-actions/dividend", "POST", { ticker: raw.ticker, amount_per_share: Number(raw.amount_per_share), rationale: raw.rationale, confirmation: raw.confirmation }, "Dividend declared.", "engine");
  });

  const programForm = $("#price-program-form");
  const updateProgramPreview = () => {
    if (!programForm) return;
    const operations = state.cache.get("operations");
    Promise.resolve(operations).then(data => {
      const ids = Array.from(programForm.elements.security_ids.selectedOptions || []).map(option => Number(option.value));
      const change = Number(programForm.elements.percent_change.value || 0);
      const rows = (data?.securities || []).filter(row => ids.includes(Number(row.id)));
      $("#program-preview").innerHTML = rows.length ? rows.map(row => `<span><strong>${esc(row.ticker)}</strong> ${money(row.price)} &rarr; ${money(Number(row.price) * (1 + change / 100))}</span>`).join("") : "Select securities to preview current and target prices.";
    });
  };
  programForm?.elements.security_ids?.addEventListener("change", updateProgramPreview);
  programForm?.elements.percent_change?.addEventListener("input", updateProgramPreview);
  programForm?.addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget; const raw = formBody(form);
    const security_ids = Array.from(form.elements.security_ids.selectedOptions).map(option => Number(option.value));
    await mutation("/admin/operations/programs", "POST", { security_ids, event_name: raw.event_name, percent_change: Number(raw.percent_change), duration_minutes: Number(raw.duration_minutes), starts_at: isoFromLocal(raw.starts_at) }, `${security_ids.length} price program${security_ids.length === 1 ? "" : "s"} scheduled.`, "operations");
  });
  $("#halt-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget; const raw = formBody(form);
    const security_ids = Array.from(form.elements.security_ids.selectedOptions).map(option => Number(option.value));
    await mutation("/admin/operations/halts", "POST", { security_ids, reason_code: raw.reason_code, reason_label: raw.reason_label, public_notice: raw.public_notice, case_reference: raw.case_reference || "", automatic_resume_at: isoFromLocal(raw.automatic_resume_at) }, `${security_ids.length} security halt${security_ids.length === 1 ? "" : "s"} placed.`, "operations");
  });
  $("#delist-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    await mutation("/admin/operations/delistings", "POST", { ...raw, security_id: Number(raw.security_id) }, "Security removed from public FCX trading.", "operations");
  });
  $("#resume-all-halts")?.addEventListener("click", () => mutation("/admin/operations/halts/resume-all", "POST", { note: "Released by authorized FEC operator" }, "All active trading halts released.", "operations"));
  document.querySelectorAll(".stop-program").forEach(button => button.addEventListener("click", () => mutation(`/admin/operations/programs/${button.dataset.id}/stop`, "POST", { keep_current_price: true }, "Program stopped at its current market price.", "operations")));
  document.querySelectorAll(".resume-halt").forEach(button => button.addEventListener("click", () => mutation(`/admin/operations/halts/${button.dataset.id}/resume`, "POST", { note: "Released by authorized FEC operator" }, "Security trading resumed.", "operations")));
  document.querySelectorAll(".relist-security").forEach(button => button.addEventListener("click", () => mutation(`/admin/operations/delistings/${button.dataset.id}/relist`, "POST", { note: "Relisted by authorized FEC operator" }, "Security relisted on FCX.", "operations")));

  document.querySelectorAll(".save-security-margin").forEach(button => button.addEventListener("click", () => {
    const id = button.dataset.id;
    const enabled = document.querySelector(`.security-margin-enabled[data-id="${id}"]`)?.value === "true";
    const max = Number(document.querySelector(`.security-margin-max[data-id="${id}"]`)?.value || 1);
    return mutation(`/admin/leverage/securities/${id}`, "PATCH", { margin_enabled: enabled, margin_max_leverage: max }, "Security leverage settings saved.", "leverage");
  }));

  $("#account-restriction-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    await mutation("/admin/fec/restrictions", "POST", { account_id: Number(raw.account_id), scope: raw.scope, reason: raw.reason, case_reference: raw.case_reference || "" }, "FEC trading restriction recorded.", "investigations");
  });
  document.querySelectorAll(".release-restriction").forEach(button => button.addEventListener("click", () => mutation(`/admin/fec/restrictions/${button.dataset.id}/release`, "POST", { note: "Released by authorized FEC investigator" }, "Account trading restriction released.", "investigations")));
  $("#fec-seizure-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    await mutation("/admin/fec/custody/seize", "POST", { account_id: Number(raw.account_id), amount: Number(raw.amount), case_reference: raw.case_reference, reason: raw.reason, authorization: raw.authorization }, "Resident FCX cash placed in FEC custody.", "investigations");
  });
  $("#fec-disposition-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    await mutation("/admin/fec/custody/dispositions", "POST", { disposition: raw.disposition, amount: Number(raw.amount), target_account_id: raw.target_account_id ? Number(raw.target_account_id) : null, case_reference: raw.case_reference, reason: raw.reason, authorization: raw.authorization }, "FEC custody disposition completed.", "investigations");
  });
  document.querySelectorAll(".ipo-decision").forEach(button => button.addEventListener("click", async () => {
    const note = prompt(`Enter the FEC review note for this ${button.dataset.decision} decision:`);
    if (!note) return;
    await mutation(`/admin/fec/ipos/${button.dataset.id}/decision`, "POST", { decision: button.dataset.decision, note }, "IPO review decision recorded.", "investigations");
  }));
  $("#fec-equity-reset-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    if (!confirm("This permanently removes all resident FCX cash. Continue?")) return;
    await mutation("/admin/fec/resets/equity", "POST", raw, "All resident FCX equity cash was reset.", "investigations");
  });
  $("#fec-share-reset-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const raw = formBody(event.currentTarget);
    if (!confirm("This permanently removes all resident FCX holdings. Continue?")) return;
    await mutation("/admin/fec/resets/shares", "POST", raw, "All resident FCX shares were reset.", "investigations");
  });
}

function showLogin() { $("#login").hidden = false; $("#workspace").hidden = true; state.user = null; state.csrf = ""; }
function showWorkspace() { $("#login").hidden = true; $("#workspace").hidden = false; $("#operator-name").textContent = `${state.user.display_name} · ${(state.user.roles || []).join(", ")}`; renderNav(); openSection(state.section, true); }

function showProfessionalWorkspace() {
  $("#login").hidden = true; $("#workspace").hidden = false;
  const name = state.user.display_name || state.user.email || "FEC operator";
  const roles = (state.user.roles || []).join(", ");
  $("#operator-name").textContent = name;
  $("#rail-operator-name").textContent = name;
  $("#rail-operator-role").textContent = roles || "Authorized access";
  $("#operator-avatar").textContent = name.split(/\s+/).map(part => part[0]).join("").slice(0,2).toUpperCase() || "FE";
  renderNav(); openSection(state.section, true);
}

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault(); $("#login-error").textContent = "";
  try {
    const data = await request("/auth/login", { method: "POST", body: { email: $("#email").value, password: $("#password").value } });
    state.user = data.user; state.csrf = data.csrf_token; showProfessionalWorkspace();
  } catch (error) { $("#login-error").textContent = error.message; }
});

$("#logout").addEventListener("click", async () => { try { await request("/auth/logout", { method: "POST" }); } finally { showLogin(); } });
$("#refresh").addEventListener("click", () => openSection(state.section, true));
$("#mobile-menu").addEventListener("click", () => { $("#workspace").classList.toggle("nav-open"); $("#mobile-scrim").hidden = !$("#workspace").classList.contains("nav-open"); });
$("#mobile-scrim").addEventListener("click", () => { $("#workspace").classList.remove("nav-open"); $("#mobile-scrim").hidden = true; });

(async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  try { const data = await request("/auth/session"); state.user = data.user; state.csrf = data.csrf_token; showProfessionalWorkspace(); } catch { showLogin(); }
})();
