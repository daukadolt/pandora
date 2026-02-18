"""Quick demo of the Pandora SDK — run against a live server."""

from client import PandoraClient

API = "http://localhost:8000"


def main() -> None:
    client = PandoraClient(API)

    print("=== health check ===")
    print(client.health())
    print()

    print("=== create sandbox ===")
    with client.create(idle_timeout=120) as sb:
        print(f"sandbox_id:  {sb.sandbox_id}")
        print(f"slot_id:     {sb.slot_id}")
        print(f"guest_ip:    {sb.guest_ip}")
        print(f"warm:        {sb.warm}")
        print(f"acquire_ms:  {sb.acquire_ms:.1f}")
        print()

        print("=== run commands in the same sandbox ===")
        r = sb.exec("echo 'hello from the sandbox' && uname -a")
        print(f"stdout:  {r.stdout.strip()}")
        print(f"exec_ms: {r.exec_ms:.0f}")
        print()

        r = sb.exec("python3 -c \"print(sum(range(1000)))\"")
        print(f"python says: {r.stdout.strip()}")
        print()

        print("=== state persists across execs ===")
        sb.exec("echo 'hello world' > /tmp/greeting.txt")
        r = sb.exec("cat /tmp/greeting.txt")
        print(f"file contents: {r.stdout.strip()}")
        print()

        print("=== intentional failure ===")
        r = sb.exec("exit 42")
        print(f"exit_code: {r.exit_code}")
        print()

        print("=== network info ===")
        r = sb.exec("ip addr show eth0 | head -3")
        print(f"stdout:\n{r.stdout.strip()}")
        print()

    print("=== sandbox auto-closed ===")

    print()
    print("=== list active sandboxes (should be empty) ===")
    print(client.list_sandboxes())
    print()

    print("=== prometheus metrics (last 10 lines) ===")
    text = client.metrics_text()
    for line in text.strip().splitlines()[-10:]:
        print(f"  {line}")

    client.close()


if __name__ == "__main__":
    main()
