"""Local head-heading KVM watcher. Importing this module opens no devices."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import queue
import signal
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
COMMANDS = {
    1: bytes.fromhex("AA 00 56"),
    2: bytes.fromhex("AA 01 55"),
    3: bytes.fromhex("AA 02 54"),
    4: bytes.fromhex("AA 03 53"),
}
WINDOW_TITLE = "eKL gaze switch - starts in PREVIEW"
INSTANCE_COMMANDS = ("show", "quit")


def default_config():
    """Missing-key fallback. New installs copy config.example.json, which sets
    dwell_ms 400, initial_host 1, an empty camera name, and COM3.
    """
    return {
        "com_port": "COM3", "baud": 115200, "camera_index": 0,
        "camera_name": "",
        "try_other_cameras": False, "left_threshold_deg": -15.0,
        "right_threshold_deg": 15.0, "dwell_ms": 1000, "cooldown_ms": 2000,
        "initial_host": 2, "max_frame_gap_ms": 300,
        "calibration": None,
        "left_host": 1, "right_host": 2,
    }


def validate_config(c):
    def number(key, low, high):
        v = c[key]
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError(f"Invalid {key}: expected {low}..{high}")
    if c.get("baud") != 115200:
        raise ValueError("Only the proven 115200 baud is allowed; never sweep baud rates")
    if not isinstance(c.get("com_port"), str) or not c["com_port"].upper().startswith("COM"):
        raise ValueError("com_port must name a Windows COM port")
    number("camera_index", 0, 10)
    if type(c["camera_index"]) is not int:
        raise ValueError("camera_index must be an integer")
    name = c.get("camera_name", "")
    if name is None:
        name = ""
    if not isinstance(name, str):
        raise ValueError("camera_name must be a string or null")
    c["camera_name"] = name.strip()
    if not isinstance(c.get("try_other_cameras", False), bool):
        raise ValueError("try_other_cameras must be true or false")
    number("left_threshold_deg", -40, -1)
    number("right_threshold_deg", 1, 40)
    number("dwell_ms", 250, 10000)
    number("cooldown_ms", 2000, 60000)
    number("max_frame_gap_ms", 50, 500)

    def host_choice(key, default):
        if key not in c or c[key] is None:
            c[key] = default
        value = c[key]
        if isinstance(value, bool) or type(value) is not int or value not in (1, 2, 3, 4):
            raise ValueError(f"{key} must be 1, 2, 3, or 4")
        return value

    host_choice("initial_host", 1)
    host_choice("left_host", 1)
    host_choice("right_host", 2)
    if c["left_host"] == c["right_host"]:
        raise ValueError("left_host and right_host must name different switch ports")
    cal = c.get("calibration")
    if cal is not None:
        if not isinstance(cal, dict):
            raise ValueError("calibration must be null or an object")
        left, right = cal.get("left_yaw"), cal.get("right_yaw")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 75 for v in (left, right)):
            raise ValueError("Invalid calibration angles")
        if abs(right - left) < 8:
            raise ValueError("Calibration separation must be at least 8 degrees; turn head, not just eyes")
    return c


def load_config(path):
    c = default_config()
    c.update(json.loads(path.read_text(encoding="utf-8-sig")))
    return validate_config(c)


def save_config(path, config):
    validate_config(config)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def ensure_config(path, example_path=None):
    """Copy the example onto path only when path is missing. Never overwrites."""
    path = Path(path)
    if path.exists():
        return False
    if example_path is None:
        example_path = path.with_name("config.example.json")
    example_path = Path(example_path)
    if not example_path.is_file():
        raise FileNotFoundError(f"Missing {path} and no example at {example_path}")
    data = example_path.read_bytes()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return True


def write_setup_choices(path, *, com_port, camera, left_host, right_host):
    """Update port, camera, and side hosts. Skip the write when nothing changed."""
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    updated = dict(config)
    changed = False
    port = str(com_port).strip().upper()
    if str(updated.get("com_port", "")).upper() != port:
        updated["com_port"] = port
        changed = True
    camera_text = str(camera).strip()
    if camera_text.isdigit():
        index = int(camera_text)
        if updated.get("camera_index") != index or (updated.get("camera_name") or "") != "":
            updated["camera_index"] = index
            updated["camera_name"] = ""
            changed = True
    elif (updated.get("camera_name") or "") != camera_text:
        updated["camera_name"] = camera_text
        changed = True
    defaults = {"left_host": 1, "right_host": 2}
    for key, value in (("left_host", int(left_host)), ("right_host", int(right_host))):
        current = updated[key] if key in updated and updated[key] is not None else defaults[key]
        if current != value:
            updated[key] = value
            changed = True
    if not changed:
        return False
    save_config(path, updated)
    return True


def heading(yaw, c):
    """Calibrated heading: LEFT=-24, RIGHT=+24, independent of camera bias/sign."""
    if yaw is None or not math.isfinite(yaw):
        return None
    cal = c.get("calibration")
    if cal is None:
        return yaw  # Display only: arming requires calibration.
    left, right = cal["left_yaw"], cal["right_yaw"]
    return (yaw - (left + right) / 2) * 48 / (right - left)


def classify(value, c):
    if value is None or not math.isfinite(value):
        return "NO FACE", None
    if value <= c["left_threshold_deg"]:
        return "LEFT", c.get("left_host", 1)
    if value >= c["right_threshold_deg"]:
        return "RIGHT", c.get("right_host", 2)
    return "DEAD ZONE", None


class SwitchController:
    """Pure safety state machine. Does not import serial or open any device."""
    def __init__(self, config):
        self.config = validate_config(config)
        self.armed = False
        self.current_host = config["initial_host"]
        self.candidate = None
        self.since = None
        self.last_frame = None
        self.cooldown_until = 0.0
        self.error = ""

    def reset_dwell(self):
        self.candidate = self.since = None

    def set_armed(self, value):
        self.reset_dwell()
        self.last_frame = None
        if value and (self.config.get("calibration") is None or self.current_host not in COMMANDS):
            self.error = "Calibrate with C and confirm current host (1-4 in preview) before arming"
            self.armed = False
            return False
        self.armed = bool(value)
        self.error = ""
        return True

    def update(self, value, now):
        stalled = self.last_frame is not None and now - self.last_frame > self.config["max_frame_gap_ms"] / 1000
        self.last_frame = now
        if stalled:
            self.reset_dwell()
        _, target = classify(value, self.config)
        if not self.armed or now < self.cooldown_until or target is None or target == self.current_host:
            self.reset_dwell()
            return None
        if target != self.candidate:
            self.candidate, self.since = target, now
            return None
        if now - self.since + 1e-9 >= self.config["dwell_ms"] / 1000:
            self.reset_dwell()
            # Reserve cooldown before returning a request; no repeated requests if dispatch stalls.
            self.cooldown_until = now + self.config["cooldown_ms"] / 1000
            return target
        return None

    def complete(self, host, now):
        self.current_host = host
        self.cooldown_until = now + self.config["cooldown_ms"] / 1000
        self.reset_dwell()

    def fail(self, message):
        self.set_armed(False)
        self.current_host = None  # A missing echo does not prove the hardware did not switch.
        self.error = str(message)


OPEN_SETTLE_S = 0.02


def configure_serial(port, c):
    port.baudrate = 115200
    port.bytesize, port.parity, port.stopbits = 8, "N", 1
    port.timeout = port.write_timeout = 0.25
    port.xonxoff = port.rtscts = port.dsrdtr = False
    port.dtr = port.rts = False
    port.port = c["com_port"]


def write_command(port, host):
    """Exactly one write. No retries, other baud rates, text or line ending."""
    if host not in COMMANDS:
        raise ValueError("Only hosts 1, 2, 3, and 4 are allowed")
    port.reset_input_buffer()
    command = COMMANDS[host]
    count = port.write(command)
    if count != 3:
        raise RuntimeError(f"Partial write ({count}/3), not retried")
    port.flush()
    echo = port.read(3)
    if echo != command:
        raise RuntimeError(f"TX {command.hex(' ').upper()} / echo {echo.hex(' ').upper() or '(none)'}; not retried")
    return echo


def send_host(host, c, *, serial_factory=None, sleeper=time.sleep, port=None):
    """Open-if-needed helper for tests. Live use keeps SerialLink open."""
    validate_config(c)
    if host not in COMMANDS:
        raise ValueError("Only hosts 1, 2, 3, and 4 are allowed")
    owns = port is None
    if owns:
        if serial_factory is None:
            import serial
            serial_factory = serial.Serial
        port = serial_factory(port=None)
        configure_serial(port, c)
        port.open()
        sleeper(OPEN_SETTLE_S)
    try:
        return write_command(port, host)
    finally:
        if owns:
            port.close()


class SerialLink:
    """Opens the configured COM port once (short settle), then each switch is a single write."""
    def __init__(self, c, serial_factory=None, sleeper=time.sleep):
        self.c = validate_config(c)
        self.serial_factory = serial_factory
        self.sleeper = sleeper
        self.port = None

    def open(self):
        if self.port is not None and getattr(self.port, "is_open", False):
            return
        factory = self.serial_factory
        if factory is None:
            import serial
            factory = serial.Serial
        port = factory(port=None)
        configure_serial(port, self.c)
        port.open()
        self.sleeper(OPEN_SETTLE_S)
        self.port = port

    def send(self, host):
        self.open()
        return write_command(self.port, host)

    def close(self):
        port, self.port = self.port, None
        if port is not None:
            port.close()


class PoseEstimator:
    def __init__(self, model_path):
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
        self.mp = mp
        self.landmarker = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(
                base_options=python.BaseOptions(model_asset_path=str(model_path)),
                running_mode=vision.RunningMode.VIDEO, num_faces=2,
                min_face_detection_confidence=0.6, min_face_presence_confidence=0.6,
                min_tracking_confidence=0.6, output_facial_transformation_matrixes=True,
            )
        )
        self.timestamp = -1

    def estimate(self, frame):
        import cv2
        import numpy as np
        self.timestamp = max(self.timestamp + 1, int(time.monotonic() * 1000))
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = self.landmarker.detect_for_video(
            self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)),
            self.timestamp,
        )
        if len(result.face_landmarks) != 1 or len(result.facial_transformation_matrixes) != 1:
            return None, "No face" if not result.face_landmarks else "Multiple faces: paused"
        matrix = np.asarray(result.facial_transformation_matrixes[0], dtype=float)
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            return None, "Invalid pose"
        rotation = matrix[:3, :3]
        # The model's rigid face-to-camera transform; remove any scale per column.
        norms = np.linalg.norm(rotation, axis=0)
        if np.any(norms < 1e-6):
            return None, "Invalid pose"
        rotation = rotation / norms
        yaw = math.degrees(math.atan2(rotation[0, 2], rotation[2, 2]))
        if abs(yaw) > 75:
            return None, "Pose out of range"
        return yaw, "One face"

    def close(self):
        self.landmarker.close()


class Calibration:
    """Explicit key-triggered one-second samples. No serial path."""
    def __init__(self):
        self.stage = "idle"
        self.samples = []
        self.started = None
        self.last = None
        self.left = None
        self.message = "C: calibrate (required before arming)"

    def key(self):
        if self.stage == "idle":
            self.stage = "left_ready"
            self.message = "Look at LEFT screen; press C to sample for 1 second"
        elif self.stage in ("left_ready", "right_ready"):
            self.stage = self.stage.replace("ready", "collect")
            self.samples, self.started, self.last = [], None, None
            self.message = "Hold your head steady..."

    def update(self, raw, now):
        if self.stage not in ("left_collect", "right_collect"):
            return None
        if raw is None or (self.last is not None and now - self.last > 0.3):
            self.samples, self.started, self.last = [], None, None
            self.message = "Need one visible face; hold steady..."
            return None
        if self.started is None:
            self.started = now
        self.last = now
        self.samples.append(raw)
        if now - self.started < 1:
            return None
        if len(self.samples) < 8 or statistics.pstdev(self.samples) > 3:
            self.samples, self.started, self.last = [], None, None
            self.message = "Too little/stable data; hold head steady for 1 second..."
            return None
        sample = statistics.median(self.samples)
        if self.stage == "left_collect":
            self.left = sample
            self.stage = "right_ready"
            self.message = "Look at RIGHT screen; press C to sample for 1 second"
            return None
        result = {"left_yaw": self.left, "right_yaw": sample}
        self.stage = "idle"
        self.message = "Calibration captured"
        return result


def _read_camera(camera):
    import cv2
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    ok, frame = camera.read()
    if ok and frame is not None:
        return frame
    return None


def camera_open_plan(c):
    """Name first when camera_name is set. Empty or null uses camera_index only."""
    name = c.get("camera_name")
    if not isinstance(name, str):
        name = ""
    name = name.strip()
    plan = [("name", name)] if name else []
    index = c.get("camera_index", 0)
    indices = [index]
    if c.get("try_other_cameras", False):
        indices += [i for i in range(4) if i not in indices]
    plan += [("index", i) for i in indices]
    return plan


def open_camera(c, capture_factory=None, read_frame=None):
    """Open the planned camera. A named camera that fails falls back to indexes.

    Indexes are not probed until the name fails, so a working named camera is
    not replaced by another device. Tests pass capture_factory and read_frame
    and do not open a real camera.
    """
    if capture_factory is None or read_frame is None:
        import cv2
        if capture_factory is None:
            def capture_factory(source):
                return cv2.VideoCapture(source, cv2.CAP_DSHOW)
        if read_frame is None:
            read_frame = _read_camera
    tried = []
    for _kind, source in camera_open_plan(c):
        tried.append(source)
        camera = capture_factory(source)
        if camera.isOpened():
            frame = read_frame(camera)
            if frame is not None:
                return camera, frame, source
        camera.release()
    raise RuntimeError("No working webcam (tried: " + str(tried) + "). Check Windows camera privacy and close other camera apps.")


def open_camera_retrying(c, attempts=1, sleeper=time.sleep, **kwargs):
    """Re-open the camera a few times. Used after relaunch, when Windows may still be releasing it.

    This does not retry serial writes. A switch command is still one raw write.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last = None
    for attempt in range(attempts):
        try:
            return open_camera(c, **kwargs)
        except RuntimeError as exc:
            last = exc
            if attempt + 1 < attempts:
                sleeper(0.3)
    raise last


def minimize_named_window(title):
    if sys.platform != "win32":
        return False
    import ctypes
    hwnd = ctypes.windll.user32.FindWindowW(None, title)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        return True
    return False


def restore_named_window(title):
    """Restore and focus the OpenCV window. Safe to call from the main thread only."""
    if sys.platform != "win32":
        return False
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def tray_visual_state(armed, calibration, error=""):
    """Return error | armed | needs_calibration | paused for icon and tooltip."""
    if error:
        return "error"
    if calibration is None:
        return "needs_calibration"
    if armed:
        return "armed"
    return "paused"


def tray_tooltip(armed, current_host, calibration, error=""):
    state = tray_visual_state(armed, calibration, error)
    host = current_host if current_host in COMMANDS else None
    if state == "error":
        detail = str(error).strip() or "error"
        return f"Gazer: error — {detail}"[:120]
    if state == "needs_calibration":
        return "Gazer: paused (needs calibration)"
    if state == "armed":
        return f"Gazer: running (armed), host {host}" if host else "Gazer: running (armed)"
    if host:
        return f"Gazer: paused (disarmed), host {host}"
    return "Gazer: paused (disarmed)"


def tray_icon_rgb(state):
    return {
        "armed": (40, 180, 70),
        "paused": (140, 140, 140),
        "disarmed": (140, 140, 140),
        "needs_calibration": (140, 140, 140),
        "error": (220, 140, 30),
    }[state]


def tray_menu_spec(armed, calibration, host, error="", window_visible=True):
    """Right-click menu. Resume stays disabled until calibration and a host are set."""
    armed = bool(armed)
    window_visible = bool(window_visible)
    error = error or ""
    if armed:
        pause_label, pause_action, pause_enabled = "Pause", "pause", True
    elif calibration is None:
        pause_label, pause_action, pause_enabled = "Resume (calibrate first)", "resume", False
    elif host not in COMMANDS:
        pause_label, pause_action, pause_enabled = "Resume (set host 1-4 first)", "resume", False
    elif error:
        pause_label, pause_action, pause_enabled = "Resume (clear error first)", "resume", False
    else:
        pause_label, pause_action, pause_enabled = "Resume", "resume", True
    return [
        {"label": "Show window", "action": "show", "enabled": True, "default": True},
        {"label": "Hide window", "action": "hide", "enabled": window_visible},
        {"label": pause_label, "action": pause_action, "enabled": pause_enabled},
        {"label": "Calibrate", "action": "calibrate", "enabled": True},
        {"label": "Relaunch", "action": "relaunch", "enabled": True},
        {"label": "Quit", "action": "quit", "enabled": True},
    ]


def render_tray_image(armed, current_host, calibration, error=""):
    """Draw a 64x64 tray icon in memory (Pillow). No binary assets."""
    from PIL import Image, ImageDraw, ImageFont
    state = tray_visual_state(armed, calibration, error)
    rgb = tray_icon_rgb(state)
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=rgb + (255,))
    host = current_host if current_host in COMMANDS else None
    if host is not None and state != "needs_calibration":
        text = str(host)
        font = ImageFont.load_default()
        for candidate in (
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arial.ttf",
            "segoeui.ttf",
        ):
            try:
                font = ImageFont.truetype(candidate, 36)
                break
            except OSError:
                continue
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((64 - tw) / 2 - bbox[0], (64 - th) / 2 - bbox[1] - 1), text, fill=(255, 255, 255, 255), font=font)
    return image


class TrayIcon:
    """System-tray icon. Menu callbacks only enqueue requests for the main loop."""

    def __init__(self):
        self.requests = queue.Queue()
        self._icon = None
        # Re-entrant: pystray evaluates menu callables on this same thread.
        self._lock = threading.RLock()
        self._armed = False
        self._host = None
        self._calibration = None
        self._error = ""
        self._window_visible = True
        self.available = False
        self.warning = ""

    def start(self, initial_armed=False, initial_host=None, calibration=None, error=""):
        try:
            import pystray
            from pystray import MenuItem as Item
        except Exception as exc:
            self.warning = f"Tray unavailable ({exc}); continuing without a notification-area icon"
            print(f"Warning: {self.warning}")
            try:
                (ROOT / ".tray.log").write_text("tray failed: " + self.warning + "\n", encoding="utf-8")
            except OSError:
                pass
            return False
        try:
            render_tray_image(False, None, None)
        except Exception as exc:
            self.warning = f"Tray icon draw failed ({exc}); continuing without a notification-area icon"
            print(f"Warning: {self.warning}")
            try:
                (ROOT / ".tray.log").write_text("tray failed: " + self.warning + "\n", encoding="utf-8")
            except OSError:
                pass
            return False
        self._armed = bool(initial_armed)
        self._host = initial_host
        self._calibration = calibration
        self._error = error or ""
        self._window_visible = True

        def enqueue(action):
            def handler(icon, item):
                self.requests.put(action)
            return handler

        def menu_entry(actions):
            for entry in self._menu_spec():
                if entry["action"] in actions:
                    return entry
            return None

        def pause_text(item):
            entry = menu_entry(("pause", "resume"))
            return entry["label"] if entry else "Resume"

        def pause_enabled(item):
            entry = menu_entry(("pause", "resume"))
            return bool(entry and entry["enabled"])

        def hide_enabled(item):
            entry = menu_entry(("hide",))
            return bool(entry and entry["enabled"])

        def enqueue_pause(icon, item):
            entry = menu_entry(("pause", "resume"))
            self.requests.put(entry["action"] if entry else "resume")

        menu = pystray.Menu(
            Item("Show window", enqueue("show"), default=True),
            Item("Hide window", enqueue("hide"), enabled=hide_enabled),
            Item(pause_text, enqueue_pause, enabled=pause_enabled),
            Item("Calibrate", enqueue("calibrate")),
            Item("Relaunch", enqueue("relaunch")),
            Item("Quit", enqueue("quit")),
        )
        image = render_tray_image(self._armed, self._host, self._calibration, self._error)
        tip = tray_tooltip(self._armed, self._host, self._calibration, self._error)
        self._icon = pystray.Icon("Gazer", image, tip, menu)
        self.available = True
        # run_detached keeps the OpenCV main thread free.
        self._icon.run_detached()
        print("Tray icon started (notification area)")
        try:
            (ROOT / ".tray.log").write_text("tray started available=1 tip=" + repr(tip) + "\n", encoding="utf-8")
        except OSError:
            pass
        return True

    def _menu_spec(self):
        with self._lock:
            return tray_menu_spec(
                self._armed, self._calibration, self._host, self._error, self._window_visible,
            )

    def update(self, armed, current_host, calibration, error="", window_visible=True):
        if not self.available or self._icon is None:
            return
        with self._lock:
            self._armed = bool(armed)
            self._host = current_host
            self._calibration = calibration
            self._error = error or ""
            self._window_visible = bool(window_visible)
            snapshot = (self._armed, self._host, self._calibration, self._error)
        # Icon, tooltip, and menu rebuild run outside the lock. update_menu()
        # calls back into _menu_spec() on this thread, and that must be able
        # to read the state just published.
        icon = self._icon
        if icon is None:
            return
        try:
            armed_now, host_now, calibration_now, error_now = snapshot
            icon.icon = render_tray_image(armed_now, host_now, calibration_now, error_now)
            icon.title = tray_tooltip(armed_now, host_now, calibration_now, error_now)
            icon.update_menu()
        except Exception:
            pass

    def poll(self):
        actions = []
        while True:
            try:
                actions.append(self.requests.get_nowait())
            except queue.Empty:
                break
        return actions

    def stop(self):
        icon, self._icon = self._icon, None
        self.available = False
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass


def note_preview_visibility(prop):
    """'closed' when the user has dismissed the preview. That hides to the tray; it does not quit."""
    try:
        value = float(prop)
    except (TypeError, ValueError):
        return "closed"
    if value < 1:
        return "closed"
    return "open"


def preview_dismiss_action(tray_available):
    """Hide when the tray icon can bring the window back. Quit when it cannot, so the camera is released."""
    return "preview_closed" if tray_available else "quit"


def plan_control(action, state):
    """Decide a key, tray, or second-launch request. Does not touch the camera or the COM port.

    close_serial means the caller must close the port and must not send a switch command.
    """
    armed = bool(state.get("armed"))
    can_arm = bool(state.get("can_arm"))
    calibrating = bool(state.get("calibrating"))
    visible = bool(state.get("window_visible"))
    result = {
        "armed": armed,
        "close_serial": False,
        "open_serial": False,
        "window": None,
        "calibrate": False,
        "quit": False,
        "relaunch": False,
    }
    if action in ("quit", "relaunch"):
        result["armed"] = False
        result["close_serial"] = True
        result["quit"] = True
        result["relaunch"] = action == "relaunch"
        return result
    if action in ("preview_closed", "hide"):
        result["window"] = "hide"
        return result
    if action == "show":
        result["window"] = "show"
        return result
    if action == "toggle_window":
        result["window"] = "hide" if visible else "show"
        return result
    if action == "pause":
        if armed:
            result["armed"] = False
            result["close_serial"] = True
        return result
    if action == "resume":
        if calibrating or not can_arm or armed:
            return result
        result["armed"] = True
        result["open_serial"] = True
        return result
    if action == "toggle_pause":
        if armed:
            return plan_control("pause", state)
        return plan_control("resume", state)
    if action == "calibrate":
        result["armed"] = False
        result["close_serial"] = True
        result["calibrate"] = True
        result["window"] = "show"
        return result
    return result


def apply_arm_plan(controller, link, plan):
    """Pause or resume. Never writes a switch command.

    Resume opens the port once. A failed open disarms and does not retry.
    """
    if plan.get("armed") and not controller.armed:
        if not controller.set_armed(True):
            return None
        if plan.get("open_serial"):
            try:
                link.open()
            except Exception as exc:
                controller.fail(str(exc))
                link.close()
                return str(exc)
        return None
    if not plan.get("armed") and controller.armed:
        controller.set_armed(False)
    if plan.get("close_serial"):
        link.close()
    return None


def _command_dir(root):
    return Path(root) / ".watcher.commands"


def post_instance_command(root, command):
    command = str(command).strip().lower()
    if command not in INSTANCE_COMMANDS:
        raise ValueError(f"Unknown instance command: {command}")
    folder = _command_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.time_ns()}-{os.getpid()}-{command}"
    path.write_text(command + "\n", encoding="utf-8")
    return path


def drain_instance_commands(root):
    folder = _command_dir(root)
    if not folder.is_dir():
        return []
    commands = []
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            text = ""
        try:
            path.unlink()
        except OSError:
            pass
        if text in INSTANCE_COMMANDS:
            commands.append(text)
    return commands


def clear_instance_commands(root):
    drain_instance_commands(root)


def is_gazer_command(command):
    text = str(command).lower().replace("\\", "/")
    return "gaze_switch.py" in text and "python" in text


def owner_pid_from_lock(path, me=None):
    """Pid recorded after the lock byte, or None for ourselves, garbage, or a missing file."""
    if me is None:
        me = os.getpid()
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    text = data[1:].split(b"\n", 1)[0].strip()
    try:
        number = int(text)
    except ValueError:
        return None
    if number <= 0 or number == me:
        return None
    return number


def gazer_pids_from_table(rows, me=None):
    if me is None:
        me = os.getpid()
    found = []
    for pid, command in rows:
        try:
            number = int(pid)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number == me:
            continue
        if is_gazer_command(command):
            found.append(number)
    return found


def read_process_table():
    if sys.platform == "win32":
        return _windows_process_table()
    return _posix_process_table()


def _posix_process_table():
    rows = []
    proc = Path("/proc")
    if not proc.is_dir():
        return rows
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        rows.append((int(entry.name), command))
    return rows


def _windows_process_table():
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'python' } | "
        "ForEach-Object { $_.ProcessId.ToString() + \"`t\" + [string]$_.CommandLine }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in completed.stdout.splitlines():
        pid_text, separator, command = line.partition("\t")
        if not separator:
            continue
        try:
            rows.append((int(pid_text.strip()), command))
        except ValueError:
            continue
    return rows


def terminate_pid(pid):
    number = int(pid)
    if number <= 0 or number == os.getpid():
        return
    os.kill(number, signal.SIGTERM)


def _try_lock_fd(fh):
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fh):
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class WatcherLock:
    """Exclusive lock so two Gazers cannot open the camera and the COM port together.

    The operating system drops the lock when this process exits, including a crash.
    """

    def __init__(self, root):
        self.path = Path(root) / ".watcher.lock"
        self._fh = None

    def try_acquire(self):
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
            except FileExistsError:
                pass
            else:
                try:
                    os.write(fd, b"0")
                finally:
                    os.close(fd)
        fh = open(self.path, "r+b")
        try:
            fh.seek(0)
            if fh.read(1) != b"0":
                fh.seek(0)
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            _try_lock_fd(fh)
        except OSError:
            fh.close()
            return False
        except Exception:
            fh.close()
            raise
        self._fh = fh
        try:
            payload = f"{os.getpid():010d}\n".encode("ascii")
            fh.seek(1)
            fh.write(payload)
            fh.flush()
        except OSError:
            pass
        return True

    def release(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        _unlock_fd(fh)
        fh.close()


def claim_watcher(root, *, replace=False, process_table=None, terminate=None, sleeper=None, timeout=8.0, restore=None, clock=None):
    """Take the single-instance lock, or ask the owner to show its window.

    Returns the lock this process must release. Returns None when another Gazer
    is already running and this launch only focused it. replace=True does not
    return None: it asks the owner to quit, then stops stray gaze_switch.py
    processes if the camera and COM port are still held.
    """
    root = Path(root)
    sleeper = time.sleep if sleeper is None else sleeper
    clock = time.monotonic if clock is None else clock
    lock = WatcherLock(root)

    def acquire_until(wait):
        deadline = clock() + wait
        while True:
            if lock.try_acquire():
                clear_instance_commands(root)
                return True
            if clock() >= deadline:
                return False
            sleeper(0.05)

    if lock.try_acquire():
        clear_instance_commands(root)
        return lock
    if not replace:
        post_instance_command(root, "show")
        restore_fn = restore if restore is not None else (lambda: restore_named_window(WINDOW_TITLE))
        try:
            restore_fn()
        except Exception:
            pass
        return None
    post_instance_command(root, "quit")
    if acquire_until(timeout):
        return lock
    table = process_table() if process_table is not None else read_process_table()
    killer = terminate if terminate is not None else terminate_pid
    pids = list(gazer_pids_from_table(table))
    owner = owner_pid_from_lock(lock.path)
    if owner is not None and owner not in pids:
        pids.append(owner)
    for pid in pids:
        try:
            killer(pid)
        except Exception:
            pass
    if acquire_until(timeout):
        return lock
    raise RuntimeError("Could not free the camera and COM port from the other Gazer")


def build_relaunch_command(config_path, model_path, executable=None):
    """A fresh visible Gazer that replaces whatever is running. Not minimized."""
    executable = sys.executable if executable is None else executable
    return [
        str(executable),
        str(ROOT / "gaze_switch.py"),
        "--replace",
        "--config", str(config_path),
        "--model", str(model_path),
    ]


def spawn_detached(command, popen=None):
    popen = subprocess.Popen if popen is None else popen
    kwargs = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    return popen(list(command), **kwargs)


def run(config_path, model_path, minimized=False, replace=False):
    lock = claim_watcher(ROOT, replace=replace)
    if lock is None:
        print("Gazer is already running. Showing its window.")
        return 0
    camera = pose = link = None
    cv2 = None
    tray = TrayIcon()
    title = WINDOW_TITLE
    did_minimize = False
    hidden = False
    seen_visible = False
    show_pending = False
    try:
        import cv2
        import numpy as np
        c = load_config(config_path)
        if not model_path.is_file():
            raise RuntimeError(f"Missing model: {model_path}. See README Setup.")
        pose = PoseEstimator(model_path)
        # Relaunch may need a moment for Windows to release the webcam. Serial writes are not retried.
        camera, frame, index = open_camera_retrying(c, attempts=8 if replace else 1)
        s, calibration = SwitchController(c), Calibration()
        link = SerialLink(c)
        last_command = "None (no serial opened)"
        smoothed, previous_time = None, None
        tray.start(initial_armed=False, initial_host=s.current_host, calibration=c.get("calibration"), error="")
        tray_snapshot = None

        def handle(actions):
            # Pause, resume, quit, and relaunch run on this thread. The tray thread only enqueues them.
            nonlocal hidden, show_pending, last_command
            quit_requested = False
            for action in actions:
                plan = plan_control(action, {
                    "armed": s.armed,
                    "can_arm": (
                        calibration.stage == "idle"
                        and c.get("calibration") is not None
                        and s.current_host in COMMANDS
                        and not s.error
                    ),
                    "calibrating": calibration.stage != "idle",
                    "window_visible": not hidden,
                })
                if plan["window"] == "hide":
                    hidden = True
                    try:
                        cv2.destroyWindow(title)
                    except Exception:
                        pass
                elif plan["window"] == "show":
                    hidden = False
                    show_pending = True
                serial_error = apply_arm_plan(s, link, plan)
                if serial_error:
                    last_command = f"ERROR opening serial: {serial_error}"
                if plan["calibrate"]:
                    calibration.key()
                if plan["relaunch"]:
                    try:
                        spawn_detached(build_relaunch_command(
                            Path(config_path).resolve(), Path(model_path).resolve(),
                        ))
                        print("Relaunching Gazer...")
                    except Exception as exc:
                        last_command = f"ERROR relaunching: {exc}"
                        s.error = str(exc)
                        continue
                if plan["quit"]:
                    quit_requested = True
            return quit_requested

        while True:
            # Tray and second-launch requests are applied before this frame can switch.
            if handle(list(tray.poll()) + drain_instance_commands(ROOT)):
                break
            raw, face_status = pose.estimate(frame)
            now = time.monotonic()
            gap = previous_time is not None and now - previous_time > c["max_frame_gap_ms"] / 1000
            previous_time = now
            if raw is None or gap:
                smoothed = None
            if raw is not None:
                smoothed = raw if smoothed is None else 0.4 * raw + 0.6 * smoothed
            captured = calibration.update(raw, now)
            if captured is not None:
                candidate = dict(c, calibration=captured)
                try:
                    save_config(config_path, candidate)
                    c.update(candidate)
                    calibration.message = "Calibration SAVED. Check heading; Space to arm."
                    smoothed = None
                except (ValueError, OSError) as exc:
                    calibration.message = f"Not saved: {exc}. Press C to retry."
            value = heading(smoothed, c)
            state, target = classify(value, c)
            # Space and tray Resume are the only arm paths. Startup stays disarmed.
            desired = s.update(value if calibration.stage == "idle" else None, now)
            if desired is not None and s.armed and calibration.stage == "idle":
                try:
                    echo = link.send(desired)
                    last_command = f"TX {COMMANDS[desired].hex(' ').upper()} / RX {echo.hex(' ').upper()}"
                    s.complete(desired, time.monotonic())
                except Exception as exc:
                    last_command = f"ERROR: {exc}"
                    s.fail(str(exc))
                    link.close()
                smoothed = None
                # Never reuse the pre-send face frame for another switch decision.
            preview = cv2.flip(frame, 1)  # Mirror DISPLAY only; pose uses unmirrored input.
            panel = np.zeros((260, max(900, preview.shape[1]), 3), dtype=np.uint8)
            remaining = max(0, s.cooldown_until - time.monotonic())
            pending = 0 if s.since is None else min(100, int((now - s.since) * 100000 / c["dwell_ms"]))
            lines = [
                f"{'RUNNING / ARMED' if s.armed else 'PAUSED / DISARMED'} | assumed host: {s.current_host or 'UNKNOWN'} | camera {index}",
                f"Raw yaw: {'--' if raw is None else f'{raw:+.1f} deg'} | heading: {'--' if value is None else f'{value:+.1f} deg'} | {state}",
                f"{face_status} | dwell {pending}% | cooldown {remaining:.1f}s | {c['com_port']}/115200 8N1; no flow",
                f"Last: {last_command}",
                calibration.message,
                s.error or "Space pause/resume | C calibrate/sample | X cancel calibration | Q/Esc quit | close hides to tray",
                "Preview only: 1/2/3/4 sets assumed host WITHOUT sending. Click window for keys.",
                "Head heading, not eye-only gaze. Keep face visible. No face/multiple faces = no switch.",
            ]
            for row, text in enumerate(lines):
                cv2.putText(panel, text[:120], (10, 27 + row * 30), cv2.FONT_HERSHEY_SIMPLEX, .49,
                            (80, 240, 110) if row == 0 and s.armed else (230, 230, 230), 1, cv2.LINE_AA)
            canvas = np.zeros((preview.shape[0] + panel.shape[0], panel.shape[1], 3), dtype=np.uint8)
            canvas[:preview.shape[0], :preview.shape[1]] = preview
            canvas[preview.shape[0]:] = panel
            key = None
            closed_preview = False
            if not hidden:
                cv2.imshow(title, canvas)
                if show_pending:
                    restore_named_window(title)
                    show_pending = False
                elif minimized and not did_minimize:
                    minimize_named_window(title)
                    did_minimize = True
                pressed = cv2.waitKey(1)
                if pressed != -1:
                    key = pressed & 0xFF
                try:
                    prop = cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE)
                except Exception:
                    prop = -1
                if note_preview_visibility(prop) == "open":
                    seen_visible = True
                elif seen_visible:
                    # The X button destroys the OpenCV window. Keep running under the tray icon.
                    closed_preview = True
            else:
                time.sleep(0.01)
            key_actions = [preview_dismiss_action(tray.available)] if closed_preview else []
            if key in (27, ord("q"), ord("Q")):
                key_actions.append("quit")
            elif key == 32:
                key_actions.append("toggle_pause")
            elif key in (ord("c"), ord("C")):
                key_actions.append("calibrate")
            elif key in (ord("x"), ord("X")):
                s.set_armed(False)
                link.close()
                calibration = Calibration()
            elif key in (ord("1"), ord("2"), ord("3"), ord("4")) and not s.armed:
                s.current_host = int(chr(key))
                s.reset_dwell()
                s.error = ""
            if handle(key_actions):
                break
            snapshot = (s.armed, s.current_host, c.get("calibration"), s.error or "", not hidden)
            if snapshot != tray_snapshot:
                tray.update(*snapshot)
                tray_snapshot = snapshot
            ok, frame = camera.read()
            if not ok or frame is None:
                s.set_armed(False)
                link.close()
                raise RuntimeError("Camera stopped delivering frames; switching disarmed; exiting")
    finally:
        tray.stop()
        if camera is not None:
            camera.release()
        if pose is not None:
            pose.close()
        if link is not None:
            link.close()
        if cv2 is not None:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
        lock.release()


def serial_chip_label(port):
    vid = getattr(port, "vid", None)
    description = getattr(port, "description", "") or ""
    manufacturer = getattr(port, "manufacturer", "") or ""
    hwid = getattr(port, "hwid", "") or ""
    text = f"{description} {manufacturer} {hwid}".upper()
    if vid == 0x0403 or "FTDI" in text:
        return "FTDI"
    if vid == 0x067B or "PROLIFIC" in text:
        return "Prolific"
    if vid == 0x1A86 or "CH340" in text or "CH341" in text:
        return "CH340"
    return ""


def describe_ports(ports):
    rows = []
    for port in ports:
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        if isinstance(vid, int) and isinstance(pid, int) and not isinstance(vid, bool) and not isinstance(pid, bool):
            vidpid = f"{vid:04X}:{pid:04X}"
        else:
            vidpid = ""
        rows.append({
            "device": getattr(port, "device", "") or "",
            "description": getattr(port, "description", "") or "",
            "vidpid": vidpid,
            "chip": serial_chip_label(port),
        })
    return rows


def format_port_report(rows):
    lines = ["Serial ports:"]
    if not rows:
        lines.append("  (none found)")
    for row in rows:
        vidpid = row["vidpid"] or "VID:PID n/a"
        tag = f"  [{row['chip']}]" if row["chip"] else ""
        description = row["description"] or "(no description)"
        lines.append(f"  {row['device']}: {description}  {vidpid}{tag}")
    lines.append("FTDI, Prolific, and CH340 tags mark common USB-serial chips.")
    return "\n".join(lines)


def iter_serial_ports(list_fn=None):
    if list_fn is None:
        from serial.tools import list_ports
        list_fn = list_ports.comports
    return list(list_fn())


def dshow_camera_names():
    """DirectShow device names via pygrabber. Does not open a camera."""
    from pygrabber.dshow_graph import FilterGraph
    return [str(name) for name in FilterGraph().get_input_devices()]


def probe_camera_index(index):
    import cv2
    camera = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    try:
        return bool(camera.isOpened())
    finally:
        camera.release()


def discover_cameras(name_source=None, probe=None):
    """Prefer DirectShow names. If those are unavailable, probe indexes 0-5."""
    source_fn = dshow_camera_names if name_source is None else name_source
    names = None
    detail = ""
    try:
        names = list(source_fn())
    except Exception as exc:
        detail = str(exc)
        names = None
    if names:
        rows = [{"index": i, "name": name, "opened": None} for i, name in enumerate(names)]
        return rows, "dshow", ""
    probe_fn = probe_camera_index if probe is None else probe
    rows = []
    for index in range(6):
        try:
            opened = bool(probe_fn(index))
        except Exception:
            opened = False
        rows.append({"index": index, "name": "", "opened": opened})
    return rows, "probe", detail


def format_camera_report(rows, source, detail=""):
    if source == "dshow":
        lines = ["Cameras (DirectShow):"]
        if not rows:
            lines.append("  (none found)")
        for row in rows:
            lines.append(f"  {row['index']}: {row['name']}")
        return "\n".join(lines)
    lines = ["DirectShow names unavailable. Probed camera indexes 0-5."]
    if detail:
        lines.append(f"  ({detail})")
    for row in rows:
        state = "opened" if row.get("opened") else "closed"
        lines.append(f"  {row['index']}: {state}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--model", type=Path, default=ROOT / "models" / "face_landmarker.task")
    parser.add_argument("--check", action="store_true", help="Validate config/imports only; no camera or serial")
    parser.add_argument("--minimized", action="store_true", help="Start the preview window minimized")
    parser.add_argument("--replace", action="store_true", help="Stop other Gazer instances, free the camera and COM port, then start")
    parser.add_argument("--list-cameras", action="store_true", help="List DirectShow cameras, or probe indexes 0-5, then exit")
    parser.add_argument("--list-ports", action="store_true", help="List serial ports without opening them, then exit")
    args = parser.parse_args(argv)
    try:
        if args.list_ports or args.list_cameras:
            if args.list_ports:
                print(format_port_report(describe_ports(iter_serial_ports())))
            if args.list_cameras:
                rows, source, detail = discover_cameras()
                print(format_camera_report(rows, source, detail))
            return 0
        if ensure_config(args.config):
            print(f"Created {args.config} from config.example.json")
        c = load_config(args.config)
        if args.check:
            import cv2
            import mediapipe
            import serial
            from mediapipe.tasks.python import vision
            print(f"Config OK; OpenCV {cv2.__version__}; MediaPipe {mediapipe.__version__}; pyserial {serial.__version__}")
            camera = c["camera_name"] or f"index {c['camera_index']}"
            print(f"No camera or COM port opened. Model present: {args.model.is_file()}. Camera setting: {camera}.")
            print(f"Look left -> host {c['left_host']}; look right -> host {c['right_host']}.")
            try:
                import pystray
                from PIL import Image  # noqa: F401
                print(f"Tray deps OK; pystray {getattr(pystray, '__version__', '?')}")
            except Exception as exc:
                print(f"Tray deps missing (optional): {exc}")
            return 0
        run(args.config, args.model, minimized=args.minimized, replace=args.replace)
        return 0
    except Exception as exc:
        print(f"Stopped safely: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
