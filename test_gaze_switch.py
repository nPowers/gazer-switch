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
        self.assertEqual(g.tray_tooltip(False, None, None, ""), "Gazer: needs calibration")
        self.assertEqual(g.tray_visual_state(False, cal, ""), "disarmed")
        self.assertEqual(
            g.tray_tooltip(False, 2, cal, ""),
            "Gazer: disarmed, host 2 (press Space or use the menu)",
        )
        self.assertEqual(g.tray_visual_state(True, cal, ""), "armed")
        self.assertEqual(g.tray_tooltip(True, 1, cal, ""), "Gazer: armed, host 1")
        self.assertEqual(g.tray_tooltip(True, 3, cal, ""), "Gazer: armed, host 3")
        self.assertEqual(g.tray_visual_state(False, cal, "bad echo"), "error")
        self.assertTrue(g.tray_tooltip(False, None, cal, "bad echo").startswith("Gazer: error"))
        self.assertEqual(g.tray_icon_rgb("armed"), (40, 180, 70))
        self.assertEqual(g.tray_icon_rgb("disarmed"), (140, 140, 140))
        self.assertEqual(g.tray_icon_rgb("error"), (220, 140, 30))

    def test_tray_requests_are_queued_not_executed(self):
        tray = g.TrayIcon()
        tray.requests.put("show")
        tray.requests.put("toggle_arm")
        tray.requests.put("quit")
        self.assertEqual(tray.poll(), ["show", "toggle_arm", "quit"])
        self.assertEqual(tray.poll(), [])


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
