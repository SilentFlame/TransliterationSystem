import time
import unittest

from tests.helpers import fake_engine
from xlit.local.engine_manager import EngineManager, EngineNotReady, plan_next_load


class EngineManagerTests(unittest.TestCase):
    def test_not_ready_without_engine(self):
        with self.assertRaises(EngineNotReady):
            EngineManager().suggest("mera", 4)

    def test_first_engine_activates_immediately(self):
        m = EngineManager()
        m.offer(fake_engine("hi-1", "base"))
        self.assertEqual(m.active.key, ("hi-1", "base"))
        self.assertIsNone(m.staged)
        self.assertEqual(m.suggest("mera", 2).candidates, ["hi-1/base:mera:0", "hi-1/base:mera:1"])

    def test_upgrade_waits_for_word_boundary(self):
        m = EngineManager(idle_promote_s=60)
        m.offer(fake_engine("hi-1", "base"))
        m.suggest("m", 4, word_start=True)
        m.offer(fake_engine("hi-1", "rescore"))

        # Mid-word keystrokes keep the old engine so the list does not reshuffle.
        self.assertEqual(m.suggest("me", 4).tier, "base")
        self.assertEqual(m.suggest("mer", 4).tier, "base")
        # First keystroke of the next word promotes.
        self.assertEqual(m.suggest("k", 4, word_start=True).tier, "rescore")
        self.assertIsNone(m.staged)

    def test_commit_promotes(self):
        m = EngineManager(idle_promote_s=60)
        m.offer(fake_engine("hi-1", "base"))
        m.offer(fake_engine("hi-1", "rescore"))
        self.assertTrue(m.word_boundary())
        self.assertEqual(m.active.tier, "rescore")
        self.assertFalse(m.word_boundary())

    def test_idle_promotes(self):
        m = EngineManager(idle_promote_s=0.05)
        m.offer(fake_engine("hi-1", "base"))
        m.suggest("me", 4)
        m.offer(fake_engine("hi-1", "rescore"))
        time.sleep(0.08)
        self.assertTrue(m.maybe_promote_idle())
        self.assertEqual(m.active.tier, "rescore")

    def test_cache_hits_and_is_invalidated_on_promotion(self):
        m = EngineManager(idle_promote_s=60)
        m.offer(fake_engine("hi-1", "base"))
        self.assertFalse(m.suggest("mera", 4).cached)
        self.assertTrue(m.suggest("mera", 4).cached)
        m.offer(fake_engine("hi-1", "rescore"))
        m.word_boundary()
        s = m.suggest("mera", 4)
        self.assertFalse(s.cached)
        self.assertEqual(s.tier, "rescore")

    def test_wants(self):
        m = EngineManager()
        m.offer(fake_engine("hi-1", "base"))
        self.assertFalse(m.wants("hi-1", "base"))
        self.assertTrue(m.wants("hi-1", "rescore"))


class PlanNextLoadTests(unittest.TestCase):
    def plan(self, active=None, staged=None, target="hi-2", base=True, rescore=False, allowed=True, has=True):
        return plan_next_load(active, staged, target, base, rescore, allowed, has)

    def test_boot_always_starts_with_base(self):
        self.assertEqual(self.plan(rescore=True), ("hi-2", "base"))
        self.assertIsNone(self.plan(base=False))

    def test_same_version_upgrades_to_rescore_when_ready(self):
        self.assertIsNone(self.plan(active=("hi-2", "base")))
        self.assertEqual(self.plan(active=("hi-2", "base"), rescore=True), ("hi-2", "rescore"))
        self.assertIsNone(self.plan(active=("hi-2", "base"), rescore=True, allowed=False))
        self.assertIsNone(self.plan(active=("hi-2", "rescore"), rescore=True))

    def test_new_version_from_base(self):
        self.assertEqual(self.plan(active=("hi-1", "base")), ("hi-2", "base"))
        self.assertEqual(self.plan(active=("hi-1", "base"), rescore=True), ("hi-2", "rescore"))

    def test_new_version_never_downgrades_from_rescore(self):
        self.assertIsNone(self.plan(active=("hi-1", "rescore")))
        self.assertEqual(self.plan(active=("hi-1", "rescore"), rescore=True), ("hi-2", "rescore"))
        # ...unless the new version ships no rescore package, or rescore got disabled.
        self.assertEqual(self.plan(active=("hi-1", "rescore"), has=False), ("hi-2", "base"))
        self.assertEqual(self.plan(active=("hi-1", "rescore"), allowed=False), ("hi-2", "base"))

    def test_staged_counts_as_current(self):
        self.assertIsNone(self.plan(active=("hi-1", "base"), staged=("hi-2", "base")))


if __name__ == "__main__":
    unittest.main()
