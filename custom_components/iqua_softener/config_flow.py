from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_DEVICE_UUID

_LOGGER = logging.getLogger(__name__)


STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_DEVICE_UUID): str,  # UUID, not serial
    }
)

STEP_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_DEVICE_UUID): str,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            device_uuid = user_input[CONF_DEVICE_UUID].strip()

            # Make entry unique per device_uuid
            await self.async_set_unique_id(device_uuid.lower())
            self._abort_if_unique_id_configured()

            title = f"iQua {device_uuid}"

            return self.async_create_entry(
                title=title,
                data={
                    CONF_EMAIL: email,
                    CONF_PASSWORD: password,
                    CONF_DEVICE_UUID: device_uuid,
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_EMAIL: user_input[CONF_EMAIL].strip(),
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_DEVICE_UUID: user_input[CONF_DEVICE_UUID].strip(),
                },
            )

        merged = dict(self._entry.data)
        merged.update(self._entry.options or {})

        defaults = {
            CONF_EMAIL: merged.get(CONF_EMAIL, ""),
            CONF_PASSWORD: merged.get(CONF_PASSWORD, ""),
            CONF_DEVICE_UUID: merged.get(CONF_DEVICE_UUID, ""),
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=defaults[CONF_EMAIL]): str,
                    vol.Required(CONF_PASSWORD, default=defaults[CONF_PASSWORD]): str,
                    vol.Required(CONF_DEVICE_UUID, default=defaults[CONF_DEVICE_UUID]): str,
                }
            ),
            errors=errors,
        )