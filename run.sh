#!/bin/bash

# 1. 自动进入项目目录
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"


# 2. 自动激活虚拟环境
VENV_PATH="./.venv/bin/activate"
if [ -f "$VENV_PATH" ]; then
    source "$VENV_PATH"
else
    # 尝试备选路径或提示
    if [ -d ".venv" ]; then
        source .venv/bin/activate
    else
        echo "⚠️ 警告: 找不到默认虚拟环境 .venv，将尝试直接运行..."
    fi
fi


# 3. 启动 Streamlit
echo "🚀 正在启动日程规划助手..."
streamlit run app.py
