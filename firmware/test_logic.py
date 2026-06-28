#!/usr/bin/env python3
# Host-runnable (CPython) tests for papertrail firmware PURE logic.
# No test framework, no hardware: plain asserts. Run:
#
#     python3 firmware/test_logic.py
#
# Covers the things the contract cares about:
#   1. battery bus-voltage -> % curve, incl. clamping below v_min / above v_max
#   2. the ETag no-op decision (render ONLY when the screen actually changed)
#   3. per-layout field selection + truncation/wrapping (via a recording canvas)
#   4. the additive v1 control/telemetry plane:
#        - poll_interval local clamp to [30,3600] (don't trust the server blindly)
#        - the schema-version guard (v1 renders; a future v2 -> offline, never drawn)
#        - telemetry query-string construction (?batt=&rssi=&fw=&up=)
#
# These modules import on a host because their hardware imports are guarded:
#   ina219 (machine), poller (urequests), render (framebuf), qr->uQR (ure).

import sys
import os
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ina219
import poller
import render
import ota

INK = render.INK
PAPER = render.PAPER


# --------------------------------------------------------------------------
# Recording canvas: captures draw ops so we can assert geometry + field choice.
# --------------------------------------------------------------------------
class RecordingCanvas:
    def __init__(self):
        self.ops = []

    def fill(self, color):
        self.ops.append(("fill", color))

    def text(self, s, x, y, color, n=1):
        self.ops.append(("text", s, x, y, color, n))

    def hline(self, y, color=INK):
        self.ops.append(("hline", y, color))

    def rect(self, x, y, w, h, color, fill=False):
        self.ops.append(("rect", x, y, w, h, color, fill))

    def pixel(self, x, y, color):
        self.ops.append(("pixel", x, y, color))

    def texts(self):
        return [o for o in self.ops if o[0] == "text"]

    def text_strings(self):
        return [o[1] for o in self.ops if o[0] == "text"]

    def has_text(self, s, x=None, y=None, color=None, n=None):
        for o in self.texts():
            if o[1] != s:
                continue
            if x is not None and o[2] != x:
                continue
            if y is not None and o[3] != y:
                continue
            if color is not None and o[4] != color:
                continue
            if n is not None and o[5] != n:
                continue
            return True
        return False

    def has_rect(self, x, y, w, h, color, fill):
        return ("rect", x, y, w, h, color, fill) in self.ops

    def has_hline(self, y):
        return any(o[0] == "hline" and o[1] == y for o in self.ops)


# --------------------------------------------------------------------------
# 1. Battery voltage -> % curve
# --------------------------------------------------------------------------
def test_battery_curve():
    vmin, vmax = 3.0, 4.2
    assert ina219.voltage_to_pct(3.0, vmin, vmax) == 0, "v_min -> 0%"
    assert ina219.voltage_to_pct(4.2, vmin, vmax) == 100, "v_max -> 100%"
    # clamping
    assert ina219.voltage_to_pct(2.5, vmin, vmax) == 0, "below v_min clamps to 0"
    assert ina219.voltage_to_pct(0.0, vmin, vmax) == 0, "way below clamps to 0"
    assert ina219.voltage_to_pct(4.5, vmin, vmax) == 100, "above v_max clamps to 100"
    assert ina219.voltage_to_pct(9.9, vmin, vmax) == 100, "way above clamps to 100"
    # linear interior (chosen to avoid .5 rounding ambiguity)
    assert ina219.voltage_to_pct(3.6, vmin, vmax) == 50, "midpoint -> 50%"
    assert ina219.voltage_to_pct(3.3, vmin, vmax) == 25, "quarter -> 25%"
    assert ina219.voltage_to_pct(3.9, vmin, vmax) == 75, "three-quarter -> 75%"
    assert ina219.voltage_to_pct(3.66, vmin, vmax) == 55, "0.55 -> 55%"
    # degenerate config guard
    assert ina219.voltage_to_pct(3.6, 4.0, 4.0) == 0, "v_max<=v_min guarded"
    # piecewise-linear curve path (overrides the v_min/v_max linear map)
    curve = ina219.DEFAULT_LIPO_CURVE
    assert ina219.voltage_to_pct(4.20, vmin, vmax, curve) == 100, "curve top -> 100"
    assert ina219.voltage_to_pct(3.30, vmin, vmax, curve) == 0, "curve floor -> 0"
    assert ina219.voltage_to_pct(2.0, vmin, vmax, curve) == 0, "below curve -> floor"
    assert ina219.voltage_to_pct(5.0, vmin, vmax, curve) == 100, "above curve -> top"
    assert ina219.voltage_to_pct(3.83, vmin, vmax, curve) == 50, "curve knee -> 50"
    assert ina219.voltage_to_pct(3.85, vmin, vmax, curve) == 55, "interp 3.85 -> 55"
    assert ina219.voltage_to_pct(3.5, vmin, vmax, ((3.0, 0), (4.0, 100))) == 50, "2pt midpoint"
    # low threshold
    assert ina219.is_low(15, 15) is True, "at threshold is low"
    assert ina219.is_low(10, 15) is True, "below threshold is low"
    assert ina219.is_low(16, 15) is False, "above threshold not low"


def test_draw_battery():
    # on battery, mid charge -> percent number + glyph, no '+' prefix
    c = RecordingCanvas(); c.red = c
    render.draw_battery(c, 50, True)
    assert c.has_text("50", color=INK), "shows the percent number"
    assert not c.has_text("+50"), "no '+' when on battery"
    assert any(o[0] == "rect" for o in c.ops), "draws a glyph rect"
    # wired -> '+' prefix as a charging cue
    cw = RecordingCanvas(); cw.red = cw
    render.draw_battery(cw, 80, False)
    assert cw.has_text("+80", color=INK), "'+' prefix when wired"
    # None pct -> nothing drawn
    cn = RecordingCanvas(); cn.red = cn
    render.draw_battery(cn, None, True)
    assert cn.ops == [], "no pct -> no draw"
    # fill width tracks charge: 100% -> full bar, 0% -> no bar
    cf = RecordingCanvas(); cf.red = cf
    render.draw_battery(cf, 100, True)
    assert cf.has_rect(228, 113, 14, 5, INK, True), "100% -> full fill bar"
    ce = RecordingCanvas(); ce.red = ce
    render.draw_battery(ce, 0, True)
    assert not any(o[0] == "rect" and o[1] == 228 and o[2] == 113 for o in ce.ops), "0% -> no fill bar"
    # low -> drawn on the red plane (here red aliases the same recorder)
    cl = RecordingCanvas(); cl.red = cl
    render.draw_battery(cl, 40, True, low=True)
    assert cl.has_text("40", color=INK), "low badge still renders"


def test_on_battery_detection():
    # discharging (negative shunt, default sign) => on battery
    assert ina219.is_on_battery(-15.0, 1, 2.0) is True, "discharge -> on battery"
    assert ina219.is_on_battery(-2.5, 1, 2.0) is True, "small discharge past noise"
    # charging / plugged (positive shunt) => not on battery
    assert ina219.is_on_battery(20.0, 1, 2.0) is False, "charging -> plugged"
    # idle/full plugged: near-zero current stays plugged (avoids false deepsleep)
    assert ina219.is_on_battery(0.5, 1, 2.0) is False, "near-zero -> plugged"
    assert ina219.is_on_battery(-1.0, 1, 2.0) is False, "within noise band -> plugged"
    # reversed shunt wiring: charge_sign=-1 flips the convention
    assert ina219.is_on_battery(15.0, -1, 2.0) is True, "flipped sign: +shunt is discharge"
    assert ina219.is_on_battery(-15.0, -1, 2.0) is False, "flipped sign: -shunt is charge"


# --------------------------------------------------------------------------
# 2. ETag no-op decision
# --------------------------------------------------------------------------
def test_etag_decision():
    # 304 -> never touch the panel
    assert poller.decide_action(304, None, "abc") == ("skip", "abc")
    # 200 with the same etag -> still skip (belt and braces)
    assert poller.decide_action(200, "abc", "abc") == ("skip", "abc")
    # 200 with a new etag -> render + adopt the new etag
    assert poller.decide_action(200, "def", "abc") == ("render", "def")
    # first fetch (no prior etag) -> render
    assert poller.decide_action(200, "def", "") == ("render", "def")
    # errors -> offline, keep the old etag
    assert poller.decide_action(500, None, "abc") == ("offline", "abc")
    assert poller.decide_action(401, None, "x") == ("offline", "x")
    assert poller.decide_action(404, None, "") == ("offline", "")


# --------------------------------------------------------------------------
# 2b. Remote poll-interval clamp (control.poll_interval; never trust the server)
# --------------------------------------------------------------------------
def test_poll_interval_clamp():
    # in-band values pass through unchanged, incl. the exact boundaries
    assert poller.clamp_interval(120) == 120, "in-band unchanged"
    assert poller.clamp_interval(30) == 30, "lower bound kept"
    assert poller.clamp_interval(3600) == 3600, "upper bound kept"
    # below the floor -> clamped up to 30
    assert poller.clamp_interval(29) == 30, "just below floor -> 30"
    assert poller.clamp_interval(5) == 30, "well below floor -> 30"
    assert poller.clamp_interval(0) == 30, "zero -> 30"
    assert poller.clamp_interval(-50) == 30, "negative -> 30"
    # above the ceiling -> clamped down to 3600
    assert poller.clamp_interval(3601) == 3600, "just above ceiling -> 3600"
    assert poller.clamp_interval(99999) == 3600, "well above ceiling -> 3600"
    # liberal coercion of numeric-ish inputs
    assert poller.clamp_interval("300") == 300, "numeric string coerced"
    assert poller.clamp_interval(45.0) == 45, "float coerced to int"
    # non-int / missing -> None so the caller keeps its configured default
    assert poller.clamp_interval(None) is None, "missing -> None"
    assert poller.clamp_interval("soon") is None, "non-numeric string -> None"
    assert poller.clamp_interval({}) is None, "wrong type (dict) -> None"
    assert poller.clamp_interval([30]) is None, "wrong type (list) -> None"


# --------------------------------------------------------------------------
# 2c. Schema-version guard (a future pico-paper.v2 must NOT hit v1 renderers)
# --------------------------------------------------------------------------
def test_schema_version_guard():
    V1 = "pico-paper.v1"
    # v1 body, new etag -> render normally
    assert poller.decide_action(200, "new", "old", V1) == ("render", "new")
    # v1 body, same etag -> still de-dupes to skip
    assert poller.decide_action(200, "same", "same", V1) == ("skip", "same")
    # a future/unknown schema on a 200 -> offline (do NOT render against v1)
    assert poller.decide_action(200, "new", "old", "pico-paper.v2") == ("offline", "old")
    assert poller.decide_action(200, "new", "old", "pico-paper.v99") == ("offline", "old")
    # schema omitted (None) -> backwards-compatible, behaves exactly as before
    assert poller.decide_action(200, "new", "old", None) == ("render", "new")
    assert poller.decide_action(200, "new", "old") == ("render", "new")
    # 304 short-circuits before the schema check (no body to inspect)
    assert poller.decide_action(304, None, "keep", "pico-paper.v2") == ("skip", "keep")


# --------------------------------------------------------------------------
# 2d. Telemetry query-string construction (?batt=&rssi=&fw=&up=)
# --------------------------------------------------------------------------
def test_telemetry_query_string():
    # full set, fixed key order: batt, rssi, fw, up
    q = poller.build_query({"batt": 83, "rssi": -61, "fw": "pt-1.0.0", "up": 43200})
    assert q == "?batt=83&rssi=-61&fw=pt-1.0.0&up=43200", "full ordered query"
    # rssi unavailable (None) is omitted; the rest keep their order
    assert poller.build_query({"batt": 50, "rssi": None, "fw": "pt-1.0.0", "up": 10}) \
        == "?batt=50&fw=pt-1.0.0&up=10", "None rssi dropped"
    # battery gauge unreadable (None batt) dropped; up=0 is still a real value
    assert poller.build_query({"batt": None, "rssi": -40, "fw": "x", "up": 0}) \
        == "?rssi=-40&fw=x&up=0", "None batt dropped, up=0 still sent"
    # ints coerced from float/string
    assert poller.build_query({"batt": 77.0, "up": "5"}) == "?batt=77&up=5", "coercion"
    # fw sanitised to the [A-Za-z0-9._-] charset, capped at 16 chars
    assert poller.build_query({"fw": "v1.2 beta/!"}) == "?fw=v1.2beta", "fw charset stripped"
    assert poller.build_query({"fw": "a" * 40}) == "?fw=" + "a" * 16, "fw capped at 16"
    # an fw that sanitises to empty contributes no key at all
    assert poller.build_query({"fw": "!!!"}) == "", "all-bad fw -> dropped"
    # nothing to send -> empty string (never a bare '?')
    assert poller.build_query({}) == "", "empty dict -> no query"
    assert poller.build_query(None) == "", "None telemetry -> no query"


# --------------------------------------------------------------------------
# 3a. clip / wrap helpers
# --------------------------------------------------------------------------
def test_clip():
    assert render.clip("hello", 10) == "hello", "short unchanged"
    assert render.clip("exactly!", 8) == "exactly!", "len==N unchanged"
    assert render.clip("HelloWorld", 8) == "Hello...", "truncate -> trailing ..."
    assert len(render.clip("HelloWorld", 8)) == 8, "clip length == N"
    assert render.clip("abc", 3) == "abc", "len==N==3 unchanged"
    assert render.clip("abcd", 3) == "abc", "N<=3 hard cut, no dots"
    assert render.clip("", 5) == "", "empty stays empty"
    assert render.clip(None, 5) == "", "None coerced to empty"


def test_wrap():
    assert render.wrap("a b c", 30, 4) == ["a b c"], "fits one line"
    assert render.wrap("aaaa bbbb cccc", 9, 4) == ["aaaa bbbb", "cccc"], "greedy fill"
    # long word hard-split
    assert render.wrap("abcdefghij", 4, 4) == ["abcd", "efgh", "ij"], "hard split"
    # overflow -> last shown line clipped with trailing ...
    out = render.wrap("one two three four five six seven", 5, 2)
    assert len(out) == 2, "capped at L lines"
    assert out[0] == "one", "first line greedy"
    assert out[-1].endswith("..."), "overflow line gets ..."
    assert all(len(line) <= 5 for line in out), "every line within width"


# --------------------------------------------------------------------------
# 3b. per-layout field selection + truncation (recording canvas)
# --------------------------------------------------------------------------
def test_status_card_fields():
    c = RecordingCanvas()
    render.render_status_card(c, {
        "title": "Home Server Rack",          # 16 chars -> clip12
        "status": "DOWN!!!!",                  # 8 chars  -> unchanged, right-aligned
        "subtitle": "s" * 40,                  # -> clip30
        "lines": ["L0", "L1", "L2", "L3", "L4", "L5", "L6"],  # only 5 shown
        "footer": "f" * 40,                    # -> clip30
    })
    # title S2 at (4,2), clipped to 12 with trailing ...
    assert c.has_text("Home Serv...", 4, 2, INK, 2), "title clip12 @ (4,2) S2"
    # status S1 right-aligned: x = 246 - 8*len(status) = 246-64 = 182
    assert c.has_text("DOWN!!!!", 182, 6, INK, 1), "status right-aligned @ x=182"
    # subtitle clip30 at (4,25)
    assert c.has_text("s" * 27 + "...", 4, 25, INK, 1), "subtitle clip30 @ (4,25)"
    # exactly 5 lines, at y = 40,51,62,73,84
    for i in range(5):
        assert c.has_text("L%d" % i, 4, 40 + 11 * i, INK, 1), "line %d row" % i
    assert not c.has_text("L5"), "6th line dropped"
    assert not c.has_text("L6"), "7th line dropped"
    # footer clip30 at (4,110)
    assert c.has_text("f" * 27 + "...", 4, 110, INK, 1), "footer clip30 @ (4,110)"
    # separators
    assert c.has_hline(20) and c.has_hline(100), "both rules drawn"
    # cleared to PAPER first
    assert c.ops[0] == ("fill", PAPER), "frame cleared to PAPER"


def test_metric_centering():
    c = RecordingCanvas()
    render.render_metric(c, {
        "label": "Solar output",
        "value": "3.42",                       # 4 chars -> value_px=128
        "unit": "kW",                          # 2 chars -> unit_px=32, gap=6
        "trend": "UP",
        "footer": "inverter-A",
    })
    # group=128+6+32=166 -> x_v=(250-166)//2=42; x_u=42+128+6=176
    assert c.has_text("3.42", 42, 34, INK, 4), "value S4 centered @ x_v=42"
    assert c.has_text("kW", 176, 50, INK, 2), "unit S2 @ x_u=176 y=50"
    # trend centered: x_t=(250-8*2)//2=117
    assert c.has_text("UP", 117, 82, INK, 1), "trend centered @ x_t=117"
    assert c.has_hline(18) and c.has_hline(100), "metric rules"

    # value truncation to 7 + empty unit (no unit op, gap=0)
    c2 = RecordingCanvas()
    render.render_metric(c2, {"label": "x", "value": "1234567890",
                              "unit": "", "trend": "FLAT", "footer": "y"})
    # clip("1234567890",7) = "1234..." ; value_px=32*7=224 ; x_v=(250-224)//2=13
    assert c2.has_text("1234...", 13, 34, INK, 4), "value clip7 centered, no unit"
    assert not any(o[5] == 2 for o in c2.texts()), "no S2 unit op when unit empty"


def test_list_fields():
    c = RecordingCanvas()
    render.render_list(c, {
        "title": "Shopping",
        "items": ["X" * 30, "i1", "i2", "i3", "i4", "i5", "i6", "i7"],  # 6 shown
        "footer": "8 items",
    })
    # checkbox + first item at row y=26; item clipped to 26 chars
    assert c.has_text("[ ] ", 4, 26, INK, 1), "decorative checkbox @ (4,26)"
    assert c.has_text("X" * 23 + "...", 36, 26, INK, 1), "item clip26 @ (36,26)"
    # six checkbox rows at y = 26,38,50,62,74,86
    for i in range(6):
        assert c.has_text("[ ] ", 4, 26 + 12 * i, INK, 1), "checkbox row %d" % i
    # 7th/8th items dropped
    assert not c.has_text("i6") and not c.has_text("i7"), "items beyond 6 dropped"


def test_alert_high_inversion():
    c = RecordingCanvas()
    render.render_alert(c, {
        "severity": "high",
        "title": "Water Leak",
        "message": "Sensor under the sink detected moisture everywhere now",
        "footer": "basement-sensor-3",
    })
    # solid INK banner rect (filled) + label drawn in PAPER
    assert c.has_rect(0, 0, 250, 28, INK, True), "high banner is solid INK fill"
    assert c.has_text("!! HIGH", 4, 6, PAPER, 2), "high label drawn in PAPER"
    # 2px whole-screen frame = two outlines
    assert c.has_rect(0, 0, 250, 122, INK, False), "outer frame outline"
    assert c.has_rect(1, 1, 248, 120, INK, False), "inner frame outline"
    # title S2 at (4,34)
    assert c.has_text("Water Leak", 4, 34, INK, 2), "title @ (4,34) S2"
    # message wraps to <=4 lines starting y=58 step 11
    msg_ys = [o[3] for o in c.texts() if o[3] in (58, 69, 80, 91)]
    assert len(msg_ys) >= 1, "message wrapped into the message band"
    assert c.has_hline(100), "footer rule always drawn"


def test_alert_low_no_inversion():
    c = RecordingCanvas()
    render.render_alert(c, {"severity": "low", "title": "Door",
                            "message": "open", "footer": "x"})
    assert c.has_text("LOW", 4, 6, INK, 2), "low label INK (not inverted)"
    assert c.has_hline(28), "low draws underline at y=28"
    assert not c.has_rect(0, 0, 250, 28, INK, True), "low has no solid banner"
    assert not c.has_rect(0, 0, 250, 122, INK, False), "low has no frame"


def test_alert_severity_labels():
    for sev, label in (("low", "LOW"), ("med", "MED"), ("high", "!! HIGH")):
        c = RecordingCanvas()
        render.render_alert(c, {"severity": sev, "title": "t",
                                "message": "m", "footer": "f"})
        col = PAPER if sev == "high" else INK
        assert c.has_text(label, 4, 6, col, 2), "severity %s -> %r" % (sev, label)


def test_dispatch_and_offline():
    # unknown layout -> offline screen (status_card under the hood), returns False
    c = RecordingCanvas()
    ok = render.render(c, "totally_bogus", {})
    assert ok is False, "unknown layout returns False"
    assert c.has_text("Offline", 4, 2, INK, 2), "offline title rendered"
    # known layout dispatches and returns True
    c2 = RecordingCanvas()
    ok2 = render.render(c2, "metric", {"label": "a", "value": "1",
                                       "unit": "", "trend": "", "footer": ""})
    assert ok2 is True, "known layout returns True"


def test_qr_end_to_end():
    # Exercises the real vendored uQR path: matrix -> module rects + wrapped caption.
    c = RecordingCanvas()
    render.render_qr(c, {
        "title": "Guest WiFi",
        "qr_data": "WIFI:T:WPA;S:GuestNet;P:welcome123;;",
        "caption": "Scan to join GuestNet now please",
    })
    assert c.has_text("Guest WiFi", 4, 2, INK, 2), "qr title @ (4,2) S2"
    module_rects = [o for o in c.ops if o[0] == "rect" and o[6] is True]
    assert len(module_rects) > 20, "QR modules drawn as filled INK rects"
    # caption wraps at width 17 beside the QR, first line at (104,30)
    cap = [o for o in c.texts() if o[2] == 104 and o[3] == 30]
    assert len(cap) == 1 and len(cap[0][1]) <= 17, "caption first line @ (104,30) <=17"


# --------------------------------------------------------------------------
# 4. OTA pure logic (device-side updater -- ota.py)
# --------------------------------------------------------------------------
def test_ota_manifest_diff():
    # pull only the changed (sha differs) + the brand-new paths; delete only the
    # path that left the manifest. Unchanged paths are left alone.
    local = {"main.py": "aaa", "render.py": "bbb", "wifi.py": "ccc", "old.py": "ddd"}
    server = {"main.py": "aaa",        # unchanged -> NOT pulled
              "render.py": "BBB",      # sha changed -> pull
              "wifi.py": "ccc",        # unchanged -> NOT pulled
              "new.py": "eee"}         # new path -> pull   (old.py -> delete)
    pull, delete = ota.manifest_diff(local, server)
    assert pull == ["new.py", "render.py"], "pull only changed+new, sorted: %r" % (pull,)
    assert delete == ["old.py"], "delete only the removed path: %r" % (delete,)

    # nested lib/ path: a changed sha there is pulled like any other.
    pull2, _ = ota.manifest_diff({"lib/uQR.py": "1"}, {"lib/uQR.py": "2"})
    assert pull2 == ["lib/uQR.py"], "changed lib file pulled: %r" % (pull2,)

    # NEVER delete device-local / runtime files, even if absent from the server
    # manifest: config.py, secrets.py, manifest.json, and any *.txt backstop.
    local3 = {"config.py": "x", "secrets.py": "y", "manifest.json": "z",
              "last_etag.txt": "t", "boot_count.txt": "b", "bad_fw.txt": "q",
              "gone.py": "g"}
    pull3, delete3 = ota.manifest_diff(local3, {})
    assert pull3 == [], "nothing to pull from an empty server manifest"
    assert delete3 == ["gone.py"], "protected files kept; only gone.py deleted: %r" % (delete3,)

    # None / empty inputs are safe and symmetric.
    assert ota.manifest_diff(None, None) == ([], []), "None inputs -> empty plan"
    assert ota.manifest_diff({}, {"a": "1"}) == (["a"], []), "all-new -> pull, no delete"
    assert ota.manifest_diff({"a": "1"}, {}) == ([], ["a"]), "all-removed -> delete, no pull"


def test_ota_protected_never_pulled():
    # Defense-in-depth: even if the server manifest advertises a CHANGED sha for a
    # protected path, the delta planner must NEVER pull it (config.py/secrets.py are
    # device-local; boot.py is the immutable recovery guard; manifest.json/*.txt are
    # device-managed). This mirrors -- but does not trust -- the server's exclusion.
    local = {"main.py": "aaa", "config.py": "c0", "secrets.py": "s0",
             "boot.py": "b0", "manifest.json": "m0", "last_etag.txt": "t0",
             "pending_fw.txt": "p0"}
    server = {"main.py": "AAA",          # ordinary file changed -> pulled
              "config.py": "c1",         # device-local creds -> NOT pulled
              "secrets.py": "s1",        # device-local creds -> NOT pulled
              "boot.py": "b1",           # immutable recovery guard -> NOT pulled
              "manifest.json": "m1",     # local commit point -> NOT pulled
              "last_etag.txt": "t1",     # *.txt backstop -> NOT pulled
              "pending_fw.txt": "p1"}    # *.txt backstop -> NOT pulled
    pull, delete = ota.manifest_diff(local, server)
    assert pull == ["main.py"], "only the ordinary changed file is pulled: %r" % (pull,)
    assert delete == [], "no deletes -- every local path still on the server: %r" % (delete,)

    # boot.py BRAND-NEW on the server (not present locally) is STILL never pulled --
    # OTA can never lay down a boot.py, it exists at flash time only.
    pull2, _ = ota.manifest_diff({}, {"boot.py": "b1", "app.py": "a1"})
    assert pull2 == ["app.py"], "boot.py never pulled even when new: %r" % (pull2,)

    # The protected set: device-local identity/creds, the local manifest, the
    # immutable recovery guard, and any *.txt backstop (basename match, any dir).
    for p in ("config.py", "secrets.py", "secrets.example.py", "boot.py",
              "manifest.json", "last_etag.txt", "pending_fw.txt", "lib/boot.py"):
        assert ota._is_protected(p) is True, "%s must be protected" % p
    for p in ("main.py", "render.py", "wifi.py", "lib/uQR.py"):
        assert ota._is_protected(p) is False, "%s must NOT be protected" % p


def test_ota_should_update():
    assert ota.should_update("abc123", "abc123") is False, "equal version -> skip"
    assert ota.should_update("def456", "abc123") is True, "different version -> update"
    assert ota.should_update("def456", None) is True, "no local manifest yet -> update"
    assert ota.should_update(None, "abc123") is False, "no advert (None) -> skip"
    assert ota.should_update("", "abc123") is False, "empty advert -> skip"


def test_ota_verify():
    data = b"papertrail firmware bytes \x00\x01\x02"
    good = hashlib.sha256(data).hexdigest()
    assert ota.verify(data, good) is True, "matching sha256 verifies"
    assert ota.verify(data, good.upper()) is True, "sha compare is case-insensitive"
    assert ota.verify(data, "00" + good[2:]) is False, "wrong sha rejected"
    assert ota.verify(b"tampered", good) is False, "tampered bytes rejected"
    assert ota.verify(data, None) is False, "missing sha rejected (never write unverified)"
    assert ota.verify(data, "") is False, "empty sha rejected"


def test_ota_crash_loop():
    # boot.py increments boot_count each boot; counter <= cap is fine, > cap restores.
    assert ota.should_restore(0, 3) is False, "fresh boot -> ok"
    assert ota.should_restore(1, 3) is False, "one boot -> ok"
    assert ota.should_restore(3, 3) is False, "exactly at the cap (<=3) -> still ok"
    assert ota.should_restore(4, 3) is True, "one past the cap (>3) -> restore /backup"
    assert ota.should_restore(99, 3) is True, "deep crash loop -> restore"
    assert ota.should_restore(None, 3) is False, "unreadable counter -> don't restore"


TESTS = [
    test_battery_curve,
    test_draw_battery,
    test_etag_decision,
    test_poll_interval_clamp,
    test_schema_version_guard,
    test_telemetry_query_string,
    test_ota_manifest_diff,
    test_ota_protected_never_pulled,
    test_ota_should_update,
    test_ota_verify,
    test_ota_crash_loop,
    test_clip,
    test_wrap,
    test_status_card_fields,
    test_metric_centering,
    test_list_fields,
    test_alert_high_inversion,
    test_alert_low_no_inversion,
    test_alert_severity_labels,
    test_dispatch_and_offline,
    test_qr_end_to_end,
    test_on_battery_detection,
]


def main():
    passed = 0
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS  " + t.__name__)
            passed += 1
        except AssertionError as e:
            print("FAIL  " + t.__name__ + ": " + str(e))
            failed += 1
        except Exception as e:
            print("ERROR " + t.__name__ + ": " + repr(e))
            failed += 1
    print("\n%d passed, %d failed, %d total" % (passed, failed, len(TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
