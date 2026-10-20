"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (v, c) => (v == null ? "" : `${c === "USD" ? "$" : c === "EUR" ? "€" : c === "CNY" ? "¥" : c + " "}${Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);

let names = {}, scen = null, current = null, legs = {}, basket = null, seen = 0, counts = {}, armed = new Set();
const FAULTS = {
  lisbon: [
    ["summit_sells_out", "The summit sells out while the other legs are held"],
    ["dinner_cancels", "The restaurant cancels the private table"],
    ["hotel_last_rooms_go", "Another buyer takes the hotel's last rooms"],
    ["rogue_hotel", "The hotel tries to capture after it is told to abort"],
    ["airline_commit_error", "The airline errors after it captured"],
    ["crash", "Kill the coordinator in the middle of the holds"],
  ],
  hardware: [
    ["displays_gone", "The display maker's stock goes to another buyer"],
    ["rogue_chips", "The chip supplier tries to capture after abort"],
    ["crash", "Kill the coordinator in the middle of the holds"],
  ],
};

// ------------------------------------------------------------ webauthn helpers
const b64uToBuf = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0)).buffer;
const bufToB64u = (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
function credToJSON(c) {
  const r = c.response, out = { id: c.id, rawId: bufToB64u(c.rawId), type: c.type, clientExtensionResults: {}, response: { clientDataJSON: bufToB64u(r.clientDataJSON) } };
  if (r.attestationObject) out.response.attestationObject = bufToB64u(r.attestationObject);
  if (r.authenticatorData) { out.response.authenticatorData = bufToB64u(r.authenticatorData); out.response.signature = bufToB64u(r.signature); out.response.userHandle = r.userHandle ? bufToB64u(r.userHandle) : null; }
  return out;
}
async function passkeyGet(opts) {
  const o = { ...opts, challenge: b64uToBuf(opts.challenge), allowCredentials: (opts.allowCredentials || []).map((c) => ({ ...c, id: b64uToBuf(c.id) })) };
  return credToJSON(await navigator.credentials.get({ publicKey: o }));
}
async function passkeyCreate(opts) {
  const o = { ...opts, challenge: b64uToBuf(opts.challenge), user: { ...opts.user, id: b64uToBuf(opts.user.id) }, excludeCredentials: [] };
  return credToJSON(await navigator.credentials.create({ publicKey: o }));
}
const post = (u, b) => fetch(u, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b || {}) }).then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw j; return j; });

// ------------------------------------------------------------ scenario
async function load(name) {
  scen = name;
  document.querySelectorAll(".scen").forEach((b) => b.classList.toggle("on", b.dataset.s === name));
  current = await fetch(`/api/scenario?name=${name}`).then((r) => r.json());
  const m = current.mandate;
  $("#mode").textContent = current.mode === "live" ? "Airwallex sandbox" : "replay (no credentials)";
  $("#purpose").textContent = m.purpose;
  $("#cap").textContent = money(m.cap, m.home_currency) + " " + m.home_currency;
  $("#commitby").textContent = new Date(m.commit_by).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("#digest").textContent = current.digest.slice(0, 23) + "…";
  $("#legspec").innerHTML = m.legs.map((l) => {
    const req = l.require.map((c) => `${c.attr.replace("_", " ")} ${{ le: "≤", ge: "≥", eq: "=" }[c.op] || c.op} ${c.value}`).join(", ");
    const sub = l.substitution.allowed ? `substitutes allowed up to ${money(l.substitution.max_price, l.currency)}` : (l.preferred_merchant ? "named merchant, no substitutes" : "no substitutes");
    return `<li><div class="t">${esc(l.title)}</div><div class="c">up to ${money(l.max_price, l.currency)}${req ? " · " + esc(req) : ""} · ${sub}</div></li>`;
  }).join("");
  $("#faults").innerHTML = FAULTS[name].map(([k, t]) => `<label><input type="checkbox" value="${k}" ${armed.has(k) ? "checked" : ""}> ${esc(t)}</label>`).join("");
  $("#faults").querySelectorAll("input").forEach((i) => i.onchange = () => (i.checked ? armed.add(i.value) : armed.delete(i.value)));
  resetLegs(m);
  $("#vicid").textContent = "none";
  names = Object.fromEntries((current.merchants || []).map((x) => [x.id, x.name]));
  const pk = await fetch("/api/passkey/status").then((r) => r.json());
  $("#approve").disabled = !pk.users.some((u) => u !== "demo-passkey");
  $("#register").hidden = !$("#approve").disabled;
}

function resetLegs(m) {
  legs = {};
  for (const l of m.legs) legs[l.leg_id] = { spec: l, merchant: "", offer: "", amount: null, home: null, st: {}, hist: [] };
  basket = null; counts = { tapok: 0, tapno: 0, cred: 0, conf: 0, rules: 0 };
  $("#bid").textContent = ""; $("#outcome").innerHTML = ""; $("#seqout").innerHTML = "";
  renderLegs(); renderCounts();
}

function renderLegs() {
  $("#legrows").innerHTML = Object.entries(legs).map(([id, l]) => {
    const s = l.st, d = (k, cls) => `<span class="dot ${s[k] ? cls || k : ""}"></span>`;
    const hold = s.fail ? "fail" : s.held ? (s.void ? "void" : "held") : "";
    const cap = s.cap ? "cap" : s.void ? "void" : "";
    return `<div class="row" id="row-${id}">
      <div class="leg">${esc(l.spec.title)}<small>${esc(id)}</small></div>
      <div class="m"><div class="name">${esc(names[l.merchant] || l.merchant || "")}</div><div class="hist">${esc(l.offer || "")}${l.hist.map((h) => ` <s>${esc(h.replace(/^[a-z-]+/, (m) => names[m] || m).replace("_", " "))}</s>`).join("")}</div></div>
      <div class="amt">${l.amount ? money(l.amount, l.spec.currency) : ""}${l.home && l.spec.currency !== current.mandate.home_currency ? `<small>${money(l.home, "USD")} locked</small>` : ""}</div>
      <div class="stages-dots"><span class="dot ${s.card ? "card" : ""}"></span><span class="dot ${hold}"></span><span class="dot ${cap}"></span></div>
    </div>`;
  }).join("");
}
const flash = (id) => { const r = $(`#row-${id}`); if (r) { r.classList.add("flash"); setTimeout(() => r.classList.remove("flash"), 700); } };
function renderCounts() {
  $("#tapok").textContent = counts.tapok; $("#tapno").textContent = counts.tapno; $("#viccred").textContent = counts.cred;
  $("#vicconf").textContent = counts.conf; $("#rules").textContent = counts.rules;
}

// ------------------------------------------------------------ events
const ACT = (a) => (["coordinator", "issuer", "vic", "network", "registry", "console", "person", "sequential"].includes(a) ? a : "merchant");
function line(e) {
  const d = e.data, k = e.kind;
  const T = {
    "keystone.started": () => `Keystone process started (${d.mode})`,
    "passkey.registered": () => `Passkey registered for ${d.user}`,
    "vic.instruction": () => `Instruction ${d.instruction_id} approved by passkey over ${d.basket_digest.slice(0, 19)}…`,
    "basket.opened": () => `Basket opened, cap ${d.cap}`,
    "basket.planned": () => `Plan fits the cap: ${d.total}`,
    "basket.order": () => `Hold order: ${d.order.join(" → ")} (${d.why})`,
    "fx.quote_locked": () => `FX quote locked: ${d.buy} for ${d.sell} until ${d.valid_to.slice(11, 16)}`,
    "issuer.card_issued": () => `Card •${d.last4} issued for ${d.leg_id}: ${d.currency} ≤ ${d.limit}, MCC ${d.mcc}, one use`,
    "vic.credential": () => `Credential for ${d.leg_id} released inside its mandate (${d.amount})`,
    "prepare.sent": () => `Hold requested at ${d.merchant}: ${d.offer}, ${d.amount}`,
    "tap.verified": () => `TAP signature verified (${d.tag})`,
    "tap.rejected": () => `TAP refused: ${d.code}`,
    "ledger.decision": () => `Basket ledger: ${d.response_status}${d.status_reason ? " " + d.status_reason : ""}${d.basket_held ? ", held " + money(d.basket_held, "USD") : ""}`,
    "issuer.authorization": () => `Authorization on card ${d.status === "FAILED" ? "declined: " + d.reason : "approved"} (${d.amount} ${d.currency})`,
    "merchant.held": () => `Held ${d.amount} ${d.currency} on ${d.intent}`,
    "prepare.held": () => `Hold receipt signed by ${d.merchant}`,
    "merchant.inventory_taken": () => `Stock taken by another buyer`,
    "merchant.sold_out": () => `Sold out`,
    "prepare.failed": () => `Hold failed at ${d.merchant}: ${d.why}`,
    "decision.substitute": () => `Substitute inside the ${d.within}: ${d.merchant}, ${d.offer}, ${d.amount}`,
    "escalation.opened": () => `Asking the person: ${d.options.length} priced options`,
    "vic.amended": () => `Instruction amended by passkey: ${d.leg_id} up to ${d.threshold}`,
    "escalation.closed": () => (d.choice ? `Person approved ${d.choice.title}` : `Person declined`),
    "advisor.ranked": () => `${d.model} ranked ${d.allowed} offers the rules allow for ${d.leg_id}: ${d.order[0]} first`,
    "advisor.explained": () => `${d.model} wrote the explanation; prices and rules come from the code`,
    "decision.commit": () => `Every leg held. Commit decision logged (${d.prepared_in_ms} ms)`,
    "fx.converted": () => `Converted ${d.sell} → ${d.buy} at the locked rate ${d.rate}`,
    "merchant.captured": () => `Captured ${d.amount} ${d.currency}`,
    "issuer.clearing": () => `Clearing on card: ${d.amount} ${d.currency} ${d.status}`,
    "commit.done": () => `${d.leg_id} committed`,
    "commit.retry": () => `Commit retry ${d.attempt} for ${d.leg_id}: ${d.why}`,
    "decision.abort": () => `Abort decision logged: ${d.why}`,
    "merchant.voided": () => `Voided ${d.amount} ${d.currency}: ${d.check?.detail || ""}`,
    "issuer.reversal": () => `Reversal on card: ${d.amount} ${d.currency}`,
    "abort.done": () => `${d.leg_id} voided at ${d.merchant} in ${d.ms} ms`,
    "basket.aborted": () => `Basket aborted. ${d.voids} holds voided in ${d.void_ms} ms`,
    "issuer.card_closed": () => `Card for ${d.leg_id} closed`,
    "vic.confirmation": () => `Outcome confirmed to VIC: ${d.leg_id} ${d.outcome}`,
    "merchant.rogue_capture_refused": () => `Capture after abort refused by Airwallex (${d.code}). Visa rule 1.7.7.2 holds`,
    "network.capture_refused": () => `Network: capture refused (${d.code})`,
    "coordinator.killed": () => `Coordinator process killed during ${d.at}`,
    "recovery.found": () => `Recovery: basket ${d.basket_id} found in the log, decision ${d.decision}, legs ${d.prepared.join(", ")}`,
    "fault.armed": () => `Scenario event armed: ${d.fault}`,
    "fault.injected": () => `Fault injected: ${d.phase} ${d.mode}`,
    "seq.paid": () => `Sequential agent paid ${d.leg_id}: ${d.amount}`,
    "merchant.refund_refused": () => `Refund refused: non-refundable ${d.amount} ${d.currency}`,
    "merchant.refunded": () => `Refunded ${d.amount} ${d.currency}`,
    "seq.unwound": () => `Sequential agent stuck: ${d.stranded_home} USD stranded, ${d.refunds} refunds pending`,
    "passkey.rejected": () => `Passkey rejected`,
    "coordinator.error": () => `Error: ${d.error}`,
  };
  return T[k] ? T[k]() : null;
}

function onEvent(e) {
  const d = e.data;
  if (e.kind === "basket.opened" && !String(d.basket_id).startsWith("seq_")) { resetLegs(current.mandate); basket = d.basket_id; $("#bid").textContent = basket; $("#intents tbody").innerHTML = ""; $("#cards tbody").innerHTML = ""; }
  const isSeq = String(d.basket_id || "").startsWith("seq_") || e.kind.startsWith("seq.") || e.actor === "sequential";
  const text = line(e);
  if (text) {
    const li = document.createElement("li");
    const bad = /failed|declined|refused|killed|sold out|taken|Error|stranded/i.test(text);
    const big = /^(Every leg|Basket aborted|Abort decision|Coordinator process|Recovery)/.test(text);
    li.className = (bad ? "bad " : "") + (big ? "big" : "");
    const actor = isSeq ? "sequential" : e.actor;
    li.innerHTML = `<span class="t">${e.at.slice(11, 19)}</span><span><span class="a ${ACT(e.actor)}">${esc(actor)}</span><span class="x">${esc(text)}</span></span>`;
    const ol = $("#events"); ol.prepend(li); while (ol.children.length > 300) ol.lastChild.remove();
  }
  if (e.kind === "tap.verified") counts.tapok++;
  if (e.kind === "tap.rejected") counts.tapno++;
  if (isSeq) {
    if (e.kind === "seq.started") { resetLegs(current.mandate); $("#bid").textContent = "sequential agent: pays each leg as it goes"; $("#intents tbody").innerHTML = ""; $("#cards tbody").innerHTML = ""; }
    if (e.kind === "seq.unwound") showSeq(d);
    const SL = d.leg_id && legs[d.leg_id];
    if (SL) {
      if (e.kind === "issuer.card_issued") SL.st.card = true;
      if (e.kind === "prepare.sent") { SL.offer = d.offer; SL.merchant = d.merchant; SL.amount = d.amount.split(" ")[0]; }
      if (e.kind === "prepare.held") SL.st.held = true;
      if (e.kind === "seq.paid") SL.st.cap = true;
      if (e.kind === "prepare.failed") { SL.st.fail = true; SL.hist.push(`${SL.merchant}: ${d.why}`); }
      flash(d.leg_id); renderLegs();
    }
    renderCounts(); return;
  }
  const L = d.leg_id && legs[d.leg_id];
  switch (e.kind) {
    case "vic.instruction": $("#vicid").textContent = d.instruction_id; break;
    case "vic.credential": counts.cred++; break;
    case "vic.confirmation": counts.conf++; break;
    case "plan.leg": if (L) Object.assign(L, { merchant: d.merchant, offer: d.offer, amount: d.amount.split(" ")[0], home: d.home }); break;
    case "basket.planned": for (const [k, v] of Object.entries(d.legs)) if (legs[k]) Object.assign(legs[k], { merchant: v.merchant, offer: v.offer, amount: v.amount.split(" ")[0], home: v.home }); break;
    case "issuer.card_issued": if (L) { L.st.card = true; flash(d.leg_id); } break;
    case "prepare.sent": if (L) { L.offer = d.offer; L.merchant = d.merchant; L.amount = d.amount.split(" ")[0]; } break;
    case "prepare.held": if (L) { L.st.held = true; L.st.fail = false; flash(d.leg_id); } break;
    case "prepare.failed": if (L) { L.st.fail = true; L.hist.push(`${L.merchant}: ${d.why}`); flash(d.leg_id); } break;
    case "decision.substitute": if (L) { L.merchant = d.merchant; L.offer = d.offer; L.amount = d.amount.split(" ")[0]; L.home = d.home; L.st.fail = false; } break;
    case "escalation.opened": showPhone(d); break;
    case "escalation.closed": $("#phone").hidden = true; if (L && d.choice) { L.merchant = d.choice.merchant_id; L.offer = d.choice.title; L.amount = d.choice.amount; L.home = d.choice.home; L.st.fail = false; } break;
    case "commit.done": if (L) { L.st.cap = true; flash(d.leg_id); } break;
    case "abort.done": if (L) { L.st.void = true; flash(d.leg_id); } break;
    case "merchant.voided": counts.rules++; break;
    case "merchant.captured": counts.rules++; break;
    case "merchant.rogue_capture_refused": $("#outcome").innerHTML += `<small>A merchant tried to capture a voided hold: Airwallex refused it.</small>`; break;
    case "basket.report": showOutcome(d); break;
    case "coordinator.killed": $("#outcome").innerHTML = `<span class="ab">Coordinator killed mid-basket.</span><small>Holds are live at the merchants; the log on disk is all that survives.</small>`; break;
    case "basket.aborted": if (d.recovered) $("#outcome").innerHTML = `<span class="ab">Recovered from the log: presumed abort.</span><small>${d.voids} holds voided in ${d.void_ms} ms after restart. $0 charged.</small>`; break;
  }
  renderLegs(); renderCounts();
}

function showOutcome(r) {
  if (r.status === "COMMITTED") {
    const planned = Object.values(r.legs).reduce((a, l) => a + Number(l.home || 0), 0);
    $("#outcome").innerHTML = `<span class="ok">Committed. ${money(r.charged_home, "USD")} charged.</span><small>Every leg captured at the quote-locked rates; total equals the plan the passkey approved (${money(planned, "USD")}), under the ${money(r.cap, "USD")} cap. ${r.elapsed_ms} ms end to end.</small>`;
  } else if (r.status === "ABORTED") {
    $("#outcome").innerHTML = `<span class="ab">Aborted. $0.00 charged.</span><small>${esc(r.why)}. ${r.void_ms != null ? `Holds voided in ${r.void_ms} ms.` : ""} Nothing to refund.</small>`;
  }
}
function showSeq(d) {
  if (!d.status) return;
  if (d.status === "COMPLETED") { $("#seqout").innerHTML = `Sequential agent: completed, ${money(d.charged_home, "USD")} paid.`; return; }
  $("#seqout").innerHTML = `Sequential agent on the same events: paid ${money(d.charged_home, "USD")} before <b>${esc(d.failed_leg)}</b> failed. <b>${money(d.stranded_home, "USD")} stranded</b> in non-refundable legs, ${money(d.refunded_home, "USD")} waiting on refunds, ${money(d.fx_loss_home, "USD")} lost to FX on the way back.`;
}

function showPhone(d) {
  $("#phone").hidden = false;
  $("#ph-why").textContent = d.explanation;
  $("#ph-options").innerHTML = d.options.map((o) => `<div class="opt"><div class="p">${esc(o.title)} · ${money(o.amount, o.currency)}</div>
    <div class="b">breaks: ${esc((o.breaks || []).join("; ") || "nothing")}</div>
    <button class="primary" data-o="${esc(o.offer_id)}">Approve with passkey</button> <button class="link" data-demo="${esc(o.offer_id)}">demo passkey</button></div>`).join("");
  $("#ph-options").querySelectorAll("button[data-o]").forEach((b) => b.onclick = async () => {
    try {
      const o = await fetch(`/api/escalations/${d.escalation_id}/options/${b.dataset.o}`).then((r) => r.json());
      const a = await passkeyGet(o.assertion_options);
      await post(`/api/escalations/${d.escalation_id}/resolve`, { offer_id: b.dataset.o, assertion: a });
    } catch (e) { $("#msg").textContent = e.detail || e.error || String(e); }
  });
  $("#ph-options").querySelectorAll("button[data-demo]").forEach((b) => b.onclick = () => post(`/api/escalations/${d.escalation_id}/resolve`, { offer_id: b.dataset.demo, demo_passkey: true }).catch((e) => ($("#msg").textContent = e.detail || e.error)));
  $("#ph-abort").onclick = () => post(`/api/escalations/${d.escalation_id}/resolve`, { offer_id: null });
}

// ------------------------------------------------------------ ledger poll
async function pollLedger() {
  try {
    const a = await fetch("/api/airwallex").then((r) => r.json());
    $("#awxmode").textContent = a.mode === "live" ? "sandbox API" : "replay simulator";
    $("#intents tbody").innerHTML = a.intents.map((i) => `<tr><td class="mono">${esc((i.id || "").slice(0, 18))}</td><td>${esc(i.leg)}</td><td><span class="st ${esc(i.status)}">${esc(i.status || i.error)}</span></td><td class="r">${money(i.amount, i.currency)}</td></tr>`).join("");
    $("#cards tbody").innerHTML = a.cards.map((c) => `<tr><td>•${esc(c.last4)} ${esc(c.leg)}</td><td class="mono">${c.txns.map((t) => `${t.type.replace("REVERSAL_AUTH", "REVERSAL")} ${Math.abs(Number(t.amount)).toFixed(2)}${t.status === "REVERSED" ? " reversed" : ""}`).join(" · ")}</td><td><span class="st ${esc(c.status)}">${esc(c.status)}</span></td></tr>`).join("");
  } catch (e) { /* the console keeps going while the coordinator restarts */ }
}

// ------------------------------------------------------------ wiring
function connect() {
  const es = new EventSource(`/api/events?since=${seen}`);
  es.onmessage = (m) => { const e = JSON.parse(m.data); seen = Math.max(seen, e.seq); onEvent(e); };
  es.onerror = () => { es.close(); setTimeout(connect, 1200); };
}
async function run(demo) {
  $("#msg").textContent = "";
  try {
    const body = { scenario: scen, digest: current.digest, faults: [...armed] };
    if (demo) body.demo_passkey = true;
    else body.assertion = await passkeyGet(current.assertion_options || (await fetch(`/api/scenario?name=${scen}`).then((r) => r.json())).assertion_options);
    await post("/api/run", body);
  } catch (e) { $("#msg").textContent = e.detail || e.error || String(e); }
}
$("#approve").onclick = () => run(false);
$("#demo").onclick = () => run(true);
$("#seq").onclick = () => post("/api/run-sequential", { scenario: scen, faults: [...armed].filter((f) => f !== "crash") });
$("#register").onclick = async () => {
  try {
    const o = await post("/api/passkey/register/options", { user: "founder" });
    await post("/api/passkey/register", { user: "founder", credential: await passkeyCreate(o) });
    await load(scen);
  } catch (e) { $("#msg").textContent = e.detail || e.error || String(e); }
};
document.querySelectorAll(".scen").forEach((b) => (b.onclick = () => { armed.clear(); load(b.dataset.s); }));
load("lisbon").then(() => { seen = 0; connect(); });
setInterval(pollLedger, 1500);
