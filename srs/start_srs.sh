#!/bin/bash
# SRS Auto Start Script

SRS_DIR="/root/srs-project/srs/trunk"
SRS_CONF="/root/srs-project/srs/conf/rtc_with_translation.conf"
SRS_BIN="objs/srs"

cd $SRS_DIR

# 检查进程是否已运行
if pgrep -f "$SRS_BIN" > /dev/null 2>&1; then
    echo "SRS is already running"
    exit 0
fi

echo "Starting SRS with $SRS_CONF..."
nohup ./$SRS_BIN -c $SRS_CONF > objs/srs.log 2>&1 &
sleep 3

if ss -tlnp 2>/dev/null | grep -q ":1985"; then
    echo "SRS started successfully"
    exit 0
else
    echo "SRS failed to start, check logs"
    exit 1
fi
