# Copyright (c) 2026, G2Virtu and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, nowdate

# "cost" -> GP % = GP Amt / TP          (markup on cost, as in the reference screen)
# "sale" -> GP % = GP Amt / Sale Rate   (margin on sale)
# Keep in sync with GP_ON in price_update_tool.js
GP_ON = "cost"

PRECISION = 3
MAX_ROWS = 3000  # safety cap for "Get Items"
BG_THRESHOLD = 200  # rows above this are applied in a background job


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def gp(sell, buy):
	amt = flt(sell) - flt(buy)
	base = flt(buy) if GP_ON == "cost" else flt(sell)
	pct = amt / base * 100 if base else 0
	return flt(amt, PRECISION), flt(pct, PRECISION)


def eff(new, cur):
	"""Effective value: the new rate if entered, else the current one."""
	return flt(new, PRECISION) or flt(cur, PRECISION)


def calc(r):
	"""Recalculate GP fields and discount % on a row (doc row or dict)."""
	b, s, m = flt(r.cur_buying), flt(r.cur_selling), flt(r.cur_mrp)
	r.gp_amt, r.gp_pct = gp(s, b)
	nb, ns, nm = eff(r.new_buying, b), eff(r.new_selling, s), eff(r.new_mrp, m)
	r.new_gp_amt, r.new_gp_pct = gp(ns, nb)
	r.discount_pct = flt((1 - ns / nm) * 100, PRECISION) if nm else 0


def party_for(price_for, customer, supplier, kind):
	"""Party filter used for Item Price rows of a price list kind
	('buying' | 'selling' | 'mrp'). Returns None when not applicable.
	  All Parties -> general prices (no customer / supplier)
	  Customer    -> selling + MRP lists only
	  Supplier    -> buying list only
	"""
	if price_for == "Customer":
		return {"customer": customer} if kind in ("selling", "mrp") else None
	if price_for == "Supplier":
		return {"supplier": supplier} if kind == "buying" else None
	return {}


def get_prices(price_list, uoms, party):
	"""Current valid rate per item for a price list, in 1 query per 1000 items.

	uoms  : {item_code: uom}
	party : {} | {"customer": x} | {"supplier": x}
	        party specific rows win over general rows.
	Returns {item_code: rate}
	"""
	if not price_list or not uoms:
		return {}

	values = {"pl": price_list, "today": nowdate()}
	if party.get("customer"):
		pcond = "and ifnull(supplier,'')='' and ifnull(customer,'') in ('', %(party)s)"
		values["party"] = party["customer"]
	elif party.get("supplier"):
		pcond = "and ifnull(customer,'')='' and ifnull(supplier,'') in ('', %(party)s)"
		values["party"] = party["supplier"]
	else:
		pcond = "and ifnull(customer,'')='' and ifnull(supplier,'')=''"

	out = {}
	codes = list(uoms)
	for i in range(0, len(codes), 1000):
		values["codes"] = tuple(codes[i : i + 1000])
		rows = frappe.db.sql(
			f"""
			select item_code, ifnull(uom,'') as uom, price_list_rate,
				(ifnull(customer,'')!='' or ifnull(supplier,'')!='') as is_party
			from `tabItem Price`
			where price_list=%(pl)s and item_code in %(codes)s
				and (valid_from is null or valid_from <= %(today)s)
				and (valid_upto is null or valid_upto >= %(today)s)
				{pcond}
			order by is_party asc, valid_from asc
			""",
			values,
			as_dict=True,
		)
		for r in rows:
			if r.uom in ("", uoms[r.item_code]):
				out[r.item_code] = flt(r.price_list_rate)  # later rows override earlier
	return out


def build_rows(items, doc):
	"""items: Item records (name, item_name, stock_uom, last_purchase_rate)."""
	uoms = {i.name: i.stock_uom for i in items}

	def prices(price_list, kind):
		# show general rates for reference even where the party can't be changed
		party = party_for(doc.price_for, doc.customer, doc.supplier, kind) or {}
		return get_prices(price_list, uoms, party)

	buy = prices(doc.buying_price_list, "buying")
	sell = prices(doc.selling_price_list, "selling")
	mrp = prices(doc.mrp_price_list, "mrp")

	rows = []
	for i in items:
		b, s, m = buy.get(i.name, 0), sell.get(i.name, 0), mrp.get(i.name, 0)
		r = frappe._dict(
			item_code=i.name,
			item_name=i.item_name,
			uom=i.stock_uom,
			cur_buying=b,
			last_purchase_rate=flt(i.last_purchase_rate),
			cur_selling=s,
			cur_mrp=m,
			new_buying=b,
			new_selling=s,
			new_mrp=m,
		)
		calc(r)
		rows.append(r)
	return rows


ITEM_FIELDS = ["name", "item_name", "stock_uom", "last_purchase_rate"]


# --------------------------------------------------------------------------
# controller
# --------------------------------------------------------------------------
class PriceUpdateTool(Document):
	# ---------------- validation ----------------
	def validate(self):
		self.validate_price_lists()
		self.validate_party()
		self.validate_rows()

	def validate_price_lists(self):
		for field, flag in (
			("buying_price_list", "buying"),
			("selling_price_list", "selling"),
			("mrp_price_list", "selling"),
		):
			label = self.meta.get_label(field)
			pl = frappe.db.get_value("Price List", self.get(field), ["enabled", "buying", "selling"], as_dict=True)
			if not pl or not pl.enabled:
				frappe.throw(_("{0} must be an enabled Price List").format(label))
			if not pl.get(flag):
				frappe.throw(_("{0}: Price List {1} is not marked for {2}").format(label, self.get(field), flag.title()))
		if self.selling_price_list == self.mrp_price_list:
			frappe.throw(_("Selling Price List and MRP Price List must be different"))

	def validate_party(self):
		if self.price_for == "Customer":
			if not self.customer:
				frappe.throw(_("Please select a Customer"))
			self.supplier = None
		elif self.price_for == "Supplier":
			if not self.supplier:
				frappe.throw(_("Please select a Supplier"))
			self.customer = None
		else:
			self.customer = self.supplier = None

	def row_changes(self, r):
		"""{kind: new_rate} for every price that actually changes on this row."""
		out = {}
		for kind, new, cur in (
			("buying", r.new_buying, r.cur_buying),
			("selling", r.new_selling, r.cur_selling),
			("mrp", r.new_mrp, r.cur_mrp),
		):
			n = flt(new, PRECISION)
			if n and n != flt(cur, PRECISION):
				out[kind] = n
		return out

	def validate_rows(self):
		if not self.items:
			frappe.throw(_("Please add items to update"))

		seen, warnings = set(), []
		for r in self.items:
			key = (r.item_code, r.uom)
			if key in seen:
				frappe.throw(_("Row {0}: {1} ({2}) is entered more than once").format(r.idx, r.item_code, r.uom))
			seen.add(key)

			for kind, rate in self.row_changes(r).items():
				if rate <= 0:
					frappe.throw(_("Row {0}: new {1} rate must be greater than zero").format(r.idx, kind))
				if party_for(self.price_for, self.customer, self.supplier, kind) is None:
					frappe.throw(
						_("Row {0}: {1} rates cannot be changed when Price For is {2}").format(
							r.idx, kind.title(), self.price_for
						)
					)

			calc(r)

			nb, ns, nm = eff(r.new_buying, r.cur_buying), eff(r.new_selling, r.cur_selling), eff(r.new_mrp, r.cur_mrp)
			if nb and ns and ns < nb:
				warnings.append(_("Row {0} ({1}): sale rate {2} is below TP {3}").format(r.idx, r.item_code, ns, nb))
			if nm and ns and ns > nm:
				warnings.append(_("Row {0} ({1}): sale rate {2} is above MRP {3}").format(r.idx, r.item_code, ns, nm))

		if warnings:
			frappe.msgprint(
				"<br>".join(warnings[:20]) + ("<br>..." if len(warnings) > 20 else ""),
				title=_("Please review"),
				indicator="orange",
			)

	def before_submit(self):
		if not any(self.row_changes(r) for r in self.items):
			frappe.throw(_("There are no price changes to submit"))

	# ---------------- submit / cancel ----------------
	def on_submit(self):
		if len(self.items) > BG_THRESHOLD:
			frappe.enqueue(
				apply_in_background,
				queue="long",
				timeout=3600,
				docname=self.name,
				user=frappe.session.user,
				enqueue_after_commit=True,
			)
			frappe.msgprint(_("Prices are being updated in the background. You will be notified when done."))
		else:
			self.apply_prices()

	def apply_prices(self):
		lists = {
			"buying": self.buying_price_list,
			"selling": self.selling_price_list,
			"mrp": self.mrp_price_list,
		}
		total = len(self.items)
		for idx, r in enumerate(self.items, 1):
			applied = []
			for kind, rate in self.row_changes(r).items():
				party = party_for(self.price_for, self.customer, self.supplier, kind)
				applied.append(self.upsert_item_price(r, lists[kind], rate, party))

			frappe.db.set_value(
				r.doctype,
				r.name,
				{
					"old_buying": r.cur_buying,
					"old_selling": r.cur_selling,
					"old_mrp": r.cur_mrp,
					"applied_prices": json.dumps(applied),
				},
				update_modified=False,
			)
			if total > 50 and idx % 25 == 0:
				frappe.publish_progress(idx * 100 / total, title=_("Updating prices..."), description=f"{idx} / {total}")

	def upsert_item_price(self, row, price_list, rate, party):
		customer = party.get("customer") or ""
		supplier = party.get("supplier") or ""

		existing = frappe.db.sql(
			"""
			select name from `tabItem Price`
			where item_code=%s and price_list=%s and ifnull(uom,'') in ('', %s)
				and ifnull(customer,'')=%s and ifnull(supplier,'')=%s
				and (valid_from is null or valid_from <= %s)
				and (valid_upto is null or valid_upto >= %s)
			order by valid_from desc limit 1
			""",
			(row.item_code, price_list, row.uom, customer, supplier, self.valid_from, self.valid_from),
		)

		if existing:
			doc = frappe.get_doc("Item Price", existing[0][0])
			old_rate = flt(doc.price_list_rate)
			doc.price_list_rate = rate
			doc.save(ignore_permissions=True)
			created = 0
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Item Price",
					"item_code": row.item_code,
					"price_list": price_list,
					"uom": row.uom,
					"price_list_rate": rate,
					"valid_from": self.valid_from,
					"customer": customer or None,
					"supplier": supplier or None,
				}
			).insert(ignore_permissions=True)
			old_rate, created = 0, 1

		return {
			"price_list": price_list,
			"item_price": doc.name,
			"old_rate": old_rate,
			"new_rate": rate,
			"created": created,
		}

	def on_cancel(self):
		skipped = []
		for r in self.items:
			for a in json.loads(r.applied_prices or "[]"):
				if not frappe.db.exists("Item Price", a["item_price"]):
					continue
				current = flt(frappe.db.get_value("Item Price", a["item_price"], "price_list_rate"), PRECISION)
				if current != flt(a["new_rate"], PRECISION):
					# rate was changed again after this document - don't overwrite it
					skipped.append(f"{r.item_code} ({a['price_list']})")
					continue
				if a.get("created"):
					frappe.delete_doc("Item Price", a["item_price"], ignore_permissions=True)
				else:
					frappe.db.set_value("Item Price", a["item_price"], "price_list_rate", a["old_rate"])

		if skipped:
			frappe.msgprint(
				_("These prices were changed after this document and were NOT reverted:") + "<br>" + "<br>".join(skipped[:30]),
				indicator="orange",
			)

	# ---------------- whitelisted helpers for the form ----------------
	@frappe.whitelist()
	def fetch_items(self):
		if not (self.item_group or self.brand or self.supplier_filter):
			frappe.throw(_("Select at least one of Item Group, Brand or Supplier (Items) to load items"))

		filters = {"disabled": 0, "has_variants": 0}

		if self.item_group:
			lft, rgt = frappe.db.get_value("Item Group", self.item_group, ["lft", "rgt"])
			filters["item_group"] = [
				"in",
				frappe.get_all("Item Group", filters={"lft": [">=", lft], "rgt": ["<=", rgt]}, pluck="name"),
			]

		if self.brand:
			filters["brand"] = self.brand

		if self.supplier_filter:
			source = self.items_source or "Both"
			codes = set()
			if source in ("Item Supplier Master", "Both"):
				codes |= set(
					frappe.get_all(
						"Item Supplier",
						filters={"supplier": self.supplier_filter, "parenttype": "Item"},
						pluck="parent",
					)
				)
			if source in ("Purchase History", "Both"):
				codes |= set(
					frappe.db.sql_list(
						"""
						select distinct pii.item_code
						from `tabPurchase Invoice Item` pii
						join `tabPurchase Invoice` pi on pi.name = pii.parent
						where pi.docstatus = 1 and pi.supplier = %s
						""",
						self.supplier_filter,
					)
				)
			if not codes:
				return []
			filters["name"] = ["in", list(codes)]

		items = frappe.get_all(
			"Item",
			filters=filters,
			fields=ITEM_FIELDS,
			order_by="item_group, item_name",
			limit_page_length=MAX_ROWS + 1,
		)
		if len(items) > MAX_ROWS:
			frappe.throw(_("More than {0} items match. Please narrow the filters.").format(MAX_ROWS))

		return build_rows(items, self)

	@frappe.whitelist()
	def fetch_customer_prices(self):
		if self.price_for != "Customer" or not self.customer:
			frappe.throw(_("Set Price For = Customer and select the target Customer first"))
		if not self.copy_from_customer:
			frappe.throw(_("Select the customer to copy prices from"))
		if self.copy_from_customer == self.customer:
			frappe.throw(_("Source and target customer are the same"))

		lists = [self.selling_price_list, self.mrp_price_list]
		codes = list(
			set(
				frappe.get_all(
					"Item Price",
					filters={"customer": self.copy_from_customer, "price_list": ["in", lists]},
					pluck="item_code",
				)
			)
		)
		if not codes:
			return []

		items = frappe.get_all(
			"Item", filters={"name": ["in", codes], "disabled": 0}, fields=ITEM_FIELDS, order_by="item_name"
		)
		rows = build_rows(items, self)  # current = target customer's effective rates

		uoms = {i.name: i.stock_uom for i in items}
		src = {"customer": self.copy_from_customer}
		src_sell = get_prices(self.selling_price_list, uoms, src)
		src_mrp = get_prices(self.mrp_price_list, uoms, src)
		for r in rows:
			r.new_selling = src_sell.get(r.item_code) or r.new_selling
			r.new_mrp = src_mrp.get(r.item_code) or r.new_mrp
			calc(r)
		return rows

	@frappe.whitelist()
	def fetch_item_rates(self, item_code):
		item = frappe.db.get_value("Item", item_code, ITEM_FIELDS + ["disabled"], as_dict=True)
		if not item or item.disabled:
			frappe.throw(_("Item {0} is missing or disabled").format(item_code))
		return build_rows([item], self)[0]


def apply_in_background(docname, user):
	doc = frappe.get_doc("Price Update Tool", docname)
	try:
		doc.apply_prices()
		frappe.db.commit()
		frappe.publish_realtime("msgprint", _("Price Update {0} has been applied.").format(docname), user=user)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"Price Update Tool {docname} failed")
		frappe.publish_realtime(
			"msgprint",
			_("Price Update {0} failed and nothing was changed. See Error Log, then cancel and re-submit.").format(docname),
			user=user,
		)