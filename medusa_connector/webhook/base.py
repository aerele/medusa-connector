# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import abc
from dataclasses import dataclass, field

import frappe

from medusa_connector.medusa.client import execute_rest
from medusa_connector.medusa.exceptions import MedusaConnectorError


@dataclass
class MedusaEvent:
	"""Normalised view of one delivered Medusa webhook.

	Handlers receive this instead of the raw request so they are decoupled from
	transport details (headers, signatures, HTTP). ``event_id`` is the connector's
	idempotency key; ``entity_id`` is the id of the affected Medusa resource.
	"""

	name: str
	event_id: str
	data: dict = field(default_factory=dict)
	raw: dict = field(default_factory=dict)
	log_name: str | None = None

	@property
	def entity_id(self) -> str | None:
		return self.data.get("id") or self.raw.get("id") or (self.raw.get("data") or {}).get("id")


class BaseHandler(abc.ABC):
	"""Template-method base for every event handler.

	The flow is fixed — optionally fetch the full entity from Medusa, then hand
	it to :meth:`process` — while subclasses supply only the parts that vary
	(which resource to fetch, what to do with it). This keeps each handler small,
	single-responsibility, and independently testable.

	Handlers MUST be idempotent: Medusa delivers at-least-once and the connector
	may retry, so processing the same ``event_id`` twice must not double-apply.
	"""

	#: events bound by the ``@register`` decorator (set on the class at import).
	events: tuple[str, ...] = ()

	#: Medusa admin resource to hydrate, e.g. ``"orders"`` → ``/admin/orders/{id}``.
	#: ``None`` means "don't fetch"; the raw payload data is used as-is.
	resource: str | None = None

	def handle(self, event: MedusaEvent) -> str | None:
		"""Entry point invoked by the dispatcher. Returns a short outcome summary."""
		entity = self._load(event)
		return self.process(event, entity)

	def _load(self, event: MedusaEvent) -> dict:
		"""Hydrate the full entity from Medusa; fall back to the thin payload.

		Webhook payloads carry only an id, so business logic needs a callback to
		the admin API. A fetch failure is non-fatal — we degrade to the payload
		data so the handler can still record the event.
		"""
		if not self.resource or not event.entity_id:
			return event.data
		try:
			resp = execute_rest("GET", f"/admin/{self.resource}/{event.entity_id}")
		except MedusaConnectorError as exc:
			frappe.logger("medusa_connector").warning(
				f"Could not hydrate {self.resource}/{event.entity_id}: {exc}"
			)
			raise
		if isinstance(resp, dict):
			# Medusa wraps single resources, e.g. {"order": {...}}.
			return resp.get(self.resource.rstrip("s"), resp)
		return event.data

	@abc.abstractmethod
	def process(self, event: MedusaEvent, entity: dict) -> str | None:
		"""Apply the event to ERPNext. Must be idempotent. Returns an outcome note."""
		raise NotImplementedError
