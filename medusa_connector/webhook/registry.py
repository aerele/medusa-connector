# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Central event → handler registry.

This is the single source of truth that both ends of the connector read:

* the **sync service** asks :func:`registered_events` for the set of Medusa
  events it must register as webhooks, and
* the **dispatcher** asks :func:`get_handler` which handler processes an
  incoming event.

Adding support for a new Medusa event is therefore a one-file change: drop a
handler class into ``webhook/handlers/`` decorated with ``@register("event.name")``
— no edits to the receiver, dispatcher, or settings are required (Open/Closed).
"""

_HANDLERS: dict[str, type] = {}
_LOADED = False


def register(*events: str):
	"""Class decorator binding a handler class to one or more Medusa events."""

	def decorator(cls):
		for event in events:
			_HANDLERS[event] = cls
		cls.events = tuple(events)
		return cls

	return decorator


def _ensure_loaded() -> None:
	"""Import the handlers package once so decorators populate the registry."""
	global _LOADED
	if not _LOADED:
		import medusa_connector.webhook.handlers  # (import triggers registration)

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
