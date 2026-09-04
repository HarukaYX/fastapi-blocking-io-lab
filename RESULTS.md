# 実行結果（2026-09-03、Docker Desktop on Mac、uvicorn 1 ワーカー、WAIT_SECONDS=5）

`docker compose run --rm load <endpoint>` の出力。重いリクエスト 3 本を並行に投げ、その間 0.2 秒ごとに `/ping` を叩いた。

## /async-boto3（async def の中で boto3 を直接呼ぶ）

```
 #   elapsed  status  thread
 1     5.010     200  MainThread
 2    15.018     200  MainThread
 3    15.017     200  MainThread

heavy requests total wall time : 15.019 s
/ping samples                  : 2
/ping p50                      : 7408.6 ms
/ping max                      : 14814.6 ms
```

3 本が直列に処理され、その間 `/ping` は 15 秒の間に 2 回しか返ってこない。イベントループが止まっている。

## /def-boto3（def で boto3 を呼ぶ）

```
 #   elapsed  status  thread
 1     5.010     200  AnyIO worker thread
 2     5.009     200  AnyIO worker thread
 3     5.009     200  AnyIO worker thread

heavy requests total wall time : 5.010 s
/ping samples                  : 25
/ping p50                      : 1.5 ms
/ping max                      : 6.4 ms
```

Starlette がスレッドプール（AnyIO worker thread）に切り出しているので 3 本が並行に返り、`/ping` は影響を受けない。

## /async-to-thread（async def + asyncio.to_thread）

```
 #   elapsed  status  thread
 1     5.184     200  ThreadPoolExecutor-0_0
 2     5.181     200  ThreadPoolExecutor-0_1
 3     5.183     200  ThreadPoolExecutor-0_2

heavy requests total wall time : 5.19 s
/ping p50                      : 1.5 ms
/ping max                      : 4.6 ms
```

asyncio 側のデフォルト executor（ThreadPoolExecutor）で動く。挙動は def と同じだが、逃がす範囲を自分で選べる。

## /async-aioboto3（async def + aioboto3）

```
 #   elapsed  status  thread
 1     5.118     200  MainThread
 2     5.118     200  MainThread
 3     5.118     200  MainThread

heavy requests total wall time : 5.119 s
/ping samples                  : 26
/ping p50                      : 1.5 ms
/ping max                      : 2.7 ms
```

MainThread のまま 3 本が並行に返る。`await` の間はイベントループに制御が戻っているため。

## asyncio デバッグモード（ASYNCIO_DEBUG=1）

`/async-boto3` を 1 回叩いたときのアプリログ:

```
Executing <Task finished name='Task-3' coro=<RequestResponseCycle.run_asgi() done, ...> took 5.041 seconds
```

イベントループを 0.1 秒以上塞いだコールバックが警告される。ローカルでの検出手段としては最も手軽。

## ハマったこと

- LocalStack の初期化スクリプト（awslocal）は us-east-1 にキューを作るが、アプリ側を ap-northeast-1 にしていたので
  `QueueDoesNotExist` になった。SQS のキューはリージョン単位。アプリ側は `create_queue`（冪等）で URL を取るようにして、リージョンも揃えた
- `asyncio.to_thread` 版で「どのスレッドで動いたか」をハンドラの中で取ると MainThread になる（await の後はループに戻っている）。
  boto3 を呼んだ関数の中でスレッド名を取る必要がある

## eBPF（blocking-io-check-py）での検出

Colima の x86_64 Ubuntu VM で実施。ツールは無改造では動かず（BCC 0.29.1 のマクロ非互換、タイムアウト付きソケットの `poll` を
IDLE 扱い、uvloop の `epoll_pwait` 未対応）、`ebpf/blocking-io-check-py.patch` を当てた結果、`/async-boto3` のみ `poll` 約5秒 × 3 が
`STALL`、他は 0 件。詳細と生ログは `ebpf/README.md` と `ebpf/results/`。
