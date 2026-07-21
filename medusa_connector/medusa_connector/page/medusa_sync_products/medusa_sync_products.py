# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Whitelisted API for the Sync Medusa Products desk page."""

from __future__ import annotations

import json
from time import process_time

import frappe
from frappe import _
from frappe.utils import cstr, get_datetime, now_datetime
from frappe.utils.background_jobs import get_jobs

from medusa_connector.constants import (
	MODULE_NAME,
	PRODUCT_SYNC_JOB_NAME,
	PRODUCT_SYNC_REALTIME_KEY,
	SETTING_DOCTYPE,
)
from medusa_connector.medusa.product import DEFAULT_PRODUCT_FIELDS, ProductService
from medusa_connector.product.mapper import ProductMapper
from medusa_connector.product.sync import ProductSync
from medusa_connector.utils.logging import create_sync_log, update_sync_log


def _assert_enabled() -> None:
	if not frappe.db.get_single_value(SETTING_DOCTYPE, "enabled"):
		frappe.throw(_("Medusa Connector is disabled. Enable it in Medusa Settings."))


def publish(
	message: str, *, synced: bool = False, error: bool = False, done: bool = False, br: bool = True
) -> None:
	frappe.publish_realtime(
		PRODUCT_SYNC_REALTIME_KEY,
		{
			"synced": synced,
			"error": error,
			"message": message + ("<br /><br />" if br else "<br />"),
			"done": done,
		},
	)


@frappe.whitelist()
def get_product_count() -> dict:
	"""Counts for the Sync Products dashboard."""
	_assert_enabled()
	service = ProductService()
	page = service.list_products(limit=1, offset=0)
	medusa_count = page.get("count") or 0

	erpnext_count = frappe.db.count("Item", {"variant_of": ["is", "not set"]})

	template_count = frappe.db.count(
		"Ecommerce Item",
		{"integration": MODULE_NAME, "has_variants": 1},
	)
	simple_sellable = frappe.db.count(
		"Ecommerce Item",
		{
			"integration": MODULE_NAME,
			"has_variants": 0,
			"variant_of": ["in", ["", None]],
		},
	)
	synced_count = template_count + simple_sellable

	return {
		"medusaCount": medusa_count,
		"erpnextCount": erpnext_count,
		"syncedCount": synced_count,
	}


def is_product_sync_running() -> bool:
	return _job_running()


def _job_running() -> bool:
	jobs = get_jobs()
	return PRODUCT_SYNC_JOB_NAME in jobs.get("queued", {}) or PRODUCT_SYNC_JOB_NAME in jobs.get("started", {})


def get_synced_product_ids(product_ids: list[str]) -> set[str]:
	"""Return the subset of Medusa product ids that already have an
	Ecommerce Item mapping.

	One batched ``IN`` query instead of a per-product ``frappe.db.exists()``
	check — matters because ``list_medusa_products`` can be called with
	``fetch_all=1``, which would otherwise turn this into an N+1 query loop
	across the whole Medusa catalog on every page load / keystroke.
	"""
	if not product_ids:
		return set()
	rows = frappe.get_all(
		"Ecommerce Item",
		filters={
			"integration": MODULE_NAME,
			"integration_item_code": ["in", product_ids],
		},
		fields=["integration_item_code"],
		distinct=True,
	)
	return {row.integration_item_code for row in rows}


@frappe.whitelist()
def get_products(
	offset: int = 0,
	limit: int = 20,
	q: str | None = None,
	status: str | None = None,
	fetch_all: int | bool = 0,
) -> dict:
	return list_medusa_products(
		offset=offset, limit=limit, q=q, status=status, fetch_all=bool(int(fetch_all))
	)


def list_medusa_products(
	offset: int = 0,
	limit: int = 20,
	q: str | None = None,
	status: str | None = None,
	fetch_all: bool = False,
) -> dict:
	"""Fetch Medusa product list with sync status for the desk page."""
	frappe.only_for("System Manager")
	_assert_enabled()

	service = ProductService()

	# When fetch_all is passed, stream all items using iter_products
	if fetch_all:
		raw_products = list(service.iter_products(q=q, status=status))
		total_count = len(raw_products)
	else:
		offset = int(offset or 0)
		limit = min(int(limit or 20), 100)
		page = service.list_products(limit=limit, offset=offset, q=q, status=status)
		raw_products = page.get("products") or []
		total_count = page.get("count") or 0

	products_meta = [
		{
			"id": p.get("id"),
			"title": p.get("title"),
			"status": p.get("status"),
			"handle": p.get("handle"),
			"thumbnail": p.get("thumbnail"),
			"sku": _primary_sku(p),
			"updated_at": p.get("updated_at"),
		}
		for p in raw_products
	]

	synced_ids = get_synced_product_ids([p["id"] for p in products_meta if p.get("id")])
	products = [{**p, "synced": (p["id"] in synced_ids) if p.get("id") else False} for p in products_meta]

	return {
		"products": products,
		"count": total_count,
		"limit": limit if not fetch_all else total_count,
		"offset": offset if not fetch_all else 0,
	}


@frappe.whitelist()
def sync_product(product_id: str) -> dict:
	return import_single_product(product_id, force=True)


@frappe.whitelist()
def resync_product(product_id: str) -> dict:
	return import_single_product(product_id, force=True)


@frappe.whitelist()
def start_sync(mode: str = "Full", q: str | None = None, status: str | None = None, force: int = 1) -> dict:
	return start_product_sync(mode=mode, q=q, status=status, force=force)


def start_product_sync(
	mode: str = "Full",
	*,
	q: str | None = None,
	status: str | None = None,
	force: int | bool = 0,
) -> dict:
	"""Enqueue a full or incremental catalog sync. Returns job meta."""
	frappe.only_for("System Manager")
	_assert_enabled()

	if _job_running():
		return {"status": "Busy", "message": "A product sync is already running."}

	force_flag = int(force)
	sync_mode = "Incremental" if str(mode).lower().startswith("inc") else "Full"

	log_name = create_sync_log(
		sync_type="Product Import",
		sync_mode=sync_mode,
		status="Queued",
		method=f"{__name__}.run_product_sync",
		message=f"{sync_mode} product sync queued",
		request_data={"mode": sync_mode, "q": q, "status": status, "force": force_flag},
	)

	frappe.enqueue(
		run_product_sync,
		queue="long",
		timeout=3600,
		job_name=PRODUCT_SYNC_JOB_NAME,
		log_name=log_name,
		mode=sync_mode,
		q=q,
		status=status,
		force=bool(force_flag),
		enqueue_after_commit=True,
	)
	return {"status": "Queued", "log": log_name, "job_name": PRODUCT_SYNC_JOB_NAME}


def run_product_sync(
	log_name: str,
	mode: str = "Full",
	q: str | None = None,
	status: str | None = None,
	force: bool = False,
) -> dict:
	"""Background worker: walk Medusa products and sync each.

	``iter_products`` is called with ``fields=DEFAULT_PRODUCT_FIELDS`` so the
	listing payload already contains everything ``ProductMapper.map()``
	needs (variants, options, prices, categories, etc.). That full payload
	is then passed straight into ``_sync_one`` as ``product_payload`` — this
	avoids a second ``GET /admin/products/{id}`` per product that a bare
	``service.get_product(product_id)`` re-fetch would otherwise cost on
	every full/incremental sync.
	"""
	frappe.set_user("Administrator")
	_assert_enabled()

	update_sync_log(log_name, status="Running", message=f"{mode} product sync started")
	publish(f"Starting {mode} product sync…")

	updated_at_gt = None
	if mode == "Incremental":
		last_sync = frappe.db.get_single_value(SETTING_DOCTYPE, "last_product_sync")
		if last_sync:
			updated_at_gt = get_datetime(last_sync).isoformat()

	service = ProductService()
	created = updated = skipped = failed = 0
	failed_ids: list[str] = []
	start = process_time()

	try:
		for product in service.iter_products(
			q=q,
			status=status,
			updated_at_gt=updated_at_gt,
			fields=DEFAULT_PRODUCT_FIELDS,
		):
			product_id = product.get("id")
			if not product_id:
				continue

			publish(f"Syncing {product_id} — {product.get('title') or ''}", br=False)
			savepoint = f"medusa_prod_{product_id[-12:]}"

			try:
				frappe.db.savepoint(savepoint)
				result = _sync_one(product_id, force=True, product_payload=product)

				if result["action"] == "created":
					created += 1
					publish(f"✅ Created {product_id} → {result['item_code']}", synced=True)
				elif result["action"] == "updated":
					updated += 1
					publish(f"↻ Updated {product_id} → {result['item_code']}", synced=True)
				else:
					skipped += 1
					publish(f"↷ Skipped {product_id}")

			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				failed += 1
				failed_ids.append(product_id)
				publish(f"❌ {product_id}: {exc}", error=True)
				frappe.log_error(title=f"Medusa product sync {product_id}", message=frappe.get_traceback())

		elapsed = process_time() - start
		status_label = "Success" if failed == 0 else ("Partial Success" if created + updated else "Failed")
		message = (
			f"{mode} sync done in {elapsed:.1f}s — "
			f"created {created}, updated {updated}, skipped {skipped}, failed {failed}."
		)
		summary = {
			"created": created,
			"updated": updated,
			"skipped": skipped,
			"failed": failed,
			"failed_ids": failed_ids,
		}

		update_sync_log(
			log_name,
			status=status_label,
			message=message,
			created=created,
			updated=updated,
			skipped=skipped,
			failed=failed,
			failed_ids=failed_ids,
			summary=summary,
			complete=True,
		)

		frappe.db.set_value(
			SETTING_DOCTYPE,
			SETTING_DOCTYPE,
			{
				"last_product_sync": now_datetime(),
				"last_product_sync_message": message,
			},
			update_modified=False,
		)

		publish(f"🎉 {message}", done=True)
		return {"status": status_label, **summary}

	except Exception as exc:
		update_sync_log(
			log_name,
			status="Failed",
			message=str(exc),
			created=created,
			updated=updated,
			skipped=skipped,
			failed=failed,
			failed_ids=failed_ids,
			traceback=frappe.get_traceback(with_context=True),
			complete=True,
		)
		publish(f"❌ Sync aborted: {exc}", error=True, done=True)
		raise


def _primary_sku(product: dict) -> str:
	variants = product.get("variants") or []
	skus = [v["sku"] for v in variants if v.get("sku")]
	return ", ".join(skus[:5])


def _sync_one(product_id: str, *, force: bool = True, product_payload: dict | None = None) -> dict:
	from medusa_connector.utils.sync_guard import inbound_sync

	service = ProductService()
	product = product_payload or service.get_product(product_id)
	if not product or not product.get("id"):
		raise frappe.ValidationError(f"Product {product_id} not found in Medusa")

	mapped = ProductMapper().map(product)
	sync = ProductSync()

	with inbound_sync():
		return sync.sync(mapped, force=force)


@frappe.whitelist()
def sync_status() -> dict:
	return {"running": is_product_sync_running()}


def import_single_product(product_id: str, *, force: bool = True) -> dict:
	"""Import or re-sync one Medusa product. Returns result dict."""
	frappe.only_for("System Manager")
	_assert_enabled()

	log_name = create_sync_log(
		sync_type="Product Single",
		status="Running",
		method=f"{__name__}.import_single_product",
		message=f"Importing product {product_id}",
		request_data={"product_id": product_id, "force": force},
	)
	try:
		result = _sync_one(product_id, force=force)
		update_sync_log(
			log_name,
			status="Success",
			message=f"{result['action'].title()} {result['item_code']}",
			created=1 if result["action"] == "created" else 0,
			updated=1 if result["action"] == "updated" else 0,
			skipped=1 if result["action"] == "skipped" else 0,
			summary=result,
			complete=True,
		)
		return {"ok": True, **result, "log": log_name}
	except Exception as exc:
		update_sync_log(
			log_name,
			status="Failed",
			message=str(exc),
			failed=1,
			failed_ids=[product_id],
			traceback=frappe.get_traceback(with_context=True),
			complete=True,
		)
		frappe.db.rollback()
		return {"ok": False, "error": str(exc), "log": log_name}
