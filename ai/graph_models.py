from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any

class DAGNode(BaseModel):
    """
    DAG 拓扑图中的操作节点。
    LLM 不再输出绝对时间，而是提取出的事实和依赖约束。
    """
    id: str = Field(description="节点唯一ID，如 'node_1'")
    action: Literal["insert", "update", "delete", "complete"] = Field(description="操作类型")
    title: str = Field(default="", description="操作的任务名称，如 '跑步'、'晚饭'、'组会'")
    target_id: Optional[str] = Field(
        default=None,
        description="可选：update/delete/complete 的目标任务 ID（完整 UUID 或短前缀）",
    )
    
    # 时长估计
    estimated_minutes: Optional[int] = Field(
        default=None, 
        description="任务估计耗时（分钟）。如果用户未指定，请根据常识填入合理的预估值（如吃饭60，开会60，跑步30）。\n若是 update 操作且用户没说时长，留空。"
    )
    
    # 依赖与时间约束 (核心)
    depends_on: Optional[List[str]] = Field(
        default_factory=list,
        description="依赖的节点ID列表。如果当前任务必须在某个任务之后进行（如'跑完步吃饭'），填入前置任务的节点ID。"
    )
    time_constraint: Optional[str] = Field(
        default=None,
        description="绝对时间约束。仅当用户明确提到绝对时间（如'下午3点'、'明天'、'推迟半小时'）时提取原始文本。如果是纯相对顺序（'接着'、'然后'），留空。"
    )
    
    # 其他属性
    difficulty: Optional[Literal["easy", "medium", "hard"]] = Field(default="medium")
    tips: Optional[List[str]] = Field(default_factory=list, description="给用户的执行建议")
    priority: Optional[Literal["high", "normal", "low"]] = Field(default="normal")
    progress_percentage: Optional[int] = Field(default=None)
    preconditions: Optional[Dict[str, Any]] = Field(
        default=None,
        description="可选的执行前约束（结构化对象）。后端会在执行前校验。",
    )
    expected_delta: Optional[Dict[str, Any]] = Field(
        default=None,
        description="可选的执行后期望变化（结构化对象）。后端会在执行后验收。",
    )


class GraphIntent(BaseModel):
    """
    替代 CommandIntent 的新图结构根模型 (The Thin Prompt Root Schema)
    """
    intent: Literal["mutate", "query", "replan_all"] = Field(
        description="宏观意图：mutate(包含增删改查和排程的混合操作), query(纯问答/无操作), replan_all(全局消除冲突重排)"
    )
    
    nodes: List[DAGNode] = Field(
        default_factory=list,
        description="抽取的任务实体和相互依赖的网络。"
    )
    
    reasoning: str = Field(
        description="你对用户意图的理解，以及为什么这样建立依赖关系的推理过程。"
    )
    reply_text: str = Field(
        description="给用户的简短自然语言回复（必须统一转换为本地时间展示）。"
    )
    
    need_clarification: bool = Field(default=False)
    clarification_question: Optional[str] = Field(default="")
