# Thin adapter over the vendored uQR generator (firmware/lib/uQR.py).
#
# Keeps render.py decoupled from uQR's API. On a Pico, copy lib/uQR.py to /lib so
# MicroPython finds it on sys.path (`import uQR`). On a host, we add the sibling
# lib/ directory to sys.path for testing.
#
# Source: JASchilz/uQR (BSD-2-Clause) -- see lib/uQR.py header for attribution.

try:
    import uQR                              # device: /lib/uQR.py on sys.path
except ImportError:                         # host: add ./lib next to this file
    import sys
    try:
        import os
        _here = os.path.dirname(__file__)
        _lib = _here + "/lib" if _here else "lib"
    except Exception:
        _lib = "lib"
    if _lib not in sys.path:
        sys.path.insert(0, _lib)
    import uQR

# Map the contract's level letters to uQR's constants. Default M (good density/ECC
# balance); L packs more data per version if you need a smaller module count.
_EC = {
    "L": uQR.ERROR_CORRECT_L,
    "M": uQR.ERROR_CORRECT_M,
    "Q": uQR.ERROR_CORRECT_Q,
    "H": uQR.ERROR_CORRECT_H,
}


def matrix(data, ec="M"):
    """Encode `data` and return the QR as a square list-of-lists of bools.

    True = dark (INK) module, False = light (PAPER). No quiet-zone border
    (border=0) -- render.py centres the grid inside the 90x90 box itself.
    box_size=1 because render.py owns module_px scaling.
    """
    q = uQR.QRCode(error_correction=_EC.get(ec, uQR.ERROR_CORRECT_M),
                   box_size=1, border=0)
    q.add_data(data)
    q.make(fit=True)                        # auto-pick the smallest fitting version
    return q.get_matrix()
