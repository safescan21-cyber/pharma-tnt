"use strict";
// Pharma Track & Trace web client. Talks to the REST API with a bearer token.

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ROLE_RANK = { viewer: 0, operator: 1, qa: 2, admin: 3 };
const state = { token: null, user: null, cache: {} };

function store(k, v) { try { v === undefined ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch (_) {} }
function load(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }

// ------------------------------------------------------------------ API
async function api(method, path, body, opts = {}) {
  const headers = {};
  if (state.token) headers.Authorization = "Bearer " + state.token;
  let payload;
  if (body !== undefined) {
    if (opts.csv) { headers["Content-Type"] = "text/csv"; payload = body; }
    else { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  }
  const res = await fetch(path, { method, headers, body: payload });
  if (res.status === 401 && !path.startsWith("/auth/login")) { logout(true); throw new Error("Session expired - please sign in again"); }
  if (opts.blob) { if (!res.ok) throw new Error((await res.json()).error || res.statusText); return res.blob(); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.error || (Array.isArray(data.detail) ? data.detail.map((d) => `${d.loc.slice(-1)[0]}: ${d.msg}`).join("; ") : data.detail) || res.statusText;
    throw new Error(msg);
  }
  return data;
}
const get = (p) => api("GET", p);
const post = (p, b, o) => api("POST", p, b, o);

async function download(path, filename) {
  try {
    const blob = await api("GET", path, undefined, { blob: true });
    saveBlob(blob, filename);
  } catch (e) { toast(e.message, true); }
}
function saveBlob(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
const saveText = (text, filename, type = "text/plain") => saveBlob(new Blob([text], { type }), filename);

function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.add("hidden"), err ? 7000 : 3500);
}

// Run an async form action with button disabled + error toast.
function bind(form, fn) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit],button:not([type])");
    if (btn) btn.disabled = true;
    try { await fn(Object.fromEntries(new FormData(form)), form); }
    catch (err) { toast(err.message, true); }
    finally { if (btn) btn.disabled = false; }
  });
}
const lines = (s) => (s || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
const can = (r) => state.user && ROLE_RANK[state.user.role] >= ROLE_RANK[r];

// ------------------------------------------------------------ dialogs
function dialog(html, onSubmit) {
  return new Promise((resolve) => {
    const d = $("#dlg"), f = $("#dlgForm");
    f.innerHTML = html + `<div class="row" style="justify-content:flex-end;margin-top:8px">
      <button type="button" class="ghost" value="cancel" id="dlgCancel">Cancel</button><button value="ok">Confirm</button></div>`;
    $("#dlgCancel").onclick = () => { d.close(); resolve(null); };
    f.onsubmit = (e) => { e.preventDefault(); const v = Object.fromEntries(new FormData(f)); d.close(); resolve(onSubmit ? onSubmit(v) : v); };
    d.showModal();
    const first = f.querySelector("input,select,textarea"); if (first) first.focus();
  });
}
// 21 CFR Part 11 electronic signature: user re-enters password and states meaning.
function signature(action, meaning) {
  return dialog(`<h3>Electronic signature</h3>
    <p class="muted">You are signing: <b>${esc(action)}</b>. Signed by <b>${esc(state.user.full_name)}</b> (${esc(state.user.username)}).</p>
    <label>Meaning of signature<input name="meaning" value="${esc(meaning)}" required minlength="3"></label>
    <label>Password<input name="password" type="password" required autocomplete="current-password"></label>`);
}

// ---------------------------------------------------- master data cache
async function master(force = false) {
  if (force || !state.cache.products) {
    const [products, locations, lots] = await Promise.all([get("/products"), get("/locations"), get("/lots")]);
    Object.assign(state.cache, { products, locations, lots });
  }
  return state.cache;
}
const opt = (v, label, sel) => `<option value="${esc(v)}" ${v === sel ? "selected" : ""}>${esc(label)}</option>`;
const productOptions = (m) => m.products.map((p) => opt(p.gtin, `${p.name} ${p.strength || ""} - ${p.gtin}`)).join("");
const locationOptions = (m) => m.locations.map((l) => opt(l.gln, `${l.name} (${l.kind}) - ${l.gln}`)).join("");
const lotOptions = (m, gtin) => m.lots.filter((l) => !gtin || l.gtin === gtin).map((l) => opt(l.lot, `${l.lot} - exp ${l.expiry} - ${l.status}`)).join("");
const locName = (gln) => (state.cache.locations || []).find((l) => l.gln === gln)?.name || gln || "";
function linkLots(form) {
  const p = form.elements.gtin, l = form.elements.lot;
  const refresh = () => { l.innerHTML = lotOptions(state.cache, p.value) || '<option value="">No lots - create one under Quality</option>'; };
  p.addEventListener("change", refresh); refresh();
}

function table(cols, rows, empty = "Nothing here yet.") {
  if (!rows.length) return `<p class="muted">${empty}</p>`;
  return `<div class="table"><table><thead><tr>${cols.map((c) => `<th>${esc(c[0])}</th>`).join("")}</tr></thead><tbody>${rows
    .map((r) => `<tr>${cols.map((c) => `<td>${c[1](r)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
const statusBadge = (s) => {
  const cls = { active: "b-ok", released: "b-ok", in_transit: "", dispensed: "", quarantine: "b-warn", recalled: "b-bad", destroyed: "b-bad", stolen: "b-bad", expired: "b-bad" }[s] ?? "";
  return `<span class="badge ${cls}">${esc(String(s).replace("_", " "))}</span>`;
};
const fmtTs = (ts) => esc((ts || "").replace("T", " ").slice(0, 19));
const summarize = (d) => esc(Object.entries(d || {}).filter(([k, v]) => v !== null && k !== "signature").map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · "))
  + (d && d.signature ? ` <span class="badge">signed: ${esc(d.signature.meaning)}</span>` : "");

// --------------------------------------------------------------- pages
const PAGES = [
  { id: "dashboard", title: "Dashboard", role: "viewer", render: pageDashboard },
  { id: "scan", title: "Scan Station", role: "viewer", render: pageScan },
  { id: "verify", title: "Verify & Trace", role: "viewer", render: pageVerify },
  { id: "serialize", title: "Serialization", role: "operator", render: pageSerialize },
  { id: "pack", title: "Packing", role: "operator", render: pagePack },
  { id: "ship", title: "Shipping & Receiving", role: "operator", render: pageShip },
  { id: "quality", title: "Quality & Recall", role: "viewer", render: pageQuality },
  { id: "master", title: "Products & Sites", role: "viewer", render: pageMaster },
  { id: "audit", title: "Audit Trail", role: "viewer", render: pageAudit },
  { id: "users", title: "Users", role: "admin", render: pageUsers },
];

async function pageDashboard(v) {
  const [s, ev] = await Promise.all([get("/stats"), get("/events?limit=15")]);
  const tiles = [["Products", s.products], ["Lots", s.lots], ["Audit events", s.events],
    ...Object.entries(s.items_by_status).map(([k, n]) => [k.replace("_", " ") + " items", n])];
  v.innerHTML = `<h2>Dashboard</h2>
    <div class="tiles card" style="background:none;border:0;padding:0">${tiles.map(([k, n]) => `<div class="tile"><b>${Number(n).toLocaleString()}</b><span>${esc(k)}</span></div>`).join("")}</div>
    <div class="card"><h3>Recent activity</h3>${eventsTable(ev)}</div>`;
}
function eventsTable(ev) {
  return table([["#", (e) => e.id], ["Time (UTC)", (e) => fmtTs(e.ts)], ["Step", (e) => esc(e.biz_step)], ["Items", (e) => e.items.toLocaleString()],
    ["Location", (e) => esc(locName(e.location))], ["User", (e) => esc(e.actor)], ["Details", (e) => `<span class="mono">${summarize(e.data)}</span>`]], ev);
}

// Continuous scanning for USB / Bluetooth barcode scanners (keyboard mode, Enter after each scan).
let audio;
function beep(ok) {
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    const o = audio.createOscillator(), g = audio.createGain();
    o.frequency.value = ok ? 880 : 220; o.type = ok ? "sine" : "square"; g.gain.value = 0.15;
    o.connect(g); g.connect(audio.destination); o.start(); o.stop(audio.currentTime + (ok ? 0.12 : 0.45));
  } catch (_) {}
}
async function pageScan(v) {
  const m = await master(), op = can("operator");
  const modes = [["verify", "Verify - check authenticity"]].concat(op ? [["receive", "Receive goods"], ["dispense", "Dispense to patient"], ["pack", "Pack into container"]] : []);
  v.innerHTML = `<h2>Scan Station</h2><div class="card" id="scard">
    <div class="row"><label>Mode<select id="smode">${modes.map(([k, l]) => opt(k, l)).join("")}</select></label>
      <label id="ssite" class="hidden">Site<select id="sgln">${locationOptions(m)}</select></label>
      <label id="sparent" class="hidden">Container - scan its SSCC first<input id="sparentIn" autocomplete="off"></label></div>
    <input id="scanIn" class="scan-input" placeholder="Scan a barcode…" autocomplete="off" spellcheck="false">
    <div id="flash" class="flash">Ready to scan</div>
    <div class="row"><span class="badge b-ok" id="nOk">0 accepted</span><span class="badge b-bad" id="nBad">0 rejected</span>
      <span class="spacer"></span><button class="ghost small" id="sexport">Export log (CSV)</button><button class="ghost small" id="sclear">Clear log</button></div></div>
    <div class="card"><h3>Scan log</h3><div id="slog"><p class="muted">Scans appear here.</p></div></div>`;
  const log = [], seen = new Set(), inp = $("#scanIn"), mode = $("#smode"), parentIn = $("#sparentIn");
  const sync = () => {
    $("#ssite").classList.toggle("hidden", !["receive", "dispense"].includes(mode.value));
    $("#sparent").classList.toggle("hidden", mode.value !== "pack");
    seen.clear(); inp.focus();
  };
  mode.onchange = sync; sync();
  $("#scard").addEventListener("click", (e) => { if (!["SELECT", "INPUT", "BUTTON"].includes(e.target.tagName)) inp.focus(); });
  const show = (ok, title, detail) => {
    const f = $("#flash"); f.className = "flash " + (ok ? "ok" : "bad");
    f.innerHTML = `<b>${esc(title)}</b><span>${esc(detail)}</span>`; beep(ok);
  };
  const render = () => {
    $("#nOk").textContent = `${log.filter((x) => x.ok).length} accepted`;
    $("#nBad").textContent = `${log.filter((x) => !x.ok).length} rejected`;
    $("#slog").innerHTML = table([["Time", (x) => esc(x.time)], ["Mode", (x) => esc(x.mode)], ["Code", (x) => `<code>${esc(x.code)}</code>`],
      ["Result", (x) => `<span class="badge ${x.ok ? "b-ok" : "b-bad"}">${x.ok ? "OK" : "REJECTED"}</span>`], ["Details", (x) => esc(x.msg)]], log.slice(0, 300));
  };
  async function handle(code) {
    const md = mode.value, gln = $("#sgln").value;
    let ok = false, msg = "";
    try {
      if (md !== "verify" && md !== "pack" && seen.has(code)) throw new Error("Already scanned in this session");
      if (md === "verify") {
        const r = await get("/verify?code=" + encodeURIComponent(code));
        ok = r.verified;
        msg = ok ? `${r.product ? r.product.name + " " + (r.product.strength || "") + " · lot " + r.lot + " · exp " + r.expiry : r.kind} · ${locName(r.location)}` : r.reasons.join("; ");
      } else if (md === "receive") {
        const r = await post("/receive", { codes: [code], at_gln: gln }); ok = true; msg = `Received ${r.items} item(s) at ${locName(gln)}`;
      } else if (md === "dispense") {
        await post("/dispense", { codes: [code], at_gln: gln }); ok = true; msg = `Dispensed at ${locName(gln)}`;
      } else if (md === "pack") {
        if (!parentIn.value.trim()) { parentIn.value = code; ok = true; msg = "Container selected - now scan the items to pack"; }
        else { await post("/aggregate", { parent: parentIn.value.trim(), children: [code] }); ok = true; msg = `Packed into ${parentIn.value.trim()}`; }
      }
    } catch (e) { ok = false; msg = e.message; }
    if (ok) seen.add(code);
    log.unshift({ time: new Date().toLocaleTimeString(), mode: md, code, ok, msg });
    show(ok, ok ? (md === "verify" ? "VERIFIED" : "OK") : (md === "verify" ? "NOT VERIFIED" : "REJECTED"), msg);
    render();
  }
  let busy = Promise.resolve();
  inp.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const code = inp.value.trim(); inp.value = "";
    if (code) busy = busy.then(() => handle(code)); // keep scans in order even when scanning fast
  });
  $("#sclear").onclick = () => { log.length = 0; seen.clear(); render(); inp.focus(); };
  $("#sexport").onclick = () => saveText("time,mode,code,result,details\n" + log.map((x) => [x.time, x.mode, x.code, x.ok ? "OK" : "REJECTED", x.msg]
    .map((c) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n"), "scan-log.csv", "text/csv");
  inp.focus();
}

async function pageVerify(v) {
  v.innerHTML = `<h2>Verify &amp; Trace</h2><div class="card">
    <form id="vf"><label>Scan or paste a pack / case / pallet code
      <input name="code" placeholder="(01)…(17)…(10)…(21)…  or  (00)SSCC" autofocus required></label>
      <div class="row"><button>Verify</button><button type="button" class="ghost" id="traceBtn">Full trace</button></div></form>
    <div id="vout" class="result"></div></div>`;
  const out = $("#vout"), f = $("#vf");
  bind(f, async ({ code }) => {
    const r = await get("/verify?code=" + encodeURIComponent(code));
    out.innerHTML = `<p><span class="badge ${r.verified ? "b-ok" : "b-bad"}" style="font-size:14px">${r.verified ? "VERIFIED" : "NOT VERIFIED"}</span></p>
      ${r.reasons.length ? `<ul>${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${r.product ? `<p><b>${esc(r.product.name)} ${esc(r.product.strength || "")}</b> · Lot ${esc(r.lot)} · Expiry ${esc(r.expiry)} · Lot ${statusBadge(r.lot_status)}</p>` : ""}
      ${r.status ? `<p>Status ${statusBadge(r.status)} · Location: ${esc(locName(r.location))}</p>` : ""}`;
    f.code.select();
  });
  $("#traceBtn").onclick = async () => {
    try {
      const t = await get("/trace?code=" + encodeURIComponent(f.code.value));
      out.innerHTML = `<p><code>${esc(t.item.code)}</code> · ${esc(t.item.kind)} · ${statusBadge(t.item.status)} · ${esc(locName(t.item.location))}</p>
        ${t.contained_in.length ? `<p>Packed in: ${t.contained_in.map((a) => `<code>${esc(a.code)}</code> (${esc(a.kind)})`).join(" → ")}</p>` : ""}
        ${t.contains.count ? `<p>Contains ${t.contains.count.toLocaleString()} item(s)</p>` : ""}
        <h3>Chain of custody</h3>${table([["#", (h) => h.id], ["Time (UTC)", (h) => fmtTs(h.ts)], ["Step", (h) => esc(h.biz_step)],
          ["Location", (h) => esc(locName(h.location))], ["User", (h) => esc(h.actor)], ["Details", (h) => `<span class="mono">${summarize(h.data)}</span>`]], t.history)}`;
    } catch (e) { toast(e.message, true); }
  };
}

async function pageSerialize(v) {
  const m = await master();
  v.innerHTML = `<h2>Serialization</h2><div class="grid">
    <div class="card"><h3>Generate serial numbers</h3>
      <form id="gen"><label>Product<select name="gtin">${productOptions(m)}</select></label>
      <label>Lot<select name="lot"></select></label><label>Site<select name="location">${locationOptions(m)}</select></label>
      <label>Quantity<input name="count" type="number" min="1" max="1000000" value="1000" required></label><button>Generate &amp; register</button></form>
      <div id="genOut" class="result"></div></div>
    <div class="card"><h3>Import serial numbers</h3>
      <p class="muted">From a line system, L4 platform or CMO. Paste barcodes / EPC URIs (one per line) or upload a CSV with
      <code>gtin,serial,lot</code> columns (or a single <code>code</code> column). The whole file is rejected if any serial is invalid or already registered.</p>
      <form id="imp"><label>Site<select name="location">${locationOptions(m)}</select></label>
      <div class="row"><label>Default product<select name="gtin"><option value="">(from file)</option>${productOptions(m)}</select></label>
      <label>Default lot<input name="lot" placeholder="(from file)"></label></div>
      <label>Codes<textarea name="codes" placeholder="(01)00312345678906(10)LOT1(21)A1B2C3&#10;urn:epc:id:sgtin:0614141.812345.6789"></textarea></label>
      <label>…or CSV file<input type="file" name="file" accept=".csv,.txt,text/csv"></label><button>Import</button></form>
      <div id="impOut" class="result"></div></div></div>`;
  const gen = $("#gen"); linkLots(gen);
  bind(gen, async (d) => {
    const r = await post("/commission", { gtin: d.gtin, lot: d.lot, location: d.location, count: +d.count });
    $("#genOut").innerHTML = `<p><span class="badge b-ok">${r.count.toLocaleString()} serials registered</span></p>
      <div class="row"><button type="button" id="dlTxt" class="ghost">Download for printer (.txt)</button><button type="button" id="dlCsv" class="ghost">Download CSV</button></div>
      <textarea readonly>${esc(r.codes.slice(0, 50).join("\n"))}${r.count > 50 ? "\n…" : ""}</textarea>`;
    $("#dlTxt").onclick = () => saveText(r.codes.join("\n"), `serials-${d.gtin}-${d.lot}.txt`);
    $("#dlCsv").onclick = () => saveText("code\n" + r.codes.join("\n"), `serials-${d.gtin}-${d.lot}.csv`, "text/csv");
    state.cache.products = null;
  });
  bind($("#imp"), async (d, form) => {
    const file = form.file.files[0];
    let r;
    if (file) {
      const q = new URLSearchParams({ location: d.location }); if (d.gtin) q.set("gtin", d.gtin); if (d.lot) q.set("lot", d.lot);
      r = await post("/serials/import/csv?" + q, await file.text(), { csv: true });
    } else {
      r = await post("/serials/import", { location: d.location, lot: d.lot || null, codes: lines(d.codes) });
    }
    $("#impOut").innerHTML = `<p><span class="badge b-ok">${r.imported.toLocaleString()} serials imported</span></p>
      ${table([["GTIN", (x) => esc(x.gtin)], ["Lot", (x) => esc(x.lot)], ["Count", (x) => x.count.toLocaleString()]], r.lots)}`;
    form.reset();
  });
}

async function pagePack(v) {
  const m = await master();
  v.innerHTML = `<h2>Packing (aggregation)</h2><div class="grid">
    <div class="card"><h3>1. Create SSCC labels</h3><form id="cf">
      <div class="row"><label>Type<select name="kind"><option value="case">Case</option><option value="pallet">Pallet</option></select></label>
      <label>Quantity<input name="count" type="number" min="1" max="100000" value="1"></label></div>
      <label>Site<select name="location">${locationOptions(m)}</select></label>
      <div class="row"><label>GS1 company prefix<input name="company_prefix" required pattern="\\d{6,12}" placeholder="0614141"></label>
      <label>Extension digit<input name="extension" type="number" min="0" max="9" value="0"></label></div><button>Create</button></form>
      <div id="cOut" class="result"></div></div>
    <div class="card"><h3>2. Pack into container</h3><form id="af">
      <label>Parent (case / pallet SSCC)<input name="parent" required></label>
      <label>Children - scan one per line<textarea name="children" required></textarea></label><button>Pack</button></form>
      <div id="aOut" class="result"></div></div>
    <div class="card"><h3>Unpack</h3><form id="df">
      <label>Parent SSCC<input name="parent" required></label>
      <label>Children to remove (empty = unpack everything)<textarea name="children"></textarea></label><button>Unpack</button></form></div></div>`;
  bind($("#cf"), async (d) => {
    const r = await post("/containers", { kind: d.kind, location: d.location, company_prefix: d.company_prefix, extension: +d.extension, count: +d.count });
    $("#cOut").innerHTML = `<textarea readonly>${esc(r.ssccs.join("\n"))}</textarea>
      <button type="button" class="ghost" id="dlS">Download labels (.txt)</button>`;
    $("#dlS").onclick = () => saveText(r.ssccs.join("\n"), `sscc-${d.kind}s.txt`);
  });
  bind($("#af"), async (d, f) => {
    const r = await post("/aggregate", { parent: d.parent, children: lines(d.children) });
    toast(`Packed ${r.packed} item(s) into ${r.parent}`); f.children.value = ""; f.children.focus();
  });
  bind($("#df"), async (d, f) => {
    const kids = lines(d.children);
    const r = await post("/disaggregate", { parent: d.parent, children: kids.length ? kids : null });
    toast(`Unpacked ${r.unpacked} item(s)`); f.reset();
  });
}

async function pageShip(v) {
  const m = await master(), locs = locationOptions(m);
  v.innerHTML = `<h2>Shipping &amp; Receiving</h2><div class="grid">
    <div class="card"><h3>Ship</h3><form id="sf">
      <div class="row"><label>From<select name="from_gln">${locs}</select></label><label>To<select name="to_gln">${locs}</select></label></div>
      <label>Reference (order / ASN no.)<input name="reference"></label>
      <label>Outermost containers or packs - one per line<textarea name="codes" required></textarea></label><button>Ship</button></form>
      <div id="sOut" class="result"></div></div>
    <div class="card"><h3>Receive</h3><form id="rf">
      <label>At<select name="at_gln">${locs}</select></label><label>Reference<input name="reference"></label>
      <label>Scanned containers / packs<textarea name="codes" required></textarea></label><button>Receive</button></form></div>
    <div class="card"><h3>Dispense to patient</h3><form id="pf">
      <label>Pharmacy / hospital<select name="at_gln">${locs}</select></label>
      <label>Packs<textarea name="codes" required></textarea></label><button>Dispense</button></form></div></div>
    <div class="card"><h3>Recent shipments</h3><div id="shipList"></div></div>`;
  const refresh = async () => {
    const list = await get("/shipments?limit=50");
    $("#shipList").innerHTML = table([["#", (e) => e.id], ["Time (UTC)", (e) => fmtTs(e.ts)], ["From", (e) => esc(locName(e.location))],
      ["To", (e) => esc(locName(e.data.destination))], ["Reference", (e) => esc(e.data.reference)], ["Items", (e) => e.items.toLocaleString()], ["By", (e) => esc(e.actor)],
      ["EPCIS", (e) => `<button class="ghost small" data-epcis="${e.id}">Download</button>`]], list, "No shipments yet.");
  };
  $("#shipList").addEventListener("click", (e) => { const id = e.target.dataset.epcis; if (id) download(`/epcis/shipments/${id}`, `shipment-${id}.epcis.json`); });
  bind($("#sf"), async (d, f) => {
    const r = await post("/ship", { ...d, codes: lines(d.codes), reference: d.reference || null });
    $("#sOut").innerHTML = `<p><span class="badge b-ok">Shipment #${r.shipment_id}</span> ${r.shipped} container(s), ${r.items.toLocaleString()} items</p>
      <button type="button" class="ghost" id="dlE">Download EPCIS for receiver</button>`;
    $("#dlE").onclick = () => download(`/epcis/shipments/${r.shipment_id}`, `shipment-${r.shipment_id}.epcis.json`);
    f.codes.value = ""; refresh();
  });
  bind($("#rf"), async (d, f) => { const r = await post("/receive", { ...d, codes: lines(d.codes), reference: d.reference || null }); toast(`Received ${r.items.toLocaleString()} items`); f.codes.value = ""; });
  bind($("#pf"), async (d, f) => { const r = await post("/dispense", { at_gln: d.at_gln, codes: lines(d.codes) }); toast(`Dispensed ${r.dispensed} pack(s)`); f.codes.value = ""; });
  refresh();
}

async function pageQuality(v) {
  const m = await master(true), qa = can("qa");
  v.innerHTML = `<h2>Quality &amp; Recall</h2>
    <div class="card"><h3>Lots</h3><div id="lots"></div></div>
    <div id="recallOut"></div>
    <div class="grid ${qa ? "" : "hidden"}">
      <div class="card"><h3>New lot</h3><form id="lf"><label>Product<select name="gtin">${productOptions(m)}</select></label>
        <div class="row"><label>Lot / batch<input name="lot" required maxlength="20"></label><label>Expiry<input name="expiry" type="date" required></label></div>
        <label>Initial status<select name="status"><option value="quarantine">Quarantine</option><option value="released">Released</option></select></label><button>Create lot</button></form></div>
      <div class="card"><h3>Decommission packs</h3><form id="xf">
        <label>Reason<select name="reason"><option>destroyed</option><option>stolen</option><option>sample</option><option>expired</option></select></label>
        <label>Packs / containers<textarea name="codes" required></textarea></label><button class="danger">Decommission (signature)</button></form></div>
      <div class="card"><h3>Import EPCIS from CMO / partner</h3><form id="ef">
        <p class="muted">Commissioning and packing events are imported in one transaction. New lots are placed in quarantine.</p>
        <label>Receiving site<select name="location">${locationOptions(m)}</select></label>
        <label>EPCIS 2.0 JSON file<input type="file" name="file" accept=".json,.jsonld" required></label><button>Import</button></form></div></div>`;
  $("#lots").innerHTML = table([["Product", (l) => esc(l.product)], ["GTIN", (l) => `<code>${esc(l.gtin)}</code>`], ["Lot", (l) => esc(l.lot)], ["Expiry", (l) => esc(l.expiry)],
    ["Status", (l) => statusBadge(l.status)], ["Units", (l) => l.units.toLocaleString()],
    ["", (l) => `<div class="row">${qa && l.status !== "released" && l.status !== "recalled" ? btn("release", l, "Release") : ""}${qa && l.status === "released" ? btn("quarantine", l, "Quarantine") : ""}
      ${qa && l.status !== "recalled" ? btn("recall", l, "Recall", "danger") : ""}${btn("where", l, "Locate")}${btn("csv", l, "Serials CSV")}</div>`]], m.lots);
  $("#lots").addEventListener("click", (e) => lotAction(e.target.dataset, v));
  if (!qa) return;
  bind($("#lf"), async (d, f) => { await post("/lots", d); toast(`Lot ${d.lot} created`); pageQuality(v); });
  bind($("#xf"), async (d, f) => {
    const sig = await signature(`Decommission ${lines(d.codes).length} code(s) as ${d.reason}`, `Confirmed ${d.reason}`); if (!sig) return;
    const r = await post("/decommission", { codes: lines(d.codes), reason: d.reason, signature: sig }); toast(`${r.decommissioned} item(s) decommissioned`); f.reset();
  });
  bind($("#ef"), async (d, f) => {
    let doc; try { doc = JSON.parse(await f.file.files[0].text()); } catch (_) { throw new Error("File is not valid JSON"); }
    const r = await post("/epcis/import?location=" + encodeURIComponent(d.location), doc);
    toast(`Imported: ${Object.entries(r).map(([k, n]) => `${n} ${k.replace("_", " ")}`).join(", ") || "nothing"}`); pageQuality(v);
  });
}
const btn = (act, l, label, cls = "ghost") => `<button class="${cls} small" data-act="${act}" data-gtin="${esc(l.gtin)}" data-lot="${esc(l.lot)}">${label}</button>`;
async function lotAction({ act, gtin, lot }, v) {
  if (!act) return;
  const path = `/lots/${encodeURIComponent(gtin)}/${encodeURIComponent(lot)}`;
  try {
    if (act === "csv") return download(`/reports${path}/serials.csv`, `serials-${gtin}-${lot}.csv`);
    if (act === "where") return showWhere(await get(`${path}/whereabouts`));
    let reason = null;
    if (act === "recall") {
      const r = await dialog(`<h3>Recall lot ${esc(lot)}</h3><label>Reason<textarea name="reason" required minlength="3"></textarea></label>`);
      if (!r) return; reason = r.reason;
    }
    const meaning = { release: "Approved for release", quarantine: "Placed in quarantine", recall: "Approved recall" }[act];
    const sig = await signature(`${meaning}: lot ${lot}`, meaning); if (!sig) return;
    const status = { release: "released", quarantine: "quarantine", recall: "recalled" }[act];
    const res = await post(`${path}/status`, { status, reason, signature: sig });
    toast(`Lot ${lot} is now ${status}`);
    await pageQuality(v);
    if (status === "recalled") showWhere(res);
  } catch (e) { toast(e.message, true); }
}
function showWhere(w) {
  const out = $("#recallOut");
  out.innerHTML = `<div class="card"><h3>Where is lot ${esc(w.lot)}?</h3>
    ${table([["Location", (r) => esc(r.location_name || r.location || "-")], ["GLN", (r) => `<code>${esc(r.location)}</code>`], ["Status", (r) => statusBadge(r.status)], ["Units", (r) => r.units.toLocaleString()]], w.by_location)}
    <h3 style="margin-top:12px">Containers holding this lot</h3>
    ${table([["SSCC", (c) => `<code>${esc(c.code)}</code>`], ["Type", (c) => esc(c.kind)], ["Status", (c) => statusBadge(c.status)], ["Location", (c) => esc(locName(c.location))]], w.top_level_containers, "No containers - all units are loose.")}
    <p><button class="ghost small" id="whereCsv">Download CSV</button></p></div>`;
  $("#whereCsv").onclick = () => download(`/reports/lots/${encodeURIComponent(w.gtin)}/${encodeURIComponent(w.lot)}/whereabouts.csv`, `whereabouts-${w.gtin}-${w.lot}.csv`);
  out.scrollIntoView({ behavior: "smooth" });
}

async function pageMaster(v) {
  const m = await master(true), admin = can("admin");
  v.innerHTML = `<h2>Products &amp; Sites</h2>
    <div class="card"><h3>Products</h3>${table([["Name", (p) => esc(p.name)], ["Strength", (p) => esc(p.strength)], ["Form", (p) => esc(p.dosage_form)],
      ["GTIN", (p) => `<code>${esc(p.gtin)}</code>`], ["NDC", (p) => esc(p.ndc)], ["Manufacturer", (p) => esc(p.manufacturer)]], m.products)}
      ${admin ? `<form id="pf" class="result"><div class="row"><label>GTIN<input name="gtin" required></label><label>Name<input name="name" required></label>
        <label>Strength<input name="strength"></label><label>Dosage form<input name="dosage_form"></label><label>NDC<input name="ndc"></label>
        <label>Manufacturer<input name="manufacturer"></label><button>Add / update</button></div></form>` : ""}</div>
    <div class="card"><h3>Sites &amp; trading partners</h3>${table([["Name", (l) => esc(l.name)], ["Type", (l) => esc(l.kind)], ["GLN", (l) => `<code>${esc(l.gln)}</code>`], ["Address", (l) => esc(l.address)]], m.locations)}
      ${admin ? `<form id="lf" class="result"><div class="row"><label>GLN<input name="gln" required pattern="\\d{13}"></label><label>Name<input name="name" required></label>
        <label>Type<select name="kind">${["manufacturer", "cmo", "warehouse", "distributor", "pharmacy", "hospital", "other"].map((k) => opt(k, k)).join("")}</select></label>
        <label>Address<input name="address"></label><button>Add / update</button></div></form>` : ""}</div>`;
  if (!admin) return;
  const clean = (d) => Object.fromEntries(Object.entries(d).map(([k, x]) => [k, x === "" ? null : x]));
  bind($("#pf"), async (d) => { await post("/products", clean(d)); toast("Product saved"); pageMaster(v); });
  bind($("#lf"), async (d) => { await post("/locations", clean(d)); toast("Site saved"); pageMaster(v); });
}

async function pageAudit(v) {
  const qa = can("qa");
  v.innerHTML = `<h2>Audit Trail</h2>
    ${qa ? `<div class="card"><h3>Integrity &amp; export</h3><div class="row"><button id="chk">Verify hash chain</button><span id="chkOut"></span></div>
      <form id="exf" class="result"><div class="row"><label>From<input type="date" name="since"></label><label>To<input type="date" name="until"></label>
      <button type="button" class="ghost" id="exCsv">Audit trail CSV</button><button type="button" class="ghost" id="exEpcis">EPCIS events</button></div></form></div>` : ""}
    <div class="card"><h3>Events</h3><div class="row" style="margin-bottom:10px"><label>Filter by step<select id="stepSel"><option value="">All</option>
      ${["commissioning", "packing", "unpacking", "shipping", "receiving", "dispensing", "decommissioning", "lot_released", "lot_quarantine", "lot_recalled", "login", "auth_failed", "user_created", "user_updated"].map((s) => opt(s, s)).join("")}</select></label></div>
      <div id="evs"></div><button class="ghost" id="more">Load more</button></div>`;
  let before = null;
  const loadPage = async (reset) => {
    if (reset) { before = null; $("#evs").innerHTML = ""; }
    const step = $("#stepSel").value;
    const rows = await get(`/events?limit=100${before ? "&before_id=" + before : ""}${step ? "&biz_step=" + step : ""}`);
    if (rows.length) before = rows[rows.length - 1].id;
    $("#evs").insertAdjacentHTML("beforeend", eventsTable(rows));
    $("#more").classList.toggle("hidden", rows.length < 100);
  };
  $("#stepSel").onchange = () => loadPage(true).catch((e) => toast(e.message, true));
  $("#more").onclick = () => loadPage(false).catch((e) => toast(e.message, true));
  await loadPage(true);
  if (!qa) return;
  $("#chk").onclick = async () => {
    try {
      const r = await get("/audit/verify");
      $("#chkOut").innerHTML = r.valid ? `<span class="badge b-ok">Intact - ${r.events_checked.toLocaleString()} events verified</span>`
        : `<span class="badge b-bad">TAMPERING DETECTED at event #${r.broken_at_event}</span>`;
    } catch (e) { toast(e.message, true); }
  };
  const range = () => { const f = $("#exf"); const q = new URLSearchParams(); if (f.since.value) q.set("since", f.since.value); if (f.until.value) q.set("until", f.until.value); return q; };
  $("#exCsv").onclick = () => download("/reports/audit.csv?" + range(), "audit-trail.csv");
  $("#exEpcis").onclick = () => download("/epcis/events?" + range(), "events.epcis.json");
}

async function pageUsers(v) {
  const users = await get("/users");
  v.innerHTML = `<h2>Users</h2><div class="card">${table([["Username", (u) => esc(u.username)], ["Name", (u) => esc(u.full_name)],
    ["Role", (u) => `<select data-role="${esc(u.username)}">${Object.keys(ROLE_RANK).map((r) => opt(r, r, u.role)).join("")}</select>`],
    ["Status", (u) => u.locked ? statusBadge("locked") : u.active ? statusBadge("active") : `<span class="badge">inactive</span>`],
    ["", (u) => `<div class="row">${u.locked ? `<button class="ghost small" data-unlock="${esc(u.username)}">Unlock</button>` : ""}
      <button class="ghost small" data-toggle="${esc(u.username)}" data-active="${u.active}">${u.active ? "Deactivate" : "Activate"}</button>
      <button class="ghost small" data-reset="${esc(u.username)}">Reset password</button></div>`]], users)}</div>
    <div class="card"><h3>New user</h3><form id="uf"><div class="row"><label>Username<input name="username" required></label><label>Full name<input name="full_name" required></label>
      <label>Role<select name="role">${Object.keys(ROLE_RANK).map((r) => opt(r, r, "operator")).join("")}</select></label>
      <label>Temporary password<input name="password" type="password" required minlength="10"></label><button>Create</button></div>
      <p class="muted">Users must change the temporary password at first sign-in. Accounts are never deleted (audit attribution) - deactivate instead.</p></form></div>`;
  const patch = async (u, body) => { try { await api("PATCH", `/users/${encodeURIComponent(u)}`, body); toast("User updated"); route(); } catch (e) { toast(e.message, true); } };
  v.addEventListener("change", (e) => { if (e.target.dataset.role) patch(e.target.dataset.role, { role: e.target.value }); });
  v.addEventListener("click", async (e) => {
    const d = e.target.dataset;
    if (d.unlock) patch(d.unlock, { unlock: true });
    if (d.toggle) patch(d.toggle, { active: d.active !== "1" });
    if (d.reset) {
      const r = await dialog(`<h3>Reset password for ${esc(d.reset)}</h3><label>Temporary password<input name="pw" type="password" required minlength="10"></label>`);
      if (r) patch(d.reset, { temporary_password: r.pw, unlock: true });
    }
  });
  bind($("#uf"), async (d) => { await post("/users", d); toast(`User ${d.username} created`); route(); });
}

// ---------------------------------------------------------- shell / auth
function route() {
  const allowed = PAGES.filter((p) => can(p.role));
  const id = location.hash.slice(1) || "dashboard";
  const page = allowed.find((p) => p.id === id) || allowed[0];
  $("#nav").innerHTML = allowed.map((p) => `<a href="#${p.id}" class="${p === page ? "active" : ""}">${esc(p.title)}</a>`).join("");
  $("#nav").classList.remove("open");
  const v = $("#view"); const fresh = v.cloneNode(false); v.replaceWith(fresh); // drop old listeners
  fresh.innerHTML = '<p class="muted">Loading…</p>';
  master().catch(() => {}).finally(() => page.render(fresh).catch((e) => { fresh.innerHTML = `<p class="error">${esc(e.message)}</p>`; }));
}

function showApp() {
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  $("#who").textContent = `${state.user.full_name} · ${state.user.role}`;
  route();
}
function logout(expired = false) {
  if (!expired && state.token) post("/auth/logout").catch(() => {});
  state.token = null; state.user = null; state.cache = {}; store("tnt.token"); store("tnt.user");
  $("#app").classList.add("hidden"); $("#login").classList.remove("hidden");
  if (expired) $("#loginMsg").textContent = "Your session expired. Please sign in again.";
}

async function changePassword(username, forced) {
  const r = await dialog(`<h3>${forced ? "Set a new password to continue" : "Change password"}</h3>
    <label>Current password<input name="old" type="password" required autocomplete="current-password"></label>
    <label>New password (10+ characters, letters and digits)<input name="new1" type="password" required minlength="10" autocomplete="new-password"></label>
    <label>Repeat new password<input name="new2" type="password" required minlength="10" autocomplete="new-password"></label>`);
  if (!r) return false;
  if (r.new1 !== r.new2) { toast("Passwords do not match", true); return false; }
  try { await post("/auth/change-password", { username, old_password: r.old, new_password: r.new1 }); toast("Password changed"); return true; }
  catch (e) { toast(e.message, true); return false; }
}

bind($("#loginForm"), async (d, f) => {
  $("#loginMsg").textContent = "";
  try {
    const r = await post("/auth/login", d);
    if (r.must_change_password) {
      if (!(await changePassword(r.username, true))) return;
      toast("Password changed - sign in with your new password"); f.password.value = ""; return;
    }
    state.token = r.token; state.user = r; store("tnt.token", r.token); store("tnt.user", JSON.stringify(r));
    f.reset(); showApp();
  } catch (e) { $("#loginMsg").textContent = e.message; }
});
$("#logoutBtn").onclick = () => logout();
$("#pwBtn").onclick = () => changePassword(state.user.username, false);
$("#menuBtn").onclick = () => $("#nav").classList.toggle("open");
window.addEventListener("hashchange", () => state.user && route());

bind($("#setupForm"), async (d) => {
  $("#setupMsg").textContent = "";
  if (d.password !== d.password2) { $("#setupMsg").textContent = "Passwords do not match"; return; }
  try {
    await post("/setup", { username: d.username, full_name: d.full_name, password: d.password });
    $("#setupForm").classList.add("hidden"); $("#loginForm").classList.remove("hidden");
    $("#loginForm").username.value = d.username; $("#loginMsg").textContent = "";
    toast("Administrator created - please sign in");
  } catch (e) { $("#setupMsg").textContent = e.message; }
});

(async function init() {
  try {
    const st = await get("/setup/status");
    if (st.needs_setup) {
      $("#login").classList.remove("hidden");
      if (st.allowed) { $("#loginForm").classList.add("hidden"); $("#setupForm").classList.remove("hidden"); }
      else $("#loginMsg").textContent = "This installation has no users yet. Run first-time setup on the server.";
      return;
    }
  } catch (_) {}
  state.token = load("tnt.token");
  try { state.user = JSON.parse(load("tnt.user") || "null"); } catch (_) { state.user = null; }
  if (state.token && state.user) {
    try { await get("/auth/me"); return showApp(); } catch (_) { /* fall through to login */ }
  }
  logout(true); $("#loginMsg").textContent = "";
})();
