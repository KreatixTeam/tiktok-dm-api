"""Measure a command's whole process tree on macOS or Linux, including this sampler."""

import argparse
import json
import os
import signal
import subprocess
import time


def tree_rss(root):
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss="], text=True)
    table = [tuple(map(int, line.split())) for line in rows.splitlines() if line.strip()]
    children = {root}
    while True:
        found = {pid for pid, ppid, rss in table if ppid in children}
        if found <= children:
            break
        children |= found
    return sum(rss * 1024 for pid, ppid, rss in table if pid in children), len(children)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--limit-bytes", type=int, default=300000000)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("Provide the server command after --")
    process = subprocess.Popen(command, start_new_session=True)
    started = time.monotonic()
    peak = processes = 0
    exceeded = False
    unexpected_exit = False
    try:
        while time.monotonic() - started < args.seconds:
            rss, count = tree_rss(os.getpid())
            peak, processes = max(peak, rss), max(processes, count)
            if rss > args.limit_bytes:
                exceeded = True
                break
            if process.poll() is not None:
                unexpected_exit = True
                break
            time.sleep(0.2)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    print(
        json.dumps(
            {
                "peak_tree_rss_bytes": peak,
                "peak_tree_rss_mb": round(peak / 1000000, 2),
                "maximum_processes": processes,
                "includes_sampler": True,
                "includes_browser": False,
                "limit_bytes": args.limit_bytes,
                "exceeded": exceeded,
                "seconds": round(time.monotonic() - started, 2),
                "unexpected_exit": unexpected_exit,
            }
        ),
        flush=True,
    )
    raise SystemExit(1 if exceeded or unexpected_exit else 0)


if __name__ == "__main__":
    main()
