# UI/UX Design — Milestone 1 (Project-Alakazam)

This is the design-led first milestone: no backend, no database, no integrations. The goal is to sketch every screen and flow this prototype needs so a future ledger/schema milestone is built against a spec the user has already seen, instead of a UI improvised after the backend exists. Same philosophy Project-Noctrowl used for its own Milestone 1 (`../project-noctrowl-2/docs/design/ui-ux-design.md`, referenced here purely for format/style — none of its business content applies to this project).

See `mockup.html` in this folder for a clickable, static version of what's described here — sample data only (fabricated SKUs/purchases, not real business data), real client-side JS for every genuine calculation, validation, or filter described below (allocation math, SKU/serial generation, uniqueness checks, search), no backend calls of any kind. Where a feature would normally need a real backend (Google Drive upload, OCR, photo storage), it's simulated client-side with realistic UI states rather than wired up for real — see each relevant section below for exactly what's simulated and how.

**Revision history**: this document originally shipped with 6 screens and a per-line "Equal/Weight/Value/Manual Estimate" allocation model applied directly to item cost. Following user review, it was substantially revised (this version) to: merge three screens into one Inventory view; separate item cost (now direct-entry by default) from shipping cost (its own independent control) entirely; and add an invoice uploader with simulated OCR, a SKU generator, a SKU uniqueness checker, typeahead SKU search, a serial-ID generator, and per-unit photo capture. The old allocation model survives only inside the new, explicitly-scoped-down "Lump-Sum Pricing Fallback" — see Screen 3 below.

## Scope for this milestone

Per the brief: build only —
- **Inventory** (merged Item List + On-Hand Stock + Category Browse)
- **Item Detail** (drill-down — fungible purchase-history rows, or serialized per-unit rows)
- **Purchase Entry** (the core novel flow — direct item pricing, independent shipping, opt-in lump-sum fallback, invoice upload + simulated OCR, SKU/serial generation)
- **Purchase History** (read-only ledger, expandable to line-level detail)

Explicitly **not** built this milestone (see brief for the full list): sale entry / stock depletion of any kind, eBay ingestion, pre-order/dropship state, consignment/consignor tracking, an export-to-Noctrowl screen, login/auth. This build is acquisition-only — purchases only ever increase on-hand stock.

## Shared chrome

- **Top bar**: app name ("Alakazam") and a currency reminder badge ("All values shown in IDR (Rp)") — always visible, since this project explicitly cannot assume a single currency is safe to leave implicit (see CLAUDE.md's Currency section).
- **Left nav**: "Inventory", "Purchase Entry", "Purchase History" — three items, down from five, after the Item List / On-Hand Stock / Category Browse merge (see Screen 1). No Settings/Login/Sync entries — none of that exists in this milestone's scope.
- No status badge (Provisional/Final), no account/period selector — those are Project-Noctrowl concepts tied to its reporting-period model; Project-Alakazam has no equivalent concept yet in this milestone.

## Screen 1: Inventory (merged)

**Why merged**: the original Item/SKU List, On-Hand Stock, and Category Browse screens were three views over the same underlying data (every item, its category, its current qty/cost) with heavily overlapping filters and columns — On-Hand Stock was really "Item List sorted differently with rollup cards," and Category Browse was really "Item List pre-filtered by category via a bigger button." Merging removes that redundancy without losing any capability.

**Layout**: a left-hand **category filter rail** (narrow column: "All Categories" + the 5 categories, each showing its item count, click to filter — this replaces Category Browse's tiles as the category-first entry point) next to a **main panel** containing, top to bottom:
- **Category rollup cards** (carried over from the old On-Hand Stock screen) — one card per category (or just the one selected category's card, if a specific category is filtered), each showing total cost-basis value and a `count SKUs · qty units` subline. This is the "where is the money currently tied up" view, kept prominent per the revision brief.
- **Filter row**: a free-text search box (type-to-filter: typing any substring of a SKU or item name narrows the table live, on every keystroke — this is the "typeahead" requirement applied directly to the visible list, which is a more useful shape for a full-page browse than a dropdown-suggestion widget would be), an Identity Mode dropdown (All / Fungible / Serialized), and a **"+ Add New Item"** button.
- **Item table**: SKU, Name, Category, Identity Mode badge, On-Hand Qty, Total Cost Basis — same columns/behavior as the original Item List. Click a row → Item Detail.

**+ Add New Item panel**: expands inline (no navigation) with Name, Category, Identity Mode, and a SKU Code field that **auto-generates live** as the user types the Name / picks the Category (see the SKU Generator section under Screen 3 — the exact same generator and uniqueness-checker logic is reused here, not a separate implementation), with a "🔄 Regenerate" button and a real-time green "✓ Available" / red "✗ Already exists" check. The Add Item button stays disabled until Name + a unique SKU are present. This is a genuine standalone item-creation path — a pre-registered SKU with zero on-hand qty/cost basis, ready to be picked up whenever it's actually purchased through Purchase Entry.

**Row interaction**: click a row → Item Detail (unchanged from the original design).

**Design note carried over from the old Category Browse**: rollup cards do not show an identity-mode split (no "X fungible / Y serialized" breakdown) — that would start to visually suggest identity mode is a category-level property, which it explicitly isn't (see Design principles below).

**Empty state**: "No items match this filter." when a search/category/identity combination matches nothing — distinct from "no items exist at all" (not really reachable in the shipped mockup, since sample data is pre-populated, but the same empty-table treatment would apply).

## Screen 2: Item Detail

Unchanged from the original design. **Header**: SKU, name, category badge, identity-mode badge, on-hand qty, total cost basis. **Body branches by identity mode**: a fungible item shows a purchase-history table (Date, Purchase Reference, Qty, Allocated Unit Cost, Line Total); a serialized item shows one row per physical unit (Serial/Asset ID, Acquisition Date, Allocated Cost, Source Purchase Reference, **Photo** — new column, see Screen 3's photo capture section; shows the attached thumbnail or "No photo" placeholder). Back-link now points to **Inventory** instead of the old Item List. Clicking a Purchase Reference jumps to that entry, expanded, in Purchase History.

## Screen 3: Purchase Entry

The core, novel flow. This revision replaces the old single "Allocation Method per line" model with three genuinely independent concerns: **item cost**, **shipping cost**, and — new this revision — **invoice capture**. All are wired up with real, live-recomputing JS, not static numbers.

### Header fields
Date, Vendor/Description, Total Amount Paid (IDR), Currency (defaults IDR, shown explicitly).

### Invoice / Proof of Transfer uploader (new)
A control box near the header fields with a real `<input type=file>` ("Choose file…") plus two "try a sample" buttons, since a static mockup can't ship real sample files for a reviewer's OS file picker to select. **Conceptually** this file goes to Google Drive — mirroring Project-Noctrowl's Drive-as-plain-file-store pattern, where Drive holds the raw file and the app's own data holds a structured record referencing it — but here the upload is entirely simulated client-side (no network call of any kind).

Once a file is "attached" (real file or sample button), a **simulated OCR pass** runs and produces one of two outcomes, both genuinely reachable and demonstrated in the shipped sample data (not just described in prose — see the Open Questions entry on OCR simulation for exactly which filenames trigger which result):
- **Parsed** (high confidence): Date, Vendor, and Total Amount Paid in the header are auto-filled from the "read" invoice, with a green "Parsed" badge and a recap line ("Read from invoice: …"), and the fields remain fully user-editable afterward.
- **Needs Review**: an amber "Needs Review" badge with an explicit message — *"This invoice couldn't be read automatically — this proof of transfer doesn't contain enough information to auto-fill. Please fill in Date, Vendor, and Total Amount Paid manually above."* No field is silently left blank without that explanation.

An attached invoice can be removed and re-attached. On Save, the invoice record (filename + OCR outcome + parsed fields, if any) is stored on the saved purchase and shown read-only in Purchase History.

### Shipping (new — fully independent of item pricing)
A control box with three mutually-exclusive modes, chosen per purchase:
- **No shipping** — every line's shipping cost is Rp 0.
- **Manual (per line)** — each line gets its own exact shipping-amount field (Rp 0 is valid, for a line that had none).
- **Pooled (split automatically)** — one shared shipping total + a split method (**Equally** by quantity, **By Weight** using a per-line weight-in-kg field, or **By Value**, which reuses each line's *already-computed item cost* as the proportional weight rather than asking for a second, redundant value figure). Any individual line can still tick **"This item shipped separately"** to opt out of the pool and enter its own manual amount instead — this is the direct successor to the old design's "Dedicated Shipping" concept, now framed as a sub-feature of Pooled mode rather than an always-on field regardless of mode.

### Item cost (redesigned — direct entry is now the default and primary path)
Each line's default pricing block is a single price field plus an explicit two-button toggle: **"Per unit"** or **"Total for this line."** Whichever the user enters, the other figure is **live-computed and displayed** next to it in grey (e.g. typing a per-unit price shows "Total for line: Rp X" live; typing a line total shows "≈ Per unit: Rp X" live, `≈` since it may not divide evenly). There is never ambiguity about which figure was actually typed.

### Item cost — Lump-Sum Pricing Fallback (narrow opt-in, replaces the old default-path allocation model)
A separate, clearly-labeled control box, off by default: **"Use a lump-sum group in this purchase."** This exists specifically for the real, confirmed case where a whole bundle of items has one combined price with no per-item breakdown at all — it is explicitly **not** how pricing normally works, and the UI copy says so directly. When switched on, the purchase gets one lump-sum amount + one split method (**Equally** / **By Weight** / **By Value**, same three concepts as the old per-line allocation model, but now scoped to a single group rather than mixed arbitrarily across every line in a purchase). Each individual line then gets a **"Include in lump-sum group"** checkbox — ticked lines are priced by their share of the lump sum (using a per-line weight/value input matching the group's method, or nothing extra for Equally); unticked lines are still priced directly as normal. A purchase can freely mix lump-sum-priced lines and directly-priced lines side by side (demonstrated in the sample data — see `PUR-2026-0102`).

### Line items
Each line: a **typeahead SKU combobox** (replaces the old plain dropdown — see the dedicated section below), Quantity, the pricing block described above (direct toggle, or the lump-sum sub-field if that line is included in an active lump-sum group), the shipping sub-field appropriate to the purchase's shipping mode, live-computed **Item Cost / Shipping Share / Line Total** outputs, and a Remove button. "+ Create new SKU inline" (from the combobox) expands a mini-form: Name, Category, Identity Mode, and a **live SKU generator** (see below) with a real-time uniqueness check.

### Typeahead SKU / title search (new)
The old plain `<select>` SKU picker is replaced by a text-input-plus-dropdown combobox: typing any substring of a SKU or item name narrows the suggestion list live (max 8 shown), with "+ Create new SKU inline…" always pinned as the first option. This is most useful once the catalog is large — the sample data was expanded from the original 8 items to **21** (across all 5 categories, Automotive again deliberately mixing Fungible and Serialized) specifically to make the narrowing behavior demonstrable rather than trivial.

### SKU Generator (new)
Convention: **`{CATEGORY_PREFIX}-{first 1–2 words of the item name, slugified}-{4-digit sequence}`** — e.g. "Charizard VMAX Box" in TCG → `TCG-CHARIZARD-VMAX-0001`. Category prefixes: TCG→`TCG`, Watches→`WATCH`, Automotive→`AUTO`, Toys & Collectibles→`TOY`, Others→`OTH`. The sequence number is scoped to the **prefix+slug combination**, not the whole category — see Open Questions for why. The generated code appears live as the user types the Name or changes the Category, in both the Purchase Entry inline new-SKU box and the Inventory "+ Add New Item" panel (same shared generator function, one implementation). The SKU field itself stays directly editable; typing into it manually marks it "touched" and stops auto-regenerating on further name/category edits, with a "🔄 Regenerate" action to intentionally reset back to the generated value.

### SKU Uniqueness Checker (new)
Real-time validation wherever a SKU is entered or generated — the Purchase Entry inline new-SKU box, and the Inventory "+ Add New Item" panel. Checks the candidate SKU against every existing item **and** any other new-SKU line currently being drafted in the same purchase (so two lines can't silently create the same duplicate SKU together). Shows an inline green "✓ Available" or red "✗ This SKU already exists" message and blocks Save/Add while a duplicate is present — demonstrated for both a manually-typed override and (indirectly) the auto-generated path, since the generator itself is collision-avoiding by construction.

### Serial ID Generator (new)
For a Serialized line, a **"⚡ Generate all"** button (or "⚡ Generate" for a single unit) auto-fills every unit's Serial/Asset ID using the convention **`{SKU}-{3-digit sequence}`**, continuing from however many units of that SKU already exist across posted purchases (so a later batch never collides with an earlier one — e.g. a SKU with 2 existing units generates `...-003`, `...-004`, `...-005` for a new 3-unit line). Every generated ID stays individually editable afterward — generation is a starting point, not a lock.

### Photo capture (new)
Each individual unit in the serial-assignment panel gets its own **"Photo"** file input (`accept="image/*"`). Selecting a file reads it client-side via `FileReader` and shows a live thumbnail preview plus the filename — no real upload. **Conceptually** stored the same way as invoices (Drive holds the file, the app's own data holds a reference/thumbnail), simulated here entirely in-browser. Not shown for Fungible lines, only Serialized ones. The photo (thumbnail + filename) is preserved on save and surfaces again in Item Detail's serialized-unit table and in Purchase History's expanded line detail.

### The balance-check indicator (still the single most important interaction)
Rebuilt against the new model, same unmistakable visual treatment as before:
- **Running Total** = sum over every line of (Item Cost, whether direct-entered or lump-sum-split) + (Shipping Share, whichever shipping mode produced it).
- Compared against **Total Amount Paid** from the header.
- **Green, checkmark, "Balanced — Rp X reconciles exactly"** when they match to the rupiah; **red, warning icon, "Out of balance by Rp Y"** (signed difference) when they don't.
- **Save Purchase** is disabled whenever unbalanced, or when any Serialized line is missing a unit ID, or when any inline-created SKU is incomplete or a duplicate — the disabled tooltip names the specific reason.
- Unlike the old model (where a proportional line always automatically absorbed whatever pool it was given, so the check rarely went genuinely red from ordinary editing), **every item-cost figure is now either a real typed number or a lump-sum split of one** — so the check now goes red from an ordinary real mistake (a wrong Total Amount Paid, a forgotten line) far more often and more meaningfully, which is arguably a more honest reconciliation check than the one it replaces.

## Screen 4: Purchase History (ledger)

Read-only, chronological, same shape as before, updated for the new model. **Table columns**: Date, Purchase Reference, Vendor/Description, Total Amount Paid, Line Count, a permanent green "Balanced" badge (every saved purchase was necessarily balanced at save time). **Row interaction**: click to expand inline (accordion) showing: a shipping-mode summary line ("Shipping: Pooled — Rp 350.000 split By Weight"), a lump-sum-group summary line if one was used, the **invoice attachment** (filename + Parsed/Needs Review badge + recap, or "No invoice / proof of transfer attached"), and a line-level table (SKU, Qty, Item Cost with its pricing-mode label, Shipping with its mode label, Line Total) plus, for any serialized lines, each assigned unit's serial ID, cost, and photo thumbnail if attached.

**No edit/delete affordance** — unchanged reasoning from the original design (acquisition-only, no correction/reversal flow, not in scope).

## Design principles carried through every screen

- **Identity mode is a property of the item, not the category.** Automotive deliberately mixes Fungible (brake pads, oil filter, spark plugs) and Serialized (ECU, turbocharger) so this reads as plausible everywhere, not just asserted in this doc.
- **Currency is always explicit.** Every monetary figure renders with an `Rp` prefix and Indonesian thousands-separator formatting (e.g. `Rp 1.250.000`); no bare numbers, including inside live-computing math.
- **The reconciliation check is the emotional center of Purchase Entry**, deliberately styled to echo Project-Noctrowl's own balance-check language (green checkmark / red warning, a blocked save action) — a design-consistency choice across the sibling projects, not a functional integration.
- **Simulated-but-real, not decorative.** Everywhere this revision needed a feature that would normally require a backend (file upload, OCR, photo storage), the mockup does the genuine client-side half for real (actual `FileReader` calls, actual generated/validated data) and only fakes the network/server half — consistent with the brief's explicit instruction not to build hollow UI states.

## Open questions / assumptions

Carried over from the original milestone, still current:
1. **What "Total Amount Paid" includes.** Still assumed to be items + whichever shipping cost mode contributes to the reconciliation total (pooled amount, or the sum of manual per-line amounts) — i.e. everything the balance check checks against. This is now more clearly scoped than before since shipping is its own independent control, but the underlying question (does the header total always include shipping, or could a business sometimes pay shipping completely separately from the goods invoice?) is still an assumption, not a confirmed rule.
2. **Serial/Asset ID and photo sourcing.** Assumed the user manually photographs/tags each unit at purchase time (or uses the new generator for the ID) — no barcode/scan integration, no camera-native capture flow beyond a standard file picker. Not specified in the brief beyond "take/attach photo."
3. **Per-unit cost split default for serialized lines.** Still an even split of the line's total, remainder absorbed by the first unit, editable per unit afterward.
4. **New-SKU / new-item creation fields.** Still the minimum viable set (Name, Category, Identity Mode, SKU) — no description, image, weight/dimensions master field.
5. **No cross-purchase costing method (FIFO/weighted-average) is implied anywhere.** Sale/depletion stays out of scope this milestone.

New this revision:
6. **SKU generation convention.** `{CATEGORY_PREFIX}-{first 1–2 words of the name, slugified}-{4-digit sequence}`, sequence scoped to the prefix+slug combination (so `TCG-CHARIZARD-VMAX-0001` and a later, unrelated `TCG-YUGIOH-...-0001` don't compete for the same counter, but a *second* Charizard-VMAX item correctly becomes `...-0002`). The alternative — a single sequence per category regardless of name — was considered and rejected as less traceable/readable; this is a placeholder pending real confirmation, not a settled rule.
7. **Serial ID generation convention.** `{SKU}-{3-digit sequence}`, continuing from the count of already-existing units of that exact SKU (not a purchase-scoped counter) so batches never collide. Not specified in the brief; a reasonable placeholder.
8. **Lump-sum fallback scope: one group per purchase.** The brief allows the toggle "on the purchase (or per applicable line-group)"; this mockup implements exactly **one** lump-sum group per purchase (a line either joins it or is priced directly) rather than supporting multiple simultaneous, differently-configured lump-sum groups within one purchase. Simpler, and covers the confirmed real scenario (one bundled price, no breakdown) — revisit only if a purchase genuinely needs two separate unrelated bundled-price groups at once.
9. **Pooled shipping's "By Value" method reuses each line's already-computed Item Cost** as its proportional weight, rather than asking for a second, separate "estimated value" figure the way the old item-cost allocation model did. This avoids a redundant input and seemed like the more honest reading of "By Value (proportional to each line's item cost)" in the brief — flagged as an interpretation, not a certainty.
10. **Invoice OCR simulation is hardcoded by filename**, not any real image analysis: `invoice-toko-grosir-jaya.jpg` and `kurasi-shipping-receipt.jpg` (plus any real filename containing "invoice"/"receipt-clear"/"toko"/"struk" without also containing "handwritten"/"blur"/"photo") simulate **Parsed**; `watch-receipt-handwritten.jpg`, `blurry-phone-photo.jpg`, and everything else simulate **Needs Review**. Both states are exercised in the shipped sample data without requiring any user action (`PUR-2026-0083` shows Parsed, `PUR-2026-0078` shows Needs Review, both visible by just opening Purchase History), and both are additionally reachable interactively in the live Purchase Entry demo via two "try a sample" buttons plus a real file input.
11. **Add New Item (Inventory screen) creates a zero-quantity, zero-cost item record.** There's no concept of "pending first purchase" state beyond simply showing 0 units / Rp 0 cost basis and an empty purchase-history table in Item Detail — same empty-state treatment as any other unpurchased catalog item, not a distinct status.

None of the above needed to block finishing this milestone's mockup — each is implemented as a single, clearly-labeled, consistent choice, exactly as instructed, so it can be reviewed and either confirmed or overridden before any real backend logic is built on top of it.
