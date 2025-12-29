from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from iqua_softener import IquaSoftener, IquaSoftenerException

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=10)


class IquaSoftenerCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, client: IquaSoftener) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Iqua Softener",
            update_interval=UPDATE_INTERVAL,
        )
        self._client = client

    async def _async_update_data(self) -> Dict[str, Any]:
        try:
            debug = await self.hass.async_add_executor_job(self._client.get_debug)
            parsed = await self.hass.async_add_executor_job(self._client.parse_debug, debug)
            return parsed
        except IquaSoftenerException as err:
            raise UpdateFailed(f"Get data failed (IquaSoftenerException): {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Get data failed ({type(err).__name__}): {err}") from err