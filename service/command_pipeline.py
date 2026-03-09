from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional
import logging

from ai.models import CommandIntent
from db.models import Task
from config import AGENT_ONLY_WRITE_MODE


log = logging.getLogger("service.command_pipeline")


@dataclass
class PipelineResult:
    cmd: CommandIntent
    reasoning: str
    source: str  # "agent" | "legacy"
    agent_meta: Optional[Dict[str, Any]] = None


def _title_key(v: Any) -> str:
    return str(v or "").strip().lower()


def _extract_unique_pending_by_title(day_tasks_for_ai: List[Task]) -> Dict[str, str]:
    by_title: Dict[str, List[str]] = {}
    for t in (day_tasks_for_ai or []):
        status = str(getattr(t, "status", "") or "").lower()
        if status == "done":
            continue
        key = _title_key(getattr(t, "title", ""))
        tid = str(getattr(t, "id", "") or "").strip()
        if not key or not tid:
            continue
        by_title.setdefault(key, []).append(tid)
    out: Dict[str, str] = {}
    for k, ids in by_title.items():
        uniq = list(dict.fromkeys(ids))
        if len(uniq) == 1:
            out[k] = uniq[0]
    return out


def _extract_unique_from_agent_facts(agent_meta: Dict[str, Any]) -> Dict[str, str]:
    tool_facts = (agent_meta or {}).get("tool_facts") if isinstance(agent_meta, dict) else None
    by_title = (tool_facts or {}).get("tasks_by_title") if isinstance(tool_facts, dict) else None
    out: Dict[str, str] = {}
    if not isinstance(by_title, dict):
        return out
    for k, rows in by_title.items():
        if not isinstance(rows, list):
            continue
        ids = [str((r or {}).get("id") or "").strip() for r in rows if isinstance(r, dict)]
        ids = [x for x in ids if x]
        uniq = list(dict.fromkeys(ids))
        if len(uniq) == 1:
            out[_title_key(k)] = uniq[0]
    return out


def _rewrite_legacy_actions_with_agent_facts(
    cmd: CommandIntent,
    day_tasks_for_ai: List[Task],
    agent_meta: Optional[Dict[str, Any]],
) -> int:
    """
    回退到 legacy 后，尽量复用 agent 已查到的事实，避免“查过又当没查”。
    规则：仅对“无显式时间锚点的 insert”且能唯一命中已存在任务时，改写为 plan（复用已有任务）。
    """
    if not isinstance(getattr(cmd, "actions", None), list):
        return 0
    if not bool((agent_meta or {}).get("protocol_failed")):
        return 0
    unique_from_facts = _extract_unique_from_agent_facts(agent_meta or {})
    unique_from_day = _extract_unique_pending_by_title(day_tasks_for_ai)
    rewritten = 0

    for a in cmd.actions:
        if not isinstance(a, dict):
            continue
        act = str(a.get("action", "")).lower().strip()
        if act != "insert":
            continue
        has_time_anchor = bool(
            a.get("start_iso")
            or a.get("end_iso")
            or a.get("new_start_iso")
            or a.get("new_end_iso")
            or a.get("start_time")
            or a.get("end_time")
        )
        if has_time_anchor:
            continue
        title = (
            a.get("title")
            or a.get("target_title")
            or a.get("target_title_expr")
        )
        key = _title_key(title)
        if not key:
            continue
        matched_id = unique_from_facts.get(key) or unique_from_day.get(key)
        if not matched_id:
            continue
        a["action"] = "plan"
        a["target_task_id"] = matched_id
        a["_fact_reuse"] = True
        rewritten += 1
    return rewritten


def _agent_only_legacy_block_cmd() -> CommandIntent:
    return CommandIntent(
        intent="query",
        actions=[],
        need_clarification=True,
        clarification_question="当前未获得稳定的 agent 结构化输出，请重试或补充更明确时间/目标。",
        reply_text="当前请求需要 agent 成功解析后才能执行写入。本次已停止写入，请重试。",
        parse_source="agent_required",
    )


def run_command_pipeline(
    user_text: str,
    today: date,
    now_dt: datetime,
    day_tasks_for_ai: List[Task],
    conversation_history: Optional[List[Dict[str, str]]],
    use_agent_mode: bool,
    plan_critic_enabled: bool,
    run_agent_fn: Callable[..., Any],
    run_plan_critic_fn: Callable[..., Any],
    resolve_graph_intent_fn: Callable[..., CommandIntent],
    parse_user_command_fn: Callable[..., CommandIntent],
    apply_guardrails_fn: Callable[..., CommandIntent],
    normalize_command_intent_fn: Callable[..., CommandIntent],
    get_agent_meta_fn: Optional[Callable[[], Dict[str, Any]]] = None,
) -> PipelineResult:
    """
    统一命令编译流水线（系统级）：
    NL -> (Agent or Legacy) -> Guardrails -> IR Normalizer -> CommandIntent
    """
    # 统一策略：不向模型传历史对话
    conversation_history = None

    cmd: Optional[CommandIntent] = None
    reasoning = ""
    source = "legacy"
    agent_meta: Dict[str, Any] = {}

    if use_agent_mode:
        log.info("pipeline: trying agent mode")
        try:
            graph_cmd = run_agent_fn(
                user_text,
                today=today,
                now_dt=now_dt,
                conversation_history=conversation_history,
            )
            if graph_cmd:
                source = "agent"
                reasoning = "[AGENT] " + (getattr(graph_cmd, "reasoning", "") or "")
                if plan_critic_enabled and graph_cmd.intent in ("mutate", "replan_all"):
                    reviewed = run_plan_critic_fn(
                        user_text=user_text,
                        proposed_graph=graph_cmd,
                        today=today,
                        now_dt=now_dt,
                    )
                    if reviewed is not None:
                        graph_cmd = reviewed
                        reasoning = "[AGENT+CRITIC] " + (getattr(graph_cmd, "reasoning", "") or "")
                cmd = resolve_graph_intent_fn(graph_cmd, today, day_tasks_for_ai, now_dt)
                if isinstance(cmd, CommandIntent) and not getattr(cmd, "parse_source", None):
                    cmd.parse_source = "agent_resolved"
                log.info(
                    "pipeline: agent resolved intent=%s actions=%d",
                    getattr(cmd, "intent", None),
                    len(cmd.actions) if isinstance(getattr(cmd, "actions", None), list) else 0,
                )
        except Exception as e:
            log.exception("pipeline: agent path failed, fallback legacy: %s", e)
            cmd = None
        finally:
            if callable(get_agent_meta_fn):
                try:
                    agent_meta = get_agent_meta_fn() or {}
                except Exception:
                    agent_meta = {}

    if cmd is None:
        source = "legacy"
        if use_agent_mode and AGENT_ONLY_WRITE_MODE:
            cmd = _agent_only_legacy_block_cmd()
            reasoning = "[LEGACY-BLOCKED] agent-only write mode"
            log.warning("pipeline: agent failed and legacy write path is blocked by AGENT_ONLY_WRITE_MODE")
            if getattr(cmd, "intent", "") != "replan_all":
                cmd = apply_guardrails_fn(cmd, today, day_tasks_for_ai, user_text, now_dt=now_dt)
            cmd = normalize_command_intent_fn(cmd, today, day_tasks_for_ai, user_text=user_text, now_dt=now_dt)
            return PipelineResult(cmd=cmd, reasoning=reasoning, source=source, agent_meta=agent_meta)

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
            for t in (day_tasks_for_ai or [])
        ]
        cmd = parse_user_command_fn(
            user_text,
            today,
            day_tasks=tasks_ctx,
            now_dt=now_dt,
            conversation_history=conversation_history,
        )
        rewritten = _rewrite_legacy_actions_with_agent_facts(cmd, day_tasks_for_ai, agent_meta)
        if rewritten > 0:
            log.info("pipeline: legacy actions rewritten with agent facts count=%d", rewritten)
        reasoning = "[LEGACY] " + (getattr(cmd, "reasoning", "") or "")
        log.info(
            "pipeline: legacy parsed intent=%s actions=%d",
            getattr(cmd, "intent", None),
            len(cmd.actions) if isinstance(getattr(cmd, "actions", None), list) else 0,
        )

    # 系统级统一规范化：两条路径共享同一 guardrails + normalizer
    if getattr(cmd, "intent", "") != "replan_all":
        cmd = apply_guardrails_fn(cmd, today, day_tasks_for_ai, user_text, now_dt=now_dt)
    cmd = normalize_command_intent_fn(cmd, today, day_tasks_for_ai, user_text=user_text, now_dt=now_dt)

    return PipelineResult(cmd=cmd, reasoning=reasoning, source=source, agent_meta=agent_meta)
