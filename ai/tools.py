"""
Agent 工具层：将 repo/guardrails 能力包装成可被 LLM function-calling 调用的工具。
每个工具返回 JSON 字符串，供 LLM 上下文消费。
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from db import repo
from db.models import Task
from ai.guardrails import parse_natural_time
from utils.log_markers import LOG_TOOL

log = logging.getLogger("ai.tools")

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _local_tz():
    tz = datetime.now().astimezone().tzinfo
    return tz or timezone.utc


def _parse_dt_naive(value: str) -> Optional[datetime]:
    """
    把时间解析为 UTC naive datetime（统一比较基准）。
    约定：无时区输入按“用户本地时间”解释，再转 UTC。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            dt = dt.replace(tzinfo=_local_tz()).astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
        return dt
    except Exception:
        try:
            raw = datetime.fromisoformat(str(value)[:19]).replace(microsecond=0)
            return raw.replace(tzinfo=_local_tz()).astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            return None


def _parse_local_naive(value: str) -> Optional[datetime]:
    """
    把时间解析为本地 naive datetime（用于 UI 语义下的时段冲突/空闲搜索）。
    约定：无时区输入视为本地时间。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(microsecond=0)
        return dt.astimezone(_local_tz()).replace(tzinfo=None, microsecond=0)
    except Exception:
        try:
            return datetime.fromisoformat(str(value)[:19]).replace(microsecond=0)
        except Exception:
            return None


def get_tasks_for_date(date_str: str) -> str:
    """获取某日全部任务，返回 JSON 列表。"""
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return json.dumps({"error": f"无效日期格式: {date_str}，请使用 YYYY-MM-DD"}, ensure_ascii=False)
    tasks = repo.tasks_for_date(d)
    result = []
    for t in tasks:
        result.append({
            "id": t.id,
            "title": t.title,
            "start_time": t.start_time,
            "end_time": t.end_time,
            "priority": t.priority,
            "status": t.status,
            "is_fixed": t.is_fixed,
            "memo": t.memo,
        })
    log.info("%s get_tasks_for_date(%s) -> %d tasks", LOG_TOOL, date_str, len(result))
    return json.dumps(result, ensure_ascii=False)


def search_tasks(keyword: str, date_str: Optional[str] = None) -> str:
    """按标题关键词模糊搜索任务。date_str 可选，不提供则搜当天。"""
    try:
        d = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        d = date.today()
    tasks = repo.tasks_by_title(d, keyword, limit=5)
    result = []
    for t in tasks:
        result.append({
            "id": t.id,
            "title": t.title,
            "start_time": t.start_time,
            "end_time": t.end_time,
            "priority": t.priority,
            "status": t.status,
        })
    log.info("%s search_tasks(%s, %s) -> %d results", LOG_TOOL, keyword, date_str, len(result))
    return json.dumps(result, ensure_ascii=False)


def get_task_by_id(task_id: str) -> str:
    """按 ID 查找任务详情。"""
    t = repo.task_by_id(task_id)
    if not t:
        return json.dumps({"error": f"未找到 ID 为 {task_id} 的任务"}, ensure_ascii=False)
    result = {
        "id": t.id,
        "title": t.title,
        "start_time": t.start_time,
        "end_time": t.end_time,
        "priority": t.priority,
        "status": t.status,
        "is_fixed": t.is_fixed,
        "memo": t.memo,
    }
    log.info("%s get_task_by_id(%s) -> %s", LOG_TOOL, task_id, t.title)
    return json.dumps(result, ensure_ascii=False)


def parse_time_expr(expr: str, base_date_str: Optional[str] = None) -> str:
    """将自然语言时间描述解析为 ISO 格式。如 '晚上8点' -> '2026-03-07T20:00:00'。"""
    try:
        base = date.fromisoformat(base_date_str) if base_date_str else date.today()
    except ValueError:
        base = date.today()
    result = parse_natural_time(expr, base_date=base)
    if result:
        iso = result.strftime("%Y-%m-%dT%H:%M:%S")
        log.info("%s parse_time_expr(%s, %s) -> %s", LOG_TOOL, expr, base_date_str, iso)
        return json.dumps({"iso": iso}, ensure_ascii=False)
    else:
        log.warning("%s parse_time_expr(%s, %s) -> failed", LOG_TOOL, expr, base_date_str)
        return json.dumps({"error": f"无法解析时间表达式: {expr}"}, ensure_ascii=False)


def get_free_slots(date_str: str, duration_minutes: int = 60) -> str:
    """查找某日可用的空闲时段（返回最早的 3 个）。"""
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return json.dumps({"error": f"无效日期格式: {date_str}"}, ensure_ascii=False)
    tasks = repo.tasks_for_date(d)
    active = [t for t in tasks if t.status != "done"]
    active.sort(key=lambda t: t.start_time)

    now = datetime.now().replace(microsecond=0)
    if d == date.today():
        cursor = now
    else:
        cursor = datetime.combine(d, datetime.min.time()).replace(hour=8, minute=0)

    slots = []
    max_iter = 200
    for _ in range(max_iter):
        end = cursor + timedelta(minutes=duration_minutes)
        conflict = False
        for t in active:
            ts = _parse_local_naive(t.start_time)
            te = _parse_local_naive(t.end_time)
            if ts is None or te is None:
                continue
            if cursor < te and ts < end:
                cursor = max(cursor, te)
                conflict = True
                break
        if not conflict:
            slots.append({
                "start": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            cursor = end
            if len(slots) >= 3:
                break
    log.info("%s get_free_slots(%s, %dmin) -> %d slots", LOG_TOOL, date_str, duration_minutes, len(slots))
    return json.dumps(slots, ensure_ascii=False)


def check_conflict(date_str: str, start_time: str, end_time: str) -> str:
    """检查指定时间段是否与已有任务冲突。"""
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return json.dumps({"error": f"无效日期格式: {date_str}"}, ensure_ascii=False)
    tasks = repo.tasks_for_date(d)
    active = [t for t in tasks if t.status != "done"]

    new_s = _parse_local_naive(start_time)
    new_e = _parse_local_naive(end_time)
    if new_s is None or new_e is None:
        return json.dumps({"error": "无效的时间格式"}, ensure_ascii=False)

    conflicts = []
    for t in active:
        ts = _parse_local_naive(t.start_time)
        te = _parse_local_naive(t.end_time)
        if ts is None or te is None:
            continue
        if new_s < te and ts < new_e:
            conflicts.append({
                "id": t.id,
                "title": t.title,
                "start_time": t.start_time,
                "end_time": t.end_time,
            })
    result = {
        "has_conflict": len(conflicts) > 0,
        "conflicting_tasks": conflicts,
    }
    log.info(
        "%s check_conflict(%s, %s, %s) -> %d conflicts",
        LOG_TOOL, date_str, start_time, end_time, len(conflicts)
    )
    return json.dumps(result, ensure_ascii=False)


def validate_plan(date_str: str, proposed_actions: List[Dict[str, Any]]) -> str:
    """
    对候选动作做确定性校验（不写库）：
    - target_task_id 是否存在
    - 时间格式是否合法、结束是否晚于开始
    - 与现有任务是否冲突
    """
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return json.dumps({"ok": False, "errors": [f"无效日期格式: {date_str}"]}, ensure_ascii=False)

    if not isinstance(proposed_actions, list):
        return json.dumps({"ok": False, "errors": ["proposed_actions 必须是数组"]}, ensure_ascii=False)

    tasks = repo.tasks_for_date(d)
    active = [t for t in tasks if str(getattr(t, "status", "pending")).lower() != "done"]
    id_set = {str(t.id) for t in tasks}

    errors: List[str] = []
    conflicts: List[Dict[str, Any]] = []
    checked: List[Dict[str, Any]] = []

    for i, action in enumerate(proposed_actions):
        idx = i + 1
        if not isinstance(action, dict):
            errors.append(f"action[{idx}] 不是对象")
            continue

        act = str(action.get("action", "unknown")).lower()
        target_task_id = action.get("target_task_id")
        if target_task_id and str(target_task_id) not in id_set:
            errors.append(f"action[{idx}] target_task_id 不存在: {target_task_id}")

        start_raw = action.get("new_start_iso") or action.get("start_iso") or action.get("start_time")
        end_raw = action.get("new_end_iso") or action.get("end_iso") or action.get("end_time")

        if bool(start_raw) != bool(end_raw):
            errors.append(f"action[{idx}] 时间字段不完整（start/end 需同时提供）")
            checked.append({"index": idx, "action": act, "has_time": False})
            continue
        if not start_raw and not end_raw:
            checked.append({"index": idx, "action": act, "has_time": False})
            continue

        start_dt = _parse_dt_naive(str(start_raw))
        end_dt = _parse_dt_naive(str(end_raw))
        if start_dt is None or end_dt is None:
            errors.append(f"action[{idx}] 时间格式无效: start={start_raw}, end={end_raw}")
            checked.append({"index": idx, "action": act, "has_time": True, "valid_time": False})
            continue
        if end_dt <= start_dt:
            errors.append(f"action[{idx}] 结束时间必须晚于开始时间")

        action_conflicts = []
        for t in active:
            tid = str(t.id)
            if target_task_id and tid == str(target_task_id):
                continue
            ts = _parse_dt_naive(t.start_time)
            te = _parse_dt_naive(t.end_time)
            if ts is None or te is None:
                continue
            if start_dt < te and ts < end_dt:
                item = {
                    "action_index": idx,
                    "action": act,
                    "conflict_task_id": tid,
                    "conflict_title": t.title,
                    "conflict_start": t.start_time,
                    "conflict_end": t.end_time,
                }
                action_conflicts.append(item)
                conflicts.append(item)

        checked.append({
            "index": idx,
            "action": act,
            "has_time": True,
            "valid_time": True,
            "conflict_count": len(action_conflicts),
        })

    result = {
        "ok": len(errors) == 0 and len(conflicts) == 0,
        "errors": errors,
        "conflicts": conflicts,
        "checked_actions": checked,
    }
    log.info(
        "%s validate_plan(%s) -> ok=%s errors=%d conflicts=%d",
        LOG_TOOL, date_str, result["ok"], len(errors), len(conflicts)
    )
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool registry: name -> (function, description, parameters_schema)
# ---------------------------------------------------------------------------

TOOL_REGISTRY: Dict[str, Any] = {
    "get_tasks_for_date": get_tasks_for_date,
    "search_tasks": search_tasks,
    "get_task_by_id": get_task_by_id,
    "parse_time_expr": parse_time_expr,
    "get_free_slots": get_free_slots,
    "check_conflict": check_conflict,
    "validate_plan": validate_plan,
}


def execute_tool(name: str, arguments: dict) -> str:
    """根据工具名和参数执行对应工具，返回 JSON 字符串结果。"""
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    try:
        result = fn(**arguments)
        log.info("%s execute_tool name=%s status=ok", LOG_TOOL, name)
        return result
    except Exception as e:
        log.exception("%s execute_tool name=%s status=failed error=%s", LOG_TOOL, name, e)
        return json.dumps({"error": f"工具执行失败: {str(e)}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# OpenAI-compatible function definitions (for Azure OpenAI / OpenAI SDK)
# ---------------------------------------------------------------------------

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_tasks_for_date",
            "description": "获取指定日期的全部任务列表。返回每个任务的 id、title、start_time、end_time、priority、status 等信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_str": {
                        "type": "string",
                        "description": "日期，格式 YYYY-MM-DD，如 2026-03-07",
                    }
                },
                "required": ["date_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tasks",
            "description": "按标题关键词模糊搜索任务。可指定日期，不指定则搜今天。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，如 '组会'、'论文'",
                    },
                    "date_str": {
                        "type": "string",
                        "description": "可选，日期 YYYY-MM-DD",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_by_id",
            "description": "按任务 ID 查询任务详情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "任务的 UUID",
                    }
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_time_expr",
            "description": "将自然语言时间表达式解析为 ISO 格式时间。如 '晚上8点' -> '2026-03-07T20:00:00'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expr": {
                        "type": "string",
                        "description": "时间表达式，如 '晚上8点'、'明天下午3点'、'下周一上午10点'",
                    },
                    "base_date_str": {
                        "type": "string",
                        "description": "可选，基准日期 YYYY-MM-DD，默认今天",
                    },
                },
                "required": ["expr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_free_slots",
            "description": "查找指定日期的空闲时段（最早的3个）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_str": {
                        "type": "string",
                        "description": "日期 YYYY-MM-DD",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "需要的时长（分钟），默认60",
                    },
                },
                "required": ["date_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_conflict",
            "description": "检查指定时间段是否与已有任务冲突。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_str": {
                        "type": "string",
                        "description": "日期 YYYY-MM-DD",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "开始时间 ISO 格式，如 2026-03-07T20:00:00",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "结束时间 ISO 格式",
                    },
                },
                "required": ["date_str", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_plan",
            "description": "校验候选动作的目标任务和时间合法性，并返回冲突明细。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_str": {
                        "type": "string",
                        "description": "日期 YYYY-MM-DD",
                    },
                    "proposed_actions": {
                        "type": "array",
                        "description": "候选动作数组，每项可含 action/target_task_id/start_iso/end_iso/new_start_iso/new_end_iso",
                        "items": {"type": "object"},
                    },
                },
                "required": ["date_str", "proposed_actions"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Gemini-compatible function declarations
# ---------------------------------------------------------------------------

GEMINI_TOOLS = [
    {
        "function_declarations": [
            {
                "name": "get_tasks_for_date",
                "description": "获取指定日期的全部任务列表。",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "date_str": {"type": "STRING", "description": "日期 YYYY-MM-DD"}
                    },
                    "required": ["date_str"],
                },
            },
            {
                "name": "search_tasks",
                "description": "按标题关键词模糊搜索任务。",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "keyword": {"type": "STRING", "description": "搜索关键词"},
                        "date_str": {"type": "STRING", "description": "可选日期 YYYY-MM-DD"},
                    },
                    "required": ["keyword"],
                },
            },
            {
                "name": "get_task_by_id",
                "description": "按 ID 查询任务详情。",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "task_id": {"type": "STRING", "description": "任务 UUID"}
                    },
                    "required": ["task_id"],
                },
            },
            {
                "name": "parse_time_expr",
                "description": "将自然语言时间表达式解析为 ISO 时间。",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "expr": {"type": "STRING", "description": "时间表达式"},
                        "base_date_str": {"type": "STRING", "description": "基准日期 YYYY-MM-DD"},
                    },
                    "required": ["expr"],
                },
            },
            {
                "name": "get_free_slots",
                "description": "查找指定日期的空闲时段。",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "date_str": {"type": "STRING", "description": "日期 YYYY-MM-DD"},
                        "duration_minutes": {"type": "INTEGER", "description": "需要时长（分钟）"},
                    },
                    "required": ["date_str"],
                },
            },
            {
                "name": "check_conflict",
                "description": "检查时间段是否冲突。",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "date_str": {"type": "STRING", "description": "日期 YYYY-MM-DD"},
                        "start_time": {"type": "STRING", "description": "开始时间 ISO"},
                        "end_time": {"type": "STRING", "description": "结束时间 ISO"},
                    },
                    "required": ["date_str", "start_time", "end_time"],
                },
            },
            {
                "name": "validate_plan",
                "description": "校验候选动作的目标任务和时间合法性，并返回冲突明细。",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "date_str": {"type": "STRING", "description": "日期 YYYY-MM-DD"},
                        "proposed_actions": {
                            "type": "ARRAY",
                            "description": "候选动作数组",
                            "items": {"type": "OBJECT"},
                        },
                    },
                    "required": ["date_str", "proposed_actions"],
                },
            },
        ]
    }
]
