#!/usr/bin/env python3
"""
resource_stress.py
--------------------
All-in-one continuous resource load generator for VPS testing.

Unlike keep_alive_load.py (CPU + memory only), this covers FOUR resource
dimensions so monitoring systems see broad, sustained activity rather than
just a CPU spike:

  1. CPU     - multiprocessing workers doing real compute (prime sieve,
               fibonacci, matrix-ish math) across all cores.
  2. Memory  - a dedicated thread that allocates, touches, and releases
               chunks continuously so RSS stays visibly non-zero.
  3. Disk    - a thread that writes, fsyncs, reads back, and deletes temp
               files in a loop, generating real disk I/O.
  4. Network - a thread that does periodic HTTP requests to a configurable
               set of URLs (defaults to a couple of reliable public
               endpoints), generating outbound network I/O.

Each subsystem runs independently and can be toggled on/off via flags, so
you can run just the parts relevant to whatever "low resource" signal your
monitoring is keying off (some platforms watch CPU only, others watch
CPU+network, etc).

Runs forever until killed (Ctrl+C, SIGTERM, or process manager stop).

Usage:
    python3 resource_stress.py                          # everything, all cores
    python3 resource_stress.py --no-network              # skip network I/O
    python3 resource_stress.py --no-disk                 # skip disk I/O
    python3 resource_stress.py --cpu-workers 2 --mem-mb 100 --disk-mb 20
    python3 resource_stress.py --network-urls https://example.com https://httpbin.org/get
"""

import argparse
import logging
import multiprocessing as mp
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(processName)s/%(threadName)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

def find_primes(limit: int) -> int:
    sieve = [True] * (limit + 1)
    sieve[0] = sieve[1] = False
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            for j in range(i * i, limit + 1, i):
                sieve[j] = False
    return sum(sieve)


def fibonacci(n: int) -> int:
    memo = {}

    def fib(k):
        if k in memo:
            return memo[k]
        if k <= 1:
            return k
        memo[k] = fib(k - 1) + fib(k - 2)
        return memo[k]

    return fib(n)


def matrix_churn(size: int) -> float:
    """Simple nested-loop matrix multiply — no numpy dependency required."""
    a = [[(i * j) % 97 for j in range(size)] for i in range(size)]
    b = [[(i + j) % 89 for j in range(size)] for i in range(size)]
    result = 0
    for i in range(size):
        for j in range(size):
            s = 0
            for k in range(size):
                s += a[i][k] * b[k][j]
            result += s
    return result


def cpu_worker(worker_id: int, cpu_target: float, stop_event):
    logger.info(f"CPU worker {worker_id} started (target={cpu_target}%)")
    iteration = 0
    burst_seconds = 0.5
    sleep_seconds = burst_seconds * (100 - cpu_target) / max(cpu_target, 1)

    while not stop_event.is_set():
        iteration += 1
        burst_end = time.time() + burst_seconds

        while time.time() < burst_end and not stop_event.is_set():
            task = iteration % 3
            if task == 0:
                find_primes(200_000 + (iteration % 10) * 50_000)
            elif task == 1:
                fibonacci(28 + (iteration % 6))
            else:
                matrix_churn(40)

        if cpu_target < 100 and sleep_seconds > 0 and not stop_event.is_set():
            time.sleep(sleep_seconds)

        if iteration % 20 == 0:
            logger.info(f"CPU worker {worker_id}: {iteration} bursts done")


def run_cpu_process(worker_id: int, cpu_target: float):
    stop_event = threading.Event()

    def handle_sig(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)
    cpu_worker(worker_id, cpu_target, stop_event)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def memory_worker(mem_mb: int, stop_event):
    logger.info(f"Memory worker started (~{mem_mb}MB per cycle)")
    iteration = 0
    chunk = 1024 * 1024

    while not stop_event.is_set():
        iteration += 1
        data = bytearray(chunk * mem_mb)
        for i in range(0, len(data), 4096):
            data[i] = (data[i] + 1) % 256
        checksum = sum(data[:2048])
        del data

        if iteration % 10 == 0:
            logger.info(f"Memory cycle {iteration} (checksum={checksum})")

        stop_event.wait(timeout=1.0)


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------

def disk_worker(disk_mb: int, work_dir: str, stop_event):
    logger.info(f"Disk worker started (~{disk_mb}MB per cycle, dir={work_dir})")
    os.makedirs(work_dir, exist_ok=True)
    iteration = 0
    chunk = os.urandom(1024 * 1024)  # 1MB of random bytes, reused per write

    while not stop_event.is_set():
        iteration += 1
        file_path = os.path.join(work_dir, f"stress_{iteration % 5}.tmp")
        try:
            with open(file_path, "wb") as f:
                for _ in range(disk_mb):
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())

            with open(file_path, "rb") as f:
                while f.read(1024 * 1024):
                    pass

            os.remove(file_path)
        except OSError as e:
            logger.warning(f"Disk worker I/O error (continuing): {e}")

        if iteration % 10 == 0:
            logger.info(f"Disk cycle {iteration} ({disk_mb}MB write+read+delete)")

        stop_event.wait(timeout=1.0)

    shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

DEFAULT_URLS = [
    "https://www.google.com/generate_204",
    "https://cloudflare.com/cdn-cgi/trace",
]


def network_worker(urls, stop_event):
    logger.info(f"Network worker started (urls={urls})")
    iteration = 0

    while not stop_event.is_set():
        iteration += 1
        url = urls[iteration % len(urls)]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "resource-stress/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read(4096)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning(f"Network request to {url} failed (continuing): {e}")

        if iteration % 10 == 0:
            logger.info(f"Network cycle {iteration}")

        stop_event.wait(timeout=2.0)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Multi-resource load generator for VPS testing")
    parser.add_argument("--cpu-workers", type=int, default=os.cpu_count() or 1,
                         help="Number of CPU-bound worker processes (default: all cores)")
    parser.add_argument("--cpu-target", type=float, default=100.0,
                         help="Duty cycle per CPU worker, 1-100 (default: 100)")
    parser.add_argument("--mem-mb", type=int, default=50,
                         help="MB touched per memory cycle (default: 50)")
    parser.add_argument("--disk-mb", type=int, default=20,
                         help="MB written per disk cycle (default: 20)")
    parser.add_argument("--disk-dir", type=str, default=os.path.join(tempfile.gettempdir(), "resource_stress"),
                         help="Directory for scratch files (default: system temp dir)")
    parser.add_argument("--network-urls", nargs="+", default=DEFAULT_URLS,
                         help="URLs to periodically request")
    parser.add_argument("--no-cpu", action="store_true", help="Disable CPU load")
    parser.add_argument("--no-memory", action="store_true", help="Disable memory load")
    parser.add_argument("--no-disk", action="store_true", help="Disable disk load")
    parser.add_argument("--no-network", action="store_true", help="Disable network load")
    args = parser.parse_args()

    if not (1 <= args.cpu_target <= 100):
        parser.error("--cpu-target must be between 1 and 100")

    logger.info("=" * 70)
    logger.info("Resource stress generator starting")
    logger.info(f"CPU: {'off' if args.no_cpu else f'{args.cpu_workers} workers @ {args.cpu_target}%'}")
    logger.info(f"Memory: {'off' if args.no_memory else f'{args.mem_mb}MB/cycle'}")
    logger.info(f"Disk: {'off' if args.no_disk else f'{args.disk_mb}MB/cycle in {args.disk_dir}'}")
    logger.info(f"Network: {'off' if args.no_network else args.network_urls}")
    logger.info("=" * 70)

    cpu_processes = []
    threads = []
    thread_stop_events = []

    if not args.no_cpu:
        for i in range(args.cpu_workers):
            p = mp.Process(target=run_cpu_process, args=(i, args.cpu_target), daemon=True)
            p.start()
            cpu_processes.append(p)

    if not args.no_memory:
        ev = threading.Event()
        t = threading.Thread(target=memory_worker, args=(args.mem_mb, ev), daemon=True, name="MemThread")
        t.start()
        threads.append(t)
        thread_stop_events.append(ev)

    if not args.no_disk:
        ev = threading.Event()
        t = threading.Thread(target=disk_worker, args=(args.disk_mb, args.disk_dir, ev), daemon=True, name="DiskThread")
        t.start()
        threads.append(t)
        thread_stop_events.append(ev)

    if not args.no_network:
        ev = threading.Event()
        t = threading.Thread(target=network_worker, args=(args.network_urls, ev), daemon=True, name="NetThread")
        t.start()
        threads.append(t)
        thread_stop_events.append(ev)

    def shutdown(signum, frame):
        logger.info("Stop signal received — shutting down all workers")
        for ev in thread_stop_events:
            ev.set()
        for p in cpu_processes:
            p.terminate()
        for p in cpu_processes:
            p.join(timeout=5)
        for t in threads:
            t.join(timeout=5)
        logger.info("Shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while True:
            time.sleep(10)
            for idx, p in enumerate(cpu_processes):
                if not p.is_alive():
                    logger.warning(f"CPU worker {idx} died — restarting")
                    new_p = mp.Process(target=run_cpu_process, args=(idx, args.cpu_target), daemon=True)
                    new_p.start()
                    cpu_processes[idx] = new_p
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
