#!/bin/bash

# 1. 自动进入项目目录
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"


# 2. 自动激活虚拟环境
VENV_PATH="/Users/nujibieke/Documents/codex/ai_schedule/.venv/bin/activate"
if [ -f "$VENV_PATH" ]; then
    source "$VENV_PATH"
else
    echo "❌ 错误: 找不到虚拟环境 $VENV_PATH"
    exit 1
fi

# 3. 启动 Streamlit
echo "🚀 正在启动日程规划助手..."
streamlit run app.py
