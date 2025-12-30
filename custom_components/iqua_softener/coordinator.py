# coordinator.py
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

import requests

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=15)

DEFAULT_API_BASE_URL = "https://api.myiquaapp.com/v1"
DEFAULT_APP_ORIGIN = "https://app.myiquaapp.com"
DEFAULT_USER_AGENT = "Mozilla/5.0 (HomeAssistant iQuaSoftener)"

CANONICAL_KV_MAP: Dict[Tuple[str, str], str] = {
    ("customer", "time_message_received"): "customer.time_message_received",
}

def _normalize(v: str | None) -> str:
    return str(v or "").strip().lower()

class IquaSoftenerCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, *, email: str, password: str, device_uuid: str) -> None:
        super().__init__(hass, _LOGGER, name="iQua Softener", update_interval=UPDATE_INTERVAL)
        self._email = email
        self._password = password
        self._device_uuid = device_uuid
        self._access_token: Optional[str] = None
        self._session: Optional[requests.Session] = None

    def _get_session(self) -> requests.Session:
        if not self._session:
            self._session = requests.Session()
        return self._session

    def _login(self) -> None:
        r = self._get_session().post(
            f"{DEFAULT_API_BASE_URL}/auth/login",
            json={"email": self._email, "password": self._password},
            timeout=20,
        )
        if r.status_code != 200:
            raise UpdateFailed("Login failed")
        self._access_token = r.json().get("access_token")

    def _sync_update(self) -> Dict[str, Any]:
        if not self._access_token:
            self._login()

        r = self._get_session().get(
            f"{DEFAULT_API_BASE_URL}/devices/{self._device_uuid}/debug",
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=20,
        )
        if r.status_code != 200:
            raise UpdateFailed("Debug fetch failed")

        payload = r.json()
        kv: Dict[str, Any] = {}

        for g in payload.get("groups", []):
            gk = _normalize(g.get("key"))
            for item in g.get("items", []):
                if item.get("type") != "kv":
                    continue
                ik = _normalize(item.get("key"))
                canonical = CANONICAL_KV_MAP.get((gk, ik))
                if canonical:
                    kv[canonical] = item.get("item_kv", {}).get("value")

        return {"kv": kv}

    async def _async_update_data(self) -> Dict[str, Any]:
        return await self.hass.async_add_executor_job(self._sync_update)
