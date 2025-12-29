from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

import requests

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=10)

# Web endpoints (discovered from your browser devtools)
DEFAULT_API_BASE_URL = "https://api.myiquaapp.com/v1"
DEFAULT_APP_ORIGIN = "https://app.myiquaapp.com"
DEFAULT_USER_AGENT = "Mozilla/5.0 (HomeAssistant iQuaSoftener)"


# -----------------------------
# Canonical mapping
# (group_key, item_key) -> canonical kv key
# -----------------------------
CANONICAL_KV_MAP: Dict[Tuple[str, str], str] = {
    # ---- Customer / metadata ----
    ("customer", "time_message_received"): "customer.time_message_received",
    ("customer", "customer_full_name"): "customer.full_name",
    ("customer", "device_id"): "customer.device_id",

    # ---- Manufacturing ----
    ("manufacturing_information", "base_software_version"): "manufacturing_information.base_software_version",
    ("manufacturing_information", "model"): "manufacturing_information.model",
    ("manufacturing_information", "model_code"): "manufacturing_information.model_code",
    ("manufacturing_information", "pwa"): "manufacturing_information.pwa",
    ("manufacturing_information", "build_year"): "manufacturing_information.build_year",
    ("manufacturing_information", "build_day"): "manufacturing_information.build_day",
    ("manufacturing_information", "build_seq"): "manufacturing_information.build_seq",
    ("manufacturing_information", "build_fixture"): "manufacturing_information.build_fixture",
    ("manufacturing_information", "wifi_module_version"): "manufacturing_information.wifi_module_version",

    # ---- Configuration ----
    ("configuration_information", "system_type"): "configuration.system_type",
    ("configuration_information", "resin_load"): "configuration.resin_load_liters",
    ("configuration_information", "refill_rate"): "configuration.refill_rate_lpm",
    ("configuration_information", "turbine_revolutions"): "configuration.turbine_revs_per_liter",
    ("configuration_information", "valve_type"): "configuration.valve_type",
    ("configuration_information", "efficiency_mode"): "configuration.efficiency_mode",

    # ---- Program settings ----
    ("program_settings", "controller_time"): "program.controller_time",
    ("program_settings", "hardness"): "program.hardness",
    ("program_settings", "max_days"): "program.max_days",
    ("program_settings", "recharge_time"): "program.recharge_time",
    ("program_settings", "second_backwash_time"): "program.second_backwash_time",
    ("program_settings", "backwash_time"): "program.backwash_time",
    ("program_settings", "fast_rinse_time"): "program.fast_rinse_time",
    ("program_settings", "regen_time_remaining"): "program.regen_time_remaining",
    ("program_settings", "rinse_type"): "program.rinse_type",
    ("program_settings", "volume_units"): "program.volume_units",
    ("program_settings", "hardness_units"): "program.hardness_units",
    ("program_settings", "97_percent_feature"): "program.feature_97_percent",
    ("program_settings", "salt_type"): "program.salt_type",
    ("program_settings", "weight_units"): "program.weight_units",
    ("program_settings", "time_format"): "program.time_format",

    # ---- Capacity ----
    ("capacity", "capacity_remaining_percent"): "capacity.capacity_remaining_percent",
    ("capacity", "average_capacity_remaining_at_regen"): "capacity.average_capacity_remaining_at_regen_percent",

    # ---- Water usage ----
    ("water_usage", "treated_water"): "water_usage.treated_water",
    ("water_usage", "untreated_water"): "water_usage.untreated_water",
    ("water_usage", "water_today"): "water_usage.water_today",
    ("water_usage", "average_daily_use"): "water_usage.average_daily_use",
    ("water_usage", "water_totalizer"): "water_usage.water_totalizer",
    ("water_usage", "treated_water_left"): "water_usage.treated_water_left",
    ("water_usage", "current_flow_rate"): "water_usage.current_flow_rate",
    ("water_usage", "peak_flow"): "water_usage.peak_flow",

    # ---- Salt usage ----
    ("salt_usage", "salt_total"): "salt_usage.salt_total",
    ("salt_usage", "total_salt_efficiency"): "salt_usage.total_salt_efficiency",
    ("salt_usage", "salt_monitor_enum"): "salt_usage.salt_monitor_enum",
    ("salt_usage", "salt_monitor_level"): "salt_usage.salt_monitor_level",
    ("salt_usage", "out_of_salt_days"): "salt_usage.out_of_salt_days",
    ("salt_usage", "average_salt_dose_per_recharge"): "salt_usage.average_salt_dose_per_recharge",

    # ---- Rock removed ----
    ("rock_removed", "total_rock_removed"): "rock_removed.total_rock_removed",
    ("rock_removed", "daily_average_rock_removed"): "rock_removed.daily_average_rock_removed",
    ("rock_removed", "since_regen_rock_removed"): "rock_removed.since_regen_rock_removed",

    # ---- Regenerations (API bug!) ----
    # In the API JSON, this item has key "total_rock_removed" but label "Time in Operation (Days)"
    ("regenerations", "total_rock_removed"): "regenerations.time_in_operation_days",
    ("regenerations", "total_regens"): "regenerations.total_regens",
    ("regenerations", "manual_regens"): "regenerations.manual_regens",
    ("regenerations", "second_backwash_cycles"): "regenerations.second_backwash_cycles",
    ("regenerations", "time_since_last_recharge"): "regenerations.time_since_last_recharge_days",
    ("regenerations", "average_days_between_recharge"): "regenerations.average_days_between_recharge_days",

    # ---- Power outages ----
    ("power_outages", "total_power_outages"): "power_outages.total_power_outages",
    ("power_outages", "total_times_power_lost"): "power_outages.total_times_power_lost",
    ("power_outages", "days_since_last_time_loss"): "power_outages.days_since_last_time_loss",
    ("power_outages", "longest_recorded_outage"): "power_outages.longest_recorded_outage",

    # ---- Functional check ----
    ("functional_check", "water_meter_sensor"): "functional_check.water_meter_sensor",
    ("functional_check", "computer_board"): "functional_check.computer_board",
    ("functional_check", "cord_power_supply"): "functional_check.cord_power_supply",

    # ---- Misc ----
    ("miscellaneous", "second_output"): "miscellaneous.second_output",
    ("miscellaneous", "regeneration_enabled"): "miscellaneous.regeneration_enabled",
    ("miscellaneous", "lockout_status"): "miscellaneous.lockout_status",
}


def _normalize_group_key(group_key: str) -> str:
    """API group keys are already stable; keep as-is but be defensive."""
    return str(group_key or "").strip().lower()


def _normalize_item_key(item_key: str) -> str:
    return str(item_key or "").strip().lower()


def _extract_item_value(item: Dict[str, Any]) -> Any:
    """
    Each group item can be:
      - type: "kv" -> item_kv: {label, value}
      - type: "table" -> item_table: {title, column_titles, rows}
    """
    if item.get("type") == "kv":
        kv = item.get("item_kv") or {}
        if isinstance(kv, dict):
            return kv.get("value")
        return None
    return None


def _parse_tables(groups: list[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Collect 'table' items into:
      tables[<table_key>] = {"column_titles": [...], "rows": [...]}
    """
    tables: Dict[str, Any] = {}
    for g in groups:
        if not isinstance(g, dict):
            continue
        gkey = _normalize_group_key(g.get("key", ""))
        items = g.get("items", [])
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "table":
                continue

            table_key = _normalize_item_key(item.get("key", ""))
            table = item.get("item_table") or {}
            if not isinstance(table, dict):
                continue

            # We store tables by their item.key (not group), because your sensor expects "daily_water_usage_patterns"
            # But still: key collision is unlikely for tables; if it happens we can namespace later.
            tables[table_key] = {
                "title": table.get("title"),
                "column_titles": table.get("column_titles", []),
                "rows": table.get("rows", []),
                "group": gkey,
            }

    return tables


class IquaSoftenerCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """
    Coordinator fetches:
      - POST /auth/login (email+password) -> access_token
      - GET  /devices/<uuid>/debug       -> groups[]
    And normalizes everything into:
      data = {"kv": {canonical_key: value, ...}, "tables": {...}}
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
        app_origin: str = DEFAULT_APP_ORIGIN,
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
        self._app_origin = app_origin

        self._access_token: Optional[str] = None
        self._session: Optional[requests.Session] = None

    # -----------------------------
    # HTTP helpers (sync; executed in executor)
    # -----------------------------
    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _headers(self, *, with_auth: bool = True) -> Dict[str, str]:
        h: Dict[str, str] = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
            "Origin": self._app_origin,
            "Referer": self._app_origin + "/",
        }
        if with_auth and self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    def _url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self._api_base_url}/{path}"

    def _login(self) -> None:
        """
        POST /auth/login
        Body: {"email": "...", "password": "..."}
        Response: {"access_token": "...", "refresh_token": "...", ...}
        """
        sess = self._get_session()
        r = sess.post(
            self._url("auth/login"),
            json={"email": self._email, "password": self._password},
            headers=self._headers(with_auth=False),
            timeout=20,
        )
        if r.status_code == 401:
            raise UpdateFailed("Authentication failed (401). Check email/password.")
        if r.status_code != 200:
            raise UpdateFailed(f"Login failed: HTTP {r.status_code} ({r.text[:200]})")

        j = r.json()
        token = j.get("access_token")
        if not token:
            raise UpdateFailed("Login response missing access_token.")
        self._access_token = token

    def _fetch_debug(self) -> Dict[str, Any]:
        """
        GET /devices/<uuid>/debug  (UUID, not serial!)
        """
        sess = self._get_session()
        r = sess.get(
            self._url(f"devices/{self._device_uuid}/debug"),
            headers=self._headers(with_auth=True),
            timeout=20,
        )

        # Token might be expired/invalid
        if r.status_code in (401, 403):
            self._access_token = None
            self._login()
            r = sess.get(
                self._url(f"devices/{self._device_uuid}/debug"),
                headers=self._headers(with_auth=True),
                timeout=20,
            )

        if r.status_code != 200:
            # 422 indicates wrong identifier or missing permission
            raise UpdateFailed(
                f"Debug request failed: HTTP {r.status_code} for {r.url}"
            )

        return r.json()

    def _parse_debug_json(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        groups = payload.get("groups", [])
        if not isinstance(groups, list):
            raise UpdateFailed("Unexpected debug payload: 'groups' is not a list")

        kv: Dict[str, Any] = {}
        tables = _parse_tables(groups)

        for g in groups:
            if not isinstance(g, dict):
                continue

            gkey = _normalize_group_key(g.get("key", ""))
            items = g.get("items", [])
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue

                if item.get("type") != "kv":
                    continue

                raw_item_key = _normalize_item_key(item.get("key", ""))
                value = _extract_item_value(item)

                # Canonicalize key: ensure NO collisions
                canonical = CANONICAL_KV_MAP.get((gkey, raw_item_key))
                if canonical is None:
                    # Fallback is still namespaced -> collision-safe
                    canonical = f"{gkey}.{raw_item_key}"

                kv[canonical] = value

        return {"kv": kv, "tables": tables}

    # -----------------------------
    # HA coordinator entrypoint
    # -----------------------------
    async def _async_update_data(self) -> Dict[str, Any]:
        try:
            return await self.hass.async_add_executor_job(self._sync_update)
        except UpdateFailed:
            raise
        except requests.exceptions.RequestException as err:
            raise UpdateFailed(f"Request error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {type(err).__name__}: {err}") from err

    def _sync_update(self) -> Dict[str, Any]:
        # Ensure we are logged in
        if not self._access_token:
            self._login()

        payload = self._fetch_debug()
        data = self._parse_debug_json(payload)

        # Extra defensive: make sure we always have dicts
        if not isinstance(data, dict) or "kv" not in data:
            raise UpdateFailed("Parsed data invalid")

        return data