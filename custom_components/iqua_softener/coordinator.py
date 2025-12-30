from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

import requests

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

# Polling – gewünscht: 15 Minuten
UPDATE_INTERVAL = timedelta(minutes=5)

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
    # In some debug payloads this item key is wrong; we canonicalize it:
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
    return str(group_key or "").strip().lower()


def _normalize_item_key(item_key: str) -> str:
    return str(item_key or "").strip().lower()


def _extract_kv_label_and_value(item: Dict[str, Any]) -> Tuple[Optional[str], Any]:
    """
    For type: "kv", returns (label, value).
    """
    if item.get("type") != "kv":
        return None, None
    kv = item.get("item_kv") or {}
    if not isinstance(kv, dict):
        return None, None
    label = kv.get("label")
    value = kv.get("value")
    return (str(label).strip() if label is not None else None), value


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

            tables[table_key] = {
                "title": table.get("title"),
                "column_titles": table.get("column_titles", []),
                "rows": table.get("rows", []),
                "group": gkey,
            }

    return tables


def _merge_detail_properties_into_kv(detail: Dict[str, Any], kv: Dict[str, Any]) -> None:
    """
    Add device.properties from /detail-or-summary into kv under "detail.<propname>".
    This gives you a second, often fresher, data source without collisions.
    """
    try:
        device = detail.get("device", {}) if isinstance(detail, dict) else {}
        props = device.get("properties", {}) if isinstance(device, dict) else {}
        if not isinstance(props, dict):
            return

        for prop_name, prop_obj in props.items():
            if not isinstance(prop_obj, dict):
                continue
            # prefer converted_value if present
            if "converted_value" in prop_obj:
                val = prop_obj.get("converted_value")
            else:
                val = prop_obj.get("value")
            kv[f"detail.{str(prop_name).strip().lower()}"] = val

        # also expose enriched_data shortcuts (optional)
        enriched = device.get("enriched_data", {}) if isinstance(device, dict) else {}
        if isinstance(enriched, dict):
            wt = enriched.get("water_treatment", {})
            if isinstance(wt, dict):
                pwa = wt.get("pwa")
                if pwa is not None:
                    kv["detail.enriched.pwa"] = pwa
                control_version = wt.get("control_version")
                if control_version is not None:
                    kv["detail.enriched.control_version"] = control_version
                days_powered = wt.get("days_powered_up")
                if days_powered is not None:
                    kv["detail.enriched.days_powered_up"] = days_powered
                days_since_last = wt.get("days_since_last_recharge")
                if days_since_last is not None:
                    kv["detail.enriched.days_since_last_recharge"] = days_since_last
                salt_percent = wt.get("salt_level_percent")
                if salt_percent is not None:
                    kv["detail.enriched.salt_level_percent"] = salt_percent
    except Exception:
        # keep coordinator robust
        return


def _label_key(s: Optional[str]) -> str:
    return (s or "").strip().lower()


class IquaSoftenerCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """
    Fetch + normalize:
      - POST /auth/login
      - GET  /auth/check
      - GET  /app/data
      - GET  /devices/<id>/detail-or-summary
      - GET  /devices/<id>/debug
    data = {"kv": {canonical_key: value}, "tables": {...}, "detail": {...}}
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
    # HTTP helpers (sync; executor)
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

    def _get(self, path: str) -> Dict[str, Any]:
        sess = self._get_session()
        r = sess.get(
            self._url(path),
            headers=self._headers(with_auth=True),
            timeout=20,
        )

        if r.status_code in (401, 403):
            self._access_token = None
            self._login()
            r = sess.get(
                self._url(path),
                headers=self._headers(with_auth=True),
                timeout=20,
            )

        if r.status_code != 200:
            raise UpdateFailed(f"GET failed: HTTP {r.status_code} for {r.url}")

        return r.json()

    def _fetch_web_sequence(self) -> Dict[str, Any]:
        """
        Mimic the web app calls. In vielen Fällen sorgt das dafür,
        dass serverseitig frische Properties/Shadow-Daten bereitstehen.
        """
        try:
            _ = self._get("auth/check")
        except Exception as err:
            _LOGGER.debug("auth/check failed (ignored): %s", err)

        try:
            _ = self._get("app/data")
        except Exception as err:
            _LOGGER.debug("app/data failed (ignored): %s", err)

        detail: Dict[str, Any] = {}
        try:
            detail = self._get(f"devices/{self._device_uuid}/detail-or-summary")
        except Exception as err:
            _LOGGER.debug("detail-or-summary failed (ignored): %s", err)

        # helpful trace
        try:
            props = (detail.get("device", {}) or {}).get("properties", {}) or {}
            app_active = (props.get("app_active", {}) or {}).get("value")
            app_active_updated = (props.get("app_active", {}) or {}).get("updated_at")
            online_updated = (props.get("_internal_is_online", {}) or {}).get("updated_at")
            _LOGGER.debug(
                "detail-or-summary: app_active=%s (updated_at=%s), online_updated_at=%s",
                app_active,
                app_active_updated,
                online_updated,
            )
        except Exception:
            pass

        return detail

    def _fetch_debug(self) -> Dict[str, Any]:
        return self._get(f"devices/{self._device_uuid}/debug")

    def _parse_debug_json(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        groups = payload.get("groups", [])
        if not isinstance(groups, list):
            raise UpdateFailed("Unexpected debug payload: 'groups' is not a list")

        kv: Dict[str, Any] = {}
        tables = _parse_tables(groups)

        # We keep labels for robust fallbacks
        label_index: Dict[str, Any] = {}

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
                label, value = _extract_kv_label_and_value(item)

                canonical = CANONICAL_KV_MAP.get((gkey, raw_item_key))
                if canonical is None:
                    canonical = f"{gkey}.{raw_item_key}"

                kv[canonical] = value

                if label:
                    label_index[_label_key(label)] = value

        # ---- Robust fallback for "time_message_received" ----
        # If mapping does not hit, try by label (this is what you see in UI)
        if kv.get("customer.time_message_received") is None:
            for candidate_label in (
                "time message received",
                "time message recieved",  # just in case typo exists upstream
                "letzte nachricht empfangen",
                "last message received",
            ):
                if candidate_label in label_index and label_index[candidate_label] is not None:
                    kv["customer.time_message_received"] = label_index[candidate_label]
                    break

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
        if not self._access_token:
            self._login()

        # 1) do the web-like sequence and keep detail payload
        detail = self._fetch_web_sequence()

        # 2) fetch debug (still valuable for grouped tables etc.)
        debug_payload = self._fetch_debug()
        parsed = self._parse_debug_json(debug_payload)

        kv = parsed.get("kv", {})
        if not isinstance(kv, dict):
            kv = {}

        # 3) merge detail properties as additional kv source (often fresher)
        if isinstance(detail, dict):
            _merge_detail_properties_into_kv(detail, kv)

        # helpful debug marker: time_message_received
        _LOGGER.debug(
            "time_message_received=%s",
            kv.get("customer.time_message_received"),
        )

        return {
            "kv": kv,
            "tables": parsed.get("tables", {}),
            "detail": detail,
        }