#!/bin/bash
# -*- coding: utf-8 -*-
#
# SRS 语音翻译服务 — 依赖安装脚本
#
# 用法: ./setup.sh
#
# 本项目使用两个 Python 版本，所有依赖必须分别安装：
#   - python3.6  (Python 3.6.8)  /usr/bin/python3.6
#   - python3.11 (Python 3.11.9) /usr/local/python3.11/bin/python3.11
#

set -e

echo "============================================"
echo "  SRS 语音翻译服务 — 依赖安装"
echo "============================================"
echo ""

# ---- 检测 Python 版本 ----
echo "[1/4] 检测 Python 版本..."
for py in /usr/bin/python3.6 /usr/local/python3.11/bin/python3.11; do
    if [ -x "$py" ]; then
        ver=$($py --version 2>&1)
        echo "  找到: $py ($ver)"
    fi
done
echo ""

# ---- Python 3.6 依赖 ----
echo "[2/4] 安装 Python 3.6 依赖..."
echo "  版本: Python 3.6.8"
echo "  用途: audio_translation_service, callback_server, translation_text_publisher_simple"

PY36_PKGS=(
    "flask"
    "flask-socketio"
    "gevent"
    "requests"
    "python-socketio"
    "python-socketio[wsaccel]"
    "langdetect"
    "websockets"
)

for pkg in "${PY36_PKGS[@]}"; do
    if $py -c "import ${pkg%%>*}" 2>/dev/null; then
        echo "  [ok] $pkg"
    else
        echo "  [install] $pkg..."
        $py -m pip install "$pkg" -q --root-user-action=ignore 2>&1 | tail -1 || true
    fi
done
echo ""

# ---- Python 3.11 依赖 ----
echo "[3/4] 安装 Python 3.11 依赖..."
echo "  版本: Python 3.11.9"
echo "  用途: 备用 / websocket-server 等"

PY311_PKGS=(
    "flask"
    "flask-socketio"
    "requests"
    "python-socketio"
    "python-socketio[wsaccel]"
    "gevent"
    "websockets"
)

for pkg in "${PY311_PKGS[@]}"; do
    if /usr/local/python3.11/bin/python3.11 -c "import ${pkg%%>*}" 2>/dev/null; then
        echo "  [ok] $pkg"
    else
        echo "  [install] $pkg..."
        /usr/local/python3.11/bin/python3.11 -m pip install "$pkg" -q --root-user-action=ignore 2>&1 | tail -1 || true
    fi
done
echo ""

# ---- 环境变量检查 ----
echo "[4/4] 检查环境变量..."
ENV_FILE="/root/srs-project/srs/.env"
if [ -f "$ENV_FILE" ]; then
    echo "  [ok] .env 存在: $ENV_FILE"
else
    echo "  [!] .env 不存在，复制模板..."
    if [ -f "/root/srs-project/srs/.env.example" ]; then
        cp /root/srs-project/srs/.env.example /root/srs-project/srs/.env
        echo "  [ok] 已从 .env.example 复制，请编辑 .env 填写百度 API 密钥"
    else
        echo "  [!] 未找到 .env.example，请手动创建 .env"
    fi
fi
echo ""

# ---- 依赖验证 ----
echo "============================================"
echo "  依赖验证"
echo "============================================"
echo ""

echo "Python 3.6 (audio_translation_service / callback_server):"
for m in flask gevent requests langdetect socketio websockets; do
    if python3.6 -c "import $m" 2>/dev/null; then
        echo "  [ok] $m"
    else
        echo "  [MISSING] $m  ← 需要安装"
    fi
done
echo ""

echo "Python 3.11 (备用):"
for m in flask requests websockets; do
    if python3.11 -c "import $m" 2>/dev/null; then
        echo "  [ok] $m"
    else
        echo "  [MISSING] $m"
    fi
done
echo ""

echo "============================================"
echo "  安装完成"
echo "============================================"
echo ""
echo "下一步: 启动服务"
echo "  python3.6 callback_server.py               # 回调 + 事件推送 (端口 8085)"
echo "  python3.6 translation_text_publisher_simple.py  # 文本推送 (WS:8086 HTTP:8087)"
echo ""
