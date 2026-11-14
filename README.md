# Gazer

Glance at your left monitor or your right monitor. A USB switch moves your keyboard and mouse to the computer on that side.

Gazer watches which way your head is turned, using MediaPipe face landmarks. It runs on your PC. There is no cloud account, and it does not record you.

## How it works

1. A webcam sees your face.
2. Gazer turns that into a head angle (yaw). It is a head turn, not where your eyes look.
3. You calibrate once: look left, then look right.
4. After you arm it, a steady look to one side (a short dwell, 400 ms in the sample config) sends one command to the USB switch.
5. The switch moves the keyboard and mouse to that computer. The middle is a dead zone, so small movements do nothing.

The preview window shows the angle before anything is sent. Gazer starts disarmed and does not open the serial port until you arm it.

## Hardware

You need:

- An [eKL EKL-04UR](https://link.amazon/B0h7xOqn1) 4-port USB 3.0 switch, also sold as SV04, with an RS-232 3-pin control socket (Amazon ASIN B0DKNDBF49). This is the switch sold with the RS-232 cable. Other USB switches with RS-232 control may work if you edit the command bytes in `gaze_switch.py`. Only this switch is tested. The manufacturer's RS-232 note is [switcher_sv04.pdf](https://www.eklhd.com/upload/file/switcher_sv04.pdf).
- A real RS-232-level USB serial adapter, not a 3.3 V TTL adapter. Tested: [Waveshare](https://link.amazon/B09zFinYQ) USB to RS232/485 converter (FT232RNL), set to RS232 mode, termination set to NC. Any FTDI or Prolific RS-232 adapter should work.
- Three wires. TX and RX are crossed:

  | Adapter pin | Switch pin |
  | --- | --- |
  | TX | R |
  | RX | T |
  | GND | G |

- Any webcam: the laptop's built-in camera or a USB webcam. Put it roughly centered between or below the two monitors, facing you.
- Two monitors side by side. Plug each computer into its own port on the switch.

As an Amazon Associate I earn from qualifying purchases. The Amazon links above are affiliate links.

Gazer runs on one Windows PC: the one with the camera and the serial adapter. It does not need to run on the other computer.

Plug the webcam and the serial adapter into that PC, not into the switch. If they are on a switched USB port, the first glance disconnects them and Gazer cannot glance back. The button on the switch is the manual fallback.

## Install

You need 64-bit Python 3.12–3.13 for Windows from https://www.python.org/downloads/windows/ . During install, tick "Add python.exe to PATH" (or use the py launcher). If Python is missing, `install.bat` points you to that page and, if winget is available, can install Python 3.13 for you. Then close the window and run `install.bat` again.

1. Download or clone this folder.
2. Double-click `install.bat`.

The installer finds Python, creates a `.venv`, installs the requirements, and copies `config.example.json` to `config.json` only when `config.json` is not already there. It then lists serial ports and cameras and asks:

- which COM port the adapter is on
- which camera to use (a DirectShow name, or an index such as `0`)
- which switch port is on the left, and which is on the right

Press Enter to keep the value in brackets. Running the installer again keeps your saved calibration and only changes answers you change.

It finishes by running `gaze_switch.py --check`.

The face landmark model is already in `models/`. Gazer does not download it.

## First run and calibration

Double-click `run.bat`. Click the preview window so the keys work.

- **C** starts calibration. Look at the left monitor, press **C**, and hold your head still for one second. Then look at the right monitor, press **C**, and hold still again. The two poses need to be at least 8 degrees apart.
- **Space** arms or disarms. Arming waits until calibration is saved and Gazer knows which host is active.
- **1** or **2** (also **3** or **4**) sets the assumed host in the preview and does not send a command. Use this after you press the button on the switch. Gazer cannot ask the switch which port is active.
- **X** cancels an in-progress calibration. The saved calibration stays. Gazer stays disarmed.
- **Q** or **Esc**, or closing the window, quits and releases the webcam.

The picture is mirrored. The angle is measured on the real camera image, not the mirror. After calibration, left should read negative and right positive.

## Tray icon

While Gazer is running, a Gazer icon sits in the notification area (by the clock).

| Color | Meaning |
| --- | --- |
| Grey | Preview / disarmed, or still needs calibration |
| Green | Armed |
| Amber | Error (camera or serial) |

The tooltip says the same thing, for example `Gazer: armed, host 2` or `Gazer: needs calibration`. The number drawn on the icon is the assumed host.

Right-click the icon:

- **Show window** (also the double-click) restores the preview
- **Arm** / **Disarm** does what Space does. Arm stays disabled until you have calibrated and set the host
- **Calibrate** does what C does
- **Quit** closes the serial port, the camera, and the icon

Clicks only ask the main window to do the work. The tray thread does not touch the camera or the serial port. If the tray libraries are missing, Gazer still runs and prints a warning.

## Start at sign-in

`install.bat` asks whether to start Gazer when you sign in. Yes creates a Startup shortcut named **Gazer** that launches `start_minimized.bat` (the window starts minimized). No removes that shortcut if it is there.

The app still starts disarmed. Arm it with Space or the tray menu after you calibrate.

If a Startup shortcut named **eKL gaze switch** is already there, the installer leaves it alone and does not create a second one.

`uninstall.bat` removes the Gazer shortcut and the `.venv`. It asks before deleting `config.json`.

## Configuration

`config.json` is yours. It is not in git, because calibration is stored there. Edit it while Gazer is stopped, or answer the installer prompts. If the file is missing, the app copies `config.example.json` on the next start and does not replace a file that is already there.

| Key | Example | What it does |
| --- | --- | --- |
| `com_port` | `COM3` | Serial port for the adapter. `COM` plus a number. |
| `baud` | `115200` | Only 115200 is accepted. |
| `camera_index` | `0` | Used when `camera_name` is empty, and as the fallback if a named camera fails. 0 through 10. |
| `camera_name` | empty | DirectShow name, such as `Integrated Camera`. Empty or null means use `camera_index`. |
| `try_other_cameras` | `false` | When true, also try indexes 0-3 if the chosen camera fails. |
| `left_threshold_deg` | `-15` | Calibrated heading at or below this is "left". Allowed -40 to -1. |
| `right_threshold_deg` | `15` | Calibrated heading at or above this is "right". Allowed 1 to 40. |
| `dwell_ms` | `400` | How long you must hold the look. Allowed 250 to 10000. |
| `cooldown_ms` | `2000` | Wait after a switch before another can start. Allowed 2000 to 60000. A new full dwell is required. |
| `initial_host` | `1` | Assumed switch port at startup. 1, 2, 3, or 4. |
| `max_frame_gap_ms` | `300` | A longer gap resets the dwell. Allowed 50 to 500. |
| `calibration` | `null` | Filled in by the C flow: `left_yaw` and `right_yaw`. |
| `left_host` | `1` | Switch port used when you look left. 1-4, and not the same as `right_host`. |
| `right_host` | `2` | Switch port used when you look right. |

One face only. No face, two faces, a bad pose, the dead zone, or a frame gap resets the dwell. A second copy of Gazer is refused (`.watcher.lock`).

List devices without starting the preview:

```bat
.venv\Scripts\python.exe gaze_switch.py --list-ports
.venv\Scripts\python.exe gaze_switch.py --list-cameras
.venv\Scripts\python.exe gaze_switch.py --check
```

`--check` validates the config and imports. It does not open the camera or the COM port. `--list-ports` does not open a port. `--list-cameras` prints DirectShow names when it can (via pygrabber). If it cannot, it tries indexes 0-5 and reports which ones open.

## Serial protocol

115200 baud, 8 data bits, no parity, 1 stop bit. No flow control (no RTS/CTS, DSR/DTR, or XON/XOFF). DTR and RTS are held off. Each switch is one raw 3-byte write. The switch echoes those 3 bytes on success. There is no text, no line ending, no retry, and no baud sweep.

| Host | Bytes |
| --- | --- |
| 1 | `AA 00 56` |
| 2 | `AA 01 55` |
| 3 | `AA 02 54` |
| 4 | `AA 03 53` |

`left_host` and `right_host` choose which row a look sends. The defaults send host 1 to the left and host 2 to the right.

The assumed host changes only after the echo matches. A bad echo disarms Gazer, marks the host unknown, and does not try again. Press a host key (1-4) to say which port is active, then Space to arm.

Warning: send commands at 115200 only. Repeated commands at other baud rates can lock the switch until you power-cycle it.

## Troubleshooting

- **Wrong camera.** Run `gaze_switch.py --list-cameras`. Put the name in `camera_name`, or a number in `camera_index` with `camera_name` empty. You can also run `install.bat` again. Close other apps that are using the camera, and check Windows camera privacy.
- **The switch does not move.** TX goes to R and RX goes to T (they are crossed), GND goes to G, and the adapter is in RS-232 mode rather than TTL. Run `gaze_switch.py --list-ports` and set `com_port` to the adapter. FTDI, Prolific, and CH340 are tagged in that list. A chip tag does not by itself mean the adapter speaks RS-232 levels.
- **Keyboard is slow to wake after a switch.** That is the keyboard or the USB hub, not Gazer.

## Privacy

Camera frames stay in memory. They are not saved and they are not sent anywhere. The only file Gazer writes for a session is `config.json` on this PC (your settings and the two calibration angles). There is no account and no network call in the watcher.

## Tests

```bat
.venv\Scripts\python.exe -m unittest -v
```

Tests use stand-ins for the serial port and the camera. They do not open a real camera or a COM port.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

The bundled `models/face_landmarker.task` model comes from Google MediaPipe and is licensed under the Apache License 2.0. See [models/SOURCE.md](models/SOURCE.md).

## Docs

- `docs/DESIGN.md` — behavior and safety rules
