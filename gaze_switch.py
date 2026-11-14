"""Local head-heading KVM watcher. Importing this module opens no devices."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import queue
import statistics
import threading
import time

ROOT = Path(__file__).resolve().parent
COMMANDS = {
    1: bytes.fromhex("AA 00 56"),
    2: bytes.fromhex("AA 01 55"),
    3: bytes.fromhex("AA 02 54"),
    4: bytes.fromhex("AA 03 53"),
}


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


def minimize_named_window(title):
    import ctypes
    hwnd = ctypes.windll.user32.FindWindowW(None, title)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE


def restore_named_window(title):
    """Restore and focus the OpenCV window. Safe to call from the main thread only."""
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def tray_visual_state(armed, calibration, error=""):
    """Return error | armed | needs_calibration | disarmed for icon/tooltip tests."""
    if error:
        return "error"
    if calibration is None:
        return "needs_calibration"
    if armed:
        return "armed"
    return "disarmed"


def tray_tooltip(armed, current_host, calibration, error=""):
    state = tray_visual_state(armed, calibration, error)
    host = current_host if current_host in COMMANDS else None
    if state == "error":
        detail = str(error).strip() or "error"
        return f"Gazer: error — {detail}"[:120]
    if state == "needs_calibration":
        return "Gazer: needs calibration"
    if state == "armed":
        return f"Gazer: armed, host {host}" if host else "Gazer: armed"
    if host:
        return f"Gazer: disarmed, host {host} (press Space or use the menu)"
    return "Gazer: disarmed (press Space or use the menu)"


def tray_icon_rgb(state):
    return {
        "armed": (40, 180, 70),
        "disarmed": (140, 140, 140),
        "needs_calibration": (140, 140, 140),
        "error": (220, 140, 30),
    }[state]


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
        self._lock = threading.Lock()
        self._armed = False
        self._host = None
        self._calibration = None
        self._error = ""
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

        def enqueue(action):
            def handler(icon, item):
                self.requests.put(action)
            return handler

        def arm_enabled(item):
            if self._armed:
                return True
            return self._calibration is not None and self._host in COMMANDS and not self._error

        def arm_note(item):
            # Shown as disabled Arm when calibration/host missing.
            if self._armed:
                return "Disarm"
            if self._calibration is None:
                return "Arm (calibrate first)"
            if self._host not in COMMANDS:
                return "Arm (set host 1-4 first)"
            if self._error:
                return "Arm (clear error first)"
            return "Arm"

        menu = pystray.Menu(
            Item("Show window", enqueue("show"), default=True),
            Item(arm_note, enqueue("toggle_arm"), enabled=arm_enabled),
            Item("Calibrate", enqueue("calibrate")),
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

    def update(self, armed, current_host, calibration, error=""):
        if not self.available or self._icon is None:
            return
        with self._lock:
            self._armed = bool(armed)
            self._host = current_host
            self._calibration = calibration
            self._error = error or ""
            try:
                self._icon.icon = render_tray_image(self._armed, self._host, self._calibration, self._error)
                self._icon.title = tray_tooltip(self._armed, self._host, self._calibration, self._error)
                self._icon.update_menu()
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


def run(config_path, model_path, minimized=False):
    import cv2
    import numpy as np
    import msvcrt
    c = load_config(config_path)
    # Windows byte-range lock prevents two watcher instances fighting for the COM port.
    lock = (ROOT / ".watcher.lock").open("a+b")
    lock.seek(0); lock.write(b"0"); lock.flush(); lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise RuntimeError("Another watcher is already running")
    camera = pose = None
    tray = TrayIcon()
    title = "eKL gaze switch - starts in PREVIEW"
    did_minimize = False
    try:
        if not model_path.is_file():
            raise RuntimeError(f"Missing model: {model_path}. See README Setup.")
        pose = PoseEstimator(model_path)
        camera, frame, index = open_camera(c)
        s, calibration = SwitchController(c), Calibration()
        link = SerialLink(c)
        last_command = "None (no serial opened)"
        smoothed, previous_time = None, None
        tray.start(initial_armed=False, initial_host=s.current_host, calibration=c.get("calibration"), error="")
        tray_snapshot = None
        while True:
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
            # Space and the tray Arm item are the only arm paths. Startup stays disarmed.
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
            pending = 0 if s.since is None else min(100, int((now-s.since)*100000/c["dwell_ms"]))
            lines = [
                f"{'ARMED' if s.armed else 'PREVIEW / DISARMED'} | assumed host: {s.current_host or 'UNKNOWN'} | camera {index}",
                f"Raw yaw: {'--' if raw is None else f'{raw:+.1f} deg'} | heading: {'--' if value is None else f'{value:+.1f} deg'} | {state}",
                f"{face_status} | dwell {pending}% | cooldown {remaining:.1f}s | {c['com_port']}/115200 8N1; no flow",
                f"Last: {last_command}",
                calibration.message,
                s.error or "Space arm/disarm | C calibrate/sample | X cancel calibration | Q/Esc quit",
                "Preview only: 1/2/3/4 sets assumed host WITHOUT sending. Click window for keys.",
                "Head heading, not eye-only gaze. Keep face visible. No face/multiple faces = no switch.",
            ]
            for row, text in enumerate(lines):
                cv2.putText(panel, text[:120], (10, 27+row*30), cv2.FONT_HERSHEY_SIMPLEX, .49,
                            (80, 240, 110) if row == 0 and s.armed else (230, 230, 230), 1, cv2.LINE_AA)
            canvas = np.zeros((preview.shape[0]+panel.shape[0], panel.shape[1], 3), dtype=np.uint8)
            canvas[:preview.shape[0], :preview.shape[1]] = preview
            canvas[preview.shape[0]:] = panel
            cv2.imshow(title, canvas)
            if minimized and not did_minimize:
                minimize_named_window(title)
                did_minimize = True
            key = cv2.waitKey(1) & 0xff
            if key in (27, ord('q'), ord('Q')):
                break
            if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                break
            if key == 32:
                if calibration.stage == "idle":
                    if s.armed:
                        s.set_armed(False)
                        link.close()
                    elif s.set_armed(True):
                        try:
                            link.open()
                        except Exception as exc:
                            last_command = f"ERROR opening serial: {exc}"
                            s.fail(str(exc))
                            link.close()
            elif key in (ord('c'), ord('C')):
                s.set_armed(False)
                link.close()
                calibration.key()
            elif key in (ord('x'), ord('X')):
                s.set_armed(False)
                link.close()
                calibration = Calibration()
            elif key in (ord('1'), ord('2'), ord('3'), ord('4')) and not s.armed:
                s.current_host = int(chr(key)); s.reset_dwell(); s.error = ""
            # Tray menu clicks only enqueue requests; serial/cv2 stay on this thread.
            quit_requested = False
            for action in tray.poll():
                if action == "quit":
                    quit_requested = True
                elif action == "show":
                    restore_named_window(title)
                elif action == "toggle_arm" and calibration.stage == "idle":
                    if s.armed:
                        s.set_armed(False)
                        link.close()
                    elif s.set_armed(True):
                        try:
                            link.open()
                        except Exception as exc:
                            last_command = f"ERROR opening serial: {exc}"
                            s.fail(str(exc))
                            link.close()
                elif action == "calibrate":
                    s.set_armed(False)
                    link.close()
                    calibration.key()
            if quit_requested:
                break
            snapshot = (s.armed, s.current_host, c.get("calibration"), s.error or "")
            if snapshot != tray_snapshot:
                tray.update(*snapshot)
                tray_snapshot = snapshot
            ok, frame = camera.read()
            if not ok or frame is None:
                s.set_armed(False)
                raise RuntimeError("Camera stopped delivering frames; switching disarmed; exiting")
    finally:
        tray.stop()
        if camera is not None:
            camera.release()
        if pose is not None:
            pose.close()
        if "link" in locals():
            link.close()
        cv2.destroyAllWindows()
        lock.close()


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
        run(args.config, args.model, minimized=args.minimized)
        return 0
    except Exception as exc:
        print(f"Stopped safely: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
