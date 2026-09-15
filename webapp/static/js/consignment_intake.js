/* Consignment Intake screen — Milestone 8.
 *
 * Creates/reuses a consigned item and adds zero-cost serial units to it —
 * NOT a Purchase (no money changes hands, /api/purchases is never called
 * here). Posts to /api/consignment/intake, which calls straight into the
 * real inventory.consignment.intake_consigned_units() engine — the server
 * is always authoritative (a client-supplied category/consignor/SKU is
 * never trusted beyond what that real engine actually accepts).
 */

let categoriesCache = [];
let ciSelectedExistingSku = null; // { sku, name } when "existing item" mode is used

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

async function loadConsignorOptions() {
  const select = document.getElementById("ci-consignor");
  const consignors = (await apiGet("/api/consignors")) || [];
  if (consignors.length === 0) {
    document.getElementById("ci-no-consignors").style.display = "block";
    select.innerHTML = "";
    select.disabled = true;
    return;
  }
  document.getElementById("ci-no-consignors").style.display = "none";
  select.disabled = false;
  select.innerHTML = consignors.map(c => `<option value="${c.id}">${escapeAttr(c.name)}</option>`).join("");
}

async function loadCategoryOptions() {
  categoriesCache = (await apiGet("/api/categories")) || [];
  document.getElementById("ci-new-category").innerHTML =
    categoriesCache.map(c => `<option value="${c.code}">${escapeAttr(c.name)}</option>`).join("");
}

function wireItemModeToggle() {
  document.querySelectorAll('input[name="ci-item-mode"]').forEach(radio => {
    radio.onchange = () => {
      const isNew = document.querySelector('input[name="ci-item-mode"]:checked').value === "new";
      document.getElementById("ci-new-item-form").style.display = isNew ? "block" : "none";
      document.getElementById("ci-existing-item-form").style.display = isNew ? "none" : "block";
    };
  });
}

function wireExistingItemCombobox() {
  const input = document.getElementById("ci-existing-search");
  const dropdown = document.getElementById("ci-existing-dropdown");
  input.oninput = debounce(() => renderExistingDropdown(input.value), 150);
  input.onfocus = () => renderExistingDropdown(input.value);
  document.addEventListener("click", (e) => {
    if (!input.contains(e.target) && !dropdown.contains(e.target)) dropdown.style.display = "none";
  });
}

async function renderExistingDropdown(query) {
  const dropdown = document.getElementById("ci-existing-dropdown");
  const q = (query || "").trim();
  // identity=serialized narrows the picker — the real, authoritative
  // "is this actually tagged to the selected consignor" check happens
  // server-side regardless.
  const matches = q ? (await apiGet("/api/items/search?identity=serialized&q=" + encodeURIComponent(q) + "&limit=8")) || [] : [];
  dropdown.innerHTML = matches.length === 0
    ? `<div class="combo-empty">${q ? "No matching serialized items." : "Type to search…"}</div>`
    : matches.map(it => `<div class="combo-option" data-sku="${escapeAttr(it.sku)}" data-name="${escapeAttr(it.name)}"><b>${escapeAttr(it.sku)}</b><span>${escapeAttr(it.name)}</span></div>`).join("");
  dropdown.style.display = "block";
  dropdown.querySelectorAll("[data-sku]").forEach(opt => {
    opt.onclick = () => {
      ciSelectedExistingSku = { sku: opt.dataset.sku, name: opt.dataset.name };
      input.value = `${opt.dataset.sku} — ${opt.dataset.name}`;
      dropdown.style.display = "none";
    };
  });
}

async function submitIntake() {
  const errorEl = document.getElementById("ci-error");
  const successEl = document.getElementById("ci-success");
  errorEl.textContent = "";
  successEl.textContent = "";

  const consignorSelect = document.getElementById("ci-consignor");
  if (!consignorSelect.value) {
    errorEl.textContent = "Select a consignor first.";
    return;
  }
  const consignorId = parseInt(consignorSelect.value, 10);

  const quantity = parseInt(document.getElementById("ci-qty").value, 10);
  if (!quantity || quantity <= 0) {
    errorEl.textContent = "Enter a quantity of at least 1.";
    return;
  }
  const serialsRaw = document.getElementById("ci-serials").value.trim();
  const serialIds = serialsRaw ? serialsRaw.split(",").map(s => s.trim()).filter(Boolean) : null;

  const mode = document.querySelector('input[name="ci-item-mode"]:checked').value;
  const body = { consignor_id: consignorId, quantity, serial_ids: serialIds };

  if (mode === "new") {
    const name = document.getElementById("ci-new-name").value.trim();
    const category = document.getElementById("ci-new-category").value;
    const skuOverride = document.getElementById("ci-new-sku").value.trim();
    if (!name) { errorEl.textContent = "Enter an item name."; return; }
    if (!category) { errorEl.textContent = "Select a category."; return; }
    body.new_item_name = name;
    body.new_item_category_code = category;
    body.new_item_sku = skuOverride || null;
  } else {
    if (!ciSelectedExistingSku) { errorEl.textContent = "Pick an existing consigned item from the dropdown."; return; }
    body.sku = ciSelectedExistingSku.sku;
  }

  try {
    const result = await apiPost("/api/consignment/intake", body);
    if (!result) return;
    successEl.textContent = `Added ${result.serial_ids.length} unit(s) to ${result.sku} (${result.item_name}): ${result.serial_ids.join(", ")}.`;
    showToast(`Added ${result.serial_ids.length} consigned unit(s) to ${result.sku}.`);
    document.getElementById("ci-qty").value = "1";
    document.getElementById("ci-serials").value = "";
    document.getElementById("ci-new-name").value = "";
    document.getElementById("ci-new-sku").value = "";
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

/* ---- Wiring ---- */

wireItemModeToggle();
wireExistingItemCombobox();
document.getElementById("ci-submit").onclick = submitIntake;
loadConsignorOptions();
loadCategoryOptions();
