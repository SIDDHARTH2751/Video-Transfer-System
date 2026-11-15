"""Cross-platform TCP/HTTP fault-injecting proxy.

Sits between client and server.  Configurable via CLI flags:

    python tools/fault_proxy.py --listen-port 9000 --target-port 8000 \
        --bandwidth 50000 --latency 100 --jitter 50 \
        --reset-prob 0.05 --hang-prob 0.02 --hang-duration 10 \
        --outage-after 5 --outage-duration 10 \
        --corrupt-prob 0.0 \
        --schedule "0-10:100000,10-20:20000,20-30:100000"

Bandwidth schedule format: "start-end:bps,start-end:bps,..."
  e.g. "0-10:100000,10-20:20000,20-30:80000" means:
    - 0-10s: 100 KB/s, 10-20s: 20 KB/s, 20-30s: 80 KB/s, then repeats.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [proxy] %(levelname)s  %(message)s",
)
logger = logging.getLogger("fault_proxy")


class FaultConfig:
    """Runtime-mutable fault configuration."""

    def __init__(
        self,
        bandwidth: float = 0,           # bytes/sec, 0 = unlimited
        latency: float = 0,             # ms
        jitter: float = 0,              # ms
        reset_prob: float = 0.0,        # probability of connection reset per chunk
        hang_prob: float = 0.0,         # probability of hanging a connection
        hang_duration: float = 10.0,    # seconds to hang
        outage_after: float = 0,        # seconds after start to begin outage (0=none)
        outage_duration: float = 0,     # seconds of outage
        corrupt_prob: float = 0.0,      # probability of byte corruption
        max_corrupt: int = 1,           # maximum number of packets to corrupt
        schedule: str = "",             # bandwidth schedule
    ):
        self.bandwidth = bandwidth
        self.latency = latency
        self.jitter = jitter
        self.reset_prob = reset_prob
        self.hang_prob = hang_prob
        self.hang_duration = hang_duration
        self.outage_after = outage_after
        self.outage_duration = outage_duration
        self.corrupt_prob = corrupt_prob
        self.max_corrupt = max_corrupt
        self.corrupted_so_far = 0
        self.schedule = schedule
        self._schedule_entries: list[tuple[float, float, float]] = []
        self._start_time = time.time()
        self._parse_schedule()

    def _parse_schedule(self) -> None:
        """Parse schedule string into (start, end, bps) tuples."""
        self._schedule_entries = []
        if not self.schedule:
            return
        for part in self.schedule.split(","):
            part = part.strip()
            if not part:
                continue
            time_range, bps = part.split(":")
            start_s, end_s = time_range.split("-")
            self._schedule_entries.append((float(start_s), float(end_s), float(bps)))

    def get_current_bandwidth(self) -> float:
        """Return current bandwidth limit based on schedule or fixed value."""
        if not self._schedule_entries:
            return self.bandwidth

        elapsed = time.time() - self._start_time
        total_cycle = max(e[1] for e in self._schedule_entries) if self._schedule_entries else 1
        if total_cycle > 0:
            t = elapsed % total_cycle  # cyclic
        else:
            t = elapsed

        for start, end, bps in self._schedule_entries:
            if start <= t < end:
                return bps
        return self.bandwidth

    def is_in_outage(self) -> bool:
        if self.outage_after <= 0 or self.outage_duration <= 0:
            return False
        elapsed = time.time() - self._start_time
        return self.outage_after <= elapsed < (self.outage_after + self.outage_duration)

    def reset_start(self) -> None:
        self._start_time = time.time()


async def _throttled_pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    config: FaultConfig,
    label: str,
) -> None:
    """Copy data from reader to writer with throttling and fault injection."""
    try:
        while True:
            data = await reader.read(8192)
            if not data:
                break

            # Outage check
            if config.is_in_outage():
                logger.info("[%s] Outage active — dropping connection", label)
                writer.close()
                return

            # Byte corruption on payload (C->S only, up to max_corrupt)
            if config.corrupt_prob > 0 and label == "C→S" and config.corrupted_so_far < config.max_corrupt:
                if random.random() < config.corrupt_prob:
                    if b"\r\n\r\n" in data:
                        hdr_end = data.find(b"\r\n\r\n") + 4
                        if len(data) > hdr_end:
                            pos = random.randint(hdr_end, len(data) - 1)
                            corrupted = bytearray(data)
                            corrupted[pos] ^= 0xFF
                            data = bytes(corrupted)
                            config.corrupted_so_far += 1
                            logger.info("[%s] Corrupted byte in payload at %d", label, pos)
                    elif not (data.startswith(b"GET ") or data.startswith(b"POST ") or data.startswith(b"PUT ")):
                        pos = random.randint(0, len(data) - 1)
                        corrupted = bytearray(data)
                        corrupted[pos] ^= 0xFF
                        data = bytes(corrupted)
                        config.corrupted_so_far += 1
                        logger.info("[%s] Corrupted byte in continuation chunk at %d", label, pos)

            # Latency + jitter
            if config.latency > 0 or config.jitter > 0:
                delay_ms = config.latency + random.uniform(-config.jitter, config.jitter)
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000.0)

            # Bandwidth throttle
            bw = config.get_current_bandwidth()
            if bw > 0:
                expected_time = len(data) / bw
                await asyncio.sleep(expected_time)

            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    except Exception as exc:
        logger.debug("[%s] Pipe error: %s", label, exc)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    config: FaultConfig,
) -> None:
    """Handle a single proxied connection."""
    peer = client_writer.get_extra_info("peername")
    logger.debug("New connection from %s", peer)

    # Outage check at connection time
    if config.is_in_outage():
        logger.info("Outage active — refusing connection from %s", peer)
        client_writer.close()
        return

    # Connection reset fault
    if config.reset_prob > 0 and random.random() < config.reset_prob:
        logger.info("Injecting connection reset on new connection from %s", peer)
        client_writer.close()
        return

    # Hang fault
    if config.hang_prob > 0 and random.random() < config.hang_prob:
        logger.info("Injecting connection hang for %.1fs on connection from %s", config.hang_duration, peer)
        await asyncio.sleep(config.hang_duration)
        client_writer.close()
        return

    try:
        server_reader, server_writer = await asyncio.open_connection(target_host, target_port)
    except Exception as exc:
        logger.error("Cannot connect to target %s:%d: %s", target_host, target_port, exc)
        client_writer.close()
        return

    # Bi-directional pipe with faults
    up = asyncio.create_task(_throttled_pipe(client_reader, server_writer, config, "C→S"))
    down = asyncio.create_task(_throttled_pipe(server_reader, client_writer, config, "S→C"))

    await asyncio.gather(up, down, return_exceptions=True)


async def run_proxy(
    listen_host: str,
    listen_port: int,
    target_host: str,
    target_port: int,
    config: FaultConfig,
) -> None:
    server = await asyncio.start_server(
        lambda r, w: handle_connection(r, w, target_host, target_port, config),
        listen_host,
        listen_port,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logger.info("Fault proxy listening on %s → %s:%d", addrs, target_host, target_port)
    logger.info(
        "Config: bw=%s lat=%sms jitter=%sms reset=%.2f hang=%.2f outage=%s/%s corrupt=%.3f schedule=%s",
        config.bandwidth or "unlimited",
        config.latency, config.jitter,
        config.reset_prob, config.hang_prob,
        config.outage_after, config.outage_duration,
        config.corrupt_prob, config.schedule or "none",
    )

    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Fault-injecting TCP proxy")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=9000)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=8000)
    parser.add_argument("--bandwidth", type=float, default=0, help="Bytes/sec limit (0=unlimited)")
    parser.add_argument("--latency", type=float, default=0, help="Added latency in ms")
    parser.add_argument("--jitter", type=float, default=0, help="Latency jitter in ms")
    parser.add_argument("--reset-prob", type=float, default=0, help="Connection reset probability (0-1)")
    parser.add_argument("--hang-prob", type=float, default=0, help="Hang probability (0-1)")
    parser.add_argument("--hang-duration", type=float, default=10, help="Hang duration in seconds")
    parser.add_argument("--outage-after", type=float, default=0, help="Seconds to outage start (0=none)")
    parser.add_argument("--outage-duration", type=float, default=0, help="Outage duration in seconds")
    parser.add_argument("--corrupt-prob", type=float, default=0, help="Byte corruption probability (0-1)")
    parser.add_argument("--max-corrupt", type=int, default=1, help="Maximum number of corrupted packets")
    parser.add_argument("--schedule", default="", help="Bandwidth schedule: 'start-end:bps,...'")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    config = FaultConfig(
        bandwidth=args.bandwidth,
        latency=args.latency,
        jitter=args.jitter,
        reset_prob=args.reset_prob,
        hang_prob=args.hang_prob,
        hang_duration=args.hang_duration,
        outage_after=args.outage_after,
        outage_duration=args.outage_duration,
        corrupt_prob=args.corrupt_prob,
        max_corrupt=args.max_corrupt,
        schedule=args.schedule,
    )

    try:
        asyncio.run(run_proxy(args.listen_host, args.listen_port, args.target_host, args.target_port, config))
    except KeyboardInterrupt:
        logger.info("Proxy stopped.")


if __name__ == "__main__":
    main()
