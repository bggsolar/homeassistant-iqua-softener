from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_DEVICE_UUID,
    CONF_HOUSE_WATERMETER_ENTITY,
    CONF_HOUSE_WATERMETER_UNIT_MODE,
    CONF_HOUSE_WATERMETER_FACTOR,
    HOUSE_UNIT_MODE_AUTO,
    HOUSE_UNIT_MODE_M3,
    HOUSE_UNIT_MODE_L,
    HOUSE_UNIT_MODE_FACTOR,
)

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


def _opt_float(value: Any) -> float | None:
    """Parse optional float from user input.

    Returns None for empty strings / None.
    Raises vol.Invalid for non-numeric values.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except Exception as err:
        raise vol.Invalid("not_a_number") from err


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

    def _build_schema(self, defaults: dict[str, Any]) -> vol.Schema:
        """Build the options form schema."""
        return vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=defaults.get(CONF_EMAIL, "")): str,
                vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): str,
                vol.Required(CONF_DEVICE_UUID, default=defaults.get(CONF_DEVICE_UUID, "")): str,

                # Optional enrichment: house watermeter (for delta + hardness calculations)
                vol.Optional(
                    CONF_HOUSE_WATERMETER_ENTITY,
                    default=defaults.get(CONF_HOUSE_WATERMETER_ENTITY, ""),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),

                vol.Optional(
                    default=defaults.get(CONF_REGEN_SELF_CONSUMPTION_L, DEFAULT_REGEN_SELF_CONSUMPTION_L),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_HOUSE_WATERMETER_UNIT_MODE,
                    default=defaults.get(CONF_HOUSE_WATERMETER_UNIT_MODE, HOUSE_UNIT_MODE_AUTO),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            HOUSE_UNIT_MODE_AUTO,
                            HOUSE_UNIT_MODE_M3,
                            HOUSE_UNIT_MODE_L,
                            HOUSE_UNIT_MODE_FACTOR,
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_HOUSE_WATERMETER_FACTOR,
                    default=defaults.get(CONF_HOUSE_WATERMETER_FACTOR, ""),
                ): str,

                # Hardness inputs (°dH) are provided as Number entities (configuration category)
                # after setup. This keeps them editable without going through the options UI.
            }
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # Normalize optional fields
            house_entity = str(user_input.get(CONF_HOUSE_WATERMETER_ENTITY, "") or "").strip()
            unit_mode = str(user_input.get(CONF_HOUSE_WATERMETER_UNIT_MODE, HOUSE_UNIT_MODE_AUTO) or HOUSE_UNIT_MODE_AUTO)
            factor_raw = user_input.get(CONF_HOUSE_WATERMETER_FACTOR)

            # Validate factor when mode=factor (optional otherwise)
            factor: float | None = _opt_float(factor_raw)
            if unit_mode == HOUSE_UNIT_MODE_FACTOR and (factor is None or factor <= 0):
                errors[CONF_HOUSE_WATERMETER_FACTOR] = "invalid_house_factor"

            # Hardness inputs are configured via Number entities after setup.
            if errors:
                # Keep user-entered values in the form
                schema = self._build_schema(
                    {
                        CONF_EMAIL: user_input.get(CONF_EMAIL, ""),
                        CONF_PASSWORD: user_input.get(CONF_PASSWORD, ""),
                        CONF_DEVICE_UUID: user_input.get(CONF_DEVICE_UUID, ""),
                        CONF_HOUSE_WATERMETER_ENTITY: house_entity,
                        CONF_HOUSE_WATERMETER_UNIT_MODE: unit_mode,
                        CONF_HOUSE_WATERMETER_FACTOR: factor_raw or "",
                    }
                )
                return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

            data: dict[str, Any] = {
                CONF_EMAIL: user_input[CONF_EMAIL].strip(),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_DEVICE_UUID: user_input[CONF_DEVICE_UUID].strip(),
                CONF_HOUSE_WATERMETER_ENTITY: house_entity,
                CONF_HOUSE_WATERMETER_UNIT_MODE: unit_mode,
            }

            # Store factor only if provided (keeps "missing" semantics)
            if factor is not None:
                data[CONF_HOUSE_WATERMETER_FACTOR] = factor
            else:
                data[CONF_HOUSE_WATERMETER_FACTOR] = ""

            return self.async_create_entry(title="", data=data)

        merged = dict(self._entry.data)
        merged.update(self._entry.options or {})

        defaults = {
            CONF_EMAIL: merged.get(CONF_EMAIL, ""),
            CONF_PASSWORD: merged.get(CONF_PASSWORD, ""),
            CONF_DEVICE_UUID: merged.get(CONF_DEVICE_UUID, ""),
            CONF_HOUSE_WATERMETER_ENTITY: merged.get(CONF_HOUSE_WATERMETER_ENTITY) or "",
            CONF_HOUSE_WATERMETER_UNIT_MODE: merged.get(CONF_HOUSE_WATERMETER_UNIT_MODE) or HOUSE_UNIT_MODE_AUTO,
            CONF_HOUSE_WATERMETER_FACTOR: merged.get(CONF_HOUSE_WATERMETER_FACTOR) or "",
        }

        schema = self._build_schema(defaults)

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
