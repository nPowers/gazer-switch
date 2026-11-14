# Design

Gazer is a local camera/head-yaw watcher that maps a look left or right to USB KVM switch ports (defaults: left to host 1, right to host 2).

It uses MediaPipe Tasks face rigid transform rather than eye-only gaze or crude face-box position. Left and right head poses are calibrated into screen-relative thresholds. The app starts in preview/disarmed mode. Calibration is required before you can arm it with Space or the tray menu.

Only one visible face is accepted. A continuous dwell (400 ms in the sample config) and a cooldown with a fresh dwell after each switch prevent repeat commands. The dwell resets on no face, a seam, or a stalled frame.

The controller is kept pure. A guarded dispatcher performs one raw serial write at 115200 8N1, with DTR/RTS off and no flow control. There is no echo retry; an exact echo acknowledges the assumed host; an error disarms the app and marks the host unknown.

The preview stays in the foreground. Gazer does not upload or record camera frames, and it does not arm itself at launch.

Unit tests use in-memory serial doubles and must never open a live camera or serial port.
