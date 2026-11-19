import unittest
import gaze_switch as g

class SafetyTests(unittest.TestCase):
    def cfg(self):
        c=g.default_config(); c['calibration']={'left_yaw':20.0,'right_yaw':-28.0}; return c
    def controller(self):
        s=g.SwitchController(self.cfg()); self.assertTrue(s.set_armed(True)); return s
    def test_start_disarmed_and_biased_reversed_calibration(self):
        c=self.cfg(); s=g.SwitchController(c); self.assertFalse(s.armed)
        for yaw,h in ((20,-24),(-28,24),(-4,0)): self.assertAlmostEqual(g.heading(yaw,c),h)
        for i in range(20): self.assertIsNone(s.update(-24,i*.1))
    def test_requires_calibration_and_safe_config(self):
        self.assertFalse(g.SwitchController(g.default_config()).set_armed(True))
        for change in ({'baud':9600},{'dwell_ms':0},{'left_threshold_deg':15},{'calibration':{'left_yaw':1,'right_yaw':2}}):
            c=g.default_config(); c.update(change)
            with self.assertRaises(ValueError): g.validate_config(c)
    def test_dwell_and_no_repeats(self):
        s=self.controller()
        for i in range(5): self.assertIsNone(s.update(-24,i*.2))
        self.assertEqual(s.update(-24,1.0),1); s.complete(1,1.0)
        for i in range(1,30): self.assertIsNone(s.update(-24,1+i*.2))
    def test_face_loss_seam_and_nan_reset_dwell(self):
        for value in (None,0,float('nan')):
            s=self.controller()
            for i in range(5): s.update(-24,i*.2)
            self.assertIsNone(s.update(value,.9)); self.assertIsNone(s.update(-24,1.0))
    def test_frame_stall_resets_dwell(self):
        s=self.controller(); s.update(-24,0); self.assertIsNone(s.update(-24,10))
    def test_cooldown_requires_fresh_dwell(self):
        s=self.controller(); s.complete(1,0)
        for i in range(11): self.assertIsNone(s.update(24,i*.2))
        for i in range(1,5): self.assertIsNone(s.update(24,2+i*.2))
        self.assertEqual(s.update(24,3.0),2)
    def test_failure_disarms(self):
        s=self.controller(); s.fail('bad echo'); self.assertFalse(s.armed)
        self.assertIsNone(s.current_host); self.assertIsNone(s.update(-24,99)); self.assertFalse(s.set_armed(True))
    def test_rearm_resets_dwell(self):
        s=self.controller()
        for i in range(5): s.update(-24,i*.2)
        s.set_armed(False); s.set_armed(True); self.assertIsNone(s.update(-24,.95))

class FakeSerial:
    instances=[]; echo=b'\xaa\x00\x56'
    def __init__(self,port=None):
        self.port=port; self.writes=[]; self.closed=False; self.is_open=False
        self.__class__.instances.append(self)
    def open(self):
        assert self.baudrate==115200 and self.bytesize==8 and self.parity=='N' and self.stopbits==1
        assert self.rts is False and self.dtr is False
        assert not self.rtscts and not self.dsrdtr and not self.xonxoff
        self.is_open=True
    def write(self,data): self.writes.append(data); return len(data)
    def read(self,size): return self.echo[:size]
    def flush(self): pass
    def reset_input_buffer(self): pass
    def close(self): self.closed=True

class SerialTests(unittest.TestCase):
    def setUp(self): FakeSerial.instances=[]; FakeSerial.echo=b'\xaa\x00\x56'
    def test_one_raw_write_after_config_and_close(self):
        sleeps=[]; echo=g.send_host(1,g.default_config(),serial_factory=FakeSerial,sleeper=sleeps.append)
        self.assertEqual(echo,b'\xaa\x00\x56')
        self.assertEqual(FakeSerial.instances[0].writes,[b'\xaa\x00\x56'])
        self.assertTrue(FakeSerial.instances[0].closed); self.assertEqual(sleeps,[g.OPEN_SETTLE_S])
    def test_bad_echo_no_retry(self):
        FakeSerial.echo=b'\xfe'
        with self.assertRaises(RuntimeError): g.send_host(1,g.default_config(),serial_factory=FakeSerial,sleeper=lambda _:None)
        self.assertEqual(len(FakeSerial.instances[0].writes),1); self.assertTrue(FakeSerial.instances[0].closed)
    def test_invalid_host_never_opens(self):
        with self.assertRaises(ValueError): g.send_host(5,g.default_config(),serial_factory=FakeSerial)
        self.assertEqual(FakeSerial.instances,[])
    def test_hosts_three_and_four_one_raw_write(self):
        FakeSerial.echo=bytes.fromhex('AA 02 54')
        echo=g.send_host(3,g.default_config(),serial_factory=FakeSerial,sleeper=lambda _s: None)
        self.assertEqual(echo,bytes.fromhex('AA 02 54'))
        self.assertEqual(FakeSerial.instances[0].writes,[bytes.fromhex('AA 02 54')])
        self.assertEqual(len(FakeSerial.instances),1)
        FakeSerial.instances.clear()
        FakeSerial.echo=bytes.fromhex('AA 03 53')
        echo=g.send_host(4,g.default_config(),serial_factory=FakeSerial,sleeper=lambda _s: None)
        self.assertEqual(echo,bytes.fromhex('AA 03 53'))
        self.assertEqual(FakeSerial.instances[0].writes,[bytes.fromhex('AA 03 53')])
        self.assertTrue(FakeSerial.instances[0].closed)
    def test_link_reuses_open_port(self):
        sleeps=[]
        link=g.SerialLink(g.default_config(),serial_factory=FakeSerial,sleeper=sleeps.append)
        self.assertEqual(link.send(1),b'\xaa\x00\x56')
        FakeSerial.echo=b'\xaa\x01\x55'
        self.assertEqual(link.send(2),b'\xaa\x01\x55')
        self.assertEqual(len(FakeSerial.instances),1)
        self.assertEqual(FakeSerial.instances[0].writes,[b'\xaa\x00\x56',b'\xaa\x01\x55'])
        self.assertEqual(sleeps,[g.OPEN_SETTLE_S])
        self.assertFalse(FakeSerial.instances[0].closed)
        link.close()
        self.assertTrue(FakeSerial.instances[0].closed)

class CalibrationTests(unittest.TestCase):
    def test_capture_left_then_right_and_mapping(self):
        c=g.Calibration(); c.key(); self.assertEqual(c.stage,'left_ready'); c.key()
        for i in range(11): result=c.update(20,i*.1)
        self.assertEqual(c.stage,'right_ready'); c.key()
        for i in range(11): result=c.update(-28,2+i*.1)
        self.assertEqual(result,{'left_yaw':20,'right_yaw':-28})
        self.assertEqual(c.stage,'idle')
    def test_face_loss_requires_new_sample(self):
        c=g.Calibration(); c.key(); c.key()
        for i in range(8): c.update(20,i*.1)
        c.update(None,.8)
        self.assertIsNone(c.update(20,1.1)); self.assertEqual(c.stage,'left_collect')
    def test_save_and_reload(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.json'
            g.save_config(path,g.default_config())
            self.assertEqual(g.load_config(path),g.default_config())
    def test_classification_boundaries(self):
        c=g.default_config()
        for value,state,target in ((-15,'LEFT',1),(15,'RIGHT',2),(0,'DEAD ZONE',None),(None,'NO FACE',None)):
            self.assertEqual(g.classify(value,c),(state,target))


class TrayTests(unittest.TestCase):
    def test_tooltip_and_visual_states(self):
        cal = {"left_yaw": 20.0, "right_yaw": -28.0}
        self.assertEqual(g.tray_visual_state(False, None, ""), "needs_calibration")
        self.assertEqual(g.tray_tooltip(False, None, None, ""), "Gazer: paused (needs calibration)")
        self.assertEqual(g.tray_visual_state(False, cal, ""), "paused")
        self.assertEqual(g.tray_tooltip(False, 2, cal, ""), "Gazer: paused (disarmed), host 2")
        self.assertEqual(g.tray_tooltip(False, None, cal, ""), "Gazer: paused (disarmed)")
        self.assertEqual(g.tray_visual_state(True, cal, ""), "armed")
        self.assertEqual(g.tray_tooltip(True, 1, cal, ""), "Gazer: running (armed), host 1")
        self.assertEqual(g.tray_tooltip(True, 3, cal, ""), "Gazer: running (armed), host 3")
        self.assertEqual(g.tray_tooltip(True, None, cal, ""), "Gazer: running (armed)")
        self.assertEqual(g.tray_visual_state(False, cal, "bad echo"), "error")
        self.assertTrue(g.tray_tooltip(False, None, cal, "bad echo").startswith("Gazer: error"))
        self.assertEqual(g.tray_icon_rgb("armed"), (40, 180, 70))
        self.assertEqual(g.tray_icon_rgb("paused"), (140, 140, 140))
        self.assertEqual(g.tray_icon_rgb("disarmed"), (140, 140, 140))
        self.assertEqual(g.tray_icon_rgb("error"), (220, 140, 30))

    def test_menu_has_pause_resume_relaunch_and_window(self):
        cal = {"left_yaw": 20.0, "right_yaw": -28.0}
        paused = g.tray_menu_spec(False, cal, 2, "", True)
        self.assertEqual(
            [item["label"] for item in paused],
            ["Show window", "Hide window", "Resume", "Calibrate", "Relaunch", "Quit"],
        )
        self.assertTrue(paused[0]["default"])
        self.assertTrue([item for item in paused if item["action"] == "resume"][0]["enabled"])
        running = g.tray_menu_spec(True, cal, 3, "", True)
        self.assertEqual([item["label"] for item in running if item["action"] == "pause"], ["Pause"])
        blocked = g.tray_menu_spec(False, None, 2, "", False)
        resume = [item for item in blocked if item["action"] == "resume"][0]
        self.assertFalse(resume["enabled"])
        self.assertIn("calibrate", resume["label"].lower())
        no_host = g.tray_menu_spec(False, cal, None, "", True)
        self.assertFalse([item for item in no_host if item["action"] == "resume"][0]["enabled"])
        self.assertIn("host", [item for item in no_host if item["action"] == "resume"][0]["label"].lower())
        hidden = g.tray_menu_spec(False, cal, 2, "", False)
        self.assertFalse([item for item in hidden if item["action"] == "hide"][0]["enabled"])
        errored = g.tray_menu_spec(False, cal, 2, "bad echo", True)
        self.assertFalse([item for item in errored if item["action"] == "resume"][0]["enabled"])

    def test_tray_requests_are_queued_not_executed(self):
        tray = g.TrayIcon()
        tray.requests.put("show")
        tray.requests.put("pause")
        tray.requests.put("resume")
        tray.requests.put("relaunch")
        tray.requests.put("quit")
        self.assertEqual(tray.poll(), ["show", "pause", "resume", "relaunch", "quit"])
        self.assertEqual(tray.poll(), [])


class ControlPlanTests(unittest.TestCase):
    def cfg(self):
        c = g.default_config()
        c["calibration"] = {"left_yaw": 20.0, "right_yaw": -28.0}
        return c

    def test_pause_disarms_and_sends_no_switch_command(self):
        s = g.SwitchController(self.cfg())
        self.assertTrue(s.set_armed(True))
        link = g.SerialLink(self.cfg(), serial_factory=FakeSerial, sleeper=lambda _s: None)
        self.assertEqual(link.send(1), bytes.fromhex("AA 00 56"))
        writes = list(FakeSerial.instances[-1].writes)
        plan = g.plan_control("pause", {"armed": True, "can_arm": True, "calibrating": False, "window_visible": True})
        self.assertFalse(plan["armed"])
        self.assertTrue(plan["close_serial"])
        self.assertFalse(plan["open_serial"])
        self.assertFalse(plan["quit"])
        self.assertNotIn("send", plan)
        g.apply_arm_plan(s, link, plan)
        self.assertFalse(s.armed)
        self.assertTrue(FakeSerial.instances[-1].closed)
        self.assertEqual(FakeSerial.instances[-1].writes, writes)
        self.assertIsNone(s.update(-24, 10))

    def test_resume_opens_the_port_without_writing_a_command(self):
        FakeSerial.instances.clear()
        s = g.SwitchController(self.cfg())
        link = g.SerialLink(self.cfg(), serial_factory=FakeSerial, sleeper=lambda _s: None)
        plan = g.plan_control("resume", {"armed": False, "can_arm": True, "calibrating": False, "window_visible": False})
        self.assertTrue(plan["armed"])
        self.assertTrue(plan["open_serial"])
        self.assertIsNone(g.apply_arm_plan(s, link, plan))
        self.assertTrue(s.armed)
        self.assertEqual(FakeSerial.instances[-1].writes, [])
        self.assertTrue(FakeSerial.instances[-1].is_open)
        link.close()

    def test_resume_refuses_without_calibration_or_during_sampling(self):
        blocked = g.plan_control("resume", {"armed": False, "can_arm": False, "calibrating": False, "window_visible": True})
        sampling = g.plan_control("resume", {"armed": False, "can_arm": True, "calibrating": True, "window_visible": True})
        for plan in (blocked, sampling):
            self.assertFalse(plan["armed"])
            self.assertFalse(plan["open_serial"])
            self.assertFalse(plan["close_serial"])

    def test_resume_serial_failure_disarms_without_a_retry_write(self):
        class Boom(FakeSerial):
            def open(self):
                raise RuntimeError("port busy")
        s = g.SwitchController(self.cfg())
        link = g.SerialLink(self.cfg(), serial_factory=Boom, sleeper=lambda _s: None)
        plan = g.plan_control("resume", {"armed": False, "can_arm": True, "calibrating": False, "window_visible": True})
        message = g.apply_arm_plan(s, link, plan)
        self.assertIn("port busy", message)
        self.assertFalse(s.armed)
        self.assertIsNone(s.current_host)
        self.assertEqual(Boom.instances[-1].writes, [])

    def test_closing_the_preview_hides_and_keeps_running(self):
        self.assertEqual(g.note_preview_visibility(0), "closed")
        self.assertEqual(g.note_preview_visibility(-1), "closed")
        self.assertEqual(g.note_preview_visibility(1), "open")
        self.assertEqual(g.note_preview_visibility("nope"), "closed")
        state = {"armed": True, "can_arm": True, "calibrating": False, "window_visible": True}
        plan = g.plan_control("preview_closed", state)
        self.assertEqual(plan["window"], "hide")
        self.assertFalse(plan["quit"])
        self.assertTrue(plan["armed"])
        self.assertFalse(plan["close_serial"])
        self.assertEqual(g.plan_control("hide", state)["window"], "hide")
        self.assertEqual(g.plan_control("show", state)["window"], "show")
        self.assertEqual(g.preview_dismiss_action(True), "preview_closed")
        self.assertEqual(g.preview_dismiss_action(False), "quit")

    def test_quit_releases_serial_and_relaunch_starts_a_replacement(self):
        state = {"armed": True, "can_arm": True, "calibrating": False, "window_visible": True}
        quit_plan = g.plan_control("quit", state)
        self.assertTrue(quit_plan["quit"])
        self.assertFalse(quit_plan["relaunch"])
        self.assertTrue(quit_plan["close_serial"])
        self.assertFalse(quit_plan["armed"])
        relaunch = g.plan_control("relaunch", state)
        self.assertTrue(relaunch["quit"])
        self.assertTrue(relaunch["relaunch"])
        self.assertTrue(relaunch["close_serial"])
        self.assertFalse(relaunch["armed"])
        command = g.build_relaunch_command(
            __import__("pathlib").Path("config.json"),
            __import__("pathlib").Path("model.task"),
            executable="pythonw.exe",
        )
        self.assertEqual(command[0], "pythonw.exe")
        self.assertTrue(command[1].endswith("gaze_switch.py"))
        self.assertIn("--replace", command)
        self.assertNotIn("--minimized", command)
        self.assertIn("config.json", command)
        seen = {}
        def popen(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return "proc"
        self.assertEqual(g.spawn_detached(["pythonw.exe", "gaze_switch.py", "--replace"], popen=popen), "proc")
        self.assertEqual(seen["kwargs"]["stdin"], __import__("subprocess").DEVNULL)
        if __import__("sys").platform == "win32":
            self.assertIn("creationflags", seen["kwargs"])
        else:
            self.assertTrue(seen["kwargs"]["start_new_session"])

    def test_camera_reopen_retries_then_raises_without_touching_serial(self):
        calls = []
        sleeps = []
        class Cap:
            def __init__(self, source):
                calls.append(source)
            def isOpened(self):
                return len(calls) >= 3
            def release(self):
                pass
        c = g.default_config()
        _camera, frame, which = g.open_camera_retrying(
            c, attempts=4, sleeper=sleeps.append,
            capture_factory=Cap, read_frame=lambda cam: "frame" if cam.isOpened() else None,
        )
        self.assertEqual(frame, "frame")
        self.assertEqual(which, c["camera_index"])
        self.assertEqual(len(sleeps), 2)
        class Closed:
            def isOpened(self):
                return False
            def release(self):
                pass
        before = len(sleeps)
        with self.assertRaises(RuntimeError):
            g.open_camera_retrying(
                c, attempts=1, sleeper=sleeps.append,
                capture_factory=lambda _source: Closed(), read_frame=lambda cam: None,
            )
        self.assertEqual(len(sleeps), before)


class InstanceTests(unittest.TestCase):
    def test_second_launch_focuses_and_does_not_take_the_lock(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            first = g.claim_watcher(folder)
            self.assertIsNotNone(first)
            restored = []
            second = g.claim_watcher(folder, restore=lambda: restored.append("shown"))
            self.assertIsNone(second)
            self.assertEqual(restored, ["shown"])
            self.assertEqual(g.drain_instance_commands(folder), ["show"])
            self.assertEqual(g.drain_instance_commands(folder), [])
            still = g.claim_watcher(folder, restore=lambda: None)
            self.assertIsNone(still)
            first.release()
            third = g.claim_watcher(folder, restore=lambda: self.fail("focused a free lock"))
            self.assertIsNotNone(third)
            third.release()

    def test_new_owner_discards_a_stale_quit(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            g.post_instance_command(folder, "quit")
            lock = g.claim_watcher(folder)
            self.assertIsNotNone(lock)
            self.assertEqual(g.drain_instance_commands(folder), [])
            lock.release()
            with self.assertRaises(ValueError):
                g.post_instance_command(folder, "relaunch")

    def test_replace_waits_for_the_holder_to_quit(self):
        import tempfile, threading, time
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            holder = g.claim_watcher(folder)
            def release_on_quit():
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if "quit" in g.drain_instance_commands(folder):
                        holder.release()
                        return
                    time.sleep(0.01)
            thread = threading.Thread(target=release_on_quit, daemon=True)
            thread.start()
            killed = []
            got = g.claim_watcher(
                folder, replace=True, process_table=lambda: [(99, "python gaze_switch.py")],
                terminate=killed.append, timeout=2,
            )
            self.assertIsNotNone(got)
            self.assertEqual(killed, [])
            got.release()
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_replace_terminates_a_gazer_that_ignores_quit(self):
        import os, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            first = g.claim_watcher(folder)
            killed = []
            def table():
                return [
                    (os.getpid(), "python gaze_switch.py --replace"),
                    (4321, r"C:\Gazer\.venv\Scripts\pythonw.exe C:\Gazer\gaze_switch.py --minimized"),
                    (50, "python -m unittest"),
                    (51, "notepad.exe readme"),
                ]
            def killer(pid):
                killed.append(pid)
                first.release()
            got = g.claim_watcher(
                folder, replace=True, process_table=table, terminate=killer,
                sleeper=lambda _dt: None, clock=lambda: 0.0, timeout=0,
            )
            self.assertEqual(killed, [4321])
            self.assertIsNotNone(got)
            self.assertEqual(g.drain_instance_commands(folder), [])
            got.release()

    def test_replace_when_nothing_is_running_does_not_kill(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            killed = []
            lock = g.claim_watcher(
                Path(folder), replace=True,
                process_table=lambda: [(7, "pythonw.exe gaze_switch.py")],
                terminate=killed.append,
            )
            self.assertIsNotNone(lock)
            self.assertEqual(killed, [])
            lock.release()

    def test_replace_reports_when_the_camera_stays_busy(self):
        import os, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            first = g.claim_watcher(folder)
            killed = []
            try:
                with self.assertRaises(RuntimeError):
                    g.claim_watcher(
                        folder, replace=True, process_table=lambda: [],
                        terminate=killed.append, sleeper=lambda _dt: None,
                        clock=lambda: 0.0, timeout=0,
                    )
            finally:
                first.release()
            self.assertEqual(killed, [])
            self.assertIsNone(g.owner_pid_from_lock(first.path, me=os.getpid()))

    def test_replace_stops_the_pid_recorded_in_the_lock(self):
        import os, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            first = g.claim_watcher(folder)
            recorded = first.path.read_bytes()
            self.assertTrue(recorded.startswith(b"0"))
            self.assertIn(f"{os.getpid():010d}".encode("ascii"), recorded)
            with open(first.path, "r+b") as handle:
                handle.seek(1)
                handle.write(b"0000004321\n")
                handle.flush()
            self.assertEqual(g.owner_pid_from_lock(first.path), 4321)
            killed = []
            def killer(pid):
                killed.append(pid)
                first.release()
            got = g.claim_watcher(
                folder, replace=True, process_table=lambda: [], terminate=killer,
                sleeper=lambda _dt: None, clock=lambda: 0.0, timeout=0,
            )
            self.assertEqual(killed, [4321])
            self.assertIsNotNone(got)
            got.release()
            self.assertIsNone(g.owner_pid_from_lock(Path(folder) / "missing.lock"))
            garbage = folder / "garbage.lock"
            garbage.write_bytes(b"0nope\n")
            self.assertIsNone(g.owner_pid_from_lock(garbage))
            self.assertIsNone(g.owner_pid_from_lock(first.path))

    def test_gazer_command_lines_skip_this_process_and_unrelated_ones(self):
        rows = [
            (10, r"C:\Python\pythonw.exe C:\Gazer\gaze_switch.py --minimized"),
            (11, "python -m unittest"),
            (12, "/usr/bin/python3 /tmp/gaze_switch.py --replace"),
            (13, "notepad.exe"),
        ]
        self.assertEqual(g.gazer_pids_from_table(rows, me=12), [10])
        self.assertTrue(g.is_gazer_command(rows[0][1]))
        self.assertFalse(g.is_gazer_command(rows[1][1]))
        self.assertFalse(g.is_gazer_command("python editor.py"))


class PortableConfigTests(unittest.TestCase):
    def test_camera_name_optional_and_name_lock_kept(self):
        for name in ("", None, "   "):
            c=g.default_config(); c["camera_name"]=name
            self.assertEqual(g.validate_config(c)["camera_name"], "")
        c=g.default_config(); c["camera_name"]="Integrated Camera"
        self.assertEqual(g.validate_config(c)["camera_name"], "Integrated Camera")
        c["camera_name"]=3
        with self.assertRaises(ValueError): g.validate_config(c)

    def test_camera_open_plan_skips_blank_name(self):
        c=g.default_config(); c["camera_name"]=""; c["camera_index"]=1
        self.assertEqual(g.camera_open_plan(c), [("index", 1)])
        c["camera_name"]=None
        self.assertEqual(g.camera_open_plan(c), [("index", 1)])
        c["camera_name"]="Integrated Camera"
        self.assertEqual(g.camera_open_plan(c), [("name", "Integrated Camera"), ("index", 1)])
        c["try_other_cameras"]=True
        self.assertEqual(g.camera_open_plan(c), [
            ("name", "Integrated Camera"), ("index", 1), ("index", 0), ("index", 2), ("index", 3),
        ])

    def test_open_camera_uses_injected_factory_only(self):
        seen=[]
        class Cap:
            def __init__(self, source):
                self.source=source; seen.append(source); self.released=False
            def isOpened(self): return self.source==2
            def release(self): self.released=True
        c=g.default_config(); c["camera_name"]=""; c["camera_index"]=2
        camera, frame, which=g.open_camera(c, capture_factory=Cap, read_frame=lambda cam: "frame")
        self.assertEqual(seen, [2]); self.assertEqual(frame, "frame"); self.assertEqual(which, 2)
        seen.clear()
        c["camera_name"]="Desk Cam"; c["camera_index"]=2
        camera, frame, which=g.open_camera(
            c, capture_factory=Cap, read_frame=lambda cam: "frame" if cam.isOpened() else None)
        self.assertEqual(seen, ["Desk Cam", 2]); self.assertEqual(which, 2)

    def test_side_hosts_default_to_today_and_can_change(self):
        c=g.default_config()
        self.assertEqual(g.validate_config(c)["left_host"], 1)
        self.assertEqual(c["right_host"], 2)
        self.assertEqual(g.classify(-15, c), ("LEFT", 1))
        self.assertEqual(g.classify(15, c), ("RIGHT", 2))
        c["left_host"], c["right_host"]=4, 3
        g.validate_config(c)
        self.assertEqual(g.classify(-20, c), ("LEFT", 4))
        self.assertEqual(g.classify(20, c), ("RIGHT", 3))
        c["calibration"]={"left_yaw":20.0,"right_yaw":-28.0}
        s=g.SwitchController(c); self.assertTrue(s.set_armed(True))
        for i in range(5): self.assertIsNone(s.update(-24, i*.2))
        self.assertEqual(s.update(-24, 1.0), 4)
        for bad in (0, 5, True, "2"):
            broken=g.default_config(); broken["left_host"]=bad
            with self.assertRaises(ValueError): g.validate_config(broken)
        same=g.default_config(); same["left_host"]=same["right_host"]=2
        with self.assertRaises(ValueError): g.validate_config(same)
        wide=g.default_config(); wide["initial_host"]=4
        self.assertEqual(g.validate_config(wide)["initial_host"], 4)

    def test_example_config_is_portable(self):
        from pathlib import Path
        example=Path(__file__).resolve().parent/"config.example.json"
        c=g.load_config(example)
        self.assertEqual(c["com_port"], "COM3")
        self.assertEqual(c["camera_name"], "")
        self.assertIsNone(c["calibration"])
        self.assertEqual(c["initial_host"], 1)
        self.assertEqual(c["left_host"], 1)
        self.assertEqual(c["right_host"], 2)
        self.assertEqual(c["dwell_ms"], 400)
        self.assertEqual(c["baud"], 115200)

    def test_ensure_config_copies_only_when_missing(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            example=folder/"config.example.json"
            example.write_text('{"copied":true}\n', encoding="utf-8")
            target=folder/"config.json"
            self.assertTrue(g.ensure_config(target, example))
            self.assertEqual(target.read_bytes(), example.read_bytes())
            target.write_text('{"calibration":{"left_yaw":1.0,"right_yaw":20.0}}\n', encoding="utf-8")
            kept=target.read_bytes()
            example.write_text('{"copied":false}\n', encoding="utf-8")
            self.assertFalse(g.ensure_config(target, example))
            self.assertEqual(target.read_bytes(), kept)
            with self.assertRaises(FileNotFoundError):
                g.ensure_config(folder/"missing.json", folder/"no-example.json")

    def test_setup_choices_keep_calibration_unless_a_choice_changes(self):
        import json, tempfile
        from pathlib import Path
        original={
            "com_port":"COM7","baud":115200,"camera_index":0,"camera_name":"Integrated Camera",
            "try_other_cameras":False,"left_threshold_deg":-15.0,"right_threshold_deg":15.0,
            "dwell_ms":400,"cooldown_ms":2000,"initial_host":2,"max_frame_gap_ms":300,
            "calibration":{"left_yaw":12.5,"right_yaw":-30.0},
        }
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"config.json"
            path.write_text(json.dumps(original, indent=2)+"\n", encoding="utf-8")
            before=path.read_bytes()
            self.assertFalse(g.write_setup_choices(
                path, com_port="com7", camera="Integrated Camera", left_host=1, right_host=2))
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(g.write_setup_choices(
                path, com_port="COM4", camera="0", left_host=2, right_host=1))
            loaded=json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["calibration"], original["calibration"])
            self.assertEqual(loaded["dwell_ms"], 400)
            self.assertEqual(loaded["initial_host"], 2)
            self.assertEqual(loaded["com_port"], "COM4")
            self.assertEqual(loaded["camera_index"], 0)
            self.assertEqual(loaded["camera_name"], "")
            self.assertEqual(loaded["left_host"], 2)
            self.assertEqual(loaded["right_host"], 1)


class ListingTests(unittest.TestCase):
    def test_ports_show_vid_pid_and_known_chips(self):
        class Port:
            def __init__(self, device, description, vid, pid, manufacturer="", hwid=""):
                self.device=device; self.description=description; self.vid=vid; self.pid=pid
                self.manufacturer=manufacturer; self.hwid=hwid
        rows=g.describe_ports([
            Port("COM3", "USB Serial Port", 0x0403, 0x6001, "FTDI"),
            Port("COM4", "USB-SERIAL CH340", 0x1A86, 0x7523),
            Port("COM5", "Prolific USB-to-Serial Comm Port", 0x067B, 0x2303),
            Port("COM8", "USB-SERIAL CH340", None, None),
            Port("COM1", "Communications Port", None, None),
        ])
        text=g.format_port_report(rows)
        self.assertIn("COM3", text); self.assertIn("0403:6001", text); self.assertIn("[FTDI]", text)
        self.assertIn("[CH340]", text); self.assertIn("[Prolific]", text)
        self.assertIn("COM1", text)
        plain=[row for row in rows if row["device"]=="COM1"][0]
        self.assertEqual(plain["chip"], "")
        self.assertEqual(plain["vidpid"], "")

    def test_cameras_prefer_names_and_otherwise_probe_without_real_devices(self):
        probed=[]
        rows, source, _detail=g.discover_cameras(
            name_source=lambda: ["Integrated Camera", "USB Cam"],
            probe=lambda index: probed.append(index) or True,
        )
        self.assertEqual(source, "dshow")
        self.assertEqual(probed, [])
        self.assertEqual(rows[0]["name"], "Integrated Camera")
        self.assertEqual(rows[1]["index"], 1)
        def fail():
            raise RuntimeError("no directshow")
        def probe(index):
            probed.append(index)
            return index==1
        probed.clear()
        rows, source, detail=g.discover_cameras(name_source=fail, probe=probe)
        self.assertEqual(source, "probe")
        self.assertEqual(probed, [0, 1, 2, 3, 4, 5])
        self.assertIn("no directshow", detail)
        self.assertTrue(rows[1]["opened"]); self.assertFalse(rows[0]["opened"])
        text=g.format_camera_report(rows, source, detail)
        self.assertIn("0-5", text); self.assertIn("opened", text)
        named=g.format_camera_report(
            [{"index":0,"name":"Integrated Camera","opened":None}], "dshow", "")
        self.assertIn("Integrated Camera", named)
        self.assertNotIn("opened", named)

    def test_list_flags_do_not_touch_config_or_devices(self):
        import io, tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            target=folder/"config.json"
            buf=io.StringIO()
            with patch.object(g, "iter_serial_ports", return_value=[]), \
                 patch.object(g, "discover_cameras", return_value=([{"index":0,"name":"Desk","opened":None}], "dshow", "")), \
                 redirect_stdout(buf):
                code=g.main(["--config", str(target), "--list-ports", "--list-cameras"])
            self.assertEqual(code, 0)
            self.assertFalse(target.exists())
            text=buf.getvalue()
            self.assertIn("Serial ports", text)
            self.assertIn("Desk", text)

if __name__=='__main__': unittest.main()
