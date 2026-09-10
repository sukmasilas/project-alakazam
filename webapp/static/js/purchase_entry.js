/* Purchase Entry — the core novel flow. Client-side state (`draftPurchase`)
 * mirrors docs/design/mockup.html's model closely, but every real
 * calculation the mockup did with static in-page arrays now either (a)
 * stays a pure client-side preview (allocation math, via calc.js — same
 * algorithm the server uses) for live responsiveness, or (b) is backed by
 * a real API call for anything that must reflect actual DB state (item
 * search, SKU/serial existence, SKU/serial candidate generation).
 *
 * Save always POSTs the raw draft to /api/purchases, which calls the real
 * inventory.purchases.save_purchase() — the server re-derives allocation,
 * SKU/serial generation, and uniqueness from scratch and is the only real
 * gate. Nothing computed in this file is ever trusted as authoritative.
 */

let categoriesCache = [];
let draftPurchase = null;
let lineIdSeq = 0;

function todayISO() { return new Date().toISOString().slice(0, 10); }

function emptyDraft() {
  return {
    date: todayISO(),
    vendor: "",
    totalAmountPaid: 0,
    shippingMode: "none", poolAmount: 0, poolMethod: "equal",
    lumpSum: null, // { amount, method }
    invoice: null, // { fileName, ocrStatus, parsedFields }
    lines: [],
  };
}

function mkLine(partial) {
  return Object.assign(
    {
      id: ++lineIdSeq,
      skuMode: "existing", sku: "", identityMode: "fungible",
      newSku: null, // { code, name, category, identity, skuTouched }
      qty: 1,
      pricingMode: "direct", priceEntryMode: "per_unit", priceValue: 0,
      lumpsumWeightKg: 0, lumpsumValue: 0,
      shipsSeparately: false, manualShipping: 0, shippingWeightKg: 0,
      serialPanelOpen: false, serialUnits: [], // [{serial, cost, costTouched, photoDataUrl, photoFileName}]
    },
    partial
  );
}

function draftLineIdentity(l) {
  return l.skuMode === "new" ? ((l.newSku && l.newSku.identity) || "fungible") : (l.identityMode || "fungible");
}
function skuLabelFor(l) {
  if (l.skuMode === "new") return (l.newSku && l.newSku.code) ? l.newSku.code + " (new)" : "New SKU — fill in below";
  return l.sku || "";
}

async function initPurchaseEntryPage() {
  categoriesCache = (await apiGet("/api/categories")) || [];
  draftPurchase = emptyDraft();
  draftPurchase.lines.push(mkLine({}));

  document.getElementById("pe-date").value = draftPurchase.date;
  document.getElementById("pe-date").oninput = e => { draftPurchase.date = e.target.value; };
  document.getElementById("pe-vendor").oninput = e => { draftPurchase.vendor = e.target.value; };
  document.getElementById("pe-total-paid").oninput = e => { draftPurchase.totalAmountPaid = Number(e.target.value) || 0; updateComputedDisplays(); };
  document.getElementById("pe-add-line").onclick = () => { draftPurchase.lines.push(mkLine({})); rebuildAllLineRows(); updateComputedDisplays(); };
  document.getElementById("save-purchase-btn").onclick = savePurchase;

  renderInvoiceBox();
  renderShippingBox();
  renderLumpsumBox();
  rebuildAllLineRows();
  updateComputedDisplays();
}

/* ---- Invoice uploader (simulated OCR — client-side only, no real Drive/OCR;
 * see ui-ux-design.md and CLAUDE.md's note that this stays simulated in
 * Milestone 3). ---- */

const SAMPLE_INVOICES = {
  "invoice-toko-grosir-jaya.jpg": { ocrStatus: "parsed", parsedFields: { date: null, vendor: "Toko Grosir Jaya", amount: null } },
  "watch-receipt-handwritten.jpg": { ocrStatus: "needs_review" },
};
function simulateOcr(fileName) {
  const known = SAMPLE_INVOICES[(fileName || "").toLowerCase()];
  if (known) return JSON.parse(JSON.stringify(known));
  const lower = (fileName || "").toLowerCase();
  if (/invoice|receipt-clear|toko|struk/.test(lower) && !/handwritten|blur|photo/.test(lower)) {
    return { ocrStatus: "parsed", parsedFields: { date: draftPurchase.date, vendor: draftPurchase.vendor || "(vendor read from invoice)", amount: draftPurchase.totalAmountPaid || 0 } };
  }
  return { ocrStatus: "needs_review" };
}
function renderInvoiceBox() {
  const inv = draftPurchase.invoice;
  let html = `<div class="control-box"><div class="control-box-title">Invoice / Proof of Transfer</div>`;
  if (!inv) {
    html += `<div class="invoice-empty">No file attached yet. This would upload to Google Drive in the real system (Drive holds the file, Postgres holds the structured record) — simulated client-side only here.</div>
    <div class="invoice-actions">
      <label class="btn btn-sm" style="display:inline-block;">Choose file… <input type="file" id="pe-invoice-file" style="display:none;"></label>
      <span style="color:var(--text-dim);font-size:12px;">or try a sample:</span>
      <button type="button" class="btn btn-sm" id="pe-sample-clear">📄 Clear invoice scan</button>
      <button type="button" class="btn btn-sm" id="pe-sample-handwritten">🧾 Handwritten receipt</button>
    </div>`;
  } else {
    html += `<div class="invoice-attached"><div><b>📎 ${escapeAttr(inv.fileName)}</b> <button class="link-btn" id="pe-invoice-remove" style="margin-left:10px;">Remove</button></div>`;
    if (inv.ocrStatus === "parsed") {
      html += `<div class="badge badge-green" style="margin-top:6px;">Parsed</div>
      <div class="ocr-fields">Read from invoice: <b>${escapeAttr(inv.parsedFields.date || "")}</b> &middot; <b>${escapeAttr(inv.parsedFields.vendor || "")}</b> &middot; <b>${inv.parsedFields.amount != null ? fmtIDR(inv.parsedFields.amount) : ""}</b> — auto-filled into the header above, still editable.</div>`;
    } else {
      html += `<div class="badge badge-amber" style="margin-top:6px;">Needs Review</div>
      <div class="ocr-fields">This invoice couldn't be read automatically — this proof of transfer doesn't contain enough information to auto-fill. Please fill in Date, Vendor, and Total Amount Paid manually above.</div>`;
    }
    html += `</div>`;
  }
  html += `</div>`;
  document.getElementById("pe-invoice-box").innerHTML = html;

  const fileInput = document.getElementById("pe-invoice-file");
  if (fileInput) fileInput.onchange = () => { const f = fileInput.files && fileInput.files[0]; if (f) attachInvoiceFile(f.name); };
  const sampleClear = document.getElementById("pe-sample-clear");
  if (sampleClear) sampleClear.onclick = () => attachInvoiceFile("invoice-toko-grosir-jaya.jpg");
  const sampleHandwritten = document.getElementById("pe-sample-handwritten");
  if (sampleHandwritten) sampleHandwritten.onclick = () => attachInvoiceFile("watch-receipt-handwritten.jpg");
  const removeBtn = document.getElementById("pe-invoice-remove");
  if (removeBtn) removeBtn.onclick = () => { draftPurchase.invoice = null; renderInvoiceBox(); };
}
function attachInvoiceFile(fileName) {
  const result = simulateOcr(fileName);
  draftPurchase.invoice = Object.assign({ fileName }, result);
  if (result.ocrStatus === "parsed" && result.parsedFields) {
    draftPurchase.date = result.parsedFields.date || draftPurchase.date;
    draftPurchase.vendor = result.parsedFields.vendor || draftPurchase.vendor;
    draftPurchase.totalAmountPaid = result.parsedFields.amount != null ? result.parsedFields.amount : draftPurchase.totalAmountPaid;
    document.getElementById("pe-date").value = draftPurchase.date;
    document.getElementById("pe-vendor").value = draftPurchase.vendor;
    document.getElementById("pe-total-paid").value = draftPurchase.totalAmountPaid;
  }
  renderInvoiceBox();
  updateComputedDisplays();
}

/* ---- Shipping mode box ---- */

function renderShippingBox() {
  const p = draftPurchase;
  let html = `<div class="control-box"><div class="control-box-title">Shipping</div><div class="chip-row">`;
  [["none", "No shipping"], ["manual", "Manual (per line)"], ["pooled", "Pooled (split automatically)"]].forEach(([mode, label]) => {
    html += `<div class="chip ${p.shippingMode === mode ? "active" : ""}" data-mode="${mode}">${label}</div>`;
  });
  html += `</div>`;
  if (p.shippingMode === "pooled") {
    html += `<div class="control-sub-grid">
      <div class="lfield"><label>Pooled Shipping Total (IDR)</label><input type="number" min="0" step="1000" id="pe-pool-amount" value="${p.poolAmount || 0}"></div>
      <div class="lfield"><label>Split Method</label><select id="pe-pool-method">
        <option value="equal" ${p.poolMethod === "equal" ? "selected" : ""}>Equally (by quantity)</option>
        <option value="by_weight" ${p.poolMethod === "by_weight" ? "selected" : ""}>By Weight</option>
        <option value="by_value" ${p.poolMethod === "by_value" ? "selected" : ""}>By Value (proportional to each line's item cost)</option>
      </select></div>
    </div>
    <div class="assumption-note">Any line below can still tick "ships separately" to opt out of this pool and enter its own exact shipping amount instead.</div>`;
  } else if (p.shippingMode === "manual") {
    html += `<div class="assumption-note">Each line below gets its own exact shipping amount — enter Rp 0 for a line that had no shipping cost of its own.</div>`;
  } else {
    html += `<div class="assumption-note">No shipping cost applies to any line in this purchase.</div>`;
  }
  html += `</div>`;
  document.getElementById("pe-shipping-box").innerHTML = html;

  document.querySelectorAll("#pe-shipping-box .chip").forEach(chip => {
    chip.onclick = () => { draftPurchase.shippingMode = chip.dataset.mode; renderShippingBox(); rebuildAllLineRows(); updateComputedDisplays(); };
  });
  const amt = document.getElementById("pe-pool-amount");
  if (amt) amt.oninput = () => { draftPurchase.poolAmount = Number(amt.value) || 0; updateComputedDisplays(); };
  const meth = document.getElementById("pe-pool-method");
  if (meth) meth.onchange = () => { draftPurchase.poolMethod = meth.value; rebuildAllLineRows(); updateComputedDisplays(); };
}

/* ---- Lump-sum fallback box ---- */

function renderLumpsumBox() {
  const p = draftPurchase;
  const active = !!p.lumpSum;
  let html = `<div class="control-box"><div class="control-box-title">Lump-Sum Pricing Fallback <span style="font-weight:400;color:var(--text-dim);">— opt-in, only for a bundle with one combined price and no per-item breakdown</span></div>`;
  html += `<label class="toggle-row"><input type="checkbox" id="pe-lumpsum-toggle" ${active ? "checked" : ""}> Use a lump-sum group in this purchase</label>`;
  if (active) {
    html += `<div class="control-sub-grid">
      <div class="lfield"><label>Lump Sum Amount (IDR)</label><input type="number" min="0" step="1000" id="pe-lumpsum-amount" value="${p.lumpSum.amount || 0}"></div>
      <div class="lfield"><label>Split Method</label><select id="pe-lumpsum-method">
        <option value="equal" ${p.lumpSum.method === "equal" ? "selected" : ""}>Equally (by quantity)</option>
        <option value="by_weight" ${p.lumpSum.method === "by_weight" ? "selected" : ""}>By Weight</option>
        <option value="by_value" ${p.lumpSum.method === "by_value" ? "selected" : ""}>By Value (estimated)</option>
      </select></div>
    </div>
    <div class="assumption-note">This is a deliberate fallback, not how pricing normally works — tick "Include in lump-sum group" on each line below that this bundled price actually covers. Lines left unticked are still priced directly as normal.</div>`;
  }
  html += `</div>`;
  document.getElementById("pe-lumpsum-box").innerHTML = html;

  document.getElementById("pe-lumpsum-toggle").onchange = (e) => {
    if (e.target.checked) draftPurchase.lumpSum = draftPurchase.lumpSum || { amount: 0, method: "equal" };
    else { draftPurchase.lumpSum = null; draftPurchase.lines.forEach(l => { if (l.pricingMode === "lumpsum") l.pricingMode = "direct"; }); }
    renderLumpsumBox();
    rebuildAllLineRows();
    updateComputedDisplays();
  };
  const amt = document.getElementById("pe-lumpsum-amount");
  if (amt) amt.oninput = () => { draftPurchase.lumpSum.amount = Number(amt.value) || 0; updateComputedDisplays(); };
  const meth = document.getElementById("pe-lumpsum-method");
  if (meth) meth.onchange = () => { draftPurchase.lumpSum.method = meth.value; rebuildAllLineRows(); updateComputedDisplays(); };
}

/* ---- Line rows ---- */

function rebuildAllLineRows() {
  const container = document.getElementById("pe-lines");
  container.innerHTML = draftPurchase.lines.map((l, idx) => buildLineRowHTML(l, idx)).join("");
  draftPurchase.lines.forEach((l) => wireLineRowEvents(l));
}

function lumpsumMethodLabel(m) { return m === "equal" ? "Equally" : m === "by_weight" ? "By Weight" : m === "by_value" ? "By Value" : m; }

function buildLineRowHTML(l, idx) {
  const p = draftPurchase;
  const isNew = l.skuMode === "new";
  let html = `<div class="line-card" id="line-card-${l.id}">`;
  html += `<div class="line-top-row">
    <div class="lfield combobox-field" style="flex:1.6;">
      <label>Item / SKU</label>
      <div class="combobox">
        <input type="text" class="combobox-input" id="line-${l.id}-sku-search" value="${escapeAttr(skuLabelFor(l))}" autocomplete="off" placeholder="Type a SKU or item name…">
        <div class="combobox-dropdown" id="line-${l.id}-sku-dropdown" style="display:none;"></div>
      </div>
    </div>
    <div class="lfield" style="width:90px;"><label>Qty</label><input type="number" min="1" step="1" id="line-${l.id}-qty" value="${l.qty}"></div>
    <div class="lfield" style="width:44px;"><label>&nbsp;</label><button class="btn btn-sm btn-danger-outline" data-remove="${idx}" title="Remove line">✕</button></div>
  </div>`;

  if (isNew) html += newSkuBoxHTML(l);

  html += `<div class="pricing-row">`;
  if (p.lumpSum) {
    html += `<label class="toggle-row-sm"><input type="checkbox" id="line-${l.id}-lumpsum-include" ${l.pricingMode === "lumpsum" ? "checked" : ""}> Include in lump-sum group (Rp ${(p.lumpSum.amount || 0).toLocaleString("id-ID")}, split ${lumpsumMethodLabel(p.lumpSum.method)})</label>`;
  }
  if (l.pricingMode === "lumpsum") {
    if (p.lumpSum.method === "by_weight") {
      html += `<div class="lfield" style="max-width:260px;"><label>Weight (kg, for split)</label><input type="number" min="0" step="0.1" id="line-${l.id}-lumpinput" value="${l.lumpsumWeightKg || 0}"></div>`;
    } else if (p.lumpSum.method === "by_value") {
      html += `<div class="lfield" style="max-width:260px;"><label>Estimated Value (relative units, for split)</label><input type="number" min="0" step="1" id="line-${l.id}-lumpinput" value="${l.lumpsumValue || 0}"></div>`;
    } else {
      html += `<div class="shipping-note">Split equally, weighted by quantity (${l.qty}).</div>`;
    }
  } else {
    html += `<div class="price-toggle-group">
      <div class="lfield"><label>Item Price</label>
        <div class="price-input-row">
          <input type="number" min="0" step="1000" id="line-${l.id}-pricevalue" value="${l.priceValue || 0}">
          <div class="mini-toggle">
            <button type="button" class="mtbtn ${l.priceEntryMode === "per_unit" ? "active" : ""}" data-mode="per_unit">Per unit</button>
            <button type="button" class="mtbtn ${l.priceEntryMode === "total" ? "active" : ""}" data-mode="total">Total for line</button>
          </div>
        </div>
      </div>
      <div class="computed-mirror" id="line-${l.id}-price-mirror"></div>
    </div>`;
  }
  html += `</div>`;

  html += `<div class="shipping-row">${shippingSubFieldsHTML(l)}</div>`;

  html += `<div class="line-outputs">
    <div class="out">Item Cost<b id="line-${l.id}-itemcost">${fmtIDR(0)}</b></div>
    <div class="out">Shipping Share<b id="line-${l.id}-shipshare">${fmtIDR(0)}</b></div>
    <div class="out">Line Total<b id="line-${l.id}-linetotal">${fmtIDR(0)}</b></div>
  </div>`;

  html += `<div id="line-${l.id}-serial-area">${serialAreaHTML(l)}</div>`;
  html += `</div>`;
  return html;
}

function shippingSubFieldsHTML(l) {
  const p = draftPurchase;
  if (p.shippingMode === "none") return `<div class="shipping-note">No shipping cost applies.</div>`;
  if (p.shippingMode === "manual") {
    return `<div class="lfield" style="max-width:220px;"><label>Shipping for this line (IDR)</label><input type="number" min="0" step="1000" id="line-${l.id}-manualship" value="${l.manualShipping || 0}"></div>`;
  }
  let html = `<label class="toggle-row-sm"><input type="checkbox" id="line-${l.id}-ships-separately" ${l.shipsSeparately ? "checked" : ""}> This item shipped separately (not from the pooled amount)</label>`;
  if (l.shipsSeparately) {
    html += `<div class="lfield" style="max-width:220px;"><label>Shipping for this line (IDR)</label><input type="number" min="0" step="1000" id="line-${l.id}-manualship" value="${l.manualShipping || 0}"></div>`;
  } else if (p.poolMethod === "by_weight") {
    html += `<div class="lfield" style="max-width:220px;"><label>Weight (kg, for pooled split)</label><input type="number" min="0" step="0.1" id="line-${l.id}-shipweight" value="${l.shippingWeightKg || 0}"></div>`;
  } else if (p.poolMethod === "by_value") {
    html += `<div class="shipping-note">Split proportional to this line's own Item Cost above.</div>`;
  } else {
    html += `<div class="shipping-note">Split equally, weighted by quantity (${l.qty}).</div>`;
  }
  return html;
}

function newSkuBoxHTML(l) {
  const ns = l.newSku || { code: "", name: "", category: (categoriesCache[0] || {}).code, identity: "fungible", skuTouched: false };
  return `
  <div class="new-sku-box">
    <div class="nsfield"><label>Name</label><input type="text" id="line-${l.id}-ns-name" value="${escapeAttr(ns.name || "")}" placeholder="Item name"></div>
    <div class="nsfield"><label>Category</label>
      <select id="line-${l.id}-ns-cat">${categoriesCache.map(c => `<option value="${c.code}" ${ns.category === c.code ? "selected" : ""}>${escapeAttr(c.name)}</option>`).join("")}</select>
    </div>
    <div class="nsfield"><label>Identity Mode</label>
      <select id="line-${l.id}-ns-identity">
        <option value="fungible" ${ns.identity === "fungible" ? "selected" : ""}>Fungible</option>
        <option value="serialized" ${ns.identity === "serialized" ? "selected" : ""}>Serialized</option>
      </select>
    </div>
    <div class="nsfield">
      <label>SKU Code <button type="button" class="link-btn" style="font-size:11px;" id="line-${l.id}-ns-regen">🔄 Regenerate</button></label>
      <input type="text" id="line-${l.id}-ns-code" value="${escapeAttr(ns.code || "")}" placeholder="Auto-generated as you type the name">
      <div class="sku-check" id="line-${l.id}-ns-code-check"></div>
    </div>
  </div>`;
}

function serialAreaHTML(l) {
  const identity = draftLineIdentity(l);
  if (identity !== "serialized") return "";
  const qty = l.qty || 1;
  if (l.serialUnits.length !== qty) l.serialUnits = defaultSerialUnits(l);
  if (qty === 1) {
    const u = l.serialUnits[0] || { serial: "", cost: 0 };
    return `<div class="serial-single">
      <div class="lfield" style="max-width:260px;"><label>Serial / Asset ID</label>
        <div style="display:flex; gap:8px;">
          <input type="text" id="line-${l.id}-serial-0" value="${escapeAttr(u.serial || "")}" placeholder="Case/serial number or internal tag">
          <button type="button" class="btn btn-sm" data-generate-serials="${l.id}">⚡ Generate</button>
        </div>
        <div class="serial-check" id="line-${l.id}-serial-check-0"></div>
      </div>
      <div class="photo-field">
        <label>Photo</label>
        <input type="file" accept="image/*" id="line-${l.id}-photo-input-0">
        <div class="unit-thumb-wrap" id="line-${l.id}-photo-0">${u.photoDataUrl ? `<img src="${u.photoDataUrl}" class="unit-thumb"><span class="photo-filename">${escapeAttr(u.photoFileName || "")}</span>` : `<span class="photo-filename" style="color:var(--text-dim);">No photo attached</span>`}</div>
      </div>
    </div>`;
  }
  const toggleLabel = l.serialPanelOpen ? `▾ Collapse serial assignment (${qty} units)` : `▸ Assign ${qty} serial numbers →`;
  let panel = "";
  if (l.serialPanelOpen) {
    const units = l.serialUnits;
    panel = `<div class="serial-panel" id="line-${l.id}-serial-panel">
      <div class="serial-panel-head">
        <div class="serial-row-head"><div>#</div><div>Serial / Asset ID</div><div>Cost (IDR)</div><div>Photo</div></div>
        <button type="button" class="btn btn-sm" data-generate-serials="${l.id}">⚡ Generate all</button>
      </div>
      ${units.map((u, i) => `
        <div class="serial-row">
          <div>${i + 1}</div>
          <input type="text" id="line-${l.id}-serial-${i}" value="${escapeAttr(u.serial || "")}" placeholder="Serial / asset ID">
          <input type="number" id="line-${l.id}-serialcost-${i}" value="${u.cost || 0}" step="1000">
          <div class="photo-cell">
            <input type="file" accept="image/*" id="line-${l.id}-photo-input-${i}">
            <div class="unit-thumb-wrap" id="line-${l.id}-photo-${i}">${u.photoDataUrl ? `<img src="${u.photoDataUrl}" class="unit-thumb"><span class="photo-filename">${escapeAttr(u.photoFileName || "")}</span>` : `<span class="photo-filename" style="color:var(--text-dim);">No photo</span>`}</div>
          </div>
        </div>`).join("")}
      <div class="serial-check" id="line-${l.id}-serial-check"></div>
    </div>`;
  }
  return `<div class="serial-toggle" data-toggle-serial="${l.id}">${toggleLabel}</div>${panel}`;
}

function defaultSerialUnits(l) {
  const alloc = computeAllocation(toAllocationInput(draftPurchase));
  const idx = draftPurchase.lines.indexOf(l);
  const lineTotal = idx >= 0 ? alloc.lines[idx].lineTotal : 0;
  const costs = splitSerialUnitCosts(lineTotal, l.qty);
  const existing = l.serialUnits || [];
  return Array.from({ length: l.qty }, (_, i) => ({
    serial: (existing[i] && existing[i].serial) || "",
    cost: costs[i],
    costTouched: (existing[i] && existing[i].costTouched) || false,
    photoDataUrl: (existing[i] && existing[i].photoDataUrl) || null,
    photoFileName: (existing[i] && existing[i].photoFileName) || null,
  }));
}

function rebuildLineRow(l) {
  const idx = draftPurchase.lines.indexOf(l);
  const card = document.getElementById("line-card-" + l.id);
  card.outerHTML = buildLineRowHTML(l, idx);
  wireLineRowEvents(l);
  updateComputedDisplays();
}

/* ---- Combobox (typeahead SKU search against real items) ---- */

function wireComboboxEvents(l) {
  const input = document.getElementById(`line-${l.id}-sku-search`);
  const dropdown = document.getElementById(`line-${l.id}-sku-dropdown`);
  input.oninput = debounce(() => renderComboboxDropdown(l, input.value), 150);
  input.onfocus = () => renderComboboxDropdown(l, input.value);
  document.addEventListener("click", function outsideClick(e) {
    if (!document.body.contains(input)) { document.removeEventListener("click", outsideClick); return; }
    if (!input.contains(e.target) && !dropdown.contains(e.target)) dropdown.style.display = "none";
  });
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

async function renderComboboxDropdown(l, query) {
  const dropdown = document.getElementById(`line-${l.id}-sku-dropdown`);
  if (!dropdown) return;
  const q = (query || "").trim();
  const matches = q ? (await apiGet("/api/items/search?q=" + encodeURIComponent(q) + "&limit=8")) || [] : [];
  let html = `<div class="combo-option combo-create" data-create="${l.id}">+ Create new SKU inline…</div>`;
  html += matches.length === 0
    ? `<div class="combo-empty">${q ? "No matching items — try \"+ Create new SKU\" above." : "Type to search…"}</div>`
    : matches.map(it => `<div class="combo-option" data-pick="${l.id}" data-sku="${escapeAttr(it.sku)}" data-identity="${escapeAttr(it.identity_mode)}"><b>${escapeAttr(it.sku)}</b><span>${escapeAttr(it.name)}</span></div>`).join("");
  dropdown.innerHTML = html;
  dropdown.style.display = "block";
  const createOpt = dropdown.querySelector("[data-create]");
  if (createOpt) createOpt.onclick = () => pickComboboxNew(l.id);
  dropdown.querySelectorAll("[data-pick]").forEach(opt => {
    opt.onclick = () => pickComboboxItem(l.id, opt.dataset.sku, opt.dataset.identity);
  });
}
function pickComboboxItem(lineId, sku, identityMode) {
  const l = draftPurchase.lines.find(x => x.id === lineId);
  l.skuMode = "existing"; l.sku = sku; l.identityMode = identityMode; l.newSku = null;
  rebuildLineRow(l);
}
function pickComboboxNew(lineId) {
  const l = draftPurchase.lines.find(x => x.id === lineId);
  l.skuMode = "new"; l.sku = "";
  l.newSku = l.newSku || { code: "", name: "", category: (categoriesCache[0] || {}).code, identity: "fungible", skuTouched: false };
  rebuildLineRow(l);
}

/* ---- New-SKU box wiring (real generator preview + real uniqueness check) ---- */

function otherDraftSkusExcluding(lineId) {
  return draftPurchase.lines
    .filter(x => x.id !== lineId && x.skuMode === "new" && x.newSku && x.newSku.code)
    .map(x => x.newSku.code);
}

async function refreshGeneratedSkuForLine(l) {
  if (l.newSku.skuTouched) return;
  const cat = categoriesCache.find(c => c.code === l.newSku.category);
  if (!cat || !l.newSku.name) { l.newSku.code = ""; return; }
  const data = await apiGet(`/api/skus/candidates?category_code=${encodeURIComponent(cat.code)}&name=${encodeURIComponent(l.newSku.name)}`);
  const existing = (data.existing || []).concat(otherDraftSkusExcluding(l.id));
  l.newSku.code = generateSkuCandidate(cat.sku_prefix, l.newSku.name, existing);
}

async function refreshNewSkuCheck(l) {
  const checkEl = document.getElementById(`line-${l.id}-ns-code-check`);
  if (!checkEl) return;
  const code = l.newSku.code;
  if (!code) { checkEl.innerHTML = ""; updateComputedDisplays(); return; }
  const siblingDupe = otherDraftSkusExcluding(l.id).some(s => s.toUpperCase() === code.toUpperCase());
  if (siblingDupe) {
    checkEl.innerHTML = '<span style="color:var(--red-text);">✗ Another line in this purchase already uses this SKU.</span>';
    updateComputedDisplays();
    return;
  }
  const res = await apiGet(`/api/skus/check?sku=${encodeURIComponent(code)}`);
  checkEl.innerHTML = res.exists
    ? '<span style="color:var(--red-text);">✗ This SKU already exists — choose another.</span>'
    : '<span style="color:var(--green-text);">✓ Available</span>';
  updateComputedDisplays();
}

function wireNewSkuBoxEvents(l) {
  const nameEl = document.getElementById(`line-${l.id}-ns-name`);
  const catEl = document.getElementById(`line-${l.id}-ns-cat`);
  const idEl = document.getElementById(`line-${l.id}-ns-identity`);
  const codeEl = document.getElementById(`line-${l.id}-ns-code`);
  const regenBtn = document.getElementById(`line-${l.id}-ns-regen`);

  nameEl.oninput = async () => {
    l.newSku.name = nameEl.value;
    if (!l.newSku.skuTouched) { await refreshGeneratedSkuForLine(l); codeEl.value = l.newSku.code; }
    await refreshNewSkuCheck(l);
  };
  catEl.onchange = async () => {
    l.newSku.category = catEl.value;
    if (!l.newSku.skuTouched) { await refreshGeneratedSkuForLine(l); codeEl.value = l.newSku.code; }
    await refreshNewSkuCheck(l);
  };
  idEl.onchange = () => { l.newSku.identity = idEl.value; rebuildLineRow(l); };
  codeEl.oninput = async () => { l.newSku.skuTouched = true; l.newSku.code = codeEl.value.trim().toUpperCase(); await refreshNewSkuCheck(l); };
  regenBtn.onclick = async () => { l.newSku.skuTouched = false; await refreshGeneratedSkuForLine(l); codeEl.value = l.newSku.code; await refreshNewSkuCheck(l); };
}

/* ---- Line-level event wiring ---- */

function wireLineRowEvents(l) {
  wireComboboxEvents(l);

  const removeBtn = document.querySelector(`#line-card-${l.id} [data-remove]`);
  if (removeBtn) removeBtn.onclick = () => {
    const idx = draftPurchase.lines.indexOf(l);
    draftPurchase.lines.splice(idx, 1);
    rebuildAllLineRows();
    updateComputedDisplays();
  };

  const qtyInput = document.getElementById(`line-${l.id}-qty`);
  qtyInput.oninput = () => {
    const newQty = Math.max(1, Number(qtyInput.value) || 1);
    const qtyChanged = newQty !== l.qty;
    l.qty = newQty;
    if (qtyChanged) l.serialUnits = [];
    if (draftLineIdentity(l) === "serialized") rebuildLineRow(l); else updateComputedDisplays();
  };

  if (l.skuMode === "new") wireNewSkuBoxEvents(l);

  if (draftPurchase.lumpSum) {
    const inc = document.getElementById(`line-${l.id}-lumpsum-include`);
    if (inc) inc.onchange = () => { l.pricingMode = inc.checked ? "lumpsum" : "direct"; rebuildLineRow(l); };
  }

  document.querySelectorAll(`#line-card-${l.id} .mtbtn`).forEach(btn => {
    btn.onclick = () => setPriceEntryMode(l.id, btn.dataset.mode);
  });

  if (l.pricingMode === "lumpsum") {
    const li = document.getElementById(`line-${l.id}-lumpinput`);
    if (li) li.oninput = () => {
      if (draftPurchase.lumpSum.method === "by_weight") l.lumpsumWeightKg = Number(li.value) || 0;
      else if (draftPurchase.lumpSum.method === "by_value") l.lumpsumValue = Number(li.value) || 0;
      updateComputedDisplays();
    };
  } else {
    const pv = document.getElementById(`line-${l.id}-pricevalue`);
    if (pv) pv.oninput = () => { l.priceValue = Number(pv.value) || 0; updateComputedDisplays(); };
  }

  if (draftPurchase.shippingMode === "manual") {
    const ms = document.getElementById(`line-${l.id}-manualship`);
    if (ms) ms.oninput = () => { l.manualShipping = Number(ms.value) || 0; updateComputedDisplays(); };
  } else if (draftPurchase.shippingMode === "pooled") {
    const sep = document.getElementById(`line-${l.id}-ships-separately`);
    if (sep) sep.onchange = () => { l.shipsSeparately = sep.checked; rebuildLineRow(l); };
    const ms = document.getElementById(`line-${l.id}-manualship`);
    if (ms) ms.oninput = () => { l.manualShipping = Number(ms.value) || 0; updateComputedDisplays(); };
    const sw = document.getElementById(`line-${l.id}-shipweight`);
    if (sw) sw.oninput = () => { l.shippingWeightKg = Number(sw.value) || 0; updateComputedDisplays(); };
  }

  wireSerialAreaEvents(l);
}

function setPriceEntryMode(lineId, mode) {
  const l = draftPurchase.lines.find(x => x.id === lineId);
  if (l.priceEntryMode !== mode) {
    l.priceValue = convertPriceValue(l.priceValue || 0, l.qty || 1, l.priceEntryMode, mode);
  }
  l.priceEntryMode = mode;
  rebuildLineRow(l);
}

/* ---- Serial area wiring (real existing-count + candidate generation + uniqueness) ---- */

function wireSerialAreaEvents(l) {
  const identity = draftLineIdentity(l);
  if (identity !== "serialized") return;
  const qty = l.qty || 1;

  const toggle = document.querySelector(`#line-card-${l.id} [data-toggle-serial]`);
  if (toggle) toggle.onclick = () => { l.serialPanelOpen = !l.serialPanelOpen; rebuildLineRow(l); };

  const genBtn = document.querySelector(`#line-card-${l.id} [data-generate-serials]`);
  if (genBtn) genBtn.onclick = () => generateSerialsForLineAndRefresh(l);

  if (qty === 1) {
    const s0 = document.getElementById(`line-${l.id}-serial-0`);
    if (s0) s0.oninput = () => { l.serialUnits[0] = l.serialUnits[0] || { serial: "", cost: 0 }; l.serialUnits[0].serial = s0.value; updateComputedDisplays(); };
    const p0 = document.getElementById(`line-${l.id}-photo-input-0`);
    if (p0) p0.onchange = () => handleUnitPhoto(l, 0, p0);
  } else if (l.serialPanelOpen) {
    l.serialUnits.forEach((u, i) => {
      const sEl = document.getElementById(`line-${l.id}-serial-${i}`);
      const cEl = document.getElementById(`line-${l.id}-serialcost-${i}`);
      const pEl = document.getElementById(`line-${l.id}-photo-input-${i}`);
      if (sEl) sEl.oninput = () => { l.serialUnits[i].serial = sEl.value; updateComputedDisplays(); };
      if (cEl) cEl.oninput = () => { l.serialUnits[i].cost = Number(cEl.value) || 0; l.serialUnits[i].costTouched = true; updateComputedDisplays(); };
      if (pEl) pEl.onchange = () => handleUnitPhoto(l, i, pEl);
    });
  }
}

function handleUnitPhoto(l, unitIndex, fileInputEl) {
  const file = fileInputEl.files && fileInputEl.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    if (!l.serialUnits[unitIndex]) l.serialUnits[unitIndex] = { serial: "", cost: 0 };
    l.serialUnits[unitIndex].photoDataUrl = reader.result;
    l.serialUnits[unitIndex].photoFileName = file.name;
    const thumbEl = document.getElementById(`line-${l.id}-photo-${unitIndex}`);
    if (thumbEl) thumbEl.innerHTML = `<img src="${reader.result}" class="unit-thumb"><span class="photo-filename">${escapeAttr(file.name)}</span>`;
  };
  reader.readAsDataURL(file);
}

async function generateSerialsForLineAndRefresh(l) {
  const effectiveSku = l.skuMode === "new" ? (l.newSku && l.newSku.code) : l.sku;
  if (!effectiveSku) { showToast("Enter/confirm a SKU before generating serial IDs."); return; }
  let existingCount = 0;
  if (l.skuMode === "existing") {
    const data = await apiGet(`/api/items/${encodeURIComponent(effectiveSku)}/serial-count`);
    existingCount = data.existing_count;
  }
  const siblingReserved = [];
  draftPurchase.lines.forEach(other => {
    if (other === l) return;
    const otherSku = other.skuMode === "new" ? (other.newSku && other.newSku.code) : other.sku;
    if (otherSku !== effectiveSku) return;
    (other.serialUnits || []).forEach(u => { if (u.serial) siblingReserved.push(u.serial); });
  });
  const qty = l.qty || 1;
  if (l.serialUnits.length !== qty) l.serialUnits = defaultSerialUnits(l);
  const generated = generateSerialsForLine(effectiveSku, qty, existingCount, siblingReserved);
  l.serialUnits.forEach((u, i) => { u.serial = generated[i]; });
  rebuildLineRow(l);
}

/* ---- Recompute + balance panel ---- */

function toAllocationInput(p) {
  return {
    totalAmountPaid: p.totalAmountPaid,
    shippingMode: p.shippingMode,
    poolAmount: p.poolAmount,
    poolMethod: p.poolMethod,
    lumpSum: p.lumpSum,
    lines: p.lines,
  };
}

function allDraftSerialIds() {
  const ids = [];
  draftPurchase.lines.forEach(l => {
    if (draftLineIdentity(l) !== "serialized") return;
    (l.serialUnits || []).forEach(u => { if ((u.serial || "").trim()) ids.push(u.serial.trim().toUpperCase()); });
  });
  return ids;
}
function findDraftDuplicateSerials() {
  const counts = new Map();
  allDraftSerialIds().forEach(id => counts.set(id, (counts.get(id) || 0) + 1));
  return new Set([...counts.entries()].filter(([, c]) => c > 1).map(([k]) => k));
}

function updateComputedDisplays() {
  const alloc = computeAllocation(toAllocationInput(draftPurchase));
  const draftDupes = findDraftDuplicateSerials();

  draftPurchase.lines.forEach((l, idx) => {
    const a = alloc.lines[idx];
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set(`line-${l.id}-itemcost`, fmtIDR(a.itemCost));
    set(`line-${l.id}-shipshare`, fmtIDR(a.shippingShare));
    set(`line-${l.id}-linetotal`, fmtIDR(a.lineTotal));

    const mirror = document.getElementById(`line-${l.id}-price-mirror`);
    if (mirror && l.pricingMode !== "lumpsum") {
      if (l.priceEntryMode === "per_unit") mirror.innerHTML = `Total for line: <b>${fmtIDR((l.priceValue || 0) * (l.qty || 0))}</b>`;
      else { const perUnit = (l.qty || 0) > 0 ? (l.priceValue || 0) / l.qty : 0; mirror.innerHTML = `≈ Per unit: <b>${fmtIDR(perUnit)}</b>`; }
    }

    const identity = draftLineIdentity(l);
    if (identity === "serialized" && l.qty === 1 && l.serialUnits[0]) {
      if (!l.serialUnits[0].costTouched) l.serialUnits[0].cost = a.lineTotal;
      const check0 = document.getElementById(`line-${l.id}-serial-check-0`);
      if (check0) {
        const serial0 = (l.serialUnits[0].serial || "").trim().toUpperCase();
        check0.innerHTML = (serial0 && draftDupes.has(serial0))
          ? `<span style="color:var(--red-text);">✗ This Serial/Asset ID duplicates another unit in this purchase.</span>` : "";
      }
    }
    if (identity === "serialized" && l.qty > 1 && l.serialPanelOpen) {
      const check = document.getElementById(`line-${l.id}-serial-check`);
      if (check) {
        const sum = l.serialUnits.reduce((s, u) => s + (u.cost || 0), 0);
        const ok = sum === a.lineTotal;
        let msg = ok
          ? `<span style="color:var(--green-text);">✓ ${l.serialUnits.length} units allocate ${fmtIDR(sum)} of this line's ${fmtIDR(a.lineTotal)} total</span>`
          : `<span style="color:var(--red-text);">✗ ${l.serialUnits.length} units allocate ${fmtIDR(sum)} — line total is ${fmtIDR(a.lineTotal)} (${fmtIDRSigned(a.lineTotal - sum)})</span>`;
        const hasDupe = l.serialUnits.some(u => (u.serial || "").trim() && draftDupes.has(u.serial.trim().toUpperCase()));
        if (hasDupe) msg += `<br><span style="color:var(--red-text);">✗ One or more Serial/Asset IDs on this line duplicate another unit in this purchase.</span>`;
        check.innerHTML = msg;
      }
    }
  });

  const panel = document.getElementById("balance-panel");
  const icon = document.getElementById("balance-icon");
  const headline = document.getElementById("balance-headline");
  const detail = document.getElementById("balance-detail");
  const saveBtn = document.getElementById("save-purchase-btn");

  const serialsOk = allSerialsAssigned();
  const serialsUniqueOk = draftDupes.size === 0;
  const newSkuOk = allNewSkusHaveRequiredFields();

  if (alloc.balanced) {
    panel.classList.remove("unbalanced"); panel.classList.add("balanced");
    icon.textContent = "✓";
    headline.textContent = `Balanced — ${fmtIDR(alloc.runningTotal)} reconciles exactly`;
    detail.textContent = "Sum of every line's Item Cost + Shipping Share matches Total Amount Paid.";
  } else {
    panel.classList.remove("balanced"); panel.classList.add("unbalanced");
    icon.textContent = "⚠";
    headline.textContent = `Out of balance by ${fmtIDRSigned(alloc.diff)}`;
    detail.textContent = `Running total ${fmtIDR(alloc.runningTotal)} vs. Total Amount Paid ${fmtIDR(draftPurchase.totalAmountPaid)}.`;
  }

  let disabledReason = "";
  if (draftPurchase.lines.length === 0) disabledReason = "Add at least one line before saving.";
  else if (!alloc.balanced) disabledReason = "Item cost + shipping across all lines must equal Total Amount Paid before saving.";
  else if (!serialsOk) disabledReason = "Assign a serial/asset ID to every unit on a Serialized line before saving.";
  else if (!serialsUniqueOk) disabledReason = "Two or more serialized units share the same Serial/Asset ID — fix the duplicate before saving.";
  else if (!newSkuOk) disabledReason = "Fill in SKU code, name, and category for every inline-created item before saving.";

  saveBtn.disabled = !!disabledReason;
  saveBtn.title = disabledReason;
}

function allSerialsAssigned() {
  return draftPurchase.lines.every(l => {
    if (draftLineIdentity(l) !== "serialized") return true;
    if (!l.serialUnits || l.serialUnits.length !== l.qty) return false;
    return l.serialUnits.every(u => (u.serial || "").trim().length > 0);
  });
}
function allNewSkusHaveRequiredFields() {
  return draftPurchase.lines.every(l => l.skuMode !== "new" || (l.newSku && l.newSku.code && l.newSku.name && l.newSku.category));
}

/* ---- Save — always goes through the real server engine ---- */

function buildPurchasePayload() {
  const p = draftPurchase;
  const lines = p.lines.map(l => {
    const identity = draftLineIdentity(l);
    const line = {
      quantity: l.qty,
      pricing_mode: l.pricingMode === "lumpsum" ? "lumpsum_group" : "direct",
      ships_separately: !!l.shipsSeparately,
    };
    if (l.skuMode === "new") {
      line.new_item = { name: l.newSku.name, category_code: l.newSku.category, identity_mode: l.newSku.identity, sku: l.newSku.code || null };
    } else {
      line.sku = l.sku;
    }
    if (line.pricing_mode === "direct") {
      line.price_entry_mode = l.priceEntryMode;
      line.price_value = l.priceValue;
    } else {
      if (p.lumpSum.method === "by_weight") line.lumpsum_weight_kg = l.lumpsumWeightKg;
      else if (p.lumpSum.method === "by_value") line.lumpsum_value = l.lumpsumValue;
    }
    if (p.shippingMode === "manual") {
      line.manual_shipping_amount = l.manualShipping;
    } else if (p.shippingMode === "pooled") {
      if (l.shipsSeparately) line.manual_shipping_amount = l.manualShipping;
      else if (p.poolMethod === "by_weight") line.shipping_weight_kg = l.shippingWeightKg;
    }
    if (identity === "serialized") {
      line.serial_units = l.serialUnits.map(u => ({
        serial_id: (u.serial || "").trim() || null,
        cost: u.costTouched ? u.cost : null,
        photo_reference: u.photoFileName || null,
      }));
    }
    return line;
  });

  return {
    purchase_date: p.date,
    vendor_description: p.vendor,
    total_amount_paid: p.totalAmountPaid,
    currency: "IDR",
    shipping_mode: p.shippingMode,
    pooled_shipping_total: p.shippingMode === "pooled" ? p.poolAmount : null,
    pooled_shipping_method: p.shippingMode === "pooled" ? p.poolMethod : null,
    lump_sum_active: !!p.lumpSum,
    lump_sum_total: p.lumpSum ? p.lumpSum.amount : null,
    lump_sum_method: p.lumpSum ? p.lumpSum.method : null,
    invoice_document_ref: p.invoice ? p.invoice.fileName : null,
    invoice_ocr_status: p.invoice ? p.invoice.ocrStatus : null,
    invoice_parsed_fields: p.invoice ? (p.invoice.parsedFields || null) : null,
    lines,
  };
}

async function savePurchase() {
  const errBox = document.getElementById("pe-server-error");
  errBox.style.display = "none";
  const payload = buildPurchasePayload();
  try {
    const saved = await apiPost("/api/purchases", payload);
    if (!saved) return;
    showToast(`Purchase ${saved.purchase_ref} saved — ${fmtIDR(saved.allocation.total_amount_paid)} across ${saved.lines.length} line(s).`);
    window.location.href = "/purchases?ref=" + encodeURIComponent(saved.purchase_ref);
  } catch (err) {
    errBox.style.display = "block";
    const type = (err.detail && err.detail.error_type) || "Error";
    errBox.textContent = `${type}: ${err.message}`;
  }
}

initPurchaseEntryPage();
