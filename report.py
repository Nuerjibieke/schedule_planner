"""智能汇报：日报归档、周报、月报。"""
from datetime import date, timedelta
from typing import List, Optional
import logging
import time

from db import repo
from db.models import Task, Summary
from config import (
    OPENAI_API_KEY, 
    GEMINI_API_KEY, 
    GEMINI_MODEL,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_API_VERSION,
    AZURE_OPENAI_API_VERSIONS,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_DEPLOYMENTS,
)

try:
    from openai import OpenAI, AzureOpenAI
except Exception:
    OpenAI = None
    AzureOpenAI = None

try:
    import httpx
except Exception:
    httpx = None

log = logging.getLogger("api.openai.report")


def _tasks_to_bullets(tasks: List[Task]) -> str:
    lines = []
    for t in tasks:
        st = t.start_time[11:16] if len(t.start_time) >= 16 else t.start_time
        et = t.end_time[11:16] if len(t.end_time) >= 16 else t.end_time
        lines.append(f"- {st}–{et} {t.title} ({t.status}, {t.priority})")
    return "\n".join(lines) if lines else "（无）"


def _try_azure_openai(system: str, user: str, max_tokens: int = 500) -> Optional[str]:
    """Gemini 不可用时，回退到 HKUST Azure OpenAI。"""
    if not AZURE_OPENAI_API_KEY or AzureOpenAI is None:
        return None
    try:
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
        text: Optional[str] = None
        for api_ver in api_versions:
            client = AzureOpenAI(
                api_key=AZURE_OPENAI_API_KEY,
                api_version=api_ver,
                azure_endpoint=AZURE_OPENAI_ENDPOINT,
            )
            for dep in deployments:
                try:
                    log.info("hkust azure report try api_version=%s deployment=%s", api_ver, dep)
                    resp = client.chat.completions.create(
                        model=dep,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        temperature=0.3,
                        max_tokens=max_tokens,
                    )
                    text = (resp.choices[0].message.content or "").strip()
                    break
                except Exception as e:
                    last_err = e
                    # 404 错误时尝试下一个 deployment/version
                    if "404" in str(e) or "not found" in str(e).lower():
                        log.warning("hkust azure 404/not found api_version=%s deployment=%s: %s", api_ver, dep, e)
                        continue
                    raise
            if text is not None:
                break
        
        if text is None:
            if last_err:
                raise last_err
            return None
        
        log.info("hkust azure report raw_len=%s", len(text))
        if log.isEnabledFor(logging.DEBUG):
            log.debug("hkust azure report raw=%s", text[:2000] + ("..." if len(text) > 2000 else ""))
        return text
    except Exception as e:
        log.exception("hkust azure report generate failed: %s", e)
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


def _ask_ai(system: str, user: str, max_tokens: int = 500) -> str:
    # 优先 HKUST Azure OpenAI
    if AZURE_OPENAI_API_KEY and AzureOpenAI is not None:
        azure_result = _try_azure_openai(system, user, max_tokens)
        if azure_result:
            log.info("使用 HKUST Azure OpenAI 生成报告")
            return azure_result
    
    # 其次 Gemini
    if GEMINI_API_KEY and httpx is not None:
        try:
            full = f"{system}\n\n{user}"
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
                    "temperature": 0.3,
                    "maxOutputTokens": int(max_tokens) if max_tokens else 800,
                },
            }
            # 绕过代理直接连接 Google API（避免代理 SSL 问题）
            import os
            old_no_proxy = os.environ.get("NO_PROXY", "")
            os.environ["NO_PROXY"] = "generativelanguage.googleapis.com"
            try:
                last_exc = None
                r = None
                max_retries = 5  # 增加重试次数到 5 次
                retry_delay = 10  # 增加初始延迟到 10 秒
                
                for attempt in range(max_retries):
                    # 尝试每个 URL
                    for url in urls:
                        try:
                            r = httpx.post(
                                url,
                                headers={"x-goog-api-key": GEMINI_API_KEY},
                                json=payload,
                                timeout=60.0,
                            )
                            r.raise_for_status()
                            # 成功，跳出所有循环
                            break
                        except httpx.HTTPStatusError as e:
                            # 429 错误：速率限制（两个 URL 共享速率限制，不需要尝试第二个 URL）
                            if e.response.status_code == 429:
                                last_exc = e
                                r = None
                                # 跳出 URL 循环，准备重试
                                break
                            else:
                                # 其他 HTTP 错误，尝试下一个 URL
                                last_exc = e
                                r = None
                                continue
                        except Exception as e:
                            # 其他异常，尝试下一个 URL
                            last_exc = e
                            r = None
                            continue
                    
                    # 如果成功，跳出重试循环
                    if r is not None:
                        break
                    
                    # 如果遇到 429 且还有重试机会，等待后重试
                    if (last_exc and isinstance(last_exc, httpx.HTTPStatusError) 
                        and last_exc.response.status_code == 429 
                        and attempt < max_retries - 1):
                        wait_time = retry_delay * (2 ** attempt)  # 指数退避：10s, 20s, 40s, 80s
                        log.warning("遇到速率限制 (429)，等待 %d 秒后重试 (第 %d/%d 次)", wait_time, attempt + 1, max_retries)
                        time.sleep(wait_time)
                        continue
                    else:
                        # 非 429 错误或最后一次重试失败，直接失败
                        if (last_exc and isinstance(last_exc, httpx.HTTPStatusError) 
                            and last_exc.response.status_code == 429):
                            log.error("经过 %d 次重试后仍然遇到速率限制 (429)，API 配额可能已用完或需要等待更长时间", max_retries)
                        break
                
                if r is None:
                    raise last_exc  # type: ignore
                data = r.json()
            finally:
                if old_no_proxy:
                    os.environ["NO_PROXY"] = old_no_proxy
                else:
                    os.environ.pop("NO_PROXY", None)
            text = ""
            try:
                text = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "") or ""
            except Exception:
                text = ""
            text = text.strip()
            log.info("gemini report raw_len=%s", len(text))
            if log.isEnabledFor(logging.DEBUG):
                log.debug("gemini report raw=%s", text[:2000] + ("..." if len(text) > 2000 else ""))
            return text
        except Exception as e:
            log.exception("gemini report generate failed: %s", e)
            error_msg = str(e)
            # Gemini 失败后，尝试回退到 Azure OpenAI
            if "429" in error_msg or "Too Many Requests" in error_msg:
                log.info("Gemini 速率限制，尝试回退到 Azure OpenAI")
            azure_result = _try_azure_openai(system, user, max_tokens)
            if azure_result:
                log.info("成功使用 Azure OpenAI 生成报告")
                return azure_result
            # Gemini 失败后，继续尝试 OpenAI

    # 最后 OpenAI（如果 Azure 和 Gemini 都失败）
    if not OPENAI_API_KEY or OpenAI is None:
        return "（未配置 Gemini/Azure OpenAI/OpenAI，无法生成）"
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        max_retries = 3
        retry_delay = 5
        
        for attempt in range(max_retries):
            try:
                r = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=0.3,
                    max_tokens=max_tokens,
                )
                text = (r.choices[0].message.content or "").strip()
                log.info("openai report raw_len=%s", len(text))
                if log.isEnabledFor(logging.DEBUG):
                    log.debug("openai report raw=%s", text[:2000] + ("..." if len(text) > 2000 else ""))
                return text
            except Exception as e:
                # 检查是否是速率限制错误
                error_str = str(e).lower()
                is_rate_limit = "429" in error_str or "rate limit" in error_str or "too many requests" in error_str
                
                if is_rate_limit and attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)  # 指数退避：5s, 10s, 20s
                    log.warning("OpenAI 遇到速率限制，等待 %d 秒后重试 (第 %d/%d 次)", wait_time, attempt + 1, max_retries)
                    time.sleep(wait_time)
                    continue
                else:
                    raise
    except Exception as e:
        log.exception("openai report generate failed: %s", e)
        return f"（生成失败：所有 API（Gemini/Azure OpenAI/OpenAI）均不可用。最后错误：{e}）"


def run_daily_archive(d: date) -> str:
    """把当天任务归档为一段约 200 字的日报，写入 summaries。"""
    tasks = repo.tasks_for_date(d)
    bullet = _tasks_to_bullets(tasks)
    system = "你是一个日程助理。根据用户给出的当日任务列表，写一段约 200 字的中文日报摘要，概括主要做了哪些事、进度如何。不要列表，用连贯段落。"
    user = f"日期：{d.isoformat()}\n任务列表：\n{bullet}"
    content = _ask_ai(system, user, max_tokens=300)
    s = Summary(id=None, date=d.isoformat(), kind="daily", content=content)
    repo.save_summary(s)
    return content


def _week_start(d: date) -> date:
    """该日所在周的周一（ISO 周一为一周开始）。"""
    return d - timedelta(days=d.weekday())


def get_weekly_report_text() -> str:
    """先读存档周报；无则根据最近 7 天任务生成并写入 summaries。"""
    today = date.today()
    week_start = _week_start(today)
    existing = repo.weekly_summary_for_week(week_start)
    if existing and (existing.content or "").strip() and not existing.content.strip().startswith("（"):
        return existing.content.strip()
    bullets = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        tasks = repo.tasks_for_date(d)
        part = _tasks_to_bullets(tasks)
        bullets.append(f"【{d}】\n{part}")
    text = "\n\n".join(bullets)
    system = "你是一个日程助理。根据下面一周的每日任务列表，写一段简洁的中文周报（约 300 字），总结本周要事与进展。"
    content = _ask_ai(system, text, max_tokens=500)
    if not content or content.strip().startswith("（"):
        raise Exception(f"周报生成失败：{content or 'API 返回空内容'}")
    s = Summary(id=None, date=week_start.isoformat(), kind="weekly", content=content)
    repo.save_summary(s)
    return content


def get_monthly_report_text() -> str:
    """先读存档月报；无则根据当月日报由 AI 生成并写入 summaries。"""
    today = date.today()
    existing = repo.monthly_summary_for_month(today.year, today.month)
    if existing and (existing.content or "").strip() and not existing.content.strip().startswith("（"):
        return existing.content.strip()
    summaries = repo.daily_summaries_for_month(today.year, today.month)
    if not summaries:
        return "本月暂无日报，请先运行「今日归档」或添加任务后再生成月报。"
    combined = "\n\n".join([f"【{s.date}】\n{s.content}" for s in summaries])
    system = "你是一个日程助理。根据下面本月的每日摘要，写一段简洁的中文月报（约 500 字），概括本月工作与成果。"
    content = _ask_ai(system, combined, max_tokens=800)
    if not content or content.strip().startswith("（"):
        raise Exception(f"月报生成失败：{content or 'API 返回空内容'}")
    first_day = f"{today.year}-{today.month:02d}-01"
    s = Summary(id=None, date=first_day, kind="monthly", content=content)
    repo.save_summary(s)
    return content
