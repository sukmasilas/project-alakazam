/* Pure client-side ports of inventory/allocation.py, inventory/sku.py, and
 * inventory/serials.py — used ONLY for live UI preview/responsiveness
 * (design doc: "live client-side preview for responsiveness"). The actual
 * Save action never trusts anything computed here: it POSTs the raw draft
 * to /api/purchases, and inventory.purchases.save_purchase() on the server
 * re-derives and re-validates everything (allocation, SKU/serial
 * generation, uniqueness) from scratch. If this file and the server ever
 * disagree, the server's answer is what actually gets saved — this file
 * only affects what the user sees before clicking Save.
 *
 * Faithfully mirrors the same algorithms already proven in
 * docs/design/mockup.html (splitInteger / computeAllocation / generateSku /
 * generateSerialsForLine) and inventory/*.py — not a new invention.
 */

function splitInteger(total, weights) {
  const n = weights.length;
  if (n === 0) return [];
  total = Math.round(Number(total) || 0);
  const totalWeight = weights.reduce((a, b) => a + Math.max(0, Number(b) || 0), 0);
  let raw;
  if (totalWeight > 0) {
    raw = weights.map(w => total * Math.max(0, Number(w) || 0) / totalWeight);
  } else {
    raw = weights.map(() => total / n);
  }
  const rounded = raw.map(v => Math.round(v));
  let diff = total - rounded.reduce((a, b) => a + b, 0);
  if (diff !== 0) {
    let idx = 0, maxVal = -Infinity;
    raw.forEach((v, i) => { if (v > maxVal) { maxVal = v; idx = i; } });
    rounded[idx] += diff;
  }
  return rounded;
}

function convertPriceValue(priceValue, quantity, fromMode, toMode) {
  const qty = Number(quantity) || 0;
  const val = Number(priceValue) || 0;
  if (fromMode === toMode) return Math.round(val);
  if (fromMode === "per_unit" && toMode === "total") return Math.round(val * qty);
  if (qty <= 0) return Math.round(val);
  return Math.round(val / qty);
}

/* p: { totalAmountPaid, shippingMode, poolAmount, poolMethod, lumpSum:{amount,method}|null,
 *      lines:[{qty, pricingMode, priceEntryMode, priceValue, lumpsumWeightKg, lumpsumValue,
 *              shipsSeparately, manualShipping, shippingWeightKg}] }
 * Returns { lines:[{itemCost, shippingShare, lineTotal}], runningTotal, diff, balanced }
 * — same shape/semantics as mockup.html's computeAllocation / inventory/allocation.py's
 * compute_purchase_allocation.
 */
function computeAllocation(p) {
  const lines = p.lines;

  const itemCostByLine = new Map();
  const lumpsumLines = [];
  lines.forEach(l => {
    if (l.pricingMode === "lumpsum") { lumpsumLines.push(l); return; }
    const val = l.priceValue || 0;
    const cost = l.priceEntryMode === "per_unit" ? Math.round(val * (l.qty || 0)) : Math.round(val);
    itemCostByLine.set(l, cost);
  });
  if (lumpsumLines.length > 0) {
    const method = p.lumpSum ? p.lumpSum.method : "equal";
    const amount = p.lumpSum ? (p.lumpSum.amount || 0) : 0;
    const weight = l => method === "equal" ? (l.qty || 0) : method === "by_weight" ? (l.lumpsumWeightKg || 0) : (l.lumpsumValue || 0);
    const shares = splitInteger(amount, lumpsumLines.map(weight));
    lumpsumLines.forEach((l, i) => itemCostByLine.set(l, shares[i]));
  }

  const shippingShareByLine = new Map();
  if (p.shippingMode === "manual") {
    lines.forEach(l => shippingShareByLine.set(l, l.manualShipping || 0));
  } else if (p.shippingMode === "pooled") {
    const separate = lines.filter(l => l.shipsSeparately);
    const pooled = lines.filter(l => !l.shipsSeparately);
    separate.forEach(l => shippingShareByLine.set(l, l.manualShipping || 0));
    const method = p.poolMethod || "equal";
    const weight = l => method === "equal" ? (l.qty || 0) : method === "by_weight" ? (l.shippingWeightKg || 0) : (itemCostByLine.get(l) || 0);
    const shares = splitInteger(p.poolAmount || 0, pooled.map(weight));
    pooled.forEach((l, i) => shippingShareByLine.set(l, shares[i]));
  } else {
    lines.forEach(l => shippingShareByLine.set(l, 0));
  }

  const resultLines = lines.map(l => {
    const itemCost = itemCostByLine.get(l) || 0;
    const shippingShare = shippingShareByLine.get(l) || 0;
    return { itemCost, shippingShare, lineTotal: itemCost + shippingShare };
  });
  const runningTotal = resultLines.reduce((s, l) => s + l.lineTotal, 0);
  const diff = Math.round(Number(p.totalAmountPaid) || 0) - runningTotal;
  return { lines: resultLines, runningTotal, diff, balanced: diff === 0 };
}

function splitSerialUnitCosts(lineTotal, quantity) {
  return splitInteger(lineTotal, Array(quantity).fill(1));
}

/* SKU generation preview — matches inventory/sku.py exactly. `existingSkus`
 * should be every DB-existing SKU for this prefix+slug family (fetched via
 * GET /api/skus/candidates) PLUS any sibling new-item SKU already assigned
 * to another line in the current in-flight purchase draft (the caller's
 * responsibility to include, same division of labor as sku.py's own
 * docstring describes for its Python callers).
 */
function slugifyName(name) {
  const words = (name || "").trim().split(/\s+/).filter(Boolean).slice(0, 2);
  const slug = words.join(" ").toUpperCase().replace(/[^A-Z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return slug || "ITEM";
}
function generateSkuCandidate(categoryPrefix, name, existingSkus) {
  const slug = slugifyName(name);
  const base = categoryPrefix + "-" + slug;
  const escaped = base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp("^" + escaped + "-(\\d{4})$", "i");
  let maxSeq = 0;
  (existingSkus || []).forEach(sku => { const m = sku.match(re); if (m) maxSeq = Math.max(maxSeq, parseInt(m[1], 10)); });
  return base + "-" + String(maxSeq + 1).padStart(4, "0");
}

/* Serial ID generation preview — matches inventory/serials.py exactly.
 * `existingCount` is the real DB count (GET /api/items/{sku}/serial-count);
 * `siblingReserved` is any serial already assigned to a sibling line of the
 * same SKU in the current in-flight draft.
 */
function generateSerialId(sku, sequence) {
  return sku + "-" + String(sequence).padStart(3, "0");
}
function generateSerialsForLine(sku, quantity, existingCount, siblingReserved) {
  const escapedSku = sku.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp("^" + escapedSku + "-(\\d{3})$", "i");
  let siblingMax = 0;
  (siblingReserved || []).forEach(serial => { const m = (serial || "").match(re); if (m) siblingMax = Math.max(siblingMax, parseInt(m[1], 10)); });
  const startAt = Math.max(existingCount, siblingMax);
  const out = [];
  for (let i = 0; i < quantity; i++) out.push(generateSerialId(sku, startAt + i + 1));
  return out;
}
