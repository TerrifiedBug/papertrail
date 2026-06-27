# On-device smoke test for the Pico-ePaper-2.13-B V4 (tri-color, 250x122 landscape).
# Run with MicroPico "Run current file" (play button). Confirms the driver drives
# THIS panel: correct resolution, black plane, RED plane.
#
# Expect on the panel (landscape, wide):
#   - a thin BLACK border framing the WHOLE screen (proves 250x122 extent)
#   - black text "PAPERTRAIL  2.13-B V4" near the top-left
#   - a solid RED bar with RED text under it
# If garbled/clipped/doubled, or red shows as black, tell me what you see.

from epaper2in13b import get_epd

epd = get_epd()                       # EPD_2in13_B_V4_Landscape, 250x122
epd.imageblack.fill(0xff)             # 0xff = white background
epd.imagered.fill(0xff)

# black plane: border + text (0x00 = draw black)
epd.imageblack.rect(0, 0, 250, 122, 0x00)         # full-extent border
epd.imageblack.text("PAPERTRAIL  2.13-B V4", 6, 8, 0x00)
epd.imageblack.text("black plane OK", 6, 26, 0x00)

# red plane: a solid bar + separate text, no overlap (0x00 = draw red)
epd.imagered.fill_rect(6, 52, 130, 24, 0x00)      # solid RED bar
epd.imagered.text("RED TEXT OK", 6, 92, 0x00)     # RED text, clear of the bar

epd.display()
print("panel test sent; tri-color full refresh ~5-10s. Do NOT Ctrl-C.")
epd.sleep()
print("panel test DONE")
