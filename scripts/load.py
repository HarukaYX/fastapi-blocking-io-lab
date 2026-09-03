"""重いエンドポイントに並行リクエストを投げつつ、/ping の応答時間を計測する。

使い方:
  docker compose run --rm load async-boto3
  docker compose run --rm load def-boto3 --concurrency 5

/ping が数秒待たされるなら、イベントループが止まっている。
"""

import argparse
import asyncio
import os
import statistics
import time

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


async def hit(client: httpx.AsyncClient, path: str) -> tuple[float, dict]:
    started = time.perf_counter()
    try:
        resp = await client.get(path)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        body.setdefault("status", resp.status_code)
        if resp.status_code >= 400:
            body["error"] = resp.text[:120]
    except httpx.HTTPError as exc:
        body = {"status": None, "error": repr(exc)}
    return time.perf_counter() - started, body


async def ping_loop(client: httpx.AsyncClient, stop: asyncio.Event, interval: float) -> list[float]:
    latencies: list[float] = []
    while not stop.is_set():
        elapsed, _ = await hit(client, "/ping")
        latencies.append(elapsed)
        await asyncio.sleep(interval)
    return latencies


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoint", help="async-boto3 / def-boto3 / async-to-thread / async-aioboto3")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--ping-interval", type=float, default=0.2)
    args = parser.parse_args()
    path = "/" + args.endpoint.lstrip("/")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120) as client:
        # ウォームアップ（get_queue_url などの初回コストを除く）
        await hit(client, "/ping")

        stop = asyncio.Event()
        pinger = asyncio.create_task(ping_loop(client, stop, args.ping_interval))
        started = time.perf_counter()
        heavy = await asyncio.gather(*(hit(client, path) for _ in range(args.concurrency)))
        total = time.perf_counter() - started
        stop.set()
        pings = await pinger

    print(f"\n=== {path}  concurrency={args.concurrency} ===")
    print(f"{'#':>2}  {'elapsed':>8}  {'status':>6}  thread")
    for i, (elapsed, body) in enumerate(heavy, 1):
        thread = body.get("thread", body.get("error", "?"))
        print(f"{i:>2}  {elapsed:8.3f}  {body.get('status')!s:>6}  {thread}")
    print(f"\nheavy requests total wall time : {total:.3f} s")
    if pings:
        print(f"/ping samples                  : {len(pings)}")
        print(f"/ping p50                      : {statistics.median(pings) * 1000:.1f} ms")
        print(f"/ping max                      : {max(pings) * 1000:.1f} ms")
    else:
        print("/ping samples                  : 0 (heavy requests finished before first ping)")


if __name__ == "__main__":
    asyncio.run(main())
