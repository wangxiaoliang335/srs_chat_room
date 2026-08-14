#!/bin/bash
# ============================================================================
# 一键启动 / 停止 / 重启 所有服务
# ----------------------------------------------------------------------------
# 服务清单：
#   1. SRS 流媒体服务器            1990/8080/1985/1935
#   2. FastAPI 主服务 (server_fastapi.py)   8085   HTTP + WebSocket
#   3. 翻译文本推送服务 (translation_text_publisher.py)   8086/8087
#
# 用法：
#   ./restart_all.sh           # 等价于 start
#   ./restart_all.sh start     # 杀掉旧进程后启动所有服务
#   ./restart_all.sh stop      # 停止所有服务
#   ./restart_all.sh restart   # stop + start
#   ./restart_all.sh status    # 查看服务状态
#   ./restart_all.sh logs      # 实时查看日志 (tail -f)
#   ./restart_all.sh help      # 帮助
# ============================================================================

set -u

# ---------------------------------------------------------------------------
# 路径与基础配置
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/logs"
PID_DIR="$SCRIPT_DIR/pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

# Python 解释器（优先使用系统 python3，找不到再回退到 3.11）
PYTHON_BIN="$(command -v python3 2>/dev/null || echo /usr/local/python3.11-ssl/bin/python3.11)"

# Python 模块搜索路径
export PYTHONPATH=/usr/local/python3.11-ssl/lib/python3.11/site-packages:/usr/local/python3.11/lib/python3.11/site-packages:${PYTHONPATH:-}
export LD_LIBRARY_PATH=/usr/local/ffmpeg/lib:${LD_LIBRARY_PATH:-}
export PATH=/usr/local/ffmpeg/bin:/usr/local/python3.11/bin:$PATH

# 加载 .env 中的环境变量
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.env"
    set +a
fi

# ---------------------------------------------------------------------------
# 颜色输出
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${BLUE}[STEP]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
log_fail()  { echo -e "${RED}[FAIL]${NC}  $*"; }

# ---------------------------------------------------------------------------
# 端口与进程管理工具
# ---------------------------------------------------------------------------

# 检查端口是否被占用
check_port() {
    local port="$1"
    if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}$"; then
        return 0   # 占用
    fi
    if netstat -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}$"; then
        return 0
    fi
    return 1   # 空闲
}

# 等待端口被监听（最多 max_wait 秒）
wait_port() {
    local port="$1"
    local max_wait="${2:-15}"
    local i=0
    while [ $i -lt "$max_wait" ]; do
        if check_port "$port"; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# 通用：根据关键字找到 PID
get_pids_by_keyword() {
    local keyword="$1"
    pgrep -f "$keyword" 2>/dev/null
}

# 强制杀进程（先 TERM，再 KILL）
kill_pids() {
    local pids="$1"
    [ -z "$pids" ] && return 0
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    # 再杀一次存活的
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
}

# 杀掉指定关键字的所有进程
kill_by_keyword() {
    local keyword="$1"
    local pids
    pids="$(get_pids_by_keyword "$keyword")"
    if [ -n "$pids" ]; then
        log_info "杀掉进程 (keyword=$keyword): $pids"
        kill_pids "$pids"
    fi
}

# 杀掉占用指定端口的进程
kill_by_port() {
    local port="$1"
    local pids
    pids="$(ss -tlnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $0}' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)"
    if [ -z "$pids" ]; then
        pids="$(lsof -ti :"$port" 2>/dev/null || true)"
    fi
    if [ -z "$pids" ]; then
        pids="$(fuser -n tcp "$port" 2>/dev/null || true)"
    fi
    if [ -n "$pids" ]; then
        log_info "杀掉占用端口 $port 的进程: $pids"
        kill_pids "$pids"
    fi
}

# 通过 PID 文件清理
clean_pid_file() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid
        pid="$(cat "$pid_file" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pid_file"
    fi
}

# ---------------------------------------------------------------------------
# 停止所有服务
# ---------------------------------------------------------------------------
stop_all() {
    log_step "================ 停止所有服务 ================"

    # 1. 先按 PID 文件优雅停止
    for pid_file in "$PID_DIR"/*.pid; do
        [ -f "$pid_file" ] || continue
        clean_pid_file "$pid_file"
    done

    # 2. 再按关键字/端口兜底清理
    log_info "按关键字清理进程..."
    kill_by_keyword "server_fastapi.py"
    kill_by_keyword "translation_text_publisher.py"
    kill_by_keyword "callback_server.py"
    kill_by_keyword "notice_server.py"
    kill_by_keyword "objs/srs"
    kill_by_keyword "nginx.*nginx_cors"

    log_info "按端口清理进程..."
    kill_by_port 8085   # FastAPI
    kill_by_port 8086   # WS publisher
    kill_by_port 8087   # HTTP publisher
    kill_by_port 8090   # notice_socket (2026-08-13 文档 §1.1)
    kill_by_port 1985   # SRS API
    kill_by_port 8080   # SRS HTTP
    kill_by_port 1990   # SRS RTC
    kill_by_port 1935   # SRS RTMP

    sleep 2

    # 3. 再次确认
    local leftover=""
    leftover+="$(get_pids_by_keyword 'server_fastapi.py')\n"
    leftover+="$(get_pids_by_keyword 'translation_text_publisher.py')\n"
    leftover+="$(get_pids_by_keyword 'objs/srs')\n"
    leftover="$(echo -e "$leftover" | grep -v '^$' | sort -u | tr '\n' ' ')"
    if [ -n "$leftover" ]; then
        log_warn "仍有进程残留，强制 KILL: $leftover"
        # shellcheck disable=SC2086
        kill -9 $leftover 2>/dev/null || true
        sleep 1
    fi

    log_ok "所有服务已停止"
}

# ---------------------------------------------------------------------------
# 启动 SRS 流媒体服务器
# ---------------------------------------------------------------------------
start_srs() {
    log_step "启动 SRS 流媒体服务器..."

    local srs_conf="$SCRIPT_DIR/conf/rtc_with_translation.conf"
    if [ ! -f "$srs_conf" ]; then
        log_warn "$srs_conf 不存在，尝试 trunk/conf/rtc_meeting.conf"
        srs_conf="$SCRIPT_DIR/trunk/conf/rtc_meeting.conf"
    fi
    if [ ! -f "$srs_conf" ]; then
        log_fail "未找到 SRS 配置文件，请先生成 conf"
        return 1
    fi

    local srs_dir="$SCRIPT_DIR/trunk"
    local srs_bin="$srs_dir/objs/srs"
    if [ ! -x "$srs_bin" ]; then
        log_fail "未找到 SRS 可执行文件: $srs_bin"
        return 1
    fi

    # 已经在跑就跳过
    if get_pids_by_keyword "objs/srs -c" > /dev/null; then
        log_warn "SRS 已在运行，跳过启动"
        return 0
    fi

    cd "$srs_dir"
    nohup ./objs/srs -c "$srs_conf" > "$LOG_DIR/srs.log" 2>&1 &
    local pid=$!
    cd "$SCRIPT_DIR"

    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        echo "$pid" > "$PID_DIR/srs.pid"
        # 等待 API 端口
        if wait_port 1985 15; then
            log_ok "SRS 启动成功 (PID: $pid, API: 1985, HTTP: 8080, RTC: 1990, RTMP: 1935)"
        else
            log_warn "SRS 进程已起但 1985 端口未就绪，请查看 $LOG_DIR/srs.log"
        fi
    else
        log_fail "SRS 启动失败，请检查 $LOG_DIR/srs.log"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# 启动 FastAPI 主服务
# ---------------------------------------------------------------------------
start_fastapi() {
    log_step "启动 FastAPI 主服务 (server_fastapi.py) on :8085 ..."

    if [ ! -f "$SCRIPT_DIR/server_fastapi.py" ]; then
        log_fail "找不到 server_fastapi.py"
        return 1
    fi

    # 已经在跑就跳过
    if get_pids_by_keyword "python.*server_fastapi.py" > /dev/null; then
        log_warn "FastAPI 主服务已在运行，跳过启动"
        return 0
    fi

    # 端口被占就清理
    if check_port 8085; then
        log_warn "8085 端口已被占用，先清理"
        kill_by_port 8085
        sleep 1
    fi

    nohup "$PYTHON_BIN" "$SCRIPT_DIR/server_fastapi.py" > "$LOG_DIR/server_fastapi.log" 2>&1 &
    local pid=$!
    sleep 1

    if wait_port 8085 15; then
        echo "$pid" > "$PID_DIR/server_fastapi.pid"
        log_ok "FastAPI 主服务启动成功 (PID: $pid, :8085)"

        # 健康检查
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8085/health || echo 000)"
        if [ "$code" = "200" ]; then
            log_ok "健康检查通过 (/health -> 200)"
        else
            log_warn "健康检查返回 $code，进程已起但可能尚未就绪"
        fi
    else
        log_fail "FastAPI 启动后 8085 端口未监听，请检查 $LOG_DIR/server_fastapi.log"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# 启动翻译文本推送服务
# ---------------------------------------------------------------------------
start_text_publisher() {
    log_step "启动翻译文本推送服务 (translation_text_publisher.py) on :8086/:8087 ..."

    if [ ! -f "$SCRIPT_DIR/translation_text_publisher.py" ]; then
        log_fail "找不到 translation_text_publisher.py"
        return 1
    fi

    if get_pids_by_keyword "python.*translation_text_publisher.py" > /dev/null; then
        log_warn "翻译文本推送服务已在运行，跳过启动"
        return 0
    fi

    if check_port 8086 || check_port 8087; then
        log_warn "8086/8087 端口已被占用，先清理"
        kill_by_port 8086
        kill_by_port 8087
        sleep 1
    fi

    nohup "$PYTHON_BIN" "$SCRIPT_DIR/translation_text_publisher.py" > "$LOG_DIR/translation_text_publisher.log" 2>&1 &
    local pid=$!

    sleep 1
    if check_port 8086 || check_port 8087; then
        echo "$pid" > "$PID_DIR/translation_text_publisher.pid"
        log_ok "翻译文本推送服务启动成功 (PID: $pid, :8086/:8087)"
    else
        log_fail "翻译文本推送服务启动后端口未监听，请检查 $LOG_DIR/translation_text_publisher.log"
        return 1
    fi
}

# 2026-08-13 文档 §1.1 / §8.2: notice_socket (跨房间事件)
start_notice_server() {
    local notice_port="${NOTICE_SOCKET_PORT:-8090}"
    log_step "启动 notice_socket (notice_server.py) on :$notice_port ..."

    if [ ! -f "$SCRIPT_DIR/notice_server.py" ]; then
        log_warn "找不到 notice_server.py，跳过 notice_socket"
        return 0
    fi

    if get_pids_by_keyword "python.*notice_server.py" > /dev/null; then
        log_warn "notice_socket 已在运行，跳过启动"
        return 0
    fi

    if check_port "$notice_port"; then
        log_warn "$notice_port 端口已被占用，先清理"
        kill_by_port "$notice_port"
        sleep 1
    fi

    NOTICE_SOCKET_PORT="$notice_port" nohup "$PYTHON_BIN" "$SCRIPT_DIR/notice_server.py" > "$LOG_DIR/notice_server.log" 2>&1 &
    local pid=$!
    sleep 1
    if check_port "$notice_port"; then
        echo "$pid" > "$PID_DIR/notice_server.pid"
        log_ok "notice_socket 启动成功 (PID: $pid, :$notice_port)"
    else
        log_fail "notice_socket 启动后端口未监听，请检查 $LOG_DIR/notice_server.log"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# 启动 nginx (用于跨域, 8089)
# ---------------------------------------------------------------------------
start_nginx() {
    log_step "启动 Nginx (跨域代理 :8089) ..."

    local nginx_conf="$SCRIPT_DIR/conf/nginx_cors_for_8089.conf"
    if [ ! -f "$nginx_conf" ]; then
        log_warn "未找到 $nginx_conf，跳过 nginx 启动"
        return 0
    fi

    if get_pids_by_keyword "nginx.*nginx_cors" > /dev/null; then
        log_warn "Nginx 已在运行，跳过启动"
        return 0
    fi

    if check_port 8089; then
        log_warn "8089 端口已被占用，先清理"
        kill_by_port 8089
        sleep 1
    fi

    nginx -c "$nginx_conf" 2>>"$LOG_DIR/nginx.log"
    sleep 1
    if get_pids_by_keyword "nginx.*nginx_cors" > /dev/null; then
        log_ok "Nginx 启动成功 (:8089)"
    else
        log_warn "Nginx 启动未确认，请手动检查"
    fi
}

# ---------------------------------------------------------------------------
# 启动流程
# ---------------------------------------------------------------------------
start_all() {
    log_step "================ 启动所有服务 ================"

    # 0. 先杀掉旧进程（用户的核心要求）
    stop_all

    # 1. SRS 流媒体（最先起）
    start_srs || { log_fail "SRS 启动失败，中止后续启动"; return 1; }
    sleep 1

    # 2. FastAPI 主服务
    start_fastapi || { log_fail "FastAPI 启动失败，中止后续启动"; return 1; }
    sleep 1

    # 3. 翻译文本推送
    start_text_publisher || log_warn "翻译文本推送启动失败，但不影响主流程"

    # 4. notice_socket (2026-08-13 文档 §1.1：跨房间事件)
    start_notice_server || log_warn "notice_socket 启动失败，但不影响主流程"

    # 5. Nginx (跨域，可选)
    start_nginx || log_warn "Nginx 启动失败，但不影响主流程"

    echo
    show_status
}

# ---------------------------------------------------------------------------
# 状态展示
# ---------------------------------------------------------------------------
show_status() {
    echo
    echo "========================================"
    echo "         服务状态总览"
    echo "========================================"

    # SRS
    if get_pids_by_keyword "objs/srs -c" > /dev/null; then
        echo -e "  ${GREEN}✓${NC} SRS 流媒体服务器"
        echo "    - RTMP:    1935    HTTP-FLV: 8080    API: 1985    WebRTC: 1990"
    else
        echo -e "  ${RED}✗${NC} SRS 流媒体服务器  未运行"
    fi

    # FastAPI
    if get_pids_by_keyword "python.*server_fastapi.py" > /dev/null; then
        local pid
        pid="$(get_pids_by_keyword 'python.*server_fastapi.py' | head -1)"
        echo -e "  ${GREEN}✓${NC} FastAPI 主服务 (server_fastapi.py)  PID: $pid"
        echo "    - HTTP:     http://localhost:8085"
        echo "    - WebSocket: ws://localhost:8085/ws"
    else
        echo -e "  ${RED}✗${NC} FastAPI 主服务  未运行"
    fi

    # 翻译文本推送
    if get_pids_by_keyword "python.*translation_text_publisher.py" > /dev/null; then
        local pid
        pid="$(get_pids_by_keyword 'python.*translation_text_publisher.py' | head -1)"
        echo -e "  ${GREEN}✓${NC} 翻译文本推送服务  PID: $pid"
        echo "    - WebSocket: ws://localhost:8086"
        echo "    - HTTP:      http://localhost:8087"
    else
        echo -e "  ${YELLOW}⚠${NC} 翻译文本推送服务  未运行"
    fi

    # notice_socket 跨房间（2026-08-13 文档 §1.1）
    if get_pids_by_keyword "python.*notice_server.py" > /dev/null; then
        local pid
        pid="$(get_pids_by_keyword 'python.*notice_server.py' | head -1)"
        echo -e "  ${GREEN}✓${NC} notice_socket (notice_server.py)  PID: $pid"
        echo "    - WebSocket: ws://localhost:8090/ws/notice"
    else
        echo -e "  ${YELLOW}⚠${NC} notice_socket  未运行 (可选)"
    fi

    # Nginx
    if get_pids_by_keyword "nginx.*nginx_cors" > /dev/null; then
        echo -e "  ${GREEN}✓${NC} Nginx (CORS)  http://localhost:8089"
    else
        echo -e "  ${YELLOW}⚠${NC} Nginx (CORS)  未运行 (可选)"
    fi

    echo "========================================"
    echo "日志目录: $LOG_DIR"
    echo "PID 目录: $PID_DIR"
    echo
    echo "实时日志:   $0 logs"
    echo "停止所有:   $0 stop"
    echo "重启所有:   $0 restart"
    echo
}

# ---------------------------------------------------------------------------
# 实时日志
# ---------------------------------------------------------------------------
show_logs() {
    echo "按 Ctrl+C 退出日志查看"
    echo
    local files=()
    [ -f "$LOG_DIR/srs.log" ]                       && files+=("$LOG_DIR/srs.log")
    [ -f "$LOG_DIR/server_fastapi.log" ]            && files+=("$LOG_DIR/server_fastapi.log")
    [ -f "$LOG_DIR/translation_text_publisher.log" ] && files+=("$LOG_DIR/translation_text_publisher.log")
    [ -f "$LOG_DIR/nginx.log" ]                     && files+=("$LOG_DIR/nginx.log")

    if [ ${#files[@]} -eq 0 ]; then
        log_warn "暂无日志文件"
        return
    fi

    # shellcheck disable=SC2016
    tail -F "${files[@]}" | awk '
        /^==> / {print "\n\033[1;34m" $0 "\033[0m"; next}
        {print}
    '
}

# ---------------------------------------------------------------------------
# 帮助
# ---------------------------------------------------------------------------
show_help() {
    cat <<EOF
用法: $0 [命令]

命令:
  start     先杀掉旧进程，再启动所有服务（默认）
  stop      停止所有服务
  restart   停止后启动所有服务
  status    查看服务状态
  logs      实时查看所有日志 (tail -F)
  help      显示此帮助

服务清单:
  1. SRS 流媒体服务器            conf/rtc_with_translation.conf
                                端口: 1935/1985/8080/1990
  2. FastAPI 主服务              server_fastapi.py
                                端口: 8085 (HTTP + WebSocket)
  3. 翻译文本推送服务            translation_text_publisher.py
                                端口: 8086 (WS) / 8087 (HTTP)
  4. Nginx (CORS, 可选)          conf/nginx_cors_for_8089.conf
                                端口: 8089

日志: $LOG_DIR
PID : $PID_DIR
EOF
}

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
main() {
    local cmd="${1:-start}"
    case "$cmd" in
        start)   start_all ;;
        stop)    stop_all ;;
        restart) start_all ;;   # start_all 内部已含 stop_all
        status)  show_status ;;
        logs)    show_logs ;;
        help|--help|-h) show_help ;;
        *)
            log_error "未知命令: $cmd"
            echo
            show_help
            exit 1
            ;;
    esac
}

main "$@"
