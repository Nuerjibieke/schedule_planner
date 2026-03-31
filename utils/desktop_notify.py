"""
系统桌面通知：在 macOS 上使用 AppleScript 弹出原生通知，联动「电脑自己的提醒」。
"""
import platform
import subprocess
import logging

log = logging.getLogger("desktop_notify")


def _escape_applescript(s: str) -> str:
    """转义 AppleScript 字符串中的反斜杠和双引号。"""
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, body: str = "") -> bool:
    """
    发送系统桌面通知。
    - macOS: 使用 osascript display notification，会出现在通知中心。
    - 其他平台: 暂无实现，返回 False。
    返回是否发送成功。
    """
    title = (title or "").strip()
    body = (body or "").strip()
    if not title and not body:
        return False

    if platform.system() != "Darwin":
        log.warning("桌面通知当前仅支持 macOS，当前系统: %s", platform.system())
        return False

    try:
        t = _escape_applescript(title or "Reminder")
        b = _escape_applescript(body)
        script = f'display notification "{b}" with title "{t}"'
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return True
    except subprocess.CalledProcessError as e:
        log.warning("osascript 执行失败: %s", e)
        return False
    except FileNotFoundError:
        log.warning("未找到 osascript（非 macOS 或环境异常）")
        return False
    except Exception as e:
        log.warning("发送桌面通知失败: %s", e)
        return False
