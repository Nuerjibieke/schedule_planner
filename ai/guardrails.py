"""
护栏逻辑层（Guardrails Tools）：将 LLM 生成的不确定文本（时间描述、模糊任务名）
确定性地转换为数据库和业务代码可精确执行的结构化数据。
"""
from datetime import date, datetime, timedelta, timezone
import re
import logging
import dateparser
from typing import Optional, List, Tuple
from fuzzywuzzy import fuzz

from db.models import Task

log = logging.getLogger("ai.guardrails")

def parse_natural_time(time_expr: str, base_date: Optional[date] = None) -> Optional[datetime]:
    """
    将自然语言的时间表达式（"明天下午两点", "下周三", "现在", "等会"）转化为确定的 datetime。
    解决 LLM 对于跨周、跨月计算能力不足的幻觉。
    """
    if not time_expr or not time_expr.strip():
        return None
        
    expr = time_expr.strip()
    now = datetime.now()
    if base_date is None:
        base_date = now.date()
        
    # 处理常见高频硬编码词
    if expr in ("现在", "刚刚", "当前"):
        return now.replace(microsecond=0)
        
    # 处理「等会」「稍后」等相对词（默认给 30 分钟后）
    if expr in ("等会", "等一下", "稍后", "一会", "一会儿"):
        return (now + timedelta(minutes=30)).replace(microsecond=0)
        
    # Dateparser 设置环境
    settings = {
        'RELATIVE_BASE': datetime.combine(base_date, now.time()),
        # 移除 'PREFER_DATES_FROM': 'future' 避免下午运行测试时把普通的 "9点" 推迟到晚上 21:00
        'TIMEZONE': 'local'
    }
    
    # 将中文常见模糊时间词替换为能被 dateparser 解析的格式
    # 处理 "下周X"
    week_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    m = re.search(r"下周([一二三四五六日天])", expr)
    target_date = base_date
    if m:
        target_w = week_map[m.group(1)]
        days_ahead = (target_w - base_date.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7
        target_date = base_date + timedelta(days=days_ahead)
        # 用绝对日期替换“下周X”以防 dateparser 解析漂移
        
        # 保存原始词汇用于判断是否有晚上的修饰
        base_expr_for_check = expr
        expr = expr.replace(m.group(0), "").strip() # 不要放日期进去，直接截获
    else:
        base_expr_for_check = expr
    
    # 手动干预解决单说“9点”被算作“晚上9点”的情况
    # 如果明确只有 "X点" 或 "X点Y分"，我们直接截获自己解析
    m_hour = re.search(r'^(?<!\d)(\d{1,2})\s*点\s*(\d{1,2})?\s*(分)?$', expr.strip())
    if m_hour and "上午" not in expr and "下午" not in expr and "晚上" not in base_expr_for_check and "凌晨" not in base_expr_for_check and "早" not in base_expr_for_check and "晚" not in base_expr_for_check:
        h = int(m_hour.group(1))
        m_val = int(m_hour.group(2) or 0)
        # 如果是早上6点到下午18点之间，直接按照 24小时制 设置当前/目标天
        # 以防 dateparser 把它搞到晚上
        parsed_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=h, minute=m_val)
    else:
        parsed_dt = dateparser.parse(expr, settings=settings)
    
    # 兜底：如果 dateparser 失败，降级使用旧版里的正则匹配逻辑（对替换过的 expr 继续匹配）
    if not parsed_dt:
        # 如果 expr 已经被替换成 2026-03-11 9点 这种形式，我们需要把基础日期提出来
        m_date = re.search(r"(\d{4})-(\d{2})-(\d{2})", expr)
        if m_date:
            base_date = date(int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3)))
        parsed_dt = _legacy_time_desc_to_naive(base_date, expr)
        
    if parsed_dt:
        return parsed_dt.replace(microsecond=0)
        
    return None

def _legacy_time_desc_to_naive(today: date, desc: str) -> Optional[datetime]:
    """旧版的简单时间提取正则（从原 nlp.py 中迁移作为兜底）"""
    base = today
    if "明天" in desc:
        base = today + timedelta(days=1)
    
    hour, minute = 0, 0
    if "凌晨" in desc:
        m = re.search(r"(\d{1,2})\s*点", desc)
        if m:
            hour = int(m.group(1))
            if hour <= 5: 
                base = today + timedelta(days=1)
        else:
            hour = 1
            base = today + timedelta(days=1)
    elif "下午" in desc or "中午" in desc:
        hour = 14 if "两点" in desc or "2点" in desc else (12 if "中午" in desc else 15)
        if "两点" in desc or "2点" in desc: hour = 14
        elif "一点" in desc or "1点" in desc: hour = 13
        elif "三点" in desc or "3点" in desc: hour = 15
        elif "四点" in desc or "4点" in desc: hour = 16
    elif "上午" in desc:
        hour = 10 if "十点" in desc or "10点" in desc else 9
    elif "晚上" in desc:
        hour = 19
        
    m = re.search(r"(\d{1,2}):(\d{2})", desc)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})\s*点", desc)
        if m:
            raw_h = int(m.group(1))
            # 根据时段修饰词（晚上/下午）调整24小时制
            if ("晚上" in desc or "下午" in desc) and raw_h < 12:
                hour = raw_h + 12
            elif "凌晨" in desc and raw_h == 12:
                hour = 0
            else:
                hour = raw_h
            
    if hour == 0 and minute == 0 and "凌晨" not in desc:
        return None # 没解析出来
    
    try:
        return datetime.combine(base, datetime.min.time()).replace(hour=hour, minute=minute)
    except Exception:
        return None

def fuzzy_match_task(target_title: str, day_tasks: List[Task]) -> Optional[Task]:
    """
    利用模糊匹配，在当天的 pending/done 任务中找到最接近用户表述的那一个。
    解决 LLM 直接提取 task_id 时的强严重幻觉。
    """
    if not target_title or not target_title.strip() or not day_tasks:
        return None
        
    q = target_title.strip().lower()
    best_task = None
    best_score = 0
    
    # 去掉用户口语中常用无意义的代词
    # 去掉用户口语中常用无意义的代词
    q_stripped = re.sub(r'^(那个|这个|那条|这条|那个叫|这个叫|这|那)', '', q).strip()
    if len(q_stripped) > 0:
        q = q_stripped
        
    log.debug("fuzzy_match_task query=%r stripped=%r", target_title, q)
    
    for t in day_tasks:
        if not getattr(t, 'title', None):
            continue
        c = t.title.lower()
        
        # 1. 绝对包含关系（直接命中）
        if q == c:
            score = 100
        elif len(q) >= 2 and (q in c or c in q):
            # 较长短词汇包含匹配给高分
            score = 90
        else:
            # 2. 模糊相似度 (token_set_ratio 对这类口语指代效果更好)
            score = fuzz.token_set_ratio(q, c)
            # 提取每一个字，如果有个单独的字匹配并且原词很短（如"会"在"会议"里），给一个保底分
            if len(q) <= 2:
                for char in q:
                    if char in c:
                        score = max(score, 75)
                        
        log.debug("fuzzy_match_task candidate=%r score=%s", c, score)
            
        if score > best_score:
            best_score = score
            best_task = t

    # 设定一个及格水位线 (例如 50 分)，低于则认为用户在说新任务（可能 intent=insert）
    if best_score >= 50:
        return best_task

    return None

def merge_progress_into_memo(old_memo: Optional[str], progress: int) -> str:
    """如果用户说了 '完成 50%'，安全地更新其任务的进度百分比"""
    progress = max(0, min(100, progress))
    current_memo = old_memo or ""
    
    memo_clean = re.sub(r'progress:\s*\d+', '', current_memo)
    memo_clean = re.sub(r'\s*\|\s*$', '', memo_clean).strip()
    memo_clean = re.sub(r'^\s*\|\s*', '', memo_clean).strip()
    memo_clean = re.sub(r'\s*\|\s*', ' | ', memo_clean).strip()
    
    if memo_clean:
        return f"{memo_clean} | progress:{progress}%"
    return f"progress:{progress}%"
