#!/usr/bin/env python3
"""TCP proxy that puts a courtroom WAN link between local services and central.

Local services point ``--central-url`` at this proxy instead of central, so every
manifest poll, heartbeat and package download crosses a link with:

  * one-way propagation delay + jitter per direction (``--rtt-ms``, ``--jitter-ms``)
  * a bandwidth cap **shared by every connection** (``--down-mbps`` / ``--up-mbps``),
    i.e. one court uplink that all typist PCs in the building compete for
  * optional random connection drops while bytes are flowing (``--drop-per-mb``),
    which exercises Range resume in the client
  * a hard outage switch: ``kill -USR1 <pid>`` toggles "link down" (new and
    existing connections are refused / reset)

Run::

    python -m benchmarks.wan_proxy --listen 127.0.0.1:8701 --upstream 127.0.0.1:8700 \\
        --rtt-ms 60 --jitter-ms 10 --down-mbps 100 --up-mbps 20 --drop-per-mb 0.002

This is user-space shaping. On macOS, kernel-level shaping (``dnctl`` + ``pfctl``)
is the closer-to-real alternative but needs root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import signal
import sys
import time
from pathlib import Path

CHUNK = 64 * 1024


class SharedLink:
    """Token bucket shared by all connections in one direction."""

    def __init__(self, mbps: float) -> None:
        self.rate = mbps * 1_000_000 / 8 if mbps > 0 else 0.0
        self.capacity = max(CHUNK, self.rate * 0.05)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()
        self.bytes = 0

    async def take(self, n: int) -> None:
        self.bytes += n
        if not self.rate:
            return
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                await asyncio.sleep((n - self.tokens) / self.rate)


class Proxy:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.up_host, up_port = args.upstream.rsplit(":", 1)
        self.up_port = int(up_port)
        self.one_way_s = args.rtt_ms / 2000.0
        self.jitter_s = args.jitter_ms / 1000.0
        self.down = SharedLink(args.down_mbps)
        self.up = SharedLink(args.up_mbps)
        self.link_up = True
        self.started = time.time()
        self.stats = {"connections": 0, "active": 0, "drops": 0, "refused_link_down": 0, "outages": 0}
        self._writers: set[asyncio.StreamWriter] = set()
        self.rng = random.Random(args.seed)

    def toggle_link(self) -> None:
        self.link_up = not self.link_up
        if not self.link_up:
            self.stats["outages"] += 1
            for w in list(self._writers):
                w.transport.abort()
        print(f"wan_proxy link {'UP' if self.link_up else 'DOWN'}", flush=True)

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    link: SharedLink, droppable: bool) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        last_due = 0.0

        async def pump_in() -> None:
            nonlocal last_due
            try:
                while True:
                    buf = await reader.read(CHUNK)
                    due = time.monotonic() + self.one_way_s + self.rng.uniform(0, self.jitter_s)
                    last_due = max(last_due, due)
                    await queue.put((last_due, buf))
                    if not buf:
                        return
            except (ConnectionError, asyncio.CancelledError):
                await queue.put((0.0, b""))

        async def pump_out() -> None:
            while True:
                due, buf = await queue.get()
                delay = due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                if not buf:
                    try:
                        writer.write_eof()
                    except (OSError, RuntimeError):
                        pass
                    return
                await link.take(len(buf))
                if droppable and self.args.drop_per_mb and self.rng.random() < self.args.drop_per_mb * len(buf) / 1e6:
                    self.stats["drops"] += 1
                    writer.transport.abort()
                    return
                writer.write(buf)
                await writer.drain()

        t_in = asyncio.create_task(pump_in())
        try:
            await pump_out()
        except (ConnectionError, OSError):
            pass
        finally:
            t_in.cancel()

    async def handle(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        self.stats["connections"] += 1
        if not self.link_up:
            self.stats["refused_link_down"] += 1
            c_writer.transport.abort()
            return
        await asyncio.sleep(self.one_way_s * 2)  # TCP handshake crosses the link once
        try:
            u_reader, u_writer = await asyncio.open_connection(self.up_host, self.up_port)
        except OSError:
            c_writer.transport.abort()
            return
        self.stats["active"] += 1
        self._writers.update({c_writer, u_writer})
        try:
            await asyncio.gather(
                self._pipe(c_reader, u_writer, self.up, droppable=False),
                self._pipe(u_reader, c_writer, self.down, droppable=True),
                return_exceptions=True,
            )
        finally:
            self.stats["active"] -= 1
            for w in (c_writer, u_writer):
                self._writers.discard(w)
                w.transport.abort()

    def snapshot(self) -> dict:
        up_s = max(1e-9, time.time() - self.started)
        return {
            "config": {
                "listen": self.args.listen, "upstream": self.args.upstream, "rtt_ms": self.args.rtt_ms,
                "jitter_ms": self.args.jitter_ms, "down_mbps": self.args.down_mbps,
                "up_mbps": self.args.up_mbps, "drop_per_mb": self.args.drop_per_mb,
            },
            "link_up": self.link_up,
            "uptime_s": round(up_s, 1),
            "down_bytes": self.down.bytes,
            "up_bytes": self.up.bytes,
            "avg_down_mbps": round(self.down.bytes * 8 / up_s / 1e6, 2),
            **self.stats,
        }

    async def write_stats_forever(self, path: Path) -> None:
        while True:
            path.write_text(json.dumps(self.snapshot(), indent=2) + "\n")
            await asyncio.sleep(2.0)


async def amain(args: argparse.Namespace) -> None:
    proxy = Proxy(args)
    host, port = args.listen.rsplit(":", 1)
    server = await asyncio.start_server(proxy.handle, host, int(port))
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGUSR1, proxy.toggle_link)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stats_task = asyncio.create_task(proxy.write_stats_forever(args.stats_file)) if args.stats_file else None
    print(f"wan_proxy {args.listen} -> {args.upstream} rtt={args.rtt_ms}ms jitter={args.jitter_ms}ms "
          f"down={args.down_mbps}Mbps up={args.up_mbps}Mbps drop/MB={args.drop_per_mb}", flush=True)
    async with server:
        await stop.wait()
    if stats_task:
        stats_task.cancel()
        args.stats_file.write_text(json.dumps(proxy.snapshot(), indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Courtroom WAN emulator in front of central")
    p.add_argument("--listen", default="127.0.0.1:8701")
    p.add_argument("--upstream", default="127.0.0.1:8700")
    p.add_argument("--rtt-ms", type=float, default=60.0)
    p.add_argument("--jitter-ms", type=float, default=10.0)
    p.add_argument("--down-mbps", type=float, default=100.0, help="central→court, shared by all connections")
    p.add_argument("--up-mbps", type=float, default=20.0, help="court→central, shared")
    p.add_argument("--drop-per-mb", type=float, default=0.0, help="probability of a reset per MB downloaded")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--stats-file", type=Path, default=None)
    args = p.parse_args(argv)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
