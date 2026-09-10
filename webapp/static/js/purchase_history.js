/* Purchase History screen — real, read-only ledger via /api/purchases and
 * /api/purchases/{ref} (line-level detail, fetched lazily on expand).
 */

let purchaseSummaries = [];
let openRefs = new Set();
let detailCache = {};

function lumpsumMethodLabel(m) { return m === "equal" ? "Equally" : m === "by_weight" ? "By Weight" : m === "by_value" ? "By Value" : m; }

function shippingModeLabel(p) {
  if (p.shipping_mode === "none") return "No shipping";
  if (p.shipping_mode === "manual") return "Manual (per line)";
  return `Pooled — ${fmtIDR(p.pooled_shipping_total)} split ${lumpsumMethodLabel(p.pooled_shipping_method)}`;
}

function lineCostLabel(l) {
  if (l.pricing_mode === "lumpsum_group") return "Lump-sum group";
  return l.price_entry_mode === "per_unit" ? `Direct — ${fmtIDR(l.price_value)}/unit` : `Direct — ${fmtIDR(l.price_value)} total`;
}
function lineShippingLabel(p, l) {
  if (p.shipping_mode === "none") return "—";
  if (p.shipping_mode === "manual") return `Manual: ${fmtIDR(l.manual_shipping_amount || 0)}`;
  if (l.ships_separately) return `Ships separately: ${fmtIDR(l.manual_shipping_amount || 0)}`;
  return "Pooled share";
}

async function loadPurchaseHistory() {
  purchaseSummaries = await apiGet("/api/purchases") || [];
  if (window.AUTO_OPEN_REF) openRefs.add(window.AUTO_OPEN_REF);
  renderHistoryList();
}

function renderHistoryList() {
  const container = document.getElementById("history-list");
  if (purchaseSummaries.length === 0) {
    container.innerHTML = `<div class="subtitle">No purchases recorded yet.</div>`;
    return;
  }
  container.innerHTML = purchaseSummaries.map(p => {
    const open = openRefs.has(p.purchase_ref);
    return `
    <div class="hist-row ${open ? "open" : ""}" id="hist-${escapeAttr(p.purchase_ref)}">
      <div class="hist-summary" data-ref="${escapeAttr(p.purchase_ref)}">
        <div>${p.purchase_date}</div>
        <div><b>${escapeAttr(p.purchase_ref)}</b></div>
        <div>${escapeAttr(p.vendor_description)}</div>
        <div class="num">${fmtIDR(p.total_amount_paid)}</div>
        <div>${p.line_count} line(s)</div>
        <div><span class="badge badge-green">Balanced</span></div>
        <div class="chevron">▶</div>
      </div>
      <div class="hist-detail" id="hist-detail-${escapeAttr(p.purchase_ref)}">${open ? "" : ""}</div>
    </div>`;
  }).join("");

  container.querySelectorAll(".hist-summary").forEach(node => {
    node.onclick = () => toggleHistoryRow(node.dataset.ref);
  });

  openRefs.forEach(ref => renderDetail(ref));
}

async function toggleHistoryRow(ref) {
  const row = document.getElementById("hist-" + ref);
  if (openRefs.has(ref)) {
    openRefs.delete(ref);
    row.classList.remove("open");
  } else {
    openRefs.add(ref);
    row.classList.add("open");
    await renderDetail(ref);
  }
}

async function renderDetail(ref) {
  const el = document.getElementById("hist-detail-" + ref);
  if (!el) return;
  el.innerHTML = `<div class="subtitle">Loading…</div>`;
  if (!detailCache[ref]) {
    detailCache[ref] = await apiGet("/api/purchases/" + encodeURIComponent(ref));
  }
  const p = detailCache[ref];
  if (!p) { el.innerHTML = `<div class="subtitle">Not found.</div>`; return; }

  let html = `<div class="hist-meta"><span>Shipping: ${shippingModeLabel(p)}</span>`;
  if (p.lump_sum_active) html += `<span>Lump-Sum Group: ${fmtIDR(p.lump_sum_total)} split ${lumpsumMethodLabel(p.lump_sum_method)}</span>`;
  html += `</div>`;

  if (p.invoice_document_ref) {
    const parsed = p.invoice_ocr_status === "parsed";
    const fields = p.invoice_parsed_fields || {};
    html += `<div class="hist-invoice"><b>📎 ${escapeAttr(p.invoice_document_ref)}</b> `;
    html += parsed
      ? `<span class="badge badge-green">Parsed</span> <span class="hist-invoice-fields">Read: ${escapeAttr(fields.date || "")} &middot; ${escapeAttr(fields.vendor || "")} &middot; ${fields.amount != null ? fmtIDR(fields.amount) : ""}</span>`
      : `<span class="badge badge-amber">Needs Review</span> <span class="hist-invoice-fields">This invoice couldn't be read automatically — fields were entered manually.</span>`;
    html += `</div>`;
  } else {
    html += `<div class="hist-invoice-none">No invoice / proof of transfer attached.</div>`;
  }

  html += `<table class="mini-table">
    <thead><tr><th>SKU</th><th>Qty</th><th>Item Cost</th><th>Shipping</th><th class="num">Line Total</th></tr></thead>
    <tbody>`;
  p.lines.forEach(l => {
    html += `<tr>
      <td><b>${escapeAttr(l.sku)}</b><br><span class="num-inline">${escapeAttr(l.item_name)}</span></td>
      <td class="num">${l.quantity}</td>
      <td>${lineCostLabel(l)}<br><span class="num-inline">${fmtIDR(l.allocated_item_cost)}</span></td>
      <td>${lineShippingLabel(p, l)}<br><span class="num-inline">${fmtIDR(l.shipping_share)}</span></td>
      <td class="num">${fmtIDR(l.line_total)}</td>
    </tr>`;
    if (l.identity_mode === "serialized" && l.serial_units.length) {
      html += `<tr><td colspan="5" style="padding-left:26px;background:#faf9fd;">
        <b style="font-size:11.5px;color:var(--text-dim);">Assigned units:</b><br>
        ${l.serial_units.map(u => `<span class="unit-chip">${escapeAttr(u.serial_id)} — ${fmtIDR(u.cost)}${u.photo_reference ? " 📷" : ""}</span>`).join("")}
      </td></tr>`;
    }
  });
  html += `</tbody></table>`;
  el.innerHTML = html;
}

loadPurchaseHistory();
