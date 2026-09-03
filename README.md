# fastapi-blocking-io-lab

PyCon JP 2026 のセッション「[FastAPI の並行処理モデルを完全に理解する](https://2026.pycon.jp/ja/talks/HHLXYU)」で聞いた
「`async def` の中で boto3 のような同期 I/O を呼ぶとイベントループ全体が止まる」を手元で再現し、
LT「[そのasync、止まってない？](https://speakerdeck.com/ryuichi1208/so-async-toma-tenai-teppan-ibento-rupu-burokkingu-shori-kenshutsujutsu)」で紹介された
eBPF ツール [blocking-io-check-py](https://github.com/ryuichi1208/blocking-io-check-py) で検出するための検証リポジトリ。

## 何を再現するか

同期 I/O のブロッキング処理には、**空の SQS キューへのロングポーリング**（`receive_message(WaitTimeSeconds=5)`）を使う。
キューが空なので boto3 の呼び出しがきっちり 5 秒ブロックする。SQS は LocalStack で用意する。

同じ処理を書き方だけ変えた 4 つのエンドポイントと、カナリア用の `/ping` を用意している（`app/main.py`）。

| エンドポイント | 書き方 | 期待する挙動 |
|---|---|---|
| `/async-boto3` | `async def` の中で boto3 を直接呼ぶ | **イベントループが止まる**。並行リクエストは直列に返り、`/ping` も数秒待たされる |
| `/def-boto3` | `def` で boto3 を呼ぶ | スレッドプールで動くので並行に返る。`/ping` は待たされない |
| `/async-to-thread` | `async def` + `asyncio.to_thread()` | 同上。色分け問題の回避策 |
| `/async-aioboto3` | `async def` + aioboto3 | `await` 中はループに制御が戻るので並行に返る |

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
```

`load` は重いエンドポイントに 3 本並行でリクエストを投げ、その間 0.2 秒ごとに `/ping` を叩いて応答時間を記録する。
`--concurrency 5` などで本数を変えられる。

## 実験の手順（ブログ用）

1. `async-boto3` は 3 本が 5 秒・10 秒・15 秒と直列に返り、`/ping` の max が数秒になることを確認する
2. `def-boto3` / `async-to-thread` / `async-aioboto3` は 3 本とも約 5 秒で返り、`/ping` は数 ms のままであることを確認する
3. `def-boto3` と `async-to-thread` の `thread` が `MainThread` ではなく `asyncio_N` / `AnyIO worker thread` になっていることを見る
4. `ASYNCIO_DEBUG=1 docker compose up -d app` で asyncio デバッグモードにし、`async-boto3` を叩いたときに
   `Executing <Task ...> took 5.0xx seconds` の警告がアプリのログ（`docker compose logs app`）に出ることを見る
5. eBPF で「どの syscall が、どのスレッドで、何秒ブロックしたか」を外から観測する（`ebpf/README.md`）
6. 分かったこと・ハマったことをまとめる

## 補足: uvicorn のワーカー数

`docker compose` で起動する uvicorn はワーカー 1 プロセスなので、イベントループも 1 つ。
本番で複数ワーカーにしていると、1 つのループが止まっても他のワーカーが受けるので症状が薄まって見える。
再現を分かりやすくするため、この検証では意図的に 1 ワーカーにしている。
