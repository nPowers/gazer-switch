# Guidance for AI agents

Gazer is a Windows utility that watches a webcam for a head turn and sends a short RS-232 command to a USB KVM switch. It is written for 64-bit Python 3.12 or 3.13.

## Safety

- Do not run the live loop, and do not open a camera or a serial port, during automated verification.
- Never change the serial settings: 115200 8N1, DTR/RTS off, no flow control. Send one raw write only. No retries and no baud sweep.
- Startup must remain preview/disarmed. Arm only after calibration, from the Space key or the tray Arm item.
- Never overwrite an existing `config.json`. Copy `config.example.json` only when `config.json` is missing.
- The installer may create a Startup shortcut only after a yes answer. That shortcut still starts disarmed.

## Tests

Run `python -m unittest -v` with fake serial and fake cameras only. Tests must not open a real camera or COM port.

`python gaze_switch.py --check` validates the config and imports. It must not open the camera or the COM port.

## Config and local files

`config.json` holds the user's port, camera, and calibration. It is gitignored. Commit only `config.example.json`.

Do not commit `.venv/`, `__pycache__/`, `logs/`, or log files such as `.tray.log`.

## Documentation

Keep the README hardware affiliate links and the Amazon Associate disclosure line unchanged unless the owner asks you to update them.
