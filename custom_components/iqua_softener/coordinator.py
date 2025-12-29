from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

import requests

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=10)

DEFAULT_API_BASE_URL = "https://api.myiquaapp.com/v1"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Home Assistant iQua Softener)"


class IquaSoftenerCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """
    Fetches iQua device debug information and exposes a normalized structure:
      coordinator.data = {
        "kv": { "<group>.<key>": "<value>", ... },
        "tables": { "<table_key>": { "title": ..., "column_titles": [...], "rows": [...] }, ... }
      }

    The key improvement is namespacing kv keys by group to avoid collisions
    (e.g. "regenerations.total_rock_removed" vs "rock_removed.total_rock_removed").
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        email: str,
        password: str,
        device_uuid: str,
        api_base_url: str = DEFAULT_API_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="iQua Softener",
            update_interval=UPDATE_INTERVAL,
        )
        self._email = email
        self._password = password
        self._device_uuid = device_uuid
        self._api_base_url = api_base_url.rstrip("/")
        self._user_agent = user_agent

        # Tokens are managed internally (access + refresh)
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None

    # -----------------------
    # HTTP helpers
    # -----------------------
    def _headers(self, *, with_auth: bool = True) -> Dict[str, str]:
        h = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        if with_auth and self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    def _url(self, path: str) -> str:
        if path.startswith("/"):
            path = path[1:]
        return f"{self._api_base_url}/{path}"

    def _login(self, session: requests.Session) -> None:
        """
        POST /auth/login
        Response (per your capture):
          { access_token, refresh_token, ... }
        """
        url = self._url("auth/login")
        payload = {"email": self._email, "password": self._password}
        resp = session.post(url, json=payload, headers=self._headers(with_auth=False), timeout=20)

        if resp.status_code != 200:
            raise UpdateFailed(
                f"Login failed: HTTP {resp.status_code} for {url}: {resp.text[:300]}"
            )

        data = resp.json()
        access = data.get("access_token")
        refresh = data.get("refresh_token")

        if not access:
            raise UpdateFailed("Login failed: missing access_token in response")

        self._access_token = str(access)
        self._refresh_token = str(refresh) if refresh else None

    def _refresh(self, session: requests.Session) -> None:
        """
        If the API supports refresh endpoint, implement here.
        If unknown, fallback to full login.
        """
        # Many APIs use /auth/refresh with refresh_token. If your API does too,
        # you can enable this by adjusting endpoint + payload.
        #
        # For now: fallback to full login which we know works.
        self._login(session)

    def _get_device_debug(self, session: requests.Session) -> Dict[str, Any]:
        url = self._url(f"devices/{self._device_uuid}/debug")
        resp = session.get(url, headers=self._headers(with_auth=True), timeout=30)

        # Typical case: token expired
        if resp.status_code in (401, 403):
            _LOGGER.debug("Access token rejected (HTTP %s), re-authenticating", resp.status_code)
            self._refresh(session)
            resp = session.get(url, headers=self._headers(with_auth=True), timeout=30)

        if resp.status_code != 200:
            raise UpdateFailed(
                f"Invalid status ({resp.status_code}) for data request: {url}; body={resp.text[:300]}"
            )

        return resp.json()

    # -----------------------
    # Parsing
    # -----------------------
    @staticmethod
    def _parse_debug_json(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalizes the debug JSON into:
          kv: namespaced keys -> value
          tables: table_key -> {title, column_titles, rows}
        """
        kv: Dict[str, Any] = {}
        tables: Dict[str, Dict[str, Any]] = {}

        groups = payload.get("groups", [])
        if not isinstance(groups, list):
            return {"kv": kv, "tables": tables}

        for group in groups:
            if not isinstance(group, dict):
                continue
            group_key = group.get("key")
            if not group_key:
                continue

            items = group.get("items", [])
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue

                item_key = item.get("key")
                item_type = item.get("type")
                if not item_key or not item_type:
                    continue

                # ---- KV item ----
                if item_type == "kv":
                    item_kv = item.get("item_kv") or {}
                    if not isinstance(item_kv, dict):
                        continue
                    value = item_kv.get("value")
                    # ✅ namespaced to avoid collisions
                    kv[f"{group_key}.{item_key}"] = value

                # ---- TABLE item ----
                elif item_type == "table":
                    item_table = item.get("item_table") or {}
                    if not isinstance(item_table, dict):
                        continue
                    tables[item_key] = {
                        "title": item_table.get("title"),
                        "column_titles": item_table.get("column_titles") or [],
                        "rows": item_table.get("rows") or [],
                    }

        return {"kv": kv, "tables": tables}

    # -----------------------
    # HA update loop
    # -----------------------
    async def _async_update_data(self) -> Dict[str, Any]:
        def _work() -> Dict[str, Any]:
            with requests.Session() as session:
                # Ensure we are logged in
                if not self._access_token:
                    self._login(session)

                payload = self._get_device_debug(session)
                return self._parse_debug_json(payload)

        try:
            return await self.hass.async_add_executor_job(_work)
        except UpdateFailed:
            raise
        except requests.exceptions.RequestException as err:
            raise UpdateFailed(f"Request error: {type(err).__name__}: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {type(err).__name__}: {err}") from err