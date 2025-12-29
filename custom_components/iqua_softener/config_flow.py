from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_DEVICE_UUID,
)


class IquaSoftenerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            device_uuid = (user_input.get(CONF_DEVICE_UUID) or "").strip()

            # prevent duplicate entries for same device uuid
            await self.async_set_unique_id(device_uuid.lower())
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"iQua {device_uuid}",
                data={
                    CONF_EMAIL: user_input[CONF_EMAIL].strip(),
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_DEVICE_UUID: device_uuid,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_DEVICE_UUID): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )