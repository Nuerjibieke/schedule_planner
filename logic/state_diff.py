"""声明式目标态(Desired State) 与当前任务列表的差分引擎。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from db.models import Task


def _parse_utc_naive(value: Optional[str], naive_as_local: bool) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s[:19])
        except Exception:
            return None
    if dt.tzinfo is None:
        if naive_as_local:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            dt = dt.replace(tzinfo=local_tz)
        else:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _normalize_iso(value: Optional[str]) -> str:
    """默认把无时区字符串按 UTC 解释，生成稳定比较键。"""
    dt = _parse_utc_naive(value, naive_as_local=False)
    if dt is None:
        s = str(value or "").strip()
        return s[:19]
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _time_equivalent(a: Optional[str], b: Optional[str]) -> bool:
    """
    时间等价判定（容忍双语义）：
    - 通道1：无时区按 UTC 解释
    - 通道2：无时区按本地时间解释
    任一通道命中即视为同一时刻。
    """
    if (a is None or str(a).strip() == "") and (b is None or str(b).strip() == ""):
        return True
    a1 = _parse_utc_naive(a, naive_as_local=False)
    b1 = _parse_utc_naive(b, naive_as_local=False)
    if a1 is not None and b1 is not None and a1 == b1:
        return True
    a2 = _parse_utc_naive(a, naive_as_local=True)
    b2 = _parse_utc_naive(b, naive_as_local=True)
    return a2 is not None and b2 is not None and a2 == b2


def _task_signature(title: str, start_time: str, end_time: str) -> Tuple[str, str, str]:
    return ((title or "").strip(), _normalize_iso(start_time), _normalize_iso(end_time))


def _desired_signature(item: Dict[str, Any]) -> Tuple[str, str, str]:
    title = str(item.get("title", "")).strip()
    start = item.get("start_time") or item.get("start_iso") or item.get("new_start_iso")
    end = item.get("end_time") or item.get("end_iso") or item.get("new_end_iso")
    return (title, _normalize_iso(start), _normalize_iso(end))


def _norm_title(title: Any) -> str:
    return str(title or "").strip().lower()


def _task_id(task: Optional[Task]) -> str:
    if task is None:
        return ""
    return str(getattr(task, "id", "") or "").strip()


def _pick_unique_unclaimed(candidates: List[Task], claimed_ids: Set[str]) -> Optional[Task]:
    remain = [t for t in candidates if _task_id(t) and _task_id(t) not in claimed_ids]
    if len(remain) == 1:
        return remain[0]
    return None


def _semantic_match_task(
    item: Dict[str, Any],
    title_to_tasks: Dict[str, List[Task]],
    claimed_ids: Set[str],
) -> Optional[Task]:
    """
    保守语义匹配：
    - 仅在候选唯一时复用已有任务
    - 若候选不唯一，返回 None（宁可新增，也不冒险误绑）
    """
    title_key = _norm_title(item.get("title"))
    if not title_key:
        return None
    candidates = [
        t for t in (title_to_tasks.get(title_key) or [])
        if _task_id(t) and _task_id(t) not in claimed_ids
    ]
    if not candidates:
        return None

    start = item.get("start_time") or item.get("start_iso") or item.get("new_start_iso")
    end = item.get("end_time") or item.get("end_iso") or item.get("new_end_iso")
    start_str = str(start or "").strip()
    end_str = str(end or "").strip()
    if start_str:
        candidates = [t for t in candidates if _time_equivalent(getattr(t, "start_time", ""), start_str)]
    if end_str:
        candidates = [t for t in candidates if _time_equivalent(getattr(t, "end_time", ""), end_str)]
    if not candidates:
        return None

    desired_status = str(item.get("status", "") or "").strip().lower()
    if desired_status in ("pending", "done"):
        same_status = [t for t in candidates if str(getattr(t, "status", "") or "").lower() == desired_status]
        if len(same_status) == 1:
            return same_status[0]
        if same_status:
            candidates = same_status

    return candidates[0] if len(candidates) == 1 else None


def _changed_fields(current: Task, desired: Dict[str, Any]) -> Dict[str, Any]:
    """抽取差异字段；仅返回真正变化的键。"""
    out: Dict[str, Any] = {}

    d_title = desired.get("title")
    d_start = desired.get("start_time") or desired.get("start_iso") or desired.get("new_start_iso")
    d_end = desired.get("end_time") or desired.get("end_iso") or desired.get("new_end_iso")
    d_priority = desired.get("priority")
    d_status = desired.get("status")
    d_is_fixed = desired.get("is_fixed")
    d_memo = desired.get("memo")

    if isinstance(d_title, str) and d_title.strip() and d_title.strip() != (current.title or "").strip():
        out["title"] = d_title.strip()

    if isinstance(d_start, str) and not _time_equivalent(d_start, current.start_time):
        out["new_start_iso"] = d_start
    if isinstance(d_end, str) and not _time_equivalent(d_end, current.end_time):
        out["new_end_iso"] = d_end

    if isinstance(d_priority, str) and d_priority in ("high", "normal", "low") and d_priority != current.priority:
        out["priority"] = d_priority

    if isinstance(d_status, str) and d_status in ("pending", "done") and d_status != current.status:
        out["status"] = d_status

    if isinstance(d_is_fixed, bool) and d_is_fixed != current.is_fixed:
        out["is_fixed"] = d_is_fixed

    if d_memo is not None and d_memo != current.memo:
        out["memo"] = d_memo

    return out


def compute_state_diff(
    current_tasks: List[Task],
    desired_state: List[Dict[str, Any]],
    allow_delete: bool = False,
) -> List[Dict[str, Any]]:
    """
    将目标态转为动作列表。

    输出动作兼容当前执行层（action + target_task_id/new_start_iso/new_end_iso/...）。
    """
    actions: List[Dict[str, Any]] = []
    if not isinstance(desired_state, list):
        return actions

    by_id: Dict[str, Task] = {str(t.id): t for t in current_tasks if t.id}
    sig_to_tasks: Dict[Tuple[str, str, str], List[Task]] = {}
    title_to_tasks: Dict[str, List[Task]] = {}
    for t in current_tasks:
        sig_to_tasks.setdefault(_task_signature(t.title, t.start_time, t.end_time), []).append(t)
        title_to_tasks.setdefault(_norm_title(t.title), []).append(t)

    matched_ids: Set[str] = set()
    claimed_ids: Set[str] = set()

    for item in desired_state:
        if not isinstance(item, dict):
            continue

        desired_id = item.get("id")
        current: Optional[Task] = None

        if desired_id is not None:
            current = by_id.get(str(desired_id))
            if current is not None and _task_id(current) in claimed_ids:
                current = None

        if current is None:
            current = _pick_unique_unclaimed(sig_to_tasks.get(_desired_signature(item), []), claimed_ids)

        if current is None:
            current = _semantic_match_task(item, title_to_tasks, claimed_ids)

        if current is not None:
            tid = _task_id(current)
            if tid:
                matched_ids.add(tid)
                claimed_ids.add(tid)
            diff = _changed_fields(current, item)
            if diff:
                action = {"action": "update", "target_task_id": str(current.id)}
                action.update(diff)
                action["expected_delta"] = {
                    "target_task_id": str(current.id),
                    "title": diff.get("title"),
                    "new_start_iso": diff.get("new_start_iso"),
                    "new_end_iso": diff.get("new_end_iso"),
                    "priority": diff.get("priority"),
                    "status": diff.get("status"),
                    "memo": diff.get("memo"),
                    "is_fixed": diff.get("is_fixed"),
                }
                actions.append(action)
            continue

        title = str(item.get("title", "")).strip()
        start = item.get("start_time") or item.get("start_iso")
        end = item.get("end_time") or item.get("end_iso")
        if not title:
            continue
        action: Dict[str, Any] = {
            "action": "insert",
            "title": title,
        }
        if isinstance(start, str):
            action["start_iso"] = start
        if isinstance(end, str):
            action["end_iso"] = end
        if item.get("priority") in ("high", "normal", "low"):
            action["priority"] = item.get("priority")
        if item.get("status") in ("pending", "done"):
            action["status"] = item.get("status")
        if isinstance(item.get("is_fixed"), bool):
            action["is_fixed"] = item.get("is_fixed")
        if item.get("memo") is not None:
            action["memo"] = item.get("memo")
        action["expected_delta"] = {
            "title": title,
            "start_iso": action.get("start_iso"),
            "end_iso": action.get("end_iso"),
            "priority": action.get("priority"),
            "status": action.get("status"),
            "memo": action.get("memo"),
            "is_fixed": action.get("is_fixed"),
        }
        actions.append(action)

    if allow_delete:
        for t in current_tasks:
            tid = str(t.id) if t.id else ""
            if not tid:
                continue
            if tid in matched_ids:
                continue
            actions.append({
                "action": "delete",
                "target_task_id": tid,
                "expected_delta": {"deleted_task_id": tid},
            })

    return actions
