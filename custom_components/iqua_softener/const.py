from __future__ import annotations

DOMAIN = "iqua_softener"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_DEVICE_UUID = "device_uuid"

# Optional enrichment / derived calculations
CONF_HOUSE_WATERMETER_ENTITY = "house_watermeter_entity"
CONF_HOUSE_WATERMETER_UNIT_MODE = "house_watermeter_unit_mode"  # auto|m3|l|factor
CONF_HOUSE_WATERMETER_FACTOR = "house_watermeter_factor"

CONF_RAW_HARDNESS_DH = "raw_hardness_dh"
CONF_SOFTENED_HARDNESS_DH = "softened_hardness_dh"

# Default raw water hardness (°dH) used for the optional treated hardness calculation.
# User requested default: 22.2 °dH
DEFAULT_RAW_HARDNESS_DH = 22.2
DEFAULT_SOFTENED_HARDNESS_DH = 0.0  # assumed soft water hardness (°dH) if not specified

HOUSE_UNIT_MODE_AUTO = "auto"
HOUSE_UNIT_MODE_M3 = "m3"
HOUSE_UNIT_MODE_L = "l"
HOUSE_UNIT_MODE_FACTOR = "factor"

# Unit strings (Home Assistant shows these as text units)
VOLUME_FLOW_RATE_LITERS_PER_MINUTE = "L/min"

# Sodium calculation (LEYCOsoft PRO): sodium increase per removed hardness
CONF_RAW_SODIUM_MG_L = "raw_sodium_mg_l"
DEFAULT_RAW_SODIUM_MG_L = 69.2
SODIUM_MG_PER_DH = 8.0  # +8 mg/L sodium per 1 °dH hardness reduction (LEYCO service guide)
SODIUM_LIMIT_MG_L = 200.0  # German drinking water ordinance limit for sodium (mg/L)

# EWMA smoothing (effective hardness & sodium) for stable values between polls
EWMA_TAU_SECONDS = 60 * 60  # ~60 minutes time constant
