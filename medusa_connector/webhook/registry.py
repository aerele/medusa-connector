# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

_HANDLERS: dict[str, type] = {}
_LOADED = False

_HANDLER_MODULES = (
	"medusa_connector.product.webhook",
	"medusa_connector.order.webhook",
)


def register(*events: str):
	def decorator(cls):
		for event in events:
			if not isinstance(event, str) or not event:
				raise ValueError("Webhook event name must be a non-empty string")

			if event in _HANDLERS:
				raise ValueError(
					f"Webhook event '{event}' is already registered by {_HANDLERS[event].__name__}"
				)

			_HANDLERS[event] = cls

		return cls

	return decorator


def _ensure_loaded() -> None:
	global _LOADED

	if _LOADED:
		return

	for module in _HANDLER_MODULES:
		__import__(module)

	_LOADED = True


def get_handler(event_name: str):
	_ensure_loaded()
	handler_class = _HANDLERS.get(event_name)
	return handler_class() if handler_class else None
