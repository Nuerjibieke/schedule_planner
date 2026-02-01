"""
日程提醒守护进程（可选）：不打开浏览器时也可收到系统提醒。
与 app.py 共用同一套逻辑；打开 app 时由 app 内 fragment 定时检查，无需单独运行本脚本。
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db.client import get_supabase
from config import REMINDER_MINUTES_BEFORE, REMINDER_POLL_INTERVAL
from utils.reminder_check import run_reminder_check


def main() -> None:
    print(
        f"[reminder] 守护进程启动，每 {REMINDER_POLL_INTERVAL} 秒检查一次，"
        f"提前 {REMINDER_MINUTES_BEFORE} 分钟提醒",
        flush=True,
    )
    get_supabase()
    while True:
        try:
            n = run_reminder_check()
            if n > 0:
                print(f"[reminder] 已发送 {n} 条提醒", flush=True)
        except KeyboardInterrupt:
            print("[reminder] 已退出", flush=True)
            break
        except Exception as e:
            print(f"[reminder] 本轮异常: {e}", flush=True)
        time.sleep(REMINDER_POLL_INTERVAL)


if __name__ == "__main__":
    main()
