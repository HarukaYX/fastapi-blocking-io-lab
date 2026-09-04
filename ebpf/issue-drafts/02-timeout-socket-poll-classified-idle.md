# タイトル案
タイムアウト付きソケット（boto3 など）の `poll` がループスレッド上で 5 秒待っても `IDLE` になり、STALL として検出されない

# 本文

PyCon JP 2026 の LT を拝見して、FastAPI の `async def` の中で boto3 を呼んでイベントループが止まる例を、このツールで検出できるか
試しました。実際にループは止まっているのに（別エンドポイント `/ping` の応答が 15 秒待たされる）、ツールの判定は **0 stalls** でした。
原因と思われる点を 2 つ報告します。

## 環境

- Ubuntu 24.04.4 LTS（x86_64）、kernel 6.8.0-117-generic、BCC 0.29.1（#1 の回避策を当てた状態）
- 対象: uvicorn 0.3x + FastAPI、`uvicorn[standard]`（uvloop あり）、1 ワーカー、Docker コンテナ内
- ブロッキング処理: boto3 の `sqs.receive_message(WaitTimeSeconds=5)` を空キューに対して呼ぶ（きっちり 5 秒ブロック）

## 再現手順

再現用のリポジトリを用意しました: https://github.com/HarukaYX/fastapi-blocking-io-lab

```bash
docker compose up -d --build                       # LocalStack(SQS) + FastAPI
PID=$(pgrep -f "uvicorn app.main:app" | head -1)
sudo blocking-io-check --pid $PID -d 40 --hide-dns --hide-netlink --only-blocking --summary &
docker compose run --rm load async-boto3           # async def の中で boto3 を呼ぶエンドポイントに並行 3 本
```

## 実際の出力

```
=== summary (0 events, 0 blocking, 0 stalls, 45.0s) ===
(no events matched)
traced 956 I/O operations in total; 956 below the 1.0ms display threshold were not shown
```

`--min-latency 100 --json` で見ると、5 秒の待ちは記録されていて、`poll` として `IDLE` に分類されていました（tid = pid = イベントループのスレッド）。

```json
{"pid":3071,"tid":3071,"comm":"uvicorn","fd":-1,"op":"poll","duration_ms":5033.57,"ret":1,"nonblock":-1,"via_epoll":false,"verdict":"IDLE"}
```

## 原因の推測（1）: タイムアウト付きソケットの待ちは `recvfrom` ではなく `poll` に出る

botocore はソケットに timeout（既定 60 秒）を設定します。CPython は timeout 付きソケットを内部で `O_NONBLOCK` にし、
`poll()` で読めるようになるまで待ってから `recv` する実装（`sock_call` → `internal_select`）なので、ブロックしている 5 秒は
`recvfrom` ではなく `poll` の所要時間として現れます。

現在の `classify` は `epoll_wait` / `poll` / `select` を「待機系」としてまとめて `V_IDLE` にしているため、
ループスレッド上で `poll` が 5 秒待っていても正常扱いになります。asyncio のループ自身の待ちは `epoll_wait` なので、
`poll` / `select` がループスレッドで長く待つのは、ループのアイドルではなく別の同期 I/O だと考えられます。

また `sys_enter_poll` の probe が `loop_tid` を立てているため、ワーカースレッド（`def` エンドポイントや `run_in_executor`）で
`poll` を呼んだスレッドもループスレッド扱いになります。

## 原因の推測（2）: uvloop だとループスレッドが識別されない

`uvicorn[standard]` は uvloop を使い、uvloop（libuv）は `epoll_wait` ではなく `epoll_pwait` を呼びます。
`cat /proc/<pid>/syscall` でメインスレッドが 281（x86_64 の epoll_pwait）で待っているのを確認しました。
現在は `sys_enter_epoll_wait` だけで `loop_tid` を立てているので、uvloop のループスレッドは識別されず、
`REQUIRE_LOOP_THREAD` が有効な既定モードでは STALL になりません（`--all-threads` を付けると出ます）。

## 試した変更と結果

手元で次の変更を当てると、期待どおりの判定になりました（C や eBPF の理解が浅いので PR ではなく参考として置きます）。

- `classify`: `epoll_wait` だけを `V_IDLE` にし、`poll` / `select` はループスレッド上で閾値以上なら `V_STALL`
- `sys_enter_poll` から `loop_tid` の更新を外す
- `sys_enter_epoll_pwait` / `sys_enter_epoll_pwait2` にも `loop_tid` を立てる probe を追加

差分: https://github.com/HarukaYX/fastapi-blocking-io-lab/blob/main/ebpf/blocking-io-check-py.patch

| エンドポイント | 変更前 | 変更後 |
|---|---|---|
| `async def` + boto3 | 0 stalls | **STALL × 3**（`poll` 5019.8 / 5020.4 / 5018.0 ms） |
| `def` + boto3 | 0 stalls | 0 stalls |
| `async def` + `asyncio.to_thread(boto3)` | 0 stalls | 0 stalls |
| `async def` + aioboto3 | 0 stalls | 0 stalls |

```
08:57:21.755 pid=  3071 comm=uvicorn  fd=-1  op=poll  dur=  5019.849ms ret=1  flags=?---  verdict=STALL
=== summary (3 events, 3 blocking, 3 stalls, 40.1s) ===
comm     pid   op    peer  count  block  stall     total       max       p50       p95       p99
uvicorn  3071  poll  -         3      3      3  15058.24  5020.423  5019.849  5020.423  5020.423
```

`poll` は fd が -1 で記録されるため peer が取れず、どの接続で待っていたかは分かりません。`poll` の `ufds` から fd を読めると
さらに有用になりそうですが、そこまでは手を出せていません。

素晴らしいツールをありがとうございます。LT もとても勉強になりました。
