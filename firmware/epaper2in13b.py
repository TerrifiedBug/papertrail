# Waveshare Pico-ePaper-2.13-B **V4** (tri-color Black/White/Red, SSD1680) driver.
#
# VENDORED from waveshareteam/Pico_ePaper_Code (python/Pico_ePaper-2.13-B_V4.py, MIT).
# Kept the LANDSCAPE class only (250x122 -- identical geometry to our mono design, so
# all papertrail layouts carry over). Changes from upstream:
#   * SPI(1) built with miso=None so it never claims GP8 (DC pin = SPI1 default MISO).
#   * cleaned the upstream `buffer_balck` typo -> buffer_black.
# Panel: 250(w) x 122(h) landscape, two planes. display() writes BW RAM (0x24) AND
# RED RAM (0x26) -- writing only 0x24 (as the mono driver does) leaves the red plane
# full of garbage, which is why the mono driver turned this panel solid red.
# Full refresh only (~5-10s); NO partial refresh. Busy is ACTIVE-HIGH (1=busy).
# Pins match config.EPAPER: RST=12 DC=8 CS=9 BUSY=13, SPI1 (SCK=10, MOSI=11).

from machine import Pin, SPI
import framebuf
import utime

EPD_WIDTH  = 122
EPD_HEIGHT = 250

RST_PIN  = 12
DC_PIN   = 8
CS_PIN   = 9
BUSY_PIN = 13
SCK_PIN  = 10
MOSI_PIN = 11


class EPD_2in13_B_V4_Landscape:
    def __init__(self):
        self.reset_pin = Pin(RST_PIN, Pin.OUT)
        self.busy_pin = Pin(BUSY_PIN, Pin.IN, Pin.PULL_UP)
        self.cs_pin = Pin(CS_PIN, Pin.OUT)
        if EPD_WIDTH % 8 == 0:
            self.width = EPD_WIDTH
        else:
            self.width = (EPD_WIDTH // 8) * 8 + 8
        self.height = EPD_HEIGHT

        # miso=None: keep SPI1 off GP8 (DC). Panel is write-only.
        self.spi = SPI(1, baudrate=4000_000, sck=Pin(SCK_PIN), mosi=Pin(MOSI_PIN), miso=None)
        self.dc_pin = Pin(DC_PIN, Pin.OUT)

        self.buffer_black = bytearray(self.height * self.width // 8)
        self.buffer_red = bytearray(self.height * self.width // 8)
        # Landscape view: framebuf is (height x width) = 250 x 122.
        self.imageblack = framebuf.FrameBuffer(self.buffer_black, self.height, self.width, framebuf.MONO_VLSB)
        self.imagered = framebuf.FrameBuffer(self.buffer_red, self.height, self.width, framebuf.MONO_VLSB)
        self.init()

    def digital_write(self, pin, value):
        pin.value(value)

    def digital_read(self, pin):
        return pin.value()

    def delay_ms(self, delaytime):
        utime.sleep(delaytime / 1000.0)

    def spi_writebyte(self, data):
        self.spi.write(bytearray(data))

    def module_exit(self):
        self.digital_write(self.reset_pin, 0)

    def reset(self):
        self.digital_write(self.reset_pin, 1)
        self.delay_ms(50)
        self.digital_write(self.reset_pin, 0)
        self.delay_ms(2)
        self.digital_write(self.reset_pin, 1)
        self.delay_ms(50)

    def send_command(self, command):
        self.digital_write(self.dc_pin, 0)
        self.digital_write(self.cs_pin, 0)
        self.spi_writebyte([command])
        self.digital_write(self.cs_pin, 1)

    def send_data(self, data):
        self.digital_write(self.dc_pin, 1)
        self.digital_write(self.cs_pin, 0)
        self.spi_writebyte([data])
        self.digital_write(self.cs_pin, 1)

    def send_data1(self, buf):
        self.digital_write(self.dc_pin, 1)
        self.digital_write(self.cs_pin, 0)
        self.spi.write(bytearray(buf))
        self.digital_write(self.cs_pin, 1)

    def ReadBusy(self):
        print('busy')
        while self.digital_read(self.busy_pin) == 1:   # 1: busy, 0: idle (active-high)
            self.delay_ms(10)
        print('busy release')
        self.delay_ms(20)

    def TurnOnDisplay(self):
        self.send_command(0x20)  # Activate Display Update Sequence
        self.ReadBusy()

    def SetWindows(self, Xstart, Ystart, Xend, Yend):
        self.send_command(0x44)
        self.send_data((Xstart >> 3) & 0xFF)
        self.send_data((Xend >> 3) & 0xFF)
        self.send_command(0x45)
        self.send_data(Ystart & 0xFF)
        self.send_data((Ystart >> 8) & 0xFF)
        self.send_data(Yend & 0xFF)
        self.send_data((Yend >> 8) & 0xFF)

    def SetCursor(self, Xstart, Ystart):
        self.send_command(0x4E)
        self.send_data(Xstart & 0xFF)
        self.send_command(0x4F)
        self.send_data(Ystart & 0xFF)
        self.send_data((Ystart >> 8) & 0xFF)

    def init(self):
        print('init')
        self.reset()
        self.ReadBusy()
        self.send_command(0x12)  # SWRESET
        self.ReadBusy()

        self.send_command(0x01)  # Driver output control
        self.send_data(0xf9)
        self.send_data(0x00)
        self.send_data(0x00)

        self.send_command(0x11)  # data entry mode (landscape)
        self.send_data(0x07)

        self.SetWindows(0, 0, self.width - 1, self.height - 1)
        self.SetCursor(0, 0)

        self.send_command(0x3C)  # BorderWaveform
        self.send_data(0x05)

        self.send_command(0x18)  # built-in temperature sensor
        self.send_data(0x80)

        self.send_command(0x21)  # Display update control
        self.send_data(0x80)
        self.send_data(0x80)

        self.ReadBusy()
        return 0

    def display(self):
        self.send_command(0x24)  # BW RAM
        for j in range(int(self.width / 8) - 1, -1, -1):
            for i in range(0, self.height):
                self.send_data(self.buffer_black[i + j * self.height])
        self.send_command(0x26)  # RED RAM
        for j in range(int(self.width / 8) - 1, -1, -1):
            for i in range(0, self.height):
                self.send_data(self.buffer_red[i + j * self.height])
        self.TurnOnDisplay()

    def Clear(self, colorblack, colorred):
        self.send_command(0x24)
        self.send_data1([colorblack] * self.height * int(self.width / 8))
        self.send_command(0x26)
        self.send_data1([colorred] * self.height * int(self.width / 8))
        self.TurnOnDisplay()

    def sleep(self):
        self.send_command(0x10)  # deep sleep
        self.send_data(0x01)
        self.delay_ms(2000)
        self.module_exit()


def get_epd():
    """Landscape 250x122 tri-color panel."""
    return EPD_2in13_B_V4_Landscape()
