#!/bin/bash
# LocalStack 起動完了後に一度だけ実行される（/etc/localstack/init/ready.d）
awslocal sqs create-queue --queue-name lab-queue
echo "created queue: lab-queue"
