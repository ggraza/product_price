// Copyright (c) 2026, G2Virtu and contributors
// For license information, please see license.txt

// "cost" = GP % on TP (markup), "sale" = GP % on sale rate (margin).
// Keep in sync with GP_ON in price_update_tool.py
const GP_ON = "cost";

const KIND_FIELDS = {
	Buying: ["cur_buying", "new_buying"],
	Selling: ["cur_selling", "new_selling"],
	MRP: ["cur_mrp", "new_mrp"],
};

frappe.ui.form.on("Price Update Tool", {
	setup(frm) {
		frm.set_query("buying_price_list", () => ({ filters: { buying: 1, enabled: 1 } }));
		["selling_price_list", "mrp_price_list"].forEach((f) =>
			frm.set_query(f, () => ({ filters: { selling: 1, enabled: 1 } }))
		);
	},

	price_for(frm) {
		if (frm.doc.price_for !== "Customer") {
			frm.set_value({ customer: "", copy_from_customer: "" });
		}
		if (frm.doc.price_for !== "Supplier") {
			frm.set_value("supplier", "");
		}
		stale_alert(frm);
	},

	customer: (frm) => stale_alert(frm),
	supplier: (frm) => stale_alert(frm),
	buying_price_list: (frm) => stale_alert(frm),
	selling_price_list: (frm) => stale_alert(frm),
	mrp_price_list: (frm) => stale_alert(frm),

	get_items(frm) {
		if (frm.doc.docstatus !== 0) return;
		const run = () =>
			frm
				.call({
					method: "fetch_items",
					doc: frm.doc,
					freeze: true,
					freeze_message: __("Loading items and current rates..."),
				})
				.then((r) => load_rows(frm, r.message));

		if ((frm.doc.items || []).length) {
			frappe.confirm(__("This will replace the existing rows. Continue?"), run);
		} else {
			run();
		}
	},

	copy_prices(frm) {
		if (frm.doc.docstatus !== 0) return;
		const run = () =>
			frm
				.call({
					method: "fetch_customer_prices",
					doc: frm.doc,
					freeze: true,
					freeze_message: __("Loading customer prices..."),
				})
				.then((r) => load_rows(frm, r.message));

		if ((frm.doc.items || []).length) {
			frappe.confirm(__("This will replace the existing rows. Continue?"), run);
		} else {
			run();
		}
	},

	apply_rule(frm) {
		if (frm.doc.docstatus !== 0) return;
		const d = frm.doc;

		if (!(d.items || []).length) {
			frappe.msgprint(__("Load items first."));
			return;
		}
		if (!flt(d.value)) {
			frappe.msgprint(__("Enter a Value."));
			return;
		}

		const discount = d.change_type === "Discount % on MRP";
		const kinds = discount ? ["Selling"] : d.apply_on === "All" ? ["Buying", "Selling", "MRP"] : [d.apply_on];
		const usable = kinds.filter((k) => is_applicable(d, k));

		if (!usable.length) {
			frappe.msgprint(__("{0} prices cannot be changed when Price For is {1}.", [kinds.join("/"), d.price_for]));
			return;
		}

		let changed = 0;
		let skipped = 0;

		d.items.forEach((row) => {
			if (discount) {
				// new sale rate = MRP less discount %
				const mrp = flt(row.new_mrp) || flt(row.cur_mrp);
				if (!mrp) {
					skipped++;
					return;
				}
				const v = round_rate(mrp * (1 - flt(d.value) / 100), d.rounding);
				if (v <= 0) {
					skipped++;
					return;
				}
				row.new_selling = v;
				changed++;
			} else {
				usable.forEach((kind) => {
					const [cur_f, new_f] = KIND_FIELDS[kind];
					const cur = flt(row[cur_f]);
					if (d.change_type !== "Set" && !cur) {
						skipped++; // nothing to increase
						return;
					}
					const v = calc_new(cur, d.change_type, flt(d.value), d.rounding);
					if (v <= 0) {
						skipped++;
						return;
					}
					row[new_f] = v;
					changed++;
				});
			}
			calc_row(row);
		});

		frm.refresh_field("items");
		frm.dirty();
		frappe.show_alert({
			message: __("{0} prices updated, {1} skipped", [changed, skipped]),
			indicator: skipped ? "orange" : "green",
		});
	},
});

frappe.ui.form.on("Price Update Tool Item", {
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item_code) return;
		frm
			.call({ method: "fetch_item_rates", doc: frm.doc, args: { item_code: row.item_code } })
			.then((r) => {
				if (!r.message) return;
				Object.assign(row, r.message);
				frm.refresh_field("items");
			});
	},

	new_buying: (frm, cdt, cdn) => row_changed(frm, cdt, cdn, "new_buying"),
	new_selling: (frm, cdt, cdn) => row_changed(frm, cdt, cdn, "new_selling"),
	new_mrp: (frm, cdt, cdn) => row_changed(frm, cdt, cdn, "new_mrp"),
	discount_pct: (frm, cdt, cdn) => row_changed(frm, cdt, cdn, "discount_pct"),
});

// --------------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------------
function load_rows(frm, rows) {
	rows = rows || [];
	if (!rows.length) {
		frappe.msgprint(__("No items found for the selected filters."));
		return;
	}
	frm.clear_table("items");
	rows.forEach((r) => frm.add_child("items", r));
	frm.refresh_field("items");
	frm.dirty();
	frappe.show_alert({ message: __("{0} items loaded", [rows.length]), indicator: "green" });
}

function stale_alert(frm) {
	if (frm.doc.docstatus === 0 && (frm.doc.items || []).length) {
		frappe.show_alert({
			message: __("Selection changed. Click Get Items again to refresh current rates."),
			indicator: "orange",
		});
	}
}

function is_applicable(doc, kind) {
	if (doc.price_for === "Customer") return kind !== "Buying";
	if (doc.price_for === "Supplier") return kind === "Buying";
	return true;
}

function round_rate(v, mode) {
	const step = { "Nearest 0.5": 0.5, "Nearest 1": 1, "Nearest 5": 5 }[mode];
	return step ? Math.round(v / step) * step : flt(v, 3);
}

function calc_new(cur, type, val, mode) {
	let v = cur;
	if (type === "Set") v = val;
	else if (type === "Increase %") v = cur * (1 + val / 100);
	else if (type === "Increase Amount") v = cur + val;
	return round_rate(v, mode);
}

function gp(sell, buy) {
	const amt = flt(sell) - flt(buy);
	const base = GP_ON === "cost" ? flt(buy) : flt(sell);
	return [flt(amt, 3), base ? flt((amt / base) * 100, 3) : 0];
}

const eff = (n, c) => flt(n) || flt(c);

function calc_row(row, keep_discount) {
	[row.gp_amt, row.gp_pct] = gp(row.cur_selling, row.cur_buying);
	const nb = eff(row.new_buying, row.cur_buying);
	const ns = eff(row.new_selling, row.cur_selling);
	const nm = eff(row.new_mrp, row.cur_mrp);
	[row.new_gp_amt, row.new_gp_pct] = gp(ns, nb);
	if (!keep_discount) {
		row.discount_pct = nm ? flt((1 - ns / nm) * 100, 3) : 0;
	}
}

// Values are assigned directly on the row (no set_value) so the field events
// don't re-trigger each other; only the affected cells are redrawn.
function row_changed(frm, cdt, cdn, field) {
	const row = locals[cdt][cdn];

	if (field === "discount_pct") {
		const mrp = eff(row.new_mrp, row.cur_mrp);
		if (mrp) {
			row.new_selling = round_rate(mrp * (1 - flt(row.discount_pct) / 100), frm.doc.rounding);
		}
	}

	calc_row(row, field === "discount_pct");

	const grid_row = frm.fields_dict.items.grid.grid_rows_by_docname[cdn];
	if (grid_row && grid_row.refresh_field) {
		["new_selling", "discount_pct", "gp_pct", "gp_amt", "new_gp_pct", "new_gp_amt"].forEach((f) =>
			grid_row.refresh_field(f)
		);
	} else {
		frm.refresh_field("items");
	}
}