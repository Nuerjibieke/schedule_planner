"""应用服务：串联 右脑(AI) -> 左脑(逻辑) -> 记忆体(DB)。"""
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple, Dict, Any
from dataclasses import dataclass
from pathlib import Path
import json
import logging
import hashlib
import time

from db import repo
from db.models import Task
from ai import parse_user_command, CommandIntent
from ai.guardrails_wrapper import apply_guardrails
from logic import run_domino, DominoResult
from logic.plan_normalizer import normalize_command_intent
from service.command_pipeline import run_command_pipeline
from service.action_executor import ActionExecutor
from service.plan_trust_gate import evaluate_plan_trust
from utils.idempotency_store import PersistentIdempotencyStore
from utils.log_markers import LOG_ACTION, LOG_VERIFY, LOG_ROLLBACK
from config import (
    WORKDAY_END_HOUR,
    AGENT_ONLY_WRITE_MODE,
    DESIRED_STATE_AUTOFIX_ENABLED,
    DESIRED_STATE_AUTOFIX_MAX_ACTIONS,
    DESIRED_STATE_AUTOFIX_ALLOW_INSERT,
    DESIRED_STATE_RECONCILE_MAX_ROUNDS,
    IDEMPOTENCY_FINGERPRINT_ENABLED,
    IDEMPOTENCY_FINGERPRINT_TTL_SECONDS,
    IDEMPOTENCY_FINGERPRINT_MAX_ENTRIES,
    STATE_RECONCILE_MAX_RETRIES,
)

log = logging.getLogger("app.service")
_WRITE_FINGERPRINT_CACHE: Dict[str, float] = {}
_PERSISTENT_IDEM_STORE = PersistentIdempotencyStore(
    path=str(Path(__file__).resolve().parents[1] / "log" / "idempotency_keys.json"),
    ttl_seconds=IDEMPOTENCY_FINGERPRINT_TTL_SECONDS,
    max_entries=IDEMPOTENCY_FINGERPRINT_MAX_ENTRIES,
)


def _local_tz():
    tz = datetime.now().astimezone().tzinfo
    return tz or timezone.utc


def _stable_json_for_fingerprint(payload: Any) -> str:
    """稳定序列化：用于生成可重复的语义指纹。"""
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(payload)


def _build_write_fingerprint(scope: str, payload: Any) -> str:
    raw = f"{scope}|{_stable_json_for_fingerprint(payload)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evict_fingerprint_cache(now_ts: float) -> None:
    if not _WRITE_FINGERPRINT_CACHE:
        return
    expire_before = now_ts - max(1, IDEMPOTENCY_FINGERPRINT_TTL_SECONDS)
    stale = [k for k, ts in _WRITE_FINGERPRINT_CACHE.items() if ts < expire_before]
    for k in stale:
        _WRITE_FINGERPRINT_CACHE.pop(k, None)

    # 容量控制：删除最老记录
    overflow = len(_WRITE_FINGERPRINT_CACHE) - max(32, IDEMPOTENCY_FINGERPRINT_MAX_ENTRIES)
    if overflow > 0:
        oldest = sorted(_WRITE_FINGERPRINT_CACHE.items(), key=lambda x: x[1])[:overflow]
        for k, _ in oldest:
            _WRITE_FINGERPRINT_CACHE.pop(k, None)


def _is_recent_duplicate_write(fingerprint: str) -> bool:
    if not IDEMPOTENCY_FINGERPRINT_ENABLED:
        return False
    now_ts = time.time()
    _evict_fingerprint_cache(now_ts)
    ts = _WRITE_FINGERPRINT_CACHE.get(fingerprint)
    if ts is not None and (now_ts - ts) <= max(1, IDEMPOTENCY_FINGERPRINT_TTL_SECONDS):
        return True
    # 进程外/重启后仍生效：持久层兜底
    is_dup = _PERSISTENT_IDEM_STORE.is_recent(fingerprint, now_ts=now_ts)
    if is_dup:
        _WRITE_FINGERPRINT_CACHE[fingerprint] = now_ts
    return is_dup


def _mark_write_fingerprint(fingerprint: str) -> None:
    if not IDEMPOTENCY_FINGERPRINT_ENABLED:
        return
    now_ts = time.time()
    _evict_fingerprint_cache(now_ts)
    _WRITE_FINGERPRINT_CACHE[fingerprint] = now_ts
    _PERSISTENT_IDEM_STORE.mark(fingerprint, now_ts=now_ts)


def _task_row_for_state_version(t: Task) -> Dict[str, Any]:
    return {
        "id": str(getattr(t, "id", "") or ""),
        "title": str(getattr(t, "title", "") or ""),
        "start_time": str(getattr(t, "start_time", "") or ""),
        "end_time": str(getattr(t, "end_time", "") or ""),
        "priority": str(getattr(t, "priority", "") or ""),
        "status": str(getattr(t, "status", "") or ""),
        "is_fixed": bool(getattr(t, "is_fixed", False)),
        "memo": getattr(t, "memo", None),
    }


def _compute_scope_state_version(scope_dates: List[date]) -> str:
    """
    基于作用域任务快照计算稳定版本号：
    - 相同状态 -> 相同 version
    - 任意任务变动 -> version 改变
    """
    rows: List[Dict[str, Any]] = []
    seen_ids = set()
    for d in (scope_dates or []):
        try:
            tasks = repo.tasks_for_date(d)
        except Exception:
            tasks = []
        for t in tasks:
            tid = str(getattr(t, "id", "") or "")
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            rows.append(_task_row_for_state_version(t))
    rows.sort(key=lambda x: (x.get("start_time", ""), x.get("id", "")))
    payload = _stable_json_for_fingerprint(rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scope_dates_from_preview_updates(updates: List[Dict[str, Any]], fallback: date) -> List[date]:
    probe_actions: List[Dict[str, Any]] = []
    for u in (updates or []):
        if not isinstance(u, dict):
            continue
        probe_actions.append(
            {
                "action": "update",
                "target_task_id": u.get("id"),
                "start_iso": u.get("old_start"),
                "end_iso": u.get("old_end"),
                "new_start_iso": u.get("new_start"),
                "new_end_iso": u.get("new_end"),
            }
        )
    return _collect_scope_dates_from_actions(probe_actions, fallback)

@dataclass
class PendingPlan:
    """需要用户确认的排程变更（预览 -> 确认 -> 落库）。"""
    # 用户原始输入（用于展示）
    user_text: str
    # 需要写回的变更列表：[{id, title, old_start, old_end, new_start, new_end}]
    updates: List[Dict[str, str]]
    # 是否超时提示
    overflow_warning: Optional[str] = None
    # 可选：声明式目标态，用于执行后闭环校验
    desired_state: Optional[List[Dict[str, Any]]] = None
    # 生成预览时的作用域状态版本，确认执行前用于并发变更检测
    state_version: Optional[str] = None
    # 确认执行时优先使用的结构化动作（insert/update/delete/complete）
    actions: Optional[List[Dict[str, Any]]] = None

def _parse_dt_naive(s: str) -> datetime:
    """把 ISO 字符串解析为可比较的 naive datetime（统一到 UTC 后去掉 tzinfo）。"""
    if not s:
        return datetime.now().replace(microsecond=0)
    try:
        ss = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        # 兜底：只取到秒
        try:
            return datetime.fromisoformat(s[:19])
        except Exception:
            return datetime.now().replace(microsecond=0)


def _parse_local_naive(s: str) -> Optional[datetime]:
    """把 ISO 字符串解析为本地 naive datetime（用于本地时间线冲突计算）。"""
    if not s:
        return None
    try:
        ss = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            return dt.replace(microsecond=0)
        return dt.astimezone(_local_tz()).replace(tzinfo=None, microsecond=0)
    except Exception:
        try:
            return datetime.fromisoformat(s[:19]).replace(microsecond=0)
        except Exception:
            return None


def _parse_dt_local_date(s: str, naive_is_local: bool = False) -> Optional[date]:
    """
    把 ISO 字符串解析为用户本地日期。

    naive_is_local=False（默认）：
    - 无时区字符串按 UTC 解释（兼容历史存储语义）

    naive_is_local=True：
    - 无时区字符串按“用户本地时间语义”解释（适用于模型 action/desired_state）
    """
    if not s:
        return None
    try:
        ss = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            if naive_is_local:
                # action/desired_state 常为本地无时区字符串（如 2026-03-09T18:50:00）
                return dt.date()
            # 默认兼容历史：无时区按 UTC 存储解释
            dt = dt.replace(tzinfo=timezone.utc)
        # 转为本地时间后取 date
        return dt.astimezone().date()
    except Exception:
        try:
            return datetime.fromisoformat(s[:19]).date()
        except Exception:
            return None


def _to_iso(dt: datetime) -> str:
    """转换为 ISO 格式，统一为 UTC 时区后缀格式以便与数据库格式一致。"""
    dt = dt.replace(microsecond=0)
    # 确保是 UTC 时间
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _local_naive_to_utc_iso(dt_local: datetime) -> str:
    """本地 naive datetime -> UTC ISO(+00:00)。"""
    dt_local = dt_local.replace(microsecond=0)
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=_local_tz())
    dt_utc = dt_local.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _local_iso_to_utc_iso(s: str) -> str:
    """
    把无时区的 ISO 字符串（如 2026-02-02T16:00:00）视为用户本地时间，转为 UTC 的 ISO 字符串。
    NLP 返回的「明天 16 点」等均为用户本地时间，若按 UTC 存会导致日历显示错一天/错时间。
    """
    if not s or not s.strip():
        return s
    s = s.strip()
    if "+" in s or "Z" in s:
        return _to_iso(_parse_dt_naive(s))
    try:
        dt_naive = datetime.fromisoformat(s[:19].rstrip())
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is None:
            return _to_iso(dt_naive.replace(tzinfo=timezone.utc))
        dt_local = dt_naive.replace(tzinfo=local_tz)
        dt_utc = dt_local.astimezone(timezone.utc)
        return _to_iso(dt_utc)
    except Exception:
        return _to_iso(_parse_dt_naive(s))


def _utc_iso_to_local_display(utc_iso: str) -> Tuple[str, str]:
    """
    把存储的 UTC ISO 字符串转为用户本地时间的显示：(日期 'YYYY-MM-DD', 时间 'HH:MM')。
    用于反馈文案，避免用户看到 UTC 时间误以为规划错误。
    """
    if not utc_iso or not utc_iso.strip():
        return ("", "")
    try:
        ss = utc_iso.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss[:26] if "+" in ss else ss[:19])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone()
        return (local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H:%M"))
    except Exception:
        return (utc_iso[:10] if len(utc_iso) >= 10 else "", utc_iso[11:16] if len(utc_iso) >= 16 else "")


def _next_weekday(d: date, target_w: int) -> date:
    """从 d 起下一个 target_w 日（周一=0，周日=6）。若今天就是该星期则返回 7 天后。"""
    days = (target_w - d.weekday() + 7) % 7
    if days == 0:
        days = 7
    return d + timedelta(days=days)


# 用户说「下周五」等时，用于从 user_text 解析出正确日期，纠正模型可能算错的星期
_RELATIVE_DAY_PHRASES = [
    ("下周一", 0), ("下周二", 1), ("下周三", 2), ("下周四", 3),
    ("下周五", 4), ("下周六", 5), ("下周日", 6),
]


def _get_relative_date_from_user_text(user_text: str, today: date) -> Optional[date]:
    """若 user_text 包含「下周一」～「下周日」，返回对应的正确日期；否则 None。"""
    if not (user_text or "").strip():
        return None
    for phrase, w in _RELATIVE_DAY_PHRASES:
        if phrase in user_text:
            return _next_weekday(today, w)
    return None


def _correct_date_in_iso(iso_str: str, new_date: date) -> str:
    """把 ISO 字符串里的日期部分替换为 new_date，保留时间部分。"""
    if not iso_str or len(iso_str) < 10:
        return iso_str
    return new_date.isoformat() + iso_str[10:]


def _correct_relative_dates_in_actions(user_text: str, today: date, actions: Optional[List[Dict]]) -> None:
    """
    当用户说了「下周五」等相对星期时，若模型返回的日期星期不对（如把下周五算成周二），
    用我们算出的正确日期覆盖 action 里的 new_start_iso/new_end_iso 或 start_iso/end_iso。
    """
    correct_date = _get_relative_date_from_user_text(user_text, today)
    if correct_date is None:
        return
    expected_weekday = correct_date.weekday()
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        for key_start, key_end in [("new_start_iso", "new_end_iso"), ("start_iso", "end_iso")]:
            start_iso = a.get(key_start)
            if not start_iso or len(start_iso) < 10:
                continue
            try:
                action_date_str = start_iso[:10]
                action_date = date.fromisoformat(action_date_str)
                if action_date.weekday() != expected_weekday:
                    a[key_start] = _correct_date_in_iso(start_iso, correct_date)
                    end_iso = a.get(key_end)
                    if end_iso and len(end_iso) >= 10:
                        a[key_end] = _correct_date_in_iso(end_iso, correct_date)
                    log.info(
                        "相对日期纠正: 用户含「下周X」-> 应为 %s（%s），将 %s 改为该日期",
                        correct_date.isoformat(),
                        "一二三四五六日"[expected_weekday],
                        action_date_str,
                    )
            except Exception:
                pass


def get_today_tasks() -> List[Task]:
    return repo.tasks_for_date(date.today())


def check_and_reschedule_overdue_tasks() -> Tuple[int, str]:
    """
    检查所有早于今天的未完成任务，自动延期到今天。
    只延期 status 为 pending 且未锁定的任务；已完成(done)的不动。
    返回：(延期任务数, 消息)
    """
    today = date.today()
    overdue_tasks = repo.tasks_overdue_pending(today)
    rescheduled_count = 0
    messages = []

    for task in overdue_tasks:
        task_end_local_date = _parse_dt_local_date(task.end_time)
        if task_end_local_date is None:
            continue
        # 仅当任务结束时间（本地日期）严格早于今天本地日期时，才真正延期。
        # 避免当天很晚创建的任务，由于 UTC 偏移量被误当作昨天处理。
        if task_end_local_date >= today:
            continue

        # 将旧的开始/结束时间按原时间段的长度计算 duration
        task_start = _parse_dt_naive(task.start_time)
        task_end = _parse_dt_naive(task.end_time)
        duration_min = int((task_end - task_start).total_seconds() / 60)
        if duration_min <= 0:
            duration_min = 60

        today_tasks = repo.tasks_for_date(today)
        new_start, new_end = _find_earliest_free_slot(today_tasks, today, duration_min)
        
        # 将算出来的新空闲时间（本地无时区）转为 UTC 存库
        new_start_iso = _local_naive_to_utc_iso(new_start)
        new_end_iso = _local_naive_to_utc_iso(new_end)
        repo.update_task(task.id, {
            "start_time": new_start_iso,
            "end_time": new_end_iso
        })
        rescheduled_count += 1
        d1, t1 = _utc_iso_to_local_display(new_start_iso)
        _, t2 = _utc_iso_to_local_display(new_end_iso)
        messages.append(f"- {task.title}：延期到 {d1} {t1}–{t2}")
        # 更新本地今日任务列表，避免下一个任务占用同一时段
        today_tasks = repo.tasks_for_date(today)

    if rescheduled_count > 0:
        msg = f"已自动延期 {rescheduled_count} 个逾期未完成任务到今天：\n" + "\n".join(messages)
    else:
        msg = "没有需要延期的任务。"
    
    return rescheduled_count, msg


def get_tasks_for_date(d: date) -> List[Task]:
    """取指定日期的全部任务，用于回溯往期日程。"""
    return repo.tasks_for_date(d)


def toggle_task_status(task_id: str) -> Tuple[bool, str]:
    """切换任务状态（完成<->待办）。"""
    task = repo.task_by_id(task_id)
    if not task:
        return False, "任务不存在"
    new_status = "done" if task.status == "pending" else "pending"
    repo.update_task(task_id, {"status": new_status})
    return True, f"任务已标记为{'完成' if new_status == 'done' else '待办'}"


def delete_task_by_id(task_id: str) -> Tuple[bool, str]:
    """删除指定任务。"""
    task = repo.task_by_id(task_id)
    if not task:
        return False, "任务不存在"
    repo.delete_task(task_id)
    return True, "任务已删除"


def update_task_progress(task_id: str, progress: int) -> Tuple[bool, str]:
    """
    更新任务进度并持久化到 DB。
    存档方式：进度写入任务表的 memo 字段，格式为 "progress:N"（或 "其他内容 | progress:N"），
    由前端从 memo 用正则 progress:\\s*(\\d+) 解析显示。只有当前端带着 ?progress_update=true&task_id=...&progress=N
    请求到本应用时才会调用本函数，若 iframe 内重定向的 URL 未命中本应用，则刷新后进度会丢失。
    progress: 0-100 的整数
    """
    task = repo.task_by_id(task_id)
    if not task:
        log.warning("update_task_progress: 任务不存在 task_id=%s", task_id)
        return False, "任务不存在"
    
    progress = max(0, min(100, progress))
    
    # 更新memo字段存储进度
    import re
    current_memo = task.memo or ""
    # 移除所有 progress:N 标记，避免残留导致刷新后仍显示旧值
    memo_clean = re.sub(r'progress:\s*\d+', '', current_memo)
    memo_clean = re.sub(r'\s*\|\s*$', '', memo_clean).strip()
    memo_clean = re.sub(r'^\s*\|\s*', '', memo_clean).strip()
    memo_clean = re.sub(r'\s*\|\s*', ' | ', memo_clean).strip()
    
    # 添加新的progress标记
    if memo_clean:
        new_memo = f"{memo_clean} | progress:{progress}"
    else:
        new_memo = f"progress:{progress}"
    
    # 若 memo 中已有相同进度则不再写库，避免无谓 update 和后续 rerun 刷屏
    progress_in_memo = re.findall(r'progress:\s*(\d+)', current_memo)
    if progress_in_memo and int(progress_in_memo[-1]) == progress:
        return True, f"任务进度已为{progress}%"
    
    log.info("update_task_progress: task_id=%s progress=%s memo_before=%r new_memo=%r",
             task_id, progress, current_memo, new_memo)
    updated = repo.update_task(task_id, {"memo": new_memo})
    log.info("update_task_progress: repo.update_task 返回=%s", updated is not None)
    
    # 如果进度是100%，自动标记为完成
    if progress >= 100:
        repo.update_task(task_id, {"status": "done"})
        return True, f"任务进度已更新为{progress}%，已自动标记为完成"
    
    # 不再在 progress<100 时把「已完成」改回待办，避免覆盖用户刚点的「完成」
    # （例如用户先点完成再调进度条，或 iframe/URL 带着 progress=0 触发更新）
    
    return True, f"任务进度已更新为{progress}%"


def start_task_timer(task_id: str) -> Tuple[bool, str]:
    """开始任务计时。"""
    task = repo.task_by_id(task_id)
    if not task:
        return False, "任务不存在"
    
    if task.status == "done":
        return False, "已完成的任务无法开始计时"
    
    # 记录开始时间到memo
    import re
    from datetime import datetime
    current_memo = task.memo or ""
    
    # 移除旧的timer_start标记
    memo_clean = re.sub(r'\s*\|\s*timer_start:\s*[^|]+\s*', '', current_memo)
    memo_clean = re.sub(r'timer_start:\s*[^|]+\s*', '', memo_clean)
    memo_clean = re.sub(r'\s*\|\s*$', '', memo_clean).strip()
    
    # 添加新的timer_start标记
    start_time_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if memo_clean:
        new_memo = f"{memo_clean} | timer_start:{start_time_iso}"
    else:
        new_memo = f"timer_start:{start_time_iso}"
    
    repo.update_task(task_id, {"memo": new_memo})
    return True, f"已开始计时：{task.title}"


def pause_task_timer(task_id: str) -> Tuple[bool, str]:
    """暂停任务计时，累计已工作时间并更新进度。"""
    task = repo.task_by_id(task_id)
    if not task:
        return False, "任务不存在"
    
    import re
    from datetime import datetime, timedelta
    
    current_memo = task.memo or ""
    
    # 提取timer_start时间
    start_match = re.search(r'timer_start:\s*([^|]+)', current_memo)
    if not start_match:
        return False, "未找到开始时间，请先开始计时"
    
    start_time_str = start_match.group(1).strip()
    try:
        start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        if start_time.tzinfo:
            start_time = start_time.astimezone().replace(tzinfo=None)
    except:
        return False, "开始时间格式错误"
    
    # 计算已工作时间（分钟）
    now = datetime.now()
    elapsed_minutes = int((now - start_time).total_seconds() / 60)
    
    # 提取累计工作时间
    total_work_match = re.search(r'total_work_minutes:\s*(\d+)', current_memo)
    total_work_minutes = int(total_work_match.group(1)) if total_work_match else 0
    total_work_minutes += elapsed_minutes
    
    # 计算任务总时长
    try:
        start_dt = datetime.fromisoformat(task.start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(task.end_time.replace("Z", "+00:00"))
        if start_dt.tzinfo:
            start_dt = start_dt.astimezone().replace(tzinfo=None)
        if end_dt.tzinfo:
            end_dt = end_dt.astimezone().replace(tzinfo=None)
        task_duration_minutes = int((end_dt - start_dt).total_seconds() / 60)
    except:
        task_duration_minutes = 60  # 默认60分钟
    
    # 计算进度（基于已工作时间）
    progress = min(100, int((total_work_minutes / task_duration_minutes) * 100)) if task_duration_minutes > 0 else 0
    
    # 更新memo：移除timer_start，添加total_work_minutes和progress
    memo_clean = re.sub(r'\s*\|\s*timer_start:\s*[^|]+\s*', '', current_memo)
    memo_clean = re.sub(r'timer_start:\s*[^|]+\s*', '', memo_clean)
    memo_clean = re.sub(r'\s*\|\s*total_work_minutes:\s*\d+\s*', '', memo_clean)
    memo_clean = re.sub(r'total_work_minutes:\s*\d+\s*', '', memo_clean)
    memo_clean = re.sub(r'\s*\|\s*progress:\s*\d+\s*', '', memo_clean)
    memo_clean = re.sub(r'progress:\s*\d+\s*', '', memo_clean)
    memo_clean = re.sub(r'\s*\|\s*$', '', memo_clean).strip()
    
    if memo_clean:
        new_memo = f"{memo_clean} | total_work_minutes:{total_work_minutes} | progress:{progress}"
    else:
        new_memo = f"total_work_minutes:{total_work_minutes} | progress:{progress}"
    
    repo.update_task(task_id, {"memo": new_memo})
    
    # 如果进度是100%，自动标记为完成
    if progress >= 100:
        repo.update_task(task_id, {"status": "done"})
        return True, f"已暂停计时，累计工作{total_work_minutes}分钟，进度{progress}%，任务已完成"
    
    return True, f"已暂停计时，累计工作{total_work_minutes}分钟，进度{progress}%"


def end_task_timer(task_id: str) -> Tuple[bool, str]:
    """结束任务计时，更新最终进度。"""
    task = repo.task_by_id(task_id)
    if not task:
        return False, "任务不存在"
    
    import re
    from datetime import datetime
    
    current_memo = task.memo or ""
    
    # 如果正在计时，先暂停
    if 'timer_start:' in current_memo:
        pause_success, pause_msg = pause_task_timer(task_id)
        if not pause_success:
            return False, pause_msg
        # 重新获取任务（因为memo已更新）
        task = repo.task_by_id(task_id)
        current_memo = task.memo or ""
    
    # 提取累计工作时间
    total_work_match = re.search(r'total_work_minutes:\s*(\d+)', current_memo)
    total_work_minutes = int(total_work_match.group(1)) if total_work_match else 0
    
    # 计算任务总时长
    try:
        start_dt = datetime.fromisoformat(task.start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(task.end_time.replace("Z", "+00:00"))
        if start_dt.tzinfo:
            start_dt = start_dt.astimezone().replace(tzinfo=None)
        if end_dt.tzinfo:
            end_dt = end_dt.astimezone().replace(tzinfo=None)
        task_duration_minutes = int((end_dt - start_dt).total_seconds() / 60)
    except:
        task_duration_minutes = 60  # 默认60分钟
    
    # 计算最终进度
    progress = min(100, int((total_work_minutes / task_duration_minutes) * 100)) if task_duration_minutes > 0 else 0
    
    # 更新memo：移除timer_start，保留total_work_minutes和progress
    memo_clean = re.sub(r'\s*\|\s*timer_start:\s*[^|]+\s*', '', current_memo)
    memo_clean = re.sub(r'timer_start:\s*[^|]+\s*', '', memo_clean)
    memo_clean = re.sub(r'\s*\|\s*progress:\s*\d+\s*', '', memo_clean)
    memo_clean = re.sub(r'progress:\s*\d+\s*', '', memo_clean)
    memo_clean = re.sub(r'\s*\|\s*$', '', memo_clean).strip()
    
    if memo_clean:
        new_memo = f"{memo_clean} | total_work_minutes:{total_work_minutes} | progress:{progress}"
    else:
        new_memo = f"total_work_minutes:{total_work_minutes} | progress:{progress}"
    
    repo.update_task(task_id, {"memo": new_memo})
    
    # 如果进度是100%，自动标记为完成
    if progress >= 100:
        repo.update_task(task_id, {"status": "done"})
        return True, f"计时结束，累计工作{total_work_minutes}分钟，进度{progress}%，任务已完成"
    
    return True, f"计时结束，累计工作{total_work_minutes}分钟，进度{progress}%"


def search_tasks_by_keyword(d: date, keyword: str) -> List[Task]:
    """按关键词搜索指定日期的任务。"""
    if not keyword or not keyword.strip():
        return repo.tasks_for_date(d)
    return repo.tasks_by_title(d, keyword.strip())


def get_task_statistics(d: date) -> Dict[str, Any]:
    """获取指定日期的任务统计信息。"""
    tasks = repo.tasks_for_date(d)
    total = len(tasks)
    done = sum(1 for t in tasks if t.status == "done")
    pending = total - done
    high_priority = sum(1 for t in tasks if t.priority == "high")
    
    # 计算总时长（分钟）
    total_minutes = 0
    for t in tasks:
        try:
            start = datetime.fromisoformat(t.start_time.replace("Z", "+00:00"))
            end = datetime.fromisoformat(t.end_time.replace("Z", "+00:00"))
            total_minutes += int((end - start).total_seconds() / 60)
        except Exception:
            pass
    
    return {
        "total": total,
        "done": done,
        "pending": pending,
        "high_priority": high_priority,
        "completion_rate": round(done / total * 100, 1) if total > 0 else 0.0,
        "total_hours": round(total_minutes / 60, 1),
    }


def _resolve_target_task(cmd: CommandIntent, today: date) -> Optional[Task]:
    if cmd.target_task_id:
        return repo.task_by_id(cmd.target_task_id)
    if cmd.target_start_iso:
        t = repo.task_by_start_time(today, cmd.target_start_iso)
        if t:
            return t
    if cmd.title:
        cand = repo.tasks_by_title(today, str(cmd.title), limit=1)
        return cand[0] if cand else None
    return None


def _resolve_target_from_action(action: Dict[str, Any], today: date) -> Optional[Task]:
    """支持按 id / 时间描述 / 标题关键词定位目标任务。"""
    if action.get("target_task_id"):
        return repo.task_by_id(str(action["target_task_id"]))
    # 时间描述 -> ISO（复用 nlp 的行为：这里直接拼接/匹配）
    target_start_iso = action.get("target_start_iso")
    if not target_start_iso and action.get("target_time_desc"):
        # 让 nlp 做同样的时间描述解析：用 parse_user_command 的内部逻辑太重，这里采用简化：交给 repo.task_by_start_time 的包含匹配
        # action 里若给的是「下午两点」等，模型通常也会给 target_start_iso；没有的话就不强求
        pass
    if target_start_iso:
        t = repo.task_by_start_time(today, target_start_iso)
        if t:
            return t
    # 兜底：部分 LLM 会把目标任务名放在 title，而非 target_title（update/delete/complete 最常见）
    target_title = action.get("target_title") or action.get("title")
    if target_title:
        cand = repo.tasks_by_title(today, str(target_title), limit=1)
        return cand[0] if cand else None
    return None


def _next_start_after_today(tasks: List[Task], today: date) -> datetime:
    """自动计划：若用户没给开始时间，默认从“现在”或“最后一个任务结束”之后开始。"""
    now = datetime.now().replace(microsecond=0)
    same_day = []
    for t in tasks:
        st = _parse_local_naive(getattr(t, "start_time", ""))
        if st is not None and st.date() == today:
            same_day.append(t)
    if not same_day:
        return datetime.combine(today, datetime.min.time()).replace(hour=9, minute=0, second=0)
    ends = []
    for t in same_day:
        try:
            et = _parse_local_naive(t.end_time)
            if et is not None:
                ends.append(et)
        except Exception:
            pass
    last_end = max(ends) if ends else datetime.combine(today, datetime.min.time()).replace(hour=9, minute=0, second=0)
    return max(now, last_end)


def _find_earliest_free_slot(day_tasks: List[Task], today: date, dur_min: int) -> Tuple[datetime, datetime]:
    """
    从“当前时间”起，找到能放下 dur_min 的最早空闲块。
    规则：
    - 支持跨天查找（24小时规划）
    - 如果今天排不下，自动排到明天
    - 避开所有已有任务（不区分 fixed/pending/done，因为时间轴已占用）
    - 若遇到重叠则跳到该任务结束后再试
    - 使用本地当前时间，与 prompt/任务时间一致
    """
    now = datetime.now().replace(microsecond=0)
    # 从当前时间开始查找（不再限制在9点，支持24小时规划）
    cursor = now
    
    # 获取未来7天的任务（避免无限查找）。
    # 复用调用方已拿到的 today 任务，减少重复查询。
    all_tasks = list(day_tasks or [])
    for i in range(1, 7):
        check_date = today + timedelta(days=i)
        all_tasks.extend(repo.tasks_for_date(check_date))
    
    # 按开始时间排序（本地时间线）
    all_tasks.sort(key=lambda x: _parse_local_naive(x.start_time) or datetime.max)
    
    # 过滤掉已完成的任务（已完成的任务不占用时间轴）
    active_tasks = [t for t in all_tasks if t.status != "done"]
    
    # 查找空闲时间段，最多查找7天
    max_iterations = 1000  # 增加迭代次数以支持跨天查找
    for _ in range(max_iterations):
        end = cursor + timedelta(minutes=dur_min)
        moved = False
        
        # 检查是否与任何任务冲突
        for t in active_tasks:
            ts = _parse_local_naive(t.start_time)
            te = _parse_local_naive(t.end_time)
            if ts is None or te is None:
                continue
            if cursor < te and ts < end:
                # 冲突：游标跳到冲突任务结束
                cursor = max(cursor, te)
                moved = True
                break
        
        if not moved:
            return cursor, end
    
    # 极端情况：如果所有时间段都被占用，直接接到最后一个任务之后
    if active_tasks:
        parsed_ends = [_parse_local_naive(t.end_time) for t in active_tasks]
        parsed_ends = [x for x in parsed_ends if x is not None]
        last_end = max(parsed_ends) if parsed_ends else cursor
        cursor = max(cursor, last_end)
    else:
        # 如果没有任务，从当前时间开始
        cursor = now
    
    end = cursor + timedelta(minutes=dur_min)
    return cursor, end


def _shift_past_fixed(existing: List[Task], start: datetime, dur_min: int, today: date) -> Tuple[datetime, datetime]:
    """避免与锁定任务重叠：若重叠则顺延到锁定任务结束（支持跨天）。"""
    end = start + timedelta(minutes=dur_min)
    # 获取未来7天的锁定任务。优先复用调用方已有的 today 数据。
    fixed = [t for t in (existing or []) if t.is_fixed]
    for i in range(1, 7):
        check_date = today + timedelta(days=i)
        day_tasks_check = repo.tasks_for_date(check_date)
        fixed.extend([t for t in day_tasks_check if t.is_fixed])
    
    # 简单循环最多 50 次避免卡死（增加次数以支持跨天）
    for _ in range(50):
        moved = False
        for t in fixed:
            try:
                fs = _parse_local_naive(t.start_time)
                fe = _parse_local_naive(t.end_time)
            except Exception:
                continue
            if fs is None or fe is None:
                continue
            if start < fe and fs < end:
                start = fe
                end = start + timedelta(minutes=dur_min)
                moved = True
                break
        if not moved:
            break
    return start, end


def _collect_scope_dates_from_actions(actions: List[Dict[str, Any]], fallback: date) -> List[date]:
    dates: List[date] = [fallback]
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            dates.extend(_collect_action_scope_dates(action, fallback))

    uniq: List[date] = []
    seen = set()
    for d in dates:
        if d in seen:
            continue
        seen.add(d)
        uniq.append(d)
    return uniq


def _capture_scope_snapshot(scope_dates: List[date]) -> List[Dict[str, Any]]:
    """
    按日期作用域抓取快照，支持跨日动作链路的补偿回滚。
    """
    rows: List[Dict[str, Any]] = []
    seen_ids = set()
    for d in scope_dates:
        try:
            day_tasks = repo.tasks_for_date(d)
        except Exception:
            day_tasks = []
        for t in day_tasks:
            tid = str(getattr(t, "id", "") or "")
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            rows.append(
                {
                    "id": t.id,
                    "start_time": t.start_time,
                    "end_time": t.end_time,
                    "title": t.title,
                    "priority": t.priority,
                    "status": t.status,
                    "is_fixed": t.is_fixed,
                    "memo": t.memo,
                }
            )
    return rows


def _restore_tasks_from_scope_snapshot(snapshot_payload: List[Dict[str, Any]], scope_dates: List[date]) -> int:
    """
    补偿式回滚（作用域版）：恢复 scope_dates 涉及日期上的任务状态。
    """
    restored = 0
    snapshot_ids = set()
    for row in (snapshot_payload or []):
        rid = row.get("id")
        if rid:
            snapshot_ids.add(str(rid))

    merged_scope: List[date] = []
    seen_scope = set()
    for d in (scope_dates or []):
        if d in seen_scope:
            continue
        seen_scope.add(d)
        merged_scope.append(d)

    for row in (snapshot_payload or []):
        for key in ("start_time", "end_time"):
            raw = row.get(key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            local_d = _parse_dt_local_date(raw)
            if local_d is not None and local_d not in seen_scope:
                seen_scope.add(local_d)
                merged_scope.append(local_d)

    for d in merged_scope:
        try:
            day_tasks = repo.tasks_for_date(d)
        except Exception:
            day_tasks = []
        for t in day_tasks:
            tid = str(getattr(t, "id", "") or "")
            if not tid:
                continue
            if tid not in snapshot_ids:
                repo.delete_task(tid)

    restored_ids = set()
    for row in (snapshot_payload or []):
        tid = row.get("id")
        if not tid:
            continue
        tid_s = str(tid)
        if tid_s in restored_ids:
            continue
        payload = {}
        for k in ("start_time", "end_time", "priority", "status", "is_fixed", "memo"):
            if k in row:
                payload[k] = row.get(k)
        if not payload:
            continue
        repo.update_task(tid, payload)
        restored_ids.add(tid_s)
        restored += 1
    return restored


def _restore_tasks_from_snapshot(snapshot_payload: List[Dict[str, Any]], today: date) -> int:
    """
    补偿式回滚：将快照中的任务恢复到写入前状态。
    说明：这不是数据库原生事务，但可避免执行失败后的“半成功”中间态。
    """
    return _restore_tasks_from_scope_snapshot(snapshot_payload, [today])


def _normalize_action_time_fields_for_validation(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    复制 action 并将时间字段统一归一到 UTC ISO，避免 validate_plan 因本地时间字符串产生误判。
    """
    if not isinstance(action, dict):
        return action
    normalized = dict(action)
    for key in ("new_start_iso", "new_end_iso", "start_iso", "end_iso", "start_time", "end_time"):
        value = normalized.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = _local_iso_to_utc_iso(value.strip())
    return normalized


def _collect_action_scope_dates(action: Dict[str, Any], fallback: date) -> List[date]:
    """
    推导 action 影响范围日期：
    - 包含 fallback（通常为 today）
    - 包含 action 开始/结束字段对应的 UTC 日期
    """
    dates: List[date] = [fallback]
    if not isinstance(action, dict):
        return dates
    for key in ("new_start_iso", "start_iso", "start_time", "new_end_iso", "end_iso", "end_time"):
        raw = action.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            local_d = _parse_dt_local_date(raw.strip(), naive_is_local=True)
            if local_d is not None:
                dates.append(local_d)
                continue
            utc_iso = _local_iso_to_utc_iso(raw.strip())
            local_d2 = _parse_dt_local_date(utc_iso)
            if local_d2 is not None:
                dates.append(local_d2)
                continue
            dates.append(date.fromisoformat(utc_iso[:10]))
        except Exception:
            try:
                dates.append(date.fromisoformat(raw[:10]))
            except Exception:
                continue
    uniq: List[date] = []
    seen = set()
    for d in dates:
        if d in seen:
            continue
        seen.add(d)
        uniq.append(d)
    return uniq


def _tasks_for_action_scope(action: Dict[str, Any], fallback: date) -> List[Task]:
    """
    拉取 action 相关日期的任务快照（去重），用于执行前后验收。
    """
    all_tasks: List[Task] = []
    seen_ids = set()
    for d in _collect_action_scope_dates(action, fallback):
        try:
            day_tasks = repo.tasks_for_date(d)
        except Exception:
            day_tasks = []
        for t in day_tasks:
            tid = str(getattr(t, "id", "") or "")
            if tid and tid in seen_ids:
                continue
            if tid:
                seen_ids.add(tid)
            all_tasks.append(t)
    return all_tasks


def _collect_active_intervals_local(tasks: List[Task]) -> List[Tuple[datetime, datetime, Optional[str]]]:
    intervals: List[Tuple[datetime, datetime, Optional[str]]] = []
    for t in (tasks or []):
        if str(getattr(t, "status", "") or "").lower() == "done":
            continue
        st = _parse_local_naive(getattr(t, "start_time", ""))
        et = _parse_local_naive(getattr(t, "end_time", ""))
        if st is None or et is None or et <= st:
            continue
        tid = str(getattr(t, "id", "") or "").strip() or None
        intervals.append((st, et, tid))
    intervals.sort(key=lambda x: x[0])
    return intervals


def _drop_occupied_by_task_id(
    occupied: List[Tuple[datetime, datetime, Optional[str]]],
    task_id: Optional[str],
) -> List[Tuple[datetime, datetime, Optional[str]]]:
    tid = str(task_id or "").strip()
    if not tid:
        return occupied
    return [row for row in occupied if str(row[2] or "") != tid]


def _find_non_conflicting_slot_from_anchor(
    anchor_start: datetime,
    duration_minutes: int,
    occupied: List[Tuple[datetime, datetime, Optional[str]]],
) -> Tuple[datetime, datetime]:
    dur = max(1, int(duration_minutes or 1))
    cursor = anchor_start.replace(microsecond=0)
    for _ in range(2000):
        end = cursor + timedelta(minutes=dur)
        moved = False
        for os, oe, _tid in sorted(occupied, key=lambda x: x[0]):
            if end <= os or oe <= cursor:
                continue
            cursor = max(cursor, oe).replace(microsecond=0)
            moved = True
            break
        if not moved:
            return cursor, end
    return cursor, cursor + timedelta(minutes=dur)


def _dependency_floor_local(
    action: Dict[str, Any],
    day_tasks: List[Task],
    source_node_end: Dict[str, datetime],
) -> Optional[datetime]:
    floor: Optional[datetime] = None
    deps = action.get("depends_on")
    if isinstance(deps, list):
        for dep in deps:
            dep_task = _resolve_dep_task_from_day(str(dep or "").strip(), day_tasks)
            if dep_task is None:
                continue
            dep_end = _parse_local_naive(getattr(dep_task, "end_time", ""))
            if dep_end is None:
                continue
            floor = dep_end if floor is None else max(floor, dep_end)
    parent_ref = str(action.get("resolved_parent_id", "") or "").strip()
    if parent_ref:
        parent_end = source_node_end.get(parent_ref)
        if parent_end is not None:
            floor = parent_end if floor is None else max(floor, parent_end)
    return floor


def _set_action_time_fields(action: Dict[str, Any], start_utc: str, end_utc: str) -> None:
    action["start_iso"] = start_utc
    action["end_iso"] = end_utc
    action["new_start_iso"] = start_utc
    action["new_end_iso"] = end_utc
    expected = action.get("expected_delta")
    if isinstance(expected, dict):
        expected["start_iso"] = start_utc
        expected["end_iso"] = end_utc
        expected["new_start_iso"] = start_utc
        expected["new_end_iso"] = end_utc


def _compile_actions_for_execution(
    actions: List[Dict[str, Any]],
    today: date,
    day_tasks: List[Task],
    now_dt: datetime,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    动作时间编译层（通用）：
    - 在进入预校验/预览前，把动作时间锚点收敛到可执行且无冲突的时间线
    - 不依赖 prompt 规则，仅依赖结构化动作 + 当前任务状态
    """
    if not isinstance(actions, list):
        return actions, 0

    occupied = _collect_active_intervals_local(day_tasks)
    # 若某任务在本轮会被 update，先从占用时间线移除旧时间，避免“先排新任务再重排旧任务”时过度保守。
    future_update_targets = {
        str((a or {}).get("target_task_id") or "").strip()
        for a in actions
        if isinstance(a, dict) and str((a or {}).get("action", "") or "").lower().strip() == "update"
    }
    future_update_targets = {x for x in future_update_targets if x}
    if future_update_targets:
        occupied = [row for row in occupied if str(row[2] or "") not in future_update_targets]

    source_node_end: Dict[str, datetime] = {}
    compiled: List[Dict[str, Any]] = []
    repaired = 0

    for raw in actions:
        if not isinstance(raw, dict):
            continue
        a = dict(raw)
        act = str(a.get("action", "") or "").lower().strip()
        floor_local = _dependency_floor_local(a, day_tasks, source_node_end)

        if act in ("insert", "plan"):
            duration = a.get("duration_minutes")
            try:
                duration_min = int(duration) if duration is not None else 60
            except Exception:
                duration_min = 60
            duration_min = max(1, duration_min)

            start_raw = a.get("start_iso") or a.get("new_start_iso") or a.get("start_time")
            end_raw = a.get("end_iso") or a.get("new_end_iso") or a.get("end_time")
            start_local = _parse_local_naive(str(start_raw).strip()) if start_raw else None
            end_local = _parse_local_naive(str(end_raw).strip()) if end_raw else None

            if start_local and end_local and end_local > start_local:
                duration_min = max(1, int((end_local - start_local).total_seconds() // 60))
            elif start_local and not end_local:
                end_local = start_local + timedelta(minutes=duration_min)
            elif end_local and not start_local:
                start_local = end_local - timedelta(minutes=duration_min)
            else:
                start_local = (floor_local or now_dt).replace(microsecond=0)
                end_local = start_local + timedelta(minutes=duration_min)

            if floor_local is not None and start_local < floor_local:
                start_local = floor_local.replace(microsecond=0)
                end_local = start_local + timedelta(minutes=duration_min)

            target_tid = str(a.get("target_task_id", "") or "").strip()
            if target_tid:
                occupied = _drop_occupied_by_task_id(occupied, target_tid)
            slot_start, slot_end = _find_non_conflicting_slot_from_anchor(start_local, duration_min, occupied)

            old_start = str(a.get("start_iso") or a.get("new_start_iso") or a.get("start_time") or "")
            old_end = str(a.get("end_iso") or a.get("new_end_iso") or a.get("end_time") or "")
            start_utc = _local_naive_to_utc_iso(slot_start)
            end_utc = _local_naive_to_utc_iso(slot_end)
            _set_action_time_fields(a, start_utc, end_utc)
            a["duration_minutes"] = duration_min

            if old_start != start_utc or old_end != end_utc:
                repaired += 1

            occupied.append((slot_start, slot_end, target_tid or None))
            occupied.sort(key=lambda x: x[0])
            src_id = str(a.get("source_node_id", "") or "").strip()
            if src_id:
                source_node_end[src_id] = slot_end

        elif act == "update":
            target_tid = str(a.get("target_task_id", "") or "").strip()
            if target_tid:
                occupied = _drop_occupied_by_task_id(occupied, target_tid)

            start_raw = a.get("new_start_iso") or a.get("start_iso") or a.get("start_time")
            end_raw = a.get("new_end_iso") or a.get("end_iso") or a.get("end_time")
            start_local = _parse_local_naive(str(start_raw).strip()) if start_raw else None
            end_local = _parse_local_naive(str(end_raw).strip()) if end_raw else None
            if start_local and end_local and end_local > start_local:
                duration_min = max(1, int((end_local - start_local).total_seconds() // 60))
                if floor_local is not None and start_local < floor_local:
                    start_local = floor_local.replace(microsecond=0)
                    end_local = start_local + timedelta(minutes=duration_min)
                slot_start, slot_end = _find_non_conflicting_slot_from_anchor(start_local, duration_min, occupied)

                old_start = str(a.get("new_start_iso") or a.get("start_iso") or a.get("start_time") or "")
                old_end = str(a.get("new_end_iso") or a.get("end_iso") or a.get("end_time") or "")
                start_utc = _local_naive_to_utc_iso(slot_start)
                end_utc = _local_naive_to_utc_iso(slot_end)
                _set_action_time_fields(a, start_utc, end_utc)
                if old_start != start_utc or old_end != end_utc:
                    repaired += 1

                occupied.append((slot_start, slot_end, target_tid or None))
                occupied.sort(key=lambda x: x[0])
                src_id = str(a.get("source_node_id", "") or "").strip()
                if src_id:
                    source_node_end[src_id] = slot_end
            elif target_tid:
                # 仅更新非时间字段时，保留目标任务原占用，避免后续动作误判该时段为空。
                target = next((t for t in day_tasks if str(getattr(t, "id", "") or "") == target_tid), None)
                if target is not None and str(getattr(target, "status", "") or "").lower() != "done":
                    ts = _parse_local_naive(getattr(target, "start_time", ""))
                    te = _parse_local_naive(getattr(target, "end_time", ""))
                    if ts is not None and te is not None and te > ts:
                        occupied.append((ts, te, target_tid))
                        occupied.sort(key=lambda x: x[0])

        elif act in ("delete", "complete"):
            target_tid = str(a.get("target_task_id", "") or "").strip()
            if target_tid:
                occupied = _drop_occupied_by_task_id(occupied, target_tid)

        compiled.append(a)

    return compiled, repaired


def _extract_action_date(action: Dict[str, Any], fallback: date) -> str:
    """从 action 中提取日期（YYYY-MM-DD），提取失败则回退到 fallback。"""
    if not isinstance(action, dict):
        return fallback.isoformat()
    for key in ("new_start_iso", "start_iso", "start_time", "new_end_iso", "end_iso", "end_time"):
        v = action.get(key)
        if isinstance(v, str) and len(v) >= 10:
            local_d = _parse_dt_local_date(v, naive_is_local=True)
            if local_d is not None:
                return local_d.isoformat()
            try:
                return date.fromisoformat(v[:10]).isoformat()
            except Exception:
                continue
    return fallback.isoformat()


def _extract_desired_date(item: Dict[str, Any], fallback: date) -> str:
    """从 desired_state item 中提取日期（YYYY-MM-DD），失败回退 fallback。"""
    if not isinstance(item, dict):
        return fallback.isoformat()
    for key in ("start_time", "start_iso", "new_start_iso", "end_time", "end_iso", "new_end_iso"):
        v = item.get(key)
        if isinstance(v, str) and len(v) >= 10:
            local_d = _parse_dt_local_date(v, naive_is_local=True)
            if local_d is not None:
                return local_d.isoformat()
            try:
                return date.fromisoformat(v[:10]).isoformat()
            except Exception:
                continue
    return fallback.isoformat()


def _verify_desired_state(desired_state: List[Dict[str, Any]], fallback_date: date) -> Tuple[bool, str, int]:
    """
    执行后闭环校验：
    - 按日期分组比较“实际任务”与“目标态”
    - 若仍有差异，返回摘要供用户决策下一轮补救
    """
    if not isinstance(desired_state, list) or not desired_state:
        return True, "", 0

    from logic import compute_state_diff

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in desired_state:
        if not isinstance(item, dict):
            continue
        d = _extract_desired_date(item, fallback_date)
        grouped.setdefault(d, []).append(item)

    total_mismatch = 0
    examples: List[str] = []
    for d, items in grouped.items():
        try:
            d_obj = date.fromisoformat(d)
        except Exception:
            d_obj = fallback_date
        current = repo.tasks_for_date(d_obj)
        pending_actions = compute_state_diff(current, items, allow_delete=False)
        total_mismatch += len(pending_actions)
        for a in pending_actions[:3]:
            act = a.get("action", "unknown")
            title = a.get("title") or a.get("target_task_id") or "未命名"
            examples.append(f"{d} {act}:{title}")

    if total_mismatch == 0:
        return True, "", 0

    msg = f"目标态校验发现 {total_mismatch} 处偏差。"
    if examples:
        msg += " 示例：" + "；".join(examples[:3])
    return False, msg, total_mismatch


def _attempt_autofix_desired_state(
    desired_state: List[Dict[str, Any]],
    fallback_date: date,
    max_actions: int = 2,
    allow_insert: bool = False,
) -> Tuple[bool, str, int]:
    """
    自动补救（低风险策略）：
    - 限制动作数量（默认最多 2）
    - 默认仅允许 update（insert 需显式开启）
    - 若检测到冲突则不自动执行，交还预览确认链路
    返回：(是否成功执行或可安全跳过, 描述信息, 实际执行动作数)
    """
    if not isinstance(desired_state, list) or not desired_state:
        return True, "无目标态，跳过自动补救。", 0

    from logic import compute_state_diff

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in desired_state:
        if not isinstance(item, dict):
            continue
        d = _extract_desired_date(item, fallback_date)
        grouped.setdefault(d, []).append(item)

    candidate_actions: List[Dict[str, Any]] = []
    for d in sorted(grouped.keys()):
        items = grouped.get(d) or []
        try:
            d_obj = date.fromisoformat(d)
        except Exception:
            d_obj = fallback_date
        current = repo.tasks_for_date(d_obj)
        candidate_actions.extend(compute_state_diff(current, items, allow_delete=False))

    if not candidate_actions:
        return True, "自动补救检查：无待补救动作。", 0

    if len(candidate_actions) > max_actions:
        return False, f"自动补救未执行：需要 {len(candidate_actions)} 个动作，超过上限 {max_actions}。", 0

    for a in candidate_actions:
        act = str(a.get("action", "")).lower().strip()
        if act == "update":
            continue
        if act == "insert" and allow_insert:
            continue
        return False, f"自动补救未执行：包含高风险动作 {act}。", 0

    ok, err_msg, conflict_count = _prevalidate_actions(candidate_actions, fallback_date)
    if not ok:
        return False, f"自动补救未执行：{err_msg}", 0
    if conflict_count > 0:
        return False, f"自动补救未执行：检测到 {conflict_count} 处冲突（需人工确认）。", 0

    executed = 0
    for a in candidate_actions:
        act = str(a.get("action", "")).lower().strip()
        if act == "update":
            tid = a.get("target_task_id")
            if not tid:
                raise RuntimeError("自动补救 update 缺少 target_task_id")
            payload: Dict[str, Any] = {}

            if a.get("new_start_iso") is not None:
                payload["start_time"] = _local_iso_to_utc_iso(str(a.get("new_start_iso")))
            if a.get("new_end_iso") is not None:
                payload["end_time"] = _local_iso_to_utc_iso(str(a.get("new_end_iso")))
            if a.get("title") is not None:
                payload["title"] = str(a.get("title"))
            if a.get("priority") in ("high", "normal", "low"):
                payload["priority"] = a.get("priority")
            if a.get("status") in ("pending", "done"):
                payload["status"] = a.get("status")
            if isinstance(a.get("is_fixed"), bool):
                payload["is_fixed"] = a.get("is_fixed")
            if a.get("memo") is not None:
                payload["memo"] = a.get("memo")

            if not payload:
                continue
            updated = repo.update_task(str(tid), payload)
            if updated is None:
                raise RuntimeError(f"自动补救 update 失败: {tid}")
            executed += 1
            continue

        if act == "insert" and allow_insert:
            title = str(a.get("title") or "").strip()
            if not title:
                raise RuntimeError("自动补救 insert 缺少 title")
            start_iso = a.get("start_iso") or a.get("new_start_iso")
            end_iso = a.get("end_iso") or a.get("new_end_iso")
            if not start_iso or not end_iso:
                raise RuntimeError("自动补救 insert 缺少 start/end")
            t = Task(
                id=None,
                start_time=_local_iso_to_utc_iso(str(start_iso)),
                end_time=_local_iso_to_utc_iso(str(end_iso)),
                title=title,
                priority=str(a.get("priority") or "normal"),
                status=str(a.get("status") or "pending"),
                is_fixed=bool(a.get("is_fixed", False)),
                memo=a.get("memo"),
            )
            repo.insert_task(t)
            executed += 1

    return True, f"已自动补救 {executed} 处偏差（跨日可处理）。", executed


def _reconcile_desired_state(
    desired_state: List[Dict[str, Any]],
    fallback_date: date,
    max_rounds: int,
    max_actions: int,
    allow_insert: bool,
) -> Tuple[bool, str, int]:
    """
    目标态收敛闭环：
    verify -> (autofix -> verify)*N
    """
    rounds = max(0, int(max_rounds))
    total_fixed = 0
    last_verify_msg = ""
    for attempt in range(rounds + 1):
        ok, verify_msg, mismatch_count = _verify_desired_state(desired_state, fallback_date)
        if ok:
            if total_fixed > 0:
                return True, f"目标态已收敛（自动补救 {total_fixed} 处）。", total_fixed
            return True, "", 0
        last_verify_msg = verify_msg
        if mismatch_count <= 0:
            return False, verify_msg, total_fixed
        if attempt >= rounds:
            break
        auto_ok, auto_msg, auto_count = _attempt_autofix_desired_state(
            desired_state=desired_state,
            fallback_date=fallback_date,
            max_actions=max_actions,
            allow_insert=allow_insert,
        )
        if auto_count > 0:
            total_fixed += auto_count
        if not auto_ok and auto_count == 0:
            return False, f"{verify_msg} 自动补救未执行：{auto_msg}", total_fixed
    if total_fixed > 0:
        return False, f"{last_verify_msg}（已自动补救 {total_fixed} 处但仍未收敛）", total_fixed
    return False, last_verify_msg, 0


def _has_reliable_action_anchor(action: Dict[str, Any]) -> bool:
    if not isinstance(action, dict):
        return False
    act = str(action.get("action", "")).lower().strip()
    if act == "query":
        return True
    if action.get("target_task_id"):
        return True
    if action.get("id"):
        return True
    if action.get("depends_on"):
        return True
    if action.get("start_iso") and action.get("end_iso"):
        return True
    if action.get("new_start_iso") and action.get("new_end_iso"):
        return True
    if action.get("start_time") and action.get("end_time"):
        return True
    return False


def _legacy_result_is_safe_after_protocol_failure(cmd: CommandIntent) -> bool:
    """
    Agent 协议失败后，对 legacy 结果做保守门控：
    - 有 desired_state，或
    - 存在 need_clarification，或
    - 每个写动作都具备可落地锚点（target_task_id / 显式时间 / depends_on）
    """
    if not isinstance(cmd, CommandIntent):
        return False
    if cmd.need_clarification:
        return True
    desired_state = getattr(cmd, "desired_state", None)
    if isinstance(desired_state, list) and desired_state:
        return True
    actions = cmd.actions if isinstance(cmd.actions, list) else []
    if not actions:
        return True
    for a in actions:
        if not _has_reliable_action_anchor(a):
            return False
    return True


def _is_write_command(cmd: CommandIntent) -> bool:
    intent = str(getattr(cmd, "intent", "") or "").lower().strip()
    if intent in ("insert", "update", "delete", "plan", "replan_all"):
        return True
    actions = getattr(cmd, "actions", None)
    if isinstance(actions, list):
        for a in actions:
            if not isinstance(a, dict):
                continue
            act = str(a.get("action", "") or "").lower().strip()
            if act in ("insert", "update", "delete", "complete", "plan"):
                return True
    return False


def _derive_semantic_contract_from_actions(actions: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """
    为非 desired_state 路径派生最小语义契约，用于执行后目标态校验。
    仅覆盖 insert/plan/update；包含 delete/complete 时返回 None（避免误判）。
    """
    if not isinstance(actions, list) or not actions:
        return None
    contract: List[Dict[str, Any]] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        act = str(a.get("action", "")).lower().strip()
        if act in ("delete", "complete"):
            return None
        if act in ("insert", "plan"):
            title = str(a.get("title", "") or "").strip()
            if not title:
                continue
            item: Dict[str, Any] = {"title": title}
            start_v = a.get("start_iso") or a.get("new_start_iso") or a.get("start_time")
            end_v = a.get("end_iso") or a.get("new_end_iso") or a.get("end_time")
            if isinstance(start_v, str) and start_v.strip():
                item["start_iso"] = start_v
            if isinstance(end_v, str) and end_v.strip():
                item["end_iso"] = end_v
            if a.get("priority") in ("high", "normal", "low"):
                item["priority"] = a.get("priority")
            contract.append(item)
            continue
        if act == "update":
            tid = a.get("target_task_id")
            if not tid:
                continue
            item = {"id": str(tid)}
            if a.get("title") is not None:
                item["title"] = a.get("title")
            if a.get("new_start_iso") is not None:
                item["start_iso"] = a.get("new_start_iso")
            if a.get("new_end_iso") is not None:
                item["end_iso"] = a.get("new_end_iso")
            if a.get("priority") is not None:
                item["priority"] = a.get("priority")
            if a.get("status") is not None:
                item["status"] = a.get("status")
            if a.get("memo") is not None:
                item["memo"] = a.get("memo")
            if isinstance(a.get("is_fixed"), bool):
                item["is_fixed"] = a.get("is_fixed")
            contract.append(item)
            continue
        # 未知动作不纳入严格语义契约
        return None
    return contract or None


def _prevalidate_actions(actions: List[Dict[str, Any]], today: date) -> Tuple[bool, str, int]:
    """
    执行前校验：
    - 只把硬错误（无效 UUID/时间格式/结束早于开始）作为阻断条件
    - 冲突仅记录告警，不阻断（后续由多米诺引擎处理）
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for a in actions:
        if not isinstance(a, dict):
            continue
        normalized = _normalize_action_time_fields_for_validation(a)
        d = _extract_action_date(normalized, today)
        grouped.setdefault(d, []).append(normalized)

    hard_errors: List[str] = []
    total_conflicts = 0
    for d, acts in grouped.items():
        try:
            d_obj = date.fromisoformat(d)
        except Exception:
            hard_errors.append(f"{d}: 无效日期")
            continue
        tasks = repo.tasks_for_date(d_obj)
        active = [t for t in tasks if str(getattr(t, "status", "pending")).lower() != "done"]
        id_set = {str(getattr(t, "id", "")) for t in tasks}

        for idx, action in enumerate(acts):
            i = idx + 1
            if not isinstance(action, dict):
                hard_errors.append(f"{d}: action[{i}] 不是对象")
                continue

            target_task_id = action.get("target_task_id")
            if target_task_id and str(target_task_id) not in id_set:
                hard_errors.append(f"{d}: action[{i}] target_task_id 不存在: {target_task_id}")

            start_raw = action.get("new_start_iso") or action.get("start_iso") or action.get("start_time")
            end_raw = action.get("new_end_iso") or action.get("end_iso") or action.get("end_time")

            if bool(start_raw) != bool(end_raw):
                hard_errors.append(f"{d}: action[{i}] 时间字段不完整（start/end 需同时提供）")
                continue
            if not start_raw and not end_raw:
                continue

            start_dt = _parse_dt_naive(str(start_raw))
            end_dt = _parse_dt_naive(str(end_raw))
            if end_dt <= start_dt:
                hard_errors.append(f"{d}: action[{i}] 结束时间必须晚于开始时间")
                continue

            for t in active:
                tid = str(getattr(t, "id", ""))
                if target_task_id and tid == str(target_task_id):
                    continue
                ts = _parse_dt_naive(getattr(t, "start_time", ""))
                te = _parse_dt_naive(getattr(t, "end_time", ""))
                if start_dt < te and ts < end_dt:
                    total_conflicts += 1

    if hard_errors:
        msg = "计划校验未通过（硬错误）：\n" + "\n".join(f"- {e}" for e in hard_errors[:8])
        if len(hard_errors) > 8:
            msg += f"\n- ... 其余 {len(hard_errors) - 8} 条错误已省略"
        return False, msg, total_conflicts
    return True, "", total_conflicts


def _resolve_dep_task_from_day(dep_ref: str, day_tasks: List[Task]) -> Optional[Task]:
    """解析依赖引用：支持完整 UUID 与唯一短前缀。"""
    if not dep_ref:
        return None
    dep_ref = str(dep_ref).strip()
    exact = [t for t in day_tasks if str(getattr(t, "id", "")) == dep_ref]
    if exact:
        return exact[0]
    short = [t for t in day_tasks if str(getattr(t, "id", "")).startswith(dep_ref)]
    if len(short) == 1:
        return short[0]
    return None


def _has_explicit_update_mutation(action: Dict[str, Any]) -> bool:
    """update 动作必须携带至少一个可执行变更字段。"""
    keys = (
        "delta_minutes",
        "duration_minutes",
        "new_start_iso",
        "new_end_iso",
        "start_iso",
        "end_iso",
        "title",
        "priority",
        "status",
        "memo",
        "is_fixed",
    )
    return any(action.get(k) is not None for k in keys)


def _validate_single_action_step(action: Dict[str, Any], today: date, day_tasks: List[Task]) -> Tuple[bool, str]:
    """
    单步闭环 - 执行前验证：
    1) 依赖必须可解析
    2) 依赖顺序必须成立（若已给时间）
    3) 通过既有 validate_plan 硬校验
    """
    if not isinstance(action, dict):
        return False, "action 不是对象"
    act = str(action.get("action", "")).lower().strip()

    if act in ("update", "delete", "complete"):
        target_hint = action.get("target_task_id") or action.get("target_title") or action.get("target_start_iso")
        if not target_hint:
            return False, f"{act} 缺少目标标识（target_task_id/target_title/target_start_iso）。"
    if act == "update" and not _has_explicit_update_mutation(action):
        return False, "update 缺少明确变更字段（如 new_start_iso/new_end_iso/delta_minutes/duration_minutes）。"

    unresolved = action.get("unresolved_dependency")
    if unresolved:
        return False, f"依赖未解析：{unresolved}"

    deps = action.get("depends_on")
    deps_list = [str(x).strip() for x in deps] if isinstance(deps, list) else []
    start_raw = action.get("new_start_iso") or action.get("start_iso") or action.get("start_time")
    end_raw = action.get("new_end_iso") or action.get("end_iso") or action.get("end_time")

    if deps_list:
        if not start_raw or not end_raw:
            return False, "存在依赖但缺少开始/结束时间锚点。"
        start_dt = _parse_dt_naive(str(start_raw))
        for dep in deps_list:
            dep_task = _resolve_dep_task_from_day(dep, day_tasks)
            if dep_task is None:
                return False, f"依赖任务不存在或不唯一：{dep}"
            dep_end = _parse_dt_naive(dep_task.end_time)
            if start_dt < dep_end:
                return False, f"依赖顺序不满足：当前动作开始早于依赖任务结束（{dep_task.title}）"

    ok, msg, conflict_count = _prevalidate_actions([action], today)
    if not ok:
        return False, msg
    if deps_list and conflict_count > 0:
        return False, f"含依赖动作检测到 {conflict_count} 处冲突，请先预览确认。"
    return True, ""


def _task_state_signature(tasks: List[Task]) -> Dict[str, Tuple[str, str, str, str, str, str, bool]]:
    sig: Dict[str, Tuple[str, str, str, str, str, str, bool]] = {}
    for t in tasks:
        tid = str(getattr(t, "id", "") or "")
        if not tid:
            continue
        sig[tid] = (
            str(getattr(t, "title", "") or ""),
            str(getattr(t, "start_time", "") or ""),
            str(getattr(t, "end_time", "") or ""),
            str(getattr(t, "status", "") or ""),
            str(getattr(t, "priority", "") or ""),
            str(getattr(t, "memo", "") or ""),
            bool(getattr(t, "is_fixed", False)),
        )
    return sig


def _verify_single_action_step_effect(
    action: Dict[str, Any],
    before_tasks: List[Task],
    after_tasks: List[Task],
) -> Tuple[bool, str]:
    """
    单步闭环 - 执行后验证：
    用结构化状态变化校验当前 action 是否生效。
    """
    act = str(action.get("action", "")).lower().strip()
    before_sig = _task_state_signature(before_tasks)
    after_sig = _task_state_signature(after_tasks)
    before_ids = set(before_sig.keys())
    after_ids = set(after_sig.keys())
    expected_delta = action.get("expected_delta") if isinstance(action.get("expected_delta"), dict) else {}

    def _as_utc_naive(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        try:
            # 约定：无时区字符串按“本地时间语义”处理，再转 UTC 比较
            if "+" in s or s.endswith("Z"):
                return _parse_dt_naive(s)
            return _parse_dt_naive(_local_iso_to_utc_iso(s))
        except Exception:
            return None

    def _same_instant(a: Any, b: Any) -> bool:
        # 通道1：按原始解析直接比较（兼容历史链路里“本地无时区 vs +00:00 同时刻字符串”）
        try:
            da_raw = _parse_dt_naive(str(a))
            db_raw = _parse_dt_naive(str(b))
            if da_raw == db_raw:
                return True
        except Exception:
            pass

        # 通道2：将无时区值按本地语义转 UTC 再比较（严格语义）
        da = _as_utc_naive(a)
        db = _as_utc_naive(b)
        return da is not None and db is not None and da == db

    if act in ("insert", "plan"):
        title = str(action.get("title", "") or expected_delta.get("title") or "").strip()
        expected_start = (
            expected_delta.get("new_start_iso")
            or expected_delta.get("start_iso")
            or expected_delta.get("start_time")
            or action.get("new_start_iso")
            or action.get("start_iso")
        )
        expected_end = (
            expected_delta.get("new_end_iso")
            or expected_delta.get("end_iso")
            or expected_delta.get("end_time")
            or action.get("new_end_iso")
            or action.get("end_iso")
        )
        new_ids = list(after_ids - before_ids)
        if title:
            created_with_title = [
                t for t in after_tasks
                if str(getattr(t, "id", "")) in new_ids and (getattr(t, "title", "") or "").strip() == title
            ]
            if created_with_title:
                if expected_start or expected_end:
                    for t in created_with_title:
                        start_ok = (not expected_start) or _same_instant(getattr(t, "start_time", ""), expected_start)
                        end_ok = (not expected_end) or _same_instant(getattr(t, "end_time", ""), expected_end)
                        if start_ok and end_ok:
                            return True, ""
                    return False, "执行后新增任务的时间与预期不一致。"
                return True, ""
        # plan 可能是更新已有任务（无新增）
        if title:
            for tid in (before_ids & after_ids):
                if before_sig[tid] != after_sig[tid]:
                    t = next((x for x in after_tasks if str(getattr(x, "id", "")) == tid), None)
                    if t and (getattr(t, "title", "") or "").strip() == title:
                        return True, ""
        # 没 title 时，只要状态发生变化也算成功
        if before_sig != after_sig:
            return True, ""
        return False, "执行后未观察到插入/计划带来的状态变化。"

    if act == "update":
        tid = str(action.get("target_task_id", "") or expected_delta.get("target_task_id") or "").strip()
        if not tid:
            return False, "执行后校验缺少 target_task_id。"
        if tid not in before_sig or tid not in after_sig:
            return False, "执行后目标任务不存在或被意外删除。"
        after_task = next((x for x in after_tasks if str(getattr(x, "id", "")) == tid), None)
        if after_task is None:
            return False, "执行后无法定位目标任务。"

        expected_start = (
            expected_delta.get("new_start_iso")
            or expected_delta.get("start_iso")
            or expected_delta.get("start_time")
            or action.get("new_start_iso")
            or action.get("start_iso")
        )
        expected_end = (
            expected_delta.get("new_end_iso")
            or expected_delta.get("end_iso")
            or expected_delta.get("end_time")
            or action.get("new_end_iso")
            or action.get("end_iso")
        )
        expected_title = expected_delta.get("title") if expected_delta.get("title") is not None else action.get("title")
        expected_priority = expected_delta.get("priority") if expected_delta.get("priority") is not None else action.get("priority")
        expected_status = expected_delta.get("status") if expected_delta.get("status") is not None else action.get("status")
        expected_memo = expected_delta.get("memo") if expected_delta.get("memo") is not None else action.get("memo")
        expected_is_fixed = expected_delta.get("is_fixed") if expected_delta.get("is_fixed") is not None else action.get("is_fixed")
        if before_sig[tid] == after_sig[tid]:
            # 幂等更新：若状态本来就满足期望，不应当被判失败。
            if expected_start is not None and not _same_instant(getattr(after_task, "start_time", ""), expected_start):
                return False, "执行后目标任务未发生变化。"
            if expected_end is not None and not _same_instant(getattr(after_task, "end_time", ""), expected_end):
                return False, "执行后目标任务未发生变化。"
            if expected_title is not None and str(getattr(after_task, "title", "") or "") != str(expected_title):
                return False, "执行后目标任务未发生变化。"
            if expected_priority is not None and str(getattr(after_task, "priority", "") or "") != str(expected_priority):
                return False, "执行后目标任务未发生变化。"
            if expected_status is not None and str(getattr(after_task, "status", "") or "") != str(expected_status):
                return False, "执行后目标任务未发生变化。"
            if expected_memo is not None and (getattr(after_task, "memo", None) != expected_memo):
                return False, "执行后目标任务未发生变化。"
            if isinstance(expected_is_fixed, bool) and bool(getattr(after_task, "is_fixed", False)) != bool(expected_is_fixed):
                return False, "执行后目标任务未发生变化。"
            return True, ""
        if expected_start is not None and not _same_instant(getattr(after_task, "start_time", ""), expected_start):
            return False, "执行后开始时间与预期不一致。"
        if expected_end is not None and not _same_instant(getattr(after_task, "end_time", ""), expected_end):
            return False, "执行后结束时间与预期不一致。"
        if expected_title is not None and str(getattr(after_task, "title", "") or "") != str(expected_title):
            return False, "执行后标题与预期不一致。"
        if expected_priority is not None and str(getattr(after_task, "priority", "") or "") != str(expected_priority):
            return False, "执行后优先级与预期不一致。"
        if expected_status is not None and str(getattr(after_task, "status", "") or "") != str(expected_status):
            return False, "执行后状态与预期不一致。"
        if expected_memo is not None and (getattr(after_task, "memo", None) != expected_memo):
            return False, "执行后备注与预期不一致。"
        if isinstance(expected_is_fixed, bool) and bool(getattr(after_task, "is_fixed", False)) != bool(expected_is_fixed):
            return False, "执行后锁定状态与预期不一致。"
        return True, ""

    if act == "delete":
        tid = str(
            action.get("target_task_id", "")
            or expected_delta.get("deleted_task_id")
            or expected_delta.get("target_task_id")
            or ""
        ).strip()
        if not tid:
            return False, "执行后校验缺少 target_task_id。"
        if tid in after_ids:
            return False, "执行后目标任务仍存在，删除未生效。"
        return True, ""

    if act == "complete":
        tid = str(action.get("target_task_id", "") or expected_delta.get("target_task_id") or "").strip()
        if not tid:
            return False, "执行后校验缺少 target_task_id。"
        t = next((x for x in after_tasks if str(getattr(x, "id", "")) == tid), None)
        expected_status = str(expected_delta.get("status", "done")).lower()
        if t and str(getattr(t, "status", "")).lower() == expected_status:
            return True, ""
        if tid in before_sig and tid in after_sig and before_sig[tid] != after_sig[tid]:
            return True, ""
        return False, "执行后未观察到目标任务的完成状态变化。"

    # 其他动作：只要状态有变化即认为生效
    if before_sig != after_sig:
        return True, ""
    return False, f"未知动作 {act} 未观察到状态变化。"


def _attempt_reach_expected_state(
    action: Dict[str, Any],
    today: date,
    after_tasks: List[Task],
) -> Tuple[bool, str]:
    """
    当后验不满足时，尝试一次确定性修复写入，使状态逼近期望。
    成功后仍需再次走 _verify_single_action_step_effect 验收。
    """
    if not isinstance(action, dict):
        return False, "action 非法"
    act = str(action.get("action", "")).lower().strip()
    expected = action.get("expected_delta") if isinstance(action.get("expected_delta"), dict) else {}

    def _pick(*keys: str):
        for k in keys:
            v = expected.get(k)
            if v is not None:
                return v
        for k in keys:
            v = action.get(k)
            if v is not None:
                return v
        return None

    if act == "update":
        tid = str(_pick("target_task_id") or action.get("target_task_id") or "").strip()
        if not tid:
            return False, "update 修复缺少目标任务"
        payload: Dict[str, Any] = {}
        start_v = _pick("new_start_iso", "start_iso", "start_time")
        end_v = _pick("new_end_iso", "end_iso", "end_time")
        if isinstance(start_v, str) and start_v.strip():
            payload["start_time"] = _local_iso_to_utc_iso(start_v)
        if isinstance(end_v, str) and end_v.strip():
            payload["end_time"] = _local_iso_to_utc_iso(end_v)
        title_v = _pick("title")
        if title_v is not None:
            payload["title"] = str(title_v)
        pr_v = _pick("priority")
        if pr_v in ("high", "normal", "low"):
            payload["priority"] = pr_v
        st_v = _pick("status")
        if st_v in ("pending", "done"):
            payload["status"] = st_v
        memo_v = _pick("memo")
        if memo_v is not None:
            payload["memo"] = memo_v
        fixed_v = _pick("is_fixed")
        if isinstance(fixed_v, bool):
            payload["is_fixed"] = fixed_v
        if not payload:
            return False, "update 修复缺少可写入字段"
        updated = repo.update_task(tid, payload)
        if updated is None:
            return False, f"update 修复失败: {tid}"
        return True, "update 修复写入成功"

    if act in ("insert", "plan"):
        title = str(_pick("title") or "").strip()
        start_v = _pick("new_start_iso", "start_iso", "start_time")
        end_v = _pick("new_end_iso", "end_iso", "end_time")
        if not title:
            return False, "insert 修复缺少 title"
        # 已满足期望则无需重复插入
        if isinstance(start_v, str) and isinstance(end_v, str):
            same_title = [t for t in after_tasks if (getattr(t, "title", "") or "").strip() == title]
            for t in after_tasks:
                if (getattr(t, "title", "") or "").strip() != title:
                    continue
                if _parse_dt_naive(getattr(t, "start_time", "")) == _parse_dt_naive(start_v) and _parse_dt_naive(getattr(t, "end_time", "")) == _parse_dt_naive(end_v):
                    return True, "insert 期望已满足"
            # 同标题任务已存在但时间不符：优先纠偏为期望时间，避免重复插入
            if same_title:
                payload = {
                    "start_time": _local_iso_to_utc_iso(start_v),
                    "end_time": _local_iso_to_utc_iso(end_v),
                }
                updated = repo.update_task(str(getattr(same_title[0], "id", "")), payload)
                if updated is not None:
                    return True, "insert 修复改为纠偏已有同标题任务"
        if not (isinstance(start_v, str) and start_v.strip() and isinstance(end_v, str) and end_v.strip()):
            return False, "insert 修复缺少 start/end"
        pr_v = _pick("priority")
        st_v = _pick("status")
        fixed_v = _pick("is_fixed")
        memo_v = _pick("memo")
        t = Task(
            id=None,
            start_time=_local_iso_to_utc_iso(start_v),
            end_time=_local_iso_to_utc_iso(end_v),
            title=title,
            priority=pr_v if pr_v in ("high", "normal", "low") else "normal",
            status=st_v if st_v in ("pending", "done") else "pending",
            is_fixed=bool(fixed_v) if isinstance(fixed_v, bool) else False,
            memo=memo_v,
        )
        repo.insert_task(t)
        return True, "insert 修复写入成功"

    if act == "delete":
        tid = str(_pick("deleted_task_id", "target_task_id") or action.get("target_task_id") or "").strip()
        if not tid:
            return False, "delete 修复缺少目标任务"
        exists = next((t for t in after_tasks if str(getattr(t, "id", "")) == tid), None)
        if exists is None:
            return True, "delete 期望已满足"
        repo.delete_task(tid)
        return True, "delete 修复写入成功"

    if act == "complete":
        tid = str(_pick("target_task_id") or action.get("target_task_id") or "").strip()
        if not tid:
            return False, "complete 修复缺少目标任务"
        status_v = str(_pick("status") or "done").lower()
        payload: Dict[str, Any] = {"status": status_v if status_v in ("pending", "done") else "done"}
        memo_v = _pick("memo")
        if memo_v is not None:
            payload["memo"] = memo_v
        updated = repo.update_task(tid, payload)
        if updated is None:
            return False, f"complete 修复失败: {tid}"
        return True, "complete 修复写入成功"

    return False, f"不支持自动修复的动作类型: {act}"


def _verify_step_with_repair(
    action: Dict[str, Any],
    before_tasks: List[Task],
    after_tasks: List[Task],
    today: date,
) -> Tuple[bool, str, List[Task]]:
    """
    先做后验；不满足则尝试修复并重试，直到达到最大重试次数。
    """
    max_retries = max(0, STATE_RECONCILE_MAX_RETRIES)
    latest_tasks = after_tasks
    last_msg = ""

    for attempt in range(max_retries + 1):
        ok, msg = _verify_single_action_step_effect(action, before_tasks, latest_tasks)
        if ok:
            return True, "", latest_tasks
        last_msg = msg or "后验未通过。"
        if attempt >= max_retries:
            break

        repaired, repair_msg = _attempt_reach_expected_state(action, today, latest_tasks)
        if repaired:
            log.warning("%s step verify mismatch repaired (attempt=%d): %s", LOG_VERIFY, attempt + 1, repair_msg)
        else:
            log.warning("%s step verify mismatch and no repair action (attempt=%d): %s", LOG_VERIFY, attempt + 1, repair_msg)

        latest_tasks = _tasks_for_action_scope(action, today)

    return False, f"{last_msg}（已重试 {max_retries} 次）", latest_tasks


def _pending_plan_actions(plan: PendingPlan) -> List[Dict[str, Any]]:
    """
    将 PendingPlan 归一化为可执行 action 列表。
    优先使用 plan.actions；若不存在则兼容旧版 updates -> update actions。
    """
    out: List[Dict[str, Any]] = []
    if isinstance(getattr(plan, "actions", None), list) and plan.actions:
        for a in plan.actions:
            if isinstance(a, dict):
                out.append(dict(a))
        if out:
            return out

    for u in (plan.updates or []):
        if not isinstance(u, dict):
            continue
        tid = str(u.get("id", "") or "").strip()
        if not tid:
            continue
        out.append(
            {
                "action": "update",
                "target_task_id": tid,
                "new_start_iso": u.get("new_start"),
                "new_end_iso": u.get("new_end"),
            }
        )
    return out


def apply_pending_plan(plan: PendingPlan) -> Tuple[str, Optional[str]]:
    """把预览计划真正写回数据库。"""
    exec_actions = _pending_plan_actions(plan)
    if not exec_actions:
        return "没有可执行的变更。", None

    plan_fingerprint = _build_write_fingerprint(
        "apply_pending_plan",
        {
            "actions": exec_actions,
            "updates": plan.updates,
            "desired_state": plan.desired_state or [],
            "state_version": plan.state_version or "",
        },
    )
    if _is_recent_duplicate_write(plan_fingerprint):
        log.info("skip duplicate apply_pending_plan by fingerprint")
        return "检测到重复确认请求，已忽略本次重复写入。", plan.overflow_warning

    today = date.today()
    scope_dates = _collect_scope_dates_from_actions(exec_actions, today)
    current_scope_version = _compute_scope_state_version(scope_dates)
    if plan.state_version and current_scope_version != plan.state_version:
        return "执行前检测到日程已变化，已中止本次写入，请重新确认最新预览。", plan.overflow_warning
    snap_payload = _capture_scope_snapshot(scope_dates)
    repo.save_snapshot(snap_payload)

    try:
        executor = ActionExecutor(
            today=today,
            fetch_scope_tasks_fn=_tasks_for_action_scope,
            verify_with_repair_fn=_verify_step_with_repair,
            default_validate_step_fn=_validate_single_action_step,
        )

        for i, action in enumerate(exec_actions):
            if not isinstance(action, dict):
                continue
            step_action = dict(action)
            act = str(step_action.get("action", "") or "").strip().lower()
            target_id = str(step_action.get("target_task_id", "") or "").strip()

            def _apply_write() -> None:
                if act in ("insert", "plan"):
                    title = str(step_action.get("title", "") or "").strip()
                    if not title:
                        raise RuntimeError("insert 缺少 title")
                    start_raw = (
                        step_action.get("new_start_iso")
                        or step_action.get("start_iso")
                        or step_action.get("start_time")
                    )
                    end_raw = (
                        step_action.get("new_end_iso")
                        or step_action.get("end_iso")
                        or step_action.get("end_time")
                    )
                    if not (isinstance(start_raw, str) and start_raw.strip() and isinstance(end_raw, str) and end_raw.strip()):
                        raise RuntimeError("insert 缺少明确开始/结束时间")
                    created = repo.insert_task(
                        Task(
                            id=None,
                            start_time=_local_iso_to_utc_iso(start_raw),
                            end_time=_local_iso_to_utc_iso(end_raw),
                            title=title,
                            priority=str(step_action.get("priority") or "normal"),
                            status=str(step_action.get("status") or "pending"),
                            is_fixed=bool(step_action.get("is_fixed", False)),
                            memo=step_action.get("memo"),
                        )
                    )
                    if created is None:
                        raise RuntimeError(f"插入失败: {title}")
                    return

                if act == "delete":
                    if not target_id:
                        raise RuntimeError("delete 缺少 target_task_id")
                    repo.delete_task(target_id)
                    return

                if act == "complete":
                    if not target_id:
                        raise RuntimeError("complete 缺少 target_task_id")
                    payload: Dict[str, Any] = {"status": str(step_action.get("status") or "done")}
                    if step_action.get("memo") is not None:
                        payload["memo"] = step_action.get("memo")
                    updated = repo.update_task(target_id, payload)
                    if updated is None:
                        raise RuntimeError(f"完成更新失败: {target_id}")
                    return

                if act == "update":
                    if not target_id:
                        raise RuntimeError("update 缺少 target_task_id")
                    payload: Dict[str, Any] = {}
                    start_raw = (
                        step_action.get("new_start_iso")
                        or step_action.get("start_iso")
                        or step_action.get("start_time")
                    )
                    end_raw = (
                        step_action.get("new_end_iso")
                        or step_action.get("end_iso")
                        or step_action.get("end_time")
                    )
                    if isinstance(start_raw, str) and start_raw.strip():
                        payload["start_time"] = _local_iso_to_utc_iso(start_raw)
                    if isinstance(end_raw, str) and end_raw.strip():
                        payload["end_time"] = _local_iso_to_utc_iso(end_raw)
                    if step_action.get("title") is not None:
                        payload["title"] = str(step_action.get("title"))
                    if step_action.get("priority") is not None:
                        payload["priority"] = str(step_action.get("priority"))
                    if step_action.get("status") is not None:
                        payload["status"] = str(step_action.get("status"))
                    if step_action.get("memo") is not None:
                        payload["memo"] = step_action.get("memo")
                    if isinstance(step_action.get("is_fixed"), bool):
                        payload["is_fixed"] = bool(step_action.get("is_fixed"))
                    if not payload:
                        # 语义层 update 在当前存储模型下可能无可写字段；留给后验做“已满足”判定。
                        return
                    updated = repo.update_task(target_id, payload)
                    if updated is None:
                        raise RuntimeError(f"更新失败，任务不存在或写入失败: {target_id}")
                    return

                raise RuntimeError(f"不支持的 action: {act}")

            step_res = executor.execute(
                action=step_action,
                apply_write_fn=_apply_write,
                step_index=i + 1,
                validate_step_fn=_validate_single_action_step,
                rollback_fn=lambda: _restore_tasks_from_scope_snapshot(snap_payload, scope_dates),
            )
            if not step_res.ok:
                tid = target_id or str(step_action.get("title", "") or "-")
                raise RuntimeError(f"第{i + 1}步后验失败（{tid}）：{step_res.message}")
        reconcile_note = None
        if plan.desired_state:
            ok, reconcile_msg, _ = _reconcile_desired_state(
                desired_state=plan.desired_state,
                fallback_date=today,
                max_rounds=DESIRED_STATE_RECONCILE_MAX_ROUNDS if DESIRED_STATE_AUTOFIX_ENABLED else 0,
                max_actions=DESIRED_STATE_AUTOFIX_MAX_ACTIONS,
                allow_insert=DESIRED_STATE_AUTOFIX_ALLOW_INSERT,
            )
            if not ok:
                raise RuntimeError(f"目标态校验未通过：{reconcile_msg}")
            elif reconcile_msg:
                reconcile_note = reconcile_msg

        merged_warn_parts = [x for x in [plan.overflow_warning, reconcile_note] if x]
        merged_warn = "\n".join(merged_warn_parts) if merged_warn_parts else None
        if reconcile_note:
            _mark_write_fingerprint(plan_fingerprint)
            return "已按预览结果执行调整，并完成自动补救。", merged_warn
        _mark_write_fingerprint(plan_fingerprint)
        return "已按预览结果执行调整。", merged_warn
    except Exception as e:
        restored = _restore_tasks_from_scope_snapshot(snap_payload, scope_dates)
        log.exception("apply_pending_plan failed, rolled back %d tasks: %s", restored, e)
        return f"执行失败，已自动回滚 {restored} 条任务。", str(e)


def handle_user_command(user_text: str, conversation_history: Optional[List[Dict[str, str]]] = None) -> Tuple[str, Optional[str], Optional[PendingPlan], Optional[str]]:
    """
    处理用户一句话。返回 (reply_text, overflow_warning, pending_plan, reasoning)。
    reply_text 为给用户看的回复；overflow_warning 为排程超时提示（若有）；reasoning 为 LLM 思考过程（若有）。
    对话历史参数已保留兼容，但当前策略默认不传给模型（每轮无记忆）。
    """
    from config import USE_AGENT_MODE, PLAN_CRITIC_ENABLED
    from ai.agent import run_agent, run_plan_critic, get_last_agent_meta
    from logic.graph_solver import resolve_graph_intent

    # 在请求入口显式获取当前系统时间。用本地时间供 prompt/NLP 使用，使「结束时间设成现在」等表述与任务时间一致
    now_dt = datetime.now().replace(microsecond=0)
    today = now_dt.date()
    log.info("handle_user_command text=%s now=%s", (user_text or "")[:200], now_dt.isoformat())
    day_tasks_for_ai = repo.tasks_for_date(today)
    # 当前策略：严格无历史记忆。信息不足时由模型通过工具查询数据库补全。
    conversation_history = None

    pipeline_out = run_command_pipeline(
        user_text=user_text,
        today=today,
        now_dt=now_dt,
        day_tasks_for_ai=day_tasks_for_ai,
        conversation_history=conversation_history,
        use_agent_mode=USE_AGENT_MODE,
        plan_critic_enabled=PLAN_CRITIC_ENABLED,
        run_agent_fn=run_agent,
        run_plan_critic_fn=run_plan_critic,
        resolve_graph_intent_fn=resolve_graph_intent,
        parse_user_command_fn=parse_user_command,
        apply_guardrails_fn=apply_guardrails,
        normalize_command_intent_fn=normalize_command_intent,
        get_agent_meta_fn=get_last_agent_meta,
    )
    cmd = pipeline_out.cmd
    reasoning = pipeline_out.reasoning
    agent_meta = pipeline_out.agent_meta or {}

    # 硬门槛：若仅得到规则兜底结果，不允许直接写库，避免“非结构化输出”污染状态。
    if (
        str(getattr(cmd, "parse_source", "") or "").lower() == "rule_fallback"
        and str(getattr(cmd, "intent", "") or "").lower() in ("insert", "update", "delete", "plan", "replan_all")
    ):
        return "当前未获得可验证的结构化模型输出，已停止写入。请补充更明确的任务与时间后重试。", None, None, reasoning

    # Agent 协议失败后，legacy 结果必须具备可靠锚点；否则只返回澄清，不写库。
    if (
        pipeline_out.source == "legacy"
        and bool(agent_meta.get("protocol_failed"))
        and not _legacy_result_is_safe_after_protocol_failure(cmd)
    ):
        return (
            "模型输出格式不稳定，且当前计划缺少可执行锚点。请补充明确时间或目标任务（例如“把跑步改到 20:00-20:30”）。",
            None,
            None,
            reasoning,
        )

    # 收敛策略：当启用 agent 模式时，仅允许 agent 协议成功后写库。
    if (
        AGENT_ONLY_WRITE_MODE
        and USE_AGENT_MODE
        and pipeline_out.source != "agent"
        and _is_write_command(cmd)
    ):
        return (
            "当前已启用严格写入模式：仅 agent 协议成功可写库。"
            "本次解析来源为 legacy，已停止写入。请重试或补充更明确信息。",
            None,
            None,
            reasoning,
        )

    # 当用户说「下周五」等时，若模型返回的日期星期错误，用我们算出的正确日期纠正
    _correct_relative_dates_in_actions(user_text, today, cmd.actions)

    # 声明式目标态优先：若模型给了 desired_state，则以 Diff 生成动作为准（覆盖 actions）
    desired_state = getattr(cmd, "desired_state", None)
    if isinstance(desired_state, list) and desired_state:
        try:
            from logic import compute_state_diff
            generated_actions = compute_state_diff(day_tasks_for_ai, desired_state, allow_delete=False)
            old_actions_len = len(cmd.actions) if isinstance(cmd.actions, list) else 0
            cmd.actions = generated_actions
            if cmd.actions:
                cmd.intent = "plan"
                if not cmd.reply_text:
                    cmd.reply_text = "已根据目标日程生成调整方案。"
                log.info(
                    "desired_state diff generated actions=%d (replaced old_actions=%d)",
                    len(cmd.actions), old_actions_len
                )
            else:
                msg = cmd.reply_text or "目标日程与当前状态一致，无需调整。"
                log.info("desired_state diff produced no actions; return no-op")
                return msg, None, None, reasoning
        except Exception as e:
            log.exception("desired_state diff failed: %s", e)

    # 写库前可信度门禁：拦截“格式合法但与事实冲突”的计划（尤其是 compiler 修复结果）。
    trust_ok, trust_msg = evaluate_plan_trust(
        cmd=cmd,
        user_text=user_text,
        day_tasks=day_tasks_for_ai,
        agent_meta=agent_meta,
    )
    if not trust_ok:
        log.warning("%s plan trust gate blocked write: %s", LOG_VERIFY, trust_msg)
        return trust_msg, None, None, reasoning

    if isinstance(cmd.actions, list) and cmd.actions:
        compiled_actions, repaired_count = _compile_actions_for_execution(
            actions=cmd.actions,
            today=today,
            day_tasks=day_tasks_for_ai,
            now_dt=now_dt,
        )
        cmd.actions = compiled_actions
        if repaired_count > 0:
            log.info("%s action compiler repaired %d action timing anchors", LOG_VERIFY, repaired_count)

    # 调试日志：检查解析结果
    log.info("%s parse result: intent=%s, actions=%s, actions_len=%s",
             LOG_ACTION,
             cmd.intent, 
             type(cmd.actions).__name__ if cmd.actions else None,
             len(cmd.actions) if isinstance(cmd.actions, list) else 0)

    if cmd.need_clarification:
        # 让前端直接展示候选；这里先文本化返回，避免误更新
        msg = cmd.clarification_question or "我没法唯一确定你要操作哪条任务，请提供更明确的描述。"
        cands = cmd.candidates or []
        lines = []
        for c in cands[:3]:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            title = c.get("title")
            _, st = _utc_iso_to_local_display(c.get("start_time") or "")
            _, et = _utc_iso_to_local_display(c.get("end_time") or "")
            lines.append(f"- {title} (ID: {cid}, 时间: {st}–{et})")
        
        if lines:
            msg += "\n候选：\n" + "\n".join(lines)
        return msg, None, None, reasoning

    # ──── 彻底重排 (replan_all) 专有逻辑：无论 actions 是否为空均触发预览 ────
    if cmd.intent == "replan_all" and (not cmd.actions or len(cmd.actions) == 0):
        log.info("触发 replan_all (空 actions 分支)")
        day_tasks = repo.tasks_for_date(today)
        pending_tasks = [t for t in day_tasks if str(t.status).lower() == "pending" and not t.is_fixed]
        
        if not pending_tasks:
            return cmd.reply_text or "当日没有待完成的任务需要重新规划。", None, None, reasoning
            
        # 按时间排序找到第一个
        pending_tasks.sort(key=lambda x: x.start_time)
        first_task = pending_tasks[0]
        
        # 触发一次“原地”多米诺，让算法自动消除空隙或解决后续冲突
        res: DominoResult = run_domino(
            day_tasks,
            str(first_task.id),
            first_task.start_time,
            first_task.end_time,
            workday_end_hour=WORKDAY_END_HOUR
        )
        
        if not res.updated_tasks or len(res.updated_tasks) <= 1:
            return cmd.reply_text or "当前日程已经是最佳状态，无需额外规划。", None, None, reasoning
            
        # 构造预览
        old_by_id = {str(t.id): t for t in day_tasks}
        updates: List[Dict[str, str]] = []
        for u in res.updated_tasks:
            old = old_by_id.get(str(u.id))
            updates.append({
                "id": str(u.id),
                "title": u.title,
                "old_start": (old.start_time if old else ""),
                "old_end": (old.end_time if old else ""),
                "new_start": u.start_time,
                "new_end": u.end_time,
            })
            
        preview_lines = ["我已为您重新安排了所有待办任务（预览）："]
        reason_str = cmd.planning_reason or cmd.reply_text or "优化剩余日程排期，消除时间冲突。"
        preview_lines.append(f"\n📌 规划思路：{reason_str}")
        for x in updates:
            _, os = _utc_iso_to_local_display(x["old_start"])
            _, oe = _utc_iso_to_local_display(x["old_end"])
            _, ns = _utc_iso_to_local_display(x["new_start"])
            _, ne = _utc_iso_to_local_display(x["new_end"])
            preview_lines.append(f"- {x['title']}：{os}–{oe} → {ns}–{ne}")
            
        plan = PendingPlan(
            user_text=user_text,
            updates=updates,
            overflow_warning=res.overflow_warning,
            desired_state=desired_state if isinstance(desired_state, list) else None,
            state_version=_compute_scope_state_version(_scope_dates_from_preview_updates(updates, today)),
        )
        return "\n".join(preview_lines), res.overflow_warning, plan, reasoning

    # 多动作写入前做一次确定性硬校验，先挡住明显非法输入
    if cmd.actions and isinstance(cmd.actions, list):
        ok, err_msg, conflict_count = _prevalidate_actions(cmd.actions, today)
        if not ok:
            log.warning("%s prevalidate blocked actions: %s", LOG_VERIFY, err_msg)
            return err_msg, None, None, reasoning
        if conflict_count > 0:
            log.info("prevalidate found %d conflicts (will be handled by domino/preview)", conflict_count)

    # 统一支持多动作：actions 存在时，逐条执行（insert/update/delete/complete/plan）
    if cmd.actions and isinstance(cmd.actions, list) and len(cmd.actions) > 0:
        log.info("%s 进入多动作处理逻辑，actions数量=%d", LOG_ACTION, len(cmd.actions))
        day_tasks = repo.tasks_for_date(today)
        effective_desired_state = desired_state if isinstance(desired_state, list) and desired_state else _derive_semantic_contract_from_actions(cmd.actions)
        scope_dates = _collect_scope_dates_from_actions(cmd.actions, today)
        snap_payload = _capture_scope_snapshot(scope_dates)
        # 非预览模式才拍快照并进入真实写库；plan 预览模式禁止提前写库。
        is_plan_intent = cmd.intent in ("plan", "replan_all")
        if not is_plan_intent:
            repo.save_snapshot(snap_payload)
        executor = ActionExecutor(
            today=today,
            fetch_scope_tasks_fn=_tasks_for_action_scope,
            verify_with_repair_fn=_verify_step_with_repair,
            default_validate_step_fn=_validate_single_action_step,
        )

        def _execute_multi_step(
            step_action: Dict[str, Any],
            apply_write_fn: Any,
            step_index: int,
            fail_label: str = "后验失败",
        ) -> Tuple[bool, Optional[str], List[Task]]:
            step_res = executor.execute(
                action=step_action,
                apply_write_fn=apply_write_fn,
                step_index=step_index,
                validate_step_fn=_validate_single_action_step,
                rollback_fn=lambda: _restore_tasks_from_scope_snapshot(snap_payload, scope_dates),
            )
            if not step_res.ok:
                restored = int(step_res.rolled_back_count or 0)
                return False, f"计划第{step_index}步{fail_label}，已回滚 {restored} 条任务：{step_res.message}", step_res.after_tasks
            return True, None, step_res.after_tasks

        inserted = 0
        updated = 0
        deleted = 0
        completed = 0
        overflow = None
        lines: List[str] = []
        tips_lines: List[str] = []

        # 规划模式或多动作预览：收集变更而不立即执行
        task_id_to_staged: Dict[str, Dict[str, str]] = {}
        staged_action_map: Dict[str, Dict[str, Any]] = {}
        virtual_task_seq = 0

        def _stage_action(key: str, action_obj: Dict[str, Any]) -> None:
            if not key:
                return
            if not isinstance(action_obj, dict):
                return
            staged_action_map[key] = dict(action_obj)

        def _upsert_day_task_snapshot(task_obj: Task) -> None:
            nonlocal day_tasks
            if task_obj is None:
                return
            tid = str(getattr(task_obj, "id", "") or "").strip()
            if not tid:
                day_tasks.append(task_obj)
                return
            replaced = False
            next_tasks: List[Task] = []
            for t in day_tasks:
                if str(getattr(t, "id", "") or "").strip() == tid:
                    next_tasks.append(task_obj)
                    replaced = True
                else:
                    next_tasks.append(t)
            if not replaced:
                next_tasks.append(task_obj)
            day_tasks = next_tasks

        def _remove_day_task_snapshot(task_id: str) -> None:
            nonlocal day_tasks
            tid = str(task_id or "").strip()
            if not tid:
                return
            day_tasks = [t for t in day_tasks if str(getattr(t, "id", "") or "").strip() != tid]

        def _resolve_target_from_day_snapshot(action_obj: Dict[str, Any]) -> Optional[Task]:
            tid = str(action_obj.get("target_task_id", "") or "").strip()
            if tid:
                for t in day_tasks:
                    if str(getattr(t, "id", "") or "").strip() == tid:
                        return t
            target_title = str(action_obj.get("target_title") or action_obj.get("title") or "").strip()
            if target_title:
                lower_q = target_title.lower()
                for t in day_tasks:
                    title = str(getattr(t, "title", "") or "").strip()
                    if title and title.lower() == lower_q:
                        return t
                for t in day_tasks:
                    title = str(getattr(t, "title", "") or "").strip()
                    if title and (lower_q in title.lower() or title.lower() in lower_q):
                        return t
            return _resolve_target_from_action(action_obj, today)

        def _next_virtual_task_id(step_index: int, title: str) -> str:
            nonlocal virtual_task_seq
            virtual_task_seq += 1
            token = (title or "task").strip().replace(" ", "_")
            token = token[:16] if token else "task"
            return f"__plan_{step_index}_{virtual_task_seq}_{token}"

        for idx, a in enumerate(cmd.actions):
            log.info("%s 处理第%d个action: %s", LOG_ACTION, idx + 1, a)
            if not isinstance(a, dict):
                continue
            act = (a.get("action") or "").lower().strip()
            before_step_tasks = _tasks_for_action_scope(a, today)
            step_ok, step_msg = _validate_single_action_step(a, today, before_step_tasks)
            if not step_ok:
                restored = _restore_tasks_from_scope_snapshot(snap_payload, scope_dates)
                log.warning("%s 计划第%d步预校验失败，触发回滚 restored=%d reason=%s", LOG_ROLLBACK, idx + 1, restored, step_msg)
                return f"计划第{idx + 1}步校验失败，已回滚 {restored} 条任务：{step_msg}", None, None, reasoning
            action_fingerprint = _build_write_fingerprint(
                "multi_action",
                {
                    "intent": cmd.intent,
                    "date": today.isoformat(),
                    "action": a,
                },
            )

            # 计划/新增：若没给 start/end，则自动排入
            if act in ("insert", "plan"):
                title = (a.get("title") or "").strip()
                log.info("%s 处理plan/insert action: title=%s, act=%s", LOG_ACTION, title, act)
                if not title:
                    log.warning("action缺少title，跳过")
                    continue
                dur = int(a.get("duration_minutes") or 60)
                difficulty = (a.get("difficulty") or "").lower().strip()
                pr = (a.get("priority") or "normal").lower()
                memo = a.get("memo")
                tips = a.get("tips")
                reason = a.get("reason")  # 规划原因
                
                # 构建memo内容：包含tips和reason
                memo_parts = []
                if reason:
                    memo_parts.append(f"规划原因: {reason}")
                if isinstance(tips, list) and tips:
                    tips_str = " | ".join([str(t).strip() for t in tips if str(t).strip()])
                    if tips_str:
                        memo_parts.append(f"执行建议: {tips_str}")
                if memo:
                    memo_parts.append(memo)
                if difficulty and difficulty in ("easy", "medium", "hard"):
                    difficulty_cn = {"easy": "简单", "medium": "中等", "hard": "困难"}.get(difficulty, difficulty)
                    memo_parts.append(f"难度: {difficulty_cn}")
                
                # 合并memo内容
                final_memo = " | ".join(memo_parts) if memo_parts else None

                # 检查时间字段：支持 start_iso/end_iso 和 new_start_iso/new_end_iso
                start_iso = a.get("start_iso") or a.get("new_start_iso")
                end_iso = a.get("end_iso") or a.get("new_end_iso")
                if start_iso and end_iso:
                    # 无时区的 ISO 视为用户本地时间，转为 UTC 再存，避免日历错日/错时
                    start_iso = _local_iso_to_utc_iso(start_iso)
                    end_iso = _local_iso_to_utc_iso(end_iso)
                    log.info("使用指定的时间: %s - %s", start_iso, end_iso)
                else:
                    log.info("查找空闲时间段，duration=%d分钟", dur)
                    slot_s, slot_e = _find_earliest_free_slot(day_tasks, today, dur)
                    log.info("_find_earliest_free_slot返回: %s - %s", slot_s, slot_e)
                    # 仍需避开锁定任务（理论上已避开所有任务，这里只是双保险）
                    slot_s, slot_e = _shift_past_fixed(day_tasks, slot_s, dur, today)
                    log.info("_shift_past_fixed返回: %s - %s", slot_s, slot_e)
                    start_iso = _local_naive_to_utc_iso(slot_s)
                    end_iso = _local_naive_to_utc_iso(slot_e)
                    log.info("找到空闲时间段: %s - %s", start_iso, end_iso)

                # 重新规划时：若当日已有同标题的待办任务，则更新其时间/memo，并在预览中展示
                if act == "plan":
                    existing_pending = [
                        t for t in day_tasks
                        if (t.title or "").strip() == title and (str(t.status or "").lower() == "pending")
                    ]
                    if existing_pending:
                        target_task = existing_pending[0]
                        # 将算出来的新时间（可能是 _find_earliest_free_slot 算出的本地无时区）转为 UTC 展示/落库
                        final_start_utc = _local_iso_to_utc_iso(start_iso)
                        final_end_utc = _local_iso_to_utc_iso(end_iso)
                        
                        if is_plan_intent:
                            # 规划模式下，记录到预览列表而不执行
                            task_id_to_staged[str(target_task.id)] = {
                                "id": str(target_task.id),
                                "title": target_task.title,
                                "old_start": target_task.start_time,
                                "old_end": target_task.end_time,
                                "new_start": final_start_utc,
                                "new_end": final_end_utc
                            }
                            _stage_action(
                                f"upd:{target_task.id}",
                                {
                                    "action": "update",
                                    "target_task_id": str(target_task.id),
                                    "title": title,
                                    "new_start_iso": final_start_utc,
                                    "new_end_iso": final_end_utc,
                                    "priority": pr if pr in ("high", "normal", "low") else None,
                                    "memo": final_memo,
                                },
                            )
                            _upsert_day_task_snapshot(
                                Task(
                                    id=target_task.id,
                                    start_time=final_start_utc,
                                    end_time=final_end_utc,
                                    title=title or target_task.title,
                                    priority=pr if pr in ("high", "normal", "low") else (target_task.priority or "normal"),
                                    status=target_task.status or "pending",
                                    is_fixed=bool(target_task.is_fixed),
                                    memo=final_memo if final_memo is not None else target_task.memo,
                                    created_at=target_task.created_at,
                                )
                            )
                            log.info("规划暂存：更新已有待办任务 id=%s title=%s", target_task.id, title)
                            updated += 1
                            continue
                        else:
                            # 传统模式，直接执行
                            if _is_recent_duplicate_write(action_fingerprint):
                                log.info("skip duplicate plan-update action by fingerprint")
                                lines.append(f"- 跳过重复写入：{title}")
                                continue
                            payload = {"start_time": final_start_utc, "end_time": final_end_utc}
                            if final_memo is not None:
                                payload["memo"] = final_memo
                            if pr in ("high", "normal", "low"):
                                payload["priority"] = pr
                            verify_action = {
                                "action": "update",
                                "target_task_id": str(target_task.id),
                                "title": title,
                                "new_start_iso": final_start_utc,
                                "new_end_iso": final_end_utc,
                                "priority": pr if pr in ("high", "normal", "low") else None,
                                "memo": final_memo,
                            }
                            def _apply_existing_plan_update() -> None:
                                updated_task = repo.update_task(target_task.id, payload)
                                if updated_task is None:
                                    raise RuntimeError(f"更新失败，任务不存在或写入失败: {target_task.id}")

                            step_ok2, step_msg2, after_step_tasks = _execute_multi_step(
                                verify_action,
                                _apply_existing_plan_update,
                                idx + 1,
                            )
                            if not step_ok2:
                                return step_msg2 or "执行失败", None, None, reasoning
                            _mark_write_fingerprint(action_fingerprint)
                            log.info("执行变更：更新已有待办任务 id=%s title=%s", target_task.id, title)
                            updated += 1
                            date_str, start_hm = _utc_iso_to_local_display(final_start_utc)
                            _, end_hm = _utc_iso_to_local_display(final_end_utc)
                            _, old_start_hm = _utc_iso_to_local_display(target_task.start_time)
                            _, old_end_hm = _utc_iso_to_local_display(target_task.end_time)
                            lines.append(
                                f"- 重排：{date_str} {start_hm}–{end_hm} {title}（原 {old_start_hm}–{old_end_hm}）"
                            )
                            day_tasks = after_step_tasks
                            continue

                # 记录过去某天已做的事：NLP 会返回 status="done"，避免被逾期重排到未来
                task_status = "done" if a.get("status") == "done" else "pending"
                # 兜底：若插入的任务结束日期早于今天，视为“记录过去”，直接标为已完成，避免被逾期重排
                if task_status == "pending" and end_iso:
                    end_local_date = _parse_dt_local_date(end_iso, naive_is_local=True)
                    if end_local_date is not None and end_local_date < today:
                        task_status = "done"
                        log.info("插入任务结束日早于今天，视为记录过去，自动标为 done: title=%s end_local=%s", title, end_local_date)

                if is_plan_intent:
                    staged_key = f"ins:{idx + 1}:{title}"
                    virtual_id = _next_virtual_task_id(idx + 1, title)
                    task_id_to_staged[staged_key] = {
                        "id": virtual_id,
                        "title": title,
                        "old_start": "",
                        "old_end": "",
                        "new_start": start_iso,
                        "new_end": end_iso,
                    }
                    _stage_action(
                        staged_key,
                        {
                            "action": "insert",
                            "title": title,
                            "start_iso": start_iso,
                            "end_iso": end_iso,
                            "new_start_iso": start_iso,
                            "new_end_iso": end_iso,
                            "priority": pr if pr in ("high", "normal", "low") else "normal",
                            "status": task_status,
                            "is_fixed": bool(a.get("is_fixed", False)),
                            "memo": final_memo,
                        },
                    )
                    _upsert_day_task_snapshot(
                        Task(
                            id=virtual_id,
                            start_time=start_iso,
                            end_time=end_iso,
                            title=title,
                            priority=pr if pr in ("high", "normal", "low") else "normal",
                            status=task_status,
                            is_fixed=bool(a.get("is_fixed", False)),
                            memo=final_memo,
                        )
                    )
                    inserted += 1
                    continue

                insert_fingerprint = _build_write_fingerprint(
                    "multi_action_insert",
                    {
                        "title": title,
                        "start_time": start_iso,
                        "end_time": end_iso,
                        "priority": pr,
                        "status": task_status,
                        "memo": final_memo,
                    },
                )
                if _is_recent_duplicate_write(insert_fingerprint):
                    log.info("skip duplicate insert action by fingerprint title=%s", title)
                    lines.append(f"- 跳过重复写入：{title}")
                    continue
                log.info("准备创建Task对象: title=%s, start_iso=%s, end_iso=%s, priority=%s, status=%s", title, start_iso, end_iso, pr, task_status)
                try:
                    t = Task(
                        id=None,
                        start_time=start_iso,
                        end_time=end_iso,
                        title=title,
                        priority=pr if pr in ("high", "normal", "low") else "normal",
                        status=task_status,
                        is_fixed=bool(a.get("is_fixed", False)),
                        memo=final_memo,
                    )
                    log.info("Task对象创建成功，准备插入任务: %s", title)
                except Exception as e:
                    log.exception("创建Task对象失败: title=%s, error=%s", title, e)
                    restored = _restore_tasks_from_scope_snapshot(snap_payload, scope_dates)
                    return f"执行失败，已自动回滚 {restored} 条任务。", None, None, reasoning
                try:
                    created_holder: Dict[str, Task] = {}
                    verify_action = {
                        "action": "insert",
                        "title": title,
                        "start_iso": start_iso,
                        "end_iso": end_iso,
                        "priority": pr if pr in ("high", "normal", "low") else "normal",
                        "status": task_status,
                        "memo": final_memo,
                    }
                    def _apply_insert_task() -> None:
                        created = repo.insert_task(t)
                        if created is None:
                            raise RuntimeError("插入失败，未返回任务")
                        created_holder["task"] = created

                    step_ok2, step_msg2, after_step_tasks = _execute_multi_step(
                        verify_action,
                        _apply_insert_task,
                        idx + 1,
                    )
                    if not step_ok2:
                        return step_msg2 or "执行失败", None, None, reasoning
                    created = created_holder.get("task")
                    if created is None:
                        raise RuntimeError("插入成功但未捕获创建结果")
                    _mark_write_fingerprint(insert_fingerprint)
                    log.info("任务插入成功: id=%s, title=%s", created.id, created.title)
                    inserted += 1
                except Exception as e:
                    log.exception("任务插入失败: title=%s, error=%s", title, e)
                    restored = _restore_tasks_from_scope_snapshot(snap_payload, scope_dates)
                    return f"执行失败，已自动回滚 {restored} 条任务。", None, None, reasoning
                diff_str = f", {difficulty}" if difficulty in ("easy", "medium", "hard") else ""
                date_str, start_hm = _utc_iso_to_local_display(created.start_time)
                _, end_hm = _utc_iso_to_local_display(created.end_time)
                lines.append(
                    f"- 新增：{date_str} {start_hm}–{end_hm} {created.title}（{created.priority}{diff_str}，{dur}min）"
                )
                if isinstance(tips, list) and tips:
                    show = [str(x).strip() for x in tips if str(x).strip()]
                    if show:
                        tips_lines.append(f"【{created.title}】")
                        tips_lines.extend([f"- {x}" for x in show[:4]])
                # 更新 day_tasks 参与后续排程避开固定项
                day_tasks = after_step_tasks
                continue

            # 完成/进度更新：优先按 id/时间/标题定位
            if act in ("complete",):
                target = _resolve_target_from_day_snapshot(a)
                if not target:
                    continue
                if is_plan_intent:
                    staged_key = f"complete:{target.id}"
                    task_id_to_staged[staged_key] = {
                        "id": str(target.id),
                        "title": target.title,
                        "old_start": target.start_time,
                        "old_end": target.end_time,
                        "new_start": target.start_time,
                        "new_end": target.end_time,
                    }
                    _stage_action(
                        staged_key,
                        {
                            "action": "complete",
                            "target_task_id": str(target.id),
                            "status": str(a.get("status") or "done"),
                            "memo": a.get("memo"),
                        },
                    )
                    _upsert_day_task_snapshot(
                        Task(
                            id=target.id,
                            start_time=target.start_time,
                            end_time=target.end_time,
                            title=target.title,
                            priority=target.priority,
                            status=str(a.get("status") or "done"),
                            is_fixed=bool(target.is_fixed),
                            memo=a.get("memo") if a.get("memo") is not None else target.memo,
                            created_at=target.created_at,
                        )
                    )
                    completed += 1
                    continue
                complete_fingerprint = _build_write_fingerprint(
                    "multi_action_complete",
                    {
                        "task_id": str(target.id),
                        "memo": a.get("memo"),
                        "user_text": user_text,
                    },
                )
                if _is_recent_duplicate_write(complete_fingerprint):
                    log.info("skip duplicate complete action by fingerprint task=%s", target.id)
                    lines.append(f"- 跳过重复写入：{target.title}")
                    continue
                
                # 检查是否有进度百分比（如"已完成50%"）
                import re
                progress_match = None
                if a.get("memo"):
                    progress_match = re.search(r'(\d+)\s*%', str(a.get("memo")))
                if not progress_match:
                    # 从用户输入中提取进度百分比
                    progress_match = re.search(r'已完成\s*(\d+)\s*%|完成\s*(\d+)\s*%|进度\s*(\d+)\s*%|(\d+)\s*%', user_text)
                
                if progress_match:
                    # 提取进度百分比
                    progress = int(progress_match.group(1) or progress_match.group(2) or progress_match.group(3) or progress_match.group(4))
                    progress = max(0, min(100, progress))
                    
                    # 更新memo字段存储进度
                    memo_text = f"progress:{progress}"
                    if a.get("memo"):
                        memo_text = f"{a.get('memo')} | {memo_text}"
                    
                    repo.update_task(target.id, {"memo": memo_text})
                    
                    # 如果进度是100%，标记为完成
                    if progress >= 100:
                        repo.update_task(target.id, {"status": "done"})
                        completed += 1
                        done_date, done_st = _utc_iso_to_local_display(target.start_time)
                        _, done_et = _utc_iso_to_local_display(target.end_time)
                        lines.append(f"- 完成：{done_date} {done_st}–{done_et} {target.title}（100%）")
                    else:
                        prog_date, prog_st = _utc_iso_to_local_display(target.start_time)
                        _, prog_et = _utc_iso_to_local_display(target.end_time)
                        lines.append(f"- 更新进度：{prog_date} {prog_st}–{prog_et} {target.title}（{progress}%）")
                else:
                    # 没有进度百分比，直接标记为完成，并确保 memo 中写入 progress:100 以便进度条显示 100%
                    current_memo = (target.memo or "").strip()
                    memo_clean = re.sub(r'\s*\|\s*progress:\s*\d+\s*', '', current_memo)
                    memo_clean = re.sub(r'progress:\s*\d+\s*', '', memo_clean)
                    memo_clean = re.sub(r'\s*\|\s*$', '', memo_clean).strip()
                    if a.get("memo"):
                        memo_clean = f"{memo_clean} | {a.get('memo')}".strip() if memo_clean else (a.get("memo") or "").strip()
                    new_memo = f"{memo_clean} | progress:100" if memo_clean else "progress:100"
                    repo.update_task(target.id, {"status": "done", "memo": new_memo})
                    completed += 1
                    done_date, done_st = _utc_iso_to_local_display(target.start_time)
                    _, done_et = _utc_iso_to_local_display(target.end_time)
                    lines.append(f"- 完成：{done_date} {done_st}–{done_et} {target.title}")
                verify_action = {"action": "complete", "target_task_id": str(target.id)}
                after_step_tasks = _tasks_for_action_scope(verify_action, today)
                ok_effect, effect_msg, after_step_tasks = _verify_step_with_repair(
                    verify_action,
                    before_step_tasks,
                    after_step_tasks,
                    today,
                )
                if not ok_effect:
                    restored = _restore_tasks_from_scope_snapshot(snap_payload, scope_dates)
                    log.warning("%s 计划第%d步后验失败，触发回滚 restored=%d reason=%s", LOG_ROLLBACK, idx + 1, restored, effect_msg)
                    return f"计划第{idx + 1}步后验失败，已回滚 {restored} 条任务：{effect_msg}", None, None, reasoning
                _mark_write_fingerprint(complete_fingerprint)
                continue

            if act == "delete":
                target = _resolve_target_from_day_snapshot(a)
                if not target:
                    continue
                if is_plan_intent:
                    staged_key = f"delete:{target.id}"
                    task_id_to_staged[staged_key] = {
                        "id": str(target.id),
                        "title": target.title,
                        "old_start": target.start_time,
                        "old_end": target.end_time,
                        "new_start": "",
                        "new_end": "",
                    }
                    _stage_action(
                        staged_key,
                        {
                            "action": "delete",
                            "target_task_id": str(target.id),
                        },
                    )
                    _remove_day_task_snapshot(str(target.id))
                    deleted += 1
                    continue
                delete_fingerprint = _build_write_fingerprint(
                    "multi_action_delete",
                    {"task_id": str(target.id)},
                )
                if _is_recent_duplicate_write(delete_fingerprint):
                    log.info("skip duplicate delete action by fingerprint task=%s", target.id)
                    lines.append(f"- 跳过重复写入：{target.title}")
                    continue
                verify_action = {"action": "delete", "target_task_id": str(target.id)}
                def _apply_delete_task() -> None:
                    repo.delete_task(target.id)

                step_ok2, step_msg2, after_step_tasks = _execute_multi_step(
                    verify_action,
                    _apply_delete_task,
                    idx + 1,
                )
                if not step_ok2:
                    return step_msg2 or "执行失败", None, None, reasoning
                _mark_write_fingerprint(delete_fingerprint)
                deleted += 1
                del_date, del_st = _utc_iso_to_local_display(target.start_time)
                _, del_et = _utc_iso_to_local_display(target.end_time)
                lines.append(f"- 删除：{del_date} {del_st}–{del_et} {target.title}")
                continue

            if act == "update":
                target = _resolve_target_from_day_snapshot(a)
                if not target:
                    continue
                if target.is_fixed:
                    continue
                # 复用原有“多米诺”更新：允许 delta 或 new_start/new_end
                st = _parse_dt_naive(target.start_time)
                et = _parse_dt_naive(target.end_time)
                original_duration_min = (et - st).total_seconds() / 60
                duration_min = original_duration_min  # 默认保持原时长
                delta = a.get("delta_minutes")
                new_start_iso = a.get("new_start_iso")
                new_end_iso = a.get("new_end_iso")
                
                # 检查是否有新的时长要求（用户说"时间需要X分钟"等）
                if a.get("duration_minutes") is not None:
                    duration_min = int(a.get("duration_minutes"))
                    # 保持开始时间不变，调整结束时间
                    et = st + timedelta(minutes=duration_min)

                # 先应用位移（若有）
                if delta is not None:
                    st = st + timedelta(minutes=int(delta))
                    et = st + timedelta(minutes=duration_min)

                # 再根据 new_start_iso / new_end_iso 调整：
                # - 若两者都给：直接以两者为准
                # - 若只给 new_start_iso：保持原时长不变，从新起点往后推 duration_min 分钟
                # - 若只给 new_end_iso：**保持开始时间不变**，仅改结束时间（如「结束时间设成现在」）
                # 注意：NLP/Agent 返回的时间是本地时间（无时区），需转为 UTC 后再存库
                if new_start_iso and new_end_iso:
                    new_start = _local_iso_to_utc_iso(new_start_iso)
                    new_end = _local_iso_to_utc_iso(new_end_iso)
                elif new_start_iso and not new_end_iso:
                    new_start = _local_iso_to_utc_iso(new_start_iso)
                    st2 = _parse_dt_naive(new_start)  # now in UTC
                    et2 = st2 + timedelta(minutes=duration_min)
                    new_end = _to_iso(et2)
                elif new_end_iso and not new_start_iso:
                    new_end = _local_iso_to_utc_iso(new_end_iso)
                    st2 = st  # 仅改结束时间时保持开始时间不变
                    et2 = _parse_dt_naive(new_end)
                    if et2 <= st2:
                        et2 = st2 + timedelta(minutes=1)
                        new_end = _to_iso(et2)
                    new_start = _to_iso(st2)
                else:
                    new_start = _to_iso(st)
                    new_end = _to_iso(et)
                day_tasks2 = list(day_tasks) if is_plan_intent else repo.tasks_for_date(today)
                try:
                    res: DominoResult = run_domino(day_tasks2, str(target.id), new_start, new_end, workday_end_hour=WORKDAY_END_HOUR)
                except Exception as e:
                    log.exception("run_domino failed: %s", e)
                    # 异常时直接更新目标任务
                    verify_action = {
                        "action": "update",
                        "target_task_id": str(target.id),
                        "new_start_iso": new_start,
                        "new_end_iso": new_end,
                    }
                    def _apply_direct_update_when_domino_fail() -> None:
                        updated_task = repo.update_task(target.id, {"start_time": new_start, "end_time": new_end})
                        if updated_task is None:
                            raise RuntimeError(f"更新失败，任务不存在或写入失败: {target.id}")

                    step_ok2, step_msg2, after_step_tasks = _execute_multi_step(
                        verify_action,
                        _apply_direct_update_when_domino_fail,
                        idx + 1,
                    )
                    if not step_ok2:
                        return step_msg2 or "执行失败", None, None, reasoning
                    updated += 1
                    _, ns_hm = _utc_iso_to_local_display(new_start)
                    _, ne_hm = _utc_iso_to_local_display(new_end)
                    lines.append(f"- 修改：{target.title} → {ns_hm}–{ne_hm}（排程异常，仅更新当前任务）")
                    day_tasks = after_step_tasks
                    continue

                # 若多米诺没有返回任何更新（极端情况下），至少保证当前任务本身被顺延
                if not res.updated_tasks:
                    log.warning("run_domino returned empty updated_tasks for task %s, new_start=%s new_end=%s", target.id, new_start, new_end)
                    verify_action = {
                        "action": "update",
                        "target_task_id": str(target.id),
                        "new_start_iso": new_start,
                        "new_end_iso": new_end,
                    }
                    def _apply_direct_update_without_domino_updates() -> None:
                        updated_task = repo.update_task(target.id, {"start_time": new_start, "end_time": new_end})
                        if updated_task is None:
                            raise RuntimeError(f"更新失败，任务不存在或写入失败: {target.id}")

                    step_ok2, step_msg2, after_step_tasks = _execute_multi_step(
                        verify_action,
                        _apply_direct_update_without_domino_updates,
                        idx + 1,
                    )
                    if not step_ok2:
                        return step_msg2 or "执行失败", None, None, reasoning
                    updated += 1
                    _, ns_hm = _utc_iso_to_local_display(new_start)
                    _, ne_hm = _utc_iso_to_local_display(new_end)
                    lines.append(f"- 修改：{target.title} → {ns_hm}–{ne_hm}（未影响其他任务）")
                    if res.overflow_warning:
                        overflow = res.overflow_warning
                    day_tasks = after_step_tasks
                    continue
                
                # 确保目标任务在更新列表中（即使 run_domino 没有返回它）
                target_in_updated = any(str(u.id) == str(target.id) for u in res.updated_tasks)
                if not target_in_updated:
                    log.warning("run_domino did not include target task %s in updated_tasks, adding it manually", target.id)
                    # 手动添加目标任务到更新列表
                    target_updated = Task(
                        id=target.id,
                        start_time=new_start,
                        end_time=new_end,
                        title=target.title,
                        priority=target.priority,
                        status=target.status,
                        is_fixed=target.is_fixed,
                        memo=target.memo,
                        created_at=target.created_at,
                    )
                    res.updated_tasks.append(target_updated)

                # --- 预览/确认策略：仅当影响多条任务或显式规划时才需用户确认 ---
                need_confirm = len(res.updated_tasks) > 1 or is_plan_intent
                if need_confirm:
                    # 收集本次动作引发的所有变更
                    current_updates = []
                    old_by_id = {str(t.id): t for t in day_tasks2}
                    for u in res.updated_tasks:
                        old = old_by_id.get(str(u.id))
                        item = {
                            "id": str(u.id),
                            "title": u.title,
                            "old_start": (old.start_time if old else ""),
                            "old_end": (old.end_time if old else ""),
                            "new_start": u.start_time,
                            "new_end": u.end_time,
                        }
                        current_updates.append(item)
                        # 存入总体暂存表，避免重复和合并
                        task_id_to_staged[str(u.id)] = item
                    
                    if is_plan_intent:
                        for item in current_updates:
                            tid = str(item.get("id", "") or "").strip()
                            if not tid:
                                continue
                            _stage_action(
                                f"upd:{tid}",
                                {
                                    "action": "update",
                                    "target_task_id": tid,
                                    "new_start_iso": item.get("new_start"),
                                    "new_end_iso": item.get("new_end"),
                                },
                            )
                        for u in res.updated_tasks:
                            _upsert_day_task_snapshot(u)
                        updated += 1
                        continue # 规划模式：继续处理后续 action，不立即返回
                    
                    # 传统模式下单条动作引发多条冲突，直接返回预览
                    preview_lines = []
                    preview_lines.append("发现时间冲突，建议做以下调整（预览）：")
                    update_reason = (a.get("reason") or a.get("memo") or cmd.reply_text or "").strip()
                    if update_reason:
                        preview_lines.append(f"📌 调整原因：{update_reason}")
                    for x in current_updates:
                        _, os = _utc_iso_to_local_display(x["old_start"])
                        _, oe = _utc_iso_to_local_display(x["old_end"])
                        _, ns = _utc_iso_to_local_display(x["new_start"])
                        _, ne = _utc_iso_to_local_display(x["new_end"])
                        preview_lines.append(f"- {x['title']}：{os}–{oe} → {ns}–{ne}")
                    if res.overflow_warning:
                        preview_lines.append(f"⚠ {res.overflow_warning}")
                    plan = PendingPlan(
                        user_text=user_text,
                        updates=current_updates,
                        overflow_warning=res.overflow_warning,
                        desired_state=desired_state if isinstance(desired_state, list) else None,
                        state_version=_compute_scope_state_version(_scope_dates_from_preview_updates(current_updates, today)),
                    )
                    return "\n".join(preview_lines), None, plan, reasoning

                # 不需要确认：直接执行
                update_fingerprint = _build_write_fingerprint(
                    "multi_action_update",
                    {
                        "task_id": str(target.id),
                        "new_start": new_start,
                        "new_end": new_end,
                    },
                )
                if _is_recent_duplicate_write(update_fingerprint):
                    log.info("skip duplicate update action by fingerprint task=%s", target.id)
                    lines.append(f"- 跳过重复写入：{target.title}")
                    continue
                shifted = 0
                verify_action = {
                    "action": "update",
                    "target_task_id": str(target.id),
                    "new_start_iso": new_start,
                    "new_end_iso": new_end,
                    "memo": a.get("memo"),
                }
                def _apply_domino_updates() -> None:
                    nonlocal shifted
                    for u in res.updated_tasks:
                        updated_task = repo.update_task(u.id, {"start_time": u.start_time, "end_time": u.end_time})
                        if updated_task is None:
                            raise RuntimeError(f"更新失败，任务不存在或写入失败: {u.id}")
                        if str(u.id) != str(target.id):
                            shifted += 1
                    if a.get("memo"):
                        updated_target = repo.update_task(target.id, {"memo": a.get("memo")})
                        if updated_target is None:
                            raise RuntimeError(f"更新备注失败: {target.id}")

                step_ok2, step_msg2, after_step_tasks = _execute_multi_step(
                    verify_action,
                    _apply_domino_updates,
                    idx + 1,
                )
                if not step_ok2:
                    return step_msg2 or "执行失败", None, None, reasoning
                _mark_write_fingerprint(update_fingerprint)
                updated += 1
                upd_date, upd_st = _utc_iso_to_local_display(new_start)
                _, upd_et = _utc_iso_to_local_display(new_end)
                lines.append(f"- 修改：{target.title} → {upd_date} {upd_st}–{upd_et}（顺延 {shifted} 条）")
                if res.overflow_warning:
                    overflow = res.overflow_warning
                day_tasks = after_step_tasks
                continue

        # 规划模式结束检测：若汇总了变更且是规划意向，则返回预览
        if is_plan_intent and (task_id_to_staged or staged_action_map):
            preview_updates = list(task_id_to_staged.values())
            preview_lines = ["我已根据您的需求规划了以下方案（预览），请确认："]
            # 规划原因
            final_reason = cmd.planning_reason or cmd.reply_text or "按需重新规划日程。"
            preview_lines.append(f"\n📌 规划思路：{final_reason}")
            for x in preview_updates:
                _, os = _utc_iso_to_local_display(x["old_start"])
                _, oe = _utc_iso_to_local_display(x["old_end"])
                _, ns = _utc_iso_to_local_display(x["new_start"])
                _, ne = _utc_iso_to_local_display(x["new_end"])
                preview_lines.append(f"- {x['title']}：{os}–{oe} → {ns}–{ne}")
            
            plan = PendingPlan(
                user_text=user_text,
                updates=preview_updates,
                overflow_warning=overflow,
                desired_state=desired_state if isinstance(desired_state, list) else None,
                state_version=_compute_scope_state_version(_collect_scope_dates_from_actions(list(staged_action_map.values()), today)),
                actions=list(staged_action_map.values()),
            )
            return "\n".join(preview_lines), overflow, plan, reasoning

        header = cmd.reply_text or "已理解并执行以下操作："
        summary = f"统计：新增 {inserted}，修改 {updated}，完成 {completed}，删除 {deleted}。"
        detail = "\n".join(lines) if lines else "（未产生可执行操作）"
        log.info("多动作处理完成: inserted=%d, updated=%d, completed=%d, deleted=%d, lines=%d", 
                 inserted, updated, completed, deleted, len(lines))
        
        # 添加规划原因（如果有）
        planning_reason = None
        if hasattr(cmd, 'planning_reason') and cmd.planning_reason:
            planning_reason = cmd.planning_reason
            log.info("从cmd.planning_reason获取规划原因: %s", planning_reason)
        elif isinstance(cmd.actions, list) and len(cmd.actions) > 0:
            # 尝试从actions中查找planning_reason（如果LLM把它放在action里）
            for action in cmd.actions:
                if isinstance(action, dict) and action.get("planning_reason"):
                    planning_reason = action.get("planning_reason")
                    log.info("从action中获取规划原因: %s", planning_reason)
                    break
        
        feedback_parts = [header, "", summary, detail]
        
        # 添加整体规划原因（必需显示）
        if planning_reason:
            feedback_parts.append("")
            feedback_parts.append(f"【规划思路】\n{planning_reason}")
        else:
            # 如果没有规划原因，给出提示
            feedback_parts.append("")
            feedback_parts.append("【规划思路】\n⚠️ 未提供规划思路，请重新规划以获得详细的规划说明。")
        
        # 添加执行建议
        if tips_lines:
            feedback_parts.append("")
            tips_block = "\n".join(tips_lines)
            feedback_parts.append(f"【执行建议】\n{tips_block}")

        # 执行后闭环校验（声明式目标态或系统派生语义契约）
        if isinstance(effective_desired_state, list) and effective_desired_state:
            ok, verify_msg, _ = _reconcile_desired_state(
                desired_state=effective_desired_state,
                fallback_date=today,
                max_rounds=DESIRED_STATE_RECONCILE_MAX_ROUNDS if DESIRED_STATE_AUTOFIX_ENABLED else 0,
                max_actions=DESIRED_STATE_AUTOFIX_MAX_ACTIONS,
                allow_insert=DESIRED_STATE_AUTOFIX_ALLOW_INSERT,
            )
            if not ok:
                restored = _restore_tasks_from_scope_snapshot(snap_payload, scope_dates)
                log.warning("%s 目标态校验失败，触发回滚 restored=%d reason=%s", LOG_ROLLBACK, restored, verify_msg)
                return f"目标态校验未通过，已回滚 {restored} 条任务：{verify_msg}", None, None, reasoning
            if verify_msg:
                feedback_parts.append("")
                prefix = "✅ " if ok else "⚠️ "
                feedback_parts.append(f"【目标态校验】\n{prefix}{verify_msg}")
        
        feedback = "\n".join(feedback_parts)
        return feedback, overflow, None, reasoning

    if cmd.intent == "insert":
        # 新增：若未给出时间，需要再让 AI 补全或默认；这里假设 AI 已给出 start_iso/end_iso 或至少 title
        # 如果 LLM 没有返回 title，尝试从用户输入中提取
        title = cmd.title
        start_iso = cmd.start_iso
        end_iso = cmd.end_iso
        
        if not title:
            # 尝试从用户输入中提取任务名称和时间
            from ai.guardrails import parse_natural_time
            import re
            text = user_text
            
            # 提取时间描述
            time_desc = None
            if "凌晨" in text:
                m = re.search(r'凌晨\s*(\d{1,2})\s*点', text)
                if m:
                    hour = int(m.group(1))
                    time_desc = f"凌晨{hour}点"
            elif re.search(r'(\d{1,2})\s*点\s*睡觉', text):
                m = re.search(r'(\d{1,2})\s*点', text)
                if m:
                    hour = int(m.group(1))
                    if hour < 6:
                        time_desc = f"凌晨{hour}点"
                    else:
                        time_desc = f"{hour}点"
            
            # 提取任务名称
            text_clean = re.sub(r'(凌晨|早上|上午|中午|下午|晚上|明天|今天|\d{1,2}点|\d{1,2}:\d{2})', '', text)
            text_clean = text_clean.strip()
            if text_clean:
                title = text_clean
            elif "睡觉" in text:
                title = "睡觉"
            else:
                title = "新任务"
            
            # 解析时间
            if time_desc and not start_iso:
                _parsed_dt = parse_natural_time(time_desc, base_date=today)
                start_iso = _parsed_dt.strftime("%Y-%m-%dT%H:%M:%S") if _parsed_dt else None
                if start_iso:
                    dur_minutes = 480 if "睡觉" in text else 60
                    start_dt = _parse_dt_naive(start_iso)
                    end_dt = start_dt + timedelta(minutes=dur_minutes)
                    end_iso = _to_iso(end_dt)
        
        if not title:
            return cmd.reply_text or "请说明要新增的任务名称与时间。", None, None, reasoning
        # 单动作新增：也按“当前时间往后空闲块”插入；无时区时间视为本地时间，转 UTC 再存
        if start_iso and end_iso:
            start = _local_iso_to_utc_iso(start_iso)
            end = _local_iso_to_utc_iso(end_iso)
        elif cmd.start_iso and cmd.end_iso:
            start = _local_iso_to_utc_iso(cmd.start_iso)
            end = _local_iso_to_utc_iso(cmd.end_iso)
        else:
            day_tasks = repo.tasks_for_date(today)
            dur = 60
            if (end_iso and start_iso) or (cmd.end_iso and cmd.start_iso):
                try:
                    end_dt = _parse_dt_naive(end_iso or cmd.end_iso)
                    start_dt = _parse_dt_naive(start_iso or cmd.start_iso)
                    dur = int((end_dt - start_dt).total_seconds() / 60)
                except Exception:
                    dur = 60
            slot_s, slot_e = _find_earliest_free_slot(day_tasks, today, dur)
            start = _local_naive_to_utc_iso(slot_s)
            end = _local_naive_to_utc_iso(slot_e)
        t = Task(
            id=None,
            start_time=start,
            end_time=end,
            title=title,
            priority=cmd.priority or "normal",
            status="pending",
            is_fixed=False,
            memo=cmd.memo,
        )
        step_action = {
            "action": "insert",
            "title": title,
            "start_iso": start,
            "end_iso": end,
        }
        before_step_tasks = _tasks_for_action_scope(step_action, today)
        step_ok, step_msg = _validate_single_action_step(step_action, today, before_step_tasks)
        if not step_ok:
            return f"当前动作校验失败：{step_msg}", None, None, reasoning
        insert_fingerprint = _build_write_fingerprint(
            "single_insert",
            {
                "title": title,
                "start_time": start,
                "end_time": end,
                "priority": cmd.priority or "normal",
                "memo": cmd.memo,
            },
        )
        if _is_recent_duplicate_write(insert_fingerprint):
            log.info("skip duplicate single insert by fingerprint title=%s", title)
            return "检测到短时间重复请求，已忽略本次重复写入。", None, None, reasoning
        insert_scope_dates = _collect_scope_dates_from_actions([step_action], today)
        insert_snapshot = _capture_scope_snapshot(insert_scope_dates)
        repo.save_snapshot(insert_snapshot)
        created_holder: Dict[str, Task] = {}
        single_executor = ActionExecutor(
            today=today,
            fetch_scope_tasks_fn=_tasks_for_action_scope,
            verify_with_repair_fn=_verify_step_with_repair,
            default_validate_step_fn=_validate_single_action_step,
        )
        def _apply_single_insert() -> None:
            created = repo.insert_task(t)
            if created is None:
                raise RuntimeError("插入失败，未返回任务")
            created_holder["task"] = created

        step_res = single_executor.execute(
            action=step_action,
            apply_write_fn=_apply_single_insert,
            step_index=1,
            validate_step_fn=_validate_single_action_step,
            rollback_fn=lambda: _restore_tasks_from_scope_snapshot(insert_snapshot, insert_scope_dates),
        )
        if not step_res.ok:
            restored = int(step_res.rolled_back_count or 0)
            log.warning("%s 单步执行后验失败，触发回滚 restored=%d reason=%s", LOG_ROLLBACK, restored, step_res.message)
            return f"执行后校验失败，已回滚 {restored} 条任务：{step_res.message}", None, None, reasoning
        semantic_contract = _derive_semantic_contract_from_actions([step_action])
        if semantic_contract:
            ok_sem, msg_sem, _ = _reconcile_desired_state(
                desired_state=semantic_contract,
                fallback_date=today,
                max_rounds=DESIRED_STATE_RECONCILE_MAX_ROUNDS if DESIRED_STATE_AUTOFIX_ENABLED else 0,
                max_actions=DESIRED_STATE_AUTOFIX_MAX_ACTIONS,
                allow_insert=DESIRED_STATE_AUTOFIX_ALLOW_INSERT,
            )
            if not ok_sem:
                restored = _restore_tasks_from_scope_snapshot(insert_snapshot, insert_scope_dates)
                log.warning("%s 单步目标态校验失败，触发回滚 restored=%d reason=%s", LOG_ROLLBACK, restored, msg_sem)
                return f"目标态校验未通过，已回滚 {restored} 条任务：{msg_sem}", None, None, reasoning
        _mark_write_fingerprint(insert_fingerprint)
        created_task = created_holder.get("task")
        if created_task is None:
            return "执行失败：插入结果缺失。", None, None, reasoning
        date_str, start_hm = _utc_iso_to_local_display(start)
        _, end_hm = _utc_iso_to_local_display(end)
        time_str = f"（{date_str} {start_hm}–{end_hm}）" if date_str else ""
        return cmd.reply_text or f"已为您新增任务：{title}{time_str}。", None, None, reasoning

    if cmd.intent == "delete":
        target = _resolve_target_task(cmd, today)
        if not target:
            return "未找到要删除的任务，请说明是「几点」或「哪条」。", None, None, reasoning
        step_action = {"action": "delete", "target_task_id": str(target.id)}
        before_step_tasks = _tasks_for_action_scope(step_action, today)
        step_ok, step_msg = _validate_single_action_step(step_action, today, before_step_tasks)
        if not step_ok:
            return f"当前动作校验失败：{step_msg}", None, None, reasoning
        delete_fingerprint = _build_write_fingerprint(
            "single_delete",
            {"task_id": str(target.id)},
        )
        if _is_recent_duplicate_write(delete_fingerprint):
            log.info("skip duplicate single delete by fingerprint task=%s", target.id)
            return "检测到短时间重复请求，已忽略本次重复写入。", None, None, reasoning
        delete_scope_dates = _collect_scope_dates_from_actions([step_action], today)
        delete_snapshot = _capture_scope_snapshot(delete_scope_dates)
        repo.save_snapshot(delete_snapshot)
        single_executor = ActionExecutor(
            today=today,
            fetch_scope_tasks_fn=_tasks_for_action_scope,
            verify_with_repair_fn=_verify_step_with_repair,
            default_validate_step_fn=_validate_single_action_step,
        )
        def _apply_single_delete() -> None:
            repo.delete_task(target.id)

        step_res = single_executor.execute(
            action=step_action,
            apply_write_fn=_apply_single_delete,
            step_index=1,
            validate_step_fn=_validate_single_action_step,
            rollback_fn=lambda: _restore_tasks_from_scope_snapshot(delete_snapshot, delete_scope_dates),
        )
        if not step_res.ok:
            restored = int(step_res.rolled_back_count or 0)
            return f"执行后校验失败，已回滚 {restored} 条任务：{step_res.message}", None, None, reasoning
        _mark_write_fingerprint(delete_fingerprint)
        return cmd.reply_text or "已删除该任务。", None, None, reasoning

    if cmd.intent == "update":
        target = _resolve_target_task(cmd, today)
        if not target:
            return "未找到要修改的任务，请说明是「下午几点的会」等。", None, None, reasoning
        if target.is_fixed:
            return "该任务已锁定，暂不自动调整。", None, None, reasoning
        single_update_probe = {
            "action": "update",
            "target_task_id": str(target.id),
            "delta_minutes": cmd.delta_minutes,
            "new_start_iso": cmd.new_start_iso,
            "new_end_iso": cmd.new_end_iso,
        }
        if not _has_explicit_update_mutation(single_update_probe):
            return "当前动作校验失败：update 缺少明确变更字段（如 new_start_iso/new_end_iso/delta_minutes）。", None, None, reasoning

        # 计算新时间（统一为 UTC ISO，输入无时区时按本地语义）
        st = _parse_dt_naive(target.start_time)
        et = _parse_dt_naive(target.end_time)
        duration_min = max(1, int((et - st).total_seconds() / 60))
        if cmd.delta_minutes is not None:
            st = st + timedelta(minutes=cmd.delta_minutes)
            et = st + timedelta(minutes=duration_min)
        new_start = _to_iso(st)
        new_end = _to_iso(et)
        if cmd.new_start_iso and cmd.new_end_iso:
            new_start = _local_iso_to_utc_iso(cmd.new_start_iso)
            new_end = _local_iso_to_utc_iso(cmd.new_end_iso)
        elif cmd.new_start_iso and not cmd.new_end_iso:
            new_start = _local_iso_to_utc_iso(cmd.new_start_iso)
            st2 = _parse_dt_naive(new_start)
            new_end = _to_iso(st2 + timedelta(minutes=duration_min))
        elif cmd.new_end_iso and not cmd.new_start_iso:
            new_end = _local_iso_to_utc_iso(cmd.new_end_iso)
            et2 = _parse_dt_naive(new_end)
            if et2 <= st:
                et2 = st + timedelta(minutes=1)
                new_end = _to_iso(et2)
            new_start = _to_iso(st)

        # 排程前快照
        day_tasks = repo.tasks_for_date(today)
        snap_payload = [
            {"id": t.id, "start_time": t.start_time, "end_time": t.end_time, "title": t.title, "priority": t.priority, "status": t.status, "is_fixed": t.is_fixed}
            for t in day_tasks
        ]
        repo.save_snapshot(snap_payload)

        # 多米诺
        res: DominoResult = run_domino(
            day_tasks,
            str(target.id),
            new_start,
            new_end,
            workday_end_hour=WORKDAY_END_HOUR,
        )

        # 确保目标任务在更新列表中
        target_in_updated = any(str(u.id) == str(target.id) for u in res.updated_tasks)
        if not target_in_updated:
            target_updated = Task(
                id=target.id,
                start_time=new_start,
                end_time=new_end,
                title=target.title,
                priority=target.priority,
                status=target.status,
                is_fixed=target.is_fixed,
                memo=target.memo,
                created_at=target.created_at,
            )
            res.updated_tasks.append(target_updated)

        # 所有更新都需要确认
        old_by_id = {str(t.id): t for t in day_tasks}
        updates: List[Dict[str, str]] = []
        for u in res.updated_tasks:
            old = old_by_id.get(str(u.id))
            updates.append(
                {
                    "id": str(u.id),
                    "title": u.title,
                    "old_start": (old.start_time if old else ""),
                    "old_end": (old.end_time if old else ""),
                    "new_start": u.start_time,
                    "new_end": u.end_time,
                }
            )
        
        preview_lines = []
        preview_lines.append("我将做一次完整调整（预览），请确认后再执行：")
        # LM 制定该修改计划的原因，展示到反馈框（单条 update 分支）
        first_act = cmd.actions[0] if (isinstance(cmd.actions, list) and len(cmd.actions) > 0) else None
        update_reason = ""
        if isinstance(first_act, dict):
            update_reason = (first_act.get("reason") or first_act.get("memo") or "").strip()
        if not update_reason:
            update_reason = (cmd.reply_text or "").strip()
        if update_reason:
            preview_lines.append("")
            preview_lines.append(f"📌 调整原因：{update_reason}")
        for x in updates:
            _, os = _utc_iso_to_local_display(x["old_start"])
            _, oe = _utc_iso_to_local_display(x["old_end"])
            _, ns = _utc_iso_to_local_display(x["new_start"])
            _, ne = _utc_iso_to_local_display(x["new_end"])
            preview_lines.append(f"- {x['title']}：{os}–{oe} → {ns}–{ne}")
        if res.overflow_warning:
            preview_lines.append(f"⚠ {res.overflow_warning}")
        
        plan = PendingPlan(
            user_text=user_text,
            updates=updates,
            overflow_warning=res.overflow_warning,
            desired_state=desired_state if isinstance(desired_state, list) else None,
            state_version=_compute_scope_state_version(_scope_dates_from_preview_updates(updates, today)),
        )
        return "\n".join(preview_lines), None, plan, reasoning

    log.warning("未匹配到任何处理逻辑: intent=%s, actions=%s", cmd.intent, cmd.actions)
    return cmd.reply_text or "已收到。", None, None, reasoning


def undo_last() -> Tuple[bool, str]:
    """撤销上一步排程：用最新快照恢复当日任务。"""
    snap = repo.latest_snapshot()
    if not snap or not snap.payload:
        return False, "没有可撤销的操作。"
    today = date.today().isoformat()
    restored = 0
    for row in snap.payload:
        if not row.get("start_time", "").startswith(today):
            continue
        repo.update_task(row["id"], {"start_time": row["start_time"], "end_time": row["end_time"]})
        restored += 1
    return True, f"已恢复 {restored} 条任务的排程。"
