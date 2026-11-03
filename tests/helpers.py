"""Test fixtures: a fake library model dir and a fake engine (no torch needed)."""

from __future__ import annotations

import threading
from pathlib import Path

from xlit.common.http import make_server
from xlit.local.engine import LoadedEngine, LoadRequest


def make_fake_source(root: Path, weights: bytes = b"W" * 4096, rescore: bytes = b"{}" * 2048) -> Path:
    """Mimic ``.../models/en2indic/v1.0`` as shipped by ai4bharat-transliteration."""
    src = root / "en2indic" / "v1.0"
    (src / "transformer").mkdir(parents=True)
    (src / "corpus-bin").mkdir()
    (src / "word_prob_dicts").mkdir()
    (root / "en2indic" / "lang_list.txt").write_text("en\nhi\nmr\n")
    (src / "transformer" / "indicxlit.pt").write_bytes(weights)
    (src / "corpus-bin" / "dict.en.txt").write_text("a 1\n")
    (src / "corpus-bin" / "dict.hi.txt").write_text("क 1\n")
    (src / "word_prob_dicts" / "hi_word_prob_dict.json").write_bytes(rescore)
    return src


class FakeImpl:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls = 0

    def translit_word(self, word, lang_code, topk=4):
        self.calls += 1
        return [f"{self.tag}:{word}:{i}" for i in range(topk)]


def fake_engine(version: str = "hi-1", tier: str = "base") -> LoadedEngine:
    return LoadedEngine(lang="hi", version=version, tier=tier, beam_width=4, impl=FakeImpl(f"{version}/{tier}"), load_ms=1.0)


def fake_loader(req: LoadRequest) -> LoadedEngine:
    return fake_engine(req.version, req.tier)


class ServerThread:
    """Run a WSGI app on an ephemeral port for real-HTTP tests."""

    def __init__(self, app) -> None:
        self.server = make_server("127.0.0.1", 0, app)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "ServerThread":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
