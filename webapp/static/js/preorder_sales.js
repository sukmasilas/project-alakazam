/* Pre-Order Sales screen — Milestone 7.
 *
 * Record a new pending pre-order sale (item picker restricted to fungible
 * items, reusing the same /api/items/search typeahead combobox pattern as
 * Purchase Entry / eBay Sales Import — see webapp/static/js/purchase_entry.js
 * and ebay_import.js), list pending/fulfilled/cancelled pre-order sales,
 * cancel a still-pending one, and fulfill a set of pending pre-order sales
 * (all for the same item) either via a brand-new purchase or an existing
 * purchase reference.
 *
 * The server (inventory/preorders.py, via webapp/api.py) is always
 * authoritative — every check here (fungible-only, same-item selection,
 * status) is a UX convenience only; a rejected request always comes back
 * as a real API error from the real engine, never a client-side illusion.
 */

let psSelectedItem = null; // { sku, name } for the "record new" form
let psRows = [];
let psSelectedIds = new Set();

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

/* ---- Item combobox for the "record new pre-order sale" form ---- */

function wireItemCombobox() {
  const input = document.getElementById("ps-item-search");
  const dropdown = document.getElementById("ps-item-dropdown");
  input.oninput = debounce(() => renderItemDropdown(input.value), 150);
  input.onfocus = () => renderItemDropdown(input.value);
  document.addEventListener("click", (e) => {
    if (!input.contains(e.target) && !dropdown.contains(e.target)) dropdown.style.display = "none";
  });
}

async function renderItemDropdown(query) {
  const dropdown = document.getElementById("ps-item-dropdown");
  const q = (query || "").trim();
  // identity=fungible: pre-order sales are fungible-only in this
  // milestone (server-enforced regardless — this only narrows the picker).
  const matches = q ? (await apiGet("/api/items/search?identity=fungible&q=" + encodeURIComponent(q) + "&limit=8")) || [] : [];
  dropdown.innerHTML = matches.length === 0
    ? `<div class="combo-empty">${q ? "No matching fungible items." : "Type to search…"}</div>`
    : matches.map(it => `<div class="combo-option" data-sku="${escapeAttr(it.sku)}" data-name="${escapeAttr(it.name)}"><b>${escapeAttr(it.sku)}</b><span>${escapeAttr(it.name)}</span></div>`).join("");
  dropdown.style.display = "block";
  dropdown.querySelectorAll("[data-sku]").forEach(opt => {
    opt.onclick = () => {
      psSelectedItem = { sku: opt.dataset.sku, name: opt.dataset.name };
      input.value = `${opt.dataset.sku} — ${opt.dataset.name}`;
      dropdown.style.display = "none";
    };
  });
}

async function submitNewPreorderSale() {
  const errorEl = document.getElementById("ps-create-error");
  errorEl.textContent = "";

  if (!psSelectedItem) {
    errorEl.textContent = "Pick an item from the dropdown first.";
    return;
  }
  const quantity = parseInt(document.getElementById("ps-qty").value, 10);
  if (!quantity || quantity <= 0) {
    errorEl.textContent = "Enter a quantity of at least 1.";
    return;
  }
  const saleDate = document.getElementById("ps-date").value || null;
  const reference = document.getElementById("ps-ref").value.trim() || null;

  try {
    await apiPost("/api/preorder-sales", { sku: psSelectedItem.sku, quantity, sale_date: saleDate, reference });
    showToast(`Recorded a pending pre-order sale for ${psSelectedItem.sku}.`);
    psSelectedItem = null;
    document.getElementById("ps-item-search").value = "";
    document.getElementById("ps-qty").value = "1";
    document.getElementById("ps-ref").value = "";
    await loadPreorderSales();
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

/* ---- List + status filter + selection for fulfillment ---- */

function statusBadge(status) {
  if (status === "pending") return '<span class="badge badge-amber">Pending</span>';
  if (status === "fulfilled") return '<span class="badge badge-green">Fulfilled</span>';
  return '<span class="badge badge-red">Cancelled</span>';
}

async function loadPreorderSales() {
  const status = document.getElementById("ps-status-filter").value;
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  psRows = (await apiGet("/api/preorder-sales?" + params.toString())) || [];
  // Drop selections for rows no longer in view / no longer pending.
  const visiblePendingIds = new Set(psRows.filter(r => r.status === "pending").map(r => r.id));
  psSelectedIds = new Set([...psSelectedIds].filter(id => visiblePendingIds.has(id)));
  renderPreorderList();
  updateFulfillButton();
}

function renderPreorderList() {
  const container = document.getElementById("ps-list");
  if (psRows.length === 0) {
    container.innerHTML = `<div class="subtitle">No pre-order sales in this view.</div>`;
    return;
  }
  container.innerHTML = `
    <table>
      <thead><tr>
        <th></th><th>ID</th><th>Sale Date</th><th>Item</th>
        <th class="num">Qty</th><th>Reference</th><th>Status</th><th></th>
      </tr></thead>
      <tbody>${psRows.map(r => `
        <tr>
          <td>${r.status === "pending" ? `<input type="checkbox" data-select="${r.id}" data-sku="${escapeAttr(r.sku)}" ${psSelectedIds.has(r.id) ? "checked" : ""}>` : ""}</td>
          <td>#${r.id}</td>
          <td>${r.sale_date}</td>
          <td><a class="link-btn" href="${appUrl("/items/" + encodeURIComponent(r.sku))}">${escapeAttr(r.sku)}</a><br><span class="num-inline">${escapeAttr(r.item_name)}</span></td>
          <td class="num">${Number(r.quantity).toLocaleString("id-ID")}</td>
          <td>${r.reference ? escapeAttr(r.reference) : `<span style="color:var(--text-dim);font-size:12px;">—</span>`}</td>
          <td>${statusBadge(r.status)}</td>
          <td>${r.status === "pending" ? `<button class="btn btn-sm" data-cancel="${r.id}">Cancel</button>` : ""}</td>
        </tr>`).join("")}</tbody>
    </table>`;

  container.querySelectorAll("[data-select]").forEach(cb => {
    cb.onchange = () => {
      if (cb.checked) psSelectedIds.add(Number(cb.dataset.select));
      else psSelectedIds.delete(Number(cb.dataset.select));
      updateFulfillButton();
    };
  });
  container.querySelectorAll("[data-cancel]").forEach(btn => {
    btn.onclick = () => cancelPreorderSale(Number(btn.dataset.cancel));
  });
}

async function cancelPreorderSale(id) {
  if (!window.confirm(`Cancel pending pre-order sale #${id}? This cannot be undone.`)) return;
  try {
    await apiPost(`/api/preorder-sales/${id}/cancel`, {});
    showToast(`Pre-order sale #${id} cancelled.`);
    await loadPreorderSales();
  } catch (err) {
    showToast("Could not cancel: " + err.message);
  }
}

function selectedRows() {
  return psRows.filter(r => psSelectedIds.has(r.id));
}

function updateFulfillButton() {
  const btn = document.getElementById("ps-fulfill-btn");
  btn.disabled = psSelectedIds.size === 0;
}

/* ---- Fulfillment panel ---- */

function openFulfillPanel() {
  const rows = selectedRows();
  if (rows.length === 0) return;
  const skus = new Set(rows.map(r => r.sku));
  const panel = document.getElementById("ps-fulfill-panel");

  if (skus.size > 1) {
    panel.style.display = "block";
    panel.innerHTML = `<div class="control-box"><div style="color:var(--red-text);font-size:13px;">
      Selected pre-order sales must all be for the SAME item — you selected ${skus.size} different items
      (${[...skus].join(", ")}). Deselect down to one item and try again.
    </div></div>`;
    return;
  }

  const sku = [...skus][0];
  const totalQty = rows.reduce((sum, r) => sum + r.quantity, 0);

  panel.style.display = "block";
  panel.innerHTML = `
    <div class="control-box">
      <div class="control-box-title">Fulfill ${rows.length} pre-order sale(s) for ${escapeAttr(sku)} (${totalQty} unit(s) total)</div>
      <div class="subtitle" style="margin-bottom:10px;">Fulfillment posts a real purchase (through the same engine as Purchase Entry) immediately followed by one depletion per selected pre-order sale, at the resulting weighted-average cost. This is atomic — if anything fails, nothing is saved.</div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;">
        <label style="font-size:13px;"><input type="radio" name="ps-fulfill-mode" value="new" checked> Create a new purchase</label>
        <label style="font-size:13px;"><input type="radio" name="ps-fulfill-mode" value="existing"> Use an existing purchase (by reference)</label>
      </div>
      <div id="ps-fulfill-new-form">
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;">
          <div class="lfield"><label>Purchase Date</label><input type="date" id="pf-date"></div>
          <div class="lfield" style="flex:1;min-width:200px;"><label>Vendor / Description</label><input type="text" id="pf-vendor" placeholder="e.g. Toko Grosir Jaya"></div>
          <div class="lfield"><label>Quantity Purchased</label><input type="number" id="pf-qty" min="${totalQty}" value="${totalQty}" style="width:110px;"></div>
          <div class="lfield"><label>Price Entry</label>
            <select id="pf-price-mode"><option value="per_unit">Per unit</option><option value="total">Total for line</option></select>
          </div>
          <div class="lfield"><label id="pf-price-label">Price per unit (IDR)</label><input type="number" id="pf-price-value" min="0" step="1" style="width:150px;"></div>
        </div>
      </div>
      <div id="ps-fulfill-existing-form" style="display:none;">
        <div class="lfield" style="max-width:300px;"><label>Purchase Reference</label><input type="text" id="pf-purchase-ref" placeholder="e.g. PUR-2026-0001"></div>
      </div>
      <div style="margin-top:12px;display:flex;gap:10px;align-items:center;">
        <button class="btn btn-primary btn-sm" id="pf-submit">Fulfill</button>
        <button class="btn btn-sm" id="pf-cancel">Cancel</button>
        <span id="pf-error" style="color:var(--red-text);font-size:12.5px;"></span>
      </div>
    </div>
  `;

  panel.querySelectorAll('input[name="ps-fulfill-mode"]').forEach(radio => {
    radio.onchange = () => {
      const isNew = panel.querySelector('input[name="ps-fulfill-mode"]:checked').value === "new";
      document.getElementById("ps-fulfill-new-form").style.display = isNew ? "block" : "none";
      document.getElementById("ps-fulfill-existing-form").style.display = isNew ? "none" : "block";
    };
  });
  document.getElementById("pf-price-mode").onchange = (e) => {
    document.getElementById("pf-price-label").textContent =
      e.target.value === "total" ? "Total price for this line (IDR)" : "Price per unit (IDR)";
  };
  document.getElementById("pf-cancel").onclick = () => { panel.style.display = "none"; panel.innerHTML = ""; };
  document.getElementById("pf-submit").onclick = () => submitFulfillment(sku, rows.map(r => r.id));
}

async function submitFulfillment(sku, preorderSaleIds) {
  const errorEl = document.getElementById("pf-error");
  errorEl.textContent = "";
  const mode = document.querySelector('input[name="ps-fulfill-mode"]:checked').value;

  const body = { preorder_sale_ids: preorderSaleIds };

  if (mode === "existing") {
    const ref = document.getElementById("pf-purchase-ref").value.trim();
    if (!ref) { errorEl.textContent = "Enter a purchase reference."; return; }
    let detail;
    try {
      detail = await apiGet("/api/purchases/" + encodeURIComponent(ref));
    } catch (err) {
      errorEl.textContent = `Could not find purchase ${ref}: ${err.message}`;
      return;
    }
    if (!detail) return;
    body.purchase_id = detail.id;
  } else {
    const purchaseDate = document.getElementById("pf-date").value;
    const vendor = document.getElementById("pf-vendor").value.trim();
    const qty = parseInt(document.getElementById("pf-qty").value, 10);
    const priceMode = document.getElementById("pf-price-mode").value;
    const priceValue = Number(document.getElementById("pf-price-value").value);
    if (!purchaseDate) { errorEl.textContent = "Purchase date is required."; return; }
    if (!vendor) { errorEl.textContent = "Vendor / description is required."; return; }
    if (!qty || qty <= 0) { errorEl.textContent = "Quantity purchased must be at least 1."; return; }
    if (!priceValue || priceValue <= 0) { errorEl.textContent = "Enter a price greater than zero."; return; }

    // Single-line, no-shipping purchase — total_amount_paid must equal
    // this line's total exactly (the reconciliation invariant, enforced
    // server-side regardless of what's computed here).
    const lineTotal = priceMode === "per_unit" ? Math.round(priceValue * qty) : Math.round(priceValue);

    body.purchase = {
      purchase_date: purchaseDate,
      vendor_description: vendor,
      total_amount_paid: String(lineTotal),
      currency: "IDR",
      shipping_mode: "none",
      lump_sum_active: false,
      lines: [{
        sku: sku,
        quantity: qty,
        pricing_mode: "direct",
        price_entry_mode: priceMode,
        price_value: String(priceValue),
        ships_separately: false,
      }],
    };
  }

  try {
    const result = await apiPost("/api/preorder-sales/fulfill", body);
    if (!result) return;
    showToast(`Fulfilled ${result.fulfilled.length} pre-order sale(s) via purchase #${result.purchase_id}.`);
    document.getElementById("ps-fulfill-panel").style.display = "none";
    document.getElementById("ps-fulfill-panel").innerHTML = "";
    psSelectedIds = new Set();
    await loadPreorderSales();
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

/* ---- Wiring ---- */

wireItemCombobox();
document.getElementById("ps-submit").onclick = submitNewPreorderSale;
document.getElementById("ps-refresh").onclick = loadPreorderSales;
document.getElementById("ps-status-filter").onchange = loadPreorderSales;
document.getElementById("ps-fulfill-btn").onclick = openFulfillPanel;
loadPreorderSales();
