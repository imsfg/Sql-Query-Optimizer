"use strict";

/* ============================== utilities ============================== */
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
};

const ICON = {
  check: '<svg viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  alert: '<svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
  shield: '<svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
  code: '<svg viewBox="0 0 24 24"><path d="M8 6l-6 6 6 6M16 6l6 6-6 6"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  download: '<svg viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg>',
  table: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/></svg>',
  gauge: '<svg viewBox="0 0 24 24"><path d="M12 14l4-4"/><path d="M3.3 17a9 9 0 1 1 17.4 0"/></svg>',
  book: '<svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/></svg>',
  chevron: '<svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
  retry: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>',
};

const EXAMPLES = [
  "Show all employees hired after January 2024",
  "Show all customers",
  "Top 5 products by total revenue",
  "Average order value by customer state",
  "Which sales reps handled the most orders in 2025?",
  "SELECT * FROM Orders o JOIN Customers c ON o.CustomerID = c.CustomerID WHERE strftime('%Y', o.OrderDate) = '2024'",
  "SELECT FirstName, COUNT(*) FROM Employees JOIN Departments ON DepartmentID = DepartmentID",
  "Delete all cancelled orders",
  "Who won the FIFA World Cup?",
];

const INTENT_LABEL = {
  generate: "Generated SQL", modify: "Refined query", explain: "Explanation", optimize: "Optimized SQL",
  debug: "Debugged SQL", schema_info: "Schema", help: "Help",
};

/* ============================== SQL highlighting ============================== */
const SQL_KEYWORDS = new Set((
  "SELECT FROM WHERE AND OR NOT IN IS NULL AS ON JOIN LEFT RIGHT INNER OUTER FULL CROSS GROUP BY ORDER HAVING " +
  "LIMIT OFFSET DISTINCT UNION ALL INTERSECT EXCEPT WITH RECURSIVE CASE WHEN THEN ELSE END ASC DESC BETWEEN LIKE " +
  "GLOB EXISTS CREATE INDEX TABLE INSERT UPDATE DELETE DROP ALTER INTO VALUES SET OVER PARTITION CAST TRUE FALSE " +
  "USING NATURAL WINDOW FILTER COLLATE ESCAPE NULLS FIRST LAST ROWS RANGE PRECEDING FOLLOWING CURRENT ROW INTERVAL"
).split(" "));

function highlightSQL(sql) {
  const re = /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*'?)|("(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\])|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?|([\s\S])/g;
  let out = "", m;
  while ((m = re.exec(sql))) {
    if (m[1]) out += `<span class="tok-com">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span class="tok-str">${esc(m[2])}</span>`;
    else if (m[3]) out += esc(m[3]);
    else if (m[4]) out += `<span class="tok-num">${m[4]}</span>`;
    else if (m[5]) {
      const word = m[5], paren = m[6] || "";
      if (SQL_KEYWORDS.has(word.toUpperCase())) out += `<span class="tok-kw">${esc(word)}</span>${esc(paren)}`;
      else if (paren) out += `<span class="tok-fn">${esc(word)}</span>${esc(paren)}`;
      else out += esc(word);
    } else out += esc(m[7]);
  }
  return out;
}

/* Minimal, safe markdown: paragraphs, "- " lists, `code`, **bold**. Input is escaped first. */
function md(text) {
  const inline = (s) => s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  let html = "", inList = false;
  for (const line of esc(text).split("\n")) {
    const li = line.match(/^\s*[-*]\s+(.*)/);
    if (li) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(li[1])}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}

/* ============================== state ============================== */
const state = {
  threadId: null,
  busy: false,
  dialect: "sqlite",
  selected: null, // { el, response }
  convs: store.get("sqla.convs", []),
};

const els = {
  messages: $("#messages"), welcome: $("#welcome"), input: $("#input"), sendBtn: $("#sendBtn"), composer: $("#composer"),
  panelEmpty: $("#panelEmpty"), panelBody: $("#panelBody"), convList: $("#convList"), convEmpty: $("#convEmpty"),
  chatTitle: $("#chatTitle"), executeToggle: $("#executeToggle"), toast: $("#toast"), sidebar: $("#sidebar"), scrim: $("#scrim"),
};

/* ============================== small UI helpers ============================== */
let toastTimer;
function toast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.remove("show"), 2200);
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  toast("Copied to clipboard");
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function scrollToBottom() { els.messages.scrollTop = els.messages.scrollHeight; }

function setBusy(busy) {
  state.busy = busy;
  els.sendBtn.disabled = busy;
  els.input.disabled = busy;
  if (!busy) els.input.focus();
}

/* ============================== conversations ============================== */
function saveConv(id, title) {
  const existing = state.convs.find((c) => c.id === id);
  if (existing) existing.updated = Date.now();
  else state.convs.unshift({ id, title, updated: Date.now() });
  state.convs.sort((a, b) => b.updated - a.updated);
  state.convs = state.convs.slice(0, 50);
  store.set("sqla.convs", state.convs);
  renderConvs();
}

function renderConvs() {
  els.convList.innerHTML = "";
  els.convEmpty.classList.toggle("hidden", state.convs.length > 0);
  for (const c of state.convs) {
    const li = document.createElement("li");
    li.className = "conv-item" + (c.id === state.threadId ? " active" : "");
    li.innerHTML = `<button class="conv-open" title="${esc(c.title)}">${esc(c.title)}</button>
      <button class="icon-btn" aria-label="Remove conversation">${ICON.trash}</button>`;
    li.querySelector(".conv-open").onclick = () => { closeSidebar(); loadThread(c.id); };
    li.querySelector(".icon-btn").onclick = () => {
      state.convs = state.convs.filter((x) => x.id !== c.id);
      store.set("sqla.convs", state.convs);
      if (c.id === state.threadId) newChat(); else renderConvs();
    };
    els.convList.appendChild(li);
  }
}

function resetView(title) {
  els.messages.querySelectorAll(".msg").forEach((m) => m.remove());
  els.welcome.classList.toggle("hidden", !!title);
  els.chatTitle.textContent = title || "New conversation";
  state.selected = null;
  els.panelBody.classList.add("hidden");
  els.panelEmpty.classList.remove("hidden");
}

function newChat() {
  state.threadId = null;
  resetView(null);
  renderConvs();
  els.input.focus();
}

async function loadThread(id) {
  if (state.busy) return;
  const conv = state.convs.find((c) => c.id === id);
  state.threadId = id;
  resetView(conv ? conv.title : "Conversation");
  renderConvs();
  try {
    const res = await fetch(`/api/threads/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(`Server error (${res.status})`);
    const { history } = await res.json();
    if (!history.length) { els.welcome.classList.remove("hidden"); return; }
    let lastSqlMsg = null;
    for (const turn of history) {
      if (turn.role === "user") addUserMessage(turn.content);
      else {
        const response = { type: turn.sql ? "sql" : "info", message: turn.content, sql: turn.sql, restored: true };
        const el = addAssistantMessage(response);
        if (turn.sql) lastSqlMsg = { el, response };
      }
    }
    if (lastSqlMsg) select(lastSqlMsg.el, lastSqlMsg.response);
    scrollToBottom();
  } catch (err) {
    toast(`Couldn't load conversation: ${err.message}`);
  }
}

/* ============================== messages ============================== */
function hideWelcome() { els.welcome.classList.add("hidden"); }

function addUserMessage(text) {
  hideWelcome();
  const msg = document.createElement("div");
  msg.className = "msg user";
  msg.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  els.messages.appendChild(msg);
  scrollToBottom();
  return msg;
}

function tagFor(response) {
  switch (response.type) {
    case "refusal": return `<div class="msg-tag refusal">${ICON.shield}Declined</div>`;
    case "error": return `<div class="msg-tag error">${ICON.alert}Error</div>`;
    case "clarification": return `<div class="msg-tag clarification">${ICON.info}Needs clarification</div>`;
    case "sql": return `<div class="msg-tag sql">${ICON.code}${esc(INTENT_LABEL[response.intent] || "SQL")}</div>`;
    default: return "";
  }
}

function assistantInner(response) {
  const kind = ["refusal", "error", "clarification"].includes(response.type) ? response.type : "";
  let html = `<div class="bubble ${kind}">${tagFor(response)}${md(response.message || "")}`;
  if (response.sql) {
    const preview = response.sql.split("\n").slice(0, 6).join("\n");
    html += `<div class="sql-preview">${highlightSQL(preview)}</div>`;
  }
  return html + "</div>";
}

function addAssistantMessage(response, trace) {
  const msg = document.createElement("div");
  msg.className = "msg assistant";
  fillAssistant(msg, response, trace);
  els.messages.appendChild(msg);
  return msg;
}

function fillAssistant(msg, response, trace) {
  msg.innerHTML = assistantInner(response);
  const bubble = msg.querySelector(".bubble");
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  if (response.sql) {
    bubble.classList.add("clickable");
    bubble.onclick = () => select(msg, response, true);
    const btn = document.createElement("button");
    btn.textContent = "View details →";
    btn.onclick = () => select(msg, response, true);
    meta.appendChild(btn);
  }
  if (trace && trace.length) {
    const details = document.createElement("details");
    details.className = "trace";
    details.innerHTML = `<summary>Show workflow (${trace.length} steps)</summary>`;
    const ul = document.createElement("ul");
    ul.className = "steps";
    trace.forEach((s) => ul.appendChild(stepItem({ ...s, status: "done" })));
    details.appendChild(ul);
    meta.appendChild(details);
  }
  if (meta.children.length) msg.appendChild(meta);
}

function stepItem(step) {
  const li = document.createElement("li");
  li.dataset.node = step.node;
  updateStepItem(li, step);
  return li;
}

function updateStepItem(li, step) {
  const retry = /failed/i.test(step.detail || "");
  li.className = `step ${step.status}${retry ? " retry" : ""}`;
  li.dataset.status = step.status;
  const dot = step.status === "running" ? '<span class="spinner"></span>' : step.status === "error" ? ICON.x : retry ? ICON.retry : ICON.check;
  li.innerHTML = `<span class="dot">${dot}</span><span><span class="label">${esc(step.label)}</span>` +
    (step.detail ? `<span class="detail">${esc(step.detail)}</span>` : "") + "</span>";
}

/* A placeholder bubble that shows live workflow progress until the result arrives. */
function addPendingMessage() {
  const msg = document.createElement("div");
  msg.className = "msg assistant";
  msg.innerHTML = `<div class="bubble"><ul class="steps"></ul></div>`;
  els.messages.appendChild(msg);
  scrollToBottom();
  const list = msg.querySelector(".steps");
  return {
    step(ev) {
      if (ev.status === "running") list.appendChild(stepItem(ev));
      else {
        const items = [...list.querySelectorAll(`li[data-node="${ev.node}"]`)];
        const li = items.reverse().find((x) => x.dataset.status === "running");
        li ? updateStepItem(li, ev) : list.appendChild(stepItem(ev));
      }
      scrollToBottom();
    },
    finish(response) {
      fillAssistant(msg, response, response.trace);
      if (response.sql && response.type === "sql") select(msg, response);
      else if (response.type === "error" && response.sql) select(msg, response);
      scrollToBottom();
    },
    fail(message, retryText) {
      msg.innerHTML = `<div class="bubble error"><div class="msg-tag error">${ICON.alert}Error</div><p>${esc(message)}</p></div>`;
      const meta = document.createElement("div");
      meta.className = "msg-meta";
      const btn = document.createElement("button");
      btn.innerHTML = "Retry";
      btn.onclick = () => { msg.remove(); send(retryText, { skipUserMessage: true }); };
      meta.appendChild(btn);
      msg.appendChild(meta);
      scrollToBottom();
    },
  };
}

/* ============================== sending (SSE over fetch) ============================== */
async function readEvents(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const data = chunk.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
      if (data) onEvent(JSON.parse(data));
    }
  }
}

async function send(text, { skipUserMessage = false } = {}) {
  text = text.trim();
  if (state.busy || !text) return;
  if (!skipUserMessage) addUserMessage(text);
  els.input.value = "";
  autoGrow();
  setBusy(true);
  const pending = addPendingMessage();
  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, thread_id: state.threadId, execute: els.executeToggle.checked }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(typeof detail.detail === "string" ? detail.detail : `Server error (${res.status})`);
    }
    let result = null;
    await readEvents(res, (ev) => {
      if (ev.event === "thread") {
        const isNew = !state.threadId;
        state.threadId = ev.thread_id;
        const title = text.length > 48 ? text.slice(0, 47) + "…" : text;
        saveConv(ev.thread_id, title);
        if (isNew) els.chatTitle.textContent = title;
      } else if (ev.event === "step") pending.step(ev);
      else if (ev.event === "result") result = ev.response;
      else if (ev.event === "error") throw new Error(ev.message);
    });
    if (!result) throw new Error("The server closed the connection before sending a response.");
    pending.finish(result);
  } catch (err) {
    const offline = err instanceof TypeError;
    pending.fail(offline ? "Couldn't reach the server. Is it running?" : err.message, text);
  } finally {
    setBusy(false);
  }
}

/* ============================== results panel ============================== */
function select(msgEl, response, reveal = false) {
  if (state.selected) state.selected.el.querySelector(".bubble")?.classList.remove("selected");
  state.selected = { el: msgEl, response };
  msgEl.querySelector(".bubble")?.classList.add("selected");
  renderPanel(response);
  // In the stacked (narrow) layout the panel is below the chat, so bring it into view on request.
  if (reveal && matchMedia("(max-width: 860px)").matches) $("#panel").scrollIntoView({ behavior: "smooth" });
}

function card(title, icon, bodyHTML, { badges = "", actions = "" } = {}) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `<div class="card-head"><h3 class="card-title">${icon}${esc(title)}</h3>${badges}
    <div class="card-actions">${actions}</div></div><div class="card-body">${bodyHTML}</div>`;
  return el;
}

function renderPanel(r) {
  els.panelEmpty.classList.add("hidden");
  const body = els.panelBody;
  body.classList.remove("hidden");
  body.innerHTML = "";
  body.appendChild(sqlCard(r));
  const explanation = explanationCard(r);
  if (explanation) body.appendChild(explanation);
  if (r.validation || r.performance) body.appendChild(checksCard(r));
  if (r.execution) body.appendChild(resultsCard(r));
  if (r.restored) {
    body.appendChild(card("Details", ICON.info,
      '<p class="results-meta">Validation details and results are not stored with conversation history. Ask again to re-run the query.</p>'));
  }
  body.parentElement.scrollTop = 0;
}

function sqlCard(r) {
  const dialects = r.dialects || { sqlite: r.sql };
  if (!(state.dialect in dialects)) state.dialect = "sqlite";
  const v = r.validation;
  let badges = "";
  if (v) {
    badges += v.ok ? `<span class="badge ok">${ICON.check}Validated</span>` : `<span class="badge err">${ICON.x}Invalid</span>`;
    if (v.attempts > 1) badges += `<span class="badge warn">${v.attempts} attempts</span>`;
  }
  const seg = r.dialects ? `<div class="seg" role="tablist">${[["sqlite", "SQLite"], ["postgres", "PostgreSQL"], ["mysql", "MySQL"]]
    .map(([k, label]) => `<button data-d="${k}" class="${k === state.dialect ? "on" : ""}" role="tab">${label}</button>`).join("")}</div>` : "";
  const actions = `${seg}<button class="btn btn-sm" data-act="copy">${ICON.copy}Copy</button>
    <button class="btn btn-sm" data-act="dl">${ICON.download}.sql</button>`;
  const el = card("SQL", ICON.code, "", { badges, actions });
  const bodyEl = el.querySelector(".card-body");
  bodyEl.style.padding = "0";

  const current = () => dialects[state.dialect] || "";
  const paint = () => {
    const sql = current();
    let html = sql ? `<pre class="code">${highlightSQL(sql)}</pre>`
      : `<pre class="code">-- Translation to this dialect is not available</pre>`;
    if (state.dialect !== "sqlite" && sql) html += `<p class="results-meta" style="padding:0 14px 10px;margin:0">Translated for reference; the query runs on SQLite.</p>`;
    if (r.original_sql) {
      html += `<details style="border-top:1px solid var(--border)"><summary style="padding:8px 14px;cursor:pointer;font-size:12.5px;color:var(--text-2)">Your original query</summary>
        <pre class="code">${highlightSQL(r.original_sql)}</pre></details>`;
    }
    bodyEl.innerHTML = html;
    el.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.d === state.dialect));
  };
  paint();
  el.querySelectorAll(".seg button").forEach((b) => b.onclick = () => { state.dialect = b.dataset.d; paint(); });
  el.querySelector('[data-act="copy"]').onclick = () => copyText(current());
  el.querySelector('[data-act="dl"]').onclick = () =>
    downloadBlob(new Blob([current() + ";\n"], { type: "application/sql" }), `query_${state.dialect}.sql`);
  return el;
}

function explanationCard(r) {
  let html = "";
  if (r.explanation) html += `<p class="explain-text">${esc(r.explanation)}</p>`;
  else if (r.restored && r.message) html += `<p class="explain-text">${esc(r.message)}</p>`;
  if (r.issues?.length) {
    html += `<div class="sub-title">Issues found</div>` + r.issues.map((i) =>
      `<div class="issue"><b>${esc(i.problem)}</b><div class="why">${esc(i.why)}</div><div class="fix">Fix: ${esc(i.fix)}</div></div>`).join("");
  }
  if (r.changes?.length) html += `<div class="sub-title">Changes</div><ul class="plain-list">${r.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`;
  if (r.steps?.length) html += `<div class="sub-title">Step by step</div><ol class="plain-list">${r.steps.map((c) => `<li>${esc(c)}</li>`).join("")}</ol>`;
  if (r.assumptions?.length) html += `<div class="sub-title">Assumptions</div><ul class="plain-list">${r.assumptions.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`;
  return html ? card("Explanation", ICON.book, html) : null;
}

const CHECKS = [
  ["Valid syntax", /syntax|statement is allowed/i],
  ["Read-only", /read-only/i],
  ["Tables exist", /unknown table/i],
  ["Columns exist", /unknown column|not produced|alias/i],
  ["Joins follow keys", /join condition/i],
  ["Accepted by SQLite", /sqlite rejected/i],
];

function checksCard(r) {
  const v = r.validation || { ok: true, errors: [], warnings: [] };
  let html = `<ul class="checks">${CHECKS.map(([label, re]) => {
    const failed = v.errors.some((e) => re.test(e));
    return `<li class="${failed ? "fail" : "pass"}">${failed ? ICON.x : ICON.check}${label}</li>`;
  }).join("")}</ul>`;
  html += v.errors.map((e) => `<div class="notice err">${ICON.x}<span>${esc(e)}</span></div>`).join("");
  html += v.warnings.map((w) => `<div class="notice warn">${ICON.alert}<span>${esc(w)}</span></div>`).join("");

  const eq = r.equivalence;
  if (eq) {
    const cls = eq.status === "equivalent" ? "ok" : eq.status === "different" ? "warn" : "info";
    const text = eq.status === "equivalent" ? `Optimized query returns the same results as the original (${eq.detail}).`
      : eq.status === "different" ? `Rewrite changed the results (${eq.detail}); original kept.`
      : `Result equivalence not checked: ${eq.detail}.`;
    html += `<div class="notice ${cls}">${cls === "ok" ? ICON.check : ICON.info}<span>${esc(text)}</span></div>`;
  }

  const p = r.performance;
  if (p) {
    html += `<div class="sub-title">Performance</div>
      <div class="cost"><span class="cost-meter ${esc(p.cost_level)}"><i></i><i></i><i></i></span>
      <b style="text-transform:capitalize">${esc(p.cost_level)} cost</b>
      <span class="results-meta">~${Number(p.estimated_rows_examined).toLocaleString()} rows examined (heuristic from query plan)</span></div>`;
    if (p.index_suggestions?.length) {
      html += `<div class="sub-title">Recommended indexes</div>` + p.index_suggestions.map((s, i) =>
        `<div class="index-row"><pre>${highlightSQL(s.ddl)}</pre><button class="btn btn-sm" data-idx="${i}" aria-label="Copy index">${ICON.copy}</button></div>
         <div class="index-reason">${esc(s.reason)}</div>`).join("");
    }
    const notes = [...(p.notes || []), ...(p.unused_joins || [])];
    html += notes.map((n) => `<div class="notice info">${ICON.info}<span>${esc(n)}</span></div>`).join("");
    if (p.plan?.length) {
      html += `<details style="margin-top:10px"><summary class="results-meta" style="cursor:pointer">Query plan (EXPLAIN QUERY PLAN)</summary>
        <pre class="plan" style="margin-top:6px">${esc(p.plan.join("\n"))}</pre></details>`;
    }
  }
  const badges = v.ok ? `<span class="badge ok">${ICON.shield}Passed</span>` : `<span class="badge err">${v.errors.length} error(s)</span>`;
  const el = card("Validation & optimization", ICON.gauge, html, { badges });
  el.querySelectorAll("[data-idx]").forEach((b) => b.onclick = () => copyText(p.index_suggestions[b.dataset.idx].ddl));
  return el;
}

function resultsCard(r) {
  const ex = r.execution;
  let html;
  if (ex.error) html = `<div class="notice err" style="margin:12px 14px">${ICON.x}<span>${esc(ex.error)}</span></div>`;
  else if (!ex.rows.length) html = `<p class="results-meta" style="padding:12px 14px;margin:0">The query ran successfully but no rows matched.</p>`;
  else {
    const numeric = ex.columns.map((_, i) => ex.rows.every((row) => row[i] === null || typeof row[i] === "number"));
    const fmt = (v, i) => v === null ? '<td class="null">NULL</td>'
      : `<td class="${numeric[i] ? "num" : ""}">${esc(typeof v === "number" && !Number.isInteger(v) ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v)}</td>`;
    html = `<div class="table-wrap"><table class="results"><thead><tr>${ex.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${ex.rows.map((row) => `<tr>${row.map(fmt).join("")}</tr>`).join("")}</tbody></table></div>`;
  }
  const meta = ex.error ? "" : `<span class="results-meta">${ex.row_count}${ex.truncated ? "+" : ""} rows · ${ex.elapsed_ms} ms</span>`;
  const truncated = ex.truncated ? `<span class="badge warn">Showing first ${ex.row_count}</span>` : "";
  const actions = ex.error ? "" : `<button class="btn btn-sm" data-act="csv">${ICON.download}CSV</button>`;
  const el = card("Results", ICON.table, html, { badges: meta + truncated, actions });
  el.querySelector(".card-body").style.padding = "0";
  const csvBtn = el.querySelector('[data-act="csv"]');
  if (csvBtn) csvBtn.onclick = () => exportCSV(r.sql, csvBtn);
  return el;
}

async function exportCSV(sql, btn) {
  btn.disabled = true;
  try {
    const res = await fetch("/api/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql }) });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `Server error (${res.status})`);
    }
    downloadBlob(await res.blob(), "query_results.csv");
    const truncated = res.headers.get("X-Truncated");
    toast(truncated ? `CSV downloaded (${truncated})` : "CSV downloaded");
  } catch (err) {
    toast(`Export failed: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

/* ============================== sidebar: schema, theme, mobile ============================== */
async function loadSchema() {
  const tree = $("#schemaTree");
  try {
    const res = await fetch("/api/schema");
    if (!res.ok) throw new Error();
    const { tables } = await res.json();
    tree.innerHTML = tables.map((t) => {
      const fks = Object.fromEntries(t.foreign_keys.map((fk) => [fk.column, fk]));
      const cols = t.columns.map((c) => {
        const fk = fks[c.name];
        const key = c.primary_key ? '<span class="key pk">PK</span>'
          : fk ? `<span class="key fk" title="References ${esc(fk.ref_table)}.${esc(fk.ref_column)}">FK</span>` : "";
        return `<li title="${esc(c.description || "")}"><code>${esc(c.name)}</code><span class="type">${esc(c.type)}</span>${key}</li>`;
      }).join("");
      return `<details class="schema-table"><summary>${ICON.chevron}<span>${esc(t.name)}</span><span class="count">${t.row_count}</span></summary>
        <ul class="schema-cols">${cols}</ul></details>`;
    }).join("");
  } catch {
    tree.innerHTML = '<p class="side-empty">Schema unavailable.</p>';
  }
}

async function loadHealth() {
  try {
    const res = await fetch("/api/health");
    if (res.ok) $("#modelName").textContent = `${(await res.json()).model} · read-only`;
  } catch { /* offline: keep default label */ }
}

function effectiveTheme() {
  return document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#themeLabel").textContent = theme === "dark" ? "Light mode" : "Dark mode";
}

function openSidebar() { els.sidebar.classList.add("open"); els.scrim.classList.add("show"); }
function closeSidebar() { els.sidebar.classList.remove("open"); els.scrim.classList.remove("show"); }

function autoGrow() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 200) + "px";
}

/* ============================== init ============================== */
function init() {
  $("#exampleChips").innerHTML = EXAMPLES.map((e) => `<button class="chip" type="button">${esc(e)}</button>`).join("");
  $("#exampleChips").querySelectorAll(".chip").forEach((chip) => chip.onclick = () => send(chip.textContent));

  els.composer.addEventListener("submit", (e) => { e.preventDefault(); send(els.input.value); });
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(els.input.value); }
  });
  els.input.addEventListener("input", autoGrow);

  els.executeToggle.checked = store.get("sqla.execute", true);
  els.executeToggle.onchange = () => store.set("sqla.execute", els.executeToggle.checked);

  $("#newChat").onclick = () => { closeSidebar(); newChat(); };
  $("#openSidebar").onclick = openSidebar;
  $("#closeSidebar").onclick = closeSidebar;
  els.scrim.onclick = closeSidebar;

  applyTheme(effectiveTheme());
  $("#themeToggle").onclick = () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    store.set("sqla.theme", next);
  };

  renderConvs();
  loadSchema();
  loadHealth();
  els.input.focus();
}

init();
