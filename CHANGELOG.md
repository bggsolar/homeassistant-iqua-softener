# Changelog

## 2.4.28.3
- Fix: Options flow schema for regen_self_consumption_l (missing key caused 500 error).


## 2.4.28.2
- Fix: Remove regen_self_consumption_l from smoothed hardness sensor (was incorrectly passed to base).
- Fix: Options flow schema cleanup for regen_self_consumption_l.


## 2.4.28.1
- Fix: Options/config flow schema for regeneration self-consumption (prevent 500 error in UI).


## 2.4.28
- Feature: Configurable regeneration self-consumption (liters, default 100) deducted from daily house consumption for mixing/verschnitt and effective hardness.


## 2.4.27.2
- Improvement: Fallback operating_capacity_grains/hardness_grains from last known values when /debug is skipped (HTTP 429 backoff).
- Maintenance: Throttle repetitive "missing operating_capacity/hardness" debug logs (max once/hour per device).


## 2.4.27.1
- Fix: Handle debug=None when /debug is skipped during 429 backoff (prevent NoneType.get crash).


## 2.4.27
- Fix: Skip /debug when /live is rate-limited (HTTP 429) to prevent cascading failures; continue with partial data.


## 2.4.26
- Fix: Daily baseline guard for effective hardness today (prevent 0 °dH artifacts when daily counters are missing at day start).
- Improvement: Hold-last behavior when daily volumes are missing or no usage yet today.



## 2.4.24
- Fix: Regeneration handling for treated capacity/remaining capacity (prevent stale-cloud snapshots from blocking reset).
- Maintenance: Version bump and packaging.

