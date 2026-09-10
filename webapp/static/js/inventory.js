/* Inventory screen — real data via /api/categories, /api/categories/stats,
 * /api/items. "+ Add New Item" posts to /api/items, which calls the real
 * inventory.items.create_item() (SKU generation + uniqueness both real).
 */

let categoriesCache = [];
let categoryStatsCache = [];
let inventoryCatFilter = "All";
let addItemDraft = null;

async function loadInventoryPage() {
  const [cats, stats] = await Promise.all([apiGet("/api/categories"), apiGet("/api/categories/stats")]);
  categoriesCache = cats || [];
  categoryStatsCache = stats || [];
  renderCatNav();
  document.getElementById("inv-search").oninput = debounce(renderInventoryTable, 200);
  document.getElementById("inv-identity-filter").onchange = renderInventoryTable;
  document.getElementById("add-item-toggle").onclick = toggleAddItemPanel;
  await renderInventoryTable();
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function statsFor(code) {
  const row = categoryStatsCache.find(s => s.category.code === code);
  return row ? { count: row.item_count, qty: row.quantity, cost: row.cost_basis } : { count: 0, qty: 0, cost: 0 };
}

function renderCatNav() {
  const el = document.getElementById("inventory-catnav");
  const totalCount = categoryStatsCache.reduce((s, r) => s + r.item_count, 0);
  const rows = [{ code: "All", name: "All Categories", count: totalCount }].concat(
    categoriesCache.map(c => ({ code: c.code, name: c.name, count: statsFor(c.code).count }))
  );
  el.innerHTML = rows.map(r => `
    <div class="catnav-item ${inventoryCatFilter === r.code ? "active" : ""}" data-code="${escapeAttr(r.code)}">
      <span>${escapeAttr(r.name)}</span><span class="catnav-count">${r.count}</span>
    </div>`).join("");
  el.querySelectorAll(".catnav-item").forEach(node => {
    node.onclick = () => { inventoryCatFilter = node.dataset.code; renderCatNav(); renderInventoryTable(); };
  });
}

function renderRollups() {
  const rollupCats = inventoryCatFilter === "All" ? categoriesCache : categoriesCache.filter(c => c.code === inventoryCatFilter);
  document.getElementById("inventory-rollups").innerHTML = rollupCats.map(c => {
    const s = statsFor(c.code);
    return `<div class="rollup-card">
      <div class="label">${escapeAttr(c.name)}</div>
      <div class="value">${fmtIDR(s.cost)}</div>
      <div class="sub">${s.count} SKUs &middot; ${Number(s.qty).toLocaleString("id-ID")} units</div>
    </div>`;
  }).join("");
}

async function renderInventoryTable() {
  renderRollups();
  const search = document.getElementById("inv-search").value.trim();
  const identity = document.getElementById("inv-identity-filter").value;
  const params = new URLSearchParams();
  if (inventoryCatFilter !== "All") params.set("category", inventoryCatFilter);
  if (identity !== "all") params.set("identity", identity);
  if (search) params.set("q", search);
  const rows = await apiGet("/api/items?" + params.toString());
  const tbody = document.getElementById("inventory-tbody");
  if (!rows || rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text-dim);padding:24px;">No items match this filter.</td></tr>`;
    return;
  }
  const sorted = rows.slice().sort((a, b) => Number(b.cost_basis) - Number(a.cost_basis));
  tbody.innerHTML = sorted.map(it => `
    <tr class="clickable" data-sku="${escapeAttr(it.sku)}">
      <td><b>${escapeAttr(it.sku)}</b></td><td>${escapeAttr(it.name)}</td>
      <td><span class="cat-tag">${escapeAttr(it.category_name)}</span></td>
      <td>${identityBadge(it.identity_mode)}</td>
      <td class="num">${fmtQty(it.quantity)}</td>
      <td class="num">${fmtIDR(it.cost_basis)}</td>
    </tr>`).join("");
  tbody.querySelectorAll("tr[data-sku]").forEach(row => {
    row.onclick = () => { window.location.href = "/items/" + encodeURIComponent(row.dataset.sku); };
  });
}

/* ---- Add New Item panel ---- */

function toggleAddItemPanel() {
  const panel = document.getElementById("add-item-panel");
  if (panel.style.display === "none") {
    addItemDraft = { name: "", category: categoriesCache[0] ? categoriesCache[0].code : "", identity: "fungible", code: "", skuTouched: false };
    panel.style.display = "block";
    renderAddItemPanel();
  } else {
    panel.style.display = "none";
  }
}

async function refreshGeneratedSku() {
  if (addItemDraft.skuTouched) return;
  const cat = categoriesCache.find(c => c.code === addItemDraft.category);
  if (!cat || !addItemDraft.name) { addItemDraft.code = ""; return; }
  const data = await apiGet(`/api/skus/candidates?category_code=${encodeURIComponent(cat.code)}&name=${encodeURIComponent(addItemDraft.name)}`);
  addItemDraft.code = data.suggestion;
}

async function renderAddItemPanel() {
  const d = addItemDraft;
  document.getElementById("add-item-panel").innerHTML = `
    <div class="new-sku-box" style="margin-bottom:14px;">
      <div class="nsfield"><label>Name</label><input type="text" id="ai-name" value="${escapeAttr(d.name)}" placeholder="Item name"></div>
      <div class="nsfield"><label>Category</label><select id="ai-cat">${categoriesCache.map(c => `<option value="${c.code}" ${d.category === c.code ? "selected" : ""}>${escapeAttr(c.name)}</option>`).join("")}</select></div>
      <div class="nsfield"><label>Identity Mode</label><select id="ai-identity">
        <option value="fungible" ${d.identity === "fungible" ? "selected" : ""}>Fungible</option>
        <option value="serialized" ${d.identity === "serialized" ? "selected" : ""}>Serialized</option>
      </select></div>
      <div class="nsfield">
        <label>SKU Code <button type="button" class="link-btn" style="font-size:11px;" id="ai-regen">🔄 Regenerate</button></label>
        <input type="text" id="ai-code" value="${escapeAttr(d.code)}">
        <div class="sku-check" id="ai-code-check"></div>
      </div>
    </div>
    <button class="btn btn-primary btn-sm" id="ai-save-btn" disabled>Add Item</button>
    <button class="btn btn-sm" id="ai-cancel-btn">Cancel</button>
  `;
  await refreshAddItemCheck();
  wireAddItemEvents();
}

async function refreshAddItemCheck() {
  const d = addItemDraft;
  const checkEl = document.getElementById("ai-code-check");
  const saveBtn = document.getElementById("ai-save-btn");
  if (!d.code) { checkEl.innerHTML = ""; saveBtn.disabled = true; return; }
  const res = await apiGet(`/api/skus/check?sku=${encodeURIComponent(d.code)}`);
  const taken = res.exists;
  checkEl.innerHTML = taken
    ? '<span style="color:var(--red-text);">✗ Already exists</span>'
    : '<span style="color:var(--green-text);">✓ Available</span>';
  saveBtn.disabled = !(d.name && d.code && !taken);
}

function wireAddItemEvents() {
  const d = addItemDraft;
  document.getElementById("ai-name").oninput = async (e) => {
    d.name = e.target.value;
    if (!d.skuTouched) { await refreshGeneratedSku(); document.getElementById("ai-code").value = d.code; }
    await refreshAddItemCheck();
  };
  document.getElementById("ai-cat").onchange = async (e) => {
    d.category = e.target.value;
    if (!d.skuTouched) { await refreshGeneratedSku(); document.getElementById("ai-code").value = d.code; }
    await refreshAddItemCheck();
  };
  document.getElementById("ai-identity").onchange = (e) => { d.identity = e.target.value; };
  document.getElementById("ai-code").oninput = async (e) => {
    d.skuTouched = true;
    d.code = e.target.value.trim().toUpperCase();
    await refreshAddItemCheck();
  };
  document.getElementById("ai-regen").onclick = async () => {
    d.skuTouched = false;
    await refreshGeneratedSku();
    document.getElementById("ai-code").value = d.code;
    await refreshAddItemCheck();
  };
  document.getElementById("ai-save-btn").onclick = saveNewItem;
  document.getElementById("ai-cancel-btn").onclick = () => { document.getElementById("add-item-panel").style.display = "none"; };
}

async function saveNewItem() {
  const d = addItemDraft;
  try {
    const item = await apiPost("/api/items", { name: d.name, category_code: d.category, identity_mode: d.identity, sku: d.code });
    if (!item) return;
    showToast(`Item ${item.sku} added.`);
    document.getElementById("add-item-panel").style.display = "none";
    await loadInventoryPage();
  } catch (err) {
    showToast("Could not add item: " + err.message);
  }
}

loadInventoryPage();
