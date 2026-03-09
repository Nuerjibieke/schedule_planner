from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from db.models import Task
from service.action_state_machine import ActionStepResult, run_action_step


@dataclass
class ExecutorFailure:
    ok: bool
    message: str
    action: Dict[str, Any]
    step_index: int


class ActionExecutor:
    """
    统一动作执行器：
    - 复用单步状态机（pre-validate -> execute -> post-verify）
    - 统一失败语义，避免各分支散落的手写流程漂移
    """

    def __init__(
        self,
        today: date,
        fetch_scope_tasks_fn: Callable[[Dict[str, Any], date], List[Task]],
        verify_with_repair_fn: Callable[[Dict[str, Any], List[Task], List[Task], date], Tuple[bool, str, List[Task]]],
        default_validate_step_fn: Callable[[Dict[str, Any], date, List[Task]], Tuple[bool, str]],
    ) -> None:
        self.today = today
        self.fetch_scope_tasks_fn = fetch_scope_tasks_fn
        self.verify_with_repair_fn = verify_with_repair_fn
        self.default_validate_step_fn = default_validate_step_fn

    def execute(
        self,
        action: Dict[str, Any],
        apply_write_fn: Callable[[], None],
        step_index: int = 1,
        validate_step_fn: Optional[Callable[[Dict[str, Any], date, List[Task]], Tuple[bool, str]]] = None,
        rollback_fn: Optional[Callable[[], int]] = None,
    ) -> ActionStepResult:
        validate_fn = validate_step_fn or self.default_validate_step_fn
        return run_action_step(
            action=action,
            today=self.today,
            fetch_scope_tasks_fn=self.fetch_scope_tasks_fn,
            validate_step_fn=validate_fn,
            apply_write_fn=apply_write_fn,
            verify_with_repair_fn=self.verify_with_repair_fn,
            rollback_fn=rollback_fn,
        )
