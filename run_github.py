"""GitHub Actions runner for the existing InnovisionBD HTTPS archive gateway.
No Google Cloud project, Cloud Run, or remote MariaDB connectivity is required.
"""
import os
import sys
import time
from worker import gateway, run_once


def main():
    required = ("IVBD_GATEWAY_URL", "IVBD_GATEWAY_SECRET")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        print("Missing required GitHub Actions configuration: " + ", ".join(missing), file=sys.stderr)
        return 2
    print("Checking the protected Hostinger gateway...")
    try:
        reply = gateway("check")
    except Exception as exc:
        # Avoid reflecting sensitive network details from exception messages.
        print("Gateway check failed: " + type(exc).__name__, file=sys.stderr)
        return 3
    if not reply.get("destination_connected"):
        print("Workspace destination is not connected. Configure it on innovisionbd.com first.", file=sys.stderr)
        return 4
    print("Connected. Running archive batches against authorized source accounts.")
    until = time.monotonic() + min(300, max(30, int(os.getenv("IVBD_ACTION_SECONDS", "210"))))
    batches = 0
    while time.monotonic() < until:
        try:
            stats = run_once()
        except Exception as exc:
            print("Archive batch failed: " + type(exc).__name__, file=sys.stderr)
            return 5
        batches += 1
        outcomes = stats.get("results", {})
        print("Batch %s: %s" % (batches, {k: int(v) for k, v in outcomes.items()}))
        if outcomes.get("gateway_error"):
            return 6
        # If no file tasks were claimed, retry briefly in case folder/index steps produced more.
        if outcomes.get("empty") and not any(outcomes.get(k) for k in ("copied", "already", "failed", "yielded")):
            break
    print("Finished this scheduled session. Pending work resumes at the next scheduled run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
