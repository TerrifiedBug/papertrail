# Copy this file to  secrets.py  and fill in REAL values.
# secrets.py is gitignored (see firmware/.gitignore) -- NEVER commit real creds/tokens.
#
#   cp secrets.example.py secrets.py
#
# These placeholders are intentionally obvious fakes.

# --- WiFi -----------------------------------------------------------------
WIFI_SSID = "your-wifi-ssid"
WIFI_PASSWORD = "your-wifi-password"

# --- Device bearer token --------------------------------------------------
# Scoped to exactly this device. Sent as:  Authorization: Bearer <DEVICE_TOKEN>
# on GET /api/devices/:id/current. The server stores only its sha256 digest.
DEVICE_TOKEN = "REPLACE_WITH_DEVICE_TOKEN"

# --- Optional server URL override -----------------------------------------
# If set, overrides config.BASE_URL (handy to keep the real LAN address out of
# git). Leave as None to use config.BASE_URL.
SERVER_URL = None
