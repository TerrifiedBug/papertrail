# Layout renderers for papertrail -- pixel geometry per ../docs/layout-specs.md.
#
# DESIGN: renderers draw into a *canvas* object (duck-typed), not directly into a
# framebuf. The device canvas (FrameCanvas) wraps the Waveshare epd framebuffer;
# the tests pass a recording canvas. This keeps the pure layout logic (field
# selection, clipping, wrapping, coordinates) importable WITHOUT `framebuf`, so it
# runs under host CPython. The `framebuf` import below is guarded for that reason.
#
# Coordinates: origin top-left, x right 0..249, y down 0..121. W=250, H=122, PAD=4.
# Colors: INK = black = 0x00, PAPER = white = 0xFF. Clear each frame with fill(PAPER).
# (framebuf masks mono colour to its LSB, so 0x00 -> 0 = ink, 0xFF -> 1 = paper.)

INK = 0x00
PAPER = 0xFF
W = 250
H = 122

try:
    import framebuf
    _HAS_FB = True
except ImportError:                         # host / tests
    framebuf = None
    _HAS_FB = False


# --------------------------------------------------------------------------
# Pure text-fitting helpers (ASCII-only; the 8x8 font has no ellipsis glyph)
# --------------------------------------------------------------------------
def clip(s, n):
    """Fit to a single line of n chars. If truncated, the last 3 kept chars
    become '...' (when n > 3); if n <= 3, hard cut."""
    s = "" if s is None else str(s)
    if len(s) <= n:
        return s
    if n <= 3:
        return s[:n]
    return s[:n - 3] + "..."


def wrap(s, n, l):
    """Greedy word-wrap to width n, at most l lines. Words longer than n are
    hard-split. If content overflows l lines, the last shown line is clipped
    with a trailing '...'."""
    s = "" if s is None else str(s)

    # Tokenise on spaces, hard-splitting any word wider than n.
    tokens = []
    for w in s.split(" "):
        if w == "":
            continue
        while len(w) > n:
            tokens.append(w[:n])
            w = w[n:]
        tokens.append(w)

    # Greedy fill (unbounded line count first).
    full = []
    cur = ""
    for tok in tokens:
        if cur == "":
            cur = tok
        elif len(cur) + 1 + len(tok) <= n:
            cur = cur + " " + tok
        else:
            full.append(cur)
            cur = tok
    if cur != "":
        full.append(cur)

    if len(full) <= l:
        return full

    # Overflow: keep l lines; force a truncation marker on the last kept line by
    # folding in the first dropped line, then clip -> guarantees a trailing '...'.
    kept = full[:l]
    kept[l - 1] = clip(kept[l - 1] + " " + full[l], n)
    return kept


def _s(content, key):
    v = content.get(key, "")
    return "" if v is None else str(v)


def _list(content, key):
    v = content.get(key, [])
    return v if isinstance(v, list) else []


# --------------------------------------------------------------------------
# Layout renderers -- each clears to PAPER then draws INK. (canvas, content)
# --------------------------------------------------------------------------
def render_status_card(canvas, content):
    canvas.fill(PAPER)
    status = clip(_s(content, "status"), 8)
    title = clip(_s(content, "title"), 12)
    canvas.text(title, 4, 2, INK, 2)                       # S2 header
    canvas.text(status, 246 - 8 * len(status), 6, INK, 1)  # S1 right-aligned badge
    canvas.hline(20)
    canvas.text(clip(_s(content, "subtitle"), 30), 4, 25, INK, 1)
    for i, line in enumerate(_list(content, "lines")[:5]):  # rows y=40,51,62,73,84
        canvas.text(clip(line, 30), 4, 40 + 11 * i, INK, 1)
    canvas.hline(100)
    canvas.text(clip(_s(content, "footer"), 30), 4, 110, INK, 1)


_SEVERITY_LABEL = {"low": "LOW", "med": "MED", "high": "!! HIGH"}


def render_alert(canvas, content):
    canvas.fill(PAPER)
    sev = _s(content, "severity") or "low"
    label = _SEVERITY_LABEL.get(sev, "LOW")
    if sev == "high":
        # Dramatic banner + 2px whole-screen frame. On a tri-color panel these draw
        # on the RED plane (canvas.red); on mono, canvas.red IS the black canvas so
        # it stays an inverted-black banner (unchanged). getattr default = canvas so
        # the host recording-canvas (no .red) records on itself -> tests unaffected.
        red = getattr(canvas, "red", canvas)
        red.rect(0, 0, 250, 28, INK, True)                 # solid banner (red on B)
        red.text(label, 4, 6, PAPER, 2)                    # label punched white
        red.rect(0, 0, 250, 122, INK)                      # 2px frame
        red.rect(1, 1, 248, 120, INK)
    else:
        canvas.text(label, 4, 6, INK, 2)
        canvas.hline(28)
    canvas.text(clip(_s(content, "title"), 15), 4, 34, INK, 2)  # S2 title
    for i, line in enumerate(wrap(_s(content, "message"), 30, 4)):  # y=58,69,80,91
        canvas.text(line, 4, 58 + 11 * i, INK, 1)
    canvas.hline(100)                                       # always drawn
    canvas.text(clip(_s(content, "footer"), 30), 4, 110, INK, 1)


def render_list(canvas, content):
    canvas.fill(PAPER)
    canvas.text(clip(_s(content, "title"), 15), 4, 2, INK, 2)
    canvas.hline(20)
    for i, item in enumerate(_list(content, "items")[:6]):  # rows y=26,38,50,62,74,86
        y = 26 + 12 * i
        canvas.text("[ ] ", 4, y, INK, 1)                  # decorative checkbox
        canvas.text(clip(item, 26), 36, y, INK, 1)
    canvas.hline(100)
    canvas.text(clip(_s(content, "footer"), 30), 4, 110, INK, 1)


def render_metric(canvas, content):
    canvas.fill(PAPER)
    canvas.text(clip(_s(content, "label"), 30), 4, 6, INK, 1)
    canvas.hline(18)

    value = clip(_s(content, "value"), 7)
    unit = clip(_s(content, "unit"), 4)
    trend = clip(_s(content, "trend"), 30)

    value_px = 32 * len(value)
    unit_px = 16 * len(unit) if unit else 0
    gap = 6 if unit else 0
    group = value_px + gap + unit_px
    x_v = max(4, (250 - group) // 2)
    x_u = x_v + value_px + gap

    canvas.text(value, x_v, 34, INK, 4)                    # S4 value
    if unit:
        canvas.text(unit, x_u, 50, INK, 2)                 # S2 unit, baseline-aligned
    x_t = max(4, (250 - 8 * len(trend)) // 2)
    canvas.text(trend, x_t, 82, INK, 1)                    # centered trend
    canvas.hline(100)
    canvas.text(clip(_s(content, "footer"), 30), 4, 110, INK, 1)


def render_qr(canvas, content):
    canvas.fill(PAPER)
    canvas.text(clip(_s(content, "title"), 15), 4, 2, INK, 2)
    canvas.hline(20)

    data = _s(content, "qr_data")
    grid = None
    if data:
        try:
            import qr
            grid = qr.matrix(data)
        except Exception as e:
            grid = None
            canvas.text(clip("QR err: " + str(e), 17), 104, 30, INK, 1)

    if grid:
        n = len(grid)
        module_px = max(2, 90 // n)
        rendered = module_px * n
        x_off = 8 + (90 - rendered) // 2
        y_off = 26 + (90 - rendered) // 2
        for ry in range(n):
            row = grid[ry]
            base_y = y_off + ry * module_px
            for rx in range(n):
                if row[rx]:                                # dark module -> INK
                    canvas.rect(x_off + rx * module_px, base_y,
                                module_px, module_px, INK, True)

    for i, line in enumerate(wrap(_s(content, "caption"), 17, 7)):  # y=30..96 step 11
        canvas.text(line, 104, 30 + 11 * i, INK, 1)


RENDERERS = {
    "status_card": render_status_card,
    "alert": render_alert,
    "list": render_list,
    "metric": render_metric,
    "qr": render_qr,
}


def render(canvas, layout, content):
    """Dispatch on layout. Unknown layout -> offline screen. Returns True if a
    known layout was drawn."""
    fn = RENDERERS.get(layout)
    if fn is None:
        render_offline(canvas, "bad layout: " + str(layout))
        return False
    fn(canvas, content or {})
    return True


# --------------------------------------------------------------------------
# Special device screens (reuse the layout renderers for consistency)
# --------------------------------------------------------------------------
def render_offline(canvas, detail=""):
    render_status_card(canvas, {
        "title": "Offline",
        "status": "X",
        "subtitle": "No server connection",
        "lines": [detail] if detail else [],
        "footer": "papertrail",
    })


def render_low_battery(canvas, pct):
    render_metric(canvas, {
        "label": "Battery low",
        "value": str(pct),
        "unit": "%",
        "trend": "CHARGE SOON",
        "footer": "papertrail",
    })


# --------------------------------------------------------------------------
# Device canvas -- wraps the Waveshare epd framebuffer (needs `framebuf`).
# --------------------------------------------------------------------------
class FrameCanvas:
    """Adapts a framebuf.FrameBuffer (the epd) to the renderer canvas API and
    implements integer-scaled text from the built-in 8x8 font."""

    def __init__(self, fb, y_offset=0):
        if not _HAS_FB:
            raise RuntimeError("FrameCanvas requires framebuf")
        self.fb = fb
        self.y0 = y_offset          # shift all draws down past the off-screen top margin
        # Scratch 8x8 mono buffer used to read the built-in font glyph pixels.
        self._gbuf = bytearray(8)
        self._gfb = framebuf.FrameBuffer(self._gbuf, 8, 8, framebuf.MONO_HLSB)

    def fill(self, color):
        self.fb.fill(color)         # full-screen; not offset

    def text(self, s, x, y, color, n=1):
        y = y + self.y0
        if n <= 1:
            self.fb.text(s, x, y, color)
            return
        gfb = self._gfb
        for ci in range(len(s)):
            gfb.fill(0)
            gfb.text(s[ci], 0, 0, 1)        # glyph drawn as set bits
            ox = x + ci * 8 * n
            for yy in range(8):
                oy = y + yy * n
                for xx in range(8):
                    if gfb.pixel(xx, yy):
                        self.fb.fill_rect(ox + xx * n, oy, n, n, color)

    def hline(self, y, color=INK):
        self.fb.hline(0, y + self.y0, W, color)   # 1px INK rule x0..249

    def rect(self, x, y, w, h, color, fill=False):
        self.fb.rect(x, y + self.y0, w, h, color, fill)

    def pixel(self, x, y, color):
        self.fb.pixel(x, y + self.y0, color)


def _prep(epd):
    """Primary (black) canvas with `.red` wired and both planes cleared.

    A tri-color epd (epaper2in13b) exposes `imageblack`/`imagered` framebuffers; a
    mono epd IS the framebuffer. On mono, `.red` folds onto the black canvas so red
    draws degrade to black. The red plane MUST be cleared each frame or stale red
    RAM shows as garbage (the bug that turned the mono driver's output solid red)."""
    try:
        import config
        off = getattr(config, "EPAPER_Y_OFFSET", 0)
    except Exception:
        off = 0
    if hasattr(epd, "imageblack"):              # tri-color B/W/Red
        black = FrameCanvas(epd.imageblack, off)
        red = FrameCanvas(epd.imagered, off)
        red.fill(PAPER)
        black.red = red
        return black
    c = FrameCanvas(epd, off)                    # mono
    c.red = c
    return c


def _push(epd):
    if hasattr(epd, "imageblack"):
        epd.display()                            # both planes, no args (tri-color)
    else:
        epd.display(epd.buffer)                  # mono


def draw_battery(canvas, pct, on_battery, low=False):
    """Bottom-right battery badge: charge % + a level-filled glyph; a '+' prefix
    when wired/charging; drawn on the red plane when low (tri-color)."""
    if pct is None:
        return
    pct = 0 if pct < 0 else 100 if pct > 100 else int(pct)
    plane = canvas.red if low else canvas
    # Seat on a cleared (PAPER) patch so a long footer's tail doesn't bleed through.
    # ponytail: the badge owns the bottom-right corner -- a >22-char footer loses its
    # tail there, and on `alert` it overlays the red frame. Acceptable for v1.
    canvas.rect(188, 109, 62, 13, PAPER, True)
    label = ("+" if not on_battery else "") + str(pct)
    plane.text(label, 222 - 8 * len(label), 112, INK, 1)
    plane.rect(226, 111, 18, 9, INK)            # body outline
    plane.rect(244, 114, 2, 3, INK, True)       # nub
    fillw = int(round(pct / 100.0 * 14))
    if fillw > 0:
        plane.rect(228, 113, fillw, 5, INK, True)


def draw_to_epd(epd, layout, content, batt=None):
    """Render a resolved screen into the epd plane(s) and push it. `batt`, when
    given, is (pct, on_battery, low) and overlays a battery badge bottom-right."""
    canvas = _prep(epd)
    render(canvas, layout, content)
    if batt is not None and batt[0] is not None:
        draw_battery(canvas, batt[0], batt[1], batt[2] if len(batt) > 2 else False)
    _push(epd)


def draw_offline(epd, detail=""):
    render_offline(_prep(epd), detail)
    _push(epd)


def draw_low_battery(epd, pct):
    render_low_battery(_prep(epd), pct)
    _push(epd)
