"""
多米诺骨牌排程算法：
- 被改任务的新时间固定，其余任务若与之重叠则整体向后平移，保持时长；
- 顺延后的任务再参与“锚点”，继续推动更后面的任务（骨牌效应）；
- 跳过 is_fixed 任务（不移动、且作为不可穿过的区间）；
- 边界报警：若任一下班时间后结束则返回报警信息。
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from db.models import Task


@dataclass
class DominoResult:
    """排程结果：新布局 + 是否超时报警。"""
    updated_tasks: List[Task] = field(default_factory=list)
    overflow_warning: Optional[str] = None


def _parse_dt(s: str) -> datetime:
    if s.endswith("Z"):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    if "+" in s or (len(s) >= 6 and s[-6] in "+-" and s[-3] == ":"):
        return datetime.fromisoformat(s)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _to_iso(dt: datetime) -> str:
    """转换为 ISO 格式，统一为 UTC 时区后缀格式以便与数据库格式一致。"""
    # 确保是 UTC 时间
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def run_domino(
    day_tasks: List[Task],
    changed_task_id: str,
    new_start: str,
    new_end: str,
    workday_end_hour: int = 22,
) -> DominoResult:
    """
    对「某任务改为 new_start~new_end」做多米诺排程。
    day_tasks：当日所有任务；changed_task_id 为被改动的任务 ID。
    返回 DominoResult，包含需要写回 DB 的更新列表与超时报警文案。
    """
    # 锚点：不可移动的区间。含 is_fixed 任务 + 被改任务的新时间
    anchors: List[Tuple[datetime, datetime]] = []
    for t in day_tasks:
        if t.is_fixed:
            anchors.append((_parse_dt(t.start_time), _parse_dt(t.end_time)))
    anchors.append((_parse_dt(new_start), _parse_dt(new_end)))

    # 可变任务：除被改任务外的非锁定任务，按原开始时间排序
    movables = [
        t for t in day_tasks
        if str(t.id) != str(changed_task_id) and not t.is_fixed
    ]
    movables.sort(key=lambda x: _parse_dt(x.start_time))

    changed_task = next((t for t in day_tasks if str(t.id) == str(changed_task_id)), None)
    id_to_task: dict = {}
    for t in day_tasks:
        if t.is_fixed:
            id_to_task[t.id] = t
        elif str(t.id) == str(changed_task_id) and changed_task:
            id_to_task[t.id] = Task(
                id=t.id, start_time=new_start, end_time=new_end,
                title=t.title, priority=t.priority, status=t.status,
                is_fixed=t.is_fixed, memo=t.memo, created_at=t.created_at,
            )
    # 已占用的区间：锚点 + 顺延后的任务，用于骨牌
    placed: List[Tuple[datetime, datetime]] = list(anchors)
    for t in movables:
        st = _parse_dt(t.start_time)
        et = _parse_dt(t.end_time)
        duration_sec = (et - st).total_seconds()
        new_st = st
        for (a_s, a_e) in placed:
            if st < a_e and a_s < et:
                if a_e > new_st:
                    new_st = a_e
        new_et = new_st + timedelta(seconds=duration_sec)
        id_to_task[t.id] = Task(
            id=t.id, start_time=_to_iso(new_st), end_time=_to_iso(new_et),
            title=t.title, priority=t.priority, status=t.status,
            is_fixed=t.is_fixed, memo=t.memo, created_at=t.created_at,
        )
        placed.append((new_st, new_et))

    result_list = [id_to_task.get(t.id, t) for t in day_tasks]
    # 时间比较：统一格式后再比较（去除时区后缀差异）
    def _normalize_time(s: str) -> str:
        """统一时间格式用于比较：去除时区后缀，统一为 YYYY-MM-DDTHH:MM:SS"""
        if not s:
            return ""
        # 移除时区后缀（+00:00 或 Z）
        s = s.replace("+00:00", "").replace("Z", "")
        if len(s) > 19:
            s = s[:19]
        return s
    
    updated = [
        result_list[i] for i in range(len(day_tasks))
        if _normalize_time(day_tasks[i].start_time) != _normalize_time(result_list[i].start_time) 
        or _normalize_time(day_tasks[i].end_time) != _normalize_time(result_list[i].end_time)
    ]
    warn = None
    # 24小时规划模式下，不再限制工作时间，但可以检查是否跨天
    for t in result_list:
        e = _parse_dt(t.end_time)
        # 如果任务结束时间超过24点（跨天），给出提示
        if e.hour >= 24 or (e.hour == 23 and e.minute > 0):
            # 检查是否真的跨天了（结束时间在第二天）
            task_date = e.date()
            today_date = datetime.now().date()
            if task_date > today_date:
                warn = f"部分任务已延期到明天（{task_date.isoformat()}）。"
                break
    return DominoResult(updated_tasks=updated, overflow_warning=warn)
