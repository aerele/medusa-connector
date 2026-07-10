"""Map Medusa products to ERPNext Items."""


class ProductMapper:
	def map(self, product: dict) -> dict:
		"""Map a complete Medusa product payload to an ERPNext item template."""
		product_id = product.get("id")
		if not product_id:
			raise ValueError("Medusa product payload does not contain an id")
		options = product.get("options") or []
		variants = product.get("variants") or []
		description = product.get("description") or ""
		if product.get("subtitle"):
			description = f"{product['subtitle']}\n\n{description}".strip()
		return {
			"item_code": product_id,
			"item_name": product.get("title") or product_id,
			"description": description,
			"image": product.get("thumbnail") or self._first_image_url(product),
			"item_group": "All Item Groups",
			"stock_uom": "Nos",
			"disabled": 0 if product.get("status") == "published" else 1,
			"has_variants": int(bool(options and variants)),
			"attributes": [
				{
					"name": option.get("title"),
					"values": [
						value.get("value") for value in option.get("values") or [] if value.get("value")
					],
				}
				for option in options
				if option.get("title")
			],
			"variants": [self._map_variant(variant, product, options) for variant in variants],
		}

	@staticmethod
	def _first_image_url(product: dict) -> str | None:
		images = product.get("images") or []
		return images[0].get("url") if images else None

	def _map_variant(self, variant: dict, product: dict, options: list[dict]) -> dict:
		values_by_option_id = {
			value.get("option_id"): value.get("value")
			for value in variant.get("options") or []
			if value.get("option_id") and value.get("value")
		}
		return {
			"item_code": variant.get("id"),
			"item_name": f"{product.get('title') or product.get('id')} - {variant.get('title') or variant.get('id')}",
			"sku": variant.get("sku"),
			"barcode": variant.get("barcode") or variant.get("ean") or variant.get("upc"),
			"image": variant.get("thumbnail") or self._first_image_url(product),
			"attributes": {
				option.get("title"): values_by_option_id.get(option.get("id"))
				for option in options
				if option.get("title") and values_by_option_id.get(option.get("id"))
			},
		}
