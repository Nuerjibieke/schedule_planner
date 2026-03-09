from datetime import date, datetime
from typing import Any, Dict, List, Optional
import hashlib
import json

from ai.models import CommandIntent
from db.models import Task


_VALID_ACTIONS = {"insert", "update", "delete", "complete", "plan"}


def _canonical_action_payload(action: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "action": action.get("action"),
        "target_task_id": action.get("target_task_id"),
        "target_title": action.get("target_title"),
        "title": action.get("title"),
        "start_iso": action.get("start_iso"),
        "end_iso": action.get("end_iso"),
        "new_start_iso": action.get("new_start_iso"),
        "new_end_iso": action.get("new_end_iso"),
        "delta_minutes": action.get("delta_minutes"),
        "duration_minutes": action.get("duration_minutes"),
        "priority": action.get("priority"),
        "status": action.get("status"),
        "is_fixed": action.get("is_fixed"),
        "memo": action.get("memo"),
        "depends_on": action.get("depends_on") if isinstance(action.get("depends_on"), list) else [],
    }


def _canonical_action_id(action: Dict[str, Any]) -> str:
    raw = json.dumps(_canonical_action_payload(action), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _first_non_empty(*vals: Any) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _normalize_depends_on(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, list):
        out: List[str] = []
        for x in value:
            sx = str(x).strip()
            if sx:
                out.append(sx)
        return out
    return []


def _match_task_id_by_title(title: str, day_tasks: List[Task]) -> Optional[str]:
    q = (title or "").strip().lower()
    if not q:
        return None
    # exact > contains
    for t in day_tasks or []:
        tt = str(getattr(t, "title", "") or "").strip().lower()
        if tt and tt == q:
            tid = str(getattr(t, "id", "") or "").strip()
            return tid or None
    for t in day_tasks or []:
        tt = str(getattr(t, "title", "") or "").strip().lower()
        if tt and (q in tt or tt in q):
            tid = str(getattr(t, "id", "") or "").strip()
            return tid or None
    return None


def _build_expected_delta(action: Dict[str, Any]) -> Dict[str, Any]:
    act = str(action.get("action", "")).lower().strip()
    if act in ("insert", "plan"):
        return {
            "title": action.get("title"),
            "start_iso": action.get("start_iso") or action.get("new_start_iso") or action.get("start_time"),
            "end_iso": action.get("end_iso") or action.get("new_end_iso") or action.get("end_time"),
            "priority": action.get("priority"),
            "status": action.get("status") or "pending",
            "memo": action.get("memo"),
            "is_fixed": action.get("is_fixed"),
        }
    if act == "update":
        return {
            "target_task_id": action.get("target_task_id"),
            "new_start_iso": action.get("new_start_iso") or action.get("start_iso") or action.get("start_time"),
            "new_end_iso": action.get("new_end_iso") or action.get("end_iso") or action.get("end_time"),
            "title": action.get("title"),
            "priority": action.get("priority"),
            "status": action.get("status"),
            "memo": action.get("memo"),
            "is_fixed": action.get("is_fixed"),
        }
    if act == "complete":
        return {
            "target_task_id": action.get("target_task_id"),
            "status": action.get("status") or "done",
            "memo": action.get("memo"),
        }
    if act == "delete":
        tid = action.get("target_task_id")
        return {"target_task_id": tid, "deleted_task_id": tid}
    return {}


def normalize_command_intent(
    cmd: CommandIntent,
    today: date,
    day_tasks: Optional[List[Task]] = None,
    user_text: str = "",
    now_dt: Optional[datetime] = None,
) -> CommandIntent:
    """
    统一 IR 归一化层：
    - 统一 action 字段别名
    - 补齐 target/title/time 的最小可执行信息
    - 默认补 expected_delta，支撑执行后验收
    """
    if cmd is None:
        return cmd

    tasks = day_tasks or []
    actions = cmd.actions if isinstance(cmd.actions, list) else []
    normalized: List[Dict[str, Any]] = []
    seen_canonical_ids = set()

    for raw in actions:
        if not isinstance(raw, dict):
            continue

        a = dict(raw)
        act = str(a.get("action", "")).strip().lower()
        if act not in _VALID_ACTIONS:
            continue
        a["action"] = act

        # title / target alias normalization
        if act in ("insert", "plan"):
            title = _first_non_empty(a.get("title"), a.get("target_title_expr"), a.get("target_title"))
            if title:
                a["title"] = title
        if act in ("update", "delete", "complete"):
            if not a.get("target_task_id"):
                target_name = _first_non_empty(a.get("target_title"), a.get("target_title_expr"), a.get("title"))
                if target_name:
                    tid = _match_task_id_by_title(target_name, tasks)
                    if tid:
                        a["target_task_id"] = tid
                        a["target_title"] = target_name

        # duration alias
        if a.get("duration_minutes") is None and a.get("new_duration_minutes") is not None:
            try:
                a["duration_minutes"] = int(a.get("new_duration_minutes"))
            except Exception:
                pass

        # time alias normalization
        if a.get("start_iso") is None and a.get("new_start_iso") is not None:
            a["start_iso"] = a.get("new_start_iso")
        if a.get("end_iso") is None and a.get("new_end_iso") is not None:
            a["end_iso"] = a.get("new_end_iso")
        if a.get("start_iso") is None and a.get("start_time") is not None:
            a["start_iso"] = a.get("start_time")
        if a.get("end_iso") is None and a.get("end_time") is not None:
            a["end_iso"] = a.get("end_time")

        if act == "update":
            if a.get("new_start_iso") is None and a.get("start_iso") is not None:
                a["new_start_iso"] = a.get("start_iso")
            if a.get("new_end_iso") is None and a.get("end_iso") is not None:
                a["new_end_iso"] = a.get("end_iso")

        a["depends_on"] = _normalize_depends_on(a.get("depends_on"))

        # cleanup extraction-only fields
        for k in ("target_title_expr", "start_time_expr", "end_time_expr", "new_duration_minutes", "is_past_record"):
            a.pop(k, None)

        if not isinstance(a.get("expected_delta"), dict):
            a["expected_delta"] = _build_expected_delta(a)

        # Canonical IR marker: unify output semantics across providers.
        canonical_id = _canonical_action_id(a)
        if canonical_id in seen_canonical_ids:
            continue
        seen_canonical_ids.add(canonical_id)
        a["_canonical_id"] = canonical_id
        a["_canonical_intent"] = cmd.intent

        normalized.append(a)

    cmd.actions = normalized
    return cmd
