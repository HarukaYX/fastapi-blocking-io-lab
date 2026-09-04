# タイトル案
FastAPI の `async def` 内で boto3 を呼んでイベントループが止まるケースが、STALL として検出されなかった（報告）

# 本文

はじめまして。PyCon JP 2026 の LT を拝見して、FastAPI の `async def` の中で boto3 を呼んでイベントループが止まる例を、
このツールで検出できるか試させていただきました。

先にお断りしておくと、私は OS や C 言語、eBPF についての知見がほとんどありません。以下の原因の推測や試した変更は、
Claude Code（AI）に手伝ってもらいながら進めたもので、私自身が中身を十分に理解できているわけではありません。
推測が的外れだったり、ツールの設計意図を私が取り違えている可能性もあると思います。その場合はご容赦ください。

## 起きたこと

実際にはイベントループが止まっている（同じプロセスの別エンドポイント `/ping` の応答が 15 秒待たされる）のに、
ツールの判定は **0 stalls** でした。

## 環境

- Ubuntu 24.04.4 LTS（x86_64）、kernel 6.8.0-117-generic、BCC 0.29.1（別 issue の `BPF_LRU_HASH` の回避策を当てた状態）
- 対象: FastAPI + uvicorn（`uvicorn[standard]`、uvloop あり）、1 ワーカー、Docker コンテナ内
- ブロッキング処理: boto3 の `sqs.receive_message(WaitTimeSeconds=5)` を空キューに対して呼ぶ（きっちり 5 秒ブロックします）

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

`--min-latency 100 --json` で見ると、5 秒の待ち自体は記録されていて、`poll` として `IDLE` に分類されていました
（tid = pid なので、イベントループのスレッドだと思います）。

```json
{"pid":3071,"tid":3071,"comm":"uvicorn","fd":-1,"op":"poll","duration_ms":5033.57,"ret":1,"nonblock":-1,"via_epoll":false,"verdict":"IDLE"}
```

## 原因と思われる点

### (1) 5 秒の待ちが `recvfrom` ではなく `poll` として記録され、`IDLE` に分類されている

botocore はソケットに timeout を設定しており、CPython は timeout 付きソケットを内部でノンブロッキングにして
`poll()` で読めるようになるまで待つ実装になっているそうです（この部分は AI の説明によるもので、私自身は確認しきれていません）。
そのため 5 秒のブロックが `recvfrom` ではなく `poll` の所要時間として現れているようです。

現在の `classify` では `epoll_wait` / `poll` / `select` がまとめて `V_IDLE` になっているため、ループスレッド上で
`poll` が 5 秒待っていても正常扱いになっている、という理解です。「asyncio のループ自身の待ちは `epoll_wait` なので、
`poll` / `select` がループスレッドで長く待つのはループのアイドルではないのでは」という考え方もあるようですが、
ツールの設計意図として意図的に IDLE にされている可能性もあり、判断はお任せします。

また `sys_enter_poll` の probe で `loop_tid` が立つため、`def` エンドポイントなどワーカースレッドで `poll` を呼んだ場合も
ループスレッド扱いになるように見えました。

### (2) uvloop 使用時、ループスレッドが `loop_tid` に登録されない

`uvicorn[standard]` は uvloop を使い、uvloop（libuv）は `epoll_wait` ではなく `epoll_pwait` を呼ぶそうです。
`cat /proc/<pid>/syscall` でメインスレッドが 281（x86_64 の epoll_pwait）で待っていることは確認しました。
現在は `sys_enter_epoll_wait` でだけ `loop_tid` を立てているので、uvloop のループスレッドは識別されず、
既定モードでは STALL にならないようです（`--all-threads` を付けると出ました）。

## 試した変更と結果

参考までに、AI に提案してもらった変更を手元で当ててみたところ、期待していた判定になりました。
内容を私が十分に説明できないため PR は出さず、差分を置いておくだけにします。もし方向性が合っていれば、
作者様の手で正しい形にしていただけると嬉しいです。

- `classify`: `epoll_wait` だけを `V_IDLE` にし、`poll` / `select` はループスレッド上で閾値以上なら `V_STALL`
- `sys_enter_poll` から `loop_tid` の更新を外す
- `sys_enter_epoll_pwait` / `sys_enter_epoll_pwait2` にも `loop_tid` を立てる probe を追加

差分: https://github.com/HarukaYX/fastapi-blocking-io-lab/blob/main/ebpf/blocking-io-check-py.patch

| エンドポイント | 変更前 | 変更後 |
|---|---|---|
| `async def` + boto3 | 0 stalls | STALL × 3（`poll` 5019.8 / 5020.4 / 5018.0 ms） |
| `def` + boto3 | 0 stalls | 0 stalls |
| `async def` + `asyncio.to_thread(boto3)` | 0 stalls | 0 stalls |
| `async def` + aioboto3 | 0 stalls | 0 stalls |

```
08:57:21.755 pid=  3071 comm=uvicorn  fd=-1  op=poll  dur=  5019.849ms ret=1  flags=?---  verdict=STALL
=== summary (3 events, 3 blocking, 3 stalls, 40.1s) ===
comm     pid   op    peer  count  block  stall     total       max       p50       p95       p99
uvicorn  3071  poll  -         3      3      3  15058.24  5020.423  5019.849  5020.423  5020.423
```

長文になってしまい申し訳ありません。素晴らしいツールと LT をありがとうございました。
