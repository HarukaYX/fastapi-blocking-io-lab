# fastapi-blocking-io-lab

FastAPI の `async def` の中で boto3（同期 I/O）を呼ぶとイベントループ全体が止まる、を手元で再現し、
asyncio のデバッグモードと eBPF の 2 つの手段で「止まっている」ことを外から検出する検証リポジトリ。

きっかけは PyCon JP 2026 の 2 つの発表。

- セッション「[FastAPI の並行処理モデルを完全に理解する](https://2026.pycon.jp/ja/talks/HHLXYU)」（[スライド](https://speakerdeck.com/hoto17296/fastapi-no-heikou-shori-moderu-o-kanzen-ni-rikai-suru)）
- LT「[そのasync、止まってない？ "鉄板"イベントループ ブロッキング処理検出術](https://speakerdeck.com/ryuichi1208/so-async-toma-tenai-teppan-ibento-rupu-burokkingu-shori-kenshutsujutsu)」（eBPF ツール [blocking-io-check-py](https://github.com/ryuichi1208/blocking-io-check-py)）

## 目次

- [何を再現するか](#何を再現するか)
- [使い方](#使い方)
- [結果 1: 負荷テスト](#結果-1-負荷テスト)
- [結果 2: asyncio デバッグモードでの検出](#結果-2-asyncio-デバッグモードでの検出)
- [結果 3: eBPF（blocking-io-check-py）での検出](#結果-3-ebpfblocking-io-check-pyでの検出)
- [ハマったこと](#ハマったこと)
- [補足](#補足)

## 何を再現するか

同期 I/O のブロッキング処理には、**空の SQS キューへのロングポーリング** `receive_message(WaitTimeSeconds=5)` を使う。
キューが空だとメッセージが来るまで最大 5 秒待つので、boto3 の呼び出しがきっちり 5 秒ブロックする。
遅延を作るプロキシを挟む必要がなく、SQS は LocalStack で用意できる。

同じ処理を書き方だけ変えた 4 つのエンドポイントと、何もせず即返す `/ping`（イベントループが生きているかを見るカナリア）を用意している（`app/main.py`）。

| エンドポイント | 書き方 | 期待する挙動 |
|---|---|---|
| `/async-boto3` | `async def` の中で boto3 を直接呼ぶ | **イベントループが止まる**。並行リクエストは直列に返り、`/ping` も数秒待たされる |
| `/def-boto3` | `def` で boto3 を呼ぶ | Starlette がスレッドプールに切り出すので並行に返る。`/ping` は待たされない |
| `/async-to-thread` | `async def` + `asyncio.to_thread()` | 同上。同期処理だけをスレッドへ逃がす（色分け問題の回避策） |
| `/async-aioboto3` | `async def` + aioboto3 | `await` 中はループに制御が戻るので並行に返る |

```python
@app.get("/async-boto3")
async def async_boto3():
    # NG: async def の中で同期I/O。イベントループ全体が止まる
    return receive_sync()

@app.get("/def-boto3")
def def_boto3():
    # def なので FastAPI(Starlette) がスレッドプールに切り出してくれる
    return receive_sync()

@app.get("/async-to-thread")
async def async_to_thread():
    # async def のまま、同期処理だけをスレッドへ逃がす
    return await asyncio.to_thread(receive_sync)

@app.get("/async-aioboto3")
async def async_aioboto3():
    # 非同期対応ライブラリを使う。await の間はイベントループに制御が戻る
    async with aioboto3.Session().client("sqs") as sqs:
        return await sqs.receive_message(QueueUrl=url, WaitTimeSeconds=5)
```

## 使い方

Python 3.12 と uv はコンテナ側に入っているので、ローカルには Docker だけあればよい。

```bash
# LocalStack（SQS）とアプリを起動。初回はイメージのビルドで数分かかる
docker compose up -d --build

# 動作確認
curl -s localhost:8000/ping
curl -s localhost:8000/def-boto3     # 5 秒後に返る

# 負荷をかけて計測。/ping が待たされるかどうかを見る
docker compose run --rm load async-boto3
docker compose run --rm load def-boto3
docker compose run --rm load async-to-thread
docker compose run --rm load async-aioboto3

# asyncio のデバッグモードで起動し直す（イベントループを 0.1 秒以上塞ぐと警告が出る）
ASYNCIO_DEBUG=1 docker compose up -d app
curl -s localhost:8000/async-boto3
docker compose logs app | grep "took"
```

`load` は重いエンドポイントに 3 本並行でリクエストを投げ、その間 0.2 秒ごとに `/ping` を叩いて応答時間を記録する（`scripts/load.py`）。
`--concurrency 5` などで本数を変えられる。uvicorn は 1 ワーカーなので、イベントループも 1 本。

## 結果 1: 負荷テスト

Docker Desktop on Mac、uvicorn 1 ワーカー、`WAIT_SECONDS=5`、並行 3 本。

| エンドポイント | 3 本の所要時間 | /ping の最大応答時間 | boto3 が動いたスレッド |
|---|---|---|---|
| `/async-boto3` | 5.0 / 15.0 / 15.0 秒 | **14.8 秒** | MainThread |
| `/def-boto3` | 5.0 / 5.0 / 5.0 秒 | 6.4 ミリ秒 | AnyIO worker thread |
| `/async-to-thread` | 5.2 / 5.2 / 5.2 秒 | 4.6 ミリ秒 | ThreadPoolExecutor |
| `/async-aioboto3` | 5.1 / 5.1 / 5.1 秒 | 2.7 ミリ秒 | MainThread |

- `/async-boto3` だけ 3 本が直列に処理され、15 秒の間に `/ping` は 2 回しか返ってこない。イベントループが止まっている
- `/def-boto3` は Starlette が自動でスレッドプール（AnyIO worker thread）に切り出しているので、3 本が並行に返り `/ping` は影響を受けない
- `/async-to-thread` は asyncio 側のデフォルト executor（ThreadPoolExecutor）で動く。挙動は `def` と同じだが、逃がす範囲を自分で選べる
- `/async-aioboto3` は MainThread のまま 3 本が並行に返る。`await` の間はイベントループに制御が戻っている

<details><summary>load の生出力</summary>

```
=== /async-boto3  concurrency=3 ===
 #   elapsed  status  thread
 1     5.010     200  MainThread
 2    15.018     200  MainThread
 3    15.017     200  MainThread
heavy requests total wall time : 15.019 s
/ping samples                  : 2
/ping p50                      : 7408.6 ms
/ping max                      : 14814.6 ms

=== /def-boto3  concurrency=3 ===
 1     5.010     200  AnyIO worker thread
 2     5.009     200  AnyIO worker thread
 3     5.009     200  AnyIO worker thread
heavy requests total wall time : 5.010 s
/ping samples                  : 25
/ping p50                      : 1.5 ms
/ping max                      : 6.4 ms

=== /async-to-thread  concurrency=3 ===
 1     5.184     200  ThreadPoolExecutor-0_0
 2     5.181     200  ThreadPoolExecutor-0_1
 3     5.183     200  ThreadPoolExecutor-0_2
/ping p50                      : 1.5 ms
/ping max                      : 4.6 ms

=== /async-aioboto3  concurrency=3 ===
 1     5.118     200  MainThread
 2     5.118     200  MainThread
 3     5.118     200  MainThread
/ping samples                  : 26
/ping p50                      : 1.5 ms
/ping max                      : 2.7 ms
```
</details>

## 結果 2: asyncio デバッグモードでの検出

`ASYNCIO_DEBUG=1`（環境変数 `PYTHONASYNCIODEBUG=1`）で `/async-boto3` を 1 回叩いたときのアプリログ。

```
Executing <Task finished name='Task-3' coro=<RequestResponseCycle.run_asgi() done, ...> took 5.041 seconds
```

イベントループを 0.1 秒以上塞いだコールバックが警告される。ローカルで気づく手段としては最も手軽。
LT で「本番では使えない（出力量とオーバーヘッド）」と言われていたとおり、開発中に一度オンにして眺める道具。

## 結果 3: eBPF（blocking-io-check-py）での検出

詳細な手順・パッチの内訳・生ログは **[`ebpf/README.md`](ebpf/README.md)** と `ebpf/results/`。ここでは要点だけ。

### 動かせた環境

Mac 上の Colima で立てた **x86_64 の Ubuntu 24.04 VM**（QEMU エミュレーション）。

動かなかった環境と理由:

| 環境 | 結果 |
|---|---|
| Docker Desktop の VM（linuxkit 6.10, arm64） | カーネルヘッダが無く BCC がコンパイルできない（`CONFIG_IKHEADERS` 無効） |
| Claude Code on the web の VM（Firecracker 6.18, x86_64） | root と CAP_BPF はあるが、カスタムカーネルでヘッダの入手経路が無い |
| Colima の arm64 Ubuntu VM | arm64 には `epoll_wait`/`poll`/`select` の syscall が無い。名前を差し替えても libc への uretprobe 装着が失敗 |

### ツールは無改造では動かず、3 点の修正が必要だった（`ebpf/blocking-io-check-py.patch`）

1. **Ubuntu 24.04 の BCC 0.29.1 で `BPF_LRU_HASH` がコンパイルエラー**。x86_64 / arm64 とも同じ。`BPF_TABLE("lru_hash", ...)` の明示形に書き換えると通る
2. **タイムアウト付きソケットの `poll` を `IDLE`（ループが暇なだけ）に分類していて、boto3 のブロッキングを見逃す**。
   無改造では `/async-boto3` を叩いても **0 stalls**。イベントを見ると `poll dur=5033ms tid=<ループスレッド> verdict=IDLE`。
   botocore はソケットに timeout を設定し、CPython は timeout 付きソケットを内部で O_NONBLOCK にして `poll()` で待つため、5 秒の待ちは `recvfrom` ではなく `poll` に現れる。
   ループスレッド上の `poll`/`select` が閾値を超えたら `STALL` にするよう変更
3. **uvloop（`uvicorn[standard]` の既定）だとループスレッドを識別できない**。ツールは `epoll_wait` を呼んだスレッドをループとみなすが、uvloop は `epoll_pwait` を使う（`/proc/<pid>/syscall` で 281 を確認）。`epoll_pwait`/`epoll_pwait2` にも同じ probe を追加

### 修正後の結果

| エンドポイント | ツールの判定 | /ping max |
|---|---|---|
| `/async-boto3` | **STALL × 3**（`poll` 5019.8 / 5020.4 / 5018.0 ms、ループスレッド） | 15.0 秒 |
| `/def-boto3` | 0 stalls | 70.9 ms |
| `/async-to-thread` | 0 stalls | 47.9 ms |
| `/async-aioboto3` | 0 stalls | 2.1 秒（エミュレーション下の揺れ） |

```
08:57:21.755 pid=  3071 comm=uvicorn  fd=-1  op=poll  dur=  5019.849ms ret=1  flags=?---  verdict=STALL
08:57:26.814 pid=  3071 comm=uvicorn  fd=-1  op=poll  dur=  5020.423ms ret=1  flags=?---  verdict=STALL
08:57:31.860 pid=  3071 comm=uvicorn  fd=-1  op=poll  dur=  5017.967ms ret=1  flags=?---  verdict=STALL
=== summary (3 events, 3 blocking, 3 stalls, 40.1s) ===
comm     pid   op    peer  count  block  stall     total       max       p50       p95       p99
uvicorn  3071  poll  -         3      3      3  15058.24  5020.423  5019.849  5020.423  5020.423
```

セッションで聞いた「`async def` の中の同期 I/O がイベントループを止める」が、eBPF からも「ループスレッド上で `poll` が 5 秒待った」として観測できた。
一方で、無改造のツールの結果を信じていたら「boto3 は問題なし」と結論づけていたことになる。

## ハマったこと

- **LocalStack の `latest` がライセンストークン必須になった**（2026.x）。無料で動く `4.7.0` に固定した
- **LocalStack の初期化スクリプト（awslocal）は us-east-1 にキューを作る**。アプリ側を ap-northeast-1 にしていて `QueueDoesNotExist` になった。SQS のキューはリージョン単位。アプリ側は `create_queue`（冪等）で URL を取り、リージョンも揃えた
- **「どのスレッドで動いたか」をハンドラの中で取ると、`asyncio.to_thread` 版でも MainThread になる**。`await` から戻った時点ではもうイベントループのスレッドにいる。boto3 を呼んだ関数の中でスレッド名を取る必要があった
- eBPF 側のハマりどころは [`ebpf/README.md`](ebpf/README.md) にまとめた

## 補足

- `docker compose` で起動する uvicorn はワーカー 1 プロセスなので、イベントループも 1 つ。本番で複数ワーカーにしていると、
  1 つのループが止まっても他のワーカーが受けるので症状が薄まって見える。再現を分かりやすくするため、意図的に 1 ワーカーにしている
- Colima の VM から Mac 側への Docker ソケット転送（`docker --context colima-x86`）は失敗したので、VM 内で `sudo docker compose` を使った
