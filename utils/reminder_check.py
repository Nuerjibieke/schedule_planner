"""
日程提醒检查：拉取今日任务，在开始前/到时发送系统桌面通知。
供 app.py 的 fragment 与 run_reminder_daemon.py 共用。
"""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from db import repo
from utils.desktop_notify import notify

# 状态文件放在项目根目录
_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = _ROOT / ".reminder_sent.json"


def _get_config() -> tuple[int, int]:
    """(REMINDER_MINUTES_BEFORE, REMINDER_POLL_INTERVAL) 延迟导入避免循环依赖。"""
    from config import REMINDER_MINUTES_BEFORE
    return REMINDER_MINUTES_BEFORE, 0


def _parse_start_local(iso: str) -> Optional[datetime]:
    """把 start_time 转成本地 datetime。"""
    if not iso:
        return None
    try:
        s = (iso or "").replace("Z", "+00:00").strip()
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except Exception:
        return None


def _load_sent() -> dict:
    """已发送提醒记录：{ "YYYY-MM-DD": ["task_id", ...] }"""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sent(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)


def _mark_sent(today: date, task_id: str) -> None:
    data = _load_sent()
    key = today.isoformat()
    data.setdefault(key, [])
    if task_id not in data[key]:
        data[key].append(task_id)
    keys = sorted(data.keys(), reverse=True)[:7]
    _save_sent({k: data[k] for k in keys})


def _already_sent(today: date, task_id: str) -> bool:
    return str(task_id) in _load_sent().get(today.isoformat(), [])


def run_reminder_check() -> int:
    """
    执行一轮提醒检查：今日未完成任务若在「开始前 N 分钟～开始后 2 分钟」内则发系统通知。
    返回本轮发送的提醒数量。
    """
    try:
        minutes_before, _ = _get_config()
    except Exception:
        minutes_before = 5
    today = date.today()
    now = datetime.now().astimezone()
    try:
        tasks = repo.tasks_for_date(today)
    except Exception:
        return 0

    sent = 0
    for t in tasks:
        if t.status == "done":
            continue
        start_local = _parse_start_local(t.start_time)
        if not start_local:
            continue
        window_start = start_local - timedelta(minutes=minutes_before)
        window_end = start_local + timedelta(minutes=2)
        if not (window_start <= now <= window_end):
            continue
        if _already_sent(today, str(t.id)):
            continue
        if notify("Schedule Reminder", f"Starting now: {t.title}"):
            _mark_sent(today, str(t.id))
            sent += 1
    return sent
