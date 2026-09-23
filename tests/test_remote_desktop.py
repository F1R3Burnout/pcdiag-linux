import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pcdiag import remote_desktop as rd  # noqa: E402
from pcdiag.common import ATTENTION, PASS, UNSUPPORTED  # noqa: E402


class RuleEvaluation(unittest.TestCase):
    def test_generated_rule_is_recognised_as_covering_its_own_user(self):
        text = rd.polkit_rule_text("oliverk")
        self.assertTrue(rd.rule_grants_networkmanager(text, "oliverk"))
        self.assertIn("org.freedesktop.NetworkManager.", text)

    def test_generated_rule_does_not_cover_a_different_user(self):
        text = rd.polkit_rule_text("oliverk")
        self.assertFalse(rd.rule_grants_networkmanager(text, "someoneelse"))

    def test_unrelated_rule_file_does_not_match(self):
        self.assertFalse(rd.rule_grants_networkmanager("polkit.addRule(function(){ return null; });", "oliverk"))
        self.assertFalse(rd.rule_grants_networkmanager("", "oliverk"))

    def test_wheel_group_rule_counts_as_covering_any_user(self):
        text = ('if (subject.isInGroup("wheel") && action.id.indexOf("org.freedesktop.NetworkManager.") == 0) '
               'return polkit.Result.YES;')
        self.assertTrue(rd.rule_grants_networkmanager(text, "anyone"))

    def test_pkla_style_rule_is_recognised(self):
        text = "[nm]\nIdentity=unix-user:oliverk\nAction=org.freedesktop.NetworkManager.*\nResultActive=yes\n"
        self.assertTrue(rd.rule_grants_networkmanager(text, "oliverk"))

    def test_has_rule_scans_given_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "49-x.rules")
            with open(f, "w") as fh:
                fh.write(rd.polkit_rule_text("oliverk"))
            self.assertTrue(rd.has_nopasswd_networkmanager_rule("oliverk", rule_files=[f]))
            self.assertFalse(rd.has_nopasswd_networkmanager_rule("someoneelse", rule_files=[f]))
            self.assertFalse(rd.has_nopasswd_networkmanager_rule("oliverk", rule_files=[]))


class TargetUser(unittest.TestCase):
    def test_prefers_sudo_user_over_effective_user(self):
        old = os.environ.get("SUDO_USER")
        os.environ["SUDO_USER"] = "oliverk"
        try:
            self.assertEqual(rd.target_username(), "oliverk")
        finally:
            if old is None:
                del os.environ["SUDO_USER"]
            else:
                os.environ["SUDO_USER"] = old


class CheckSection(unittest.TestCase):
    def test_skips_when_xrdp_not_installed(self):
        # xrdp_installed uses the real `have` by default; on this (non-xrdp) test host it's absent,
        # so check() should report UNSUPPORTED rather than crash.
        result = rd.check(username="oliverk")
        self.assertIn(result.status, (UNSUPPORTED, ATTENTION, PASS))

    def test_attention_when_xrdp_present_but_no_rule(self):
        orig = rd.xrdp_installed
        orig_has = rd.has_nopasswd_networkmanager_rule
        rd.xrdp_installed = lambda present=None: True
        rd.has_nopasswd_networkmanager_rule = lambda user, rule_files=None: False
        try:
            result = rd.check(username="oliverk")
            self.assertEqual(result.status, ATTENTION)
            self.assertIn("oliverk", result.findings[0].detail)
        finally:
            rd.xrdp_installed = orig
            rd.has_nopasswd_networkmanager_rule = orig_has

    def test_pass_when_xrdp_present_and_rule_exists(self):
        orig = rd.xrdp_installed
        orig_has = rd.has_nopasswd_networkmanager_rule
        rd.xrdp_installed = lambda present=None: True
        rd.has_nopasswd_networkmanager_rule = lambda user, rule_files=None: True
        try:
            result = rd.check(username="oliverk")
            self.assertEqual(result.status, PASS)
        finally:
            rd.xrdp_installed = orig
            rd.has_nopasswd_networkmanager_rule = orig_has


class ApplyFix(unittest.TestCase):
    def test_declines_without_confirmation(self):
        orig_has = rd.has_nopasswd_networkmanager_rule
        rd.has_nopasswd_networkmanager_rule = lambda user, rule_files=None: False
        try:
            ok = rd.apply_fix(username="oliverk", assume_yes=False, ask=lambda _: "n", log=lambda *_: None)
            self.assertFalse(ok)
        finally:
            rd.has_nopasswd_networkmanager_rule = orig_has

    def test_noop_when_already_present(self):
        orig_has = rd.has_nopasswd_networkmanager_rule
        rd.has_nopasswd_networkmanager_rule = lambda user, rule_files=None: True
        try:
            ok = rd.apply_fix(username="oliverk", assume_yes=True, log=lambda *_: None)
            self.assertTrue(ok)
        finally:
            rd.has_nopasswd_networkmanager_rule = orig_has


if __name__ == "__main__":
    unittest.main()
