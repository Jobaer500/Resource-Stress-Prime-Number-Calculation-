# resource-stress

A dependency-free Python 3 utility engineered to generate sustained, multi-vector synthetic load — encompassing computational (multi-process CPU saturation), resident memory allocation, block-level disk I/O, and outbound network traffic — against a target host, for the empirical validation of platform-level idle-detection heuristics and the verification that auto-suspend mechanisms remain unlatched under conditions of continuous, non-trivial resource utilization.

## What it does

| Resource | Mechanism |
|---|---|
| **CPU** | One OS process per core (`multiprocessing`), each running prime sieve / Fibonacci / matrix-multiply in a duty-cycled loop |
| **Memory** | A thread that allocates, touches, checksums, and frees memory chunks on a 1s cycle (no leak — bounded and released each cycle) |
| **Disk** | A thread that writes a file, `fsync`s it to force real disk I/O, reads it back, then deletes it, in a loop |
| **Network** | A thread that sends periodic HTTP requests to configurable URLs |

All four run independently and forever until the process is stopped. Any
CPU worker that dies unexpectedly is automatically restarted. No
third-party packages required — standard library only.

## Requirements

- Python 3.7+
- No pip dependencies

## Usage

```bash
# Full load — all CPU cores, default memory/disk/network settings
python3 resource_stress.py

# Lighter load
python3 resource_stress.py --cpu-target 40 --mem-mb 20 --disk-mb 5

# Disable specific subsystems
python3 resource_stress.py --no-network
python3 resource_stress.py --no-disk

# Custom worker count and network targets
python3 resource_stress.py --cpu-workers 2 --network-urls https://example.com https://httpbin.org/get
```

### All flags

| Flag | Default | Description |
|---|---|---|
| `--cpu-workers` | all cores | Number of CPU-bound worker processes |
| `--cpu-target` | 100 | Duty cycle per CPU worker, 1–100% |
| `--mem-mb` | 50 | MB touched per memory cycle |
| `--disk-mb` | 20 | MB written per disk cycle |
| `--disk-dir` | system temp dir | Directory for scratch files |
| `--network-urls` | Google/Cloudflare check endpoints | URLs to periodically request |
| `--no-cpu` | off | Disable CPU load |
| `--no-memory` | off | Disable memory load |
| `--no-disk` | off | Disable disk load |
| `--no-network` | off | Disable network load |

## Running on a VPS

```bash
# Clone
git clone <your-repo-url>
cd resource-stress

# Run in background, survives SSH disconnect
nohup python3 resource_stress.py > resource_stress.log 2>&1 &

# Verify it's running
ps aux | grep resource_stress
tail -f resource_stress.log

# Watch live resource usage
htop   # or: top
```

## Stopping

```bash
pkill -f resource_stress.py
```

Or if run as a systemd service, `systemctl stop <service-name>`.

## Notes

- This intentionally consumes real system resources — don't run it on a
  shared or production host where other workloads depend on available
  capacity.
- Disk cycles clean up after themselves (write → read → delete), so this
  will not fill up disk space over time under normal operation.
- Memory cycles are bounded per-iteration and freed each cycle, so this
  will not leak or grow unbounded.
