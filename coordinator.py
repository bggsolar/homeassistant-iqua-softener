from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from iqua_softener import IquaSoftener, IquaSoftenerData, IquaSoftenerException

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=10)


class IquaSoftenerCoordinator(DataUpdateCoordinator[IquaSoftenerData]):
    def __init__(self, hass: HomeAssistant, iqua_softener: IquaSoftener) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Iqua Softener",
            update_interval=UPDATE_INTERVAL,
        )
        self._iqua_softener = iqua_softener

    async def _async_update_data(self) -> IquaSoftenerData:
        try:
            # Library is sync -> run in executor
            return await self.hass.async_add_executor_job(self._iqua_softener.get_data)
        except IquaSoftenerException as err:
            raise UpdateFailed(f"Get data failed (IquaSoftenerException): {err}") from err
        except TimeoutError as err:
            raise UpdateFailed(f"Get data failed (TimeoutError): {err}") from err
        except OSError as err:
            # DNS/socket/connection errors from sync libs often bubble as OSError
            raise UpdateFailed(f"Get data failed (OSError): {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Get data failed ({type(err).__name__}): {err}") from err