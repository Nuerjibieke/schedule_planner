"""
右脑（AI 层）：听懂人话，输出标准 JSON 数据单。
- 意图识别：Insert / Update / Delete / Query
- 实体提取：目标任务描述、变更参数、备注
- 输出给逻辑层的标准结构，不做计算。
"""
import json
import re
import logging
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Optional, Literal, Any, List, Dict

# 若无 OpenAI，可退回规则解析（仅作占位）
try:
    from openai import OpenAI, AzureOpenAI
except Exception:
    OpenAI = None
    AzureOpenAI = None

# Gemini
try:
    import httpx
except Exception:
    httpx = None

log = logging.getLogger("api.openai.nlp")


IntentType = Literal["insert", "update", "delete", "query", "plan"]


@dataclass
class CommandIntent:
    """解析后的标准单，给逻辑层使用。"""
    intent: IntentType
    # 目标：update/delete 时用。可为 task_id 或由「日期+时间描述」解析出的 target_start_iso
    target_task_id: Optional[str] = None
    target_start_iso: Optional[str] = None  # 如 "2025-01-28T14:00:00"
    # 变更参数：update 时
    delta_minutes: Optional[int] = None
    new_start_iso: Optional[str] = None
    new_end_iso: Optional[str] = None
    # 新增任务：insert 时
    title: Optional[str] = None
    start_iso: Optional[str] = None
    end_iso: Optional[str] = None
    priority: Optional[Literal["high", "normal", "low"]] = None
    # 备注（如「老板加塞汇报」）
    memo: Optional[str] = None
    # 自然语言回复，给用户看
    reply_text: Optional[str] = None
    # LLM 思考过程（如何理解用户意图），供用户查看
    reasoning: Optional[str] = None
    # 整体规划原因（plan intent时提供）
    planning_reason: Optional[str] = None
    # 多动作：允许一句话拆成多条操作（新增/修改/完成/删除/计划）
    actions: Optional[list] = None
    # 需要澄清（例如多任务同名、时间不明确等）
    need_clarification: Optional[bool] = None
    candidates: Optional[list] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


_SCHEMA = """
你是日程助理，只负责把用户自然语言拆解为结构化 JSON 操作清单（不做排班计算）。
请只返回一份 JSON，不要任何额外文字、不要 markdown 代码块包裹。

【非常重要】当 intent 为 "plan" 时，必须提供 planning_reason 字段，详细说明规划思路。planning_reason 必须包含：
1. 为什么这样安排任务的顺序（例如：先完成高优先级任务，再处理需要长时间专注的任务）
2. 为什么这样分配时间（例如：将需要深度思考的任务安排在上午，将简单任务安排在下午）
3. 为什么这样设置优先级（例如：根据任务的重要性和紧急性）
4. 考虑了什么因素（例如：任务之间的依赖关系、用户的工作习惯、任务难度等）
必须提供具体、详细的规划理由（至少50字），不能只是"今天的任务安排"这样泛泛的描述。如果没有提供详细的planning_reason，用户将无法信任规划结果。

【重要】actions 中每条记录的 title 必须是“要排入日程的具体事项名称”，不能包含：
- 用户对系统的请求（如“并给我执行建议”“安排到下班前”“从现在开始安排”等）
- 句尾的祈使、补充说明（如“并给我执行建议。”应忽略，不单独作为一条任务）
只把真正的待办/任务名放入 title，例如用户说“今天的任务是：agent开发（2小时），模型调研（1小时）。从现在开始安排到下班前，并给我执行建议。”则只输出两条 action：title 分别为“agent开发”“模型调研”。

输出格式（严格按此 JSON 结构）：
{{
  "intent": "insert" | "update" | "delete" | "query" | "plan",
  "planning_reason": "整体规划原因（plan intent时必需，详细说明规划思路）",
  "actions": [
    {{
      "action": "insert" | "update" | "delete" | "complete" | "plan",
      "title": "任务名称（仅真实待办，见上方说明）",
      "difficulty": "easy" | "medium" | "hard",
      "duration_minutes": 数字,
      "tips": ["建议1", "建议2"],
      "priority": "high" | "normal" | "low",
      "reason": "该任务的时间安排和优先级规划原因（可选，plan action时提供）",
      "target_task_id": "可选",
      "target_time_desc": "可选",
      "target_title": "可选",
      "delta_minutes": "可选",
      "new_start_iso": "可选",
      "new_end_iso": "可选",
      "start_iso": "可选，insert/plan 且用户指定了该任务开始时刻时必填，格式：今日T HH:MM:00",
      "end_iso": "可选，insert/plan 且用户指定了该任务结束时刻时必填，格式：今日T HH:MM:00",
      "memo": "可选"
    }}
  ],
  "reply_text": "一句简短中文，描述我将做什么",
  "reasoning": "可选，2-5句话说明：① 用户这句话的意图是什么；② 你匹配到了哪条/哪些任务（或为何新建）；③ 你将执行什么操作（思考过程，供用户查看）"
}}

- intent 为 "plan" 时：把用户描述的"今天要做的待办"逐条拆成 action="plan"，每条给 title、duration_minutes、difficulty、tips、reason。**必须提供 planning_reason 字段，详细说明规划思路（至少50字）**。当用户为每条任务给出了具体起止时间（如「13点开始讨论研究方向，到14点。14点半到16点开组会」），每个 action **必须**填写 start_iso 和 end_iso（格式：今日T时:分:00，如 2026-01-30T13:00:00、2026-01-30T14:00:00；14点半即 T14:30:00），否则系统会按「最早空闲段」排入导致时间错误。
- title：仅填具体事项名，不填“并给我执行建议”等对系统的请求。
- duration_minutes：根据用户说的“x小时”或任务难度估算，单位分钟。
- difficulty：根据任务复杂度评估（easy=简单，medium=中等，hard=困难）。
- tips：提供2-4条具体的执行建议，要详细且可操作。
- reason：说明为什么这样安排时间、设置优先级和难度，例如"该任务需要专注时间，安排在上午效率更高"或"这是高优先级任务，需要优先完成"。
- planning_reason：详细说明整体规划思路，必须包含：为什么这样安排任务的顺序、为什么这样分配时间、为什么这样设置优先级、考虑了什么因素。至少50字以上，要能让用户理解你的规划逻辑。不能只是"今天的任务安排"这样泛泛的描述。
- reply_text：一句中文，不描述具体时间计算结果。
- reasoning：用 2-5 句话说明思考过程：① 用户这句话的意图是什么；② 你匹配到了哪条任务（或为何判断为新建）；③ 你将执行什么操作。不要只写一句，要能让用户看到完整的理解过程。
- **重要**：如果用户说"已完成X%"或"完成X%"（X为0-100的数字），这是更新任务进度，不是标记为完成。请返回 action="complete"，并在 memo 字段中包含进度百分比（如"已完成50%"或"progress:50"）。只有当用户明确说"完成"或"已完成"（没有百分比）时，才标记为100%完成。
- **重要**：当用户说「X是A点到B点」或「X是A到B点」（指定某任务的起止时刻）时，必须返回 update 意图，并给出 **new_start_iso** 和 **new_end_iso**（格式为今日日期 T 小时:00:00），不要使用 delta_minutes。例如「研究问题讨论会议是15到16点」应返回 new_start_iso=今日T15:00:00、new_end_iso=今日T16:00:00。只有用户说「推迟」「延后」「晚一点」等未指定具体时刻时，才用 delta_minutes。
- **重要**：如果用户说"无法开始X"、"无法在X点开始Y"、"不能在X点做Y"、"我无法开始X"、"无法在目前规划的时间内做X"、"无法在计划的时间内做X"、"无法在安排的时间内做X"、"我无法在目前规划的时间内做X"等，这是表示需要调整任务时间，应该返回 update 意图（不是 query 或 need_clarification），target_task_id 为任务X或Y的 id，并给出 delta_minutes（如+60表示推迟1小时）或新的时间安排。如果用户没有明确说明新时间，可以默认推迟1小时（delta_minutes=60）。不要返回 query 或 need_clarification 意图。
- **重要**：如果用户说"时间需要X分钟"、"时长为X"、"需要X时间"、"时间改为X"、"改成X分钟"等（X为数字，可能带"分钟"、"小时"、"半"等），这是表示要调整任务的时长（duration_minutes），不是推迟时间（delta_minutes）。请返回 update 意图，给出 target_task_id 和 duration_minutes（将X转换为分钟，如"半小时"=30，"1小时"=60，"30分钟"=30）。如果用户同时说"必须今天做"或"今天做完"，还需要调整开始时间以确保今天能完成。
- **重要**：如果用户对之前的操作结果不满意（如说"不行"、"不对"、"需要今天做"等），请根据对话历史中的预览计划，理解用户要修改的是哪个任务，返回 update 意图并给出 target_task_id。不要返回 plan 或 insert 意图。
- **非常重要**：区分 insert 和 update 意图：
  - 如果用户说"记录"、"添加"、"新增"、"安排"、"加入"等关键词，并且提到了**新的任务名称**（该任务名称不在当日任务列表中），这应该被识别为 **insert** 意图，创建新任务。例如"记录21点开始阅读书籍，半小时"应该被识别为插入新任务"阅读书籍"，从21:00开始，持续30分钟。
  - 只有当用户明确提到**现有任务**的名称或ID时，才应该识别为 **update** 意图。例如"把agent开发推迟半小时"是update，因为"agent开发"是现有任务。
  - 如果用户提到的新任务名称与现有任务名称完全不同，必须识别为 insert，不要误识别为 update。
- **【记录已发生的事】优先规则**：当用户说"从X点到现在我在做Y"、"从X点到刚才我在做Y"、"X点到现在一直在做Y"、"刚才从X点到现在在Y"等（X为小时，Y为**具体做的事/任务名**），这是在**记录已经发生过的活动**，不是要完成或修改当日任务列表里的任何现有任务。必须识别为 **insert** 意图：action="insert"，title=Y（如"调试日程助手代码"），start_iso=今日T X:00:00+00:00（或今日T X:00:00），end_iso=**当前时间**（使用上文给出的「当前时间」的完整值，如 2026-01-29T09:03:24+00:00）。不要返回 complete/update 现有任务，不要用当日任务列表里的任务名去匹配Y。

当前时间：{now_iso}，今天日期：{today}，星期：{weekday}。
"""


def _time_desc_to_iso(today: date, desc: str) -> Optional[str]:
    """将「下午两点」「14:00」「凌晨1点」等转为今日的 ISO 开始时间。"""
    if not desc or not desc.strip():
        return None
    desc = desc.strip()
    # 明天
    base = today
    if "明天" in desc:
        base = today + timedelta(days=1)
        desc = desc.replace("明天", "").strip()
    # 凌晨（0-5点，通常是第二天）
    hour, minute = 0, 0
    if "凌晨" in desc:
        base = today + timedelta(days=1)  # 凌晨通常是第二天
        m = re.search(r"(\d{1,2})\s*点", desc)
        if m:
            hour = int(m.group(1))
            if hour > 5:  # 如果超过5点，可能是当天凌晨
                base = today
        else:
            hour = 1  # 默认凌晨1点
    elif "下午" in desc or "中午" in desc:
        hour = 14 if "两点" in desc or "2点" in desc else (12 if "中午" in desc else 15)
        if "两点" in desc or "2点" in desc:
            hour = 14
        elif "一点" in desc or "1点" in desc:
            hour = 13
        elif "三点" in desc or "3点" in desc:
            hour = 15
        elif "四点" in desc or "4点" in desc:
            hour = 16
    elif "上午" in desc:
        if "九点" in desc or "9点" in desc:
            hour = 9
        elif "十点" in desc or "10点" in desc:
            hour = 10
        else:
            hour = 9
    elif "晚上" in desc:
        hour = 19
    # 直接数字 14:00
    m = re.search(r"(\d{1,2}):(\d{2})", desc)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        # 如果小时数小于6且没有"凌晨"关键词，可能是凌晨时间（第二天）
        if hour < 6 and "凌晨" not in desc and "上午" not in desc:
            base = today + timedelta(days=1)
    else:
        m = re.search(r"(\d{1,2})\s*点", desc)
        if m:
            hour = int(m.group(1))
            # 如果小时数小于6且没有"凌晨"关键词，可能是凌晨时间（第二天）
            if hour < 6 and "凌晨" not in desc and "上午" not in desc:
                base = today + timedelta(days=1)
    return f"{base.isoformat()}T{hour:02d}:{minute:02d}:00"


def _parse_time_range_from_text(today: date, text: str) -> Optional[tuple]:
    """
    从「X是15到16点」「X是14点到17点」等表述解析出 (new_start_iso, new_end_iso)。
    返回 (start_iso, end_iso) 或 None。
    """
    if not text or not text.strip():
        return None
    text = text.strip()
    # 匹配「是 15 到 16 点」「是14点到17点」
    m = re.search(r"是\s*(\d{1,2})\s*点?\s*到\s*(\d{1,2})\s*点", text)
    if m:
        h1, h2 = int(m.group(1)), int(m.group(2))
        if 0 <= h1 <= 23 and 0 <= h2 <= 23 and h1 != h2:
            start_iso = f"{today.isoformat()}T{h1:02d}:00:00"
            end_iso = f"{today.isoformat()}T{h2:02d}:00:00"
            return (start_iso, end_iso)
    # 匹配「是 14:00 到 17:30」等
    m = re.search(r"是\s*(\d{1,2})\s*:\s*(\d{2})\s*到\s*(\d{1,2})\s*:\s*(\d{2})", text)
    if m:
        h1, m1, h2, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        if 0 <= h1 <= 23 and 0 <= m1 <= 59 and 0 <= h2 <= 23 and 0 <= m2 <= 59:
            start_iso = f"{today.isoformat()}T{h1:02d}:{m1:02d}:00"
            end_iso = f"{today.isoformat()}T{h2:02d}:{m2:02d}:00"
            return (start_iso, end_iso)
    return None


def parse_user_command(
    user_text: str,
    today: Optional[date] = None,
    day_tasks: Optional[list] = None,
    now_dt: Optional[datetime] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
) -> CommandIntent:
    """
    把用户一句话解析为标准单。
    若配置了 OpenAI，则用模型；否则用简单规则兜底。
    """
    today = today or date.today()
    # 使用调用方传入的当前系统时间（建议 UTC，与数据库一致）；未传入则用本地 now
    from datetime import timezone as _tz
    _raw = now_dt or datetime.now()
    now_dt = _raw.replace(microsecond=0) if _raw.tzinfo is None else _raw.astimezone(_tz.utc).replace(microsecond=0)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S") + ("+00:00" if now_dt.tzinfo else "")
    if now_dt.tzinfo:
        now_dt = now_dt.replace(tzinfo=None)  # 后续格式用 naive
    weekday = "一二三四五六日"[now_dt.weekday()]
    from config import OPENAI_API_KEY, GEMINI_API_KEY, GEMINI_MODEL
    from config import (
        AZURE_OPENAI_API_KEY,
        AZURE_OPENAI_ENDPOINT,
        AZURE_OPENAI_API_VERSION,
        AZURE_OPENAI_API_VERSIONS,
        AZURE_OPENAI_DEPLOYMENT,
        AZURE_OPENAI_DEPLOYMENTS,
    )

    def _exc_status_code(exc: Exception) -> Optional[int]:
        resp = getattr(exc, "response", None)
        return getattr(resp, "status_code", None) if resp is not None else None

    def _log_http_error(prefix: str, exc: Exception) -> None:
        """把 API 失败的 status/body（截断）写到终端日志，方便排查。"""
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None) if resp is not None else None
        body = ""
        if resp is not None:
            try:
                body = resp.text or ""
            except Exception:
                body = ""
        body = body.replace("\n", "\\n")
        if len(body) > 800:
            body = body[:800] + "...(truncated)"
        log.warning("%s failed status=%s body=%s", prefix, code, body)

    def _is_azure_404_or_not_found(exc: Exception) -> bool:
        """404 或 Resource/Deployment not found 时视为当前 version+deployment 不可用，试下一个。"""
        if _exc_status_code(exc) == 404:
            return True
        msg = str(exc).lower()
        return (
            "resource not found" in msg
            or "deploymentnotfound" in msg
            or "deployment for this resource does not exist" in msg
        )

    def _try_hkust_azure(prompt: str) -> Optional[CommandIntent]:
        """Gemini 限流/不可用时，回退到 HKUST Azure OpenAI。多 api-version × 多 deployment 自动尝试。"""
        if not AZURE_OPENAI_API_KEY or AzureOpenAI is None:
            return None
        try:
            # Azure 端点在部分代理/抓包环境下会 TLS EOF，优先为该域名设置 NO_PROXY 绕过代理
            import os
            from urllib.parse import urlparse

            old_no_proxy = os.environ.get("NO_PROXY", "")
            host = ""
            try:
                host = (urlparse(AZURE_OPENAI_ENDPOINT).hostname or "").strip()
            except Exception:
                host = ""
            if host:
                parts = [p.strip() for p in (old_no_proxy or "").split(",") if p.strip()]
                if host not in parts:
                    parts.append(host)
                os.environ["NO_PROXY"] = ",".join(parts)

            api_versions = [v.strip() for v in (AZURE_OPENAI_API_VERSIONS or "").split(",") if v.strip()]
            if AZURE_OPENAI_API_VERSION and AZURE_OPENAI_API_VERSION not in api_versions:
                api_versions.insert(0, AZURE_OPENAI_API_VERSION)
            if not api_versions:
                api_versions = [AZURE_OPENAI_API_VERSION or "2023-05-15"]

            deployments = [AZURE_OPENAI_DEPLOYMENT]
            for x in (x.strip() for x in (AZURE_OPENAI_DEPLOYMENTS or "").split(",") if x.strip()):
                if x not in deployments:
                    deployments.append(x)

            last_err: Optional[Exception] = None
            raw: Optional[str] = None
            for api_ver in api_versions:
                client = AzureOpenAI(
                    api_key=AZURE_OPENAI_API_KEY,
                    api_version=api_ver,
                    azure_endpoint=AZURE_OPENAI_ENDPOINT,
                )
                for dep in deployments:
                    try:
                        log.info("hkust azure nlp try api_version=%s deployment=%s endpoint=%s", api_ver, dep, AZURE_OPENAI_ENDPOINT)
                        resp = client.chat.completions.create(
                            model=dep,
                            messages=[
                                {"role": "system", "content": prompt},
                                {"role": "user", "content": user_text},
                            ],
                            temperature=0.1,
                        )
                        raw = (resp.choices[0].message.content or "").strip()
                        break
                    except Exception as e:
                        last_err = e
                        if _is_azure_404_or_not_found(e):
                            log.warning("hkust azure 404/not found api_version=%s deployment=%s: %s", api_ver, dep, e)
                            continue
                        raise
                if raw is not None:
                    break

            if raw is None:
                if last_err:
                    raise last_err
                return None

            log.info("hkust azure nlp raw_len=%s", len(raw))
            if log.isEnabledFor(logging.DEBUG):
                log.debug("hkust azure nlp raw=%s", raw[:2000] + ("..." if len(raw) > 2000 else ""))
            start = raw.find("{")
            if start != -1:
                end = raw.rfind("}") + 1
                if end > start:
                    raw = raw[start:end]
            obj = json.loads(raw)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("hkust azure nlp json=%s", json.dumps(obj, ensure_ascii=False)[:2000])
            cmd = _dict_to_intent(obj, today, user_text)
            return _maybe_fill_update_actions(cmd, today, user_text, day_tasks)
        except Exception as e:
            log.exception("hkust azure nlp parse failed: %s", e)
            return None
        finally:
            try:
                import os
                if "old_no_proxy" in locals():
                    if old_no_proxy:
                        os.environ["NO_PROXY"] = old_no_proxy
                    else:
                        os.environ.pop("NO_PROXY", None)
            except Exception:
                pass

    # 当日日程上下文（给 LLM 用来选 task_id）
    # 只传最必要字段，避免 token 爆炸
    tasks_ctx = day_tasks if isinstance(day_tasks, list) else []
    tasks_ctx_json = ""
    try:
        tasks_ctx_json = json.dumps(tasks_ctx, ensure_ascii=False)
    except Exception:
        tasks_ctx_json = "[]"

    def _with_context(system_prompt: str) -> str:
        history_text = ""
        if conversation_history and len(conversation_history) > 0:
            # 只取最近 5 轮对话（10 条消息）作为上下文
            recent_history = conversation_history[-10:]
            history_lines = []
            for msg in recent_history:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user":
                    history_lines.append(f"用户：{content}")
                elif role == "assistant":
                    history_lines.append(f"助手：{content}")
            if history_lines:
                history_text = f"\n【对话历史】\n" + "\n".join(history_lines) + "\n"
        
        return (
            f"{system_prompt}\n\n"
            f"{history_text}"
            f"【当日日程上下文】这是你今天已有的任务清单（含 id）。若需要 update/delete/complete，请优先返回 target_task_id：\n"
            f"{tasks_ctx_json}\n\n"
            f"【补充说明】\n"
            f"- **首先判断意图**：如果用户提到的新任务名称**不在**上述任务列表中，必须是 **insert** 意图，创建新任务。只有当用户明确提到**现有任务**的名称或ID时，才使用 update/delete/complete 意图。\n"
            f"- 从当日任务列表中查找 target_task_id，优先匹配任务标题或时间\n"
            f"- update 操作：必须提供 delta_minutes（时间偏移）或 new_start_iso/new_end_iso（新时间）或 duration_minutes（新时长）中的至少一个\n"
            f"- 如果无法确定要操作的任务，返回 need_clarification=true 并提供 candidates（最多3条）\n"
            f"- 如果用户对之前的操作不满意，结合对话历史中的预览计划理解要修改的任务\n"
            f"- 根据用户表达的自然语言，合理推断时间调整方式（推迟 vs 调整时长 vs 指定新时间）\n"
        )

    # 优先 HKUST Azure OpenAI
    if AZURE_OPENAI_API_KEY and AzureOpenAI is not None:
        prompt = _SCHEMA.format(today=today.isoformat(), now_iso=now_iso, weekday=weekday)
        azure_res = _try_hkust_azure(_with_context(prompt))
        if azure_res is not None:
            log.info("使用 HKUST Azure OpenAI 进行解析")
            return azure_res
    
    # 其次 Gemini（适配你当前 AIza... key）
    if GEMINI_API_KEY and httpx is not None:
        try:
            prompt = _SCHEMA.format(today=today.isoformat(), now_iso=now_iso, weekday=weekday)
            sys_prompt = _with_context(prompt)
            full = f"{sys_prompt}\n用户输入：{user_text}"
            # Google AI Studio / Gemini Developer API：优先 v1beta
            model_name = GEMINI_MODEL or "gemini-1.5-flash"
            urls = [
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                f"https://generativelanguage.googleapis.com/v1/models/{model_name}:generateContent",
            ]
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": full}],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 1200,
                },
            }
            # 绕过代理直接连接 Google API（避免代理 SSL 问题）
            import os
            old_no_proxy = os.environ.get("NO_PROXY", "")
            os.environ["NO_PROXY"] = "generativelanguage.googleapis.com"
            # 429 限流时重试：最多 3 轮，每轮试完两个 URL 后若全是 429 则退避 2s/4s/8s 再试
            max_retries = 3
            last_exc = None
            resp = None
            try:
                for attempt in range(max_retries):
                    for url in urls:
                        try:
                            resp = httpx.post(
                                url,
                                headers={"x-goog-api-key": GEMINI_API_KEY},
                                json=payload,
                                timeout=60.0,
                            )
                            resp.raise_for_status()
                            break
                        except Exception as e:
                            last_exc = e
                            resp = None
                    if resp is not None:
                        break
                    # 本轮全部失败，若是 429 且还有重试次数则退避后再试；body 含 limit: 0 时直接走 Azure，不重试
                    is_429 = (
                        getattr(last_exc, "response", None) is not None
                        and getattr(last_exc.response, "status_code", None) == 429
                    )
                    if is_429 and attempt < max_retries - 1:
                        body_429 = ""
                        try:
                            r = getattr(last_exc, "response", None)
                            body_429 = (getattr(r, "text", None) or "") if r else ""
                        except Exception:
                            body_429 = ""
                        if "limit: 0" in body_429 or '"limit":0' in body_429:
                            log.warning("Gemini 429 limit: 0，跳过重试直接走 Azure")
                            break
                        wait = 2 ** (attempt + 1)
                        log.warning("Gemini 429 限流，%ds 后重试 (%d/%d)", wait, attempt + 1, max_retries)
                        time.sleep(wait)
                if resp is None:
                    if last_exc is not None:
                        raise last_exc  # type: ignore[misc]
                    else:
                        raise Exception("Gemini API 请求失败，未返回响应")

                data = resp.json()
            finally:
                if old_no_proxy:
                    os.environ["NO_PROXY"] = old_no_proxy
                else:
                    os.environ.pop("NO_PROXY", None)
            # candidates[0].content.parts[0].text
            raw = ""
            try:
                raw = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "") or ""
            except Exception:
                raw = ""
            raw = raw.strip()
            log.info("gemini nlp raw_len=%s", len(raw))
            if log.isEnabledFor(logging.DEBUG):
                log.debug("gemini nlp raw=%s", raw[:2000] + ("..." if len(raw) > 2000 else ""))
            start = raw.find("{")
            if start != -1:
                end = raw.rfind("}") + 1
                if end > start:
                    raw = raw[start:end]
            obj = json.loads(raw)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("gemini nlp json=%s", json.dumps(obj, ensure_ascii=False)[:2000])
            cmd = _dict_to_intent(obj, today, user_text)
            return _maybe_fill_update_actions(cmd, today, user_text, day_tasks)
        except Exception as e:
            # Gemini 失败时：优先回退 HKUST Azure 进行解析
            _log_http_error("gemini nlp", e)
            log.exception("gemini nlp parse failed: %s", e)
            prompt = _SCHEMA.format(today=today.isoformat(), now_iso=now_iso, weekday=weekday)
            azure_res = _try_hkust_azure(_with_context(prompt))
            if azure_res is not None:
                log.info("Gemini 失败，成功回退到 Azure OpenAI")
                return azure_res
            # Azure 也失败，继续尝试 OpenAI

    # 最后 OpenAI（如果 Azure 和 Gemini 都失败）
    if OPENAI_API_KEY and OpenAI is not None:
        try:
            client = OpenAI(api_key=OPENAI_API_KEY)
            prompt = _SCHEMA.format(today=today.isoformat(), now_iso=now_iso, weekday=weekday)
            sys_prompt = _with_context(prompt)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.1,
            )
            raw = (resp.choices[0].message.content or "").strip()
            log.info("openai nlp raw_len=%s", len(raw))
            if log.isEnabledFor(logging.DEBUG):
                log.debug("openai nlp raw=%s", raw[:2000] + ("..." if len(raw) > 2000 else ""))
            # 取首个 JSON 块
            start = raw.find("{")
            if start != -1:
                end = raw.rfind("}") + 1
                if end > start:
                    raw = raw[start:end]
            obj = json.loads(raw)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("openai nlp json=%s", json.dumps(obj, ensure_ascii=False)[:2000])
            cmd = _dict_to_intent(obj, today, user_text)
            return _maybe_fill_update_actions(cmd, today, user_text, day_tasks)
        except Exception as e:
            log.exception("openai nlp parse failed: %s", e)

    cmd = _rule_fallback(user_text, today)
    return _maybe_fill_update_actions(cmd, today, user_text, day_tasks)


def _dict_to_intent(obj: dict, today: date, user_text: str) -> CommandIntent:
    intent = (obj.get("intent") or "query").lower()
    if intent not in ("insert", "update", "delete", "query", "plan"):
        intent = "query"

    target_start_iso = None
    if obj.get("target_time_desc"):
        target_start_iso = _time_desc_to_iso(today, obj["target_time_desc"])

    delta = obj.get("delta_minutes")
    if delta is not None and isinstance(delta, (int, float)):
        delta = int(delta)

    # --- 兜底修正：LLM 有时把 update 的目标任务放在 title 而不是 target_title ---
    # 同时，用户说“推迟/延后/延期”但未给具体分钟/时间点时，给一个合理默认值，避免 update 成为空操作。
    text = (user_text or "").strip()
    text_lower = text.lower()

    def _default_delta_minutes_from_text(t: str) -> int:
        # 与规则兜底保持一致：默认 60min；含“半”则 30min；若明确写了数字小时/分钟则从文本提取
        if "半" in t:
            base = 30
        else:
            base = 60
        m = re.search(r"(\d+)\s*小时", t)
        if m:
            return 60 * int(m.group(1))
        m = re.search(r"(\d+)\s*分钟", t)
        if m:
            return int(m.group(1))
        return base

    def _normalize_actions(actions: Any) -> Any:
        if not isinstance(actions, list):
            return actions
        out = []
        for a in actions:
            if not isinstance(a, dict):
                out.append(a)
                continue
            act = (a.get("action") or "").lower().strip()
            # 若模型把目标放在 title，则在 update/delete/complete 时迁移到 target_title
            if act in ("update", "delete", "complete"):
                if not a.get("target_title") and isinstance(a.get("title"), str) and a.get("title").strip():
                    a = dict(a)
                    a["target_title"] = a["title"].strip()
            # 用户表达“推迟/延后/延期”但缺少位移/时间点时：补 delta_minutes
            if act == "update":
                has_shift = any(a.get(k) is not None for k in ("delta_minutes", "new_start_iso", "new_end_iso"))
                if (not has_shift) and ("推迟" in text or "延后" in text or "延期" in text or "延迟" in text):
                    a = dict(a)
                    a["delta_minutes"] = _default_delta_minutes_from_text(text)
                # 兜底：用户说「X是A到B点」时，从文本解析起止时刻，覆盖错误使用的 delta_minutes
                if not (a.get("new_start_iso") and a.get("new_end_iso")):
                    parsed = _parse_time_range_from_text(today, text)
                    if parsed:
                        a = dict(a)
                        a["new_start_iso"], a["new_end_iso"] = parsed
                        if "delta_minutes" in a:
                            del a["delta_minutes"]
            out.append(a)
        return out

    actions = _normalize_actions(obj.get("actions"))
    
    # 调试日志：检查actions解析结果
    log.info("_dict_to_intent: intent=%s, actions类型=%s, actions长度=%s", 
             intent,
             type(actions).__name__ if actions else None,
             len(actions) if isinstance(actions, list) else 0)
    if isinstance(actions, list) and len(actions) > 0:
        log.debug("actions内容: %s", [a.get("action") if isinstance(a, dict) else str(a) for a in actions[:3]])

    return CommandIntent(
        intent=intent,
        target_task_id=obj.get("target_task_id") or None,
        target_start_iso=target_start_iso or obj.get("target_start_iso"),
        delta_minutes=delta,
        new_start_iso=obj.get("new_start_iso"),
        new_end_iso=obj.get("new_end_iso"),
        title=obj.get("title"),
        start_iso=obj.get("start_iso"),
        end_iso=obj.get("end_iso"),
        priority=(obj.get("priority") or "normal").lower(),
        memo=obj.get("memo"),
        reply_text=obj.get("reply_text"),
        reasoning=(obj.get("reasoning") or "").strip() or None,
        planning_reason=obj.get("planning_reason"),
        actions=actions,
        need_clarification=bool(obj.get("need_clarification")) if obj.get("need_clarification") is not None else None,
        candidates=obj.get("candidates") if isinstance(obj.get("candidates"), list) else None,
    )


def _maybe_fill_update_actions(
    cmd: CommandIntent, today: date, user_text: str, day_tasks: Optional[list]
) -> CommandIntent:
    """当 intent 为 update 且 actions 为空时，从用户文本「X是A点到B点」解析并补全一条 update action。"""
    if cmd.intent != "update" or not day_tasks:
        return cmd
    if isinstance(cmd.actions, list) and len(cmd.actions) > 0:
        return cmd
    parsed = _parse_time_range_from_text(today, user_text or "")
    if not parsed:
        return cmd
    # 提取任务名：句首到「是」之前（如「研究问题讨论会议是15点到16点」->「研究问题讨论会议」）
    m = re.search(r"^(.+?)是\s*\d", (user_text or "").strip())
    if not m:
        return cmd
    title_cand = m.group(1).strip()
    for t in day_tasks:
        tit = (t.get("title") if isinstance(t, dict) else getattr(t, "title", None)) or ""
        tit = (tit or "").strip()
        if tit == title_cand or (title_cand in tit) or (tit in title_cand):
            tid = t.get("id") if isinstance(t, dict) else getattr(t, "id", None)
            if tid:
                cmd.actions = [
                    {
                        "action": "update",
                        "target_task_id": str(tid),
                        "new_start_iso": parsed[0],
                        "new_end_iso": parsed[1],
                    }
                ]
                log.info(
                    "update actions 为空，已从文本兜底补全: target_task_id=%s, new_start=%s new_end=%s",
                    tid, parsed[0], parsed[1],
                )
            break
    return cmd


def _rule_fallback(user_text: str, today: date) -> CommandIntent:
    """规则兜底：仅做意图分类，不解析任务列表；任务解析依赖 LLM。"""
    t_lower = (user_text or "").strip().lower()
    raw = (user_text or "").strip()

    def _extract_target_title_for_update(text: str) -> str:
        # 例：推迟模型调研到晚上 / 把模型调研推迟到晚上
        s = text
        s = re.sub(r"^(把|将|请|麻烦)\s*", "", s)
        # 先抓“调整/修改/更改 X 时间/日程”这种写法（任务名在“推迟”之前）
        m0 = re.search(r"(调整|修改|更改)\s*([^，。,.；;]+?)\s*(时间|日程|安排)", s)
        if m0:
            cand = (m0.group(2) or "").strip()
            cand = re.sub(r"^(把|将)\s*", "", cand).strip()
            return cand
        # 尝试抓取“推迟/延后/延期”后到“到/至/到晚上/到明天/一小时”等之间的片段
        m = re.search(r"(推迟|延后|延期|延迟)\s*([^，。,.；;]+?)\s*(到|至|为|一下|一会儿|(\d+\s*小时)|(\d+\s*分钟)|$)", s)
        if m:
            cand = (m.group(2) or "").strip()
            cand = re.sub(r"^(把|将)\s*", "", cand).strip()
            return cand
        # 兜底：取“推迟”后的尾巴
        m2 = re.search(r"(推迟|延后|延期|延迟)\s*(.+)$", s)
        if m2:
            cand = (m2.group(2) or "").strip()
            cand = re.sub(r"(到|至).*$", "", cand).strip()
            return cand
        return ""

    def _extract_time_desc(text: str) -> str:
        # 极简：仅识别“明天/晚上/下午/上午/中午/今天/几点”
        if "明天" in text:
            # 若有具体“9点/14:00”等，交给 _time_desc_to_iso
            m = re.search(r"明天\s*([^\s，。,.；;]+)", text)
            return ("明天 " + (m.group(1) if m else "")).strip()
        if "晚上" in text:
            m = re.search(r"晚上\s*([^\s，。,.；;]+)", text)
            return ("晚上 " + (m.group(1) if m else "")).strip()
        if "下午" in text:
            m = re.search(r"下午\s*([^\s，。,.；;]+)", text)
            return ("下午 " + (m.group(1) if m else "")).strip()
        if "上午" in text:
            m = re.search(r"上午\s*([^\s，。,.；;]+)", text)
            return ("上午 " + (m.group(1) if m else "")).strip()
        if "中午" in text:
            return "中午"
        # 直接数字时间
        m = re.search(r"(\d{1,2}:\d{2}|\d{1,2}\s*点)", text)
        return m.group(1) if m else ""

    # --- 更新/推迟：在 LLM 不可用时，至少能按标题关键词更新 ---
    if "推迟" in t_lower or "延后" in t_lower or "延期" in t_lower or "延迟" in t_lower:
        delta = 60
        if "半" in t_lower:
            delta = 30
        m = re.search(r"(\d+)\s*小时", raw)
        if m:
            delta = 60 * int(m.group(1))
        m = re.search(r"(\d+)\s*分钟", raw)
        if m:
            delta = int(m.group(1))

        target_title = _extract_target_title_for_update(raw)
        time_desc = _extract_time_desc(raw)
        new_start_iso = _time_desc_to_iso(today, time_desc) if time_desc else None

        actions = []
        a = {"action": "update"}
        if target_title:
            a["target_title"] = target_title
        if new_start_iso:
            a["new_start_iso"] = new_start_iso
        else:
            a["delta_minutes"] = delta
        actions.append(a)
        return CommandIntent(intent="update", actions=actions, reply_text="已理解：将尝试更新目标任务时间。")

    # --- 删除/取消：极简按标题关键词 ---
    if "删除" in t_lower or "取消" in t_lower:
        cand = re.sub(r"^(删除|取消)\s*", "", raw).strip()
        actions = [{"action": "delete", "target_title": cand}] if cand else [{"action": "delete"}]
        return CommandIntent(intent="delete", actions=actions, reply_text="已理解：将尝试删除目标任务。")

    # --- 完成：按标题关键词 ---
    if "完成" in t_lower or "搞定" in t_lower or "做完" in t_lower:
        cand = re.sub(r"^(完成|搞定|做完)\s*", "", raw).strip()
        actions = [{"action": "complete", "target_title": cand}] if cand else [{"action": "complete"}]
        return CommandIntent(intent="query", actions=actions, reply_text="已理解：将尝试把目标任务标记为完成。")

    if "添加" in t_lower or "新增" in t_lower or "安排" in t_lower or "记录" in t_lower or "加入" in t_lower:
        return CommandIntent(intent="insert", reply_text="已理解：将新增一条任务。")

    return CommandIntent(intent="query", reply_text="已收到；若需排程请配置 LLM（Gemini/OpenAI）或用“删除/完成/推迟+任务名”指令。")
