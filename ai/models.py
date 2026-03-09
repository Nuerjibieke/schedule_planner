from dataclasses import dataclass, asdict
from typing import Optional, Literal, List, Dict, Any

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
    actions: Optional[List] = None
    # 声明式目标态：用于后续 Diff 引擎转换为 actions
    desired_state: Optional[List[Dict[str, Any]]] = None
    # 需要澄清（例如多任务同名、时间不明确等）
    need_clarification: Optional[bool] = None
    clarification_question: Optional[str] = None
    candidates: Optional[List[str]] = None
    # 解析来源：llm / rule_fallback / agent_resolved
    parse_source: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}
