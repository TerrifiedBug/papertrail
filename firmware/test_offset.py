# Calibrate EPAPER_Y_OFFSET. Loop:
#   1. edit EPAPER_Y_OFFSET in config.py
#   2. upload config.py to the Pico
#   3. MicroPico "Run" this file
#   4. look at the panel; repeat until the top isn't clipped AND the bottom isn't.
# Renders a test alert directly via the real render path (FrameCanvas applies the
# offset to both planes) -- no network, no ETag cache to fight.

import config
import render
import epaper2in13b

off = getattr(config, "EPAPER_Y_OFFSET", 0)
epd = epaper2in13b.get_epd()
render.draw_to_epd(epd, "alert", {
    "severity": "high",
    "title": "TOP MARGIN",
    "message": "top edge clear? bottom edge clear?",
    "footer": "y_offset = %d" % off,
})
epd.sleep()
print("rendered with EPAPER_Y_OFFSET =", off)
