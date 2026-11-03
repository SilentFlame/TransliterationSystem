"""IndicXlit engine loaded from a versioned local package directory.

The stock ``XlitEngine`` hard-codes its model location under site-packages and
downloads from GitHub on first use. For a managed runtime we need to point it at
``<store>/packages/<lang>/<version>/model`` instead, so we subclass the
library's base engine and supply our own paths. Inference code is untouched.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from xlit.common.logging_setup import get_logger, log_event

log = get_logger("local.engine")

TIER_BASE = "base"
TIER_RESCORE = "rescore"
TIER_RANK = {TIER_BASE: 0, TIER_RESCORE: 1}

_generation = itertools.count(1)


@dataclass(frozen=True)
class LoadRequest:
    lang: str
    version: str
    tier: str
    models_path: Path
    beam_width: int


@dataclass
class LoadedEngine:
    """A warm engine plus the metadata needed to route and report on it."""

    lang: str
    version: str
    tier: str
    beam_width: int
    impl: object
    load_ms: float
    generation: int = field(default_factory=lambda: next(_generation))
    loaded_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.version, self.tier)

    def suggest(self, word: str, topk: int) -> list[str]:
        # fairseq's generator is not documented as thread-safe; one typist means
        # contention is negligible, so serialize per engine.
        with self._lock:
            out = self.impl.translit_word(word, lang_code=self.lang, topk=topk)
        return list(out)[:topk]

    def describe(self) -> dict:
        return {
            "lang": self.lang,
            "version": self.version,
            "tier": self.tier,
            "beam_width": self.beam_width,
            "load_ms": round(self.load_ms, 1),
            "generation": self.generation,
        }


def _build_engine_class():
    from ai4bharat.transliteration.transformer.base_engine import LANG_LIST_FILE, BaseEngineTransformer

    class PackagedXlitEngine(BaseEngineTransformer):
        def __init__(self, models_path: Path, lang: str, beam_width: int, rescore: bool) -> None:
            lang_list = (models_path / LANG_LIST_FILE).resolve()
            supported = set(lang_list.read_text().split()) - {"en"}
            if lang not in supported:
                raise ValueError(f"language {lang!r} not in package ({sorted(supported)})")
            self._all_supported_langs = supported
            self._tgt_langs = {lang}
            super().__init__(str(models_path), beam_width=beam_width, rescore=rescore)

        @property
        def all_supported_langs(self):
            return self._all_supported_langs

        @property
        def tgt_langs(self):
            return self._tgt_langs

        def translit_word(self, word, lang_code, topk=4):
            return self._transliterate_word(word, src_lang="en", tgt_lang=lang_code, topk=topk)

    return PackagedXlitEngine


def load_engine(req: LoadRequest) -> LoadedEngine:
    """Load + warm an engine. Blocking: ~2-3 s for base, ~1 min for rescore."""
    log_event(log, logging.INFO, "engine_load_started", version=req.version, tier=req.tier, beam=req.beam_width)
    t0 = time.perf_counter()
    cls = _build_engine_class()
    impl = cls(req.models_path, req.lang, req.beam_width, rescore=req.tier == TIER_RESCORE)
    impl.translit_word("namaste", lang_code=req.lang, topk=1)
    load_ms = (time.perf_counter() - t0) * 1000
    engine = LoadedEngine(
        lang=req.lang, version=req.version, tier=req.tier, beam_width=req.beam_width, impl=impl, load_ms=load_ms
    )
    log_event(log, logging.INFO, "engine_load_finished", **engine.describe())
    return engine
