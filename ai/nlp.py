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


from .models import IntentType, CommandIntent
from .guardrails import parse_natural_time as _parse_natural_time


_SCHEMA = """
你是日程助理，只负责把用户自然语言拆解为结构化 JSON 提取结果。
请不要自行推算跨月跨周的绝对日期，所有的日期推算、模糊匹配和规则将在你输出 JSON 后由底层代码兜底。
请只返回一份 JSON，不要任何额外文字、不要 markdown 代码块包裹。

【非常重要】当 intent 为 "plan" 时，必须提供 planning_reason 字段，详细说明规划思路（至少50字）：
1. 为什么这样安排任务顺序 2. 为什么这样分配时间 3. 考虑了什么因素。
不能只是泛泛的描述。

输出格式（严格按此 JSON 结构）：
{
  "intent": "insert" | "update" | "delete" | "query" | "plan" | "replan_all",
  "planning_reason": "整体规划原因（plan 时必需，详细说明思路）",
  "desired_state": [
    {
      "id": "可选，已有任务ID（更新时建议提供）",
      "title": "任务标题",
      "start_iso": "可选，开始时间 ISO",
      "end_iso": "可选，结束时间 ISO",
      "priority": "high" | "normal" | "low",
      "status": "pending" | "done",
      "is_fixed": true或false,
      "memo": "可选备注"
    }
  ],
  "actions": [
    {
      "action": "insert" | "update" | "delete" | "complete" | "plan",
      "target_title_expr": "任务名称（要操作的原任务名或新创建的待办名，原样提取）",
      "difficulty": "easy" | "medium" | "hard",
      "new_duration_minutes": 数字(可选，表示任务需要改成耗时X分钟),
      "tips": ["建议1", "建议2"],
      "priority": "high" | "normal" | "low",
      "reason": "该任务的时间安排和优先级规划原因（可选）",
      "start_time_expr": "可选，用户原话中对开始时间的描述，如『明天下午两点』『等会』『原样提取』",
      "end_time_expr": "可选，用户原话中对结束时间的描述",
      "delta_minutes": "可选，推迟/提前的具体分钟数，如 60 或 -30（如果是推迟没有说多久，不填）",
      "progress_percentage": "可选，如用户说完成50%，填 50 (数字)",
      "is_past_record": true或false (当用户说『从X点到现在看了书』『昨天写了代码』时，此项为true)
    }
  ],
  "reply_text": "一句简短中文，描述我将做什么",
  "reasoning": "2-5句话说明：① 用户意图 ② 识别出的关键信息 ③ 你判定的操作（供查看思考过程）"
}

- intent="replan_all"：当用户说"重新规划待完成任务"、"全部重新安排"时，返回 intent="replan_all" 即可，无需在 actions 里列举所有任务（系统会自动顺延所有 pending 任务）。
- intent="plan" 时优先返回 desired_state；若无法稳定描述目标态，再返回 actions。
- intent="update" vs "insert"：如果提到现有需要修改的事，是 update。提到新的、记录某事，是 insert。
- **时间调整 vs 时长**：如果用户说"需要X时间""改半小时"，填到 `new_duration_minutes`。如果说"晚一点""推迟X小时"，填到 `delta_minutes`。若用户没给具体位移/时间点，保持字段为空，不要猜。
- **进度追踪**：如果说"完成50%"，填 action="complete", progress_percentage=50，不要乱改内容。
- **目标识别**：不要试图找出系统数据库里的 ID，绝大部情况把用户提到的称呼放进 `target_title_expr` 即可（如 "那个会", "研讨会"）。
"""


def _build_nlp_context_prompt(
    system_prompt: str,
    today: date,
    now_iso: str,
    weekday: str,
    day_tasks: Optional[list],
    conversation_history: Optional[List[Dict[str, str]]],
    include_history: bool,
    include_day_tasks: bool,
) -> str:
    history_text = ""
    if include_history and conversation_history and len(conversation_history) > 0:
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

    tasks_section = ""
    if include_day_tasks:
        tasks_ctx_str = " (无)"
        if day_tasks:
            titles = []
            for t in day_tasks:
                if isinstance(t, dict):
                    title = (t.get("title") or "").strip()
                else:
                    title = (getattr(t, "title", None) or "").strip()
                if title:
                    titles.append(title)
            if titles:
                tasks_ctx_str = ", ".join(titles)
        tasks_section = (
            f"【当日已有任务概览】{tasks_ctx_str}\n\n"
            f"【补充说明】\n"
            f"- 判断意图时，如果用户说的新任务不在上面已有概览中，是 insert。如果是已有的，是 update。\n"
        )

    return (
        f"【时间上下文】今天是 {today.isoformat()}，星期{weekday}。当前具体时间是 {now_iso}。\n\n"
        f"{system_prompt}\n\n"
        f"{history_text}"
        f"{tasks_section}"
    )









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
    from config import OPENAI_API_KEY, GEMINI_API_KEY, GEMINI_MODEL, GEMINI_BASE_URL, GEMINI_API_TYPE
    from config import (
        AZURE_OPENAI_API_KEY,
        AZURE_OPENAI_ENDPOINT,
        AZURE_OPENAI_API_VERSION,
        AZURE_OPENAI_API_VERSIONS,
        AZURE_OPENAI_DEPLOYMENT,
        AZURE_OPENAI_DEPLOYMENTS,
        NLP_INCLUDE_HISTORY_IN_PROMPT,
        NLP_INCLUDE_DAY_TASKS_IN_PROMPT,
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

    def _try_gemini_openai_compat(prompt: str) -> Optional[CommandIntent]:
        """
        优先支持 openai-style Gemini 兼容网关（如 https://yunwu.ai/v1）。
        成功返回 CommandIntent；失败返回 None 继续后续 provider。
        """
        if (GEMINI_API_TYPE or "").lower() != "openai":
            return None
        if not GEMINI_API_KEY or OpenAI is None:
            return None
        try:
            base_url = (GEMINI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            model_name = GEMINI_MODEL or "gemini-2.5-pro"
            client = OpenAI(api_key=GEMINI_API_KEY, base_url=base_url)
            log.info("gemini openai-style nlp try model=%s base_url=%s", model_name, base_url)
            resp = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            raw = (resp.choices[0].message.content or "").strip()
            if log.isEnabledFor(logging.DEBUG):
                log.debug("gemini openai-style nlp raw=%s", raw[:2000] + ("..." if len(raw) > 2000 else ""))
            start = raw.find("{")
            if start != -1:
                end = raw.rfind("}") + 1
                if end > start:
                    raw = raw[start:end]
            obj = json.loads(raw)
            cmd = _dict_to_intent(obj, today, user_text)
            return _maybe_fill_update_actions(cmd, today, user_text, day_tasks)
        except Exception as e:
            _log_http_error("gemini openai-style nlp", e)
            log.exception("gemini openai-style nlp parse failed: %s", e)
            return None

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

    def _with_context(system_prompt: str, today: date, now_iso: str, weekday: str) -> str:
        return _build_nlp_context_prompt(
            system_prompt=system_prompt,
            today=today,
            now_iso=now_iso,
            weekday=weekday,
            day_tasks=day_tasks,
            conversation_history=conversation_history,
            include_history=NLP_INCLUDE_HISTORY_IN_PROMPT,
            include_day_tasks=NLP_INCLUDE_DAY_TASKS_IN_PROMPT,
        )

    # 最高优先：Gemini OpenAI 兼容网关（如 yunwu.ai/v1）
    gemini_openai_res = _try_gemini_openai_compat(_with_context(_SCHEMA, today, now_iso, weekday))
    if gemini_openai_res is not None:
        log.info("使用 Gemini OpenAI 兼容网关进行解析")
        return gemini_openai_res

    # 次优先 HKUST Azure OpenAI
    if AZURE_OPENAI_API_KEY and AzureOpenAI is not None:
        azure_res = _try_hkust_azure(_with_context(_SCHEMA, today, now_iso, weekday))
        if azure_res is not None:
            log.info("使用 HKUST Azure OpenAI 进行解析")
            return azure_res
    
    # 其次 Gemini（适配你当前 AIza... key）
    if GEMINI_API_KEY and httpx is not None:
        try:
            sys_prompt = _with_context(_SCHEMA, today, now_iso, weekday)
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
            azure_res = _try_hkust_azure(_with_context(_SCHEMA, today, now_iso, weekday))
            if azure_res is not None:
                log.info("Gemini 失败，成功回退到 Azure OpenAI")
                return azure_res
            # Azure 也失败，继续尝试 OpenAI

    # 最后 OpenAI（如果 Azure 和 Gemini 都失败）
    if OPENAI_API_KEY and OpenAI is not None:
        try:
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys_prompt = _with_context(_SCHEMA, today, now_iso, weekday)
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

    log.warning("All LLM providers unavailable or failed, falling back to rule-based parsing")
    cmd = _rule_fallback(user_text, today)
    return _maybe_fill_update_actions(cmd, today, user_text, day_tasks)


def _dict_to_intent(obj: dict, today: date, user_text: str) -> CommandIntent:
    intent = (obj.get("intent") or "query").lower()
    if intent not in ("insert", "update", "delete", "query", "plan", "replan_all"):
        intent = "query"

    target_start_iso = None
    if obj.get("target_time_desc"):
        _parsed = _parse_natural_time(obj["target_time_desc"], base_date=today)
        target_start_iso = _parsed.strftime("%Y-%m-%dT%H:%M:%S") if _parsed else None

    delta = obj.get("delta_minutes")
    if delta is not None and isinstance(delta, (int, float)):
        delta = int(delta)

    # --- 兜底修正：LLM 有时把 update 的目标任务放在 title 而不是 target_title ---
    text = (user_text or "").strip()

    def _parse_time_range_from_text(base_date: date, text: str):
        """从文本中解析 '从X到Y' 的时间范围，返回 (start_iso, end_iso) 或 None。"""
        # 匹配 "X点到Y点"、"从X到Y" 等模式
        m = re.search(r'(?:从)?(\d{1,2})[点:：时](?:(\d{1,2})分?)?\s*(?:到|至|-|—|~)\s*(\d{1,2})[点:：时](?:(\d{1,2})分?)?', text)
        if m:
            sh, sm = int(m.group(1)), int(m.group(2) or 0)
            eh, em = int(m.group(3)), int(m.group(4) or 0)
            from datetime import datetime as _dt
            try:
                s = _dt(base_date.year, base_date.month, base_date.day, sh, sm)
                e = _dt(base_date.year, base_date.month, base_date.day, eh, em)
                return s.strftime("%Y-%m-%dT%H:%M:%S"), e.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None
        # 匹配 "上午/下午/晚上X点到Y点"
        m2 = re.search(r'(上午|下午|晚上|凌晨|早上|中午)\s*(\d{1,2})[点:：时]?\s*(?:到|至|-|—|~)\s*(?:(上午|下午|晚上|凌晨|早上|中午)\s*)?(\d{1,2})[点:：时]?', text)
        if m2:
            def _adj(period, h):
                if period in ("下午", "晚上") and h < 12:
                    return h + 12
                if period == "凌晨" and h == 12:
                    return 0
                return h
            p1, h1 = m2.group(1), int(m2.group(2))
            p2 = m2.group(3) or m2.group(1)
            h2 = int(m2.group(4))
            sh = _adj(p1, h1)
            eh = _adj(p2, h2)
            from datetime import datetime as _dt
            try:
                s = _dt(base_date.year, base_date.month, base_date.day, sh, 0)
                e = _dt(base_date.year, base_date.month, base_date.day, eh, 0)
                return s.strftime("%Y-%m-%dT%H:%M:%S"), e.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None
        return None

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
            if act == "update":
                # 兜底：用户说「X是A到B点」时，从文本解析起止时刻，覆盖错误使用的 delta_minutes
                if not (a.get("new_start_iso") and a.get("new_end_iso")):
                    parsed = _parse_time_range_from_text(today, text)
                    if parsed:
                        a = dict(a)
                        a["new_start_iso"], a["new_end_iso"] = parsed
                        if "delta_minutes" in a:
                            del a["delta_minutes"]
            pre = a.get("preconditions")
            if pre is not None and not isinstance(pre, dict):
                a = dict(a)
                a.pop("preconditions", None)
            expected = a.get("expected_delta")
            if expected is not None and not isinstance(expected, dict):
                a = dict(a)
                a.pop("expected_delta", None)
            out.append(a)
        return out

    def _normalize_desired_state(desired: Any) -> Optional[List[Dict[str, Any]]]:
        if not isinstance(desired, list):
            return None
        out: List[Dict[str, Any]] = []
        for item in desired:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            d: Dict[str, Any] = {"title": title.strip()}
            if item.get("id") is not None:
                d["id"] = str(item.get("id"))
            for key in ("start_iso", "end_iso", "priority", "status", "memo"):
                if item.get(key) is not None:
                    d[key] = item.get(key)
            if isinstance(item.get("is_fixed"), bool):
                d["is_fixed"] = item.get("is_fixed")
            out.append(d)
        return out or None

    actions = _normalize_actions(obj.get("actions"))
    desired_state = _normalize_desired_state(obj.get("desired_state"))
    
    # 调试日志：检查actions解析结果
    log.info("_dict_to_intent: intent=%s, actions类型=%s, actions长度=%s", 
             intent,
             type(actions).__name__ if actions else None,
             len(actions) if isinstance(actions, list) else 0)
    if desired_state:
        log.info("_dict_to_intent: desired_state长度=%d", len(desired_state))
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
        desired_state=desired_state,
        need_clarification=bool(obj.get("need_clarification")) if obj.get("need_clarification") is not None else None,
        candidates=obj.get("candidates") if isinstance(obj.get("candidates"), list) else None,
        parse_source="llm",
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

    # --- 更新/推迟/修改/改到：在 LLM 不可用时，至少能按标题关键词更新 ---
    update_keywords = ("推迟", "延后", "延期", "延迟", "改", "调整", "修改", "换到", "改到", "移到", "挪到", "提前")
    if any(kw in t_lower for kw in update_keywords):
        delta = None
        if "半" in t_lower:
            delta = -30 if "提前" in t_lower else 30
        if "提前" in t_lower:
            delta = -60 if delta is None else delta
        m = re.search(r"(\d+)\s*小时", raw)
        if m:
            base_d = 60 * int(m.group(1))
            delta = -base_d if "提前" in t_lower else base_d
        m = re.search(r"(\d+)\s*分钟", raw)
        if m:
            base_d = int(m.group(1))
            delta = -base_d if "提前" in t_lower else base_d

        target_title = _extract_target_title_for_update(raw)
        # 兜底：如果没提取到标题，尝试按「把X改到」或「X改成」的模式提取
        if not target_title:
            m_title = re.search(r'(?:把|将)?\s*(.+?)\s*(?:改到|改成|换到|移到|挪到|调整到|修改到|提前到)', raw)
            if m_title:
                target_title = m_title.group(1).strip()
                target_title = re.sub(r'^(把|将)\s*', '', target_title).strip()
        time_desc = _extract_time_desc(raw)
        _parsed_t = _parse_natural_time(time_desc, base_date=today) if time_desc else None
        new_start_iso = _parsed_t.strftime("%Y-%m-%dT%H:%M:%S") if _parsed_t else None

        actions = []
        a = {"action": "update"}
        if target_title:
            a["target_title"] = target_title
        if new_start_iso:
            a["new_start_iso"] = new_start_iso
        elif delta is not None:
            a["delta_minutes"] = delta
        actions.append(a)
        return CommandIntent(intent="update", actions=actions, reply_text="已理解：将尝试更新目标任务时间。", parse_source="rule_fallback")

    # --- 删除/取消：极简按标题关键词 ---
    if "删除" in t_lower or "取消" in t_lower:
        cand = re.sub(r"^(删除|取消)\s*", "", raw).strip()
        actions = [{"action": "delete", "target_title": cand}] if cand else [{"action": "delete"}]
        return CommandIntent(intent="delete", actions=actions, reply_text="已理解：将尝试删除目标任务。", parse_source="rule_fallback")

    # --- 完成：按标题关键词 ---
    if "完成" in t_lower or "搞定" in t_lower or "做完" in t_lower:
        cand = re.sub(r"^(完成|搞定|做完)\s*", "", raw).strip()
        actions = [{"action": "complete", "target_title": cand}] if cand else [{"action": "complete"}]
        return CommandIntent(intent="query", actions=actions, reply_text="已理解：将尝试把目标任务标记为完成。", parse_source="rule_fallback")

    if any(kw in t_lower for kw in ("添加", "新增", "安排", "记录", "加入", "加个", "加一个", "开")):
        return CommandIntent(intent="insert", reply_text="已理解：将新增一条任务。", parse_source="rule_fallback")

    return CommandIntent(intent="query", reply_text='已收到；若需排程请配置 LLM（Gemini/OpenAI）或用"删除/完成/推迟+任务名"指令。', parse_source="rule_fallback")
