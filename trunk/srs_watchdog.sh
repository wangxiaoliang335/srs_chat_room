#!/bin/bash
# SRS看门狗脚本 - 自动重启崩溃的SRS进程

SRS_BIN="/root/srs-project/srs/trunk/objs/srs"
SRS_CONF="/root/srs-project/srs/trunk/conf/rtc_with_translation.conf"
SRS_PID_FILE="/root/srs-project/srs/trunk/objs/srs.pid"
LOG_FILE="/root/srs-project/srs/trunk/objs/srs_watchdog.log"

echo "[$(date)] SRS Watchdog started" >> $LOG_FILE

while true; do
    # 检查SRS是否在运行
    if pgrep -f "objs/srs.*rtc_with_translation" > /dev/null; then
        # 检查进程是否真正存活
        SRS_PID=$(pgrep -f "objs/srs.*rtc_with_translation" | head -1)
        if ! kill -0 $SRS_PID 2>/dev/null; then
            echo "[$(date)] SRS process $SRS_PID not responding, restarting..." >> $LOG_FILE
            pkill -9 -f "objs/srs.*rtc_with_translation" 2>/dev/null
            sleep 1
        fi
    else
        echo "[$(date)] SRS not running, starting..." >> $LOG_FILE
        cd /root/srs-project/srs/trunk
        rm -f $SRS_PID_FILE objs/srs.log
        nohup ./objs/srs -c $SRS_CONF >> objs/srs.log 2>&1 &
        sleep 3
        
        if pgrep -f "objs/srs.*rtc_with_translation" > /dev/null; then
            echo "[$(date)] SRS started successfully" >> $LOG_FILE
        else
            echo "[$(date)] Failed to start SRS" >> $LOG_FILE
        fi
    fi
    
    sleep 5
done
