/* Sales / Depletion Log screen — real, read-only ledger via
 * /api/depletions (optionally ?sku=... when linked-to from Item Detail).
 * Mirrors Purchase History's list shape; no expand-to-accordion needed
 * here since one depletion event has no line-item structure to drill
 * into — it's already a single flat fact.
 */

async function loadSalesLog() {
  const sku = window.SALES_FILTER_SKU || "";
  const noteEl = document.getElementById("sales-filter-note");
  if (sku) {
    noteEl.style.display = "block";
    noteEl.innerHTML = `Showing only <b>${escapeAttr(sku)}</b> — <a class="link-btn" href="${appUrl("/sales")}">clear filter</a>`;
  }

  const params = new URLSearchParams();
  if (sku) params.set("sku", sku);
  const rows = await apiGet("/api/depletions?" + params.toString()) || [];
  renderSalesLog(rows);
}

function depletionTypeLabel(row) {
  return row.depletion_type === "serialized"
    ? '<span class="badge badge-serialized">Serialized unit</span>'
    : '<span class="badge badge-fungible">Fungible</span>';
}

function renderSalesLog(rows) {
  const container = document.getElementById("sales-list");
  if (rows.length === 0) {
    container.innerHTML = `<div class="subtitle">No depletion events recorded yet.</div>`;
    return;
  }
  container.innerHTML = `
    <table>
      <thead><tr>
        <th>Date</th><th>Item</th><th>Type</th>
        <th class="num">Qty</th><th class="num">Unit Cost</th>
        <th class="num">Cost of Goods Depleted</th><th>Reference</th>
      </tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td>${r.depletion_date}</td>
          <td><a class="link-btn" href="${appUrl("/items/" + encodeURIComponent(r.sku))}">${escapeAttr(r.sku)}</a><br><span class="num-inline">${escapeAttr(r.item_name)}</span></td>
          <td>${depletionTypeLabel(r)}</td>
          <td class="num">${Number(r.quantity).toLocaleString("id-ID")}</td>
          <td class="num">${fmtIDR(r.unit_cost)}</td>
          <td class="num">${fmtIDR(r.total_cost)}</td>
          <td>${r.reference ? escapeAttr(r.reference) : `<span style="color:var(--text-dim);font-size:12px;">—</span>`}</td>
        </tr>`).join("")}</tbody>
    </table>`;
}

loadSalesLog();
