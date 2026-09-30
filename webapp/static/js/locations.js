/* Locations screen — shelving/location feature, confirmed 2026-10-01.
 * Simple CRUD: create + list. See inventory/locations.py::create_location /
 * list_locations, reached via /api/locations. The server is always
 * authoritative — a blank or duplicate name is rejected server-side
 * regardless of any client-side check here.
 */

async function loadLocations() {
  const container = document.getElementById("l-list");
  const rows = (await apiGet("/api/locations")) || [];
  if (rows.length === 0) {
    container.innerHTML = `<div class="subtitle">No locations yet — add one above.</div>`;
    return;
  }
  container.innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>Name</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr><td>#${r.id}</td><td><b>${escapeAttr(r.name)}</b></td></tr>
      `).join("")}</tbody>
    </table>`;
}

async function submitNewLocation() {
  const errorEl = document.getElementById("l-create-error");
  errorEl.textContent = "";
  const name = document.getElementById("l-name").value.trim();
  if (!name) {
    errorEl.textContent = "Enter a location name.";
    return;
  }
  try {
    await apiPost("/api/locations", { name });
    showToast(`Added location ${name}.`);
    document.getElementById("l-name").value = "";
    await loadLocations();
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

document.getElementById("l-submit").onclick = submitNewLocation;
loadLocations();
