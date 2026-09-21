import io
import os
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pcdiag import distro, gpu  # noqa: E402
from pcdiag.common import FAIL, PASS, UNSUPPORTED  # noqa: E402


class Distros(unittest.TestCase):
    def fam(self, text):
        return distro.classify(distro.parse_os_release(text)).family

    def test_families(self):
        self.assertEqual(self.fam('ID=cachyos\nID_LIKE=arch\nPRETTY_NAME="CachyOS Linux"'), "arch")
        self.assertEqual(self.fam('ID=linuxmint\nID_LIKE="ubuntu debian"'), "debian")
        self.assertEqual(self.fam('ID=debian\nVERSION_ID="12"'), "debian")
        self.assertEqual(self.fam("ID=fedora"), "rhel")
        self.assertEqual(self.fam('ID=opensuse-tumbleweed\nID_LIKE="opensuse suse"'), "suse")
        self.assertEqual(self.fam('ID=nobara\nID_LIKE="rhel centos fedora"'), "rhel")

    def test_install_commands(self):
        self.assertEqual(distro.install_commands(distro.Distro(family="arch"), ["smartmontools"]),
                         [["pacman", "-S", "--needed", "--noconfirm", "smartmontools"]])
        cmds = distro.install_commands(distro.Distro(family="debian"), ["memtester"])
        self.assertEqual(cmds[0], ["apt-get", "update"])
        self.assertEqual(cmds[1], ["apt-get", "install", "-y", "memtester"])
        self.assertEqual(distro.install_commands(distro.Distro(), ["x"]), [])

    def test_missing_packages(self):
        d = distro.Distro(family="arch")
        pk = distro.missing_packages(d, "stability", present=lambda c: c == "lspci")
        self.assertEqual(pk, ["smartmontools", "memtester", "vulkan-tools"])
        self.assertEqual(distro.missing_packages(d, "netdiag", present=lambda c: True), [])

    def test_no_install_returns_missing_without_running_anything(self):
        msgs = []
        orig = distro.detect
        distro.detect = lambda: distro.Distro(family="arch")
        try:
            distro.ensure_dependencies("stability", allow_install=False, log=msgs.append)
        finally:
            distro.detect = orig
        # either everything happens to be installed (no messages) or install was skipped
        self.assertTrue(not msgs or "übersprungen" in msgs[0])


class Gpu(unittest.TestCase):
    def test_evaluate(self):
        self.assertEqual(gpu.evaluate_output("1 iteration. Passed  0.0031 seconds\n", True).status, PASS)
        self.assertEqual(gpu.evaluate_output("Passed\nError found. 3 errors\n", True).status, FAIL)
        self.assertEqual(gpu.evaluate_output("ERROR_DEVICE_LOST", False).status, FAIL)
        self.assertEqual(gpu.evaluate_output("Failed to create instance", True).status, UNSUPPORTED)
        self.assertEqual(gpu.evaluate_output("", True).status, UNSUPPORTED)

    def test_safe_extract_rejects_traversal_and_links(self):
        with tempfile.TemporaryDirectory() as d:
            arc = os.path.join(d, "a.tar.xz")
            with tarfile.open(arc, "w:xz") as tf:
                data = b"#!/bin/sh\n"
                ti = tarfile.TarInfo("memtest_vulkan")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
                ln = tarfile.TarInfo("evil")
                ln.type, ln.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tf.addfile(ln)
            out = gpu.safe_extract_member(arc, "memtest_vulkan", d)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), data)
            for bad in ("evil", "../x"):
                with self.assertRaises(ValueError):
                    gpu.safe_extract_member(arc, bad, d)

    @unittest.skipIf(sys.platform == "win32", "process groups are POSIX-only")
    def test_stop_group_kills_orphan_children(self):
        import subprocess
        import time
        p = subprocess.Popen(["sh", "-c", "sleep 300 & echo $!; wait"], stdout=subprocess.PIPE,
                             text=True, start_new_session=True)
        child = int(p.stdout.readline())
        gpu._stop_group(p)
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_pinned_hash_shape(self):
        self.assertRegex(gpu.TOOL["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(gpu.TOOL["url"].startswith("https://github.com/GpuZelenograd/"))


if __name__ == "__main__":
    unittest.main()
