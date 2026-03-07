"""配置：从环境变量读取密钥，供 Docker 注入。"""
import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
# HKUST Azure OpenAI 配置（备选）
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "https://hkust.azure-api.net")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-07-18")
# 逗号分隔的候选 api-version 列表（遇到 404 Resource not found 时自动尝试下一个）
AZURE_OPENAI_API_VERSIONS = os.getenv("AZURE_OPENAI_API_VERSIONS", "2023-05-15,2024-07-18")
# 填 Azure 里实际的 Deployment 名（如 gpt-4o-mini / gpt-35-turbo）
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
# 逗号分隔的候选 deployment 列表（遇到 404 时自动尝试下一个）
AZURE_OPENAI_DEPLOYMENTS = os.getenv("AZURE_OPENAI_DEPLOYMENTS", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
# 下班时间（小时），超过则边界报警
WORKDAY_END_HOUR = int(os.getenv("WORKDAY_END_HOUR", "22"))
# 时间轴显示范围（小时）
WORKDAY_START_HOUR = int(os.getenv("WORKDAY_START_HOUR", "8"))
# 日程提醒：提前多少分钟弹出系统通知（0=仅到点提醒）
REMINDER_MINUTES_BEFORE = int(os.getenv("REMINDER_MINUTES_BEFORE", "5"))
# 提醒守护进程轮询间隔（秒）
REMINDER_POLL_INTERVAL = int(os.getenv("REMINDER_POLL_INTERVAL", "60"))