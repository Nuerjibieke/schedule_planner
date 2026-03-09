from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import logging

from ai.graph_models import GraphIntent, DAGNode
from ai.guardrails import parse_natural_time, fuzzy_match_task
from ai.models import CommandIntent
from db.models import Task

log = logging.getLogger(__name__)


def resolve_graph_intent(
    graph: GraphIntent,
    today_date: datetime.date,
    day_tasks: List[Task],
    now_dt: datetime
) -> CommandIntent:
    """
    将基于 DAG 的 GraphIntent 翻译为线性的、带绝对时间的 CommandIntent。
    主要职责：
    1. Fuzzy Match (模糊匹配)
    2. 锚点时间解析 (Anchoring)
    3. 拓扑排序与时间传播 (Topological Sorting & Propagation)
    """
    # 转换回旧的 CommandIntent 外壳，无缝衔接现有系统
    # GraphIntent 的 mutate 表示“混合写操作”，在执行层应落到 plan 通道而非 query。
    intent_map = {
        "mutate": "plan",
        "query": "query",
        "replan_all": "replan_all",
    }
    cmd = CommandIntent(
        intent=intent_map.get(graph.intent, "query"),
        reasoning=graph.reasoning,
        reply_text=graph.reply_text,
        need_clarification=graph.need_clarification,
        clarification_question=graph.clarification_question,
        planning_reason=graph.reasoning,
        actions=[]
    )
    
    if graph.intent == "replan_all":
        cmd.intent = "replan_all"
        return cmd
        
    if not graph.nodes:
        return cmd

    day_task_by_id: Dict[str, Task] = {}
    day_task_by_short: Dict[str, List[Task]] = {}
    for t in day_tasks:
        tid = str(getattr(t, "id", "") or "")
        if not tid:
            continue
        day_task_by_id[tid] = t
        short = tid[:8]
        if short:
            day_task_by_short.setdefault(short, []).append(t)

    # 1. 构建节点查找表与度数表
    nodes_by_id: Dict[str, DAGNode] = {n.id: n for n in graph.nodes}
    # 关键修复：入度应表示“当前节点依赖了多少图内父节点”，而不是“有多少节点依赖它”。
    in_degrees: Dict[str, int] = {
        n.id: sum(1 for dep in (n.depends_on or []) if dep in nodes_by_id)
        for n in graph.nodes
    }
    adj_list: Dict[str, List[str]] = {n.id: [] for n in graph.nodes}
    for n in graph.nodes:
        for dep_id in n.depends_on:
            if dep_id in adj_list:
                adj_list[dep_id].append(n.id)
                
    # 解析时间表
    resolved_times: Dict[str, dict] = {} # node_id -> {"start": datetime, "end": datetime, "target_task_id": str}
    
    # 2. 拓扑排序队列
    queue = [nid for nid, deg in in_degrees.items() if deg == 0]
    
    while queue:
        curr_id = queue.pop(0)
        curr_node = nodes_by_id[curr_id]
        
        # --- 计算当前节点的绝对时间 ---
        start_dt: Optional[datetime] = None
        duration = curr_node.estimated_minutes or 60 # 默认60分
        
        # A. 如果有绝对时间约束
        if curr_node.time_constraint:
            parsed = parse_natural_time(curr_node.time_constraint, base_date=today_date)
            if parsed:
                start_dt = parsed
                
        resolved_parent_id: Optional[str] = None
        unresolved_dependency: Optional[str] = None

        # B. 如果依赖前置节点
        if not start_dt and curr_node.depends_on:
            for parent_id in curr_node.depends_on:
                # B1. 前置节点在本次 DAG 内（已被解析过）
                if parent_id in resolved_times and resolved_times[parent_id]["end"]:
                    start_dt = resolved_times[parent_id]["end"]
                    resolved_parent_id = parent_id
                    unresolved_dependency = None
                    break

                # B2. 前置节点是外部任务（真实 DB UUID 或短前缀）
                task = day_task_by_id.get(parent_id)
                if task is None:
                    candidates = day_task_by_short.get(parent_id, [])
                    if len(candidates) == 1:
                        task = candidates[0]
                if task is None:
                    unresolved_dependency = parent_id
                    continue
                try:
                    end_str = task.end_time.replace("Z", "+00:00") if isinstance(task.end_time, str) else task.end_time.isoformat()
                    parent_end_utc = datetime.fromisoformat(end_str)
                    # DB 存的是 UTC，但 app_service 期望 solver 输出本地时间
                    parent_end_local = parent_end_utc.astimezone().replace(tzinfo=None)
                    start_dt = parent_end_local
                    resolved_parent_id = str(task.id)
                    unresolved_dependency = None
                    log.info(
                        "依赖外部任务 %s (%s), end_utc=%s, end_local=%s",
                        resolved_parent_id, task.title, parent_end_utc, start_dt
                    )
                    break
                except Exception as e:
                    unresolved_dependency = parent_id
                    log.warning("解析外部依赖任务时间失败: %s", e)
                
        # C. 兜底方案：如果没有预设约束，也没有依赖，认为是立刻(now)开始
        if not start_dt:
            # 只有当这是一个 insert 操作且并非孤立的 update 时赋值 now
            if curr_node.action == "insert" and not curr_node.depends_on:
                start_dt = now_dt
        
        def _to_local_naive(value: Any) -> Optional[datetime]:
            if value is None:
                return None
            try:
                if isinstance(value, str):
                    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                else:
                    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    return dt.replace(microsecond=0)
                return dt.astimezone().replace(tzinfo=None, microsecond=0)
            except Exception:
                return None
        
        # --- 处理目标绑定 ---
        target_uuid = None
        target_ref = str(getattr(curr_node, "target_id", "") or "").strip()
        resolved_title = str(getattr(curr_node, "title", "") or "").strip()
        if curr_node.action in ("update", "delete", "complete"):
            matched = None
            if target_ref:
                matched = day_task_by_id.get(target_ref)
                if matched is None:
                    candidates = day_task_by_short.get(target_ref, [])
                    if len(candidates) == 1:
                        matched = candidates[0]
            if matched is None and resolved_title:
                matched = fuzzy_match_task(resolved_title, day_tasks)
            if matched:
                target_uuid = str(matched.id)
                if not resolved_title:
                    resolved_title = str(getattr(matched, "title", "") or "").strip()
                if not start_dt and curr_node.action == "update":
                    # update 未给显式时间时，继承目标任务原时间窗口，避免误生成“无时间锚点更新”。
                    inherited_start = _to_local_naive(getattr(matched, "start_time", None))
                    inherited_end = _to_local_naive(getattr(matched, "end_time", None))
                    if inherited_start is not None:
                        start_dt = inherited_start
                        if inherited_end is not None and inherited_end > inherited_start:
                            duration = max(1, int((inherited_end - inherited_start).total_seconds() // 60))
                        log.info(
                            "update 节点继承目标任务时间: target=%s title=%s start=%s duration=%s",
                            target_uuid, resolved_title or getattr(matched, "title", ""), start_dt, duration
                        )
        
        end_dt = start_dt + timedelta(minutes=duration) if start_dt else None
                    
        resolved_times[curr_id] = {
            "start": start_dt,
            "end": end_dt,
            "target_task_id": target_uuid
        }
        
        # --- 组装 action ---
        normalized_depends_on: List[str] = []
        for dep in list(curr_node.depends_on or []):
            dep_s = str(dep or "").strip()
            if not dep_s:
                continue
            # 图内依赖节点 ID 不应直接下发给执行层（执行层只认任务 ID）。
            if dep_s in nodes_by_id:
                parent_state = resolved_times.get(dep_s) or {}
                parent_target = str(parent_state.get("target_task_id") or "").strip()
                if parent_target:
                    normalized_depends_on.append(parent_target)
                continue
            if dep_s in day_task_by_id:
                normalized_depends_on.append(dep_s)
                continue
            short_candidates = day_task_by_short.get(dep_s, [])
            if len(short_candidates) == 1:
                normalized_depends_on.append(str(short_candidates[0].id))
                continue
            # 外部依赖且无法解析时保留原值，让预校验明确报错。
            normalized_depends_on.append(dep_s)

        action_dict = {
            "action": curr_node.action,
            "source_node_id": curr_id,
            "title": resolved_title,
            "priority": curr_node.priority,
            "duration_minutes": duration,
            "depends_on": normalized_depends_on,
        }
        if start_dt:
            action_dict["start_iso"] = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
            # 兼容 update 需要的字段
            action_dict["new_start_iso"] = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        if end_dt:
            action_dict["end_iso"] = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
            action_dict["new_end_iso"] = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
            
        if target_uuid:
            action_dict["target_task_id"] = target_uuid
        elif target_ref:
            # 若今天任务快照里未命中，也保留模型给出的目标标识，交由执行层继续解析。
            action_dict["target_task_id"] = target_ref
        if resolved_parent_id:
            action_dict["resolved_parent_id"] = resolved_parent_id
        if unresolved_dependency:
            action_dict["unresolved_dependency"] = unresolved_dependency
        if isinstance(curr_node.preconditions, dict):
            action_dict["preconditions"] = curr_node.preconditions
        if isinstance(curr_node.expected_delta, dict):
            action_dict["expected_delta"] = curr_node.expected_delta
            
        memo_str = ""
        if curr_node.tips:
            memo_str += "执行建议: " + " | ".join(curr_node.tips)
        if curr_node.difficulty:
            memo_str += f" | 难度: {curr_node.difficulty}"
        if memo_str:
            action_dict["memo"] = memo_str.strip(" |")
            
        cmd.actions.append(action_dict)
        
        # 将邻居入度 -1
        for neighbor in adj_list[curr_id]:
            in_degrees[neighbor] -= 1
            if in_degrees[neighbor] == 0:
                queue.append(neighbor)
                
    # Fallback for cyclic graphs (if any)
    if len(resolved_times) < len(graph.nodes):
        unresolved_ids = [n.id for n in graph.nodes if n.id not in resolved_times]
        log.warning("Detected cycle in DAG, unresolved nodes=%s", unresolved_ids)
        cmd.actions = []
        cmd.need_clarification = True
        if not cmd.clarification_question:
            cmd.clarification_question = "我没法稳定解析这次任务依赖关系，请换一种更直接的描述（例如先做A再做B，并给出大致时长）。"
        if not cmd.reply_text:
            cmd.reply_text = cmd.clarification_question

    return cmd
