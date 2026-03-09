"""
协议翻译层：把模型输出的“近似动作/意图”稳定归一到系统可执行原语。

设计原则：
- 不依赖 prompt 规则，纯解析期容错
- 仅做协议归一，不做业务写库决策
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_INTENT_SYNONYMS = {
    "mutate": "mutate",
    "mutation": "mutate",
    "schedule": "mutate",
    "scheduleactions": "mutate",
    "scheduleaction": "mutate",
    "scheduling": "mutate",
    "create": "mutate",
    "createtasks": "mutate",
    "writetasks": "mutate",
    "modify": "mutate",
    "update": "mutate",
    "query": "query",
    "ask": "query",
    "read": "query",
    "search": "query",
    "replanall": "replan_all",
    "replan": "replan_all",
    "rearrange": "replan_all",
}

_ACTION_SYNONYMS = {
    "insert": "insert",
    "create": "insert",
    "add": "insert",
    "new": "insert",
    "plan": "insert",
    "schedule": "insert",
    "takemedicine": "insert",
    "readpaper": "insert",
    "update": "update",
    "edit": "update",
    "modify": "update",
    "move": "update",
    "reschedule": "update",
    "shift": "update",
    "delete": "delete",
    "remove": "delete",
    "drop": "delete",
    "cancel": "delete",
    "complete": "complete",
    "done": "complete",
    "finish": "complete",
    "markdone": "complete",
}


def _norm_token(value: Any) -> str:
    s = str(value or "").strip().lower()
    # 仅保留字母数字与下划线，消除大小写/连字符/空格噪声
    return re.sub(r"[^a-z0-9_]+", "", s.replace("-", "_").replace(" ", "_"))


def translate_intent(raw_intent: Any, has_nodes: bool = False) -> str:
    token = _norm_token(raw_intent)
    mapped = _INTENT_SYNONYMS.get(token)
    if mapped:
        return mapped
    # 未知 intent：若存在节点，默认 mutate；否则 query
    return "mutate" if has_nodes else "query"


def infer_action(node: Dict[str, Any]) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    has_target = bool(
        str(node.get("target_id") or node.get("target_task_id") or node.get("task_id") or "").strip()
    )
    has_write_fields = any(
        node.get(k) is not None
        for k in (
            "new_start_iso",
            "new_end_iso",
            "start_iso",
            "end_iso",
            "delta_minutes",
            "duration_minutes",
            "priority",
            "memo",
            "is_fixed",
            "status",
        )
    )
    status_token = _norm_token(node.get("status"))
    if has_target and status_token in ("done", "complete", "completed"):
        return "complete"
    if has_target and has_write_fields:
        return "update"
    if has_target:
        return "update"
    if str(node.get("title") or node.get("target_title") or node.get("target_title_expr") or "").strip():
        return "insert"
    return None


def translate_action(raw_action: Any, node: Optional[Dict[str, Any]] = None) -> Optional[str]:
    token = _norm_token(raw_action)
    mapped = _ACTION_SYNONYMS.get(token)
    if mapped:
        return mapped
    if isinstance(node, dict):
        return infer_action(node)
    return None


def normalize_graph_protocol(payload: Any) -> Any:
    """
    归一 GraphIntent 协议的关键枚举字段：
    - intent
    - nodes[].action
    """
    if not isinstance(payload, dict):
        return payload

    nodes = payload.get("nodes")
    has_nodes = isinstance(nodes, list) and len(nodes) > 0
    payload["intent"] = translate_intent(payload.get("intent"), has_nodes=has_nodes)

    if not isinstance(nodes, list):
        return payload

    for node in nodes:
        if not isinstance(node, dict):
            continue
        mapped_action = translate_action(node.get("action"), node=node)
        if mapped_action:
            node["action"] = mapped_action
    return payload

