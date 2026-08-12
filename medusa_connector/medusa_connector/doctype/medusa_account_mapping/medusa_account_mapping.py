# import frappe
from frappe.model.document import Document


class MedusaAccountMapping(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		erpnext_account: DF.Link
		medusa_tax_or_shipping_id: DF.Data
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		tax_code: DF.Data | None
		tax_name: DF.Data | None
		tax_rate: DF.Float
	# end: auto-generated types

	pass
