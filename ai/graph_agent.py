import logging
from typing import Optional, List, Dict
from datetime import date
from pydantic import ValidationError

from .graph_models import GraphIntent
from db.models import Task

log = logging.getLogger(__name__)

# ====== The Thin Prompt (极简指令) ======
# 不再有长篇大论的冲突处理规则，只有实体提取和拓扑定义。
THIN_SYSTEM_PROMPT = """\
你是一个智能日程的自然语言理解模块。你的唯一任务是将用户的请求映射为关系图 (DAG)。
当前所有的已有排程已经作为 <Context> 提供给你。

【输出规范】
你需要输出一个满足系统指定 Schema 的 JSON。
1. **intent**: "mutate"(新增、修改、排程混合) | "query"(纯问答) | "replan_all"(全局重排/消除空隙)。
2. **nodes**: 用户意图中的操作动作。
    - `action`: 分为 "insert", "update", "delete", "complete"。
    - `depends_on`: 如果动作 A 必须在动作 B 之后，放入 B 的 ID。
    - `time_constraint`: 抽取绝对时间（如“明天3点”、“推迟2小时”）。对于“然后”、“接着”这种基于顺序的指令，留空 `time_constraint`，只用 `depends_on` 表达即可。
    - `estimated_minutes`: 如果是 insert，必须凭常识填入合理耗时，如吃饭60，开会60，跑步30。
3. **need_clarification**: 只有当你完全无法理解用户指代哪个任务时才设为 true。

【原则】
- **不要计算时间**！你只需要提取并描述拓扑结构，后端的 Python 引擎会自动根据 `depends_on` 和 `time_constraint` 进行排程和日界线处理。
- 绝大多数情况输出一个或多个 action="insert" / "update" 的 node。
"""

def generate_graph_intent_prompt(user_text: str, today: date, day_tasks: List[Task], now_str: str) -> str:
    """将当天的任务树格式化为极简字符串，直接前置注入"""
    context_lines = []
    if not day_tasks:
        context_lines.append("（今日暂无日程）")
    else:
        for t in sorted(day_tasks, key=lambda x: str(x.start_time)):
            status_flag = "[已完成]" if getattr(t, 'status', 'pending') == 'done' else "[待办]"
            # 极简展示：ID - 时间 - 标题
            local_st = str(t.start_time)[:16].replace("T", " ") if t.start_time else "??"
            local_et = str(t.end_time)[:16].replace("T", " ") if t.end_time else "??"
            context_lines.append(f"- ID: {t.id} | {status_flag} {local_st}~{local_et} | {t.title}")
            
    context_str = "\n".join(context_lines)
    
    prompt = f"""
<Context>
【当前时刻】{now_str}
【今日已有日程】
{context_str}
</Context>

【用户请求】
{user_text}
"""
    return prompt

import json
from openai import AzureOpenAI
import google.generativeai as genai
from .models import CommandIntent
import os
import config

def run_graph_agent(user_text: str, today: date, day_tasks: List[Task], now_str: str) -> Optional[GraphIntent]:
    """使用精简 Prompt 和前置上下文调用大模型提取 GraphIntent。"""
    system_prompt = THIN_SYSTEM_PROMPT
    user_prompt = generate_graph_intent_prompt(user_text, today, day_tasks, now_str)
    
    # 尝试 Azure OpenAI (GPT-4o) 借助 instructor 或 内置 API (Structured Outputs)
    # 因为 pydantic 版本的限制，我们这里直接让 LLM 输出 JSON 字符串并通过 parse_raw 解析
    
    system_prompt += "\n\n请输出符合以下 JSON Schema 的结果:\n" + json.dumps(GraphIntent.model_json_schema(), ensure_ascii=False)
    
    api_key = os.getenv("AZURE_OPENAI_API_KEY", config.AZURE_OPENAI_API_KEY)
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", config.AZURE_OPENAI_ENDPOINT)
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", config.AZURE_OPENAI_API_VERSION)
    
    try:
        client = AzureOpenAI(
            api_key=api_key,
            api_version=api_version,
            azure_endpoint=endpoint
        )
        resp = client.chat.completions.create(
            model=config.AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            timeout=15,
        )
        content = resp.choices[0].message.content
        if content:
            return GraphIntent.parse_raw(content)
    except Exception as e:
        log.error(f"[Graph Agent] API failed: {e}")
        return None

