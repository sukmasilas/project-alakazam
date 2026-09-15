/* Consignor Reimbursements screen — Milestone 8.
 *
 * A paid/unpaid STATUS per sold consigned unit, filterable by consignor
 * and/or status, with a "Mark as Paid" action on unpaid rows. Never shows
 * or asks for an amount — see inventory/consignment.py's own module
 * docstring for why (payout amount computation stays entirely on Project-
 * Noctrowl's side). The server (inventory.consignment.mark_reimbursement_paid,
 * via webapp/api.py) is always authoritative for the unpaid -> paid
 * transition — a rejected double-mark-paid always comes back as a real API
 * error from the real engine, never a client-side illusion.
 */

async function loadConsignorFilterOptions() {
  const select = document.getElementById("cr-consignor-filter");
  const consignors = (await apiGet("/api/consignors")) || [];
  select.innerHTML = `<option value="">All</option>` +
    consignors.map(c => `<option value="${c.id}">${escapeAttr(c.name)}</option>`).join("");
}

function statusBadge(status) {
  return status === "paid"
    ? '<span class="badge badge-green">Paid</span>'
    : '<span class="badge badge-amber">Unpaid</span>';
}

async function loadReimbursements() {
  const consignorId = document.getElementById("cr-consignor-filter").value;
  const status = document.getElementById("cr-status-filter").value;
  const params = new URLSearchParams();
  if (consignorId) params.set("consignor_id", consignorId);
  if (status) params.set("status", status);

  const rows = (await apiGet("/api/consignment/reimbursements?" + params.toString())) || [];
  const container = document.getElementById("cr-list");
  if (rows.length === 0) {
    container.innerHTML = `<div class="subtitle">No reimbursement records in this view.</div>`;
    return;
  }
  container.innerHTML = `
    <table>
      <thead><tr>
        <th>ID</th><th>Consignor</th><th>Item</th><th>Serial/Asset ID</th>
        <th>Reference</th><th>Status</th><th>Paid Date</th><th></th>
      </tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td>#${r.id}</td>
          <td>${escapeAttr(r.consignor_name)}</td>
          <td><a class="link-btn" href="/items/${encodeURIComponent(r.sku)}">${escapeAttr(r.sku)}</a><br><span class="num-inline">${escapeAttr(r.item_name)}</span></td>
          <td>${escapeAttr(r.serial_id)}</td>
          <td>${r.reference ? escapeAttr(r.reference) : `<span style="color:var(--text-dim);font-size:12px;">—</span>`}</td>
          <td>${statusBadge(r.status)}</td>
          <td>${r.paid_date || `<span style="color:var(--text-dim);font-size:12px;">—</span>`}</td>
          <td>${r.status === "unpaid" ? `<button class="btn btn-sm" data-markpaid="${r.id}">Mark as Paid</button>` : ""}</td>
        </tr>`).join("")}</tbody>
    </table>`;

  container.querySelectorAll("[data-markpaid]").forEach(btn => {
    btn.onclick = () => markReimbursementPaid(Number(btn.dataset.markpaid));
  });
}

async function markReimbursementPaid(id) {
  const paymentReference = window.prompt("Optional payment reference (e.g. bank transfer note):", "");
  if (paymentReference === null) return; // user cancelled
  try {
    await apiPost(`/api/consignment/reimbursements/${id}/mark-paid`, {
      payment_reference: paymentReference.trim() || null,
    });
    showToast(`Reimbursement #${id} marked as paid.`);
    await loadReimbursements();
  } catch (err) {
    showToast("Could not mark as paid: " + err.message);
  }
}

document.getElementById("cr-refresh").onclick = loadReimbursements;
document.getElementById("cr-consignor-filter").onchange = loadReimbursements;
document.getElementById("cr-status-filter").onchange = loadReimbursements;
loadConsignorFilterOptions().then(loadReimbursements);
