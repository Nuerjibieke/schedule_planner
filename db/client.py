"""Supabase 客户端封装。"""
import os
from typing import Optional

_supabase = None


def init_supabase(url: str, key: str):
    global _supabase
    from supabase import create_client
    _supabase = create_client(url, key)


def get_supabase():
    if _supabase is None:
        from config import SUPABASE_URL, SUPABASE_KEY
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("请配置 SUPABASE_URL 和 SUPABASE_KEY 环境变量（或 .env）")
        init_supabase(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


# ---------- Tables ----------
TASKS_TABLE = "tasks"
SNAPSHOTS_TABLE = "snapshots"
SUMMARIES_TABLE = "summaries"
REMINDERS_TABLE = "reminders"
LONG_TASKS_TABLE = "long_tasks"
LONG_TASK_LOGS_TABLE = "long_task_logs"
