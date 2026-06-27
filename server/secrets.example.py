"""EXAMPLE secrets / seed generator.  ===  DO NOT COMMIT REAL SECRETS  ===

Copy to `secrets.py` (gitignored), replace the placeholder tokens with real,
high-entropy secrets, then generate the seed the server reads:

    python secrets.py            # writes ./seed.json

The server NEVER imports this file; it reads the generated seed.json and stores
only the sha256 of each token (plaintext is never persisted). Generate strong
tokens with e.g.:

    python -c "import secrets; print(secrets.token_urlsafe(32))"

WiFi creds and the Pico's copy of its device token live on the device side
(firmware secrets), never on the server — keep them out of git too.
"""

import json

# --- devices the server owns (channels / fallback / poll intervals) ------------

DEVICES = [
    {
        "id": "kitchen-01",
        "channels": ["home.status", "home.alerts", "home.tasks"],
        "fallback": {
            "layout": "status_card",
            "content": {
                "title": "Papertrail",
                "status": "IDLE",
                "subtitle": "Waiting for updates",
                "lines": ["No active messages"],
                "footer": "papertrail",
            },
        },
        "poll_interval_s": 120,
        "low_batt_interval_s": 600,
    }
]

# --- tokens (PLAINTEXT here; hashed at seed time) -------------------------------
#   kind:       'device' (GET /current) | 'ingest' (POST /events)
#   device_id:  the single device this token is scoped to
#   channels:   ingest only; list = allowed channels, None/omit = all channels
#   rate_per_min: per-token request ceiling (best-effort, see auth.py)

TOKENS = [
    {
        "token": "REPLACE_ME_device_token_kitchen01",
        "kind": "device",
        "device_id": "kitchen-01",
        "rate_per_min": 60,
    },
    {
        "token": "REPLACE_ME_ingest_token_kitchen01",
        "kind": "ingest",
        "device_id": "kitchen-01",
        "channels": None,  # all channels; set e.g. ["home.alerts"] to restrict
        "rate_per_min": 120,
    },
]


def build_seed() -> dict:
    return {"devices": DEVICES, "tokens": TOKENS}


if __name__ == "__main__":
    with open("seed.json", "w", encoding="utf-8") as fh:
        json.dump(build_seed(), fh, indent=2)
    print("wrote seed.json")
