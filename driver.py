#!/usr/bin/env python3
"""Experimental Linux driver / diagnostic harness for VINSA 1060 Plus (08f2:6811).

This version deliberately follows the proven upstream F33RNI USB lifecycle:
reset -> detach kernel HID -> set configuration -> claim interface 2 ->
SET_REPORT sequence -> read endpoint 0x84 from interface 1.

It adds diagnostic logging and a permissive action mode for studying pen
up/hover/down transitions without silently dropping unknown action codes.
"""
from __future__ import annotations

import argparse
import logging
import signal
import time
from array import array
from dataclasses import dataclass
from typing import Optional

import usb
import usb.core
import usb.util
from evdev import AbsInfo, UInput, ecodes

VID = 0x08F2
PID = 0x6811
CONTROL_IFACE = 2
DATA_IFACE = 1
DATA_EP = 0x84
DATA_SIZE = 64

T501_REPORTS = (
    (0x0308, bytes.fromhex("08 04 1d 01 ff ff 06 2e")),
    (0x0308, bytes.fromhex("08 03 00 ff f0 00 ff f0")),
    (0x0308, bytes.fromhex("08 06 01 00 00 00 00 00")),
    (0x0308, bytes.fromhex("08 03 00 ff f0 00 ff f0")),
)

# Tablet hardware-button state from bytes 11:13 of the 64-byte T501 report.
# 0xFF33 is the idle value; buttons pull exactly one bit low.
TABLET_KEY_IDLE = 0xFF33

# The supplied test log contains 12 button states. Their order matches the
# labels supplied with the log: e, b, ctrl-, ctrl+, [, ], wheel up, tab,
# wheel down, space, ctrl, alt.
TABLET_BUTTONS = {
    0xFF31: "e",
    0xFF23: "b",
    0x7F33: "ctrl-",
    0xFF32: "ctrl+",
    0xBF33: "[",
    0xFF13: "]",
    0xDF33: "wheel_up",
    0xFE33: "tab",
    0xEF33: "wheel_down",
    0xFD33: "space",
    0xF733: "ctrl",
    0xFB33: "alt",
}


@dataclass
class Config:
    vendor_id: int = VID
    product_id: int = PID
    min_x: int = 0
    max_x: int = 4095
    min_y: int = 0
    max_y: int = 4095
    swap_axes: bool = False
    invert_x: bool = False
    invert_y: bool = False
    pressure_in_min: int = 1560   # light/near-zero contact, measured on this unit
    pressure_in_max: int = 1050   # hard press, measured on this unit
    pressure_out_min: int = 0
    pressure_out_max: int = 2047
    pressure_threshold_press: int = 60    # ~3% of 2047, above Wacom's default 27/2048 for margin
    pressure_threshold_release: int = 30  # hysteresis gap below press threshold
    resolution_x: int = 1
    resolution_y: int = 1
    resolution_pressure: int = 1
    timeout_ms: int = 250


class T501Parser:
    def __init__(self, cfg: Config, permissive_actions: bool, raw: bool):
        self.cfg = cfg
        self.permissive_actions = permissive_actions
        self.raw = raw
        self.touch = False
        self.last_action: Optional[int] = None

    @staticmethod
    def u16be(data: bytes | array, start: int) -> int:
        return (data[start] << 8) | data[start + 1]

    def pressure(self, raw: int) -> int:
        in_min = self.cfg.pressure_in_min
        in_max = self.cfg.pressure_in_max
        out_min = self.cfg.pressure_out_min
        out_max = self.cfg.pressure_out_max
        if in_max == in_min:
            return out_min
        value = (raw - in_min) * (out_max - out_min) / (in_max - in_min) + out_min
        return int(max(out_min, min(out_max, value)))

    def parse(self, data: bytes | array):
        if not isinstance(data, array):
            data = array('B', data)
        if len(data) < DATA_SIZE:
            logging.warning("short HID report: %d bytes", len(data))
            return None

        action = int(data[5])
        x0 = self.u16be(data, 1)
        y0 = self.u16be(data, 3)
        x = y0 if self.cfg.swap_axes else x0
        y = x0 if self.cfg.swap_axes else y0
        pressure_raw = self.u16be(data, 5)
        pen_btn = int(data[9])
        tablet_key = self.u16be(data, 11)

        accepted = action in (2, 3, 4, 5, 6, 7)
        if self.raw:
            logging.debug(
                "RAW action=%d x=%d y=%d pressure_raw=%d pen_btn=%d key=%d bytes=%s",
                action, x, y, pressure_raw, pen_btn, tablet_key,
                bytes(data).hex(' '),
            )
        if not accepted:
            logging.debug("UNKNOWN pen_action=%d", action)

        if self.cfg.invert_x:
            x = self.cfg.max_x - x
        if self.cfg.invert_y:
            y = self.cfg.max_y - y

        valid_position = (
            self.cfg.min_x <= x <= self.cfg.max_x
            and self.cfg.min_y <= y <= self.cfg.max_y
        )

        pressure = self.pressure(pressure_raw)
        previous_touch = self.touch
        # NOTE: `action` (data[5]) is literally the high byte of the same
        # 16-bit field as pressure_raw (data[5:7]) -- they are NOT
        # independent. At light/moderate pressure this byte reads 4/5/6;
        # at very hard presses the raw value drops low enough that this
        # same byte reads 3. There is no real "hover" action code on this
        # chip -- it simply stops sending data entirely once the pen is
        # far enough away (handled by the silence-timeout release in the
        # read loop). So touch state must be derived purely from the
        # mapped pressure value with hysteresis, never from `action`.
        if self.touch:
            if pressure < self.cfg.pressure_threshold_release:
                self.touch = False
        else:
            if pressure > self.cfg.pressure_threshold_press:
                self.touch = True

        if self.touch != previous_touch:
            logging.info(
                "TOUCH STATE %s -> %s (action=%d raw_pressure=%d pressure=%d)",
                previous_touch, self.touch, action, pressure_raw, pressure,
            )

        self.last_action = action
        return {
            "action": action,
            "accepted": accepted,
            "x": x,
            "y": y,
            "pressure_raw": pressure_raw,
            "pressure": pressure,
            "pen_btn": pen_btn,
            "tablet_key": tablet_key,
            "valid_position": valid_position,
            "touch": self.touch,
        }


class T501Driver:
    def __init__(self, cfg: Config, debug: bool, raw: bool, permissive: bool):
        self.cfg = cfg
        self.debug = debug
        self.raw = raw
        self.permissive = permissive
        self.dev: Optional[usb.core.Device] = None
        self.endpoint = None
        self.virtual_pen: Optional[UInput] = None
        self.virtual_keys: Optional[UInput] = None
        self.active_tablet_button: Optional[str] = None
        self.running = True
        self.parser = T501Parser(cfg, permissive, raw)
        self.read_count = 0
        self.timeout_count = 0
        self.consecutive_timeouts = 0
        self.HEARTBEAT_TIMEOUT_THRESHOLD = 3  # ~3 * timeout_ms of silence triggers a heartbeat

    def stop(self, *_):
        self.running = False

    def find(self):
        dev = usb.core.find(idVendor=self.cfg.vendor_id, idProduct=self.cfg.product_id)
        if dev is None:
            raise RuntimeError(f"USB device {self.cfg.vendor_id:04x}:{self.cfg.product_id:04x} not found")
        return dev

    def prepare_device(self):
        dev = self.find()
        self.dev = dev
        logging.info("Resetting USB device")
        try:
            dev.reset()
        except usb.core.USBError as exc:
            logging.warning("USB reset failed: %r", exc)

        # Match upstream lifecycle: detach all three interfaces first.
        logging.info("Detaching kernel driver from USB device")
        for iface in (0, 1, 2):
            try:
                active = dev.is_kernel_driver_active(iface)
            except (usb.core.USBError, NotImplementedError):
                active = False
            if active:
                logging.info("Detaching kernel driver from interface %d", iface)
                try:
                    dev.detach_kernel_driver(iface)
                except usb.core.USBError as exc:
                    logging.warning("detach interface %d failed: %r", iface, exc)

        # Match upstream: set configuration after detaching.
        logging.info("Setting new configuration")
        try:
            dev.set_configuration()
        except usb.core.USBError as exc:
            # Resource-busy can occur after reset/re-enumeration; keep going,
            # exactly as the known working driver does.
            logging.debug("set_configuration: %r", exc)

        # Match upstream exactly: claim ONLY interface 2.
        logging.info("Claiming USB interface %d", CONTROL_IFACE)
        usb.util.claim_interface(dev, CONTROL_IFACE)

        # The working driver reads the data endpoint from interface 1 but does
        # not explicitly claim that interface.
        cfg = dev.get_active_configuration()
        data_iface = usb.util.find_descriptor(cfg, bInterfaceNumber=DATA_IFACE)
        if data_iface is None:
            raise RuntimeError("T501 data interface 1 not found")
        endpoint = usb.util.find_descriptor(
            data_iface,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR
            ),
        )
        if endpoint is None:
            raise RuntimeError("T501 interrupt IN endpoint on interface 1 not found")
        self.endpoint = endpoint
        logging.info(
            "data endpoint iface=%d addr=0x%02x max_packet=%d interval=%d",
            DATA_IFACE, endpoint.bEndpointAddress, endpoint.wMaxPacketSize, endpoint.bInterval,
        )

    def enter_full_area(self):
        assert self.dev is not None
        for i, (w_value, report) in enumerate(T501_REPORTS, 1):
            logging.info("SET_REPORT #%d wValue=0x%04x data=%s", i, w_value, report.hex(' '))
            self.dev.ctrl_transfer(
                0x21, 0x09, w_value, CONTROL_IFACE, report, timeout=1000,
            )
            time.sleep(0.05)

        # Empirically, the mode switch only actually takes effect on this
        # tablet after a reset that happens AFTER the SET_REPORT sequence
        # (not before it). prepare_device() resets, re-detaches, re-claims
        # interface 2, and re-fetches the interface-1 endpoint -- all of
        # which become stale across a reset, so just re-run it.
        logging.info("Re-running prepare_device() after SET_REPORT sequence to latch full-area mode")
        time.sleep(0.3)
        self.prepare_device()

    def create_uinput(self):
        pen_caps = {
            ecodes.EV_KEY: [
                ecodes.BTN_TOOL_PEN,
                ecodes.BTN_TOUCH,
                ecodes.BTN_STYLUS,
                ecodes.BTN_STYLUS2,
            ],
            ecodes.EV_ABS: [
                (ecodes.ABS_X, AbsInfo(0, self.cfg.min_x, self.cfg.max_x, 0, 0, self.cfg.resolution_x)),
                (ecodes.ABS_Y, AbsInfo(0, self.cfg.min_y, self.cfg.max_y, 0, 0, self.cfg.resolution_y)),
                (ecodes.ABS_PRESSURE, AbsInfo(0, self.cfg.pressure_out_min, self.cfg.pressure_out_max, 0, 0, self.cfg.resolution_pressure)),
            ],
        }
        # Keep the pen as an INPUT_PROP_DIRECT absolute device.
        self.virtual_pen = UInput(
            pen_caps,
            name="VINSA-1060-T501",
            version=0x3,
            input_props=[ecodes.INPUT_PROP_DIRECT],
        )

        # Buttons are exposed through a separate virtual keyboard/mouse-wheel
        # device so REL_WHEEL never changes how the tablet pointer is classified.
        key_caps = {
            ecodes.EV_KEY: [
                ecodes.KEY_E,
                ecodes.KEY_B,
                ecodes.KEY_LEFTCTRL,
                ecodes.KEY_LEFTALT,
                ecodes.KEY_LEFTSHIFT,
                ecodes.KEY_LEFTBRACE,
                ecodes.KEY_RIGHTBRACE,
                ecodes.KEY_TAB,
                ecodes.KEY_SPACE,
                ecodes.KEY_MINUS,
                ecodes.KEY_EQUAL,
            ],
            ecodes.EV_REL: [
                ecodes.REL_WHEEL,
            ],
        }
        self.virtual_keys = UInput(
            key_caps,
            name="VINSA-1060-T501 Buttons",
            version=0x1,
        )
        logging.info("uinput created: VINSA-1060-T501 + VINSA-1060-T501 Buttons")

    def _button_press_events(self, name: str) -> list[tuple[int, int]]:
        if name == "e":
            return [(ecodes.EV_KEY, ecodes.KEY_E)]
        if name == "b":
            return [(ecodes.EV_KEY, ecodes.KEY_B)]
        if name == "ctrl-":
            return [
                (ecodes.EV_KEY, ecodes.KEY_LEFTCTRL),
                (ecodes.EV_KEY, ecodes.KEY_MINUS),
            ]
        if name == "ctrl+":
            # Physical '+' is SHIFT+EQUAL.
            return [
                (ecodes.EV_KEY, ecodes.KEY_LEFTCTRL),
                (ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT),
                (ecodes.EV_KEY, ecodes.KEY_EQUAL),
            ]
        if name == "[":
            return [(ecodes.EV_KEY, ecodes.KEY_LEFTBRACE)]
        if name == "]":
            return [(ecodes.EV_KEY, ecodes.KEY_RIGHTBRACE)]
        if name == "tab":
            return [(ecodes.EV_KEY, ecodes.KEY_TAB)]
        if name == "space":
            return [(ecodes.EV_KEY, ecodes.KEY_SPACE)]
        if name == "ctrl":
            return [(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL)]
        if name == "alt":
            return [(ecodes.EV_KEY, ecodes.KEY_LEFTALT)]
        return []

    def release_tablet_button(self, reason: str = ""):
        if self.virtual_keys is None:
            self.active_tablet_button = None
            return

        if self.active_tablet_button in ("wheel_up", "wheel_down"):
            # Wheel buttons are one-shot: there is no held EV_KEY to release.
            self.active_tablet_button = None
            return

        if self.active_tablet_button is not None:
            events = self._button_press_events(self.active_tablet_button)
            for ev_type, code in reversed(events):
                self.virtual_keys.write(ev_type, code, 0)
            self.virtual_keys.syn()
            logging.debug(
                "TABLET BUTTON UP %s%s",
                self.active_tablet_button,
                f" ({reason})" if reason else "",
            )

        self.active_tablet_button = None

    def handle_tablet_key(self, tablet_key: int):
        if self.virtual_keys is None:
            return

        name = TABLET_BUTTONS.get(tablet_key)

        if tablet_key == TABLET_KEY_IDLE:
            if self.active_tablet_button is not None:
                self.release_tablet_button("idle")
            return

        if name is None:
            logging.debug("UNKNOWN tablet_key=0x%04x", tablet_key)
            return

        # Holding a button produces many identical HID reports. Only the
        # transition into the new state should generate a key event.
        if name == self.active_tablet_button:
            return

        # If the firmware ever switches directly from one button to another,
        # release the old one before pressing the new one.
        if self.active_tablet_button is not None:
            self.release_tablet_button("state-change")

        if name == "wheel_up":
            self.virtual_keys.write(ecodes.EV_REL, ecodes.REL_WHEEL, 1)
            self.virtual_keys.syn()
            self.active_tablet_button = name
            logging.debug("TABLET BUTTON WHEEL UP")
            return

        if name == "wheel_down":
            self.virtual_keys.write(ecodes.EV_REL, ecodes.REL_WHEEL, -1)
            self.virtual_keys.syn()
            self.active_tablet_button = name
            logging.debug("TABLET BUTTON WHEEL DOWN")
            return

        events = self._button_press_events(name)
        for ev_type, code in events:
            self.virtual_keys.write(ev_type, code, 1)
        self.virtual_keys.syn()
        self.active_tablet_button = name
        logging.debug("TABLET BUTTON DOWN %s (key=0x%04x)", name, tablet_key)

    def emit(self, p):
        assert self.virtual_pen is not None

        # Hardware tablet buttons are independent of pen touch/pressure.
        self.handle_tablet_key(p["tablet_key"])

        # Normal mode mirrors upstream action filtering. Diagnostic permissive
        # mode processes unknown actions too so transitions can be observed.
        usable_action = p["accepted"] or self.permissive
        if not usable_action:
            if not p["touch"]:
                # Make sure an unknown release cannot leave a stale button down.
                self.release_pen(reason=f"unknown-action-{p['action']}")
            return

        if not p["valid_position"]:
            logging.debug("invalid position x=%d y=%d", p["x"], p["y"])
            if not p["touch"]:
                self.release_pen(reason="invalid-position-release")
            return

        self.virtual_pen.write(ecodes.EV_KEY, ecodes.BTN_TOOL_PEN, 1)
        self.virtual_pen.write(ecodes.EV_ABS, ecodes.ABS_X, p["x"])
        self.virtual_pen.write(ecodes.EV_ABS, ecodes.ABS_Y, p["y"])
        self.virtual_pen.write(ecodes.EV_ABS, ecodes.ABS_PRESSURE, p["pressure"] if p["touch"] else 0)
        self.virtual_pen.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, int(p["touch"]))

        # Pen side buttons.
        self.virtual_pen.write(ecodes.EV_KEY, ecodes.BTN_STYLUS, 1 if p["pen_btn"] == 4 else 0)
        self.virtual_pen.write(ecodes.EV_KEY, ecodes.BTN_STYLUS2, 1 if p["pen_btn"] == 6 else 0)
        self.virtual_pen.syn()

        logging.debug(
            "OUT x=%d y=%d pressure=%d touch=%s action=%d pen_btn=%d",
            p["x"], p["y"], p["pressure"], p["touch"], p["action"], p["pen_btn"],
        )

    def release_pen(self, reason: str):
        # Avoid a stuck modifier if the USB stream stops while a tablet key is held.
        self.release_tablet_button(reason)

        if self.virtual_pen is None:
            return
        logging.info("pen release: %s", reason)
        self.virtual_pen.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0)
        self.virtual_pen.write(ecodes.EV_KEY, ecodes.BTN_TOOL_PEN, 0)
        self.virtual_pen.write(ecodes.EV_KEY, ecodes.BTN_STYLUS, 0)
        self.virtual_pen.write(ecodes.EV_KEY, ecodes.BTN_STYLUS2, 0)
        self.virtual_pen.write(ecodes.EV_ABS, ecodes.ABS_PRESSURE, 0)
        self.virtual_pen.syn()
        self.parser.touch = False

    def run(self):
        self.prepare_device()
        self.enter_full_area()
        self.create_uinput()
        logging.info("DRIVER READY -- waiting for 64-byte T501 reports")

        assert self.dev is not None
        assert self.endpoint is not None
        while self.running:
            try:
                self.read_count += 1
                if self.debug:
                    logging.debug(
                        "READ #%d endpoint=0x%02x size=%d timeout=%dms",
                        self.read_count, self.endpoint.bEndpointAddress,
                        self.endpoint.wMaxPacketSize, self.cfg.timeout_ms,
                    )
                data = self.dev.read(
                    self.endpoint.bEndpointAddress,
                    self.endpoint.wMaxPacketSize,
                    timeout=self.cfg.timeout_ms,
                )
                logging.debug("READ OK #%d len=%d", self.read_count, len(data))
                self.consecutive_timeouts = 0
                parsed = self.parser.parse(data)
                if parsed is not None:
                    self.emit(parsed)
            except usb.core.USBError as exc:
                errno = getattr(exc, "errno", None)
                if errno in (110,):
                    self.timeout_count += 1
                    self.consecutive_timeouts += 1
                    if self.debug and (self.timeout_count <= 5 or self.timeout_count % 50 == 0):
                        logging.debug("USB read timeout #%d (read=%d)", self.timeout_count, self.read_count)
                    if self.consecutive_timeouts >= self.HEARTBEAT_TIMEOUT_THRESHOLD:
                        self.release_pen(reason="silence-timeout (pen lifted)")
                        logging.info(
                            "No data for %d consecutive timeouts (~%dms) -- "
                            "doing a full re-arm cycle (reset+detach+claim+SET_REPORT)",
                            self.consecutive_timeouts,
                            self.consecutive_timeouts * self.cfg.timeout_ms,
                        )
                        try:
                            self.enter_full_area()
                        except usb.core.USBError as heartbeat_exc:
                            logging.warning("re-arm cycle failed: %r", heartbeat_exc)
                        self.consecutive_timeouts = 0
                    continue
                logging.error("USB read error errno=%r: %r", errno, exc)
                break
            except KeyboardInterrupt:
                break
            except Exception:
                logging.exception("unexpected driver error")
                break

        self.release_pen("driver-stop")
        if self.virtual_pen is not None:
            self.virtual_pen.close()
        if self.virtual_keys is not None:
            self.virtual_keys.close()
        if self.dev is not None:
            try:
                usb.util.release_interface(self.dev, CONTROL_IFACE)
            except Exception:
                pass
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--debug", action="store_true")
    ap.add_argument("--raw", action="store_true", help="dump every 64-byte report")
    ap.add_argument("--permissive-actions", action="store_true", help="process unknown pen_action values")
    ap.add_argument("--timeout", type=int, default=250)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if (args.debug or args.raw) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = Config(timeout_ms=args.timeout)
    driver = T501Driver(cfg, args.debug, args.raw, args.permissive_actions)
    signal.signal(signal.SIGINT, driver.stop)
    signal.signal(signal.SIGTERM, driver.stop)
    driver.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
