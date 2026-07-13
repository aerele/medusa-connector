# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Product import orchestration: single, full, and incremental Medusa → ERPNext sync."""

from __future__ import annotations

import json
from time import process_time

import frappe
from frappe.utils import get_datetime, now_datetime

from medusa_connector.constants import (
	PRODUCT_SYNC_JOB_NAME,
	PRODUCT_SYNC_REALTIME_KEY,
	SETTING_DOCTYPE,
)
from medusa_connector.medusa.product import ProductService
from medusa_connector.medusa_connector.doctype.medusa_item_mapping.medusa_item_mapping import (
	get_mapping_health,
	is_synced,
)
from medusa_connector.medusa_connector.doctype.medusa_sync_log.medusa_sync_log import (
	create_sync_log,
	update_sync_log,
)
from medusa_connector.product.mapper import ProductMapper
from medusa_connector.product.sync import ProductSync


def import_single_product(product_id: str, *, force: bool = True) -> dict:
	"""Import or re-sync one Medusa product. Returns result dict."""
	frappe.only_for("System Manager")
	_assert_enabled()

	log_name = create_sync_log(
		sync_type="Product Single",
		status="Running",
		method="medusa_connector.product.import_products.import_single_product",
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

	force = int(force)
	mode = "Incremental" if str(mode).lower().startswith("inc") else "Full"
	log_name = create_sync_log(
		sync_type="Product Import",
		sync_mode=mode,
		status="Queued",
		method="medusa_connector.product.import_products.run_product_sync",
		message=f"{mode} product sync queued",
		request_data={"mode": mode, "q": q, "status": status, "force": force},
	)

	frappe.enqueue(
		run_product_sync,
		queue="long",
		timeout=3600,
		job_name=PRODUCT_SYNC_JOB_NAME,
		log_name=log_name,
		mode=mode,
		q=q,
		status=status,
		force=bool(force),
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
	"""Background worker: walk Medusa products and sync each."""
	frappe.set_user("Administrator")
	_assert_enabled()

	update_sync_log(log_name, status="Running", message=f"{mode} product sync started")
	publish(f"Starting {mode} product sync…")

	settings = frappe.get_doc(SETTING_DOCTYPE)
	updated_at_gt = None
	if mode == "Incremental" and settings.get("last_product_sync"):
		updated_at_gt = get_datetime(settings.last_product_sync).isoformat()

	service = ProductService()
	created = updated = skipped = failed = 0
	failed_ids: list[str] = []
	start = process_time()

	try:
		for product in service.iter_products(q=q, status=status, updated_at_gt=updated_at_gt):
			product_id = product.get("id")
			if not product_id:
				continue
			publish(f"Syncing {product_id} — {product.get('title') or ''}", br=False)
			savepoint = f"medusa_prod_{product_id[-12:]}"
			try:
				frappe.db.savepoint(savepoint)
				# Incremental/full without force: still update existing via force=False
				# which still updates fields when mapped.
				if not force and is_synced(product_id) and mode == "Full":
					# Full without force still re-syncs for reconciliation; only skip
					# when explicitly wanting create-only — default is update.
					pass
				# Prefer full product fetch so variants/options are complete.
				result = _sync_one(product_id, force=True, product_payload=None)
				if result["action"] == "created":
					created += 1
					publish(f"✅ Created {product_id} → {result['item_code']}", synced=True)
				elif result["action"] == "updated":
					updated += 1
					publish(f"↻ Updated {product_id} → {result['item_code']}", synced=True)
				else:
					skipped += 1
					publish(f"↷ Skipped {product_id}")
				frappe.db.commit()
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
		update_sync_log(
			log_name,
			status=status_label,
			message=message,
			created=created,
			updated=updated,
			skipped=skipped,
			failed=failed,
			failed_ids=failed_ids,
			summary={
				"created": created,
				"updated": updated,
				"skipped": skipped,
				"failed": failed,
				"failed_ids": failed_ids,
			},
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
		frappe.db.commit()
		publish(f"🎉 {message}", done=True)
		return {
			"status": status_label,
			"created": created,
			"updated": updated,
			"skipped": skipped,
			"failed": failed,
			"failed_ids": failed_ids,
		}
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


def retry_failed_products(log_name: str) -> dict:
	"""Re-run product ids stored on a Sync Log's failed_ids field."""
	frappe.only_for("System Manager")
	log = frappe.get_doc("Medusa Sync Log", log_name)
	ids = [x.strip() for x in (log.failed_ids or "").split(",") if x.strip()]
	if not ids:
		return {"ok": False, "message": "No failed product ids on this log."}

	results = []
	for product_id in ids:
		results.append(import_single_product(product_id, force=True))
	return {"ok": True, "results": results}


def get_product_counts() -> dict:
	"""Counts for the Sync Products dashboard."""
	_assert_enabled()
	service = ProductService()
	page = service.list_products(limit=1, offset=0)
	medusa_count = page.get("count") or 0

	erpnext_count = frappe.db.count("Item", {"variant_of": ["is", "not set"], "has_variants": ["in", [0, 1]]})
	# Templates + simple items only for "synced products"
	synced_count = frappe.db.count(
		"Medusa Item Mapping",
		{"has_variants": ["in", [0, 1]], "medusa_variant_id": ["in", ["", None]]},
	)
	# Include simple products that store empty variant_id
	if synced_count == 0:
		synced_count = frappe.db.sql(
			"""
			select count(*) from `tabMedusa Item Mapping`
			where ifnull(medusa_variant_id, '') = ''
			"""
		)[0][0]

	return {
		"medusaCount": medusa_count,
		"erpnextCount": erpnext_count,
		"syncedCount": synced_count,
	}


def list_medusa_products(
	offset: int = 0, limit: int = 20, q: str | None = None, status: str | None = None
) -> dict:
	"""Paginated Medusa product list with sync status for the desk page."""
	frappe.only_for("System Manager")
	_assert_enabled()
	offset = int(offset or 0)
	limit = min(int(limit or 20), 100)
	service = ProductService()
	page = service.list_products(limit=limit, offset=offset, q=q, status=status)
	products = []
	for product in page.get("products") or []:
		pid = product.get("id")
		products.append(
			{
				"id": pid,
				"title": product.get("title"),
				"status": product.get("status"),
				"handle": product.get("handle"),
				"thumbnail": product.get("thumbnail"),
				"sku": _primary_sku(product),
				"synced": is_synced(pid) if pid else False,
				"updated_at": product.get("updated_at"),
			}
		)
	return {
		"products": products,
		"count": page.get("count", 0),
		"limit": limit,
		"offset": offset,
		"nextOffset": offset + limit if offset + limit < (page.get("count") or 0) else None,
		"prevOffset": max(offset - limit, 0) if offset > 0 else None,
	}


def health_check() -> dict:
	"""Connection + mapping health for the Sync Products page."""
	from medusa_connector.medusa.client import MedusaClient

	connection = {"ok": False, "message": ""}
	try:
		client = MedusaClient()
		client.health_check()
		connection = {"ok": True, "message": "Connected"}
	except Exception as exc:
		connection = {"ok": False, "message": str(exc)}

	mapping = get_mapping_health()
	return {"connection": connection, "mapping": mapping}


def is_product_sync_running() -> bool:
	return _job_running()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _sync_one(product_id: str, *, force: bool = True, product_payload: dict | None = None) -> dict:
	from medusa_connector.utils.sync_guard import inbound_sync

	service = ProductService()
	product = product_payload or service.get_product(product_id)
	if not product or not product.get("id"):
		raise frappe.ValidationError(f"Product {product_id} not found in Medusa")
	mapped = ProductMapper().map(product)
	sync = ProductSync()
	# inbound_sync also wraps ProductSync.sync; double-wrap is harmless.
	with inbound_sync():
		return sync.sync(mapped, force=force)


def _primary_sku(product: dict) -> str:
	variants = product.get("variants") or []
	skus = [v.get("sku") for v in variants if v.get("sku")]
	return ", ".join(skus[:5])


def _assert_enabled() -> None:
	if not frappe.db.get_single_value(SETTING_DOCTYPE, "enabled"):
		frappe.throw("Medusa Connector is disabled. Enable it in Medusa Settings.")


def _job_running() -> bool:
	jobs = frappe.get_all(
		"RQ Job",
		filters={"status": ["in", ["queued", "started"]]},
		fields=["job_name"],
		limit=50,
	)
	return any(j.job_name == PRODUCT_SYNC_JOB_NAME for j in jobs)


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
