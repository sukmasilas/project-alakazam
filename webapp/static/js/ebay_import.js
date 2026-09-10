/* eBay Sales Import — Milestone 6. A review queue, not an auto-matcher:
 * every Order row needs an explicit human "Confirm match" click (picking a
 * real Alakazam item, plus specific on-hand units for a serialized item)
 * before "Process confirmed rows" ever calls the real depletion engine.
 * Nothing here ever calls a deplete endpoint directly — only
 * /api/ebay-import/process does, and only for rows already staged
 * 'matched' by a human. Mirrors purchase_entry.js's typeahead-combobox
 * pattern for item search.
 */

let currentBatchId = null;
let rowsCache = [];
// Per-row IN-PROGRESS match draft (not yet confirmed) — keyed by row id.
// { sku, name, identityMode, quantity, selectedSerials: Set<string> }
const matchDrafts = {};

function todayLabel(dateStr) {
  return dateStr || "—";
}

function rowTypeBadge(row) {
  return row.row_type === "Refund"
    ? '<span class="badge badge-amber">Refund</span>'
    : '<span class="badge badge-fungible">Order</span>';
}

function statusBadge(row) {
  const map = {
    pending: '<span class="badge" style="background:#eee;color:#555;">Needs review</span>',
    matched: '<span class="badge badge-green">Matched — awaiting Process</span>',
    skipped: '<span class="badge" style="background:#eee;color:#888;">Skipped</span>',
    posted: '<span class="badge badge-serialized">Posted</span>',
  };
  return map[row.review_status] || escapeAttr(row.review_status);
}

async function initEbayImportPage() {
  document.getElementById("ei-upload-btn").onclick = uploadFile;
  document.getElementById("ei-refresh-btn").onclick = () => { loadBatches(); loadRows(); };
  document.getElementById("ei-process-btn").onclick = processConfirmedRows;
  document.getElementById("ei-batch-select").onchange = (e) => {
    currentBatchId = e.target.value === "" ? null : Number(e.target.value);
    loadRows();
  };
  await loadBatches();
  await loadRows();
}

/* ---- Upload ---- */

async function uploadFile() {
  const input = document.getElementById("ei-file-input");
  const resultBox = document.getElementById("ei-upload-result");
  const file = input.files && input.files[0];
  if (!file) { showToast("Choose a CSV file first."); return; }

  const formData = new FormData();
  formData.append("file", file);
  resultBox.innerHTML = `<div class="subtitle">Uploading and parsing…</div>`;
  try {
    const res = await fetch("/api/ebay-import/upload", { method: "POST", credentials: "same-origin", body: formData });
    if (res.status === 401) { window.location.href = "/login"; return; }
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      const msg = (body && body.detail && body.detail.message) || `Upload failed (${res.status})`;
      resultBox.innerHTML = `<div class="badge badge-red">✗ ${escapeAttr(msg)}</div>`;
      return;
    }
    const summary = await res.json();
    resultBox.innerHTML = `
      <div class="badge badge-green">✓ Parsed ${escapeAttr(summary.source_filename)}${summary.seller ? " — seller " + escapeAttr(summary.seller) : ""}</div>
      <div class="subtitle" style="margin-top:6px;">
        ${summary.order_rows_stored} Order line item(s) ready for review &middot;
        ${summary.refund_rows_stored} Refund row(s) shown for visibility &middot;
        ${summary.order_rows_duplicate} already-imported duplicate(s) skipped &middot;
        ${summary.order_summary_rows_skipped} multi-item order summary row(s) skipped (no item detail) &middot;
        ${summary.non_actionable_rows_skipped} Hold/Other fee/Payout row(s) not applicable to inventory
        ${summary.order_rows_missing_transaction_id ? " &middot; <b>" + summary.order_rows_missing_transaction_id + " Order row(s) had no Transaction ID and were NOT imported (can't guarantee no double-post)</b>" : ""}
      </div>`;
    input.value = "";
    await loadBatches();
    currentBatchId = summary.batch_id;
    document.getElementById("ei-batch-select").value = String(summary.batch_id);
    await loadRows();
  } catch (err) {
    resultBox.innerHTML = `<div class="badge badge-red">✗ ${escapeAttr(err.message || "Upload failed")}</div>`;
  }
}

/* ---- Batches ---- */

async function loadBatches() {
  const batches = (await apiGet("/api/ebay-import/batches")) || [];
  const select = document.getElementById("ei-batch-select");
  const prior = currentBatchId;
  select.innerHTML = `<option value="">All batches</option>` + batches.map(b =>
    `<option value="${b.id}">${escapeAttr(b.source_filename)} — ${escapeAttr((b.uploaded_at || "").slice(0, 10))} (${b.order_rows_stored} order rows)</option>`
  ).join("");
  select.value = prior != null ? String(prior) : "";
}

/* ---- Rows ---- */

async function loadRows() {
  const params = new URLSearchParams();
  if (currentBatchId != null) params.set("batch_id", currentBatchId);
  rowsCache = (await apiGet("/api/ebay-import/rows?" + params.toString())) || [];
  renderRows();
}

function renderRows() {
  const container = document.getElementById("ei-rows");
  if (rowsCache.length === 0) {
    container.innerHTML = `<div class="subtitle">No rows yet — upload a Transaction report CSV above.</div>`;
    return;
  }
  container.innerHTML = `
    <table>
      <thead><tr>
        <th>Date</th><th>Type</th><th>Order # / Txn ID</th><th>Item</th>
        <th class="num">Qty</th><th>Match</th><th>Status</th><th>Actions</th>
      </tr></thead>
      <tbody>${rowsCache.map(rowHTML).join("")}</tbody>
    </table>`;
  rowsCache.forEach(wireRowEvents);
}

function rowHTML(row) {
  const idCell = `${escapeAttr(row.order_number || "—")}<br><span class="num-inline">txn ${escapeAttr(row.ebay_transaction_id || "—")}</span>`;
  const itemCell = `${escapeAttr(row.item_title)}${row.custom_label ? `<br><span class="num-inline">Custom label: ${escapeAttr(row.custom_label)}</span>` : ""}`;

  let matchCell;
  let actionsCell;
  if (row.row_type === "Refund") {
    matchCell = `<span style="color:var(--text-dim); font-size:12px;">Visibility only — no automatic reversal. Handle manually if needed.</span>`;
    actionsCell = "";
  } else if (row.review_status === "posted") {
    matchCell = `<b>${escapeAttr(row.matched_item_sku || "")}</b><br><span class="num-inline">${escapeAttr(row.matched_item_name || "")}</span>`;
    actionsCell = `<span style="color:var(--text-dim); font-size:12px;">Posted — corrections to a posted row are out of scope for this prototype.</span>`;
  } else {
    matchCell = matchAreaHTML(row);
    actionsCell = `
      <button type="button" class="btn btn-sm btn-primary" data-confirm="${row.id}">Confirm match</button>
      <button type="button" class="btn btn-sm btn-danger-outline" data-skip="${row.id}">Skip</button>`;
  }

  const errorNote = row.last_process_error
    ? `<div class="badge badge-red" style="margin-top:6px;">Last attempt failed: ${escapeAttr(row.last_process_error)}</div>`
    : "";

  return `<tr id="ei-row-${row.id}">
    <td>${todayLabel(row.transaction_date)}</td>
    <td>${rowTypeBadge(row)}</td>
    <td>${idCell}</td>
    <td>${itemCell}</td>
    <td class="num">${row.quantity != null ? row.quantity : "—"}</td>
    <td id="ei-match-${row.id}">${matchCell}${errorNote}</td>
    <td>${statusBadge(row)}</td>
    <td>${actionsCell}</td>
  </tr>`;
}

function draftFor(row) {
  if (!matchDrafts[row.id]) {
    matchDrafts[row.id] = {
      sku: row.matched_item_sku || "",
      name: row.matched_item_name || "",
      identityMode: row.matched_identity_mode || null,
      quantity: row.quantity || 1,
      selectedSerials: new Set(row.matched_serial_ids || []),
      serialOptions: [],
    };
  }
  return matchDrafts[row.id];
}

function matchAreaHTML(row) {
  const draft = draftFor(row);
  let html = `<div class="combobox-field" style="min-width:260px;">
    <div class="combobox">
      <input type="text" class="combobox-input" id="ei-search-${row.id}" autocomplete="off"
             value="${escapeAttr(draft.sku ? draft.sku + " — " + draft.name : "")}"
             placeholder="Search Alakazam item by SKU or name…">
      <div class="combobox-dropdown" id="ei-dropdown-${row.id}" style="display:none;"></div>
    </div>
  </div>`;

  if (row.suggestions && row.suggestions.length && !draft.sku) {
    html += `<div style="margin-top:6px;">Suggested: ${row.suggestions.map(s =>
      `<button type="button" class="btn btn-sm" data-suggest="${row.id}" data-sku="${escapeAttr(s.sku)}" data-name="${escapeAttr(s.name)}" data-identity="${escapeAttr(s.identity_mode)}" style="margin:2px;">${escapeAttr(s.sku)} (${Math.round(s.score * 100)}%)</button>`
    ).join("")}</div>`;
  }

  if (draft.identityMode === "serialized") {
    html += `<div id="ei-serial-area-${row.id}" style="margin-top:8px;">${serialAreaHTML(row, draft)}</div>`;
  }
  return html;
}

function serialAreaHTML(row, draft) {
  if (!draft.serialOptionsLoaded) {
    return `<span style="color:var(--text-dim); font-size:12px;">Loading on-hand units…</span>`;
  }
  if (draft.serialOptions.length === 0) {
    return `<span style="color:var(--red-text); font-size:12px;">No on-hand units for this item — cannot match this row.</span>`;
  }
  const need = draft.quantity;
  return `<div style="font-size:12px; color:var(--text-dim); margin-bottom:4px;">Select exactly ${need} on-hand unit(s) sold:</div>` +
    draft.serialOptions.map(opt => `
      <label style="display:block; font-size:12.5px;">
        <input type="checkbox" data-serial-pick="${row.id}" value="${escapeAttr(opt.serial_id)}" ${draft.selectedSerials.has(opt.serial_id) ? "checked" : ""}>
        ${escapeAttr(opt.serial_id)} <span class="num-inline">(${fmtIDR(opt.cost)})</span>
      </label>`).join("");
}

async function refreshSerialArea(row, draft) {
  if (draft.identityMode !== "serialized") return;
  draft.serialOptionsLoaded = false;
  const area = document.getElementById(`ei-serial-area-${row.id}`);
  if (area) area.innerHTML = serialAreaHTML(row, draft);
  draft.serialOptions = (await apiGet("/api/ebay-import/serial-options?sku=" + encodeURIComponent(draft.sku))) || [];
  draft.serialOptionsLoaded = true;
  if (area) {
    area.innerHTML = serialAreaHTML(row, draft);
    wireSerialCheckboxes(row, draft);
  }
}

function wireSerialCheckboxes(row, draft) {
  document.querySelectorAll(`[data-serial-pick="${row.id}"]`).forEach(cb => {
    cb.onchange = () => {
      if (cb.checked) draft.selectedSerials.add(cb.value);
      else draft.selectedSerials.delete(cb.value);
    };
  });
}

/* ---- Combobox wiring (reuses the real /api/items/search typeahead) ---- */

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function wireRowEvents(row) {
  if (row.row_type === "Refund" || row.review_status === "posted") return;

  const input = document.getElementById(`ei-search-${row.id}`);
  const dropdown = document.getElementById(`ei-dropdown-${row.id}`);
  if (input) {
    input.oninput = debounce(() => renderComboboxDropdown(row, input.value), 150);
    input.onfocus = () => renderComboboxDropdown(row, input.value);
    document.addEventListener("click", function outsideClick(e) {
      if (!document.body.contains(input)) { document.removeEventListener("click", outsideClick); return; }
      if (!input.contains(e.target) && dropdown && !dropdown.contains(e.target)) dropdown.style.display = "none";
    });
  }

  document.querySelectorAll(`[data-suggest="${row.id}"]`).forEach(btn => {
    btn.onclick = () => pickItem(row, btn.dataset.sku, btn.dataset.name, btn.dataset.identity);
  });

  const draft = draftFor(row);
  if (draft.identityMode === "serialized" && !draft.serialOptionsLoaded) {
    refreshSerialArea(row, draft);
  } else if (draft.identityMode === "serialized") {
    wireSerialCheckboxes(row, draft);
  }

  const confirmBtn = document.querySelector(`[data-confirm="${row.id}"]`);
  if (confirmBtn) confirmBtn.onclick = () => confirmMatch(row);
  const skipBtn = document.querySelector(`[data-skip="${row.id}"]`);
  if (skipBtn) skipBtn.onclick = () => skipRow(row);
}

async function renderComboboxDropdown(row, query) {
  const dropdown = document.getElementById(`ei-dropdown-${row.id}`);
  if (!dropdown) return;
  const q = (query || "").trim();
  const matches = q ? (await apiGet("/api/items/search?q=" + encodeURIComponent(q) + "&limit=8")) || [] : [];
  dropdown.innerHTML = matches.length === 0
    ? `<div class="combo-empty">${q ? "No matching items." : "Type to search…"}</div>`
    : matches.map(it => `<div class="combo-option" data-pick="${row.id}" data-sku="${escapeAttr(it.sku)}" data-name="${escapeAttr(it.name)}" data-identity="${escapeAttr(it.identity_mode)}"><b>${escapeAttr(it.sku)}</b><span>${escapeAttr(it.name)}</span></div>`).join("");
  dropdown.style.display = "block";
  dropdown.querySelectorAll("[data-pick]").forEach(opt => {
    opt.onclick = () => pickItem(row, opt.dataset.sku, opt.dataset.name, opt.dataset.identity);
  });
}

function pickItem(row, sku, name, identityMode) {
  const draft = draftFor(row);
  draft.sku = sku;
  draft.name = name;
  draft.identityMode = identityMode;
  draft.selectedSerials = new Set();
  draft.serialOptionsLoaded = false;
  const matchCell = document.getElementById(`ei-match-${row.id}`);
  if (matchCell) matchCell.innerHTML = matchAreaHTML(row) + (row.last_process_error ? `<div class="badge badge-red" style="margin-top:6px;">Last attempt failed: ${escapeAttr(row.last_process_error)}</div>` : "");
  wireRowEvents(row);
}

/* ---- Confirm / skip ---- */

async function confirmMatch(row) {
  const draft = draftFor(row);
  if (!draft.sku) { showToast("Pick an Alakazam item first."); return; }
  const body = { sku: draft.sku };
  if (draft.identityMode === "serialized") {
    body.serial_ids = Array.from(draft.selectedSerials);
  }
  try {
    await apiPost(`/api/ebay-import/rows/${row.id}/match`, body);
    showToast(`Row matched to ${draft.sku} — click "Process confirmed rows" to post it.`);
    delete matchDrafts[row.id];
    await loadRows();
  } catch (err) {
    showToast(err.message || "Could not confirm this match.");
  }
}

async function skipRow(row) {
  try {
    await apiPost(`/api/ebay-import/rows/${row.id}/skip`, {});
    delete matchDrafts[row.id];
    await loadRows();
  } catch (err) {
    showToast(err.message || "Could not skip this row.");
  }
}

async function processConfirmedRows() {
  const resultBox = document.getElementById("ei-process-result");
  resultBox.innerHTML = `<div class="subtitle">Processing…</div>`;
  const params = new URLSearchParams();
  if (currentBatchId != null) params.set("batch_id", currentBatchId);
  try {
    const results = await apiPost("/api/ebay-import/process?" + params.toString(), {});
    if (!results || results.length === 0) {
      resultBox.innerHTML = `<div class="subtitle">No matched rows were ready to process.</div>`;
    } else {
      const okCount = results.filter(r => r.success).length;
      const failCount = results.length - okCount;
      resultBox.innerHTML = `
        <div class="badge ${failCount ? "badge-amber" : "badge-green"}">
          ${okCount} row(s) posted to inventory${failCount ? `, ${failCount} failed — see per-row errors below` : ""}
        </div>
        <ul style="margin-top:8px; font-size:12.5px;">
          ${results.map(r => `<li>${r.success ? "✓" : "✗"} ${escapeAttr(r.item_title)} (txn ${escapeAttr(r.ebay_transaction_id || "—")})${r.success ? "" : ": " + escapeAttr(r.error)}</li>`).join("")}
        </ul>`;
    }
    await loadRows();
  } catch (err) {
    resultBox.innerHTML = `<div class="badge badge-red">✗ ${escapeAttr(err.message || "Processing failed")}</div>`;
  }
}

initEbayImportPage();
