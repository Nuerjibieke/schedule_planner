"""数据仓库：对 Supabase 的增删改查。"""
from datetime import datetime, date
from typing import List, Optional

from .client import (
    get_supabase,
    TASKS_TABLE,
    SNAPSHOTS_TABLE,
    SUMMARIES_TABLE,
    REMINDERS_TABLE,
    LONG_TASKS_TABLE,
    LONG_TASK_LOGS_TABLE,
)
from .models import Task, Snapshot, Summary, Reminder, LongTask, LongTaskLog
import logging

log = logging.getLogger("api.supabase")


def _log_result(action: str, r) -> None:
    # supabase-py 的返回对象通常有 data / error / status_code（不同版本字段略有差异）
    data = getattr(r, "data", None)
    err = getattr(r, "error", None)
    status = getattr(r, "status_code", None) or getattr(r, "status", None)
    try:
        n = len(data) if isinstance(data, list) else (1 if data else 0)
    except Exception:
        n = -1
    log.info("%s status=%s error=%s data_len=%s", action, status, err, n)
    if log.isEnabledFor(logging.DEBUG):
        # DEBUG 才打印数据（截断），避免刷屏/泄露
        s = str(data)
        log.debug("%s data=%s", action, s[:1200] + ("..." if len(s) > 1200 else ""))


def tasks_for_date(d: date) -> List[Task]:
    """取某日全部任务，按 start_time 排序。"""
    sb = get_supabase()
    day_start = d.isoformat() + "T00:00:00"
    day_end = d.isoformat() + "T23:59:59"
    r = sb.table(TASKS_TABLE).select("*").gte("start_time", day_start).lte("start_time", day_end).order("start_time").execute()
    _log_result(f"tasks_for_date({d.isoformat()})", r)
    return [Task.from_row(x) for x in (r.data or [])]


def tasks_for_date_range(start_d: date, end_d: date) -> List[Task]:
    """
    取日期区间内的任务（按 start_time 过滤，包含起止日期），按 start_time 排序。
    用于统计类场景（如活跃度矩阵），避免按天多次请求。
    """
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    sb = get_supabase()
    start_iso = start_d.isoformat() + "T00:00:00"
    end_iso = end_d.isoformat() + "T23:59:59"
    r = (
        sb.table(TASKS_TABLE)
        .select("*")
        .gte("start_time", start_iso)
        .lte("start_time", end_iso)
        .order("start_time")
        .execute()
    )
    _log_result(f"tasks_for_date_range({start_d.isoformat()}..{end_d.isoformat()})", r)
    return [Task.from_row(x) for x in (r.data or [])]


def task_by_id(task_id: str) -> Optional[Task]:
    sb = get_supabase()
    r = sb.table(TASKS_TABLE).select("*").eq("id", task_id).maybe_single().execute()
    _log_result(f"task_by_id({task_id})", r)
    if r.data:
        return Task.from_row(r.data)
    return None


def task_by_start_time(day: date, start_iso: str) -> Optional[Task]:
    """按日期 + 开始时间查找任务（用于「下午两点的会」这类描述）。"""
    tasks = tasks_for_date(day)
    for t in tasks:
        if t.start_time.startswith(day.isoformat()) and start_iso in t.start_time:
            return t
    return None


def tasks_by_title(day: date, title_keyword: str, limit: int = 5) -> List[Task]:
    """按日期 + 标题关键词模糊查找（用于「写代码做完了」这类描述）。"""
    sb = get_supabase()
    day_start = day.isoformat() + "T00:00:00"
    day_end = day.isoformat() + "T23:59:59"
    kw = (title_keyword or "").strip()
    if not kw:
        return []
    r = (
        sb.table(TASKS_TABLE)
        .select("*")
        .gte("start_time", day_start)
        .lte("start_time", day_end)
        .ilike("title", f"%{kw}%")
        .order("start_time")
        .limit(limit)
        .execute()
    )
    _log_result(f"tasks_by_title({day.isoformat()},{kw})", r)
    return [Task.from_row(x) for x in (r.data or [])]


def insert_task(t: Task) -> Task:
    sb = get_supabase()
    row = t.to_row()
    if "id" in row:
        del row["id"]
    r = sb.table(TASKS_TABLE).insert(row).execute()
    _log_result("insert_task", r)
    data = (r.data or [{}])[0]
    return Task.from_row(data)


def update_task(task_id: str, updates: dict) -> Optional[Task]:
    sb = get_supabase()
    r = sb.table(TASKS_TABLE).update(updates).eq("id", task_id).execute()
    _log_result(f"update_task({task_id})", r)
    if r.data and len(r.data) > 0:
        return Task.from_row(r.data[0])
    return None


def delete_task(task_id: str) -> bool:
    sb = get_supabase()
    r = sb.table(TASKS_TABLE).delete().eq("id", task_id).execute()
    _log_result(f"delete_task({task_id})", r)
    return True


def save_snapshot(payload: list) -> Snapshot:
    sb = get_supabase()
    r = sb.table(SNAPSHOTS_TABLE).insert({"payload": payload}).execute()
    _log_result("save_snapshot", r)
    data = (r.data or [{}])[0]
    return Snapshot.from_row(data)


def latest_snapshot() -> Optional[Snapshot]:
    sb = get_supabase()
    r = sb.table(SNAPSHOTS_TABLE).select("*").order("created_at", desc=True).limit(1).execute()
    _log_result("latest_snapshot", r)
    if r.data and len(r.data) > 0:
        return Snapshot.from_row(r.data[0])
    return None


def save_summary(s: Summary) -> Summary:
    """保存或更新summary。如果该日期和类型的记录已存在，则更新内容；否则创建新记录。"""
    sb = get_supabase()
    row = s.to_row()
    # 使用upsert：如果(date, kind)组合已存在，则更新content；否则插入新记录
    # on_conflict参数指定冲突的列（多列用逗号分隔的字符串）
    try:
        r = sb.table(SUMMARIES_TABLE).upsert(
            row,
            on_conflict="date,kind"
        ).execute()
        _log_result("save_summary (upsert)", r)
        # upsert后重新查询获取完整记录（包括id和created_at）
        existing = summary_by_date_and_kind(s.date, s.kind)
        if existing:
            return existing
        # 如果查询不到，尝试从返回数据中获取
        if r.data and len(r.data) > 0:
            return Summary.from_row(r.data[0])
        # 如果还是没有，创建一个临时对象（不应该发生）
        log.warning("upsert succeeded but could not retrieve record for date=%s kind=%s", s.date, s.kind)
        return s
    except Exception as e:
        log.exception("upsert failed, trying insert: %s", e)
        # 如果upsert失败（可能是约束不存在），尝试先查询再更新或插入
        existing = summary_by_date_and_kind(s.date, s.kind)
        if existing:
            # 更新现有记录
            updates = {"content": s.content}
            r = sb.table(SUMMARIES_TABLE).update(updates).eq("date", s.date).eq("kind", s.kind).execute()
            _log_result("save_summary (update)", r)
            return summary_by_date_and_kind(s.date, s.kind) or existing
        else:
            # 插入新记录
            r = sb.table(SUMMARIES_TABLE).insert(row).execute()
            _log_result("save_summary (insert fallback)", r)
            data = (r.data or [{}])[0]
            return Summary.from_row(data)


def summary_by_date_and_kind(d: str, kind: str) -> Optional[Summary]:
    sb = get_supabase()
    r = sb.table(SUMMARIES_TABLE).select("*").eq("date", d).eq("kind", kind).maybe_single().execute()
    _log_result(f"summary_by_date_and_kind({d},{kind})", r)
    if r.data:
        return Summary.from_row(r.data)
    return None


def daily_summaries_for_month(year: int, month: int) -> List[Summary]:
    sb = get_supabase()
    prefix = f"{year}-{month:02d}"
    r = sb.table(SUMMARIES_TABLE).select("*").eq("kind", "daily").like("date", f"{prefix}%").order("date").execute()
    _log_result(f"daily_summaries_for_month({prefix})", r)
    return [Summary.from_row(x) for x in (r.data or [])]


def weekly_summary_for_week(week_start: date) -> Optional[Summary]:
    """周报：date 为当周周一。"""
    sb = get_supabase()
    r = sb.table(SUMMARIES_TABLE).select("*").eq("date", week_start.isoformat()).eq("kind", "weekly").maybe_single().execute()
    _log_result(f"weekly_summary_for_week({week_start.isoformat()})", r)
    if r.data:
        return Summary.from_row(r.data)
    return None


def monthly_summary_for_month(year: int, month: int) -> Optional[Summary]:
    """月报：date 为当月1日。"""
    d = f"{year}-{month:02d}-01"
    return summary_by_date_and_kind(d, "monthly")


# ---------- Reminders ----------
def all_reminders() -> List[Reminder]:
    """获取所有提醒事项，按创建时间倒序。"""
    sb = get_supabase()
    r = sb.table(REMINDERS_TABLE).select("*").order("created_at", desc=False).execute()
    _log_result("all_reminders", r)
    return [Reminder.from_row(x) for x in (r.data or [])]


def insert_reminder(content: str) -> Reminder:
    """新增提醒事项。"""
    sb = get_supabase()
    row = {"content": content, "is_completed": False}
    r = sb.table(REMINDERS_TABLE).insert(row).execute()
    _log_result("insert_reminder", r)
    data = (r.data or [{}])[0]
    return Reminder.from_row(data)


def update_reminder(reminder_id: str, updates: dict) -> Optional[Reminder]:
    """更新提醒事项。"""
    sb = get_supabase()
    # updated_at 由数据库触发器或默认值处理，如果需要手动更新可以使用 now()
    r = sb.table(REMINDERS_TABLE).update(updates).eq("id", reminder_id).execute()
    _log_result(f"update_reminder({reminder_id})", r)
    if r.data and len(r.data) > 0:
        return Reminder.from_row(r.data[0])
    return None


def delete_reminder(reminder_id: str) -> bool:
    """删除提醒事项。"""
    sb = get_supabase()
    r = sb.table(REMINDERS_TABLE).delete().eq("id", reminder_id).execute()
    _log_result(f"delete_reminder({reminder_id})", r)
    return True


# ---------- Long Tasks ----------
def list_long_tasks(limit: int = 200) -> List[LongTask]:
    """获取长周期任务列表（按更新时间倒序）。"""
    sb = get_supabase()
    r = sb.table(LONG_TASKS_TABLE).select("*").order("updated_at", desc=True).limit(limit).execute()
    _log_result("list_long_tasks", r)
    return [LongTask.from_row(x) for x in (r.data or [])]


def long_task_by_id(task_id: str) -> Optional[LongTask]:
    sb = get_supabase()
    r = sb.table(LONG_TASKS_TABLE).select("*").eq("id", task_id).maybe_single().execute()
    _log_result(f"long_task_by_id({task_id})", r)
    if r.data:
        return LongTask.from_row(r.data)
    return None


def insert_long_task(t: LongTask) -> LongTask:
    sb = get_supabase()
    row = t.to_row()
    r = sb.table(LONG_TASKS_TABLE).insert(row).execute()
    _log_result("insert_long_task", r)
    data = (r.data or [{}])[0]
    created = LongTask.from_row(data)
    # 自动写一条系统日志
    try:
        add_long_task_log(created.id or "", "system", f"创建任务：{created.title}")
    except Exception:
        pass
    return created


def update_long_task(task_id: str, updates: dict) -> Optional[LongTask]:
    sb = get_supabase()
    r = sb.table(LONG_TASKS_TABLE).update(updates).eq("id", task_id).execute()
    _log_result(f"update_long_task({task_id})", r)
    if r.data and len(r.data) > 0:
        return LongTask.from_row(r.data[0])
    return None


def delete_long_task(task_id: str) -> bool:
    sb = get_supabase()
    r = sb.table(LONG_TASKS_TABLE).delete().eq("id", task_id).execute()
    _log_result(f"delete_long_task({task_id})", r)
    return True


def add_long_task_log(task_id: str, kind: str, content: str) -> Optional[LongTaskLog]:
    if not task_id:
        return None
    sb = get_supabase()
    row = LongTaskLog(id=None, task_id=task_id, kind=kind, content=content).to_row()
    r = sb.table(LONG_TASK_LOGS_TABLE).insert(row).execute()
    _log_result("add_long_task_log", r)
    if r.data and len(r.data) > 0:
        return LongTaskLog.from_row(r.data[0])
    return None


def logs_for_long_task(task_id: str, limit: int = 50) -> List[LongTaskLog]:
    sb = get_supabase()
    r = (
        sb.table(LONG_TASK_LOGS_TABLE)
        .select("*")
        .eq("task_id", task_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    _log_result(f"logs_for_long_task({task_id})", r)
    return [LongTaskLog.from_row(x) for x in (r.data or [])]
