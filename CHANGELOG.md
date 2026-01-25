# Changelog

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

