"""Local API redacts courtroom text from INFO logs by default."""

import logging
import unittest

from tests.helpers import fake_engine
from xlit.local.config import LocalConfig
from xlit.local.engine_manager import EngineManager
from xlit.local.server import create_app


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class PrivacyLoggingTests(unittest.TestCase):
    def test_suggest_redacts_by_default(self):
        manager = EngineManager()
        manager.offer(fake_engine("hi-1", "base"))
        app = create_app(LocalConfig(redact_content=True), manager, None)
        handler = _Capture()
        log = logging.getLogger("xlit.local.http")
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            r = app.test_client().get("/v1/suggest?q=mera&k=2")
            self.assertEqual(r.status_code, 200)
            suggest_events = [rec for rec in handler.records if rec.getMessage() == "suggest"]
            self.assertTrue(suggest_events)
            fields = suggest_events[-1].fields
            self.assertNotIn("q", fields)
            self.assertNotIn("top1", fields)
            self.assertEqual(fields["q_len"], 4)
        finally:
            log.removeHandler(handler)

    def test_suggest_logs_content_when_disabled(self):
        manager = EngineManager()
        manager.offer(fake_engine("hi-1", "base"))
        app = create_app(LocalConfig(redact_content=False), manager, None)
        handler = _Capture()
        log = logging.getLogger("xlit.local.http")
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            app.test_client().get("/v1/suggest?q=mera&k=2")
            fields = [rec for rec in handler.records if rec.getMessage() == "suggest"][-1].fields
            self.assertEqual(fields["q"], "mera")
            self.assertIn("top1", fields)
        finally:
            log.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
