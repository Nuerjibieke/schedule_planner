"""数据模型：与 Supabase 表对应。"""
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, Literal, List

TaskStatus = Literal["pending", "done"]
TaskPriority = Literal["high", "normal", "low"]
LongTaskStatus = Literal["todo", "doing", "blocked", "done", "cancelled"]


@dataclass
class Task:
    """任务总表：开始时间、结束时间、标题、优先级、状态、是否锁定。"""
    id: Optional[str]
    start_time: str  # ISO 格式
    end_time: str
    title: str
    priority: TaskPriority
    status: TaskStatus
    is_fixed: bool
    memo: Optional[str] = None
    created_at: Optional[str] = None

    def to_row(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_row(cls, row: dict) -> "Task":
        def _str(v):
            return v if isinstance(v, str) else (v.isoformat() if hasattr(v, "isoformat") else str(v))
        return cls(
            id=str(row["id"]) if row.get("id") else None,
            start_time=_str(row["start_time"]),
            end_time=_str(row["end_time"]),
            title=str(row["title"]),
            priority=row.get("priority", "normal"),
            status=row.get("status", "pending"),
            is_fixed=bool(row.get("is_fixed", False)),
            memo=row.get("memo"),
            created_at=row.get("created_at"),
        )


@dataclass
class Snapshot:
    """快照表：排程前拍照，用于撤销。"""
    id: Optional[str]
    payload: list  # 当日任务的 JSON 列表
    created_at: Optional[str] = None

    def to_row(self) -> dict:
        return {"payload": self.payload}

    @classmethod
    def from_row(cls, row: dict) -> "Snapshot":
        return cls(id=row.get("id"), payload=row.get("payload", []), created_at=row.get("created_at"))


@dataclass
class Summary:
    """总结表：日报/周报/月报摘要。"""
    id: Optional[str]
    date: str  # YYYY-MM-DD（日报=当日，周报=当周周一，月报=当月1日）
    kind: Literal["daily", "weekly", "monthly"]
    content: str
    created_at: Optional[str] = None

    def to_row(self) -> dict:
        return {"date": self.date, "kind": self.kind, "content": self.content}

    @classmethod
    def from_row(cls, row: dict) -> "Summary":
        return cls(
            id=row.get("id"),
            date=row["date"],
            kind=row["kind"],
            content=row["content"],
            created_at=row.get("created_at"),
        )


@dataclass
class Reminder:
    """提醒事项表：记录重要事项，可标记完成。"""
    id: Optional[str]
    content: str
    is_completed: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_row(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None and k != "id"}

    @classmethod
    def from_row(cls, row: dict) -> "Reminder":
        def _str(v):
            return v if isinstance(v, str) else (v.isoformat() if hasattr(v, "isoformat") else str(v))
        return cls(
            id=str(row["id"]) if row.get("id") else None,
            content=str(row["content"]),
            is_completed=bool(row.get("is_completed", False)),
            created_at=_str(row["created_at"]) if row.get("created_at") else None,
            updated_at=_str(row["updated_at"]) if row.get("updated_at") else None,
        )


@dataclass
class LongTask:
    """长周期任务：支持父子层级、进度、起止日期、标签、状态。"""
    id: Optional[str]
    parent_id: Optional[str]
    title: str
    description: Optional[str]
    status: LongTaskStatus
    priority: TaskPriority
    progress: int
    start_date: Optional[str] = None  # YYYY-MM-DD
    due_date: Optional[str] = None    # YYYY-MM-DD
    tags: Optional[List[str]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_row(self) -> dict:
        d = asdict(self)
        # supabase insert/update 时不传 None，id 由数据库生成
        out = {k: v for k, v in d.items() if v is not None}
        if "id" in out:
            del out["id"]
        return out

    @classmethod
    def from_row(cls, row: dict) -> "LongTask":
        def _str(v):
            return v if isinstance(v, str) else (v.isoformat() if hasattr(v, "isoformat") else str(v))
        tags = row.get("tags")
        if tags is None:
            tags_list = None
        elif isinstance(tags, list):
            tags_list = [str(x) for x in tags]
        else:
            # 兜底：可能是字符串/其他结构
            tags_list = [str(tags)]
        return cls(
            id=str(row["id"]) if row.get("id") else None,
            parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
            title=str(row.get("title", "")),
            description=row.get("description"),
            status=row.get("status", "todo"),
            priority=row.get("priority", "normal"),
            progress=int(row.get("progress", 0) or 0),
            start_date=_str(row["start_date"]) if row.get("start_date") else None,
            due_date=_str(row["due_date"]) if row.get("due_date") else None,
            tags=tags_list,
            created_at=_str(row["created_at"]) if row.get("created_at") else None,
            updated_at=_str(row["updated_at"]) if row.get("updated_at") else None,
        )


@dataclass
class LongTaskLog:
    """长周期任务日志：记录进度/状态/备注等变化。"""
    id: Optional[str]
    task_id: str
    kind: Literal["note", "status", "progress", "system"]
    content: str
    created_at: Optional[str] = None

    def to_row(self) -> dict:
        d = asdict(self)
        out = {k: v for k, v in d.items() if v is not None}
        if "id" in out:
            del out["id"]
        return out

    @classmethod
    def from_row(cls, row: dict) -> "LongTaskLog":
        def _str(v):
            return v if isinstance(v, str) else (v.isoformat() if hasattr(v, "isoformat") else str(v))
        return cls(
            id=str(row["id"]) if row.get("id") else None,
            task_id=str(row.get("task_id", "")),
            kind=row.get("kind", "note"),
            content=str(row.get("content", "")),
            created_at=_str(row["created_at"]) if row.get("created_at") else None,
        )
