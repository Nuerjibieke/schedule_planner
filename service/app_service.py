"""应用服务：串联 右脑(AI) -> 左脑(逻辑) -> 记忆体(DB)。"""
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple, Dict, Any
from dataclasses import dataclass
import logging

from db import repo
from db.models import Task
from ai import parse_user_command, CommandIntent
from logic import run_domino, DominoResult
from config import WORKDAY_END_HOUR

log = logging.getLogger("app.service")

@dataclass
class PendingPlan:
    """需要用户确认的排程变更（预览 -> 确认 -> 落库）。"""
    # 用户原始输入（用于展示）
    user_text: str
    # 需要写回的变更列表：[{id, title, old_start, old_end, new_start, new_end}]
    updates: List[Dict[str, str]]
    # 是否超时提示
    overflow_warning: Optional[str] = None

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


def _parse_dt_local_date(s: str) -> Optional[date]:
    """把 ISO 字符串解析为本地日期，用于「是否早于今天」等比较。"""
    if not s:
        return None
    try:
        ss = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
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
        # 仅当任务结束时间（本地日期）早于今天时才延期，避免把「今天凌晨已结束」的任务再挪到今天
        if task_end_local_date >= today:
            continue

        task_start = _parse_dt_naive(task.start_time)
        task_end = _parse_dt_naive(task.end_time)
        duration_min = int((task_end - task_start).total_seconds() / 60)

        today_tasks = repo.tasks_for_date(today)
        new_start, new_end = _find_earliest_free_slot(today_tasks, today, duration_min)
        new_start_iso = _to_iso(new_start)
        new_end_iso = _to_iso(new_end)
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
        # 按「日期+时间」在当日任务里找最接近的
        tasks = repo.tasks_for_date(today)
        for t in tasks:
            if t.start_time.startswith(today.isoformat()) and cmd.target_start_iso[:16] in t.start_time[:16]:
                return t
            # 允许模糊：用户说「下午两点」对应 14:00
            if cmd.target_start_iso in t.start_time or t.start_time.startswith(cmd.target_start_iso[:13]):
                return t
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
    day_prefix = today.isoformat()
    same_day = [t for t in tasks if (t.start_time or "").startswith(day_prefix)]
    if not same_day:
        return datetime.combine(today, datetime.min.time()).replace(hour=9, minute=0, second=0)
    ends = []
    for t in same_day:
        try:
            ends.append(_parse_dt_naive(t.end_time))
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
    
    # 获取未来7天的任务（避免无限查找）
    all_tasks = []
    for i in range(7):
        check_date = today + timedelta(days=i)
        day_tasks_check = repo.tasks_for_date(check_date)
        all_tasks.extend(day_tasks_check)
    
    # 按开始时间排序
    all_tasks.sort(key=lambda x: _parse_dt_naive(x.start_time))
    
    # 过滤掉已完成的任务（已完成的任务不占用时间轴）
    active_tasks = [t for t in all_tasks if t.status != "done"]
    
    # 查找空闲时间段，最多查找7天
    max_iterations = 1000  # 增加迭代次数以支持跨天查找
    for _ in range(max_iterations):
        end = cursor + timedelta(minutes=dur_min)
        moved = False
        
        # 检查是否与任何任务冲突
        for t in active_tasks:
            ts = _parse_dt_naive(t.start_time)
            te = _parse_dt_naive(t.end_time)
            if cursor < te and ts < end:
                # 冲突：游标跳到冲突任务结束
                cursor = max(cursor, te)
                moved = True
                break
        
        if not moved:
            return cursor, end
    
    # 极端情况：如果所有时间段都被占用，直接接到最后一个任务之后
    if active_tasks:
        last_end = max(_parse_dt_naive(t.end_time) for t in active_tasks)
        cursor = max(cursor, last_end)
    else:
        # 如果没有任务，从当前时间开始
        cursor = now
    
    end = cursor + timedelta(minutes=dur_min)
    return cursor, end


def _shift_past_fixed(existing: List[Task], start: datetime, dur_min: int, today: date) -> Tuple[datetime, datetime]:
    """避免与锁定任务重叠：若重叠则顺延到锁定任务结束（支持跨天）。"""
    end = start + timedelta(minutes=dur_min)
    # 获取未来7天的锁定任务
    fixed = []
    for i in range(7):
        check_date = today + timedelta(days=i)
        day_tasks_check = repo.tasks_for_date(check_date)
        fixed.extend([t for t in day_tasks_check if t.is_fixed])
    
    # 简单循环最多 50 次避免卡死（增加次数以支持跨天）
    for _ in range(50):
        moved = False
        for t in fixed:
            try:
                fs = _parse_dt_naive(t.start_time)
                fe = _parse_dt_naive(t.end_time)
            except Exception:
                continue
            if start < fe and fs < end:
                start = fe
                end = start + timedelta(minutes=dur_min)
                moved = True
                break
        if not moved:
            break
    return start, end


def apply_pending_plan(plan: PendingPlan) -> Tuple[str, Optional[str]]:
    """把预览计划真正写回数据库。"""
    if not plan.updates:
        return "没有可执行的变更。", None
    # 落库前再拍一次快照，确保可撤销
    today = date.today()
    day_tasks = repo.tasks_for_date(today)
    snap_payload = [
        {"id": t.id, "start_time": t.start_time, "end_time": t.end_time, "title": t.title, "priority": t.priority, "status": t.status, "is_fixed": t.is_fixed}
        for t in day_tasks
    ]
    repo.save_snapshot(snap_payload)

    for u in plan.updates:
        tid = u.get("id")
        if not tid:
            continue
        repo.update_task(tid, {"start_time": u["new_start"], "end_time": u["new_end"]})
    return "已按预览结果执行调整。", plan.overflow_warning


def handle_user_command(user_text: str, conversation_history: Optional[List[Dict[str, str]]] = None) -> Tuple[str, Optional[str], Optional[PendingPlan], Optional[str]]:
    """
    处理用户一句话。返回 (reply_text, overflow_warning, pending_plan, reasoning)。
    reply_text 为给用户看的回复；overflow_warning 为排程超时提示（若有）；reasoning 为 LLM 思考过程（若有）。
    conversation_history: 对话历史，格式为 [{"role": "user"/"assistant", "content": "..."}, ...]
    """
    # 在请求入口显式获取当前系统时间。用本地时间供 prompt/NLP 使用，使「结束时间设成现在」等表述与任务时间一致
    now_dt = datetime.now().replace(microsecond=0)
    today = now_dt.date()
    log.info("handle_user_command text=%s now=%s", (user_text or "")[:200], now_dt.isoformat())
    day_tasks_for_ai = repo.tasks_for_date(today)
    tasks_ctx = [
        {
            "id": str(t.id),
            "title": t.title,
            "start_time": t.start_time,
            "end_time": t.end_time,
            "priority": t.priority,
            "status": t.status,
            "is_fixed": t.is_fixed,
        }
        for t in day_tasks_for_ai
    ]
    cmd = parse_user_command(user_text, today, day_tasks=tasks_ctx, now_dt=now_dt, conversation_history=conversation_history)
    reasoning = getattr(cmd, "reasoning", None) or (cmd.reasoning if hasattr(cmd, "reasoning") else None)

    # 当用户说「下周五」等时，若模型返回的日期星期错误，用我们算出的正确日期纠正
    _correct_relative_dates_in_actions(user_text, today, cmd.actions)

    # 调试日志：检查解析结果
    log.info("parse_user_command result: intent=%s, actions=%s, actions_len=%s", 
             cmd.intent, 
             type(cmd.actions).__name__ if cmd.actions else None,
             len(cmd.actions) if isinstance(cmd.actions, list) else 0)

    if cmd.need_clarification:
        # 让前端直接展示候选；这里先文本化返回，避免误更新
        cands = cmd.candidates or []
        lines = []
        for c in cands[:3]:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            title = c.get("title")
            _, st = _utc_iso_to_local_display(c.get("start_time") or "")
            _, et = _utc_iso_to_local_display(c.get("end_time") or "")
            lines.append(f"- {cid} {st}–{et} {title}")
        msg = "我没法唯一确定你要操作哪条任务，请回复任务 id 或更明确的描述。"
        if lines:
            msg += "\n候选：\n" + "\n".join(lines)
        return msg, None, None, reasoning

    # 统一支持多动作：actions 存在时，逐条执行（insert/update/delete/complete/plan）
    if cmd.actions and isinstance(cmd.actions, list) and len(cmd.actions) > 0:
        log.info("进入多动作处理逻辑，actions数量=%d", len(cmd.actions))
        day_tasks = repo.tasks_for_date(today)
        snap_payload = [
            {"id": t.id, "start_time": t.start_time, "end_time": t.end_time, "title": t.title, "priority": t.priority, "status": t.status, "is_fixed": t.is_fixed}
            for t in day_tasks
        ]
        # 多动作执行前拍快照，方便撤销
        repo.save_snapshot(snap_payload)

        inserted = 0
        updated = 0
        deleted = 0
        completed = 0
        overflow = None
        lines: List[str] = []
        tips_lines: List[str] = []

        for idx, a in enumerate(cmd.actions):
            log.debug("处理第%d个action: %s", idx + 1, a)
            if not isinstance(a, dict):
                continue
            act = (a.get("action") or "").lower().strip()

            # 计划/新增：若没给 start/end，则自动排入
            if act in ("insert", "plan"):
                title = (a.get("title") or "").strip()
                log.info("处理plan/insert action: title=%s, act=%s", title, act)
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
                    start_iso = _to_iso(slot_s)
                    end_iso = _to_iso(slot_e)
                    log.info("找到空闲时间段: %s - %s", start_iso, end_iso)

                # 重新规划时：若当日已有同标题的待办任务，则更新其时间/memo，避免重复创建
                if act == "plan":
                    existing_pending = [
                        t for t in day_tasks
                        if (t.title or "").strip() == title and (str(t.status or "").lower() == "pending")
                    ]
                    if existing_pending:
                        target_task = existing_pending[0]
                        payload = {"start_time": start_iso, "end_time": end_iso}
                        if final_memo is not None:
                            payload["memo"] = final_memo
                        if pr in ("high", "normal", "low"):
                            payload["priority"] = pr
                        repo.update_task(target_task.id, payload)
                        log.info("重新规划：更新已有待办任务 id=%s title=%s -> %s–%s", target_task.id, title, start_iso[:16], end_iso[:16])
                        updated += 1
                        date_str, start_hm = _utc_iso_to_local_display(start_iso)
                        _, end_hm = _utc_iso_to_local_display(end_iso)
                        _, old_start_hm = _utc_iso_to_local_display(target_task.start_time)
                        _, old_end_hm = _utc_iso_to_local_display(target_task.end_time)
                        lines.append(
                            f"- 重排：{date_str} {start_hm}–{end_hm} {title}（原 {old_start_hm}–{old_end_hm}）"
                        )
                        day_tasks = repo.tasks_for_date(today)
                        continue

                # 记录过去某天已做的事：NLP 会返回 status="done"，避免被逾期重排到未来
                task_status = "done" if a.get("status") == "done" else "pending"
                # 兜底：若插入的任务结束日期早于今天，视为“记录过去”，直接标为已完成，避免被逾期重排
                if task_status == "pending" and end_iso:
                    end_local_date = _parse_dt_local_date(end_iso)
                    if end_local_date is not None and end_local_date < today:
                        task_status = "done"
                        log.info("插入任务结束日早于今天，视为记录过去，自动标为 done: title=%s end_local=%s", title, end_local_date)
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
                    raise
                try:
                    created = repo.insert_task(t)
                    log.info("任务插入成功: id=%s, title=%s", created.id, created.title)
                    inserted += 1
                except Exception as e:
                    log.exception("任务插入失败: title=%s, error=%s", title, e)
                    raise
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
                day_tasks = repo.tasks_for_date(today)
                continue

            # 完成/进度更新：优先按 id/时间/标题定位
            if act in ("complete",):
                target = _resolve_target_from_action(a, today)
                if not target:
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
                continue

            if act == "delete":
                target = _resolve_target_from_action(a, today)
                if not target:
                    continue
                repo.delete_task(target.id)
                deleted += 1
                del_date, del_st = _utc_iso_to_local_display(target.start_time)
                _, del_et = _utc_iso_to_local_display(target.end_time)
                lines.append(f"- 删除：{del_date} {del_st}–{del_et} {target.title}")
                continue

            if act == "update":
                target = _resolve_target_from_action(a, today)
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

                # 安全兜底：LLM 有时只给出 target_task_id 但没给时间变化字段。
                # 对“推迟/延后/延期/延迟”这种口令，默认顺延 60min（含“半”则 30min）。
                if delta is None and not new_start_iso and not new_end_iso and a.get("duration_minutes") is None:
                    txt = (user_text or "")
                    delta = 30 if ("半" in txt) else 60

                # 先应用位移（若有）
                if delta is not None:
                    st = st + timedelta(minutes=int(delta))
                    et = st + timedelta(minutes=duration_min)

                # 再根据 new_start_iso / new_end_iso 调整：
                # - 若两者都给：直接以两者为准
                # - 若只给 new_start_iso：保持原时长不变，从新起点往后推 duration_min 分钟
                # - 若只给 new_end_iso：**保持开始时间不变**，仅改结束时间（如「结束时间设成现在」）
                if new_start_iso and new_end_iso:
                    st2 = _parse_dt_naive(new_start_iso)
                    et2 = _parse_dt_naive(new_end_iso)
                    new_start = _to_iso(st2)
                    new_end = _to_iso(et2)
                elif new_start_iso and not new_end_iso:
                    st2 = _parse_dt_naive(new_start_iso)
                    et2 = st2 + timedelta(minutes=duration_min)
                    new_start = _to_iso(st2)
                    new_end = _to_iso(et2)
                elif new_end_iso and not new_start_iso:
                    et2 = _parse_dt_naive(new_end_iso)
                    st2 = st  # 仅改结束时间时保持开始时间不变
                    if et2 <= st2:
                        et2 = st2 + timedelta(minutes=1)
                    new_start = _to_iso(st2)
                    new_end = _to_iso(et2)
                else:
                    new_start = _to_iso(st)
                    new_end = _to_iso(et)
                day_tasks2 = repo.tasks_for_date(today)
                try:
                    res: DominoResult = run_domino(day_tasks2, str(target.id), new_start, new_end, workday_end_hour=WORKDAY_END_HOUR)
                except Exception as e:
                    log.exception("run_domino failed: %s", e)
                    # 异常时直接更新目标任务
                    repo.update_task(target.id, {"start_time": new_start, "end_time": new_end})
                    updated += 1
                    _, ns_hm = _utc_iso_to_local_display(new_start)
                    _, ne_hm = _utc_iso_to_local_display(new_end)
                    lines.append(f"- 修改：{target.title} → {ns_hm}–{ne_hm}（排程异常，仅更新当前任务）")
                    continue

                # 若多米诺没有返回任何更新（极端情况下），至少保证当前任务本身被顺延
                if not res.updated_tasks:
                    log.warning("run_domino returned empty updated_tasks for task %s, new_start=%s new_end=%s", target.id, new_start, new_end)
                    repo.update_task(target.id, {"start_time": new_start, "end_time": new_end})
                    updated += 1
                    _, ns_hm = _utc_iso_to_local_display(new_start)
                    _, ne_hm = _utc_iso_to_local_display(new_end)
                    lines.append(f"- 修改：{target.title} → {ns_hm}–{ne_hm}（未影响其他任务）")
                    if res.overflow_warning:
                        overflow = res.overflow_warning
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

                # --- 预览/确认策略：仅当影响多条任务时才需用户确认，单条直接落库 ---
                affected = [u for u in res.updated_tasks if str(u.id) != str(target.id)]
                need_confirm = len(res.updated_tasks) > 1
                if need_confirm:
                    # 构造预览计划（不落库）
                    old_by_id = {str(t.id): t for t in day_tasks2}
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
                    # LM 制定该修改计划的原因，展示到反馈框
                    update_reason = (a.get("reason") or a.get("memo") or cmd.reply_text or "").strip()
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
                    plan = PendingPlan(user_text=user_text, updates=updates, overflow_warning=res.overflow_warning)
                    return "\n".join(preview_lines), None, plan, reasoning

                # 不需要确认：直接落库
                shifted = 0
                for u in res.updated_tasks:
                    repo.update_task(u.id, {"start_time": u.start_time, "end_time": u.end_time})
                    if str(u.id) != str(target.id):
                        shifted += 1
                if a.get("memo"):
                    repo.update_task(target.id, {"memo": a.get("memo")})
                updated += 1
                upd_date, upd_st = _utc_iso_to_local_display(new_start)
                _, upd_et = _utc_iso_to_local_display(new_end)
                lines.append(f"- 修改：{target.title} → {upd_date} {upd_st}–{upd_et}（顺延 {shifted} 条）")
                if res.overflow_warning:
                    overflow = res.overflow_warning
                continue

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
            from ai.nlp import _time_desc_to_iso
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
                start_iso = _time_desc_to_iso(today, time_desc)
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
            start = _to_iso(slot_s)
            end = _to_iso(slot_e)
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
        repo.insert_task(t)
        date_str, start_hm = _utc_iso_to_local_display(start)
        _, end_hm = _utc_iso_to_local_display(end)
        time_str = f"（{date_str} {start_hm}–{end_hm}）" if date_str else ""
        return cmd.reply_text or f"已为您新增任务：{title}{time_str}。", None, None, reasoning

    if cmd.intent == "delete":
        target = _resolve_target_task(cmd, today)
        if not target:
            return "未找到要删除的任务，请说明是「几点」或「哪条」。", None, None, reasoning
        repo.delete_task(target.id)
        return cmd.reply_text or "已删除该任务。", None, None, reasoning

    if cmd.intent == "update":
        target = _resolve_target_task(cmd, today)
        if not target:
            return "未找到要修改的任务，请说明是「下午几点的会」等。", None, None, reasoning
        if target.is_fixed:
            return "该任务已锁定，暂不自动调整。", None, None, reasoning

        # 计算新时间
        st = datetime.fromisoformat(target.start_time.replace("Z", "+00:00"))
        et = datetime.fromisoformat(target.end_time.replace("Z", "+00:00"))
        duration_min = (et - st).total_seconds() / 60
        if cmd.delta_minutes is not None:
            st = st + timedelta(minutes=cmd.delta_minutes)
            et = st + timedelta(minutes=duration_min)
        new_start = st.strftime("%Y-%m-%dT%H:%M:%S")
        new_end = et.strftime("%Y-%m-%dT%H:%M:%S")
        if cmd.new_start_iso:
            new_start = cmd.new_start_iso
        if cmd.new_end_iso:
            new_end = cmd.new_end_iso
        elif cmd.new_start_iso and not cmd.new_end_iso:
            new_end = (datetime.fromisoformat(cmd.new_start_iso) + timedelta(minutes=duration_min)).strftime("%Y-%m-%dT%H:%M:%S")

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
        
        plan = PendingPlan(user_text=user_text, updates=updates, overflow_warning=res.overflow_warning)
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
