# eBPF でブロッキング I/O を検出する

[blocking-io-check-py](https://github.com/ryuichi1208/blocking-io-check-py) は eBPF で対象プロセスの I/O syscall の所要時間を計測し、
「イベントループのスレッド上で、ブロッキングな fd に対して、実際に待った I/O」だけを `STALL` と判定するツール。

## 結論（2026-09-04 時点）

- **動かせた環境**: Mac 上の Colima で立てた **x86_64 の Ubuntu 24.04 VM**（QEMU エミュレーション）
- **動かなかった環境**: Docker Desktop の VM、Claude Code on the web の VM（いずれもカーネルヘッダが無く BCC がコンパイルできない）、
  Colima の arm64 VM（BCC の uretprobe 装着が失敗する）
- **ツールは無改造では動かず**、`ebpf/blocking-io-check-py.patch` の修正が必要だった（内訳は後述）
- **修正後の結果**: `/async-boto3` だけ `poll` 約5秒 × 3 が `STALL`、他の 3 エンドポイントは 0 件。`ebpf/results/` に生ログ

## 手順（Colima x86_64）

```bash
# Mac 側
brew install colima qemu lima-additional-guestagents
colima start --profile x86 --arch x86_64 --vm-type qemu --cpu 4 --memory 6 --disk 30 --activate=false
colima ssh --profile x86

# VM 側（Ubuntu 24.04）
sudo apt-get update
sudo apt-get install -y bpfcc-tools python3-bpfcc linux-headers-$(uname -r) git
sudo python3 -c "from bcc import BPF; print('ok')"
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/ryuichi1208/blocking-io-check-py && cd blocking-io-check-py
git apply /Users/<you>/.../fastapi-blocking-io-lab/ebpf/blocking-io-check-py.patch   # ホームは VM にマウントされている
uv venv --system-site-packages && uv sync
sudo .venv/bin/blocking-io-check --version

# アプリ（VM 内の Docker で起動。Mac 側の docker context 転送は失敗したので VM 内で実行）
cd /Users/<you>/.../fastapi-blocking-io-lab
sudo docker compose up -d --build

# 計測（別ターミナルで負荷をかける）
PID=$(pgrep -f "uvicorn app.main:app" | head -1)
sudo ~/blocking-io-check-py/.venv/bin/blocking-io-check --pid $PID -d 40 --hide-dns --hide-netlink --only-blocking --summary
sudo docker compose run --rm load async-boto3
```

## ツールに必要だった修正（`blocking-io-check-py.patch`）

### 1. Ubuntu 24.04 の BCC 0.29.1 で `BPF_LRU_HASH` がコンパイルエラー

```
/virtual/main.c:106:27: error: expected identifier
  106 | BPF_LRU_HASH(fd_nonblock, struct key_t, u8, 65536);
```

引数の数や構造体名に関係なく `BPF_LRU_HASH(...)` が通らない。同じ内容を `BPF_TABLE("lru_hash", key, leaf, name, size)` と
明示的に書くと通る。x86_64 / arm64 の両方で同じ。README の apt 手順どおりに入れた BCC で起きるので、環境依存のバグ。

### 2. タイムアウト付きソケットの `poll` が `IDLE` 扱いになり、boto3 のブロッキングを見逃す

無改造のツールで `/async-boto3` を叩くと **0 stalls**。`--min-latency 100` でイベントを見ると、

```
uvicorn  poll  dur=5033.570ms  tid=3071(=pid, ループスレッド)  verdict=IDLE
```

botocore はソケットに timeout を設定する。CPython は timeout 付きソケットを内部で O_NONBLOCK にし、`poll()` で読めるまで待ってから
`recv` する。つまり 5 秒の待ちは `recvfrom` ではなく `poll` に現れる。ツールは `epoll_wait` / `poll` / `select` を「待機系」として
一括で `IDLE`（ループが暇なだけ）に分類するため、ループスレッド上の `poll` 5 秒待ちが正常扱いになる。

パッチでは `epoll_wait` だけを `IDLE` にし、ループスレッド上の `poll` / `select` が閾値を超えたら `STALL` にした。
あわせて `poll` を呼んだスレッドを「ループスレッド」とみなす処理を外した（ワーカースレッドの poll が誤ってループ扱いになる）。

### 3. uvloop（`uvicorn[standard]` の既定）だとループスレッドを識別できない

ツールは `epoll_wait` を呼んだスレッドをループスレッドとみなすが、uvloop（libuv）は `epoll_pwait` を使う。
`cat /proc/<pid>/syscall` でメインスレッドが 281（epoll_pwait）で待っているのを確認した。
パッチでは `epoll_pwait` / `epoll_pwait2` にも同じ probe を足した。

### arm64 でさらに必要だったこと（結局 x86_64 に切り替えた）

- arm64 のカーネルには `epoll_wait` / `poll` / `select` の syscall が無く（`epoll_pwait` / `ppoll` / `pselect6` のみ）、
  tracepoint 名の差し替えが必要
- それでも libc の `fcntl` への uretprobe 装着が `perf_event_open: Invalid argument` で失敗し、先に進めなかった

## 修正後の結果（x86_64 VM、uvicorn 1 ワーカー）

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
