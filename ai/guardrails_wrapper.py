from datetime import date, datetime, timedelta
import logging
from typing import Optional, List

from db.models import Task
from .models import CommandIntent
from .guardrails import parse_natural_time, fuzzy_match_task, merge_progress_into_memo

log = logging.getLogger(__name__)


def apply_guardrails(
    cmd: CommandIntent,
    today: date,
    day_tasks: Optional[List[Task]],
    user_text: str,
    now_dt: Optional[datetime] = None
) -> CommandIntent:
    """
    拦截 LLM 输出的初步 JSON（已映射为 CommandIntent，但其中的字段可能是
    尚未解析的自然语言，如 target_title_expr 等），使用确定性 Python 逻辑进行
    补全和防幻觉修正，最终组装成 app_service 期望的、包含准确 target_task_id 和 ISO 格式的合法命令。
    """
    if day_tasks is None:
        day_tasks = []
        
    now = now_dt or datetime.now()

    # 处理全局重排意图
    if cmd.intent == "replan_all":
        cmd.intent = "plan"
        cmd.actions = []
        # 将所有 pending 的任务提取出来
        pending_tasks = [t for t in day_tasks if getattr(t, 'status', 'pending') == "pending"]
        
        current_start = now
        for pt in pending_tasks:
            duration = 60 # 默认60分钟，或从原任务解析
            if hasattr(pt, 'start_time') and hasattr(pt, 'end_time'):
                try:
                    s_dt = datetime.fromisoformat(pt.start_time.replace("Z", "+00:00")).replace(tzinfo=None)
                    e_dt = datetime.fromisoformat(pt.end_time.replace("Z", "+00:00")).replace(tzinfo=None)
                    duration = max(15, int((e_dt - s_dt).total_seconds() / 60))
                except Exception:
                    duration = 60

            next_end = current_start + timedelta(minutes=duration)
            cmd.actions.append({
                "action": "plan",
                "title": getattr(pt, 'title', '未命名任务'),
                "start_iso": current_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "end_iso": next_end.strftime("%Y-%m-%dT%H:%M:%S"),
                "duration_minutes": duration,
                "target_task_id": str(getattr(pt, 'id', ''))
            })
            current_start = next_end
            
        if not cmd.planning_reason:
            cmd.planning_reason = "根据当前时间自动顺延了所有尚未完成的待办事项。"
        if not cmd.reply_text:
            cmd.reply_text = "已为您重新安排今天的待办。"
            
        return cmd

    # 遍历 Actions 进行逐一护栏校验
    actions = cmd.actions if isinstance(cmd.actions, list) else []
    
    for action in actions:
        if not isinstance(action, dict):
            continue
            
        act_type = action.get("action", "")
        
        # 1. 处理目标标识符（将 expr 映射到真实 ID 或字段）
        target_title_expr = action.get("target_title_expr")
        if target_title_expr:
            if act_type in ("insert", "plan"):
                # 新增/计划任务：expr 直接作为任务标题
                if not action.get("title"):
                    action["title"] = target_title_expr
            elif act_type in ("update", "delete", "complete"):
                # 修改/删除：尝试模糊匹配已有任务
                matched_task = fuzzy_match_task(target_title_expr, day_tasks)
                if matched_task:
                    action["target_task_id"] = str(matched_task.id)
                    action["target_title"] = matched_task.title

        # 2. 解析自然时间
        start_expr = action.get("start_time_expr")
        if start_expr:
            parsed_start = parse_natural_time(start_expr, base_date=today)
            if parsed_start:
                action["start_iso"] = parsed_start.strftime("%Y-%m-%dT%H:%M:%S")
                
        end_expr = action.get("end_time_expr")
        if end_expr:
            parsed_end = parse_natural_time(end_expr, base_date=today)
            if parsed_end:
                action["end_iso"] = parsed_end.strftime("%Y-%m-%dT%H:%M:%S")
                
        # 3. 记录已发生的事情 ("昨天看书")
        if action.get("is_past_record"):
            action["status"] = "done"
            # 强制用现在的时刻结束
            if "end_iso" not in action:
                action["end_iso"] = now.strftime("%Y-%m-%dT%H:%M:%S")

        # 4. 进度追踪
        prog = action.get("progress_percentage")
        if prog is not None and isinstance(prog, (int, float)):
            action["action"] = "complete" # 进度更新也走 complete 路由，但会保留进度
            tid = action.get("target_task_id")
            if tid:
                # 寻找旧的 memo
                old_m = ""
                for t in day_tasks:
                    if str(getattr(t, "id", "")) == tid:
                        old_m = getattr(t, "memo", "") or ""
                        break
                action["memo"] = merge_progress_into_memo(old_m, int(prog))

        # 5. 修正时长覆盖
        ndm = action.get("new_duration_minutes")
        if ndm is not None:
            action["duration_minutes"] = ndm # app_service.py update branch respects this
            
        # 清理供 LLM 提取的冗余字段（避免后续报错）
        action.pop("target_title_expr", None)
        action.pop("start_time_expr", None)
        action.pop("end_time_expr", None)
        action.pop("is_past_record", None)
        action.pop("new_duration_minutes", None)
        action.pop("progress_percentage", None)

    cmd.actions = actions
    return cmd
