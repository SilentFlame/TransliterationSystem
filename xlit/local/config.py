"""Local service configuration (CLI flags map 1:1 onto these fields)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

RESCORE_MODES = ("auto", "on", "off")


@dataclass(frozen=True)
class LocalConfig:
    central_url: str = "http://127.0.0.1:8700"
    host: str = "127.0.0.1"
    port: int = 8765
    lang: str = "hi"
    store_dir: Path = Path("var/local_store")
    log_dir: Path = Path("logs")
    beam_width: int = 4
    default_topk: int = 4
    rescore_mode: str = "auto"
    rescore_min_ram_gb: float = 4.0
    poll_interval_s: float = 60.0
    max_backoff_s: float = 300.0
    bootstrap_retry_s: float = 5.0
    idle_promote_s: float = 2.0
    cache_size: int = 4096
    max_word_len: int = 48
    # Courtroom text is sensitive. Default redact roman/chosen from INFO logs;
    # pass --log-content to keep them for local debugging.
    redact_content: bool = True

    def rescore_allowed(self) -> bool:
        if self.rescore_mode == "on":
            return True
        if self.rescore_mode == "off":
            return False
        return total_ram_gb() >= self.rescore_min_ram_gb


def total_ram_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024**3)
    except (ValueError, OSError, AttributeError):
        return 0.0
