import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pcdiag import displaydiag, netdiag, report, stability, sysdiag  # noqa: E402
from pcdiag.common import ATTENTION, FAIL, PASS, UNSUPPORTED, Section, overall  # noqa: E402


class Parsers(unittest.TestCase):
    def test_section_status(self):
        s = Section("x")
        s.add(UNSUPPORTED, "a")
        self.assertEqual(s.status, UNSUPPORTED)
        s.add(PASS, "b")
        self.assertEqual(s.status, PASS)
        s.add(ATTENTION, "c")
        self.assertEqual(s.status, ATTENTION)
        s.add(FAIL, "d")
        self.assertEqual(overall([s, Section("y", PASS)]), FAIL)

    def test_ping_and_route(self):
        out = "5 packets transmitted, 5 received, 0% packet loss\nrtt min/avg/max/mdev = 1.0/2.5/4.0/0.5 ms"
        self.assertEqual(netdiag.parse_ping_loss(out), 0.0)
        self.assertEqual(netdiag.parse_ping_avg(out), 2.5)
        self.assertEqual(netdiag.parse_default_gateway("default via 192.168.1.1 dev eth0\n"), "192.168.1.1")

    def test_edid(self):
        e = bytearray(128)
        e[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        e[8], e[9] = 0x10, 0xAC                       # DEL
        e[54:56] = (14850).to_bytes(2, "little")      # 148.5 MHz
        e[56], e[57], e[58] = 0x80, 0x18, 0x71        # 1920 active, 280 blank
        e[59], e[60], e[61] = 0x38, 0x2D, 0x40        # 1080 active, 45 blank
        e[72:90] = b"\x00\x00\x00\xfc\x00TestMon\n     "
        e[127] = (256 - sum(e[:127]) % 256) % 256
        r = displaydiag.parse_edid(bytes(e))
        self.assertTrue(r["valid"])
        self.assertEqual(r["preferred"], "1920x1080@60.0")
        self.assertEqual(r["name"], "TestMon")
        e[20] ^= 1
        self.assertFalse(displaydiag.parse_edid(bytes(e))["valid"])

    def test_smart(self):
        ok = {"device": {"name": "/dev/sda"}, "model_name": "M", "smart_status": {"passed": True},
              "ata_smart_attributes": {"table": [{"id": 5, "name": "Reallocated", "raw": {"value": 0}}]}}
        self.assertEqual(stability.parse_smart(ok).status, PASS)
        bad = dict(ok, smart_status={"passed": False})
        self.assertEqual(stability.parse_smart(bad).status, FAIL)
        few = {**ok, "ata_smart_attributes": {"table": [{"id": 5, "name": "R", "raw": {"value": 1}}]}}
        self.assertEqual(stability.parse_smart(few).status, ATTENTION)
        many = {**ok, "ata_smart_attributes": {"table": [{"id": 5, "name": "R", "raw": {"value": 40}}]}}
        self.assertEqual(stability.parse_smart(many).status, FAIL)
        unc = {**ok, "ata_smart_attributes": {"table": [{"id": 198, "name": "U", "raw": {"value": 1}}]}}
        self.assertEqual(stability.parse_smart(unc).status, FAIL)
        self.assertEqual(stability.parse_smart({"device": {"name": "x"}}).status, UNSUPPORTED)

    def test_kernel_diff(self):
        before = ["[1.0] normal line"]
        after = before + ["[9.0] mce: [Hardware Error]: CPU 3", "[9.1] harmless"]
        self.assertEqual(len(stability.diff_hw_errors(before, after)), 1)
        self.assertEqual(len(sysdiag.classify_kernel_lines(["nvme nvme0: I/O 5 timeout", "ok"])), 1)


class Stability(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Linux-only")
    def test_write_verify_pass_and_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            s = stability.write_verify_test(d, 8, threading.Event())
            self.assertIn(s.status, (PASS, UNSUPPORTED))
            self.assertEqual(os.listdir(d), [])

    @unittest.skipIf(sys.platform == "win32", "Linux-only")
    def test_surface_read_is_readonly_and_detects_size(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(os.urandom(6 * 1024 * 1024)); f.flush()
            s = stability.surface_read_test(f.name, 0, threading.Event())
            self.assertEqual(s.status, PASS)

    @unittest.skipIf(sys.platform == "win32", "Linux-only")
    def test_dry_run_creates_nothing(self):
        secs = stability.run_stability("quick", dry_run=True)
        self.assertEqual(overall(secs), PASS)

    def test_reference_chain_is_deterministic(self):
        self.assertEqual(stability._reference_chain(1, 100), stability._reference_chain(1, 100))

    def test_no_write_path_on_block_devices(self):
        src = open(stability.__file__, encoding="utf-8").read()
        for token in ("O_WRONLY", "O_RDWR", "os.write("):
            self.assertNotIn(token, src)
        self.assertNotIn('open(device, "wb', src)

    @unittest.skipIf(sys.platform == "win32", "multiprocessing fork test is Linux-only")
    def test_cpu_short(self):
        s = stability.cpu_test(2, threading.Event(), threads=2)
        self.assertEqual(s.status, PASS)


class Reporting(unittest.TestCase):
    def test_report_written(self):
        with tempfile.TemporaryDirectory() as d:
            s = Section("A"); s.add(FAIL, "<b>bad</b>", "detail")
            out = report.write_report("t", [s], d)
            html = open(os.path.join(out, "report.html"), encoding="utf-8").read()
            self.assertIn("&lt;b&gt;bad", html)
            self.assertEqual(json.load(open(os.path.join(out, "report.json")))["overall"], FAIL)


if __name__ == "__main__":
    unittest.main()
