# eBPF でブロッキング I/O を検出する

[blocking-io-check-py](https://github.com/ryuichi1208/blocking-io-check-py) は eBPF で対象プロセスの I/O syscall の所要時間を計測し、
「イベントループのスレッド上で、ブロッキングな fd に対して、実際に待った I/O」だけを `STALL` と判定するツール。

## 動かす環境

eBPF は Linux カーネルの機能なので、**macOS 上では直接動かない**。

Docker Desktop の Linux VM 内で動かすことも試したが（2026-09-04、Docker Desktop 28.4 / kernel 6.10.14-linuxkit arm64）、
BCC が `Unable to find kernel headers` で失敗する。BPF syscall・kprobe・uprobe・BTF は有効なのに、
`CONFIG_IKHEADERS` が無効で `/lib/modules/$(uname -r)/build` も無いため、BCC が BPF プログラムをコンパイルできない。
`--privileged --pid=host` にしても変わらない。カーネル側の問題なのでコンテナ側では回避できない。

そのため次のどちらかを推奨する。

- **EC2（Ubuntu 24.04 など）**に Docker を入れて、このリポジトリを clone して動かす
- **Mac 上の Linux VM**（[Multipass](https://multipass.run/) や [Lima](https://lima-vm.io/)）に Docker と BCC を入れて動かす

いずれも「VM の中で `docker compose up` してアプリを動かし、同じ VM の中で eBPF ツールを root で動かす」形になる。
コンテナ内のプロセスも VM のカーネルから見えるので、ホスト側の `pgrep` で PID が取れる。

## 手順（Ubuntu）

```bash
# 1. BCC を入れる（pip の bcc は別物なので入れないこと）
sudo apt update
sudo apt install -y bpfcc-tools python3-bpfcc linux-headers-$(uname -r)
sudo python3 -c "from bcc import BPF; print('ok')"

# 2. ツールを入れる
git clone https://github.com/ryuichi1208/blocking-io-check-py
cd blocking-io-check-py
uv venv --system-site-packages
uv sync
sudo .venv/bin/blocking-io-check --version

# 3. アプリを起動（このリポジトリ側）
docker compose up -d --build

# 4. uvicorn の PID を取ってトレース開始（30 秒）
APP_PID=$(pgrep -f "uvicorn app.main:app" | head -1)
sudo .venv/bin/blocking-io-check --pid "$APP_PID" -d 30 --hide-dns --summary

# 5. 別ターミナルで負荷をかける
docker compose run --rm load async-boto3
```

## 見るポイント

- `/async-boto3` を叩いたとき: `op=recvfrom`（または `read`）に `dur≈5000ms`、`flags=B---`、`verdict=STALL` の行が出るはず。
  peer は LocalStack の 4566 番ポート
- `/def-boto3` `/async-to-thread` を叩いたとき: 同じ 5 秒の recvfrom は起きているが、イベントループのスレッドではないので
  `STALL` にはならないはず（`--all-threads` を付けると見える）
- `/async-aioboto3` を叩いたとき: fd がノンブロッキングで epoll に登録されているので、そもそも blocking 扱いにならないはず

うまく取れたら、summary の表をそのままブログに貼れる。
