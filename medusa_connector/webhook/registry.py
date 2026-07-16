# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Central event → handler registry.

Single source of truth for:

* **webhook sync** — :func:`registered_events` (which Medusa events to subscribe)
* **dispatcher** — :func:`get_handler` (which handler processes a delivery)

Handlers register via ``@register`` from domain modules:

* ``product.webhook`` — real product import
* ``product.inventory_webhook`` — inventory item metadata
* ``customer.webhook`` — Medusa → ERPNext Customer / Contact / Address
* ``order.webhook`` — order lifecycle
* ``webhook.stubs`` — remaining catalog / master-data stubs
"""

from __future__ import annotations

_HANDLERS: dict[str, type] = {}
_LOADED = False

# Domain modules that call ``@register`` on import.
# Real domain handlers must load before stubs that might share event names.
_HANDLER_MODULES = (
	"medusa_connector.product.webhook",
	"medusa_connector.product.inventory_webhook",
	"medusa_connector.customer.webhook",
	"medusa_connector.order.webhook",
	"medusa_connector.webhook.stubs",
)


def register(*events: str):
	"""Class decorator binding a handler class to one or more Medusa events."""

	def decorator(cls):
		for event in events:
			_HANDLERS[event] = cls
		cls.events = tuple(events)
		return cls

	return decorator


def _ensure_loaded() -> None:
	"""Import handler modules once so decorators populate the registry."""
	global _LOADED
	if _LOADED:
		return
	for path in _HANDLER_MODULES:
		__import__(path)
	_LOADED = True


def get_handler(event_name: str):
	"""Return an instantiated handler for ``event_name`` or ``None`` if unmapped."""
	_ensure_loaded()
	cls = _HANDLERS.get(event_name)
	return cls() if cls else None


def registered_events() -> list[str]:
	"""Return the sorted list of events the connector knows how to handle."""
	_ensure_loaded()
	return sorted(_HANDLERS)
