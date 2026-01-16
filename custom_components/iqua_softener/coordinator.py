from __future__ import annotations

import logging
import time
import random
from datetime import timedelta, datetime
from typing import Any, Dict, Optional, Tuple

import requests

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

def _to_float(val):
    """Best-effort float conversion (handles strings with comma decimals)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        s = str(val).strip().replace(" ", "")
        if s.count(",") == 1 and s.count(".") == 0:
            s = s.replace(",", ".")
        return float(s)
    except Exception:
        return None


_STORAGE_VERSION = 2
_STORAGE_KEY_FMT = f"{DOMAIN}_baseline_{'{'}device_uuid{'}'}"

# Polling interval: 15 minutes
UPDATE_INTERVAL = timedelta(minutes=5)

DEFAULT_API_BASE_URL = "https://api.myiquaapp.com/v1"
DEFAULT_APP_ORIGIN = "https://app.myiquaapp.com"
DEFAULT_USER_AGENT = "Mozilla/5.0 (HomeAssistant iQuaSoftener)"


# (group_key, item_key) -> canonical kv key
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

    # ---- Program settings (capacity) ----
    ("program_settings", "operating_capacity_grains"): "configuration.operating_capacity_grains",

    # operating capacity (grains)
    ("configuration_information", "operating_capacity"): "configuration.operating_capacity_grains",
    ("configuration_information", "operating_capacity_grains"): "configuration.operating_capacity_grains",

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


def _extract_item_value(item: Dict[str, Any]) -> Any:
    if item.get("type") == "kv":
        kv = item.get("item_kv") or {}
        if isinstance(kv, dict):
            return kv.get("value")
    return None


def _parse_tables(groups: list[Dict[str, Any]]) -> Dict[str, Any]:
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


class IquaSoftenerCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Fetch + normalize device data."""

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

        # Persisted baseline for the lifelong treated-water counter at last regeneration.
        self._baseline_store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY_FMT.format(device_uuid=device_uuid))
        self._baseline_loaded: bool = False
        self._baseline_treated_total_l: Optional[float] = None
        self._regen_active_prev: bool = False
        # Latch regeneration state for a short period because the cloud status
        # may disappear immediately after the final phase while the unit is
        # still effectively regenerating.
        self._regen_latch_until: float = 0.0
        self._regen_latch_seconds: float = 15 * 60


        # Persisted derived-metrics state
        self._last_regen_end: Optional[datetime] = None
        self._regen_end_history: list[datetime] = []
        self._daily_usage_history: list[dict[str, Any]] = []  # [{'date': 'YYYY-MM-DD', 'liters': float}]
        self._last_water_today_l: Optional[float] = None
        self._last_water_today_date: Optional[str] = None

        # Persisted capacity-delta state (fix17)
        self._capacity_ist_ready: bool = False
        self._capacity_remaining_l: Optional[float] = None
        self._water_total_last_l: Optional[float] = None

        # fix23: regen-reset guard + last known total capacity
        self._last_capacity_reset_date: Optional[str] = None
        self._last_total_capacity_l: Optional[float] = None
        # fix24: track recharge counter to detect regeneration completion even if status flags are stale
        self._last_recharge_cycles: Optional[int] = None

        # Rate limit (HTTP 429) backoff state (server-side throttling)
        self._rl_until: float = 0.0  # epoch seconds until which we should avoid calling throttled endpoints
        self._rl_backoff_s: float = 0.0  # last computed backoff duration (seconds)
        self._rl_hits: int = 0  # consecutive 429 hits (for exponential backoff)


    async def async_load_baseline(self) -> None:
        """Load persisted baseline for treated water counter."""
        if self._baseline_loaded:
            return
        self._baseline_loaded = True
        try:
            data = await self._baseline_store.async_load()
            if isinstance(data, dict):
                if data.get("baseline_treated_total_l") is not None:
                    self._baseline_treated_total_l = float(data["baseline_treated_total_l"])
                # optional derived state
                if data.get("last_regen_end"):
                    try:
                        self._last_regen_end = dt_util.parse_datetime(data["last_regen_end"])
                    except Exception:
                        self._last_regen_end = None
                if isinstance(data.get("regen_end_history"), list):
                    hist = []
                    for s in data["regen_end_history"]:
                        try:
                            d = dt_util.parse_datetime(s)
                            if d is not None:
                                hist.append(d)
                        except Exception:
                            continue
                    self._regen_end_history = hist[-30:]
                if isinstance(data.get("daily_usage_history"), list):
                    # list of {'date': 'YYYY-MM-DD', 'liters': float}
                    cleaned = []
                    for it in data["daily_usage_history"]:
                        if isinstance(it, dict) and isinstance(it.get("date"), str) and it.get("liters") is not None:
                            try:
                                cleaned.append({"date": it["date"], "liters": float(it["liters"])})
                            except Exception:
                                continue
                    self._daily_usage_history = cleaned[-60:]
                if data.get("last_water_today_l") is not None:
                    try:
                        self._last_water_today_l = float(data["last_water_today_l"])
                    except Exception:
                        self._last_water_today_l = None
                if isinstance(data.get("last_water_today_date"), str):
                    self._last_water_today_date = data.get("last_water_today_date")

                # fix17: capacity delta state
                if data.get("capacity_ist_ready") is not None:
                    self._capacity_ist_ready = bool(data["capacity_ist_ready"])
                if data.get("capacity_remaining_l") is not None:
                    try:
                        self._capacity_remaining_l = float(data["capacity_remaining_l"])
                    except Exception:
                        self._capacity_remaining_l = None
                if data.get("water_total_last_l") is not None:
                    try:
                        self._water_total_last_l = float(data["water_total_last_l"])
                    except Exception:
                        self._water_total_last_l = None
                # fix23: regen-reset guard + last known total capacity
                if isinstance(data.get("last_capacity_reset_date"), str):
                    self._last_capacity_reset_date = data.get("last_capacity_reset_date")
                if data.get("last_total_capacity_l") is not None:
                    try:
                        self._last_total_capacity_l = float(data["last_total_capacity_l"])
                    except Exception:
                        self._last_total_capacity_l = None



        except Exception as err:
            _LOGGER.debug("Failed to load iQua baseline store: %s", err)

    async def _async_save_baseline(self) -> None:
        """Persist current baseline."""
        try:
            await self._baseline_store.async_save(
                {
                    "baseline_treated_total_l": self._baseline_treated_total_l,
                    "last_regen_end": self._last_regen_end.isoformat() if self._last_regen_end else None,
                    "regen_end_history": [d.isoformat() for d in self._regen_end_history][-30:],
                    "daily_usage_history": self._daily_usage_history[-60:],
                    "last_water_today_l": self._last_water_today_l,
                    "last_water_today_date": self._last_water_today_date,
                    "capacity_ist_ready": self._capacity_ist_ready,
                    "capacity_remaining_l": self._capacity_remaining_l,
                    "water_total_last_l": self._water_total_last_l,
                    "last_capacity_reset_date": self._last_capacity_reset_date,
                    "last_total_capacity_l": self._last_total_capacity_l,
                }
            )
        except Exception as err:
            _LOGGER.debug("Failed to save iQua baseline store: %s", err)

    def _compute_capacity_total_l(self, kv: Dict[str, Any]) -> Optional[float]:
        """Compute total treated capacity in liters from grains + hardness."""
        op = kv.get("configuration.operating_capacity_grains")
        if op is None:
            for k, v in kv.items():
                if isinstance(k, str) and k.endswith("operating_capacity_grains"):
                    op = v
                    break
        hardness = kv.get("program.hardness_grains")
        if hardness is None:
            for k, v in kv.items():
                if isinstance(k, str) and (k.endswith("hardness_grains") or k.endswith("hardness")):
                    hardness = v
                    break
        try:
            op_f = float(op)
            hard_f = float(hardness)
            if op_f <= 0 or hard_f <= 0:
                return None
        except Exception:
            return None
        L_PER_GAL = 3.78541
        return (op_f / hard_f) * L_PER_GAL

    async def _postprocess_calculations(self, data: Dict[str, Any]) -> None:
        """Derive continuously updated calculated capacity values.

        The cloud may update capacity_remaining_percent infrequently. We compute
        remaining treated capacity from the lifelong treated_water_total_l counter
        and a persisted baseline set at the last regeneration.
        """
        kv = data.get("kv")
        if not isinstance(kv, dict):
            return

        treated_total = kv.get("water_usage.treated_water")
        try:
            treated_total_l = float(treated_total) if treated_total is not None else None
        except Exception:
            treated_total_l = None

        # fix24: detect regeneration completion via recharge cycle counter (second_backwash_cycles)
        cur_cycles = kv.get("regenerations.second_backwash_cycles")
        cur_cycles_i: int | None = None
        try:
            if cur_cycles is not None and str(cur_cycles).strip() != "":
                cur_cycles_i = int(float(cur_cycles))
        except Exception:
            cur_cycles_i = None

        # True if the cycle counter increased since last poll (best-effort regen marker)
        recharge_bumped = (
            cur_cycles_i is not None
            and self._last_recharge_cycles is not None
            and cur_cycles_i > self._last_recharge_cycles
        )

        # Remember last seen cycle counter
        if cur_cycles_i is not None:
            self._last_recharge_cycles = cur_cycles_i


        regen_raw = kv.get("program.regen_time_remaining")
        try:
            regen_rem = float(regen_raw) if regen_raw is not None else 0.0
        except Exception:
            regen_rem = 0.0

        # Newer API responses may include an explicit regeneration status.
        # Older versions often omit this key entirely; therefore we only
        # consider it when present.
        regen_status = kv.get("regeneration.regeneration_status") or kv.get("regeneration_status")
        regen_active_by_status: bool | None = None
        if isinstance(regen_status, str) and regen_status.strip():
            st = regen_status.strip().lower()
            if st in ("regenerating", "recharging", "recharge", "backwash", "rinse", "brine", "fast_rinse", "slow_rinse"):
                regen_active_by_status = True
            elif st in ("idle", "ready", "standby", "off"):
                regen_active_by_status = False

        # Raw regeneration detection:
        # - Prefer explicit status when present (newer/regen-only responses).
        # - Fall back to remaining-time counter (older responses).
        regen_raw_active = (regen_active_by_status is True) or (regen_active_by_status is None and regen_rem > 0.0)

        now_ts = time.time()

        # If we get an explicit idle/ready status, clear any latch immediately.
        if regen_active_by_status is False:
            self._regen_latch_until = 0.0

        # If we observe regeneration active, extend the latch window.
        if regen_raw_active:
            self._regen_latch_until = max(self._regen_latch_until, now_ts + self._regen_latch_seconds)

        # Latched regeneration state: stay "on" for a short period even if the
        # cloud stops sending regen keys, to avoid flickering.
        regen_active = regen_raw_active or (now_ts < self._regen_latch_until)


        # Track regeneration edges. We want the baseline to represent the
        # lifelong treated-water counter **after** a regeneration has completed.
        # The device reports regen_time_remaining > 0 while regenerating.
        if regen_active and not self._regen_active_prev:
            _LOGGER.debug("Regeneration started (regen_time_remaining=%s)", regen_rem)

        # Regen ended: active -> inactive
        if treated_total_l is not None and (not regen_active) and self._regen_active_prev:
            self._baseline_treated_total_l = treated_total_l
            # fix17: start delta-based tracking from regeneration end
            # fix24: on regeneration completion, reset remaining treated capacity to full
            total_l_now = self._compute_capacity_total_l(kv)
            if total_l_now is not None and total_l_now > 0:
                self._capacity_total_l = float(total_l_now)
                self._capacity_remaining_l = float(total_l_now)
                self._capacity_ist_ready = True
            else:
                # if total capacity is unknown, at least clear IST state so it can recover later
                self._capacity_ist_ready = False
            self._water_total_last_l = treated_total_l

            # Record regeneration end timestamp and history
            now = dt_util.now()
            self._last_regen_end = now
            self._regen_end_history.append(now)
            self._regen_end_history = self._regen_end_history[-30:]
            await self._async_save_baseline()
            _LOGGER.debug("Set treated-water baseline at regeneration end: %s L (regen_end=%s)", treated_total_l, now)

        # fix24b: If the cloud's regeneration flags are delayed or missing,
        # use the regeneration counter bump as a hard signal that a regeneration
        # has completed, and reset capacity/baselines accordingly.
        if recharge_bumped and treated_total_l is not None and (not regen_active):
            total_l_now = self._compute_capacity_total_l(kv)
            if total_l_now is not None and total_l_now > 0:
                self._baseline_treated_total_l = treated_total_l
                self._capacity_total_l = float(total_l_now)
                self._capacity_remaining_l = float(total_l_now)
                self._capacity_ist_ready = True
                self._water_total_last_l = treated_total_l
                _LOGGER.debug(
                    "Detected regeneration completion via recharge counter bump; reset capacity to full (total=%s L)",
                    total_l_now,
                )

        self._regen_active_prev = regen_active


        # fix23: If we missed the regen edge (e.g., HA offline overnight), force a capacity reset when:
        # - cloud says days_since_last_recharge == 0 (regen today)
        # - regen_time_remaining == 0 (not running)
        # This must only happen once per day, and only when we can resolve a total capacity.
        try:
            today_iso = dt_util.now().date().isoformat()
            # fix21: compute total capacity here (avoid unbound total_l before later calculations)
            total_l_now = self._compute_capacity_total_l(kv)
            # Keep last known total capacity for fallback
            if total_l_now is not None and total_l_now > 0:
                self._last_total_capacity_l = float(total_l_now)

            # fix22: determine days_since_last_recharge locally (avoid unbound cloud_days_since_f)
            cloud_days_since_f = None
            try:
                _cloud_days_since = None
                for _k in (
                    "enriched_data.days_since_last_recharge",
                    "enriched.days_since_last_recharge",
                    "enriched.days_since_last_recharge_days",
                    "enriched_data.days_since_last_recharge_days",
                    "days_since_last_recharge",
                    "days_since_last_recharge_days",
                    "regenerations.time_since_last_recharge_days",
                    "regenerations.days_since_last_recharge_days",
                ):
                    if kv.get(_k) is not None:
                        _cloud_days_since = kv.get(_k)
                        break
                if _cloud_days_since is not None:
                    cloud_days_since_f = _to_float(_cloud_days_since)
            except Exception:
                cloud_days_since_f = None

            reset_candidate = (cloud_days_since_f == 0.0) and (regen_rem == 0.0) and (not regen_active)
            # fix23: recovery reset if we missed the regen day and capacity is stuck at 0/None
            recovery_candidate = (
                (not reset_candidate)
                and (cloud_days_since_f is not None)
                and (cloud_days_since_f <= 1.0)
                and (regen_rem == 0.0)
                and (not regen_active)
                and (
                    (self._capacity_remaining_l is None)
                    or (float(self._capacity_remaining_l) <= 0.0)
                    or (not self._capacity_ist_ready)
                    or recharge_bumped
                )
            )
            if reset_candidate or recovery_candidate:
                if self._last_capacity_reset_date != today_iso:
                    # Resolve total capacity (prefer current computed, else last known)
                    total_for_reset = None
                    if total_l_now is not None and total_l_now > 0:
                        total_for_reset = float(total_l_now)
                    elif self._last_total_capacity_l is not None and self._last_total_capacity_l > 0:
                        total_for_reset = float(self._last_total_capacity_l)

                    if total_for_reset is None:
                        _LOGGER.debug(
                            "fix23: reset candidate true but total capacity unavailable (cloud_days_since=%s regen_rem=%s)",
                            cloud_days_since_f,
                            regen_rem,
                        )
                    else:
                        # Only reset if current remaining is missing/zero OR we are not IST-ready yet.
                        curr_rem = self._capacity_remaining_l
                        if (not self._capacity_ist_ready) or (curr_rem is None) or (float(curr_rem) <= 0.0):
                            _LOGGER.debug(
                                "fix23: applying capacity reset (%s): remaining->%sL",
                                ("recovery" if recovery_candidate else "regen-day"),
                                total_for_reset,
                            )
                            self._capacity_remaining_l = total_for_reset
                            self._capacity_ist_ready = True

                            # fix23: also reset the treated-water baseline so remaining capacity starts full.
                            treated_total_l_current = None
                            try:
                                _tt = kv.get("water_usage.treated_water")
                                treated_total_l_current = float(_tt) if _tt is not None else None
                            except Exception:
                                treated_total_l_current = None
                            if treated_total_l_current is not None:
                                self._baseline_treated_total_l = float(treated_total_l_current)

                            self._last_capacity_reset_date = today_iso
                            await self._async_save_baseline()
                        else:
                            # Still set the guard so we don't spam logs
                            self._last_capacity_reset_date = today_iso
                            await self._async_save_baseline()
                else:
                    _LOGGER.debug("fix23: reset candidate true but already reset today (%s)", today_iso)
        except Exception as err:
            _LOGGER.debug("fix23: regen-reset fallback failed: %s", err)


        # Track daily usage history using the device's 'water today' counter.
        water_today = kv.get("water_usage.water_today")
        try:
            water_today_l = float(water_today) if water_today is not None else None
        except Exception:
            water_today_l = None
        today_str = dt_util.now().date().isoformat()
        if self._last_water_today_date is None:
            self._last_water_today_date = today_str
            self._last_water_today_l = water_today_l
        else:
            # Detect daily reset: today's value drops compared to last observed value.
            if water_today_l is not None and self._last_water_today_l is not None:
                if water_today_l + 0.1 < self._last_water_today_l and self._last_water_today_l > 1.0:
                    # Assume reset happened -> store previous day's usage.
                    prev_date = self._last_water_today_date
                    self._daily_usage_history.append({"date": prev_date, "liters": float(self._last_water_today_l)})
                    # keep last 60 days, unique per date (keep latest)
                    dedup = {}
                    for it in self._daily_usage_history:
                        if isinstance(it, dict) and isinstance(it.get("date"), str) and it.get("liters") is not None:
                            dedup[it["date"]] = float(it["liters"])
                    self._daily_usage_history = [{"date": d, "liters": dedup[d]} for d in sorted(dedup.keys())][-60:]
                    self._last_water_today_date = today_str
                    self._last_water_today_l = water_today_l
                    await self._async_save_baseline()
                else:
                    # Normal progression within a day
                    self._last_water_today_l = water_today_l
                    self._last_water_today_date = today_str
            else:
                self._last_water_today_date = today_str
                self._last_water_today_l = water_today_l

        # If we have no baseline yet (e.g., first install), infer it from the
        # most reliable cloud inputs we have. Prefer the absolute "treated water left"
        # value when present, as the percent value is known to update infrequently.
        if self._baseline_treated_total_l is None and treated_total_l is not None:
            total_l = self._compute_capacity_total_l(kv)

            # 1) Prefer absolute remaining liters from cloud
            left_raw = kv.get("water_usage.treated_water_left")
            left_l = None
            if left_raw is not None:
                try:
                    left_l = float(left_raw)
                except Exception:
                    left_l = None
            if total_l is not None and left_l is not None:
                used_l = max(0.0, total_l - left_l)
                self._baseline_treated_total_l = treated_total_l - used_l
                await self._async_save_baseline()
                _LOGGER.debug(
                    "Inferred treated-water baseline from cloud treated_water_left: baseline=%s (treated_total=%s, left_l=%s, total_l=%s)",
                    self._baseline_treated_total_l,
                    treated_total_l,
                    left_l,
                    total_l,
                )
            else:
                # 2) Fallback to cloud remaining percent (scaled-by-10 sometimes)
                pct_raw = (
                    kv.get("capacity.capacity_remaining_percent")
                    or kv.get("status.capacity_remaining_percent")
                    or kv.get("detail.capacity_remaining_percent")
                    or kv.get("capacity_remaining_percent")
                )
                pct = None
                if pct_raw is not None:
                    try:
                        pct = float(pct_raw)
                        if pct > 100:
                            pct = pct / 10.0
                        pct = max(0.0, min(100.0, pct))
                    except Exception:
                        pct = None
                if total_l is not None and pct is not None:
                    used_l = total_l * (1.0 - pct / 100.0)
                    self._baseline_treated_total_l = treated_total_l - used_l
                    await self._async_save_baseline()
                    _LOGGER.debug(
                        "Inferred treated-water baseline from cloud percent: baseline=%s (treated_total=%s, pct=%s, total_l=%s)",
                        self._baseline_treated_total_l,
                        treated_total_l,
                        pct,
                        total_l,
                    )

        total_l = self._compute_capacity_total_l(kv)
        if total_l is not None:
            kv["calculated.treated_capacity_total_l"] = total_l

        # Expose regeneration status (info-only entities must not drive logic)
        kv["calculated.regen_time_remaining_secs"] = regen_rem
        kv["calculated.regeneration_running"] = regen_active
        kv["calculated.regeneration_status"] = regen_status if isinstance(regen_status, str) and regen_status.strip() else None

        # fix17: delta-based remaining capacity tracking
        # We only subtract *changes* in the lifetime treated-water counter after regeneration end,
        # so absolute values are never double-counted.
        if (not regen_active) and self._capacity_ist_ready and (self._capacity_remaining_l is not None) and (self._water_total_last_l is not None) and (treated_total_l is not None):
            delta = treated_total_l - self._water_total_last_l
            if delta > 0:
                self._capacity_remaining_l = max(0.0, float(self._capacity_remaining_l) - float(delta))
                self._water_total_last_l = treated_total_l
            elif delta < 0:
                # Counter reset/jump backwards: keep remaining but reset baseline to avoid negative deltas
                _LOGGER.debug("Treated-water counter moved backwards (last=%s now=%s). Resetting delta baseline.", self._water_total_last_l, treated_total_l)
                self._water_total_last_l = treated_total_l

        kv["calculated.capacity_ist_ready"] = self._capacity_ist_ready

        # Prefer fix17 delta-based remaining if ready; otherwise fall back to baseline-based absolute calc.
        remaining_l = None
        used_since_regen = None

        if total_l is not None and self._capacity_ist_ready and self._capacity_remaining_l is not None:
            remaining_l = max(0.0, min(float(total_l), float(self._capacity_remaining_l)))
            used_since_regen = max(0.0, float(total_l) - remaining_l)
            kv["calculated.treated_used_since_regen_l"] = used_since_regen
            kv["calculated.treated_capacity_remaining_l"] = remaining_l
            kv["calculated.treated_capacity_remaining_percent"] = (remaining_l / total_l) * 100.0 if total_l > 0 else None
            kv["calculated.baseline_treated_total_l"] = self._baseline_treated_total_l
        elif self._baseline_treated_total_l is not None and treated_total_l is not None and total_l is not None:
            used_since_regen = max(0.0, treated_total_l - self._baseline_treated_total_l)
            remaining_l = max(0.0, total_l - used_since_regen)
            kv["calculated.baseline_treated_total_l"] = self._baseline_treated_total_l
            kv["calculated.treated_used_since_regen_l"] = used_since_regen
            kv["calculated.treated_capacity_remaining_l"] = remaining_l
            kv["calculated.treated_capacity_remaining_percent"] = (remaining_l / total_l) * 100.0 if total_l > 0 else None

            # Derived metrics (local) based on persisted history
            now_dt = dt_util.now()

            # Prefer cloud 'enriched' days_since_last_recharge when present (it is not always included).
            cloud_days_since = None
            # Try explicit known keys first
            for k in (
                "enriched_data.days_since_last_recharge",
                "enriched.days_since_last_recharge",
                "enriched.days_since_last_recharge_days",
                "enriched_data.days_since_last_recharge_days",
                "days_since_last_recharge",
                "days_since_last_recharge_days",
                # Some payloads expose the authoritative value under "regenerations".
                "regenerations.time_since_last_recharge_days",
                "regenerations.days_since_last_recharge_days",
            ):
                if kv.get(k) is not None:
                    cloud_days_since = kv.get(k)
                    break
            # As a fallback, scan for any key that contains 'days_since_last_recharge'
            # As a fallback, scan ONLY enriched keys for any path that contains 'days_since_last_recharge'.
            # (Do not pick up local/calculated keys, which can be fractional and drift.)
            if cloud_days_since is None:
                for k, v in kv.items():
                    if (
                        isinstance(k, str)
                        and v is not None
                        and "days_since_last_recharge" in k
                        and (k.startswith("enriched") or k.startswith("enriched_data") or k == "days_since_last_recharge")
                    ):
                        cloud_days_since = v
                        break
            try:
                cloud_days_since_f = _to_float(cloud_days_since) if cloud_days_since is not None else None
            except Exception:
                cloud_days_since_f = None

            if self._last_regen_end is None:
                cloud_days = kv.get("regenerations.time_since_last_recharge_days")
                try:
                    cloud_days_f = float(cloud_days) if cloud_days is not None else None
                except Exception:
                    cloud_days_f = None
                if cloud_days_f is not None:
                    self._last_regen_end = now_dt - timedelta(days=cloud_days_f)
                    # Do not backfill full history; just seed last_regen_end for immediate availability.
                    await self._async_save_baseline()



            # If the enriched cloud counter is present, we can also detect a recharge event by a drop in this value.
            if cloud_days_since_f is not None:
                prev_cloud = getattr(self, "_cloud_days_since_last_recharge_prev", None)
                # Store current for next cycle
                self._cloud_days_since_last_recharge_prev = cloud_days_since_f
                # A significant drop indicates a new recharge just completed.
                if prev_cloud is not None and cloud_days_since_f + 0.5 < float(prev_cloud):
                    _LOGGER.debug(
                        "Detected recharge event via days_since_last_recharge drop: %.2f -> %.2f",
                        float(prev_cloud), float(cloud_days_since_f)
                    )
                    # Reset local regen timestamp and history
                    self._last_regen_end = now_dt
                    self._regen_end_history.append(now_dt)
                    self._regen_end_history = self._regen_end_history[-30:]
                    # Reset capacity-delta baseline so remaining capacity starts fresh after recharge
                    if total_l is not None and treated_total_l is not None:
                        try:
                            self._capacity_remaining_l = float(total_l)
                            self._water_total_last_l = float(treated_total_l)
                            self._capacity_ist_ready = True
                        except Exception:
                            pass
                    await self._async_save_baseline()

            if self._last_regen_end is not None or cloud_days_since_f is not None:
                try:
                    if cloud_days_since_f is not None:
                        # Prefer authoritative cloud/enriched counter when present
                        kv["calculated.days_since_last_regen_days"] = max(0, int(_to_float(cloud_days_since_f)))
                        # Keep local timestamp in sync for any other calculations that rely on it
                        self._last_regen_end = now_dt - timedelta(days=_to_float(cloud_days_since_f))
                    else:
                        days = (now_dt - self._last_regen_end).total_seconds() / 86400.0
                        kv["calculated.days_since_last_regen_days"] = max(0.0, days)
                except Exception:
                    kv["calculated.days_since_last_regen_days"] = None

            # Average daily use (7d default)
            if self._daily_usage_history:
                hist = [it for it in self._daily_usage_history if isinstance(it, dict) and it.get("liters") is not None]
                last7 = [float(it["liters"]) for it in hist[-7:]]
                kv["calculated.average_daily_use_l"] = (sum(last7) / len(last7)) if last7 else None
                if kv.get("calculated.average_daily_use_l") is None:
                    cloud_avg = kv.get("water_usage.average_daily_use")
                    try:
                        kv["calculated.average_daily_use_l"] = float(cloud_avg) if cloud_avg is not None else None
                    except Exception:
                        kv["calculated.average_daily_use_l"] = None

                last14 = [float(it["liters"]) for it in hist[-14:]]
                kv["calculated.average_daily_use_l_14d"] = (sum(last14) / len(last14)) if last14 else None
                last30 = [float(it["liters"]) for it in hist[-30:]]
                kv["calculated.average_daily_use_l_30d"] = (sum(last30) / len(last30)) if last30 else None


            # Ensure avg daily use is available even without local history
            if kv.get("calculated.average_daily_use_l") is None:
                kv["calculated.average_daily_use_l"] = _to_float(kv.get("water_usage.average_daily_use"))

            # Restart fallback / baseline bootstrap:
            # If we are not IST-ready yet, try to bootstrap a plausible remaining-capacity value from
            # the cloud-enriched days_since_last_recharge and the device's treated-water 'today' counter.
            #
            # Why: On fresh installs / after restarts, we may not have an internal delta baseline yet.
            # The enriched counter is authoritative when present (and integer-like), but it is not always included.
            try:
                if (not self._capacity_ist_ready) and (cloud_days_since_f is not None):
                    total_l_now = kv.get("calculated.treated_capacity_total_l")
                    try:
                        total_l_now_f = float(total_l_now) if total_l_now is not None else None
                    except Exception:
                        total_l_now_f = None

                    if total_l_now_f is not None and total_l_now_f > 0:
                        days_f = _to_float(cloud_days_since_f)
                        # Only apply when regeneration is not in progress (0 means in/just after recharge).
                        if days_f >= 1.0:
                            # Today's treated usage from device counter (liters).
                            treated_today = kv.get("water_usage.water_today")
                            try:
                                treated_today_f = float(treated_today) if treated_today is not None else 0.0
                            except Exception:
                                treated_today_f = 0.0

                            # Optional average daily treated usage (persisted local history).
                            avg_day = kv.get("calculated.average_daily_use_l")
                            try:
                                avg_day_f = float(avg_day) if avg_day is not None else None
                            except Exception:
                                avg_day_f = None

                            # Estimate used since last regen:
                            # - Always subtract today's treated usage
                            # - If we have an average and days>1, also subtract (days-1)*avg
                            est_used = max(0.0, treated_today_f)
                            if days_f > 1.0 and avg_day_f is not None and avg_day_f >= 0:
                                est_used += max(0.0, (days_f - 1.0)) * avg_day_f

                            remaining_est = max(0.0, min(total_l_now_f, total_l_now_f - est_used))

                            # Publish estimate for sensors and bootstrap delta tracking.
                            kv["calculated.treated_capacity_remaining_l"] = remaining_est
                            kv["calculated.treated_capacity_remaining_percent"] = (remaining_est / total_l_now_f) * 100.0
                            kv["calculated.treated_capacity_remaining_is_estimate"] = True

                            # Bootstrap internal IST tracking so we can start subtracting deltas immediately.
                            self._capacity_remaining_l = remaining_est
                            self._capacity_ist_ready = True

                            # Seed delta baseline from current treated_total counter (if present).
                            if treated_total_l is not None:
                                self._water_total_last_l = treated_total_l

                                # Also seed an approximate regen baseline (for absolute fallback display),
                                # only if we don't have one yet.
                                if self._baseline_treated_total_l is None:
                                    baseline_est = max(0.0, float(treated_total_l) - float(est_used))
                                    self._baseline_treated_total_l = baseline_est

                            await self._async_save_baseline()
            except Exception:
                pass

            # Average days between regenerations
            avg_days_between: float | None = None

            # 1) Prefer local regeneration end history (last up to 5 intervals)
            if len(self._regen_end_history) >= 2:
                hist_ts = sorted([d for d in self._regen_end_history if d is not None])
                diffs = [(hist_ts[i] - hist_ts[i - 1]).total_seconds() / 86400.0 for i in range(1, len(hist_ts))]
                last_int = [d for d in diffs[-5:] if d is not None and d >= 0]
                avg_days_between = (sum(last_int) / len(last_int)) if last_int else None

            # 2) Fallback: derive from time-in-operation / total regenerations (cloud-provided counters)
            if avg_days_between is None:
                try:
                    op_days = _to_float(kv.get("regenerations.time_in_operation_days"))
                    reg_total = _to_float(kv.get("regenerations.total_regens"))
                    if op_days is not None and reg_total is not None and reg_total > 0:
                        avg_days_between = op_days / reg_total
                except Exception:
                    avg_days_between = None

            # 3) Fallback: use cloud-provided average (if available)
            if avg_days_between is None:
                cloud_avg = None
                for k in (
                    "regenerations.average_days_between_recharge_days",
                    "regenerations.average_days_between_regen_days",
                    "enriched.average_days_between_recharge_days",
                    "enriched_data.average_days_between_recharge_days",
                ):
                    if kv.get(k) is not None:
                        cloud_avg = kv.get(k)
                        break
                try:
                    avg_days_between = _to_float(cloud_avg)
                except Exception:
                    avg_days_between = None

            kv["calculated.average_days_between_regen_days"] = avg_days_between

        elif self._baseline_treated_total_l is not None:
            kv["calculated.baseline_treated_total_l"] = self._baseline_treated_total_l
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
        return f"{self._api_base_url}/{path.lstrip('/')}"

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

    def _get(self, path: str, *, use_token: bool = True) -> Dict[str, Any]:
        """Perform a GET request.

        NOTE: iQua cloud may throttle requests with HTTP 429. We apply a local
        exponential backoff (with jitter) to avoid hammering the API and to keep
        the integration stable during outages.
        """
        # Respect local backoff window (primarily for /live which is most rate-limited)
        now = time.time()
        if self._rl_until and now < self._rl_until:
            raise UpdateFailed(
                f"GET failed: HTTP 429 (backoff active) for {self._url(path)}"
            )

        sess = self._get_session()
        r = sess.get(
            self._url(path),
            headers=self._headers(with_auth=use_token),
            timeout=20,
        )

        if use_token and r.status_code in (401, 403):
            self._access_token = None
            self._login()
            r = sess.get(
                self._url(path),
                headers=self._headers(with_auth=True),
                timeout=20,
            )

        if r.status_code == 429:
            # Compute backoff duration
            retry_after = None
            try:
                ra = r.headers.get("Retry-After")
                if ra:
                    retry_after = float(ra)
            except Exception:
                retry_after = None

            # Exponential backoff starting at 60s, capped at 15 minutes
            self._rl_hits = min(self._rl_hits + 1, 10)
            base = 60.0 * (2 ** (self._rl_hits - 1))
            backoff = min(900.0, base)
            if retry_after is not None:
                backoff = max(backoff, retry_after)
            # small jitter to desynchronize with other clients
            jitter = random.uniform(0.0, 10.0)
            backoff = backoff + jitter
            self._rl_backoff_s = backoff
            self._rl_until = time.time() + backoff
            raise UpdateFailed(f"GET failed: HTTP 429 for {r.url}")

        if r.status_code != 200:
            # reset backoff counter on non-429 errors? keep hits but don't extend window
            raise UpdateFailed(f"GET failed: HTTP {r.status_code} for {r.url}")

        # Success: clear rate-limit state
        self._rl_hits = 0
        self._rl_backoff_s = 0.0
        self._rl_until = 0.0
        return r.json()
    def _fetch_web_sequence(self, device_uuid: str) -> dict[str, object]:
        """Fetch additional device info via the same sequence as the web UI.

        Returns a dict with optional keys:
          - device_or_summary
          - detail_or_summary
          - ease
        """
        out: dict[str, object] = {}

        # Pre-calls the web UI does (keep, as it seems to trigger server-side refresh)
        try:
            self._get("auth", use_token=True)
        except Exception:
            pass
        try:
            self._get("login", use_token=True)
        except Exception:
            pass

        # Main payloads
        try:
            out["device_or_summary"] = self._get(
                f"devices/{device_uuid}/device-or-summary", use_token=True
            )
        except Exception:
            out["device_or_summary"] = None

        try:
            out["detail_or_summary"] = self._get(
                f"devices/{device_uuid}/detail-or-summary", use_token=True
            )
        except Exception:
            out["detail_or_summary"] = None

        try:
            out["ease"] = self._get(
                f"devices/{device_uuid}/support/ease", use_token=True
            )
        except Exception:
            out["ease"] = None

        return out
    def _fetch_debug(self) -> dict[str, object]:
        device_uuid = self._device_uuid

        live: dict[str, object] | None = None
        try:
            live = self._get(f"devices/{device_uuid}/live", use_token=True)
        except UpdateFailed as err:
            # If the cloud throttles us (HTTP 429), continue with other endpoints.
            # This keeps sensors stable while we are in backoff.
            if "HTTP 429" in str(err):
                _LOGGER.warning("iQua API rate-limited for /live (HTTP 429); continuing with partial data")
            else:
                raise

        detail = self._fetch_web_sequence(device_uuid)
        debug = self._get(f"devices/{device_uuid}/debug", use_token=True)

        return {"debug": debug, "detail": detail, "live": live}
    def _merge_detail_into_kv(self, kv: Dict[str, Any], detail: Dict[str, Any]) -> None:
        """Fill missing KV entries from detail-or-summary.

        Some installations do not receive certain configuration values in
        /debug (notably operating_capacity_grains). detail-or-summary has them
        under device.properties.
        """
        if not isinstance(detail, dict):
            return

        device = detail.get("device") or {}
        props = (device.get("properties") or {}) if isinstance(device, dict) else {}
        if not isinstance(props, dict):
            return

        def _prop_value(name: str) -> Any:
            p = props.get(name) or {}
            if isinstance(p, dict):
                return p.get("value")
            return None

        # ---- Values needed for calculated capacity sensors ----
        # Use the canonical keys expected by sensor.py.
        if kv.get("configuration.operating_capacity_grains") is None:
            op = _prop_value("operating_capacity_grains")
            if op is not None:
                kv["configuration.operating_capacity_grains"] = op

        if kv.get("program.hardness_grains") is None:
            hg = _prop_value("hardness_grains")
            if hg is not None:
                kv["program.hardness_grains"] = hg

        # Resin load is available in debug for many devices, but we can also
        # pick it up here if needed later.
        if kv.get("configuration.resin_load_liters") is None:
            rl = _prop_value("resin_load")
            if rl is not None:
                kv["configuration.resin_load_liters"] = rl
        # Capacity / salt values (these should be updated whenever present)
        cap_pct = _prop_value("capacity_remaining_percent") or _prop_value("restkapazitat") or _prop_value("remaining_capacity_percent")
        if cap_pct is not None:
            kv["capacity.capacity_remaining_percent"] = cap_pct

        salt_days = _prop_value("salt_remaining_days") or _prop_value("salt_days_remaining")
        if salt_days is not None:
            kv["salt.salt_remaining_days"] = salt_days

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
                if not isinstance(item, dict) or item.get("type") != "kv":
                    continue
                raw_item_key = _normalize_item_key(item.get("key", ""))
                value = _extract_item_value(item)

                canonical = CANONICAL_KV_MAP.get((gkey, raw_item_key)) or f"{gkey}.{raw_item_key}"
                kv[canonical] = value

        # Alias for robustness (timestamp appears under different groups for some accounts)
        if "customer.time_message_received" not in kv:
            for k in list(kv.keys()):
                if k.endswith(".time_message_received") and kv.get(k) is not None:
                    kv["customer.time_message_received"] = kv[k]
                    break

        return {"kv": kv, "tables": tables}

    async def _async_update_data(self) -> Dict[str, Any]:
        await self.async_load_baseline()
        try:
            data = await self.hass.async_add_executor_job(self._sync_update)
            await self._postprocess_calculations(data)
            return data
        except UpdateFailed:
            raise
        except requests.exceptions.RequestException as err:
            raise UpdateFailed(f"Request error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {type(err).__name__}: {err}") from err

    def _sync_update(self) -> Dict[str, Any]:
        # Ensure we have a valid token
        if not self._access_token:
            self._login()

        payloads = self._fetch_debug()

        data = self._parse_debug_json(payloads.get("debug", {}))
        kv = data.get("kv", {})

        # Merge /live values into kv. This endpoint tends to update whenever the
        # web UI is opened and often carries the freshest runtime properties.
        live = payloads.get("live")
        if isinstance(live, dict):
            try:
                self._merge_detail_into_kv(kv, live)
            except Exception as err:
                _LOGGER.debug("Failed to merge /live into kv (ignored): %s", err)

        # Merge web-sequence payloads (detail-or-summary / ease) into kv
        detail_bundle = payloads.get("detail") or {}
        if isinstance(detail_bundle, dict):
            for k in ("detail_or_summary", "ease"):
                part = detail_bundle.get(k)
                if isinstance(part, dict):
                    self._merge_detail_into_kv(kv, part)

        # Keep raw payloads around for troubleshooting / future sensors
        data["raw"] = {
            "live": payloads.get("live"),
            "detail": detail_bundle,
        }

        return data
