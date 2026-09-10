/* Item Detail screen — real purchase-history / serial-unit data via
 * /api/items/{sku}.
 */

async function loadItemDetail() {
  const sku = window.ITEM_SKU;
  let data;
  try {
    data = await apiGet("/api/items/" + encodeURIComponent(sku));
  } catch (err) {
    document.getElementById("detail-header").innerHTML = `<div>Item not found.</div>`;
    return;
  }
  if (!data) return;

  document.getElementById("detail-header").innerHTML = `
    <div>
      <div class="dh-title">${escapeAttr(data.name)}</div>
      <div class="dh-sku">${escapeAttr(data.sku)} &middot; <span class="cat-tag">${escapeAttr(data.category_name || "")}</span> &middot; ${identityBadge(data.identity_mode)}</div>
    </div>
    <div class="detail-stat"><div class="label">On-Hand Qty</div><div class="value">${fmtQty(data.quantity)}</div></div>
    <div class="detail-stat"><div class="label">Total Cost Basis</div><div class="value">${fmtIDR(data.cost_basis)}</div></div>
  `;

  const body = document.getElementById("detail-body");
  if (data.identity_mode === "serialized") {
    const rows = data.serialized_units || [];
    body.innerHTML = `
      <table>
        <thead><tr><th>Serial / Asset ID</th><th>Acquisition Date</th><th class="num">Allocated Cost</th><th>Source Purchase</th><th>Photo</th></tr></thead>
        <tbody>${rows.map(u => `
          <tr><td><b>${escapeAttr(u.serial_id)}</b></td><td>${u.acquired_date}</td><td class="num">${fmtIDR(u.cost)}</td>
          <td><a class="link-btn" href="/purchases?ref=${encodeURIComponent(u.purchase_ref)}">${escapeAttr(u.purchase_ref)}</a></td>
          <td>${u.photo_reference ? escapeAttr(u.photo_reference) : `<span style="color:var(--text-dim);font-size:12px;">No photo</span>`}</td></tr>
        `).join("") || `<tr><td colspan="5" style="text-align:center;color:var(--text-dim);padding:20px;">No units on hand.</td></tr>`}</tbody>
      </table>`;
  } else {
    const rows = data.fungible_rows || [];
    body.innerHTML = `
      <table>
        <thead><tr><th>Date</th><th>Purchase Reference</th><th class="num">Qty</th><th class="num">Allocated Unit Cost</th><th class="num">Line Total</th></tr></thead>
        <tbody>${rows.map(r => `
          <tr><td>${r.purchase_date}</td><td><a class="link-btn" href="/purchases?ref=${encodeURIComponent(r.purchase_ref)}">${escapeAttr(r.purchase_ref)}</a></td>
          <td class="num">${Number(r.quantity).toLocaleString("id-ID")}</td><td class="num">${fmtIDR(r.allocated_unit_cost)}</td><td class="num">${fmtIDR(r.line_total)}</td></tr>
        `).join("") || `<tr><td colspan="5" style="text-align:center;color:var(--text-dim);padding:20px;">No purchase history yet.</td></tr>`}</tbody>
      </table>`;
  }
}

loadItemDetail();
