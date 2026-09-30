"""Gateway overhead: how much latency does Tollgate add to a request?

k6 posts the same chat completion to the mock provider directly, then through Tollgate
(configured with the same mock, no delay), with the same load. The difference in p50/p95 is
Tollgate's cost per request: key lookup, Redis rate limits, the Postgres budget reservation
and settle, and the request log.

    uv run python -m loadtest.overhead                  # 10 virtual users, 20 s each
    uv run python -m loadtest.overhead --vus 20 --duration 30s

Needs Docker (k6 runs as grafana/k6) and the compose Postgres and Redis. Tollgate runs as one
uvicorn worker on a scratch database.
"""

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import generate_api_key
from app.models import ApiKey, Customer
from loadtest.overspend import scratch_database

ROOT = Path(__file__).resolve().parents[1]
K6_IMAGE = "grafana/k6:2.3.0"
UVICORN = [sys.executable, "-m", "uvicorn", "--log-level", "warning", "--no-access-log"]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def server(app: str, port: int, env: dict[str, str]) -> Iterator[None]:
    process = subprocess.Popen([*UVICORN, app, "--port", str(port)], cwd=ROOT, env=os.environ | env)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=1)
                break
            except OSError:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError(f"{app} didn't start") from None
                time.sleep(0.2)
        yield
    finally:
        process.terminate()
        process.wait(timeout=10)


def create_key(database_url: str) -> str:
    """A key that no limit or budget will stop during the benchmark."""
    new_key = generate_api_key()
    engine = create_engine(database_url)
    with Session(engine) as session:
        customer = Customer(name="Overhead benchmark")
        session.add(customer)
        session.flush()
        session.add(
            ApiKey(
                customer_id=customer.id,
                key_hash=new_key.key_hash,
                prefix=new_key.prefix,
                rpm_limit=10_000_000,
                tpm_limit=2_000_000_000,
                monthly_budget_micros=10**15,
            )
        )
        session.commit()
    engine.dispose()
    return new_key.key


def k6(target: str, key: str, vus: int, duration: str, out_dir: Path, name: str) -> dict:
    """Run k6 against `target` and return http_req_duration's stats, in ms."""
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "--user",
            f"{os.getuid()}",
            "-v",
            f"{ROOT / 'loadtest' / 'k6'}:/scripts:ro",
            "-v",
            f"{out_dir}:/out",
            K6_IMAGE,
            "run",
            "--quiet",
            "-e",
            f"TARGET={target}",
            "-e",
            f"KEY={key}",
            "-e",
            f"VUS={vus}",
            "-e",
            f"DURATION={duration}",
            "--summary-export",
            f"/out/{name}.json",
            "/scripts/overhead.js",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    summary = json.loads((out_dir / f"{name}.json").read_text())
    metrics = summary["metrics"]
    duration_ms = metrics["http_req_duration"]
    return {
        "p50": duration_ms["med"],
        "p95": duration_ms["p(95)"],
        "p99": duration_ms["p(99)"],
        "requests": metrics["http_reqs"]["count"],
        "rps": metrics["http_reqs"]["rate"],
        "failed": metrics["checks"]["fails"],
    }


def warm_up(url: str, key: str, requests: int = 50) -> None:
    body = json.dumps(
        {"model": "mock", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 32}
    ).encode()
    for _ in range(requests):
        request = urllib.request.Request(
            url,
            body,
            {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        urllib.request.urlopen(request, timeout=10).read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--vus", type=int, default=10)
    parser.add_argument("--duration", default="20s")
    args = parser.parse_args()

    settings = Settings(_env_file=None)
    mock_port, tollgate_port = free_port(), free_port()
    with (
        scratch_database(settings.database_url) as database_url,
        tempfile.TemporaryDirectory() as out,
    ):
        key = create_key(database_url)
        tollgate_env = {
            "DATABASE_URL": database_url,
            "REDIS_URL": settings.redis_url,
            "MOCK_DELAY_MS": "0",
            "MOCK_OUTPUT_TOKENS": "32",
        }
        direct_url = f"http://127.0.0.1:{mock_port}/v1/chat/completions"
        tollgate_url = f"http://127.0.0.1:{tollgate_port}/v1/chat/completions"
        with (
            server("loadtest.mock_server:app", mock_port, {}),
            server("app.main:app", tollgate_port, tollgate_env),
        ):
            warm_up(direct_url, key)
            warm_up(tollgate_url, key)
            direct = k6(direct_url, key, args.vus, args.duration, Path(out), "direct")
            through = k6(tollgate_url, key, args.vus, args.duration, Path(out), "tollgate")

    print(f"\n{args.vus} virtual users, {args.duration} each, mock provider with no delay\n")
    print("| | p50 | p95 | p99 | Requests/s | Failed |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, result in (("Mock directly", direct), ("Through Tollgate", through)):
        print(
            f"| {name} | {result['p50']:.1f} ms | {result['p95']:.1f} ms | "
            f"{result['p99']:.1f} ms | {result['rps']:.0f} | {result['failed']} |"
        )
    print(
        f"| **Tollgate adds** | **{through['p50'] - direct['p50']:.1f} ms** | "
        f"**{through['p95'] - direct['p95']:.1f} ms** | "
        f"{through['p99'] - direct['p99']:.1f} ms | | |"
    )
    return 0 if direct["failed"] == through["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
