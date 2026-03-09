import logging
import os
from collections import deque
from typing import List, Tuple
from datetime import datetime
import re


# 内存日志缓冲区：存储最近500条日志
_log_buffer: deque = deque(maxlen=500)


class MemoryLogHandler(logging.Handler):
    """将日志同时输出到stdout和内存缓冲区"""
    
    def emit(self, record):
        try:
            msg = self.format(record)
            _log_buffer.append({
                "time": datetime.now().isoformat(),
                "level": record.levelname,
                "name": record.name,
                "message": msg
            })
        except Exception:
            pass


class RedactSecretsFilter(logging.Filter):
    """
    终端/系统日志脱敏：避免把 Supabase/Gemini/OpenAI 等密钥打印出来。
    仅做“展示安全”，不影响实际请求。
    """

    _patterns = [
        # Bearer token / apikey / x-goog-api-key
        re.compile(r"(Bearer\s+)[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE),
        re.compile(r"(apikey['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE),
        re.compile(r"(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE),
        # JWT-like token (very loose)
        re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
        # Google AI Studio key (AIza...)
        re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),
        # OpenAI key (sk-...)
        re.compile(r"\bsk-[0-9A-Za-z\-_]{20,}\b"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = msg
            for p in self._patterns:
                # keep prefix group if present
                if p.groups >= 1:
                    redacted = p.sub(r"\1<redacted>", redacted)
                else:
                    redacted = p.sub("<redacted>", redacted)
            # overwrite record message safely
            record.msg = redacted
            record.args = ()
        except Exception:
            pass
        return True


def setup_logging() -> None:
    """
    统一日志输出到 stdout（Streamlit 运行终端可见）。
    通过环境变量 LOG_LEVEL 控制级别：DEBUG/INFO/WARNING/ERROR
    """
    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    
    # 清除已有的handlers（避免重复）
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    
    # 降噪：httpx/httpcore/hpack 在 DEBUG 时会极其啰嗦
    for noisy in ("httpx", "httpcore", "hpack", "openai._base_client", "openai"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    # 标准输出handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(RedactSecretsFilter())
    
    # 内存缓冲区handler
    memory_handler = MemoryLogHandler()
    memory_handler.setLevel(level)
    memory_handler.setFormatter(console_formatter)
    memory_handler.addFilter(RedactSecretsFilter())
    
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(memory_handler)


def get_recent_logs(limit: int = 100) -> List[dict]:
    """获取最近的日志条目"""
    return list(_log_buffer)[-limit:]
