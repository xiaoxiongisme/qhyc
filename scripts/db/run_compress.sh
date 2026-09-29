#!/bin/sh
# 低峰窗口触发存量 chunk 压缩（由 crontab 调用）。详见 compress_existing_chunks.sql。
docker exec -i qhyc-timescaledb psql -U futures futures -f - < /Docker/qhyc/scripts/db/compress_existing_chunks.sql >> /var/log/qhyc_compress.log 2>&1
