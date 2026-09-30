/* Opening Inventory — a simpler, single-item variant of Purchase Entry for
 * recording stock the user already physically has (see CLAUDE.md's
 * "Opening inventory entry, decision confirmed 2026-09-30"). No vendor/
 * shipping-mode complexity — Save always POSTs to /api/opening-inventory,
 * which calls the real, unmodified inventory.purchases.save_purchase()
 * (via inventory.opening_inventory.record_opening_inventory()). Nothing
 * computed in this file is ever trusted as authoritative; the server
 * re-derives and re-validates everything from scratch, exactly like
 * Purchase Entry.
 *
 * Reuses the same item-picker (typeahead + inline new-item creation with
 * live SKU generation preview) and per-unit serial/cost UI patterns already
 * proven in purchase_entry.js, rather than inventing a second version —
 * just without the multi-line/shipping/lump-sum machinery that doesn't
 * apply here (one item, one quantity, one value per entry).
 */

let categoriesCache = [];

function todayISO() { return new Date().toISOString().slice(0, 10); }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

let draft = null;

function emptyDraft() {
  return {
    date: todayISO(),
    vendor: "",
    itemMode: "existing", // 'existing' | 'new'
    existingSku: "", existingIdentity: "fungible",
    newItem: { code: "", name: "", category: "", identity: "fungible", skuTouched: false },
    qty: 1,
    priceEntryMode: "per_unit", priceValue: 0,
    serialUnits: [], // [{serial, cost, costTouched}]
    serialPanelOpen: false,
  };
}

function currentIdentity() {
  return draft.itemMode === "new" ? (draft.newItem.identity || "fungible") : (draft.existingIdentity || "fungible");
}
function currentSku() {
  return draft.itemMode === "new" ? (draft.newItem.code || "") : (draft.existingSku || "");
}

async function initOpeningInventoryPage() {
  categoriesCache = (await apiGet("/api/categories")) || [];
  draft = emptyDraft();

  document.getElementById("oi-date").value = draft.date;
  document.getElementById("oi-date").oninput = e => { draft.date = e.target.value; };
  document.getElementById("oi-vendor").oninput = e => { draft.vendor = e.target.value; };
  document.getElementById("oi-qty").oninput = () => {
    draft.qty = Math.max(1, Number(document.getElementById("oi-qty").value) || 1);
    draft.serialUnits = [];
    renderSerialArea();
    updateComputedDisplays();
  };
  document.getElementById("oi-pricevalue").oninput = () => {
    draft.priceValue = Number(document.getElementById("oi-pricevalue").value) || 0;
    updateComputedDisplays();
  };
  document.querySelectorAll(".price-toggle-group .mtbtn").forEach(btn => {
    btn.onclick = () => setPriceEntryMode(btn.dataset.mode);
  });
  document.getElementById("oi-submit-btn").onclick = submitEntry;

  wireItemModeToggle();
  wireExistingItemCombobox();
  renderNewItemForm();
  renderSerialArea();
  updateComputedDisplays();
}

/* ---- Item mode toggle + existing-item combobox (searches ALL items — no
 * identity filter, since this screen must cover fungible AND serialized). ---- */

function wireItemModeToggle() {
  document.querySelectorAll('input[name="oi-item-mode"]').forEach(radio => {
    radio.onchange = () => {
      draft.itemMode = document.querySelector('input[name="oi-item-mode"]:checked').value;
      document.getElementById("oi-existing-item-form").style.display = draft.itemMode === "existing" ? "block" : "none";
      document.getElementById("oi-new-item-form").style.display = draft.itemMode === "new" ? "block" : "none";
      draft.serialUnits = [];
      renderSerialArea();
      updateComputedDisplays();
    };
  });
}

function wireExistingItemCombobox() {
  const input = document.getElementById("oi-existing-search");
  const dropdown = document.getElementById("oi-existing-dropdown");
  input.oninput = debounce(() => renderExistingDropdown(input.value), 150);
  input.onfocus = () => renderExistingDropdown(input.value);
  document.addEventListener("click", (e) => {
    if (!input.contains(e.target) && !dropdown.contains(e.target)) dropdown.style.display = "none";
  });
}

async function renderExistingDropdown(query) {
  const dropdown = document.getElementById("oi-existing-dropdown");
  const q = (query || "").trim();
  const matches = q ? (await apiGet("/api/items/search?q=" + encodeURIComponent(q) + "&limit=8")) || [] : [];
  dropdown.innerHTML = matches.length === 0
    ? `<div class="combo-empty">${q ? "No matching items." : "Type to search…"}</div>`
    : matches.map(it => `<div class="combo-option" data-sku="${escapeAttr(it.sku)}" data-identity="${escapeAttr(it.identity_mode)}"><b>${escapeAttr(it.sku)}</b><span>${escapeAttr(it.name)}</span></div>`).join("");
  dropdown.style.display = "block";
  dropdown.querySelectorAll("[data-sku]").forEach(opt => {
    opt.onclick = () => {
      draft.existingSku = opt.dataset.sku;
      draft.existingIdentity = opt.dataset.identity;
      draft.serialUnits = [];
      document.getElementById("oi-existing-search").value = `${opt.dataset.sku} — ${opt.querySelector("span").textContent}`;
      dropdown.style.display = "none";
      renderSerialArea();
      updateComputedDisplays();
    };
  });
}

/* ---- New-item form (same pattern as purchase_entry.js's new-SKU box —
 * live generated-SKU preview + real uniqueness check, not reinvented). ---- */

function renderNewItemForm() {
  const ns = draft.newItem;
  if (!ns.category) ns.category = (categoriesCache[0] || {}).code || "";
  const html = `
  <div class="new-sku-box">
    <div class="nsfield"><label>Name</label><input type="text" id="oi-ns-name" value="${escapeAttr(ns.name || "")}" placeholder="Item name"></div>
    <div class="nsfield"><label>Category</label>
      <select id="oi-ns-cat">${categoriesCache.map(c => `<option value="${c.code}" ${ns.category === c.code ? "selected" : ""}>${escapeAttr(c.name)}</option>`).join("")}</select>
    </div>
    <div class="nsfield"><label>Identity Mode</label>
      <select id="oi-ns-identity">
        <option value="fungible" ${ns.identity === "fungible" ? "selected" : ""}>Fungible</option>
        <option value="serialized" ${ns.identity === "serialized" ? "selected" : ""}>Serialized</option>
      </select>
    </div>
    <div class="nsfield">
      <label>SKU Code <button type="button" class="link-btn" style="font-size:11px;" id="oi-ns-regen">🔄 Regenerate</button></label>
      <input type="text" id="oi-ns-code" value="${escapeAttr(ns.code || "")}" placeholder="Auto-generated as you type the name">
      <div class="sku-check" id="oi-ns-code-check"></div>
    </div>
  </div>`;
  document.getElementById("oi-new-item-form").innerHTML = html;
  wireNewItemFormEvents();
}

async function refreshGeneratedSku() {
  const ns = draft.newItem;
  if (ns.skuTouched) return;
  const cat = categoriesCache.find(c => c.code === ns.category);
  if (!cat || !ns.name) { ns.code = ""; return; }
  const data = await apiGet(`/api/skus/candidates?category_code=${encodeURIComponent(cat.code)}&name=${encodeURIComponent(ns.name)}`);
  ns.code = generateSkuCandidate(cat.sku_prefix, ns.name, data.existing || []);
}

async function refreshNewSkuCheck() {
  const checkEl = document.getElementById("oi-ns-code-check");
  if (!checkEl) return;
  const code = draft.newItem.code;
  if (!code) { checkEl.innerHTML = ""; updateComputedDisplays(); return; }
  const res = await apiGet(`/api/skus/check?sku=${encodeURIComponent(code)}`);
  checkEl.innerHTML = res.exists
    ? '<span style="color:var(--red-text);">✗ This SKU already exists — choose another.</span>'
    : '<span style="color:var(--green-text);">✓ Available</span>';
  updateComputedDisplays();
}

function wireNewItemFormEvents() {
  const nameEl = document.getElementById("oi-ns-name");
  const catEl = document.getElementById("oi-ns-cat");
  const idEl = document.getElementById("oi-ns-identity");
  const codeEl = document.getElementById("oi-ns-code");
  const regenBtn = document.getElementById("oi-ns-regen");

  nameEl.oninput = async () => {
    draft.newItem.name = nameEl.value;
    if (!draft.newItem.skuTouched) { await refreshGeneratedSku(); codeEl.value = draft.newItem.code; }
    await refreshNewSkuCheck();
  };
  catEl.onchange = async () => {
    draft.newItem.category = catEl.value;
    if (!draft.newItem.skuTouched) { await refreshGeneratedSku(); codeEl.value = draft.newItem.code; }
    await refreshNewSkuCheck();
  };
  idEl.onchange = () => {
    draft.newItem.identity = idEl.value;
    draft.serialUnits = [];
    renderSerialArea();
    updateComputedDisplays();
  };
  codeEl.oninput = async () => {
    draft.newItem.skuTouched = true;
    draft.newItem.code = codeEl.value.trim().toUpperCase();
    await refreshNewSkuCheck();
  };
  regenBtn.onclick = async () => {
    draft.newItem.skuTouched = false;
    await refreshGeneratedSku();
    codeEl.value = draft.newItem.code;
    await refreshNewSkuCheck();
  };
}

/* ---- Price entry mode toggle (per-unit <-> total — genuinely converts the
 * value, same as purchase_entry.js, never just reinterprets raw digits). ---- */

function setPriceEntryMode(mode) {
  if (draft.priceEntryMode !== mode) {
    draft.priceValue = convertPriceValue(draft.priceValue || 0, draft.qty || 1, draft.priceEntryMode, mode);
  }
  draft.priceEntryMode = mode;
  document.getElementById("oi-pricevalue").value = draft.priceValue;
  document.querySelectorAll(".price-toggle-group .mtbtn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  });
  updateComputedDisplays();
}

/* ---- Serial area (single unit inline for qty=1, an expandable per-unit
 * panel for qty>1) — same shape as purchase_entry.js's serialAreaHTML. ---- */

function currentLineTotal() {
  const qty = draft.qty || 1;
  return draft.priceEntryMode === "per_unit"
    ? Math.round((draft.priceValue || 0) * qty)
    : Math.round(draft.priceValue || 0);
}

function defaultSerialUnits() {
  const lineTotal = currentLineTotal();
  const costs = splitSerialUnitCosts(lineTotal, draft.qty);
  const existing = draft.serialUnits || [];
  return Array.from({ length: draft.qty }, (_, i) => ({
    serial: (existing[i] && existing[i].serial) || "",
    cost: costs[i],
    costTouched: (existing[i] && existing[i].costTouched) || false,
  }));
}

function renderSerialArea() {
  const container = document.getElementById("oi-serial-area");
  if (currentIdentity() !== "serialized") { container.innerHTML = ""; return; }
  const qty = draft.qty || 1;
  if (draft.serialUnits.length !== qty) draft.serialUnits = defaultSerialUnits();

  let html = `<div class="control-box"><div class="control-box-title">Serial / Asset IDs</div>`;
  if (qty === 1) {
    const u = draft.serialUnits[0] || { serial: "", cost: 0 };
    html += `<div class="serial-single">
      <div class="lfield" style="max-width:260px;"><label>Serial / Asset ID</label>
        <div style="display:flex; gap:8px;">
          <input type="text" id="oi-serial-0" value="${escapeAttr(u.serial || "")}" placeholder="Case/serial number or internal tag">
          <button type="button" class="btn btn-sm" id="oi-generate-serials">⚡ Generate</button>
        </div>
        <div class="serial-check" id="oi-serial-check-0"></div>
      </div>
    </div>`;
  } else {
    const toggleLabel = draft.serialPanelOpen ? `▾ Collapse serial assignment (${qty} units)` : `▸ Assign ${qty} serial numbers →`;
    html += `<div class="serial-toggle" id="oi-serial-toggle">${toggleLabel}</div>`;
    if (draft.serialPanelOpen) {
      html += `<div class="serial-panel">
        <div class="serial-panel-head">
          <div class="serial-row-head"><div>#</div><div>Serial / Asset ID</div><div>Cost (IDR)</div></div>
          <button type="button" class="btn btn-sm" id="oi-generate-serials">⚡ Generate all</button>
        </div>
        ${draft.serialUnits.map((u, i) => `
          <div class="serial-row">
            <div>${i + 1}</div>
            <input type="text" id="oi-serial-${i}" value="${escapeAttr(u.serial || "")}" placeholder="Serial / asset ID">
            <input type="number" id="oi-serialcost-${i}" value="${u.cost || 0}" step="1000">
          </div>`).join("")}
        <div class="serial-check" id="oi-serial-check"></div>
      </div>`;
    }
  }
  html += `</div>`;
  container.innerHTML = html;
  wireSerialAreaEvents();
}

function wireSerialAreaEvents() {
  if (currentIdentity() !== "serialized") return;
  const qty = draft.qty || 1;

  const toggle = document.getElementById("oi-serial-toggle");
  if (toggle) toggle.onclick = () => { draft.serialPanelOpen = !draft.serialPanelOpen; renderSerialArea(); updateComputedDisplays(); };

  const genBtn = document.getElementById("oi-generate-serials");
  if (genBtn) genBtn.onclick = generateSerialsAndRefresh;

  if (qty === 1) {
    const s0 = document.getElementById("oi-serial-0");
    if (s0) s0.oninput = () => { draft.serialUnits[0] = draft.serialUnits[0] || { serial: "", cost: 0 }; draft.serialUnits[0].serial = s0.value; updateComputedDisplays(); };
  } else if (draft.serialPanelOpen) {
    draft.serialUnits.forEach((u, i) => {
      const sEl = document.getElementById(`oi-serial-${i}`);
      const cEl = document.getElementById(`oi-serialcost-${i}`);
      if (sEl) sEl.oninput = () => { draft.serialUnits[i].serial = sEl.value; updateComputedDisplays(); };
      if (cEl) cEl.oninput = () => { draft.serialUnits[i].cost = Number(cEl.value) || 0; draft.serialUnits[i].costTouched = true; updateComputedDisplays(); };
    });
  }
}

async function generateSerialsAndRefresh() {
  const sku = currentSku();
  if (!sku) { showToast("Enter/confirm a SKU before generating serial IDs."); return; }
  let existingCount = 0;
  if (draft.itemMode === "existing") {
    const data = await apiGet(`/api/items/${encodeURIComponent(sku)}/serial-count`);
    existingCount = data.existing_count;
  }
  const qty = draft.qty || 1;
  if (draft.serialUnits.length !== qty) draft.serialUnits = defaultSerialUnits();
  const generated = generateSerialsForLine(sku, qty, existingCount, []);
  draft.serialUnits.forEach((u, i) => { u.serial = generated[i]; });
  renderSerialArea();
  updateComputedDisplays();
}

/* ---- Recompute + gating ---- */

function findDuplicateSerials() {
  const ids = (draft.serialUnits || []).map(u => (u.serial || "").trim().toUpperCase()).filter(Boolean);
  const counts = new Map();
  ids.forEach(id => counts.set(id, (counts.get(id) || 0) + 1));
  return new Set([...counts.entries()].filter(([, c]) => c > 1).map(([k]) => k));
}

function allSerialsAssigned() {
  if (currentIdentity() !== "serialized") return true;
  if (!draft.serialUnits || draft.serialUnits.length !== draft.qty) return false;
  return draft.serialUnits.every(u => (u.serial || "").trim().length > 0);
}

function itemChosen() {
  if (draft.itemMode === "existing") return !!draft.existingSku;
  return !!(draft.newItem.code && draft.newItem.name && draft.newItem.category);
}

function updateComputedDisplays() {
  const lineTotal = currentLineTotal();
  document.getElementById("oi-total-cost").textContent = fmtIDR(lineTotal);

  const mirror = document.getElementById("oi-price-mirror");
  if (mirror) {
    if (draft.priceEntryMode === "per_unit") mirror.innerHTML = `Total: <b>${fmtIDR(lineTotal)}</b>`;
    else { const perUnit = (draft.qty || 0) > 0 ? (draft.priceValue || 0) / draft.qty : 0; mirror.innerHTML = `≈ Per unit: <b>${fmtIDR(perUnit)}</b>`; }
  }

  const dupes = findDuplicateSerials();
  const identity = currentIdentity();
  if (identity === "serialized" && draft.qty === 1 && draft.serialUnits[0]) {
    if (!draft.serialUnits[0].costTouched) draft.serialUnits[0].cost = lineTotal;
    const check0 = document.getElementById("oi-serial-check-0");
    if (check0) {
      const serial0 = (draft.serialUnits[0].serial || "").trim().toUpperCase();
      check0.innerHTML = (serial0 && dupes.has(serial0))
        ? `<span style="color:var(--red-text);">✗ This Serial/Asset ID duplicates another unit.</span>` : "";
    }
  }
  if (identity === "serialized" && draft.qty > 1 && draft.serialPanelOpen) {
    const check = document.getElementById("oi-serial-check");
    if (check) {
      const sum = draft.serialUnits.reduce((s, u) => s + (u.cost || 0), 0);
      const ok = sum === lineTotal;
      let msg = ok
        ? `<span style="color:var(--green-text);">✓ ${draft.serialUnits.length} units allocate ${fmtIDR(sum)} of the ${fmtIDR(lineTotal)} total</span>`
        : `<span style="color:var(--red-text);">✗ ${draft.serialUnits.length} units allocate ${fmtIDR(sum)} — total is ${fmtIDR(lineTotal)} (${fmtIDRSigned(lineTotal - sum)})</span>`;
      if (dupes.size > 0) msg += `<br><span style="color:var(--red-text);">✗ One or more Serial/Asset IDs duplicate another unit.</span>`;
      check.innerHTML = msg;
    }
  }

  const submitBtn = document.getElementById("oi-submit-btn");
  const detailEl = document.getElementById("oi-panel-detail");
  let disabledReason = "";
  if (!itemChosen()) disabledReason = "Pick an existing item, or fill in Name/Category/SKU for a new item.";
  else if (!draft.qty || draft.qty <= 0) disabledReason = "Enter a quantity of at least 1.";
  else if (!allSerialsAssigned()) disabledReason = "Assign a Serial/Asset ID to every unit before saving.";
  else if (dupes.size > 0) disabledReason = "Two or more serialized units share the same Serial/Asset ID.";
  else if (identity === "serialized" && draft.qty > 1 && draft.serialUnits.reduce((s, u) => s + (u.cost || 0), 0) !== lineTotal) {
    disabledReason = "Per-unit costs must sum exactly to the total cost above.";
  }
  submitBtn.disabled = !!disabledReason;
  submitBtn.title = disabledReason;
  detailEl.textContent = disabledReason || `Ready to save — ${fmtIDR(lineTotal)} for ${draft.qty} unit(s).`;
}

/* ---- Save — always goes through the real server engine ---- */

function buildPayload() {
  const body = {
    quantity: draft.qty,
    price_entry_mode: draft.priceEntryMode,
    price_value: draft.priceValue,
    entry_date: draft.date,
    vendor_description: draft.vendor || null,
  };
  if (draft.itemMode === "existing") {
    body.sku = draft.existingSku;
  } else {
    body.new_item = {
      name: draft.newItem.name,
      category_code: draft.newItem.category,
      identity_mode: draft.newItem.identity,
      sku: draft.newItem.code || null,
    };
  }
  if (currentIdentity() === "serialized") {
    body.serial_units = draft.serialUnits.map(u => ({
      serial_id: (u.serial || "").trim() || null,
      cost: u.costTouched ? u.cost : null,
    }));
  }
  return body;
}

async function submitEntry() {
  const errBox = document.getElementById("oi-server-error");
  const successBox = document.getElementById("oi-success");
  errBox.style.display = "none";
  successBox.style.display = "none";
  const payload = buildPayload();
  try {
    const saved = await apiPost("/api/opening-inventory", payload);
    if (!saved) return;
    showToast(`Opening inventory entry saved — ${fmtIDR(saved.allocation.total_amount_paid)} (${saved.purchase_ref}).`);
    successBox.style.display = "block";
    successBox.innerHTML = `Saved as ${escapeAttr(saved.purchase_ref)} — <a href="${appUrl("/purchases?ref=" + encodeURIComponent(saved.purchase_ref))}">view in Purchase History</a>. Enter another item below.`;

    // Reset the item/quantity/value fields for the next entry — date and
    // vendor label are left as-is, since onboarding many items in one
    // sitting usually keeps both the same.
    draft.itemMode = "existing";
    draft.existingSku = ""; draft.existingIdentity = "fungible";
    draft.newItem = { code: "", name: "", category: (categoriesCache[0] || {}).code || "", identity: "fungible", skuTouched: false };
    draft.qty = 1; draft.priceEntryMode = "per_unit"; draft.priceValue = 0;
    draft.serialUnits = []; draft.serialPanelOpen = false;

    document.querySelector('input[name="oi-item-mode"][value="existing"]').checked = true;
    document.getElementById("oi-existing-item-form").style.display = "block";
    document.getElementById("oi-new-item-form").style.display = "none";
    document.getElementById("oi-existing-search").value = "";
    document.getElementById("oi-qty").value = 1;
    document.getElementById("oi-pricevalue").value = 0;
    setPriceEntryMode("per_unit");
    renderNewItemForm();
    renderSerialArea();
    updateComputedDisplays();
  } catch (err) {
    errBox.style.display = "block";
    const type = (err.detail && err.detail.error_type) || "Error";
    errBox.textContent = `${type}: ${err.message}`;
  }
}

initOpeningInventoryPage();
