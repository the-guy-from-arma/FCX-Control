const API = "/api/v1";
const state = { user: null, csrf: "", section: "overview", cache: new Map() };
const sections = [
  ["overview", "Overview"], ["market", "Market"], ["companies", "Companies"],
  ["securities", "Securities"], ["orders", "Orders"], ["trades", "Trades"],
  ["accounts", "Ravenhood Accounts"], ["investigations", "FEC Investigations"],
  ["communities", "Communities"], ["connections", "API Connections"],
  ["leverage", "Leverage"], ["settings", "Market Settings"],
  ["audit", "Audit Log"], ["health", "System Health"],
];

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
  $("#nav").innerHTML = sections.map(([key, label]) => `<button class="nav-button ${key === state.section ? "active" : ""}" data-section="${key}">${label}</button>`).join("");
  $("#nav").querySelectorAll("button").forEach(button => button.addEventListener("click", () => openSection(button.dataset.section)));
}

async function openSection(section, force = false) {
  state.section = section;
  renderNav();
  const label = sections.find(item => item[0] === section)?.[1] || "Control";
  $("#section-title").textContent = label;
  $("#section-kicker").textContent = section === "investigations" ? "FEDERAL EXCHANGE COMMISSION" : "RAVENHOOD EXCHANGE OPERATIONS";
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
  return `<div class="metric-strip">${items.map(item => `<div class="metric"><small>${esc(item[0])}</small><strong class="${item[3] || ""}">${esc(item[1])}</strong><em>${esc(item[2] || "")}</em></div>`).join("")}</div>`;
}

function sectionHead(kicker, title, description = "") {
  return `<div class="section-head"><div><p class="eyebrow">${esc(kicker)}</p><h3>${esc(title)}</h3><p>${esc(description)}</p></div></div>`;
}

function bars(values) {
  const nums = values.map(Number); const max = Math.max(1, ...nums);
  return `<div class="mini-chart">${nums.map((value, index) => `<i style="height:${Math.max(6, value / max * 100)}%;opacity:${.4 + index / Math.max(1, nums.length) * .6}"></i>`).join("")}</div>`;
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

async function renderInvestigations() {
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

async function renderLeverage() {
  const data = await marketData(); const settings = Object.fromEntries(data.settings.map(row => [row.setting_key, row.setting_value]));
  return `${sectionHead("RISK ENGINE", "Leverage administration", "Central limits apply consistently to every connected CAD.")}<div class="grid"><article class="panel"><h4>Current risk posture</h4><p>Leverage is ${yes(settings.leverage_enabled) ? "enabled" : "disabled"}; the configured ceiling is ${esc(settings.max_leverage || "not set")}x.</p>${bars([Number(settings.max_leverage || 1), 10, 25, 50, 100])}</article><article class="panel"><h4>Change leverage controls</h4><form id="leverage-form" class="form-grid"><label>Enabled<select name="leverage_enabled"><option value="true" ${yes(settings.leverage_enabled) ? "selected" : ""}>Enabled</option><option value="false" ${!yes(settings.leverage_enabled) ? "selected" : ""}>Disabled</option></select></label><label>Maximum leverage<input type="number" name="max_leverage" min="1" max="200" step="1" value="${esc(settings.max_leverage || 5)}"></label><button class="primary full" type="submit">Save risk controls</button></form></article></div>`;
}

async function renderSettings() {
  const data = await marketData(); const settings = Object.fromEntries(data.settings.map(row => [row.setting_key, row.setting_value]));
  const controls = [["market_open", "Market open"], ["buy_enabled", "Buying enabled"], ["sell_enabled", "Selling enabled"], ["account_creation_enabled", "Account creation"], ["maintenance_mode", "Maintenance mode"]];
  return `${sectionHead("GLOBAL CONTROLS", "Market settings", "These controls belong only to FCX Control and are never stored in a CAD.")}<div class="grid">${controls.map(([key, label]) => `<article class="panel third"><h4>${label}</h4><p>Current state: ${yes(settings[key]) ? "enabled" : "disabled"}</p><button class="market-toggle ${yes(settings[key]) ? "on" : "off"}" data-field="${key}" data-value="${yes(settings[key]) ? "0" : "1"}">${yes(settings[key]) ? "Disable" : "Enable"}</button></article>`).join("")}</div>`;
}

async function renderAudit() {
  const data = await cached("audit", "/admin/audit?limit=500");
  return `${sectionHead("IMMUTABLE OPERATIONS", "Privileged action ledger", "Credential, community, market, investigation, and settlement interventions are recorded here.")}<div class="panel wide">${data.audit.map(row => `<div class="audit-entry"><time>${esc(stamp(row.created_at))}</time><strong>${esc(row.action)}</strong><span>${esc(row.actor_role || row.actor_type)} → ${esc(row.target_type)} ${esc(row.target_id)}<br><small>${esc(row.reason || "No reason recorded")}</small></span></div>`).join("") || `<div class="empty">No audit records</div>`}</div>`;
}

async function renderHealth() {
  const [health, overview] = await Promise.all([request("/health"), cached("overview", "/admin/overview")]);
  return `<section class="hero"><span class="status-chip">Service healthy</span><h3>FCX is answering<br>for itself.</h3><p>The exchange database, authenticated control API, and standalone PWA are independent from CAD 1 and CAD 2.</p></section>${metrics([["Database", health.database, "Dedicated FCX connection", "up"], ["Communities", health.counts.communities, "Registry rows"], ["Pending settlements", health.counts.pending_settlements, "Awaiting terminal state"], ["Open cases", health.counts.open_investigations, "FEC docket"], ["Service", health.service, "API v1"], ["CAD dependency", "None", `${overview.communities.length} API clients`, "up"]])}`;
}

const renderers = { overview: renderOverview, market: renderMarket, companies: renderCompanies, securities: renderSecurities, orders: renderOrders, trades: renderTrades, accounts: renderAccounts, investigations: renderInvestigations, communities: renderCommunities, connections: renderConnections, leverage: renderLeverage, settings: renderSettings, audit: renderAudit, health: renderHealth };

function bindActions() {
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
    event.preventDefault(); const form = new FormData(event.currentTarget); const body = { leverage_enabled: form.get("leverage_enabled") === "true", max_leverage: Number(form.get("max_leverage")) };
    try { await request("/admin/market/settings", { method: "PATCH", body }); state.cache.clear(); notice("Leverage controls saved."); await openSection("leverage", true); } catch (error) { notice(error.message, true); }
  });
  document.querySelectorAll(".market-toggle").forEach(button => button.addEventListener("click", async () => {
    try { await request("/admin/market/settings", { method: "PATCH", body: { [button.dataset.field]: button.dataset.value === "1" } }); state.cache.clear(); await openSection("settings", true); } catch (error) { notice(error.message, true); }
  }));
}

function showLogin() { $("#login").hidden = false; $("#workspace").hidden = true; state.user = null; state.csrf = ""; }
function showWorkspace() { $("#login").hidden = true; $("#workspace").hidden = false; $("#operator-name").textContent = `${state.user.display_name} · ${(state.user.roles || []).join(", ")}`; renderNav(); openSection(state.section, true); }

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault(); $("#login-error").textContent = "";
  try {
    const data = await request("/auth/login", { method: "POST", body: { email: $("#email").value, password: $("#password").value } });
    state.user = data.user; state.csrf = data.csrf_token; showWorkspace();
  } catch (error) { $("#login-error").textContent = error.message; }
});

$("#logout").addEventListener("click", async () => { try { await request("/auth/logout", { method: "POST" }); } finally { showLogin(); } });
$("#refresh").addEventListener("click", () => openSection(state.section, true));

(async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  try { const data = await request("/auth/session"); state.user = data.user; state.csrf = data.csrf_token; showWorkspace(); } catch { showLogin(); }
})();
