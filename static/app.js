/* Card Binder – front end. Plain JS, no build step. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  status: null,
  view: "scan",
  stream: null,
  queue: [],
  busy: false,
  session: [],          // {cardId, name, value, action, undone}
  rankBy: "value",
};

const FINISHES = {
  normal: "Normal",
  holo: "Holo",
  reverse_holo: "Reverse holo / mirror",
  poke_ball_pattern: "Poké Ball mirror",
  master_ball_pattern: "Master Ball mirror",
};

const eur = new Intl.NumberFormat(navigator.language || "en", { style: "currency", currency: "EUR" });
const money = (v) => (v === null || v === undefined || v === "" ? null : eur.format(Number(v)));

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers["Content-Type"] = "application/json";
    delete opts.json;
  }
  const res = await fetch(path, opts);
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return data;
}

const MAX_TOASTS = 2;

function toast(message, { action, onAction, error = false, timeout } = {}) {
  timeout = timeout ?? (action ? 6000 : 3500);
  const box = $("#toasts");
  while (box.children.length >= MAX_TOASTS) box.firstElementChild.remove();
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.innerHTML = `<span>${esc(message)}</span>`;
  if (action) {
    const btn = document.createElement("button");
    btn.textContent = action;
    btn.addEventListener("click", () => { onAction(); el.remove(); });
    el.append(btn);
  }
  box.append(el);
  setTimeout(() => el.remove(), timeout);
}

function safeUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url) ? url : null;
}

function cardImage(card) {
  if (card.image_url) return card.image_url;
  if (card.photo) return `/photos/${encodeURIComponent(card.photo)}`;
  return null;
}

function displayName(card) {
  return card.name_en || card.name_jp || "Unknown card";
}

function setLine(card) {
  const parts = [];
  if (card.set_code) parts.push(card.set_code);
  if (card.number) parts.push(card.set_total ? `${card.number}/${card.set_total}` : card.number);
  if (card.rarity) parts.push(card.rarity);
  return parts.join(" · ");
}

function imgTag(src, alt, cls = "") {
  if (!src) return `<div class="placeholder">No image</div>`;
  return `<img src="${esc(src)}" alt="${esc(alt)}" class="${cls}" loading="lazy"
           onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'placeholder',textContent:'No image'}))">`;
}

/* ================================================================== tabs */
function showView(view) {
  state.view = view;
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === view)));
  $$(".view").forEach((v) => { v.hidden = v.id !== `view-${view}`; });
  if (view !== "scan") stopCamera();
  if (view === "collection") loadCollection();
  if (view === "ranking") loadRanking();
  try { localStorage.setItem("view", view); } catch { /* storage unavailable */ }
}

/* ================================================================ status */
async function loadStatus() {
  try {
    state.status = await api("/api/status");
  } catch (e) {
    state.status = null;
  }
  const banner = $("#setup-banner");
  if (state.status && !state.status.claude_configured) {
    banner.hidden = false;
    banner.textContent = "Card recognition is off: add your ANTHROPIC_API_KEY to the .env file and restart the server. You can still browse your collection.";
  }
}

/* ================================================================ camera */
function setupCamera() {
  const canLive = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) && window.isSecureContext;
  $("#btn-camera").hidden = !canLive;
  if (!canLive) {
    const hint = $("#camera-hint");
    hint.hidden = false;
    hint.textContent = "Live camera preview needs HTTPS or localhost. “Take photo” still opens your phone camera.";
  }
}

async function startCamera() {
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
  } catch (e) {
    toast(`Could not open the camera: ${e.message}`, { error: true });
    return;
  }
  const video = $("#video");
  video.srcObject = state.stream;
  $("#camera").hidden = false;
  $("#btn-capture").hidden = false;
  $("#btn-capture").disabled = false;
  $("#btn-camera").textContent = "Stop camera";
}

function stopCamera() {
  if (!state.stream) return;
  state.stream.getTracks().forEach((t) => t.stop());
  state.stream = null;
  $("#camera").hidden = true;
  $("#btn-capture").hidden = true;
  $("#btn-camera").textContent = "Start camera";
}

function captureFrame() {
  const video = $("#video");
  if (!video.videoWidth) return;
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  canvas.toBlob((blob) => enqueue([new File([blob], "capture.jpg", { type: "image/jpeg" })]), "image/jpeg", 0.92);
}

/* ================================================================== scan */
function enqueue(files) {
  state.queue.push(...files);
  if (!state.busy) processQueue();
}

async function processQueue() {
  state.busy = true;
  let index = 0;
  const total = () => index + state.queue.length;
  while (state.queue.length) {
    const file = state.queue.shift();
    index += 1;
    setScanStatus(total() > 1 ? `Reading card ${index} of ${total()}…` : "Reading card…", true);
    try {
      const form = new FormData();
      form.append("file", file);
      const result = await api("/api/scan", { method: "POST", body: form });
      await handleScanResult(result);
      setScanStatus("");
    } catch (e) {
      setScanStatus(e.message, false, true);
    }
  }
  state.busy = false;
}

function setScanStatus(message, spinning = false, error = false) {
  const el = $("#scan-status");
  el.className = "scan-status" + (error ? " error" : "");
  el.innerHTML = message ? `${spinning ? '<span class="spinner"></span>' : ""}<span>${esc(message)}</span>` : "";
}

async function handleScanResult(result) {
  if (!result.cards.length) {
    setScanStatus(result.image_problem || "No card found in that photo.", false, true);
    return;
  }
  const quick = $("#quick-mode").checked;
  for (const entry of result.cards.slice().reverse()) {
    if (quick && entry.confidence === "high" && entry.matched) {
      try {
        await saveCard(entry, entry.duplicate ? entry.duplicate.id : null);
        continue;
      } catch (e) {
        toast(e.message, { error: true });
      }
    }
    $("#scan-results").prepend(renderResult(entry));
  }
  if (result.image_problem) toast(result.image_problem);
}

function renderResult(entry) {
  const card = { ...entry.card };
  const el = document.createElement("article");
  el.className = "result";

  const draw = () => {
    const dup = entry.duplicate;
    const price = money(card.price_eur);
    el.innerHTML = `
      <div class="result-images">
        ${card.image_url ? imgTag(card.image_url, "Official card image") : ""}
        ${card.photo ? imgTag(`/photos/${encodeURIComponent(card.photo)}`, "Your photo", card.image_url ? "photo" : "") : ""}
      </div>
      <div>
        <div class="result-title">
          <strong>${esc(displayName(card))}</strong>
          <span class="jp muted" lang="ja">${esc(card.name_jp)}</span>
        </div>
        <div class="badges">
          <span class="badge ${entry.confidence === "high" ? "good" : entry.confidence === "low" ? "accent" : "warn"}">
            ${esc(entry.confidence || "manual")} confidence</span>
          ${entry.matched ? `<span class="badge good">Found in catalogue${card.set_name ? `: ${esc(card.set_name)}` : ""}</span>`
                          : `<span class="badge warn">Not matched in catalogue</span>`}
          ${dup ? `<span class="badge accent">You already have ×${dup.quantity}</span>` : ""}
        </div>
        <div class="fields">
          <label class="span-3">Japanese name<input type="text" name="name_jp" lang="ja" value="${esc(card.name_jp)}"></label>
          <label class="span-3">English name<input type="text" name="name_en" value="${esc(card.name_en)}"></label>
          <label class="span-2">Set code<input type="text" name="set_code" value="${esc(card.set_code)}" autocapitalize="characters"></label>
          <label class="span-2">Number<input type="text" name="number" value="${esc(card.number)}" inputmode="numeric"></label>
          <label class="span-2">Rarity<input type="text" name="rarity" value="${esc(card.rarity)}"></label>
          <label class="span-3 m-half">Copies<input type="number" name="quantity" min="1" max="999" value="${esc(card.quantity || 1)}"></label>
          <label class="span-3">Finish<select name="finish">
            ${Object.entries(FINISHES).map(([k, v]) => `<option value="${k}" ${card.finish === k ? "selected" : ""}>${v}</option>`).join("")}
          </select></label>
          ${price || dup ? "" : `<label class="span-3">Your price (€, optional)<input type="number" name="manual_price"
              min="0" step="0.01" inputmode="decimal" value="${esc(card.manual_price ?? "")}"></label>`}
        </div>
        <div class="price-line">
          ${price ? `<span class="price">${price}</span><span class="muted small">${esc(card.price_source || "")}</span>`
                  : `<span class="price none">No price yet</span>`}
          <a class="small" href="${esc(entry.cardmarket_search_url)}" target="_blank" rel="noopener">Check on Cardmarket ↗</a>
        </div>
        ${entry.catalog_error ? `<p class="notes">${esc(entry.catalog_error)}</p>` : ""}
        ${entry.notes ? `<p class="notes">Note: ${esc(entry.notes)}</p>` : ""}
        <div class="actions">
          ${dup ? `<button class="btn primary" data-act="merge">Add copy (have ×${dup.quantity})</button>
                   <button class="btn" data-act="new">Save as separate card</button>`
                : `<button class="btn primary" data-act="new">Add to collection</button>`}
          <button class="btn ghost" data-act="rematch">Re-match</button>
          <button class="btn ghost" data-act="discard">Discard</button>
        </div>
      </div>`;
  };

  const readFields = () => {
    $$("input, select", el).forEach((input) => {
      if (input.name === "manual_price") {
        card.manual_price = input.value === "" ? null : Number(input.value);
      } else {
        card[input.name] = input.type === "number" ? Number(input.value || 1) : input.value.trim();
      }
    });
  };

  el.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    readFields();
    const act = btn.dataset.act;
    if (act === "discard") { el.remove(); return; }
    btn.disabled = true;
    try {
      if (act === "rematch") {
        const fresh = await api("/api/lookup", { method: "POST", json: card });
        const keep = { photo: card.photo, quantity: card.quantity };
        for (const key of Object.keys(card)) if (key.startsWith("price_")) delete card[key];
        Object.assign(card, fresh.card, keep);
        Object.assign(entry, { matched: fresh.matched, duplicate: fresh.duplicate,
                               catalog_error: fresh.catalog_error, cardmarket_search_url: fresh.cardmarket_search_url });
        draw();
        toast(fresh.matched ? "Found it in the catalogue." : "Still no catalogue match. Check the set code and number.");
        return;
      }
      entry.card = card;
      await saveCard(entry, act === "merge" ? entry.duplicate.id : null);
      el.remove();
    } catch (e) {
      toast(e.message, { error: true });
      btn.disabled = false;
    }
  });

  draw();
  return el;
}

async function saveCard(entry, mergeInto) {
  const payload = { ...entry.card };
  if (mergeInto) payload.merge_into = mergeInto;
  const res = await api("/api/cards", { method: "POST", json: payload });
  const copies = Number(entry.card.quantity || 1);
  const item = {
    cardId: res.card.id,
    name: displayName(res.card),
    value: res.card.value,
    copies,
    action: res.action,
    undone: false,
  };
  state.session.unshift(item);
  renderSession();
  const what = res.action === "merged" ? `+${copies} ${item.name} (now ×${res.card.quantity})` : `Added ${item.name}`;
  toast(`${what}${item.value ? ` · ${money(item.value)}` : ""}`, { action: "Undo", onAction: () => undo(item) });
}

async function undo(item) {
  if (item.undone) return;
  try {
    if (item.action === "merged") {
      await api(`/api/cards/${item.cardId}/quantity?delta=${-item.copies}`, { method: "POST" });
    } else {
      await api(`/api/cards/${item.cardId}`, { method: "DELETE" });
    }
    item.undone = true;
    renderSession();
    toast(`Removed ${item.name}`);
  } catch (e) {
    toast(e.message, { error: true });
  }
}

function renderSession() {
  const live = state.session.filter((s) => !s.undone);
  $("#session").hidden = state.session.length === 0;
  const count = live.reduce((n, s) => n + s.copies, 0);
  const value = live.reduce((n, s) => n + (Number(s.value) || 0) * s.copies, 0);
  $("#session-summary").textContent = `${count} card${count === 1 ? "" : "s"} · ${money(value)}`;
  $("#session-log").innerHTML = state.session.map((s, i) => `
    <li class="${s.undone ? "undone" : ""}">
      <span>${esc(s.name)}${s.copies > 1 ? ` ×${s.copies}` : ""}${s.action === "merged" ? ' <span class="muted small">(extra copy)</span>' : ""}</span>
      <span>${s.value ? money(s.value) : '<span class="muted">—</span>'}
        ${s.undone ? "" : `<button class="btn small ghost" data-undo="${i}">Undo</button>`}</span>
    </li>`).join("");
}

/* ============================================================ collection */
let searchTimer = null;

async function loadCollection() {
  const q = $("#search").value.trim();
  const sort = $("#sort").value;
  let cards = [];
  try {
    cards = await api(`/api/cards?sort=${encodeURIComponent(sort)}&q=${encodeURIComponent(q)}`);
  } catch (e) {
    toast(e.message, { error: true });
  }
  $("#collection-empty").hidden = cards.length > 0;
  $("#collection-empty").textContent = q ? "No cards match that search." : "No cards yet. Open a pack and start scanning.";
  $("#collection-grid").innerHTML = cards.map((c) => `
    <button class="tile" data-id="${c.id}">
      <div class="tile-img">
        ${imgTag(cardImage(c), displayName(c))}
        ${c.quantity > 1 ? `<span class="qty">×${c.quantity}</span>` : ""}
      </div>
      <div class="tile-body">
        <span class="tile-name">${esc(displayName(c))}</span>
        <span class="tile-jp jp" lang="ja">${esc(c.name_jp)}</span>
        <span class="tile-meta">${esc(setLine(c))}</span>
        ${c.value !== null ? `<span class="tile-price">${money(c.value)}</span>` : `<span class="tile-price none">No price</span>`}
      </div>
    </button>`).join("");
}

async function refreshAllPrices() {
  const btn = $("#btn-refresh-prices");
  btn.disabled = true;
  try {
    let s = await api("/api/prices/refresh", { method: "POST" });
    while (s.running) {
      $("#refresh-status").textContent = `Updating prices… ${s.done} / ${s.total}`;
      await new Promise((r) => setTimeout(r, 1000));
      s = await api("/api/prices/refresh");
    }
    $("#refresh-status").textContent =
      `Prices updated for ${s.updated} card${s.updated === 1 ? "" : "s"}` +
      (s.no_price ? ` · ${s.no_price} without a catalogue price (open the card, check Cardmarket and enter your price)` : "") +
      (s.message ? ` · ${s.message}` : "");
    loadCollection();
  } catch (e) {
    toast(e.message, { error: true });
  } finally {
    btn.disabled = false;
  }
}

/* ================================================================ detail */
async function openDetail(id) {
  let card;
  try {
    card = await api(`/api/cards/${id}`);
  } catch (e) {
    toast(e.message, { error: true });
    return;
  }
  renderDetail(card);
  const dlg = $("#detail");
  if (!dlg.open) dlg.showModal();
}

function renderDetail(card) {
  const body = $("#detail-body");
  const market = money(card.price_eur);
  const value = money(card.value);
  const photo = card.photo ? `/photos/${encodeURIComponent(card.photo)}` : null;
  const history = (card.history || []).slice().reverse();

  body.innerHTML = `
    <div class="detail-grid">
      <div class="detail-images">
        ${card.image_url ? `<figure>${imgTag(card.image_url, "Official card image")}<figcaption>Official image</figcaption></figure>` : ""}
        ${photo ? `<figure>${imgTag(photo, "Your photo")}<figcaption>Your photo</figcaption></figure>` : ""}
        ${!card.image_url && !photo ? `<div class="placeholder">No image</div>` : ""}
      </div>
      <div>
        <h2>${esc(displayName(card))}</h2>
        <div class="jp muted" lang="ja">${esc(card.name_jp)}</div>
        <div class="muted small">${esc(setLine(card))}${card.set_name ? ` · ${esc(card.set_name)}` : ""}</div>

        <div class="price-panel">
          <div class="price-line" style="margin:0">
            ${value ? `<span class="price">${value}</span>` : `<span class="price none">No price yet</span>`}
            <span class="muted small">${card.manual_price !== null ? "your price" : esc(card.price_source || "")}</span>
          </div>
          ${market ? `<div class="price-grid">
            <div>Trend<b>${money(card.price_trend) || "—"}</b></div>
            <div>30-day avg<b>${money(card.price_avg30) || "—"}</b></div>
            <div>Average<b>${money(card.price_avg) || "—"}</b></div>
            <div>From<b>${money(card.price_low) || "—"}</b></div>
          </div>` : ""}
          ${card.price_note ? `<p class="notes">${esc(card.price_note)}</p>` : ""}
          ${card.price_updated_at ? `<p class="hint">Updated ${esc(new Date(card.price_updated_at).toLocaleString())}</p>` : ""}
          <div class="actions">
            <button class="btn small" data-act="price-catalog">Update price</button>
            <a class="btn small ghost" href="${esc(safeUrl(card.cardmarket_url) || safeUrl(card.price_url) || card.cardmarket_search_url)}" target="_blank" rel="noopener">Open Cardmarket ↗</a>
          </div>
        </div>

        <div class="qty-control">
          <span class="muted small">Copies</span>
          <button class="icon-btn" data-act="dec" aria-label="Remove a copy">−</button>
          <output>${card.quantity}</output>
          <button class="icon-btn" data-act="inc" aria-label="Add a copy">+</button>
        </div>

        <form class="fields" id="detail-form" style="margin-top:14px">
          <label class="span-3">Japanese name<input type="text" name="name_jp" lang="ja" value="${esc(card.name_jp)}"></label>
          <label class="span-3">English name<input type="text" name="name_en" value="${esc(card.name_en)}"></label>
          <label class="span-2">Set code<input type="text" name="set_code" value="${esc(card.set_code)}"></label>
          <label class="span-2">Number<input type="text" name="number" value="${esc(card.number)}"></label>
          <label class="span-2">Rarity<input type="text" name="rarity" value="${esc(card.rarity)}"></label>
          <label class="span-3">Finish<select name="finish">
            ${Object.entries(FINISHES).map(([k, v]) => `<option value="${k}" ${card.finish === k ? "selected" : ""}>${v}</option>`).join("")}
          </select></label>
          <label class="span-3">Your price (€, overrides market)<input type="number" name="manual_price" min="0" step="0.01" value="${card.manual_price ?? ""}"></label>
          <label class="span-6">Cardmarket page (optional, exact product URL)<input type="url" name="cardmarket_url" value="${esc(card.cardmarket_url)}" placeholder="https://www.cardmarket.com/en/Pokemon/Products/Singles/…"></label>
          <label class="span-6">Notes<textarea name="notes">${esc(card.notes)}</textarea></label>
        </form>

        <div class="actions">
          <button class="btn primary" data-act="save">Save changes</button>
          <button class="btn" data-act="rematch">Re-match catalogue</button>
          <button class="btn ghost danger" data-act="delete">Delete</button>
        </div>

        ${history.length ? `<details><summary>Price history (${history.length})</summary>
          <table class="history">${history.map((h) => `<tr><td>${esc(new Date(h.fetched_at).toLocaleDateString())}</td>
            <td class="muted">${esc(h.source)}</td><td>${money(h.price_eur)}</td></tr>`).join("")}</table></details>` : ""}
      </div>
    </div>`;

  body.onclick = async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    ev.preventDefault();
    const act = btn.dataset.act;
    btn.disabled = true;
    try {
      if (act === "inc" || act === "dec") {
        renderDetail({ ...(await api(`/api/cards/${card.id}/quantity?delta=${act === "inc" ? 1 : -1}`, { method: "POST" })), history: card.history });
      } else if (act === "save") {
        const form = $("#detail-form");
        const data = Object.fromEntries(new FormData(form).entries());
        data.manual_price = data.manual_price === "" ? null : Number(data.manual_price);
        data.cardmarket_url = data.cardmarket_url || null;
        const saved = await api(`/api/cards/${card.id}`, { method: "PATCH", json: data });
        renderDetail({ ...saved, history: card.history });
        toast("Saved.");
      } else if (act === "rematch") {
        const res = await api(`/api/cards/${card.id}/rematch`, { method: "POST" });
        toast(res.matched ? "Linked to the catalogue." : "No catalogue match. Check set code and number, save, then try again.");
        await openDetail(card.id);
      } else if (act === "price-catalog") {
        const res = await api(`/api/cards/${card.id}/price`, { method: "POST" });
        if (!res.ok) toast(res.message || "No price found.", { error: true, timeout: 9000 });
        else toast(`Price: ${money(res.card.price_eur)}`);
        await openDetail(card.id);
      } else if (act === "delete") {
        if (!confirm(`Delete ${displayName(card)} from your collection?`)) { btn.disabled = false; return; }
        await api(`/api/cards/${card.id}`, { method: "DELETE" });
        $("#detail").close();
        toast("Deleted.");
      }
    } catch (e) {
      toast(e.message, { error: true });
      btn.disabled = false;
    }
  };
}

/* =============================================================== ranking */
async function loadRanking() {
  let stats, cards;
  try {
    [stats, cards] = await Promise.all([api("/api/stats"), api(`/api/cards?sort=${state.rankBy}`)]);
  } catch (e) {
    toast(e.message, { error: true });
    return;
  }
  $("#stats").innerHTML = `
    <div class="stat"><div class="stat-label">Collection value</div><div class="stat-value">${money(stats.total_value)}</div></div>
    <div class="stat"><div class="stat-label">Cards</div><div class="stat-value">${stats.total_cards}</div></div>
    <div class="stat"><div class="stat-label">Different cards</div><div class="stat-value">${stats.unique_cards}</div></div>
    <div class="stat"><div class="stat-label">With a price</div><div class="stat-value">${stats.priced_cards} / ${stats.unique_cards}</div></div>`;
  const priced = cards.filter((c) => c.value !== null);
  $("#ranking-empty").hidden = priced.length > 0;
  $("#ranking").innerHTML = priced.map((c, i) => {
    const total = Number(c.value) * c.quantity;
    const headline = state.rankBy === "total" ? total : c.value;
    const sub = state.rankBy === "total"
      ? `${money(c.value)} × ${c.quantity}`
      : (c.quantity > 1 ? `×${c.quantity} = ${money(total)}` : "");
    return `
      <li class="rank-row" data-id="${c.id}">
        <span class="rank-no">${i + 1}</span>
        <span class="thumb">${imgTag(cardImage(c), displayName(c))}</span>
        <span class="rank-names">
          <span class="tile-name" style="display:block">${esc(displayName(c))}</span>
          <span class="tile-jp jp" lang="ja" style="display:block">${esc(c.name_jp)}</span>
          <span class="tile-meta">${esc(setLine(c))}</span>
        </span>
        <span class="rank-value"><strong>${money(headline)}</strong><span class="muted small">${sub}</span></span>
      </li>`;
  }).join("");
}

/* ================================================================= wiring */
function init() {
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

  $("#btn-camera").addEventListener("click", () => (state.stream ? stopCamera() : startCamera()));
  $("#btn-capture").addEventListener("click", captureFrame);
  for (const id of ["#input-photo", "#input-upload"]) {
    $(id).addEventListener("change", (ev) => {
      enqueue([...ev.target.files]);
      ev.target.value = "";
    });
  }
  const quick = $("#quick-mode");
  try { quick.checked = localStorage.getItem("quick") === "1"; } catch { /* ignore */ }
  quick.addEventListener("change", () => {
    try { localStorage.setItem("quick", quick.checked ? "1" : "0"); } catch { /* ignore */ }
  });

  $("#session-log").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-undo]");
    if (btn) undo(state.session[Number(btn.dataset.undo)]);
  });

  $("#search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadCollection, 200);
  });
  $("#sort").addEventListener("change", () => {
    loadCollection();
    try { localStorage.setItem("sort", $("#sort").value); } catch { /* ignore */ }
  });
  try { const s = localStorage.getItem("sort"); if (s) $("#sort").value = s; } catch { /* ignore */ }
  $("#btn-refresh-prices").addEventListener("click", refreshAllPrices);

  $("#collection-grid").addEventListener("click", (ev) => {
    const tile = ev.target.closest(".tile");
    if (tile) openDetail(tile.dataset.id);
  });
  $("#ranking").addEventListener("click", (ev) => {
    const row = ev.target.closest(".rank-row");
    if (row) openDetail(row.dataset.id);
  });
  $$(".segmented button").forEach((b) => b.addEventListener("click", () => {
    state.rankBy = b.dataset.rank;
    $$(".segmented button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    loadRanking();
  }));
  $("#detail").addEventListener("close", () => {
    if (state.view === "collection") loadCollection();
    if (state.view === "ranking") loadRanking();
  });

  setupCamera();
  loadStatus();
  let view = "scan";
  try { view = localStorage.getItem("view") || "scan"; } catch { /* ignore */ }
  showView(["scan", "collection", "ranking"].includes(view) ? view : "scan");
}

document.addEventListener("DOMContentLoaded", init);
