/* Item Detail screen — real purchase-history / serial-unit data via
 * /api/items/{sku}, plus (Milestone 5) a real "Mark as Sold" action via
 * /api/items/{sku}/deplete (fungible) and
 * /api/items/{sku}/serial-units/{serial_id}/deplete (serialized).
 *
 * The server is always authoritative for the negative-stock check — the
 * quantity input's `max` attribute below is guidance only, never trusted;
 * a rejected/over-large request always comes back as a real API error
 * from the real inventory.depletions engine, never a client-side illusion.
 */

let lastItemData = null;

async function loadItemDetail() {
  const sku = window.ITEM_SKU;
  document.getElementById("sales-log-link").href = appUrl("/sales?sku=" + encodeURIComponent(sku));

  let data;
  try {
    data = await apiGet("/api/items/" + encodeURIComponent(sku));
  } catch (err) {
    document.getElementById("detail-header").innerHTML = `<div>Item not found.</div>`;
    return;
  }
  if (!data) return;
  lastItemData = data;

  const consignorTag = data.consignor_id
    ? ` &middot; <span class="badge badge-amber">Consigned — ${escapeAttr(data.consignor_name || ("consignor #" + data.consignor_id))}</span>`
    : "";
  document.getElementById("detail-header").innerHTML = `
    <div>
      <div class="dh-title">${escapeAttr(data.name)}</div>
      <div class="dh-sku">${escapeAttr(data.sku)} &middot; <span class="cat-tag">${escapeAttr(data.category_name || "")}</span> &middot; ${identityBadge(data.identity_mode)}${consignorTag}</div>
    </div>
    <div class="detail-stat"><div class="label">On-Hand Qty</div><div class="value">${fmtQty(data.quantity)}</div></div>
    <div class="detail-stat"><div class="label">Total Cost Basis</div><div class="value">${fmtIDR(data.cost_basis)}</div></div>
  `;

  renderMarkSoldPanel(data);

  const body = document.getElementById("detail-body");
  if (data.identity_mode === "serialized") {
    const rows = data.serialized_units || [];
    body.innerHTML = `
      <table>
        <thead><tr><th>Serial / Asset ID</th><th>Acquisition Date</th><th class="num">Allocated Cost</th><th>Source Purchase</th><th>Photo</th><th>Status</th></tr></thead>
        <tbody>${rows.map(u => `
          <tr><td><b>${escapeAttr(u.serial_id)}</b></td><td>${u.acquired_date}</td><td class="num">${fmtIDR(u.cost)}</td>
          <td><a class="link-btn" href="${appUrl("/purchases?ref=" + encodeURIComponent(u.purchase_ref))}">${escapeAttr(u.purchase_ref)}</a></td>
          <td>${u.photo_reference ? escapeAttr(u.photo_reference) : `<span style="color:var(--text-dim);font-size:12px;">No photo</span>`}</td>
          <td>${renderSerialStatusCell(data.sku, u)}</td></tr>
        `).join("") || `<tr><td colspan="6" style="text-align:center;color:var(--text-dim);padding:20px;">No units on hand.</td></tr>`}</tbody>
      </table>`;
    wireSerialMarkSoldButtons(data.sku);
  } else {
    const rows = data.fungible_rows || [];
    body.innerHTML = `
      <table>
        <thead><tr><th>Date</th><th>Purchase Reference</th><th class="num">Qty</th><th class="num">Allocated Unit Cost</th><th class="num">Line Total</th></tr></thead>
        <tbody>${rows.map(r => `
          <tr><td>${r.purchase_date}</td><td><a class="link-btn" href="${appUrl("/purchases?ref=" + encodeURIComponent(r.purchase_ref))}">${escapeAttr(r.purchase_ref)}</a></td>
          <td class="num">${Number(r.quantity).toLocaleString("id-ID")}</td><td class="num">${fmtIDR(r.allocated_unit_cost)}</td><td class="num">${fmtIDR(r.line_total)}</td></tr>
        `).join("") || `<tr><td colspan="5" style="text-align:center;color:var(--text-dim);padding:20px;">No purchase history yet.</td></tr>`}</tbody>
      </table>`;
  }
}

/* ---- Fungible: quantity + reference form ---- */

function renderMarkSoldPanel(data) {
  const panel = document.getElementById("mark-sold-panel");
  if (data.identity_mode !== "fungible") {
    // Serialized items get a per-row action instead (see
    // wireSerialMarkSoldButtons) — no purchase-wide panel needed.
    panel.innerHTML = "";
    return;
  }
  panel.innerHTML = `
    <div class="mark-sold-panel">
      <div class="msp-title">Mark as Sold</div>
      <div class="mark-sold-row">
        <div class="msfield">
          <label>Quantity (max ${Number(data.quantity).toLocaleString("id-ID")})</label>
          <input type="number" id="ms-qty" min="1" max="${data.quantity}" value="${data.quantity > 0 ? 1 : 0}" ${data.quantity === 0 ? "disabled" : ""}>
        </div>
        <div class="msfield" style="flex:1;min-width:200px;">
          <label>Reference (optional)</label>
          <input type="text" id="ms-ref" placeholder="e.g. eBay order #12345">
        </div>
        <button class="btn btn-primary btn-sm" id="ms-submit" ${data.quantity === 0 ? "disabled" : ""}>Mark as Sold</button>
      </div>
      <div class="mark-sold-error" id="ms-error"></div>
    </div>
  `;
  document.getElementById("ms-submit").onclick = () => submitFungibleDepletion(data.sku);
}

async function submitFungibleDepletion(sku) {
  const qtyInput = document.getElementById("ms-qty");
  const refInput = document.getElementById("ms-ref");
  const errorEl = document.getElementById("ms-error");
  errorEl.textContent = "";

  const quantity = parseInt(qtyInput.value, 10);
  if (!quantity || quantity <= 0) {
    errorEl.textContent = "Enter a quantity of at least 1.";
    return;
  }

  try {
    const result = await apiPost(`/api/items/${encodeURIComponent(sku)}/deplete`, {
      quantity: quantity,
      reference: refInput.value.trim() || null,
    });
    if (!result) return;
    showToast(`Depleted ${quantity} unit(s) of ${sku} — Rp ${Number(result.total_cost).toLocaleString("id-ID")} cost of goods.`);
    await loadItemDetail();
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

/* ---- Serialized: per-row action ---- */

function renderSerialStatusCell(sku, unit) {
  if (unit.status === "sold") {
    const ref = unit.sold_reference ? ` &middot; ${escapeAttr(unit.sold_reference)}` : "";
    return `<span class="badge badge-sold">Sold ${unit.sold_date || ""}</span>${ref}`;
  }
  return `<div class="serial-unit-actions">
    <span class="badge badge-green">On Hand</span>
    <button class="btn btn-sm mark-sold-serial-btn" data-serial="${escapeAttr(unit.serial_id)}">Mark as Sold</button>
  </div>`;
}

function wireSerialMarkSoldButtons(sku) {
  document.querySelectorAll("[data-serial]").forEach(btn => {
    btn.onclick = () => markSerialUnitSold(sku, btn.dataset.serial);
  });
}

async function markSerialUnitSold(sku, serialId) {
  const reference = window.prompt(`Optional reference for ${serialId} (e.g. eBay order #):`, "");
  if (reference === null) return; // user cancelled
  try {
    await apiPost(`/api/items/${encodeURIComponent(sku)}/serial-units/${encodeURIComponent(serialId)}/deplete`, {
      reference: reference.trim() || null,
    });
    showToast(`${serialId} marked as sold.`);
    await loadItemDetail();
  } catch (err) {
    showToast("Could not mark as sold: " + err.message);
  }
}

loadItemDetail();
