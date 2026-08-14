#!/bin/bash

# ============================================================================
# 一键启动所有服务脚本
# 功能：后台启动 SRS 流媒体服务器、回调服务器、翻译服务
# ============================================================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 日志目录
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

# PID目录
PID_DIR="$SCRIPT_DIR/pids"
mkdir -p "$PID_DIR"

# 服务端口
SRS_PORT=1935
SRS_HTTP_PORT=8080
SRS_API_PORT=1985
SRS_RTC_PORT=8000
CALLBACK_PORT=8085
PUSH_PORT=8086

# ---------------------------------------------------------------------------
# 打印彩色日志
# ---------------------------------------------------------------------------
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_service() {
    echo -e "${BLUE}[SERVICE]${NC} $1"
}

# ---------------------------------------------------------------------------
# 检查端口是否被占用
# ---------------------------------------------------------------------------
check_port() {
    local port=$1
    if netstat -tuln 2>/dev/null | grep -q ":$port " || ss -tuln 2>/dev/null | grep -q ":$port "; then
        return 0  # 端口被占用
    fi
    return 1  # 端口空闲
}

# ---------------------------------------------------------------------------
# 检查服务是否运行
# ---------------------------------------------------------------------------
check_service() {
    local pid_file=$1
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$pid_file"
    fi
    return 1
}

# ---------------------------------------------------------------------------
# 停止所有服务
# ---------------------------------------------------------------------------
stop_all() {
    log_info "正在停止所有服务..."
    
    # 首先强制杀死可能存在的进程
    pkill -9 -f "callback_server.py" 2>/dev/null || true
    pkill -9 -f "srs -c" 2>/dev/null || true
    sleep 1
    
    # 杀死 PID 文件中的进程
    for pid_file in "$PID_DIR"/*.pid; do
        if [ -f "$pid_file" ]; then
            local name=$(basename "$pid_file" .pid)
            local pid=$(cat "$pid_file")
            if kill -0 "$pid" 2>/dev/null; then
                log_info "停止 $name (PID: $pid)..."
                kill "$pid" 2>/dev/null || true
                sleep 1
                # 强制停止
                if kill -0 "$pid" 2>/dev/null; then
                    kill -9 "$pid" 2>/dev/null || true
                fi
            fi
            rm -f "$pid_file"
        fi
    done
    
    # 确保端口被释放
    sleep 2
    
    log_info "所有服务已停止"
}

# ---------------------------------------------------------------------------
# 启动 SRS 流媒体服务器
# ---------------------------------------------------------------------------
start_srs() {
    log_service "启动 SRS 流媒体服务器..."
    
    if check_service "$PID_DIR/srs.pid"; then
        log_warn "SRS 已在运行"
        return 0
    fi
    
    if check_port $SRS_PORT || check_port $SRS_HTTP_PORT; then
        log_error "SRS 端口已被占用 ($SRS_PORT 或 $SRS_HTTP_PORT)"
        log_info "检查占用端口的进程:"
        lsof -i:$SRS_PORT 2>/dev/null || lsof -i:$SRS_HTTP_PORT 2>/dev/null || true
        return 1
    fi
    
    local srs_conf="$SCRIPT_DIR/conf/rtc_with_translation.conf"
    if [ ! -f "$srs_conf" ]; then
        srs_conf="$SCRIPT_DIR/trunk/conf/rtc_meeting.conf"
    fi
    
    if [ ! -f "$srs_conf" ]; then
        log_error "未找到 SRS 配置文件"
        return 1
    fi
    
    cd "$SCRIPT_DIR/trunk"
    nohup ./objs/srs -c "$srs_conf" > "$LOG_DIR/srs.log" 2>&1 &
    local srs_pid=$!
    
    sleep 2
    
    if kill -0 "$srs_pid" 2>/dev/null; then
        echo "$srs_pid" > "$PID_DIR/srs.pid"
        log_info "SRS 启动成功 (PID: $srs_pid)"
        log_info "日志文件: $LOG_DIR/srs.log"
    else
        log_error "SRS 启动失败，请检查日志: $LOG_DIR/srs.log"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# 启动回调服务器
# ---------------------------------------------------------------------------
start_callback_server() {
    log_service "启动回调服务器..."
    
    if check_service "$PID_DIR/callback.pid"; then
        log_warn "回调服务器已在运行"
        return 0
    fi
    
    if check_port $CALLBACK_PORT; then
        log_error "回调服务器端口 $CALLBACK_PORT 已被占用"
        return 1
    fi
    
    cd "$SCRIPT_DIR"
    
    # 加载环境变量
    if [ -f ".env" ]; then
        export $(cat .env | grep -v '^#' | xargs)
    fi
    
    export PYTHONPATH=/usr/local/python3.11-ssl/lib/python3.11/site-packages:$PYTHONPATH
    nohup /usr/local/python3.11-ssl/bin/python3.11 callback_server.py > "$LOG_DIR/callback.log" 2>&1 &
    local cb_pid=$!
    
    sleep 2
    
    if kill -0 "$cb_pid" 2>/dev/null; then
        echo "$cb_pid" > "$PID_DIR/callback.pid"
        log_info "回调服务器启动成功 (PID: $cb_pid)"
        log_info "日志文件: $LOG_DIR/callback.log"
    else
        log_error "回调服务器启动失败，请检查日志: $LOG_DIR/callback.log"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# 启动文本推送服务
# ---------------------------------------------------------------------------
start_text_publisher() {
    log_service "启动文本推送服务..."
    
    if check_service "$PID_DIR/text_publisher.pid"; then
        log_warn "文本推送服务已在运行"
        return 0
    fi
    
    if check_port $PUSH_PORT; then
        log_error "文本推送服务端口 $PUSH_PORT 已被占用"
        return 1
    fi
    
    cd "$SCRIPT_DIR"
    
    nohup /usr/local/python3.11-ssl/bin/python3.11 translation_text_publisher.py > "$LOG_DIR/text_publisher.log" 2>&1 &
    local tp_pid=$!
    
    sleep 2
    
    if kill -0 "$tp_pid" 2>/dev/null; then
        echo "$tp_pid" > "$PID_DIR/text_publisher.pid"
        log_info "文本推送服务启动成功 (PID: $tp_pid)"
        log_info "日志文件: $LOG_DIR/text_publisher.log"
    else
        log_error "文本推送服务启动失败，请检查日志: $LOG_DIR/text_publisher.log"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# 显示服务状态
# ---------------------------------------------------------------------------
show_status() {
    echo ""
    echo "========================================"
    echo "         服务状态"
    echo "========================================"
    
    local all_running=true
    
    # SRS
    if check_service "$PID_DIR/srs.pid"; then
        local pid=$(cat "$PID_DIR/srs.pid")
        echo -e "${GREEN}✓ SRS 流媒体服务器${NC}  PID: $pid"
        if check_port $SRS_HTTP_PORT; then
            echo "  - HTTP-FLV:  http://localhost:$SRS_HTTP_PORT"
        fi
        if check_port $SRS_API_PORT; then
            echo "  - HTTP API:  http://localhost:$SRS_API_PORT"
        fi
    else
        echo -e "${RED}✗ SRS 流媒体服务器${NC}  未运行"
        all_running=false
    fi
    
    # 回调服务器 - 通过端口检测
    if check_port $CALLBACK_PORT; then
        # 尝试获取进程 PID
        local cb_pid=$(lsof -t -i:$CALLBACK_PORT 2>/dev/null | head -1)
        if [ -n "$cb_pid" ]; then
            echo -e "${GREEN}✓ 回调服务器${NC}            PID: $cb_pid"
        else
            echo -e "${GREEN}✓ 回调服务器${NC}            (运行中)"
        fi
        echo "  - HTTP API:  http://localhost:$CALLBACK_PORT"
    else
        echo -e "${RED}✗ 回调服务器${NC}            未运行"
        all_running=false
    fi
    
    # 文本推送服务
    if check_service "$PID_DIR/text_publisher.pid"; then
        local pid=$(cat "$PID_DIR/text_publisher.pid")
        echo -e "${GREEN}✓ 文本推送服务${NC}          PID: $pid"
        if check_port $PUSH_PORT; then
            echo "  - WebSocket: ws://localhost:$PUSH_PORT"
        fi
    else
        echo -e "${YELLOW}⚠ 文本推送服务${NC}          未运行"
    fi
    
    echo "========================================"
    
    if $all_running; then
        echo -e "${GREEN}所有核心服务已启动！${NC}"
        echo ""
        echo "服务地址："
        echo "  - SRS 控制台:  http://localhost:$SRS_API_PORT/console"
        echo "  - 回调 API:    http://localhost:$CALLBACK_PORT"
        echo ""
        echo "日志文件："
        echo "  - SRS:         $LOG_DIR/srs.log"
        echo "  - 回调服务器:  $LOG_DIR/callback.log"
        echo "  - 文本推送:    $LOG_DIR/text_publisher.log"
    else
        echo -e "${RED}部分服务启动失败，请检查日志${NC}"
    fi
    
    echo ""
}

# ---------------------------------------------------------------------------
# 显示帮助信息
# ---------------------------------------------------------------------------
show_help() {
    echo "用法: $0 [命令]"
    echo ""
    echo "命令:"
    echo "  start     启动所有服务（默认）"
    echo "  stop      停止所有服务"
    echo "  restart   重启所有服务"
    echo "  status    查看服务状态"
    echo "  logs      查看所有日志（实时）"
    echo "  help      显示帮助信息"
    echo ""
    echo "示例:"
    echo "  $0          # 启动所有服务"
    echo "  $0 status   # 查看服务状态"
    echo "  $0 stop     # 停止所有服务"
}

# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
main() {
    local command="${1:-start}"
    
    case "$command" in
        start)
            log_info "========== 启动所有服务 =========="
            
            # 检查依赖
            if ! command -v nohup &> /dev/null; then
                log_error "nohup 命令不可用"
                exit 1
            fi
            
            if ! command -v python3 &> /dev/null; then
                log_error "python3 不可用"
                exit 1
            fi
            
            # 停止已存在的服务
            stop_all
            
            # 启动服务
            if ! start_srs; then
                log_error "SRS 启动失败，退出"
                exit 1
            fi
            
            sleep 1
            
            if ! start_callback_server; then
                log_error "回调服务器启动失败，退出"
                exit 1
            fi
            
            sleep 1
            
            start_text_publisher  # 文本推送服务可选
            
            echo ""
            show_status
            ;;
            
        stop)
            stop_all
            ;;
            
        restart)
            log_info "========== 重启所有服务 =========="
            stop_all
            sleep 2
            $0 start
            ;;
            
        status)
            show_status
            ;;
            
        logs)
            echo "按 Ctrl+C 退出日志查看"
            echo ""
            if [ -f "$LOG_DIR/srs.log" ]; then
                echo "=== SRS 日志 ==="
                tail -f "$LOG_DIR/srs.log" &
            fi
            if [ -f "$LOG_DIR/callback.log" ]; then
                echo "=== 回调服务器日志 ==="
                tail -f "$LOG_DIR/callback.log" &
            fi
            if [ -f "$LOG_DIR/text_publisher.log" ]; then
                echo "=== 文本推送服务日志 ==="
                tail -f "$LOG_DIR/text_publisher.log" &
            fi
            wait
            ;;
            
        help|--help|-h)
            show_help
            ;;
            
        *)
            log_error "未知命令: $command"
            echo ""
            show_help
            exit 1
            ;;
    esac
}

main "$@"
