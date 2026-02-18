"""
Pandora – Stress Test

Fires N parallel sandboxes, each running a sequence of commands, and
reports aggregate latency stats.

Usage:
    python stress_test.py                             # 10 sandboxes, 4 parallel
    python stress_test.py -n 20 -p 4                  # 20 sandboxes, 4 parallel
    python stress_test.py -n 8 -p 4 --execs-per 5     # 5 commands each
    python stress_test.py -c "sleep 1 && echo done"
"""

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from client import PandoraClient

API = "http://localhost:8000"


@dataclass
class SandboxRun:
    """Aggregated result from one sandbox session."""

    sandbox_id: str
    slot_id: int
    acquire_ms: float
    exec_results: list[float] = field(default_factory=list)
    total_ms: float = 0.0
    error: str = ""


def run_one(client: PandoraClient, code: str, execs_per: int,
            idx: int) -> SandboxRun:
    """Create a sandbox, run commands, close it, return stats."""
    t0 = time.monotonic()
    try:
        sb = client.create(idle_timeout=120)
    except Exception as e:
        return SandboxRun(
            sandbox_id="", slot_id=-1, acquire_ms=0, error=f"create failed: {e}",
        )

    run = SandboxRun(
        sandbox_id=sb.sandbox_id,
        slot_id=sb.slot_id,
        acquire_ms=sb.acquire_ms,
    )
    try:
        for _ in range(execs_per):
            r = sb.exec(code)
            run.exec_results.append(r.exec_ms)
    except Exception as e:
        run.error = str(e)
    finally:
        try:
            sb.close()
        except Exception:
            pass
        run.total_ms = (time.monotonic() - t0) * 1000

    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Pandora stress test")
    parser.add_argument("-n", "--requests", type=int, default=10,
                        help="Total number of sandboxes to create (default: 10)")
    parser.add_argument("-p", "--parallel", type=int, default=4,
                        help="Max concurrent sandboxes (default: 4)")
    parser.add_argument("-e", "--execs-per", type=int, default=3,
                        help="Number of exec calls per sandbox (default: 3)")
    parser.add_argument("-c", "--code", type=str, default="echo hello && hostname",
                        help="Command to execute")
    parser.add_argument("--url", type=str, default=API,
                        help=f"API base URL (default: {API})")
    args = parser.parse_args()

    client = PandoraClient(args.url)

    print(f"Health: {client.health()}")
    print(f"Plan: {args.requests} sandboxes × {args.execs_per} execs, "
          f"{args.parallel} parallel")
    print(f"Command: {args.code!r}")
    print()

    runs: list[SandboxRun] = []
    wall_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {
            pool.submit(run_one, client, args.code, args.execs_per, i): i
            for i in range(args.requests)
        }
        for future in as_completed(futures):
            idx = futures[future]
            run = future.result()
            runs.append(run)
            if run.error:
                print(f"  [{idx:3d}] FAIL: {run.error}")
            else:
                avg_exec = statistics.mean(run.exec_results) if run.exec_results else 0
                print(
                    f"  [{idx:3d}] sb={run.sandbox_id} slot={run.slot_id} "
                    f"acquire={run.acquire_ms:.0f}ms "
                    f"execs={len(run.exec_results)} "
                    f"avg_exec={avg_exec:.0f}ms "
                    f"total={run.total_ms:.0f}ms"
                )

    wall_s = time.monotonic() - wall_start
    ok = [r for r in runs if not r.error]
    fail = [r for r in runs if r.error]

    print()
    print("=" * 64)
    print(f"Wall clock:     {wall_s:.1f}s")
    print(f"Succeeded:      {len(ok)}")
    print(f"Failed:         {len(fail)}")

    if not ok:
        print("No successful results.")
        client.close()
        sys.exit(1)

    def stats_line(name: str, values: list[float]) -> str:
        s = sorted(values)
        return (
            f"  {name:<14s}  "
            f"min={s[0]:7.0f}  "
            f"p50={s[len(s)//2]:7.0f}  "
            f"p90={s[int(len(s)*0.9)]:7.0f}  "
            f"p99={s[min(int(len(s)*0.99), len(s)-1)]:7.0f}  "
            f"max={s[-1]:7.0f}  ms"
        )

    acquires = [r.acquire_ms for r in ok]
    totals = [r.total_ms for r in ok]
    all_execs = [ms for r in ok for ms in r.exec_results]

    print()
    print("Latency breakdown (ms):")
    print(stats_line("acquire", acquires))
    if all_execs:
        print(stats_line("exec (each)", all_execs))
    print(stats_line("total/sandbox", totals))
    print()

    total_execs = sum(len(r.exec_results) for r in ok)
    throughput = total_execs / wall_s if wall_s > 0 else 0
    print(f"Total execs:    {total_execs}")
    print(f"Throughput:     {throughput:.2f} execs/sec")

    slots_used = sorted(set(r.slot_id for r in ok))
    print(f"Slots used:     {slots_used}")

    client.close()


if __name__ == "__main__":
    main()
