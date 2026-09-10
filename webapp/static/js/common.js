/* Shared helpers used by every page's JS — formatting, escaping, and a thin
 * fetch() wrapper around the real JSON API (webapp/api.py). All money
 * figures arrive from the API as strings (see api.py's serialization note)
 * — never parsed as JS floats for anything that matters to the reconciliation
 * check; Number() is only ever used here for *display* formatting.
 */

function fmtIDR(n) {
  n = Math.round(Number(n) || 0);
  return "Rp " + n.toLocaleString("id-ID");
}
function fmtIDRSigned(n) {
  n = Math.round(Number(n) || 0);
  const sign = n < 0 ? "-" : "+";
  return sign + "Rp " + Math.abs(n).toLocaleString("id-ID");
}
function fmtQty(qty) {
  return Number(qty).toLocaleString("id-ID") + " units";
}
function escapeAttr(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function identityBadge(identity) {
  return identity === "serialized"
    ? '<span class="badge badge-serialized">Serialized</span>'
    : '<span class="badge badge-fungible">Fungible</span>';
}
function showToast(msg) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => { t.style.display = "none"; }, 3200);
}

async function apiGet(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (res.status === 401) { window.location.href = "/login"; return null; }
  if (!res.ok) throw await _apiError(res);
  return res.json();
}
async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) { window.location.href = "/login"; return null; }
  if (!res.ok) throw await _apiError(res);
  return res.json();
}
async function _apiError(res) {
  let detail;
  try { detail = (await res.json()).detail; } catch (e) { detail = null; }
  const err = new Error((detail && detail.message) ? detail.message : `Request failed (${res.status})`);
  err.status = res.status;
  err.detail = detail;
  return err;
}
