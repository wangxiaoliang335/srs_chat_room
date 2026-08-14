#!/bin/bash
# =====================================================
# SRS 进程监控 + 自动重启脚本
# - 进程挂掉自动拉起
# - 检测到 core dump 时自动 gdb 记录 stack
# - 配合 systemd 或 nohup 后台运行
# =====================================================

SRS_BIN="/root/srs-project/srs/trunk/objs/srs"
SRS_CONF="/root/srs-project/srs/conf/rtc_with_translation.conf"
SRS_DIR="/root/srs-project/srs"
LOG_DIR="${SRS_DIR}/logs"
CRASH_DIR="${SRS_DIR}/crashes"
PID_FILE="${SRS_DIR}/.srs.pid"
RESTART_LOG="${LOG_DIR}/watchdog.log"
SRS_LOG="${LOG_DIR}/srs.log"

mkdir -p "$LOG_DIR" "$CRASH_DIR"

# ulimit 让 core dump 真实落盘
ulimit -c unlimited
echo "/root/srs-project/srs/core-%e-%p-%t" > /proc/sys/kernel/core_pattern 2>/dev/null

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$RESTART_LOG"
}

run_gdb_on_core() {
    local core_file="$1"
    local stamp
    stamp=$(date '+%Y%m%d_%H%M%S')
    local dump="${CRASH_DIR}/crash_${stamp}.log"

    log "[CRASH] 捕获到 core: $core_file，正在用 gdb 记录 stack..."

    if ! command -v gdb >/dev/null 2>&1; then
        log "[CRASH] gdb 未安装，跳过 stack 分析"
        return
    fi

    gdb -batch \
        -ex "set pagination off" \
        -ex "thread apply all bt full" \
        -ex "info registers" \
        -ex "info threads" \
        -ex "quit" \
        "$SRS_BIN" "$core_file" > "$dump" 2>&1

    log "[CRASH] stack 已保存到: $dump"
    # 把 core 移到 crashes 目录归档
    mv "$core_file" "${CRASH_DIR}/$(basename "$core_file")" 2>/dev/null
}

cleanup_old_cores() {
    # 启动前清理目录里残留的 core
    find "$SRS_DIR" -maxdepth 1 -name 'core-*' -type f 2>/dev/null | while read -r f; do
        log "[CLEANUP] 启动前发现残留 core: $f"
        run_gdb_on_core "$f"
    done
}

start_srs() {
    cleanup_old_cores

    log "[START] 启动 SRS: $SRS_BIN -c $SRS_CONF"
    cd "$SRS_DIR" || exit 1
    nohup "$SRS_BIN" -c "$SRS_CONF" >> "$SRS_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    log "[START] SRS PID=$pid"
}

is_running() {
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

stop_srs() {
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null)
    [ -z "$pid" ] && return
    log "[STOP] 停止 SRS PID=$pid"
    kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || { rm -f "$PID_FILE"; return; }
        sleep 1
    done
    kill -KILL "$pid" 2>/dev/null
    rm -f "$PID_FILE"
}

case "${1:-start}" in
    start)
        if is_running; then
            log "[START] SRS 已在运行"
        else
            start_srs
        fi
        ;;
    stop)
        stop_srs
        ;;
    restart)
        stop_srs
        sleep 1
        start_srs
        ;;
    status)
        if is_running; then
            pid=$(cat "$PID_FILE")
            echo "SRS 正在运行，PID=$pid"
        else
            echo "SRS 未运行"
            exit 1
        fi
        ;;
    watch)
        log "[WATCH] 进入监控模式"
        while true; do
            if ! is_running; then
                log "[WATCH] 检测到 SRS 进程退出，扫描 core..."
                # 等文件系统刷新
                sleep 1
                # 找最新生成的 core
                latest_core=$(find "$SRS_DIR" -maxdepth 1 -name 'core-*' -type f -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | awk '{print $2}')
                if [ -n "$latest_core" ]; then
                    run_gdb_on_core "$latest_core"
                else
                    log "[WATCH] 未发现 core dump（可能是 SIGINT 等正常退出）"
                fi
                log "[WATCH] 重新启动 SRS..."
                start_srs
            fi
            sleep 5
        done
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|watch}"
        exit 1
        ;;
esac
