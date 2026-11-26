import json
import os
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from pcdiag import displaydiag, events, netdiag, report, stability, sysdiag
from pcdiag.common import ATTENTION, FAIL, PASS, SKIPPED, UNSUPPORTED, Section


def entry(message, **fields):
    return {"MESSAGE": message, "_BOOT_ID": "old-boot", "__CURSOR": "record-cursor",
            "__REALTIME_TIMESTAMP": "1700000000000000", **fields}


class JournalDiagnostics(unittest.TestCase):
    def test_journal_records_are_retained_across_warnings_and_invalid_lines(self):
        row = entry("Hello")
        self.assertEqual(events.json_rows("warning\n" + json.dumps(row) + "\nnull\n"), [row])

    def test_restart_does_not_infer_a_reset_from_missing_shutdown_messages(self):
        data = [entry("Application is not rebooting", SYSLOG_IDENTIFIER="application")]
        with patch.object(events, "query", return_value=(data, "")):
            s = events.restart_context()
        self.assertEqual(s.status, ATTENTION)
        self.assertIn("kein Nachweis", s.findings[0].detail)
        self.assertIn("Cursor record-cursor", s.findings[0].source)

    def test_shutdown_and_panic_are_different(self):
        rows = [entry("Reached target System Reboot.", _PID="1")]
        with patch.object(events, "query", return_value=(rows, "")):
            self.assertEqual(events.restart_context().status, PASS)
            rows.insert(0, entry("Kernel panic - not syncing", _TRANSPORT="kernel"))
            self.assertEqual(events.restart_context().status, FAIL)

    def test_cpu_error_is_classified_with_evidence_and_action(self):
        rows = [entry("mce: [Hardware Error]: CPU 2: Machine Check: Bank 5", _TRANSPORT="kernel")]
        with patch.object(events, "query", side_effect=[(rows, ""), ([], "unavailable")]):
            s = events.hardware_context()
        self.assertEqual(s.status, ATTENTION)
        self.assertEqual(len([f for f in s.findings if f.status == ATTENTION]), 1)
        self.assertIn("CPU / Machine Check", s.findings[0].title)
        self.assertIn("CPU 2", s.findings[0].detail)
        self.assertIn("Cursor record-cursor", s.findings[0].source)

    def test_crashes_group_on_exe_and_signal_without_copying_dump(self):
        rows = [entry("crashed", COREDUMP_EXE="/usr/bin/app", COREDUMP_SIGNAL_NAME="SIGSEGV",
                      COREDUMP_FILENAME="/some/missing/core", COREDUMP_PID=p) for p in ("9", "10")]
        with patch.object(events, "query", return_value=(rows, "")):
            s = events.application_crashes()
        self.assertEqual(s.status, ATTENTION)
        self.assertEqual(len(s.findings), 1)
        self.assertIn("2×", s.findings[0].title)
        self.assertIn("Letzte PID: 10", s.findings[0].detail)


class NetworkDiagnostics(unittest.TestCase):
    def test_networkmanager_expected_activation_is_not_a_warning(self):
        good = ("dhcp4 (eth0): activation: beginning transaction (timeout in 45 seconds)",
                "device (eth0): state change: unavailable -> disconnected (reason 'carrier-changed')",
                "device (eth0): state change: disconnected -> prepare")
        bad = ("dhcp4 (eth0): timed out waiting for a lease",
               "device (eth0): state change: activated -> disconnected",
               "device (eth0): carrier lost")
        self.assertFalse(any(netdiag.LINK_PROBLEM.search(x) for x in good))
        self.assertTrue(all(netdiag.LINK_PROBLEM.search(x) for x in bad))

    def test_vpn_detected_by_type_or_known_adapter_name(self):
        self.assertTrue(netdiag.is_vpn({"ifname": "office", "linkinfo": {"info_kind": "wireguard"}}))
        self.assertTrue(netdiag.is_vpn({"ifname": "tailscale0"}))
        self.assertFalse(netdiag.is_vpn({"ifname": "enp1s0"}))
        self.assertFalse(netdiag.is_vpn({"ifname": "bond0", "linkinfo": {"info_kind": "bond"}}))

    def test_network_lan_only_never_resolves_or_connects_outside(self):
        links = [{"ifname": "testnic0", "operstate": "UP"}, {"ifname": "tailscale0", "operstate": "UP"}]
        routes = [{"dst": "default", "gateway": "192.0.2.1", "dev": "testnic0"},
                  {"dst": "100.64.0.0/10", "dev": "tailscale0"}]
        with patch.object(netdiag, "_ip", side_effect=[(links, ""), (routes, ""), ([], "")]), \
                patch.object(netdiag, "_ping") as ping, patch.object(netdiag, "_dns") as dns, \
                patch.object(netdiag, "_speedtest") as speed, \
                patch.object(netdiag, "run", return_value=(0, "")), \
                patch.object(events, "query", return_value=([], "")):
            sections = netdiag.collect(lan_only=True, speedtest=True)
        self.assertEqual(ping.call_count, 1)
        self.assertEqual(ping.call_args.args[1], "192.0.2.1")
        dns.assert_not_called()
        speed.assert_not_called()
        self.assertNotIn("tailscale0", repr(sections[0]))
        self.assertNotIn("tailscale0", repr(next(s for s in sections if s.name.startswith("Routing"))))
        self.assertIn("100.64.0.0/10", repr(next(s for s in sections if s.name.startswith("VPN"))))
        self.assertEqual(sections[-1].status, SKIPPED)

    def test_speedtest_units_and_server_for_both_clients(self):
        common = {"server": {"name": "Example", "location": "Berlin", "id": 1}}
        cli = {"download": 300_000_000, "upload": 50_000_000, "ping": 8.5, **common}
        ookla = {"download": {"bandwidth": 37_500_000}, "upload": {"bandwidth": 6_250_000},
                 "ping": {"latency": 8.5}, **common}
        self.assertEqual(netdiag.parse_speedtest(cli), netdiag.parse_speedtest(ookla))
        self.assertEqual(netdiag.parse_speedtest(cli), {"download_mbps": 300, "upload_mbps": 50,
                                                       "ping_ms": 8.5, "server": "Example · Berlin · 1"})


class DisplayDiagnostics(unittest.TestCase):
    def test_edid_hdr_is_a_capability_not_an_active_setting(self):
        base = bytearray(128)
        base[:8] = bytes.fromhex("00FFFFFFFFFFFF00")
        base[126] = 1
        base[127] = -sum(base) % 256
        ext = bytearray(128)
        ext[:8] = bytes([2, 3, 8, 0, 0xE3, 6, 12, 1])
        ext[127] = -sum(ext) % 256
        e = displaydiag.parse_edid(bytes(base + ext))
        self.assertEqual(e["hdr_formats"], ["HDR10/PQ", "HLG"])
        self.assertTrue(e["extensions_valid"])
        self.assertNotIn("hdr_active", e)
        self.assertFalse(displaydiag.parse_edid(bytes(base))["extensions_valid"])

    def test_current_mode_is_read_from_session_and_can_be_compared(self):
        r = displaydiag.parse_kscreen({"outputs": [{"name": "HDMI-A-1", "connected": True, "enabled": True,
                                                   "currentModeId": "2", "modes": [
                                                       {"id": "2", "refreshRate": 59.94,
                                                        "size": {"width": 1920, "height": 1080}}]}]})
        self.assertEqual(r["HDMI-A-1"]["active_mode"], "1920x1080@59.94")
        x = displaydiag.parse_xrandr("HDMI-A-1 connected\n  1920x1080  60.00 + 59.94*\n")
        self.assertEqual(x, r)
        y = displaydiag.parse_xrandr("HDMI-A-1 connected\n  1920x1080  60.00*+\n")
        self.assertEqual(displaydiag.compare_state(x, y).status, ATTENTION)
        self.assertEqual(displaydiag.compare_state(x, r).status, PASS)


class SafetyAndReporting(unittest.TestCase):
    def test_smart_uses_auto_detection_before_scanner_type(self):
        scan = {"devices": [{"name": "/dev/sda", "type": "scsi"}]}
        healthy = {"device": {"name": "/dev/sda"}, "smart_status": {"passed": True}}
        with patch.object(stability, "have", return_value=True), patch.object(stability, "is_root", return_value=True), \
                patch.object(stability, "run", side_effect=[(0, json.dumps(scan)), (0, json.dumps(healthy))]) as fake:
            sections = stability.smart_test()
        self.assertNotIn("-d", fake.call_args.args[0])
        self.assertEqual(sections[0].status, PASS)
        self.assertIn("smartctl -j -a /dev/sda", sections[0].findings[0].source)

    def test_memtester_shortcut_does_not_claim_full_test(self):
        with patch.object(stability, "read", return_value="MemAvailable: 4000000 kB"), \
                patch.object(stability, "have", return_value=True), patch.object(stability, "is_root", return_value=True), \
                patch.object(stability, "_memtester", return_value=(-15, "", True)) as tool:
            s = stability.memory_test(64, threading.Event(), load_seconds=1)
        self.assertEqual(tool.call_args.args[2], 1)
        self.assertTrue(any(f.status == SKIPPED for f in s.findings))

    def test_dry_run_does_not_consume_previous_run_marker(self):
        with patch.object(stability, "check_previous_run") as marker:
            self.assertEqual(stability.run_stability(dry_run=True)[0].status, PASS)
            marker.assert_not_called()

    def test_thermal_guard_detects_hot_idle_sensor_before_starting_load(self):
        stop = threading.Event()
        with patch.object(stability, "read_temps", return_value={"/path": 99.0}):
            guard = stability.ThermalGuard(stop)
        self.assertTrue(guard.aborted and stop.is_set())
        self.assertEqual(guard.peaks, {"/path": 99.0})

    def test_missing_logs_do_not_produce_a_pass(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"XDG_STATE_HOME": d}), \
                patch.object(stability, "kernel_log_lines", return_value=None):
            sections = stability.run_stability(components=["kernel"])
        self.assertEqual(sections[-1].status, UNSUPPORTED)

    def test_report_is_compact_and_escapes_source_and_raw_data(self):
        ok = Section("healthy")
        ok.add(PASS, "working", source="test <file>")
        warn = Section("warnings", raw="<raw>")
        warn.add(PASS, "info")
        warn.add(ATTENTION, "investigate")
        html = report.render_html("example", [ok, warn])
        self.assertIn('<details class="section"><summary>healthy', html)
        self.assertIn('<details class="section" open><summary>warnings', html)
        self.assertIn("test &lt;file&gt;", html)
        self.assertIn("&lt;raw&gt;", html)
        self.assertLess(html.index("investigate"), html.index("Weitere Ergebnisse"))
        with tempfile.TemporaryDirectory() as d:
            report.write_report("test", [ok], d, archive=True)
            with zipfile.ZipFile(os.path.join(d, "report.zip")) as z:
                self.assertEqual(set(z.namelist()), {"report.html", "report.json"})
                self.assertEqual(json.loads(z.read("report.json"))["sections"][0]["findings"][0]["source"], "test <file>")
            with self.assertRaises(FileExistsError):
                report.write_report("test", [ok], d)


if __name__ == "__main__":
    unittest.main()
