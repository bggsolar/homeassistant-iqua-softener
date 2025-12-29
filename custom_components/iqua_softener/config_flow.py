from __future__ import annotations

import re
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, CONF_USERNAME, CONF_PASSWORD, CONF_DEVICE_UUID

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors = {}

        if user_input is not None:
            uuid = user_input[CONF_DEVICE_UUID].strip()

            if not UUID_RE.match(uuid):
                errors[CONF_DEVICE_UUID] = "invalid_uuid"
            else:
                return self.async_create_entry(
                    title=f"iQua {uuid}",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_DEVICE_UUID: uuid,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,   # email
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_DEVICE_UUID): str,  # UUID from /devices/<UUID>
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)