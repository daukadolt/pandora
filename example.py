"""Quick demo of the Pandora SDK — run against a live server."""

from client import PandoraClient

API = "http://localhost:8000"


def main() -> None:
    with PandoraClient(API) as p:
        print("=== health check ===")
        print(p.health())
        print()

        print("=== simple command ===")
        r = p.execute("echo 'hello from the sandbox' && uname -a")
        print(f"exit_code: {r.exit_code}")
        print(f"stdout:    {r.stdout.strip()}")
        print(f"boot:      {r.boot_ms:.0f} ms")
        print(f"ssh_ready: {r.ssh_ready_ms:.0f} ms")
        print(f"exec:      {r.exec_ms:.0f} ms")
        print(f"teardown:  {r.teardown_ms:.0f} ms")
        print(f"total:     {r.total_ms:.0f} ms")
        print()

        print("=== python one-liner ===")
        r = p.execute("python3 -c \"print(sum(range(1000)))\"")
        print(f"stdout: {r.stdout.strip()}")
        print(f"total:  {r.total_ms:.0f} ms")
        print()

        print("=== intentional failure ===")
        r = p.execute("exit 42")
        print(f"exit_code: {r.exit_code}")
        print()

        print("=== filesystem isolation ===")
        r = p.execute("echo 'secret' > /tmp/test.txt && cat /tmp/test.txt")
        print(f"stdout: {r.stdout.strip()}")
        print()

        print("=== network check ===")
        r = p.execute("ip addr show eth0 | head -3")
        print(f"stdout:\n{r.stdout.strip()}")
        print()

        print("=== prometheus metrics (last 10 lines) ===")
        text = p.metrics_text()
        for line in text.strip().splitlines()[-10:]:
            print(f"  {line}")


if __name__ == "__main__":
    main()
