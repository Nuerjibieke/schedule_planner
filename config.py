"""配置：从环境变量读取密钥，供 Docker 注入。"""
import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-pro-preview-thinking")
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "")
GEMINI_API_TYPE = os.getenv("GEMINI_API_TYPE", "google") # 'google' or 'openai'
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
# Agent 模式：是否启用 tool-calling agent（默认启用）
USE_AGENT_MODE = os.getenv("USE_AGENT_MODE", "true").lower() in ("true", "1", "yes")
# 仅允许 agent 协议成功后执行写库；legacy 仅允许 query/澄清
AGENT_ONLY_WRITE_MODE = os.getenv("AGENT_ONLY_WRITE_MODE", "true").lower() in ("true", "1", "yes")
# Agent 最大工具调用轮次
AGENT_MAX_TOOL_ROUNDS = int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "8"))
# Agent 是否在 system prompt 里预注入“今日关键任务快照”（默认关闭：严格按需查库）
AGENT_INCLUDE_TASK_SNAPSHOT = os.getenv("AGENT_INCLUDE_TASK_SNAPSHOT", "false").lower() in ("true", "1", "yes")
# Agent 二阶段计划审查（planner->critic）：是否启用
PLAN_CRITIC_ENABLED = os.getenv("PLAN_CRITIC_ENABLED", "true").lower() in ("true", "1", "yes")
# Critic 最多修正轮次（默认 1 轮：审查+修正一次）
PLAN_CRITIC_MAX_ROUNDS = int(os.getenv("PLAN_CRITIC_MAX_ROUNDS", "1"))

# Agent 协议修复编译器（Planner 输出无效时，使用专用编译模型提取合法 GraphIntent）
AGENT_COMPILER_ENABLED = os.getenv("AGENT_COMPILER_ENABLED", "true").lower() in ("true", "1", "yes")
# 编译器修复重试次数
AGENT_COMPILER_MAX_RETRIES = int(os.getenv("AGENT_COMPILER_MAX_RETRIES", "2"))
# 可选：指定与 planner 不同的编译模型；留空则复用当前模型
AGENT_COMPILER_MODEL = os.getenv("AGENT_COMPILER_MODEL", "")

# Legacy NLP prompt 是否包含对话历史/当日任务概览（默认关闭：避免隐式记忆）
NLP_INCLUDE_HISTORY_IN_PROMPT = os.getenv("NLP_INCLUDE_HISTORY_IN_PROMPT", "false").lower() in ("true", "1", "yes")
NLP_INCLUDE_DAY_TASKS_IN_PROMPT = os.getenv("NLP_INCLUDE_DAY_TASKS_IN_PROMPT", "false").lower() in ("true", "1", "yes")

# 声明式目标态闭环：是否启用自动补救（默认关闭，保守策略）
DESIRED_STATE_AUTOFIX_ENABLED = os.getenv("DESIRED_STATE_AUTOFIX_ENABLED", "false").lower() in ("true", "1", "yes")
# 自动补救动作上限（超过则不自动执行）
DESIRED_STATE_AUTOFIX_MAX_ACTIONS = int(os.getenv("DESIRED_STATE_AUTOFIX_MAX_ACTIONS", "2"))
# 自动补救是否允许插入新任务（默认关闭，仅允许 update）
DESIRED_STATE_AUTOFIX_ALLOW_INSERT = os.getenv("DESIRED_STATE_AUTOFIX_ALLOW_INSERT", "false").lower() in ("true", "1", "yes")
# 目标态收敛闭环：自动补救最多循环轮数（每轮都执行 校验->补救->再校验）
DESIRED_STATE_RECONCILE_MAX_ROUNDS = int(os.getenv("DESIRED_STATE_RECONCILE_MAX_ROUNDS", "2"))

# 语义指纹幂等门：短时间窗口内拦截重复等价写入
IDEMPOTENCY_FINGERPRINT_ENABLED = os.getenv("IDEMPOTENCY_FINGERPRINT_ENABLED", "true").lower() in ("true", "1", "yes")
# 幂等窗口（秒）
IDEMPOTENCY_FINGERPRINT_TTL_SECONDS = int(os.getenv("IDEMPOTENCY_FINGERPRINT_TTL_SECONDS", "45"))
# 内存缓存上限（超过时淘汰最早记录）
IDEMPOTENCY_FINGERPRINT_MAX_ENTRIES = int(os.getenv("IDEMPOTENCY_FINGERPRINT_MAX_ENTRIES", "512"))

# 动作后验闭环：单步执行后最多自动修复重试次数
STATE_RECONCILE_MAX_RETRIES = int(os.getenv("STATE_RECONCILE_MAX_RETRIES", "2"))
