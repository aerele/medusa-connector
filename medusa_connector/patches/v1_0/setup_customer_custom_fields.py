# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from medusa_connector.setup import setup_custom_fields


def execute() -> None:
	setup_custom_fields(update=True)
