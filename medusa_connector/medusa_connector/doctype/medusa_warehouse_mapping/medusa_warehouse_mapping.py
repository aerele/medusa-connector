# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MedusaWarehouseMapping(Document):
	# begin: auto-generated types
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled: DF.Check
		erpnext_warehouse: DF.Link | None
		medusa_location_id: DF.Data
		medusa_location_name: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
	# end: auto-generated types

	pass
