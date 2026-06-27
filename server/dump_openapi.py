"""Dump the bridge's OpenAPI schema to ``docs/openapi.json`` (indent=2).

Builds the app against a throwaway DB and an empty seed so generating the spec
has ZERO side effects on any real store (the lifespan never runs here — we only
call ``app.openapi()``, which touches no DB). Run from anywhere:

    python server/dump_openapi.py        # or: python -m server.dump_openapi
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# Put the repo root (parent of `server/`) on sys.path so `import server.*` works
# regardless of the directory this is invoked from.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from server.app import create_app  # noqa: E402

_OUT = os.path.join(_REPO_ROOT, "docs", "openapi.json")


def main() -> None:
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        app = create_app(db_path=db, seed={"devices": [], "tokens": []})
        spec = app.openapi()
    finally:
        os.unlink(db)

    os.makedirs(os.path.dirname(_OUT), exist_ok=True)
    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=2)
        fh.write("\n")
    print(f"wrote {_OUT}")


if __name__ == "__main__":
    main()
