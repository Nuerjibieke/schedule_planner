from typing import Any, Dict, List, Tuple

from ai.models import CommandIntent
from db.models import Task


_WRITE_INTENTS = {"insert", "update", "delete", "plan", "replan_all"}
_WRITE_ACTIONS = {"insert", "update", "delete", "complete", "plan"}


def _norm_title(v: Any) -> str:
    return str(v or "").strip().lower()


def _unique_pending_by_title(tasks: List[Task]) -> Dict[str, str]:
    grouped: Dict[str, List[str]] = {}
    for t in (tasks or []):
        status = str(getattr(t, "status", "") or "").lower()
        if status == "done":
            continue
        key = _norm_title(getattr(t, "title", ""))
        tid = str(getattr(t, "id", "") or "").strip()
        if not key or not tid:
            continue
        grouped.setdefault(key, []).append(tid)
    out: Dict[str, str] = {}
    for k, ids in grouped.items():
        uniq = list(dict.fromkeys(ids))
        if len(uniq) == 1:
            out[k] = uniq[0]
    return out


def _unique_by_title_from_tool_facts(agent_meta: Dict[str, Any]) -> Dict[str, str]:
    tool_facts = (agent_meta or {}).get("tool_facts") if isinstance(agent_meta, dict) else None
    title_map = (tool_facts or {}).get("tasks_by_title") if isinstance(tool_facts, dict) else None
    out: Dict[str, str] = {}
    if not isinstance(title_map, dict):
        return out
    for title, rows in title_map.items():
        if not isinstance(rows, list):
            continue
        ids = [str((r or {}).get("id") or "").strip() for r in rows if isinstance(r, dict)]
        ids = [x for x in ids if x]
        uniq = list(dict.fromkeys(ids))
        if len(uniq) == 1:
            out[_norm_title(title)] = uniq[0]
    return out


def _is_write_intent(cmd: CommandIntent) -> bool:
    intent = str(getattr(cmd, "intent", "") or "").lower().strip()
    if intent in _WRITE_INTENTS:
        return True
    actions = getattr(cmd, "actions", None)
    if isinstance(actions, list):
        for a in actions:
            if isinstance(a, dict) and str(a.get("action", "") or "").lower().strip() in _WRITE_ACTIONS:
                return True
    return False


def _has_explicit_time_anchor(action: Dict[str, Any]) -> bool:
    if not isinstance(action, dict):
        return False
    for k in ("start_iso", "end_iso", "new_start_iso", "new_end_iso", "start_time", "end_time"):
        v = action.get(k)
        if isinstance(v, str) and v.strip():
            return True
    return False


def evaluate_plan_trust(
    cmd: CommandIntent,
    user_text: str,
    day_tasks: List[Task],
    agent_meta: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    写库前可信度门禁（无 prompt 规则依赖）：
    1) 编译器低信号修复结果不可直接写库
    2) 已查到唯一任务事实时，禁止无锚点同名重复 insert
    3) 禁止把图内 node_x 依赖直接下发执行层
    """
    if not isinstance(cmd, CommandIntent):
        return True, ""
    if not _is_write_intent(cmd):
        return True, ""

    compiler_repaired = bool((agent_meta or {}).get("compiler_repaired"))
    compiler_low_signal = bool((agent_meta or {}).get("compiler_repair_low_signal"))
    actions = cmd.actions if isinstance(cmd.actions, list) else []

    issues: List[str] = []
    if compiler_repaired and compiler_low_signal:
        issues.append("模型最终输出为低信号无效内容，编译器修复结果可信度不足。")

    unique_facts = _unique_by_title_from_tool_facts(agent_meta or {})
    unique_day = _unique_pending_by_title(day_tasks or [])

    for idx, a in enumerate(actions):
        if not isinstance(a, dict):
            continue
        act = str(a.get("action", "") or "").lower().strip()
        if act not in _WRITE_ACTIONS:
            continue

        deps = a.get("depends_on")
        if isinstance(deps, list):
            if any(str(x or "").strip().lower().startswith("node_") for x in deps):
                issues.append(f"第{idx + 1}步依赖仍是图内节点引用，未解析为任务 ID。")

        if act in ("insert", "plan"):
            title = _norm_title(a.get("title") or a.get("target_title") or a.get("target_title_expr"))
            if not title:
                continue
            known_id = unique_facts.get(title) or unique_day.get(title)
            has_target = bool(str(a.get("target_task_id") or "").strip())
            has_time = _has_explicit_time_anchor(a)
            if known_id and (not has_target) and (compiler_repaired or not has_time):
                issues.append(f"第{idx + 1}步尝试新增“{title}”，但本轮已定位到唯一已有任务（{known_id[:8]}...）。")

    if issues:
        preview = "；".join(issues[:3])
        msg = (
            "计划可信度校验未通过，已停止写入。"
            f"原因：{preview} "
            "请重试，或明确说明是“复用已有任务”还是“新增同名任务”。"
        )
        return False, msg

    return True, ""

