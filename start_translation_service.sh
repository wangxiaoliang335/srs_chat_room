#!/bin/bash
# 启动语音翻译服务的脚本

set -e

# 检查环境变量
if [ -z "$BAIDU_API_KEY" ] || [ -z "$BAIDU_SECRET_KEY" ]; then
    echo "错误: 请设置环境变量 BAIDU_API_KEY 和 BAIDU_SECRET_KEY"
    echo "示例:"
    echo "  export BAIDU_API_KEY='your_api_key'"
    echo "  export BAIDU_SECRET_KEY='your_secret_key'"
    exit 1
fi

# 检查Python依赖
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 python3，请先安装 Python 3.7+"
    exit 1
fi

# 检查FFmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "错误: 未找到 ffmpeg，请先安装 FFmpeg"
    exit 1
fi

# 安装Python依赖
echo "安装Python依赖..."
pip3 install -r requirements.txt

# 设置默认值
export SRS_URL=${SRS_URL:-"http://localhost:8080"}
export CALLBACK_PORT=${CALLBACK_PORT:-8085}
export CALLBACK_HOST=${CALLBACK_HOST:-"0.0.0.0"}

# 启动回调服务器
echo "启动回调服务器..."
python3 callback_server.py &
CALLBACK_PID=$!

# 等待回调服务器启动
sleep 2

# 检查回调服务器是否正常运行
if ! ps -p $CALLBACK_PID > /dev/null; then
    echo "错误: 回调服务器启动失败"
    exit 1
fi

echo "回调服务器已启动 (PID: $CALLBACK_PID)"
echo "服务运行在 http://${CALLBACK_HOST}:${CALLBACK_PORT}"
echo ""
echo "按 Ctrl+C 停止服务"

# 等待中断信号
trap "echo '正在停止服务...'; kill $CALLBACK_PID 2>/dev/null; exit" INT TERM

# 保持运行
wait $CALLBACK_PID
