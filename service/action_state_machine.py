from dataclasses import dataclass
from datetime import date
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from db.models import Task
from utils.log_markers import LOG_ACTION, LOG_VERIFY, LOG_ROLLBACK

log = logging.getLogger("service.action_state_machine")


@dataclass
class ActionStepResult:
    ok: bool
    message: str
    before_tasks: List[Task]
    after_tasks: List[Task]
    rolled_back_count: int = 0


def run_action_step(
    action: Dict[str, Any],
    today: date,
    fetch_scope_tasks_fn: Callable[[Dict[str, Any], date], List[Task]],
    validate_step_fn: Callable[[Dict[str, Any], date, List[Task]], Tuple[bool, str]],
    apply_write_fn: Callable[[], None],
    verify_with_repair_fn: Callable[[Dict[str, Any], List[Task], List[Task], date], Tuple[bool, str, List[Task]]],
    rollback_fn: Optional[Callable[[], int]] = None,
) -> ActionStepResult:
    """
    统一单步执行状态机：
    pre-validate -> execute -> post-verify -> (optional) rollback.
    """
    act = str(action.get("action", "") or "").strip().lower()
    title = str(action.get("title", "") or "").strip()
    target = str(action.get("target_task_id", "") or "").strip()
    brief = f"act={act or 'unknown'} title={title or '-'} target={target or '-'}"

    log.info("%s step_start %s", LOG_ACTION, brief)
    before_tasks = fetch_scope_tasks_fn(action, today)
    step_ok, step_msg = validate_step_fn(action, today, before_tasks)
    if not step_ok:
        log.warning("%s pre_validate_failed %s reason=%s", LOG_VERIFY, brief, step_msg)
        return ActionStepResult(
            ok=False,
            message=step_msg,
            before_tasks=before_tasks,
            after_tasks=before_tasks,
            rolled_back_count=0,
        )

    log.info("%s write_apply %s", LOG_ACTION, brief)
    apply_write_fn()
    after_tasks = fetch_scope_tasks_fn(action, today)
    ok_effect, effect_msg, after_final = verify_with_repair_fn(action, before_tasks, after_tasks, today)
    if ok_effect:
        log.info("%s post_verify_ok %s", LOG_VERIFY, brief)
        return ActionStepResult(
            ok=True,
            message="",
            before_tasks=before_tasks,
            after_tasks=after_final,
            rolled_back_count=0,
        )

    log.warning("%s post_verify_failed %s reason=%s", LOG_VERIFY, brief, effect_msg or "unknown")
    rolled = 0
    if rollback_fn is not None:
        try:
            rolled = int(rollback_fn() or 0)
            log.warning("%s rollback_done %s restored=%d", LOG_ROLLBACK, brief, rolled)
        except Exception:
            rolled = 0
            log.exception("%s rollback_failed %s", LOG_ROLLBACK, brief)

    return ActionStepResult(
        ok=False,
        message=effect_msg or "post-verify failed",
        before_tasks=before_tasks,
        after_tasks=after_final,
        rolled_back_count=rolled,
    )
