"""Keep the Mac awake and Roam/Teams presence active (formerly ~/stayawake.sh).
caffeinate holds off sleep for as long as DevBot runs; after 5 minutes with no input, a tiny cursor loop and a Shift tap
count as real user input. Each nudge is verified by reading the system idle timer."""
import os
import re
import subprocess
import threading
import time

from devbot import bus

IDLE = 300  # only move the cursor after 5 minutes without any keyboard or mouse input
CHECK = 30
NUDGE = r"""
import CoreGraphics
import Foundation
let p = CGEvent(source: nil)!.location
func move(_ pt: CGPoint) {
  CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
}
for i in 0...40 {
  let t = Double(i) / 40 * 2 * .pi
  move(CGPoint(x: p.x + 20 * sin(t), y: p.y + 10 * sin(2 * t)))
  usleep(15000)
}
move(p)
for down in [true, false] {   // tap Shift (keycode 56): harmless, counts as keyboard input
  CGEvent(keyboardEventSource: nil, virtualKey: 56, keyDown: down)?.post(tap: .cghidEventTap)
}
"""


def idle_seconds():
    """Seconds since the last user input, from the HID system. None if unreadable."""
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True).stdout
    m = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(m.group(1)) / 1e9 if m else None


def nudge():
    """Post the input events. Returns (ok, message)."""
    before = idle_seconds()
    r = subprocess.run(["swift", "-"], input=NUDGE, capture_output=True, text=True, timeout=90)
    if r.returncode:
        return False, f"Could not nudge your presence: {r.stderr.strip()[-300:]}"
    after = idle_seconds()
    if before is not None and after is not None and before > 5 and after > 3:
        return False, ("macOS ignored the presence nudge, so Roam and Teams may show you as away. Give your terminal app "
                       "Accessibility access in System Settings > Privacy & Security > Accessibility.")
    return True, "Kept you active in Roam and Teams."


def keep_awake():
    # A reload re-execs with the same pid, so drop the previous run's caffeinate instead of stacking another.
    subprocess.run(["pkill", "-f", f"caffeinate -dimsu -w {os.getpid()}$"])
    caffeinate = subprocess.Popen(["caffeinate", "-dimsu", "-w", str(os.getpid())])
    bus.log("RoamBrain", f"Keeping the Mac awake. After {IDLE // 60} minutes without input, a small cursor loop keeps you active in Roam and Teams.")
    while True:
        try:
            idle = idle_seconds()
            if idle is not None and idle < IDLE:  # someone is using the Mac: leave the cursor alone
                time.sleep(CHECK)
                continue
            ok, msg = nudge()
            bus.log("RoamBrain", msg, "info" if ok else "error")
            bus.fire("RoamBrain", "Roam", "presence" if ok else "presence failed")
        except Exception as e:  # never let presence die silently
            bus.log("RoamBrain", f"The presence nudge hit a problem and will retry: {e}", "error")
        time.sleep(CHECK)


def start():
    threading.Thread(target=keep_awake, daemon=True, name="awake").start()
