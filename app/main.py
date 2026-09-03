"""FastAPI の並行処理モデルを手元で確かめるための最小アプリ。

すべてのエンドポイントは「空の SQS キューに対して WAIT_SECONDS 秒のロングポーリングをする」という
同じ処理を、書き方だけ変えて実装している。ロングポーリングは boto3 の receive_message が
WAIT_SECONDS 秒間ブロックするので、同期 I/O のブロッキング処理としてちょうどよい。

  /ping             何もしない async def。イベントループが生きているかを見るカナリア
  /async-boto3      async def の中で boto3 を直接呼ぶ  → イベントループが止まる（NG パターン）
  /def-boto3        def で boto3 を呼ぶ                → スレッドプールで動くのでループは止まらない
  /async-to-thread  async def の中で asyncio.to_thread → 明示的にスレッドへ逃がす
  /async-aioboto3   async def の中で aioboto3          → 非同期対応ライブラリを使う
"""

import asyncio
import os
import threading
import time
from functools import lru_cache

import aioboto3
import boto3
from fastapi import FastAPI

QUEUE_NAME = os.environ.get("QUEUE_NAME", "lab-queue")
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "5"))

app = FastAPI(title="fastapi-blocking-io-lab")


@lru_cache(maxsize=1)
def sqs_client():
    return boto3.client("sqs")


@lru_cache(maxsize=1)
def queue_url() -> str:
    # create_queue は同名・同属性なら冪等なので、存在確認と URL 取得を兼ねる
    return sqs_client().create_queue(QueueName=QUEUE_NAME)["QueueUrl"]


def receive_sync() -> tuple[int, str]:
    """boto3 でロングポーリング。キューが空なら WAIT_SECONDS 秒ブロックする。

    戻り値の 2 つ目は、実際に boto3 を呼んだスレッド名（どこでブロックしたかを見るため）。
    """
    resp = sqs_client().receive_message(QueueUrl=queue_url(), WaitTimeSeconds=WAIT_SECONDS, MaxNumberOfMessages=1)
    return len(resp.get("Messages", [])), threading.current_thread().name


def result(started: float, messages: int, thread: str) -> dict:
    return {"elapsed_sec": round(time.perf_counter() - started, 3), "messages": messages, "thread": thread}


@app.get("/ping")
async def ping() -> dict:
    return {"pong": True, "thread": threading.current_thread().name}


@app.get("/async-boto3")
async def async_boto3() -> dict:
    """NG: async def の中で同期 I/O。イベントループ全体が WAIT_SECONDS 秒止まる。"""
    started = time.perf_counter()
    return result(started, *receive_sync())


@app.get("/def-boto3")
def def_boto3() -> dict:
    """def なので FastAPI(Starlette) がスレッドプールに切り出してくれる。"""
    started = time.perf_counter()
    return result(started, *receive_sync())


@app.get("/async-to-thread")
async def async_to_thread() -> dict:
    """async def のまま、同期処理だけをスレッドへ逃がす（色分け問題の回避策）。"""
    started = time.perf_counter()
    return result(started, *await asyncio.to_thread(receive_sync))


@app.get("/async-aioboto3")
async def async_aioboto3() -> dict:
    """非同期対応ライブラリを使う。await の間はイベントループに制御が戻る。"""
    started = time.perf_counter()
    session = aioboto3.Session()
    async with session.client("sqs") as sqs:
        resp = await sqs.receive_message(QueueUrl=queue_url(), WaitTimeSeconds=WAIT_SECONDS, MaxNumberOfMessages=1)
    return result(started, len(resp.get("Messages", [])), threading.current_thread().name)
