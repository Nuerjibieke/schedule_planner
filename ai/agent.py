"""
ReAct-style Agent: LLM + Tool-calling loop.

支持 Azure OpenAI 和 Gemini 两种后端的 function calling。
LLM 可以在最终输出 JSON 前调用工具查询数据库、解析时间、检查冲突。
"""
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple
import copy

from .graph_models import GraphIntent
from .models import CommandIntent
from .tools import execute_tool, OPENAI_TOOLS, GEMINI_TOOLS
from .protocol_translator import normalize_graph_protocol
from utils.log_markers import LOG_AGENT, LOG_TOOL

import os
log = logging.getLogger("ai.agent")

# 日志目录
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_LOG_DIR = os.path.join(_PROJECT_ROOT, "log")

_LAST_AGENT_META: Dict[str, Any] = {
    "status": "idle",
    "provider": "",
    "protocol_failed": False,
    "compiler_repaired": False,
    "compiler_repair_low_signal": False,
    "tool_facts": {},
    "error": "",
}


def _safe_json_loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _merge_tool_facts(base: Dict[str, Any], inc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base or {})
    b_title = out.setdefault("tasks_by_title", {})
    b_task = out.setdefault("task_by_id", {})
    i_title = (inc or {}).get("tasks_by_title") or {}
    i_task = (inc or {}).get("task_by_id") or {}
    for k, vals in i_title.items():
        lk = str(k or "").strip().lower()
        if not lk:
            continue
        cur = b_title.setdefault(lk, [])
        for row in (vals or []):
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "").strip()
            if rid and any(str(x.get("id") or "").strip() == rid for x in cur if isinstance(x, dict)):
                continue
            cur.append(row)
    for tid, row in i_task.items():
        sid = str(tid or "").strip()
        if not sid or not isinstance(row, dict):
            continue
        b_task[sid] = row
    return out


def _extract_tool_facts_from_rounds(rounds: List[Dict[str, Any]]) -> Dict[str, Any]:
    facts: Dict[str, Any] = {"tasks_by_title": {}, "task_by_id": {}}
    for r in (rounds or []):
        calls = r.get("tool_calls") if isinstance(r, dict) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "").strip()
            data = _safe_json_loads(call.get("result"))
            if name == "search_tasks" and isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    title = str(row.get("title") or "").strip()
                    tid = str(row.get("id") or "").strip()
                    if title:
                        key = title.lower()
                        facts["tasks_by_title"].setdefault(key, []).append(row)
                    if tid:
                        facts["task_by_id"][tid] = row
            elif name == "get_task_by_id" and isinstance(data, dict):
                tid = str(data.get("id") or "").strip()
                title = str(data.get("title") or "").strip()
                if tid:
                    facts["task_by_id"][tid] = data
                if title:
                    key = title.lower()
                    facts["tasks_by_title"].setdefault(key, []).append(data)
            elif name == "get_tasks_for_date" and isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    title = str(row.get("title") or "").strip()
                    tid = str(row.get("id") or "").strip()
                    if title:
                        key = title.lower()
                        facts["tasks_by_title"].setdefault(key, []).append(row)
                    if tid:
                        facts["task_by_id"][tid] = row
    return facts


def _set_last_agent_meta(
    status: str,
    provider: str,
    protocol_failed: bool,
    compiler_repaired: bool = False,
    compiler_repair_low_signal: bool = False,
    tool_facts: Optional[Dict[str, Any]] = None,
    error: str = "",
) -> None:
    global _LAST_AGENT_META
    _LAST_AGENT_META = {
        "status": status,
        "provider": provider,
        "protocol_failed": bool(protocol_failed),
        "compiler_repaired": bool(compiler_repaired),
        "compiler_repair_low_signal": bool(compiler_repair_low_signal),
        "tool_facts": tool_facts or {},
        "error": error or "",
    }


def get_last_agent_meta() -> Dict[str, Any]:
    """供 pipeline 获取最近一次 agent 运行元数据。"""
    return copy.deepcopy(_LAST_AGENT_META)

def _log_agent_trace(user_text: str, rounds: List[Dict[str, Any]], final_result: Optional[CommandIntent] = None):
    """将 Agent 执行全流程记录到单一文件中。"""
    try:
        if not os.path.exists(AGENT_LOG_DIR):
            os.makedirs(AGENT_LOG_DIR, exist_ok=True)
        
        filepath = os.path.join(AGENT_LOG_DIR, "agent_activity.jsonl")
        
        trace = {
            "timestamp": datetime.now().isoformat(),
            "user_text": user_text,
            "rounds": rounds,
            "final_intent": final_result.intent if final_result else "failed",
            "reply_text": final_result.reply_text if final_result else None
        }
        
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(trace, ensure_ascii=False) + "\n")
            
        # 调试用：直接推到终端 stderr
        if not final_result:
            print(f"\n[AGENT TRACE] Failed to resolve: {user_text}\n", file=sys.stderr)
        
        log.info("Agent trace appended to %s", filepath)
    except Exception as e:
        log.warning("Failed to save agent trace: %s", e)

# ---------------------------------------------------------------------------
# System prompt (much simpler than the old _SCHEMA — tools do the heavy lifting)
# ---------------------------------------------------------------------------

_AGENT_SYSTEM_PROMPT = """\
你是一个智能日程规划助手。用户会用自然语言告诉你他们想做什么，你需要理解意图并输出结构化的任务安排。

你可以使用以下工具来了解用户的日程现状：
- get_tasks_for_date(date_str)：查看某天已有的任务列表
- search_tasks(keyword)：按关键词搜索任务
- get_task_by_id(task_id)：按 ID 查看任务详情
- parse_time_expr(expr)：将自然语言时间（如"晚上8点"）转为 ISO 格式
- get_free_slots(date_str, duration_minutes)：查看某天的空闲时段
- check_conflict(date_str, start_time, end_time)：检查某时段是否有冲突
- validate_plan(date_str, proposed_actions)：对候选动作做硬规则校验（UUID/时间/冲突）

当任务信息收集完整后，你**必须**输出一个合法的 JSON 对象。
你的思考过程（Reasoning）应包含在 JSON 的 "reasoning" 字段内，不要在 JSON 之外输出任何文字。

输出格式示例：
{
  "intent": "mutate",
  "reasoning": "用户想在组会后跑步。我查到组会 20:00 结束，所以安排 21:00 跑步。",
  "reply_text": "已为您安排今晚 9 点跑步。",
  "need_clarification": false,
  "clarification_question": "",
  "nodes": [
    {
      "id": "node_1",
      "action": "insert",
      "title": "跑步",
      "estimated_minutes": 30,
      "depends_on": ["组会的UUID"],
      "time_constraint": null
    }
  ]
}

字段说明：
- depends_on：如果新任务必须排在某个已有任务之后，填入那个任务的 UUID。
- time_constraint：只在用户明确说了绝对时间时填写，如"8点"、"下午3点"。
- estimated_minutes：凭常识估算，如开会60、跑步30、吃饭60。
- target_id：当 action 是 update/delete/complete 且已定位到已有任务时，优先填写该任务 ID（完整 UUID 或短前缀）。
- action 只允许：insert / update / delete / complete。
"""

_PLAN_CRITIC_SYSTEM_PROMPT = """\
你是日程计划审查器（Plan Critic）。
你的职责是：检查“候选计划”是否满足用户原始需求；如果不满足，直接修正计划并输出最终 JSON。

强约束：
1) 只输出一个合法 JSON 对象，禁止 markdown、禁止额外解释。
2) 输出结构必须与 GraphIntent 一致：intent/reasoning/reply_text/need_clarification/clarification_question/nodes。
3) 不要照搬错误计划；如果有歧义，设置 need_clarification=true 并给出问题。
4) 优先保证“用户意图满足”，其次保证“可执行（依赖可解析、时间字段合理）”。
"""

_GRAPH_COMPILER_SYSTEM_PROMPT = """\
你是 GraphIntent 协议编译器。
输入可能是带噪声的模型文本、半结构化 JSON 或字段缺失的草稿。
你的唯一任务：输出一个合法的 GraphIntent JSON。

硬约束：
1) 只输出一个 JSON 对象，禁止 markdown、禁止解释。
2) 必须包含字段：intent/reasoning/reply_text/need_clarification/clarification_question/nodes。
3) nodes 中每个节点必须包含：id/action，title 可为空字符串，depends_on 必须是数组。
4) update/delete/complete 若能识别到目标任务 ID，请放入 target_id。
5) 不要编造用户未提供的新事实；信息不足时，need_clarification=true，并给出 clarification_question。
6) action 只允许：insert / update / delete / complete。
"""


def _parse_local_dt(iso_str: str) -> Optional[datetime]:
    """把 ISO 字符串解析为本地 naive datetime；失败返回 None。"""
    if not iso_str:
        return None
    try:
        ss = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().replace(tzinfo=None, microsecond=0)
    except Exception:
        try:
            return datetime.fromisoformat(str(iso_str)[:19]).replace(microsecond=0)
        except Exception:
            return None


def _build_task_snapshot(today: date, now_dt: datetime, window_hours: int = 4, max_items: int = 12) -> str:
    """
    构建动态上下文窗口：
    1) 当前前后 window_hours 内任务
    2) 高优先级或固定任务
    3) 其余任务按时间补齐，最多 max_items 条
    """
    try:
        from db import repo
        tasks = repo.tasks_for_date(today)
    except Exception as e:
        log.warning("Failed to load task snapshot: %s", e)
        return "【今日关键任务快照】\n（加载失败，建议先调用 get_tasks_for_date）"

    if not tasks:
        return "【今日关键任务快照】\n（今日暂无任务）"

    win_start = now_dt - timedelta(hours=window_hours)
    win_end = now_dt + timedelta(hours=window_hours)

    important: List[Tuple[datetime, Any]] = []
    normal: List[Tuple[datetime, Any]] = []
    for t in tasks:
        st = _parse_local_dt(getattr(t, "start_time", ""))
        et = _parse_local_dt(getattr(t, "end_time", ""))
        if st is None:
            st = now_dt
        if et is None:
            et = st

        in_window = st < win_end and et > win_start
        is_high = str(getattr(t, "priority", "normal")).lower() == "high"
        is_fixed = bool(getattr(t, "is_fixed", False))
        if in_window or is_high or is_fixed:
            important.append((st, t))
        else:
            normal.append((st, t))

    important.sort(key=lambda x: x[0])
    normal.sort(key=lambda x: x[0])
    merged = important + normal
    merged = merged[:max_items]

    lines = ["【今日关键任务快照】"]
    for _, t in merged:
        st = _parse_local_dt(getattr(t, "start_time", ""))
        et = _parse_local_dt(getattr(t, "end_time", ""))
        st_s = st.strftime("%H:%M") if st else "??:??"
        et_s = et.strftime("%H:%M") if et else "??:??"
        status = "已完成" if str(getattr(t, "status", "pending")).lower() == "done" else "待办"
        tags = []
        if bool(getattr(t, "is_fixed", False)):
            tags.append("固定")
        if str(getattr(t, "priority", "normal")).lower() == "high":
            tags.append("高优")
        tag_s = f" [{'|'.join(tags)}]" if tags else ""
        tid = str(getattr(t, "id", ""))[:8]
        title = str(getattr(t, "title", "未命名任务"))
        lines.append(f"- {tid} | [{status}]{tag_s} {st_s}-{et_s} | {title}")

    if len(tasks) > len(merged):
        lines.append(f"- ... 已省略 {len(tasks) - len(merged)} 条非关键任务")
    return "\n".join(lines)


def _build_context_header(today: date, now_dt: datetime, include_task_snapshot: bool = False) -> str:
    """构建系统上下文（默认仅时间；可选注入关键任务快照）。"""
    weekday = "一二三四五六日"[now_dt.weekday()]
    if include_task_snapshot:
        task_snapshot = _build_task_snapshot(today, now_dt)
    else:
        task_snapshot = "【任务快照】\n（未预注入。若信息不足，请先调用工具 get_tasks_for_date/search_tasks）"
    header = (
        f"当前状态：用户通过 UI 与你交互。你会看到 UTC 时间，但你要转换为本地时间给用户看。\n"
        f"当前时间（本地时间，UTC+8）：{now_dt.strftime('%Y-%m-%d %H:%M:%S')}，星期{weekday}\n"
        f"今天日期（本地）：{today.isoformat()}\n"
        f"{task_snapshot}\n"
    )
    return header


# ---------------------------------------------------------------------------
def _tool_call_signature(name: str, args: Dict[str, Any]) -> str:
    try:
        payload = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        payload = str(args)
    return f"{name}:{payload}"


def _tool_name_signature(names: List[str]) -> Optional[str]:
    normalized = [str(x or "").strip().lower() for x in names if str(x or "").strip()]
    if not normalized:
        return None
    return "|".join(normalized)


def _detect_tool_stagnation(last_sig: Optional[str], repeat_count: int, signatures: List[str], threshold: int = 2) -> Tuple[Optional[str], int, bool]:
    """
    Detect repeated identical tool-call batches.
    Returns: (new_last_sig, new_repeat_count, should_force_finalize)
    """
    curr = "|".join(signatures) if signatures else None
    if curr and curr == last_sig:
        repeat_count += 1
    else:
        repeat_count = 1 if curr else 0
    force_finalize = bool(curr) and repeat_count >= max(1, threshold)
    return curr, repeat_count, force_finalize


def _is_low_signal_invalid_final(raw_output: str) -> bool:
    s = str(raw_output or "").strip()
    if not s:
        return True
    lower = s.lower()
    if lower in ("```", "```json", "```json```", "```json\n```", "```json```", "```json ```"):
        return True
    if s.startswith("```") and s.endswith("```") and len(s) <= 24:
        return True
    if len(s) <= 8:
        return True
    return False


def _trim_json_for_prompt(payload: Any, max_chars: int = 3000) -> str:
    try:
        text = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        text = str(payload or "")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 64] + "...(truncated)..."


def _run_openai_graph_compiler(
    client: Any,
    model_name: str,
    user_text: str,
    raw_output: str,
    today: date,
    now_dt: datetime,
    tool_facts: Optional[Dict[str, Any]] = None,
    tool_rounds: Optional[List[Dict[str, Any]]] = None,
) -> Optional[GraphIntent]:
    """
    协议修复层：当 planner 最终输出不可解析时，用专用编译提示词二次提取 GraphIntent。
    该阶段只做“文本 -> 合法 JSON”编译，不做工具调用。
    """
    from config import AGENT_COMPILER_ENABLED, AGENT_COMPILER_MAX_RETRIES, AGENT_COMPILER_MODEL

    if not AGENT_COMPILER_ENABLED:
        return None
    raw = (raw_output or "").strip()
    if not raw:
        return None

    max_rounds = max(1, int(AGENT_COMPILER_MAX_RETRIES or 1))
    compiler_models: List[str] = []
    preferred_model = str(AGENT_COMPILER_MODEL or "").strip()
    if preferred_model:
        compiler_models.append(preferred_model)
    if model_name not in compiler_models:
        compiler_models.append(model_name)

    # 避免把超长噪声直接塞给编译器，保留头尾最有信息量的片段。
    if len(raw) > 6000:
        raw = raw[:3000] + "\n...\n" + raw[-2000:]

    facts_text = _trim_json_for_prompt(tool_facts or {}, max_chars=3500)
    rounds_text = _trim_json_for_prompt(tool_rounds or [], max_chars=3500)

    for comp_model in compiler_models:
        messages = [
            {"role": "system", "content": _GRAPH_COMPILER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"当前时间：{now_dt.strftime('%Y-%m-%d %H:%M:%S')}，今天：{today.isoformat()}\n"
                    f"用户请求：{user_text}\n"
                    f"待编译输出：\n{raw}\n"
                    "以下是本轮工具查询得到的结构化事实（必须优先复用，不要丢失已查询到的任务 ID）：\n"
                    f"{facts_text}\n"
                    "以下是本轮工具调用轨迹（节选）：\n"
                    f"{rounds_text}\n"
                ),
            },
        ]
        for round_idx in range(max_rounds):
            try:
                resp = client.chat.completions.create(
                    model=comp_model,
                    messages=messages,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                candidate_raw = (resp.choices[0].message.content or "").strip()
                parsed = _parse_agent_response(candidate_raw, today, user_text)
                if parsed is not None:
                    log.info(
                        "graph compiler repaired invalid final output model=%s round=%d",
                        comp_model,
                        round_idx + 1,
                    )
                    return parsed
                messages.append({"role": "assistant", "content": candidate_raw[:4000]})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一条仍不是合法 GraphIntent JSON。"
                            "请只返回一个合法 JSON 对象。"
                        ),
                    }
                )
            except Exception as e:
                log.warning(
                    "graph compiler failed model=%s round=%d err=%s",
                    comp_model,
                    round_idx + 1,
                    e,
                )
                break
    return None


# ---------------------------------------------------------------------------
def _run_openai_format_agent(
    client: Any,
    model_name: str,
    user_text: str,
    today: date,
    now_dt: datetime,
    max_rounds: int = 5,
    conversation_history: Optional[List[Dict]] = None,
) -> Tuple[Optional[GraphIntent], List[Dict[str, Any]]]:
    """通用的 OpenAI 格式 ReAct 循环（Azure 或兼容 OpenAI 的三方 API）。"""
    trace_rounds = []
    from config import AGENT_INCLUDE_TASK_SNAPSHOT

    context = _build_context_header(today, now_dt, include_task_snapshot=AGENT_INCLUDE_TASK_SNAPSHOT)
    system_msg = _AGENT_SYSTEM_PROMPT + "\n" + context

    messages = [{"role": "system", "content": system_msg}]
    if conversation_history:
        recent = conversation_history[-6:]
        for msg in recent:
            if msg.get("role") in ("user", "assistant"):
                messages.append({
                    "role": msg["role"], 
                    "content": (msg.get("content") or "")[:500]
                })
    messages.append({"role": "user", "content": user_text})
    force_no_tools = False
    last_tool_sig: Optional[str] = None
    repeat_tool_rounds = 0
    last_tool_name_sig: Optional[str] = None
    repeat_tool_name_rounds = 0

    for round_idx in range(max_rounds):
        try:
            log.info("openai-format agent round %d: sending %d messages", round_idx + 1, len(messages))
            resp = client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=OPENAI_TOOLS,
                tool_choice="none" if force_no_tools else "auto",
                temperature=0.1,
                response_format={"type": "json_object"} if "vision" not in model_name else None
            )
            choice = resp.choices[0]
            msg = choice.message

            if msg.tool_calls:
                sigs: List[str] = []
                names: List[str] = []
                for tc in msg.tool_calls:
                    try:
                        sig_args = json.loads(tc.function.arguments)
                    except Exception:
                        sig_args = {}
                    sigs.append(_tool_call_signature(tc.function.name, sig_args))
                    names.append(str(tc.function.name or ""))
                last_tool_sig, repeat_tool_rounds, should_force_finalize = _detect_tool_stagnation(
                    last_tool_sig, repeat_tool_rounds, sigs, threshold=2
                )
                name_sig = _tool_name_signature(names)
                if name_sig and name_sig == last_tool_name_sig:
                    repeat_tool_name_rounds += 1
                else:
                    repeat_tool_name_rounds = 1 if name_sig else 0
                last_tool_name_sig = name_sig
                if name_sig and repeat_tool_name_rounds >= 3:
                    should_force_finalize = True
                if should_force_finalize:
                    trace_rounds.append(
                        {
                            "round": round_idx + 1,
                            "tool_calls": [
                                {"name": tc.function.name, "arguments": tc.function.arguments}
                                for tc in msg.tool_calls
                            ],
                            "stagnation_guard": True,
                            "repeat_tool_name_rounds": repeat_tool_name_rounds,
                        }
                    )
                    messages.append({
                        "role": "assistant",
                        "content": "检测到重复工具调用，停止继续查询。",
                    })
                    messages.append({
                        "role": "user",
                        "content": (
                            "你连续重复调用了同样工具。"
                            "请停止继续调用工具，直接输出最终 JSON。"
                            "若信息仍不足，请设置 need_clarification=true 并给出 clarification_question。"
                        ),
                    })
                    force_no_tools = True
                    log.warning("openai-format agent stagnation guard triggered at round %d", round_idx + 1)
                    continue
                force_no_tools = False
                log.info("%s openai-format agent round %d: %d tool calls", LOG_TOOL, round_idx + 1, len(msg.tool_calls))
                current_round = {"round": round_idx + 1, "tool_calls": []}
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                })
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        fn_args = {}
                    log.info("%s openai-format agent tool_call %s(%s)", LOG_TOOL, fn_name, fn_args)
                    result = execute_tool(fn_name, fn_args)
                    current_round["tool_calls"].append({
                        "name": fn_name,
                        "args": fn_args,
                        "result": result
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                trace_rounds.append(current_round)
                continue

            raw = (msg.content or "").strip()
            force_no_tools = False
            log.info("openai-format agent: final answer len=%d", len(raw))
            res = _parse_agent_response(raw, today, user_text)
            trace_item: Dict[str, Any] = {
                "round": round_idx + 1,
                "final_answer": raw
            }
            if res is not None:
                trace_rounds.append(trace_item)
                return res, trace_rounds

            trace_item["invalid_final"] = True
            trace_rounds.append(trace_item)
            if round_idx < max_rounds - 1:
                compiler_low_signal = _is_low_signal_invalid_final(raw)
                compiler_facts = _extract_tool_facts_from_rounds(trace_rounds)
                compiler_rounds = [
                    r for r in trace_rounds
                    if isinstance(r, dict) and isinstance(r.get("tool_calls"), list)
                ]
                # 先走一次“协议编译器”修复：将坏文本编译成合法 GraphIntent JSON。
                compiled = _run_openai_graph_compiler(
                    client=client,
                    model_name=model_name,
                    user_text=user_text,
                    raw_output=raw,
                    today=today,
                    now_dt=now_dt,
                    tool_facts=compiler_facts,
                    tool_rounds=compiler_rounds,
                )
                if compiled is not None:
                    trace_rounds.append(
                        {
                            "round": round_idx + 1,
                            "compiler_repaired": True,
                            "compiler_repair_low_signal": compiler_low_signal,
                            "final_answer": compiled.model_dump(mode="json"),
                        }
                    )
                    return compiled, trace_rounds
                # 同 provider 内自修复：要求模型把“无效最终输出”改为可解析 JSON
                # 避免把无意义碎片（如 ```）写回上下文，防止模型陷入坏循环。
                if raw and raw.strip() not in ("```", "```json", "```JSON"):
                    messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        "你上一条最终输出不是可解析 JSON。"
                        "请仅返回一个合法 JSON 对象，禁止 markdown 代码块、禁止额外解释。"
                    ),
                })
                log.warning("openai-format agent round %d invalid final output, retrying finalize", round_idx + 1)
                continue
            return None, trace_rounds

        except Exception as e:
            log.exception("openai-format agent round %d failed: %s", round_idx + 1, e)
            return None, trace_rounds

    log.warning("openai-format agent: exceeded max rounds (%d)", max_rounds)
    return None, trace_rounds


def _run_openai_plan_critic(
    client: Any,
    model_name: str,
    user_text: str,
    proposed_graph: GraphIntent,
    today: date,
    now_dt: datetime,
    max_rounds: int = 1,
) -> Optional[GraphIntent]:
    """
    对 planner 产出的 GraphIntent 做二次审查与修正。
    失败时返回 None（由上层回退到原计划）。
    """
    from config import AGENT_INCLUDE_TASK_SNAPSHOT

    context = _build_context_header(today, now_dt, include_task_snapshot=AGENT_INCLUDE_TASK_SNAPSHOT)
    proposed_json = proposed_graph.model_dump_json(ensure_ascii=False)
    system_msg = _PLAN_CRITIC_SYSTEM_PROMPT + "\n" + context
    user_msg = (
        "请审查并必要时修正下面的候选计划。\n"
        f"用户原始请求：{user_text}\n"
        f"候选计划（GraphIntent JSON）：{proposed_json}\n"
        "请返回修正后的最终 GraphIntent JSON。"
    )
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

    for round_idx in range(max_rounds):
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            raw = (resp.choices[0].message.content or "").strip()
            reviewed = _parse_agent_response(raw, today, user_text)
            if reviewed is not None:
                return reviewed
            # 自修复：强制仅返回 JSON
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": "你上一条输出不是合法 GraphIntent JSON。请只返回合法 JSON 对象。"
            })
        except Exception as e:
            log.warning("plan critic round %d failed: %s", round_idx + 1, e)
            return None
    return None


def _run_azure_plan_critic(
    user_text: str,
    proposed_graph: GraphIntent,
    today: date,
    now_dt: datetime,
    max_rounds: int = 1,
) -> Optional[GraphIntent]:
    from config import (
        AZURE_OPENAI_API_KEY,
        AZURE_OPENAI_ENDPOINT,
        AZURE_OPENAI_API_VERSION,
        AZURE_OPENAI_API_VERSIONS,
        AZURE_OPENAI_DEPLOYMENT,
        AZURE_OPENAI_DEPLOYMENTS,
    )
    try:
        from openai import AzureOpenAI
    except ImportError:
        return None
    if not AZURE_OPENAI_API_KEY:
        return None

    api_versions = [v.strip() for v in (AZURE_OPENAI_API_VERSIONS or "").split(",") if v.strip()]
    if AZURE_OPENAI_API_VERSION and AZURE_OPENAI_API_VERSION not in api_versions:
        api_versions.insert(0, AZURE_OPENAI_API_VERSION)
    if not api_versions:
        api_versions = ["2024-07-18"]

    deployments = [AZURE_OPENAI_DEPLOYMENT]
    for x in (x.strip() for x in (AZURE_OPENAI_DEPLOYMENTS or "").split(",") if x.strip()):
        if x not in deployments:
            deployments.append(x)

    for api_ver in api_versions:
        for dep in deployments:
            try:
                client = AzureOpenAI(
                    api_key=AZURE_OPENAI_API_KEY,
                    api_version=api_ver,
                    azure_endpoint=AZURE_OPENAI_ENDPOINT,
                )
                reviewed = _run_openai_plan_critic(
                    client=client,
                    model_name=dep,
                    user_text=user_text,
                    proposed_graph=proposed_graph,
                    today=today,
                    now_dt=now_dt,
                    max_rounds=max_rounds,
                )
                if reviewed is not None:
                    return reviewed
            except Exception as e:
                log.warning("azure plan critic failed api_version=%s dep=%s err=%s", api_ver, dep, e)
                continue
    return None


def run_plan_critic(
    user_text: str,
    proposed_graph: GraphIntent,
    today: Optional[date] = None,
    now_dt: Optional[datetime] = None,
) -> Optional[GraphIntent]:
    """
    Planner 二阶段：对候选图进行一次模型审查。
    返回修正后的 GraphIntent；失败返回 None（上层应继续使用原计划）。
    """
    from config import PLAN_CRITIC_MAX_ROUNDS

    if not isinstance(proposed_graph, GraphIntent):
        return None
    today = today or date.today()
    now_dt = now_dt or datetime.now().replace(microsecond=0)
    max_rounds = max(1, PLAN_CRITIC_MAX_ROUNDS)
    return _run_azure_plan_critic(
        user_text=user_text,
        proposed_graph=proposed_graph,
        today=today,
        now_dt=now_dt,
        max_rounds=max_rounds,
    )


def _run_azure_agent(
    user_text: str,
    today: date,
    now_dt: datetime,
    max_rounds: int = 5,
    conversation_history: Optional[List[Dict]] = None,
) -> Tuple[Optional[GraphIntent], List[Dict[str, Any]]]:
    """通过 Azure OpenAI 的 function calling 实现 ReAct 循环。"""
    from config import (
        AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT,
        AZURE_OPENAI_API_VERSION, AZURE_OPENAI_API_VERSIONS,
        AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_DEPLOYMENTS,
    )
    try:
        from openai import AzureOpenAI
    except ImportError:
        return None, []

    if not AZURE_OPENAI_API_KEY:
        return None, []

    api_versions = [v.strip() for v in (AZURE_OPENAI_API_VERSIONS or "").split(",") if v.strip()]
    if AZURE_OPENAI_API_VERSION and AZURE_OPENAI_API_VERSION not in api_versions:
        api_versions.insert(0, AZURE_OPENAI_API_VERSION)
    if not api_versions: api_versions = ["2024-07-18"]

    deployments = [AZURE_OPENAI_DEPLOYMENT]
    for x in (x.strip() for x in (AZURE_OPENAI_DEPLOYMENTS or "").split(",") if x.strip()):
        if x not in deployments: deployments.append(x)

    for api_ver in api_versions:
        for dep in deployments:
            try:
                client = AzureOpenAI(
                    api_key=AZURE_OPENAI_API_KEY,
                    api_version=api_ver,
                    azure_endpoint=AZURE_OPENAI_ENDPOINT,
                )
                log.info("azure agent: trying api_version=%s deployment=%s", api_ver, dep)
                return _run_openai_format_agent(client, dep, user_text, today, now_dt, max_rounds, conversation_history)
            except Exception as e:
                log.warning("azure agent: failed with version %s: %s", api_ver, e)
    return None, []


def _run_gemini_agent(
    user_text: str,
    today: date,
    now_dt: datetime,
    max_rounds: int = 5,
    conversation_history: Optional[List[Dict]] = None,
) -> Tuple[Optional[GraphIntent], List[Dict[str, Any]]]:
    """通过 Gemini REST API 或 OpenAI 兼容接口实现 ReAct 循环。"""
    from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_BASE_URL, GEMINI_API_TYPE
    if not GEMINI_API_KEY:
        return None, []

    model_name = GEMINI_MODEL or "gemini-3-pro-preview-thinking"
    
    # 如果是 OpenAI 兼容模式
    if (GEMINI_API_TYPE or "").lower() == "openai":
        try:
            from openai import OpenAI
            base_url = (GEMINI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            client = OpenAI(api_key=GEMINI_API_KEY, base_url=base_url)
            log.info("gemini agent (openai-style): using model %s and base_url %s", model_name, base_url)
            return _run_openai_format_agent(client, model_name, user_text, today, now_dt, max_rounds, conversation_history)
        except Exception as e:
            log.exception("gemini agent (openai-style) failed: %s", e)
            return None, []

    # 默认 Google REST 模式
    trace_rounds = []
    try:
        import httpx
    except ImportError:
        return None, trace_rounds

    from config import AGENT_INCLUDE_TASK_SNAPSHOT

    context = _build_context_header(today, now_dt, include_task_snapshot=AGENT_INCLUDE_TASK_SNAPSHOT)
    system_instruction = _AGENT_SYSTEM_PROMPT + "\n" + context
    contents = []
    if conversation_history:
        recent = conversation_history[-6:]
        for msg in recent:
            role = "user" if msg.get("role") == "user" else "model"
            contents.append({"role": role, "parts": [{"text": (msg.get("content") or "")[:500]}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    base_url = (GEMINI_BASE_URL or "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{base_url}/v1beta/models/{model_name}:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    force_no_tools = False
    last_tool_sig: Optional[str] = None
    repeat_tool_rounds = 0
    last_tool_name_sig: Optional[str] = None
    repeat_tool_name_rounds = 0

    for round_idx in range(max_rounds):
        try:
            body = {
                "system_instruction": {"parts": [{"text": system_instruction}]},
                "contents": contents,
                "tools": GEMINI_TOOLS,
                "tool_config": {"function_calling_config": {"mode": "NONE" if force_no_tools else "AUTO"}},
                "generationConfig": {
                    "temperature": 0.1,
                    "response_mime_type": "application/json"
                },
            }
            log.info("gemini agent round %d: sending request", round_idx + 1)
            with httpx.Client(timeout=30) as client:
                resp = client.post(url, json=body, headers=headers, params=params)

            if resp.status_code != 200:
                log.warning("gemini agent: HTTP %d: %s", resp.status_code, resp.text[:500])
                return None, trace_rounds

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates: return None, trace_rounds
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts: return None, trace_rounds

            function_calls = [p for p in parts if "functionCall" in p]
            if function_calls:
                sigs: List[str] = []
                names: List[str] = []
                for fc_part in function_calls:
                    fc = fc_part["functionCall"]
                    sigs.append(_tool_call_signature(fc.get("name", ""), fc.get("args", {}) or {}))
                    names.append(str(fc.get("name", "") or ""))
                last_tool_sig, repeat_tool_rounds, should_force_finalize = _detect_tool_stagnation(
                    last_tool_sig, repeat_tool_rounds, sigs, threshold=2
                )
                name_sig = _tool_name_signature(names)
                if name_sig and name_sig == last_tool_name_sig:
                    repeat_tool_name_rounds += 1
                else:
                    repeat_tool_name_rounds = 1 if name_sig else 0
                last_tool_name_sig = name_sig
                if name_sig and repeat_tool_name_rounds >= 3:
                    should_force_finalize = True
                if should_force_finalize:
                    contents.append({"role": "model", "parts": parts})
                    contents.append({
                        "role": "user",
                        "parts": [{
                            "text": (
                                "你连续重复调用了同样工具。"
                                "请停止继续调用工具，直接输出最终 JSON。"
                                "若信息不足，请返回 need_clarification=true 并给出 clarification_question。"
                            )
                        }],
                    })
                    trace_rounds.append(
                        {
                            "round": round_idx + 1,
                            "tool_calls": [{"name": fc_part["functionCall"].get("name"), "args": fc_part["functionCall"].get("args", {})} for fc_part in function_calls],
                            "stagnation_guard": True,
                            "repeat_tool_name_rounds": repeat_tool_name_rounds,
                        }
                    )
                    force_no_tools = True
                    log.warning("gemini agent stagnation guard triggered at round %d", round_idx + 1)
                    continue
                force_no_tools = False
                log.info("%s gemini agent round %d: %d function calls", LOG_TOOL, round_idx + 1, len(function_calls))
                current_round = {"round": round_idx + 1, "tool_calls": []}
                contents.append({"role": "model", "parts": parts})
                tool_response_parts = []
                for fc_part in function_calls:
                    fc = fc_part["functionCall"]
                    fn_name = fc["name"]
                    fn_args = fc.get("args", {})
                    log.info("%s gemini agent tool_call %s(%s)", LOG_TOOL, fn_name, fn_args)
                    result_str = execute_tool(fn_name, fn_args)
                    current_round["tool_calls"].append({"name": fn_name, "args": fn_args, "result": result_str})
                    try:
                        result_obj = json.loads(result_str)
                    except json.JSONDecodeError:
                        result_obj = {"result": result_str}
                    tool_response_parts.append({"functionResponse": {"name": fn_name, "response": result_obj}})
                contents.append({"role": "user", "parts": tool_response_parts})
                trace_rounds.append(current_round)
                continue
            
            text_parts = [p.get("text", "") for p in parts if "text" in p]
            raw = " ".join(text_parts).strip()
            force_no_tools = False
            res = _parse_agent_response(raw, today, user_text)
            trace_item: Dict[str, Any] = {"round": round_idx + 1, "final_answer": raw}
            if res is not None:
                trace_rounds.append(trace_item)
                return res, trace_rounds

            trace_item["invalid_final"] = True
            trace_rounds.append(trace_item)
            if round_idx < max_rounds - 1:
                if raw and raw.strip() not in ("```", "```json", "```JSON"):
                    contents.append({"role": "model", "parts": [{"text": raw}]})
                contents.append({
                    "role": "user",
                    "parts": [{
                        "text": "你上一条最终输出不是可解析 JSON。请仅返回一个合法 JSON 对象，禁止 markdown 代码块、禁止额外解释。"
                    }],
                })
                log.warning("gemini agent round %d invalid final output, retrying finalize", round_idx + 1)
                continue
            return None, trace_rounds
        except Exception as e:
            log.exception("gemini agent round %d failed: %s", round_idx + 1, e)
            return None, trace_rounds
    return None, trace_rounds


# ---------------------------------------------------------------------------
# Response parser: JSON text -> CommandIntent
# ---------------------------------------------------------------------------

def _parse_any_dt(value: Any) -> Optional[datetime]:
    """尽量解析时间文本为本地 naive datetime；失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        try:
            return datetime.fromisoformat(value[:19])
        except Exception:
            return None


def _normalize_graph_payload(payload: Any) -> Any:
    """
    对模型输出做最小归一化，避免轻微 schema 偏差导致整轮失败。
    不改 prompt，只在解析阶段做容错。
    """
    if not isinstance(payload, dict):
        return payload

    payload = normalize_graph_protocol(payload)

    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return payload

    for node in nodes:
        if not isinstance(node, dict):
            continue

        act = str(node.get("action", "") or "").strip().lower()

        # 兼容目标标识别名：target_task_id/task_id -> target_id
        if not node.get("target_id"):
            target_alias = node.get("target_task_id") or node.get("task_id")
            if isinstance(target_alias, str) and target_alias.strip():
                node["target_id"] = target_alias.strip()

        # 允许非 insert 节点缺 title：仅从“标题语义字段”回填，避免把 UUID 填成 title。
        if not isinstance(node.get("title"), str) or not node.get("title", "").strip():
            title_alias = node.get("target_title") or node.get("target_title_expr")
            if isinstance(title_alias, str):
                node["title"] = title_alias.strip()
            elif node.get("title") is None:
                node["title"] = ""

        # depends_on 允许模型误返回字符串，统一转列表
        deps = node.get("depends_on")
        if deps is None:
            node["depends_on"] = []
        elif isinstance(deps, str):
            dep = deps.strip()
            node["depends_on"] = [dep] if dep else []
        elif isinstance(deps, list):
            node["depends_on"] = [str(x).strip() for x in deps if str(x).strip()]

        tc = node.get("time_constraint")
        if isinstance(tc, dict):
            start_raw = tc.get("start_time") or tc.get("start") or tc.get("from")
            end_raw = tc.get("end_time") or tc.get("end") or tc.get("to")

            # DAGNode.time_constraint 是字符串：优先使用显式开始时间锚点
            if isinstance(start_raw, str) and start_raw.strip():
                node["time_constraint"] = start_raw.strip()
            elif isinstance(end_raw, str) and end_raw.strip():
                node["time_constraint"] = end_raw.strip()
            else:
                node["time_constraint"] = None

            # 若未给 estimated_minutes，但 start/end 都可解析，则自动补齐
            est = node.get("estimated_minutes")
            if (est is None or est == "" or est == 0):
                st = _parse_any_dt(start_raw)
                et = _parse_any_dt(end_raw)
                if st is not None and et is not None and et > st:
                    node["estimated_minutes"] = int((et - st).total_seconds() / 60)
        elif tc is not None and not isinstance(tc, str):
            node["time_constraint"] = str(tc)

    return payload


def _parse_agent_response(raw: str, today: date, user_text: str) -> Optional[GraphIntent]:
    """解析 agent 的最终 JSON 响应为 DAG GraphIntent。使用正则增强稳定性。"""
    import re
    from .graph_models import GraphIntent

    raw_text = (raw or "").strip()
    if not raw_text:
        log.warning("Agent response is empty")
        return None

    # 尝试提取最外层 JSON 块（适配前后夹杂文本/markdown 的场景）
    match = re.search(r"(\{.*\})", raw_text, re.DOTALL)
    if not match:
        log.warning("No JSON block found in agent response: %s", raw_text[:500])
        return None

    json_str = match.group(1)
    try:
        payload = json.loads(json_str)
    except Exception as e:
        log.warning("JSON extraction found, but json.loads failed: %s. JSON: %s", e, json_str[:800])
        return None

    payload = _normalize_graph_payload(payload)
    try:
        return GraphIntent.model_validate(payload)
    except Exception as e:
        log.warning("JSON validation failed after normalization: %s. JSON: %s", e, json_str[:800])
        return None

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_agent(
    user_text: str,
    today: Optional[date] = None,
    now_dt: Optional[datetime] = None,
    conversation_history: Optional[List[Dict]] = None,
) -> Optional[GraphIntent]:
    """
    主入口：运行 tool-calling agent。
    优先 Gemini（支持 openai-style，如 https://yunwu.ai/v1），fallback Azure OpenAI。
    返回由 DAG Tree 推演的 GraphIntent 或 None。
    """
    from config import AGENT_MAX_TOOL_ROUNDS

    today = today or date.today()
    now_dt = now_dt or datetime.now().replace(microsecond=0)
    max_rounds = AGENT_MAX_TOOL_ROUNDS
    # 系统级策略：不向模型传历史对话，避免“上一轮记忆”污染当前回合。
    conversation_history = None

    def _compiler_flags(rounds: List[Dict[str, Any]]) -> Tuple[bool, bool]:
        repaired = False
        low_signal = False
        for r in (rounds or []):
            if not isinstance(r, dict):
                continue
            if bool(r.get("compiler_repaired")):
                repaired = True
                if bool(r.get("compiler_repair_low_signal")):
                    low_signal = True
        return repaired, low_signal

    try:
        _set_last_agent_meta(
            status="running",
            provider="",
            protocol_failed=False,
            compiler_repaired=False,
            compiler_repair_low_signal=False,
            tool_facts={},
            error="",
        )
        all_rounds: List[Dict[str, Any]] = []
        all_facts: Dict[str, Any] = {"tasks_by_title": {}, "task_by_id": {}}

        msg = f"\n>>> {LOG_AGENT} [AGENT START] text='{user_text}' today={today.isoformat()}\n"
        print(msg, end="", flush=True)
        log.info(msg.strip())

        # Try Gemini first (例如 openai-style 的 yunwu.ai/v1)
        result, rounds = _run_gemini_agent(user_text, today, now_dt, max_rounds, conversation_history)
        all_rounds.extend(rounds or [])
        all_facts = _merge_tool_facts(all_facts, _extract_tool_facts_from_rounds(rounds or []))
        if result:
            compiler_repaired, compiler_low_signal = _compiler_flags(all_rounds)
            print(f">>> {LOG_AGENT} [AGENT SUCCESS] Gemini, intent={result.intent}\n", flush=True)
            _set_last_agent_meta(
                status="success",
                provider="gemini",
                protocol_failed=False,
                compiler_repaired=compiler_repaired,
                compiler_repair_low_signal=compiler_low_signal,
                tool_facts=all_facts,
            )
            _log_agent_trace(user_text, all_rounds, result)
            return result

        # Fallback to Azure
        print(f">>> {LOG_AGENT} [AGENT FALLBACK] Gemini failed, trying Azure...\n", flush=True)
        result, rounds = _run_azure_agent(user_text, today, now_dt, max_rounds, conversation_history)
        all_rounds.extend(rounds or [])
        all_facts = _merge_tool_facts(all_facts, _extract_tool_facts_from_rounds(rounds or []))
        if result:
            compiler_repaired, compiler_low_signal = _compiler_flags(all_rounds)
            print(f">>> {LOG_AGENT} [AGENT SUCCESS] Azure, intent={result.intent}\n", flush=True)
            _set_last_agent_meta(
                status="success",
                provider="azure",
                protocol_failed=False,
                compiler_repaired=compiler_repaired,
                compiler_repair_low_signal=compiler_low_signal,
                tool_facts=all_facts,
            )
            _log_agent_trace(user_text, all_rounds, result)
            return result

        print(f">>> {LOG_AGENT} [AGENT FAILED] All providers failed.\n", flush=True)
        protocol_failed = any(bool(r.get("invalid_final")) for r in all_rounds if isinstance(r, dict))
        compiler_repaired, compiler_low_signal = _compiler_flags(all_rounds)
        _set_last_agent_meta(
            status="failed",
            provider="all",
            protocol_failed=protocol_failed,
            compiler_repaired=compiler_repaired,
            compiler_repair_low_signal=compiler_low_signal,
            tool_facts=all_facts,
            error="all providers failed",
        )
        _log_agent_trace(user_text, all_rounds, None)
        return None
    except Exception as e:
        print(f"\n!!! [AGENT FATAL ERROR] {e}\n", flush=True)
        import traceback
        traceback.print_exc()
        # 依然记录 trace 确保日志不丢
        _set_last_agent_meta(
            status="failed",
            provider="fatal",
            protocol_failed=False,
            compiler_repaired=False,
            compiler_repair_low_signal=False,
            tool_facts={},
            error=str(e),
        )
        _log_agent_trace(user_text, [], None)
        return None
