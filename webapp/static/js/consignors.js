/* Consignors screen — Milestone 8. Simple CRUD: create + list. See
 * inventory/consignment.py::create_consignor / list_consignors, reached via
 * /api/consignors. The server is always authoritative — a blank name is
 * rejected server-side regardless of any client-side check here.
 */

async function loadConsignors() {
  const container = document.getElementById("c-list");
  const rows = (await apiGet("/api/consignors")) || [];
  if (rows.length === 0) {
    container.innerHTML = `<div class="subtitle">No consignors yet — add one above.</div>`;
    return;
  }
  container.innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>Name</th><th>Contact Info</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr><td>#${r.id}</td><td><b>${escapeAttr(r.name)}</b></td>
        <td>${r.contact_info ? escapeAttr(r.contact_info) : `<span style="color:var(--text-dim);font-size:12px;">—</span>`}</td></tr>
      `).join("")}</tbody>
    </table>`;
}

async function submitNewConsignor() {
  const errorEl = document.getElementById("c-create-error");
  errorEl.textContent = "";
  const name = document.getElementById("c-name").value.trim();
  const contact = document.getElementById("c-contact").value.trim();
  if (!name) {
    errorEl.textContent = "Enter a consignor name.";
    return;
  }
  try {
    await apiPost("/api/consignors", { name, contact_info: contact || null });
    showToast(`Added consignor ${name}.`);
    document.getElementById("c-name").value = "";
    document.getElementById("c-contact").value = "";
    await loadConsignors();
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

document.getElementById("c-submit").onclick = submitNewConsignor;
loadConsignors();
