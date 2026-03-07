"""
智能排程终端：Streamlit UI
- 左侧：对话与控制（聊天框、生成周报、撤销）
- 右侧：日程时间轴（日期选择 + 时间块可视化）
"""
import streamlit as st
from datetime import date, datetime, timedelta, timezone
import streamlit.components.v1 as components

from db.client import init_supabase
from config import SUPABASE_URL, SUPABASE_KEY, WORKDAY_START_HOUR, WORKDAY_END_HOUR, REMINDER_POLL_INTERVAL
from service.app_service import (
    handle_user_command, 
    apply_pending_plan,
    undo_last, 
    get_today_tasks, 
    get_tasks_for_date,
    toggle_task_status,
    delete_task_by_id,
    search_tasks_by_keyword,
    get_task_statistics,
    check_and_reschedule_overdue_tasks,
    update_task_progress,
    start_task_timer,
    pause_task_timer,
    end_task_timer,
)
from report import get_weekly_report_text, get_monthly_report_text, run_daily_archive
from utils.logging_setup import setup_logging, get_recent_logs
from db.models import Task
from db import repo
from utils.reminder_check import run_reminder_check

setup_logging()

# ---------- 最先处理 URL 触发的进度/计时更新，再 rerun，避免后续用旧数据渲染 ----------
if "progress_update" in st.query_params:
    _tid = st.query_params.get("task_id")
    _pstr = st.query_params.get("progress")
    if _tid and _pstr:
        try:
            _p = int(_pstr)
            _ok, _msg = update_task_progress(str(_tid).strip(), _p)
            st.session_state.last_feedback = _msg
        except Exception as e:
            st.session_state.last_feedback = f"更新进度失败：{e}"
    st.query_params.clear()
    st.rerun()

if "timer_action" in st.query_params:
    _act = st.query_params.get("timer_action")
    _tid = st.query_params.get("task_id")
    if _act and _tid:
        try:
            if _act == "start":
                _ok, _msg = start_task_timer(str(_tid).strip())
                if _ok:
                    st.session_state.current_executing_task_id = str(_tid).strip()
            elif _act == "pause":
                _ok, _msg = pause_task_timer(str(_tid).strip())
            elif _act == "end":
                _ok, _msg = end_task_timer(str(_tid).strip())
                if _ok:
                    st.session_state.current_executing_task_id = None
            else:
                _ok, _msg = False, "未知操作"
            if _ok:
                st.session_state.last_feedback = _msg
            else:
                st.session_state.last_feedback = f"操作失败：{_msg}"
        except Exception as e:
            st.session_state.last_feedback = f"操作失败：{e}"
        st.query_params.clear()
        st.rerun()

# ---------- 活跃度矩阵（GitHub 风格） ----------
def _to_naive_local(dt: datetime) -> datetime:
    """把可能带时区的 datetime 转成可比较的 naive（以本地时区为准）。"""
    try:
        if dt.tzinfo is not None:
            return dt.astimezone().replace(tzinfo=None)
    except Exception:
        pass
    return dt.replace(tzinfo=None)


def _daily_work_minutes_from_tasks(tasks: list[Task]) -> dict[str, int]:
    """
    将任务列表聚合为按天的工作分钟数：{YYYY-MM-DD: minutes}
    规则：按 start_time 所在日期归档；单任务时长按 end-start（异常忽略）。
    """
    out: dict[str, int] = {}
    for t in tasks:
        try:
            start = datetime.fromisoformat((t.start_time or "").replace("Z", "+00:00"))
            end = datetime.fromisoformat((t.end_time or "").replace("Z", "+00:00"))
            start_n = _to_naive_local(start)
            end_n = _to_naive_local(end)
            dur = int((end_n - start_n).total_seconds() / 60)
            if dur <= 0:
                continue
            day_key = start_n.date().isoformat()
            out[day_key] = out.get(day_key, 0) + dur
        except Exception:
            continue
    return out


def _heat_level(minutes: int) -> int:
    """0-4 档热力等级（越大越深）。"""
    if minutes <= 0:
        return 0
    if minutes <= 30:
        return 1
    if minutes <= 120:
        return 2
    if minutes <= 240:
        return 3
    return 4


def render_activity_matrix(year_days: int = 365) -> None:
    """渲染左侧活跃度矩阵：按日历年 1月1日～12月31日 显示，月份为 1月～12月。"""
    today = date.today()
    # 固定为当年 1月1日～12月31日，保证横轴始终是 1月到12月
    start_d = date(today.year, 1, 1)
    end_d = date(today.year, 12, 31)

    try:
        tasks = repo.tasks_for_date_range(start_d, end_d)
    except Exception:
        tasks = []

    daily = _daily_work_minutes_from_tasks(tasks)

    # GitHub 风格：按周列（从周一开始），7 行（周一到周日）
    start_aligned = start_d - timedelta(days=start_d.weekday())
    end_aligned = end_d + timedelta(days=(6 - end_d.weekday()))

    days: list[date] = []
    cur = start_aligned
    while cur <= end_aligned:
        days.append(cur)
        cur += timedelta(days=1)

    weeks: list[list[date]] = []
    for i in range(0, len(days), 7):
        weeks.append(days[i : i + 7])

    colors = {
        0: "#e5e7eb",
        1: "#c7e9c0",
        2: "#74c476",
        3: "#31a354",
        4: "#006d2c",
    }

    # 月份：只在当年范围内、每月首次出现的周显示标签（1月～12月，不出现去年12月等）
    month_annotations: list[tuple[int, str]] = []
    last_m = None
    for wi, w in enumerate(weeks):
        d0 = w[0]
        m, y = d0.month, d0.year
        if y != start_d.year:
            continue  # 不显示去年/明年的月份，避免出现 "Dec 26" 等
        if last_m is None or m != last_m:
            month_annotations.append((wi, d0.strftime("%b")))
        last_m = m

    row_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    cell = 12
    gap = 3
    weeks_count = len(weeks)
    grid_w = weeks_count * (cell + gap) - gap
    grid_h = 7 * (cell + gap) - gap

    def fmt_tip(d: date) -> str:
        mins = daily.get(d.isoformat(), 0)
        if mins <= 0:
            return f"{d.isoformat()}：0 分钟"
        h = mins // 60
        m = mins % 60
        if h > 0:
            return f"{d.isoformat()}：{h} 小时 {m} 分钟"
        return f"{d.isoformat()}：{m} 分钟"

    start_key = start_d.isoformat()
    end_key = end_d.isoformat()
    # 仅“今天及以前”的格子有数据高亮，之后的格子灰显
    today_key = today.isoformat()

    squares_html = []
    for w in weeks:
        for d in w:
            key = d.isoformat()
            in_range = (start_key <= key <= end_key)
            # 仅今天及以前显示数据与高亮，之后灰显
            active = in_range and (key <= today_key)
            mins = daily.get(key, 0) if active else 0
            lvl = _heat_level(mins) if active else 0
            bg = colors.get(lvl, colors[0])
            opacity = "1" if active else "0.15"
            tip = fmt_tip(d) if active else ""
            squares_html.append(
                f"""<div class="hm-cell" style="background:{bg};opacity:{opacity}" data-tip="{tip}"></div>"""
            )

    col_w = cell + gap
    month_html = "".join(
        [f'<span class="hm-month" style="left:{wi * col_w}px">{label}</span>'
         for wi, label in month_annotations]
    )

    html = f"""
    <div style="width:100%; box-sizing:border-box;">
      <div style="
        padding: 10px 14px;
        border: 1px solid rgba(220, 218, 214, 0.5);
        border-radius: 20px;
        background: rgba(245, 250, 247, 0.9);
        backdrop-filter: blur(20px);
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.04), 0 1px 4px rgba(0, 0, 0, 0.02);
      ">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom: 6px;">
          <div style="font-size: 12px; color:#7a7872; font-weight: 700;">📈 活跃度</div>
          <div style="font-size: 11px; color:#94a3b8; font-weight: 600;">今年 1月～12月</div>
        </div>

        <style>
          .hm-wrap {{
            display:flex;
            gap: 8px;
            align-items:flex-start;
          }}
          .hm-rows {{
            display:flex;
            flex-direction:column;
            gap:{gap}px;
            padding-top: 18px;
            min-width: 28px;
          }}
          .hm-rowlab {{
            height:{cell}px;
            line-height:{cell}px;
            font-size: 10px;
            color:#94a3b8;
            font-weight: 600;
          }}
          .hm-grid-area {{
            flex: 1;
            overflow-x:auto;
            padding-bottom: 4px;
          }}
          .hm-months {{
            position: relative;
            width: {grid_w}px;
            height: 14px;
            margin-bottom: 4px;
            color:#94a3b8;
            font-size:10px;
            font-weight:600;
          }}
          .hm-month {{
            position: absolute;
            text-align: left;
            height: 14px;
            line-height: 14px;
            white-space: nowrap;
          }}
          .hm-grid {{
            width:{grid_w}px;
            height:{grid_h}px;
            display:grid;
            grid-auto-flow: column;
            grid-template-rows: repeat(7, {cell}px);
            gap:{gap}px;
          }}
          .hm-cell {{
            width:{cell}px;
            height:{cell}px;
            border-radius: 3px;
            position: relative;
            box-shadow: inset 0 0 0 1px rgba(120, 120, 120, 0.10);
          }}
          .hm-cell:hover {{
            outline: 2px solid rgba(99, 102, 241, 0.25);
            outline-offset: 1px;
          }}
          .hm-tooltip {{
            position: fixed;
            z-index: 999999;
            padding: 6px 8px;
            background: rgba(15, 23, 42, 0.92);
            color: #fff;
            border-radius: 8px;
            font-size: 11px;
            font-weight: 600;
            pointer-events: none;
            transform: translate(-50%, -110%);
            white-space: nowrap;
            box-shadow: 0 8px 24px rgba(0,0,0,0.18);
            display:none;
          }}
          .hm-legend {{
            display:flex;
            align-items:center;
            gap: 6px;
            justify-content:flex-end;
            margin-top: 6px;
            color:#94a3b8;
            font-size: 10px;
            font-weight: 600;
          }}
          .hm-lg {{
            width:{cell}px;
            height:{cell}px;
            border-radius: 3px;
            box-shadow: inset 0 0 0 1px rgba(120, 120, 120, 0.10);
          }}
        </style>

        <div class="hm-wrap">
          <div class="hm-rows">
            {''.join([f'<div class="hm-rowlab">{x}</div>' for x in row_labels])}
          </div>
          <div class="hm-grid-area">
            <div class="hm-months">{month_html}</div>
            <div class="hm-grid">
              {''.join(squares_html)}
            </div>
          </div>
        </div>

        <div class="hm-legend">
          <span>Less</span>
          <span class="hm-lg" style="background:{colors[0]}"></span>
          <span class="hm-lg" style="background:{colors[1]}"></span>
          <span class="hm-lg" style="background:{colors[2]}"></span>
          <span class="hm-lg" style="background:{colors[3]}"></span>
          <span class="hm-lg" style="background:{colors[4]}"></span>
          <span>More</span>
        </div>
      </div>
      <div class="hm-tooltip" id="hm-tip"></div>
      <script>
        const tip = document.getElementById('hm-tip');
        function showTip(text, x, y) {{
          if (!text) return;
          tip.textContent = text;
          tip.style.left = x + 'px';
          tip.style.top = y + 'px';
          tip.style.display = 'block';
        }}
        function hideTip() {{
          tip.style.display = 'none';
        }}
        document.querySelectorAll('.hm-cell').forEach(el => {{
          el.addEventListener('mousemove', (e) => {{
            const t = el.getAttribute('data-tip') || '';
            showTip(t, e.clientX, e.clientY);
          }});
          el.addEventListener('mouseleave', hideTip);
        }});
      </script>
    </div>
    """

    components.html(html, height=220, scrolling=False)

# （下方曾有一份重复的活跃度矩阵实现，已移除，避免重复定义。）

# 仅在有密钥时初始化 Supabase
if SUPABASE_URL and SUPABASE_KEY:
    init_supabase(SUPABASE_URL, SUPABASE_KEY)

st.set_page_config(page_title="智能排程", layout="wide", initial_sidebar_state="collapsed")

# 会话状态初始化（必须在所有使用之前）
if "last_feedback" not in st.session_state:
    st.session_state.last_feedback = ""
if "overflow_warning" not in st.session_state:
    st.session_state.overflow_warning = None
if "daily_report_text" not in st.session_state:
    st.session_state.daily_report_text = ""
if "weekly_report_text" not in st.session_state:
    st.session_state.weekly_report_text = ""
if "monthly_report_text" not in st.session_state:
    st.session_state.monthly_report_text = ""
if "report_cache" not in st.session_state:
    st.session_state.report_cache = {}
if "view_date" not in st.session_state:
    st.session_state.view_date = date.today()
if "search_keyword" not in st.session_state:
    st.session_state.search_keyword = ""
if "pending_plan" not in st.session_state:
    st.session_state.pending_plan = None
if "llm_reasoning" not in st.session_state:
    st.session_state.llm_reasoning = ""
if "conversation_history" not in st.session_state:
    st.session_state.conversation_history = []
if "auto_checked_overdue" not in st.session_state:
    st.session_state.auto_checked_overdue = False
if "show_logs" not in st.session_state:
    st.session_state.show_logs = False
if "current_executing_task_id" not in st.session_state:
    st.session_state.current_executing_task_id = None


@st.fragment(run_every=timedelta(seconds=REMINDER_POLL_INTERVAL))
def _schedule_reminder_fragment():
    """后台定时检查：到点或临近开始时发送系统桌面提醒（与电脑通知联动）。"""
    run_reminder_check()


_schedule_reminder_fragment()

# 全局样式美化：现代化设计风格
st.markdown(
    """
    <style>
      /* 主背景 - 浅灰绿色系，护眼舒适 */
      .stApp {
        background: #f0f4f0 !important;
      }
      main {
        background: transparent !important;
      }
      body {
        background: transparent !important;
      }
      #root {
        background: transparent !important;
      }
      
      /* 主容器 - 完全移除顶部空白 */
      .block-container {
        padding-top: 0 !important;
        padding-bottom: 2rem;
        max-width: 1600px;
      }
      
      /* 减少顶部空白 */
      .main .block-container {
        padding-top: 0 !important;
      }
      
      /* 移除Streamlit默认的顶部间距 */
      header[data-testid="stHeader"] {
        display: none !important;
      }
      
      /* 移除所有可能的顶部间距 */
      div[data-testid="stVerticalBlock"] {
        padding-top: 0 !important;
        margin-top: 0 !important;
      }
      
      div[data-testid="stVerticalBlock"] > div:first-child {
        padding-top: 0 !important;
        margin-top: 0 !important;
      }
      
      /* 移除第一个容器的顶部间距 */
      [data-testid="stContainer"]:first-child {
        margin-top: 0 !important;
        padding-top: 0.5rem !important;
      }
      
      /* 确保内容从顶部开始 */
      main {
        padding-top: 0 !important;
      }
      
      section[data-testid="stAppViewContainer"] {
        padding-top: 0 !important;
      }
      
      /* 移除第一个st.columns上方的所有空白 - 最激进 */
      div[data-testid="stVerticalBlock"]:has(> div[data-testid="column"]:first-child) {
        margin-top: -12rem !important;
        padding-top: 0 !important;
      }
      
      /* 确保时钟部分紧贴顶部 */
      div[data-testid="column"]:has([id*="sp-time"]),
      div[data-testid="column"]:has([id*="pomo-display"]) {
        margin-top: 0 !important;
        padding-top: 0 !important;
      }
      
      /* 进一步移除时钟区域上方的空白 - 最激进 */
      div[data-testid="stVerticalBlock"]:has(> div[data-testid="column"]:has([id*="sp-time"])) {
        margin-top: -12rem !important;
        padding-top: 0 !important;
      }
      
      div[data-testid="stVerticalBlock"]:has(> div[data-testid="column"]:has([id*="pomo-display"])) {
        margin-top: -12rem !important;
        padding-top: 0 !important;
      }
      
      /* 移除所有包含st.columns的垂直块的上方空白 */
      div[data-testid="stVerticalBlock"]:has(div[data-testid="column"]) {
        margin-top: -10rem !important;
        padding-top: 0 !important;
      }
      
      /* 第一个包含columns的垂直块 - 最激进 */
      div[data-testid="stVerticalBlock"]:has(div[data-testid="column"]):first-of-type {
        margin-top: -14rem !important;
        padding-top: 0 !important;
      }
      
      /* 卡片容器 - 浅灰绿色系，护眼舒适 */
      [data-testid="stContainer"] {
        border-radius: 20px;
        border: 1px solid rgba(210, 225, 215, 0.5);
        background: rgba(245, 250, 247, 0.85);
        backdrop-filter: blur(20px);
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.04),
                    0 1px 4px rgba(0, 0, 0, 0.02);
        padding: 1rem 1.25rem;
        margin-bottom: 0.75rem;
        transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
      }
      
      /* 第1个容器 - 浅灰绿色变体1 */
      [data-testid="stContainer"]:nth-of-type(1),
      [data-testid="stContainer"]:nth-of-type(6),
      [data-testid="stContainer"]:nth-of-type(11) {
        background: rgba(238, 245, 240, 0.9);
        border-color: rgba(210, 225, 215, 0.4);
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05),
                    0 1px 4px rgba(0, 0, 0, 0.02);
      }
      
      /* 第2个容器 - 浅灰绿色变体2 */
      [data-testid="stContainer"]:nth-of-type(2),
      [data-testid="stContainer"]:nth-of-type(7),
      [data-testid="stContainer"]:nth-of-type(12) {
        background: rgba(235, 245, 238, 0.9);
        border-color: rgba(205, 220, 210, 0.4);
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05),
                    0 1px 4px rgba(0, 0, 0, 0.02);
      }
      
      /* 第3个容器 - 浅灰绿色变体3 */
      [data-testid="stContainer"]:nth-of-type(3),
      [data-testid="stContainer"]:nth-of-type(8),
      [data-testid="stContainer"]:nth-of-type(13) {
        background: rgba(240, 248, 242, 0.9);
        border-color: rgba(210, 228, 218, 0.4);
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05),
                    0 1px 4px rgba(0, 0, 0, 0.02);
      }
      
      /* 第4个容器 - 浅灰绿色变体4 */
      [data-testid="stContainer"]:nth-of-type(4),
      [data-testid="stContainer"]:nth-of-type(9),
      [data-testid="stContainer"]:nth-of-type(14) {
        background: rgba(242, 250, 245, 0.9);
        border-color: rgba(215, 230, 220, 0.4);
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05),
                    0 1px 4px rgba(0, 0, 0, 0.02);
      }
      
      /* 第5个容器 - 浅灰绿色变体5 */
      [data-testid="stContainer"]:nth-of-type(5),
      [data-testid="stContainer"]:nth-of-type(10),
      [data-testid="stContainer"]:nth-of-type(15) {
        background: rgba(237, 243, 240, 0.9);
        border-color: rgba(208, 220, 215, 0.4);
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05),
                    0 1px 4px rgba(0, 0, 0, 0.02);
      }
      
      [data-testid="stContainer"]:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 24px rgba(0, 0, 0, 0.06),
                    0 2px 6px rgba(0, 0, 0, 0.03);
      }
      
      /* 减少列之间的间距 */
      [data-testid="column"] {
        gap: 0.5rem;
      }
      
      /* 标题样式 - 浅色莫兰迪色系 */
      h1 {
        color: #6b6a65;
        font-weight: 700;
        font-size: 2.5rem;
        letter-spacing: -0.02em;
        margin-bottom: 0.5rem;
      }
      
      h2, h3 {
        color: #6b6a65;
        font-weight: 600;
        letter-spacing: -0.01em;
      }
      
      /* 指标卡片 */
      [data-testid="stMetric"] {
        background: rgba(240, 248, 242, 0.8);
        padding: 1rem 1.25rem;
        border-radius: 16px;
        border: 1px solid rgba(210, 225, 215, 0.4);
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04);
        transition: all 0.3s ease;
      }
      [data-testid="stMetric"]:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(0, 0, 0, 0.12);
      }
      [data-testid="stMetricLabel"] {
        color: #7a7872;
        font-weight: 500;
        font-size: 0.875rem;
      }
      [data-testid="stMetricValue"] {
        color: #6b6a65;
        font-weight: 700;
        font-size: 1.5rem;
      }
      
      /* 展开器 */
      [data-testid="stExpander"] {
        border-radius: 16px;
        border: 1px solid rgba(210, 225, 215, 0.4);
        background: rgba(240, 248, 242, 0.8);
        backdrop-filter: blur(10px);
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04);
      }
      
      /* 输入框和文本区域 */
      textarea, input[type="text"], input[type="number"] {
        border-radius: 12px !important;
        border: 1px solid rgba(210, 225, 215, 0.5) !important;
        background: rgba(245, 250, 247, 0.8) !important;
        transition: all 0.3s ease !important;
      }
      textarea:focus, input:focus {
        border-color: rgba(190, 210, 200, 0.6) !important;
        box-shadow: 0 0 0 3px rgba(210, 225, 215, 0.2),
                    0 2px 8px rgba(0, 0, 0, 0.04) !important;
        background: rgba(255, 255, 255, 0.95) !important;
      }
      
      /* 按钮样式 - 浅色莫兰迪色系，柔和高级 */
      .stButton > button {
        border-radius: 12px !important;
        border: 1px solid rgba(210, 208, 205, 0.4) !important;
        background: rgba(240, 238, 235, 0.6) !important;
        color: #6b6a65 !important;
        font-weight: 500 !important;
        font-size: 0.875rem !important;
        padding: 0.6rem 1.2rem !important;
        transition: all 0.3s ease !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04) !important;
      }
      .stButton > button:hover {
        transform: translateY(-1px) !important;
        background: rgba(230, 228, 225, 0.7) !important;
        border-color: rgba(200, 198, 195, 0.5) !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06) !important;
        color: #5a5954 !important;
      }
      .stButton > button:active {
        transform: translateY(0) !important;
      }
      
      /* 主要按钮（primary）- 浅色莫兰迪色系 */
      button[kind="primary"] {
        background: rgba(210, 205, 200, 0.7) !important;
        border-color: rgba(190, 185, 180, 0.5) !important;
        color: #5a5954 !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05) !important;
      }
      button[kind="primary"]:hover {
        background: rgba(200, 195, 190, 0.8) !important;
        border-color: rgba(180, 175, 170, 0.6) !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08) !important;
      }
      
      /* 日期选择器 */
      [data-baseweb="input"] {
        border-radius: 12px !important;
        border: 2px solid rgba(148, 163, 184, 0.3) !important;
        background: rgba(255, 255, 255, 0.9) !important;
      }
      
      /* 任务进度条样式 - 可拖动滑块 */
      .task-progress-container {
        margin-top: 0.5rem;
        margin-bottom: 0.75rem;
      }
      
      /* 滑块样式 */
      .task-progress-slider {
        -webkit-appearance: none !important;
        appearance: none !important;
        width: 100% !important;
        height: 20px !important;
        margin: 0 !important;
        padding: 0 !important;
        border-radius: 999px;
        background: transparent !important;
        outline: none !important;
        cursor: pointer !important;
        pointer-events: auto !important;
        position: relative !important;
        z-index: 10 !important;
      }
      
      .task-progress-slider::-webkit-slider-runnable-track {
        width: 100% !important;
        height: 10px !important;
        border-radius: 999px !important;
        background: linear-gradient(90deg, rgba(226, 232, 240, 0.6), rgba(241, 245, 249, 0.8)) !important;
        box-shadow: inset 0 2px 4px rgba(15, 23, 42, 0.08) !important;
        border: none !important;
      }
      
      .task-progress-slider::-webkit-slider-thumb {
        -webkit-appearance: none !important;
        appearance: none !important;
        width: 20px !important;
        height: 20px !important;
        border-radius: 50% !important;
        background: rgba(180, 178, 190, 0.9) !important;
        cursor: grab !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1) !important;
        transition: all 0.2s ease !important;
        border: 2px solid white !important;
        margin-top: -5px !important;
        position: relative !important;
        z-index: 11 !important;
      }
      
      .task-progress-slider::-webkit-slider-thumb:hover {
        transform: scale(1.1) !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15) !important;
      }
      
      .task-progress-slider::-webkit-slider-thumb:active {
        cursor: grabbing !important;
        transform: scale(1.15) !important;
      }
      
      .task-progress-slider::-moz-range-track {
        width: 100% !important;
        height: 10px !important;
        border-radius: 999px !important;
        background: linear-gradient(90deg, rgba(226, 232, 240, 0.6), rgba(241, 245, 249, 0.8)) !important;
        box-shadow: inset 0 2px 4px rgba(15, 23, 42, 0.08) !important;
        border: none !important;
      }
      
      .task-progress-slider::-moz-range-thumb {
        width: 20px !important;
        height: 20px !important;
        border-radius: 50% !important;
        background: rgba(180, 178, 190, 0.9) !important;
        cursor: grab !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1) !important;
        transition: all 0.2s ease !important;
        border: 2px solid white !important;
        position: relative !important;
        z-index: 11 !important;
      }
      
      .task-progress-slider::-moz-range-thumb:hover {
        transform: scale(1.1) !important;
        box-shadow: 0 4px 12px rgba(102, 126, 234, 0.6) !important;
      }
      
      .task-progress-slider::-moz-range-thumb:active {
        cursor: grabbing !important;
        transform: scale(1.15) !important;
      }
      
      .task-progress-slider:disabled {
        cursor: not-allowed !important;
        opacity: 0.6 !important;
      }
      
      .task-progress-slider:disabled::-webkit-slider-thumb {
        cursor: not-allowed !important;
        opacity: 0.6 !important;
      }
      
      .task-progress-slider:disabled::-moz-range-thumb {
        cursor: not-allowed !important;
        opacity: 0.6 !important;
      }
      
      /* 进度填充覆盖层 */
      .task-progress-fill-overlay {
        height: 10px;
        border-radius: 999px;
        transition: width 0.3s ease;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
        pointer-events: none;
      }
      
      .task-progress-fill-overlay.done {
        background: rgba(180, 195, 175, 0.8);
      }
      
      .task-progress-fill-overlay.pending {
        background: rgba(180, 178, 190, 0.8);
      }
      
      .task-progress-fill-overlay.high {
        background: rgba(200, 180, 175, 0.8);
      }
      
      .task-progress-text {
        font-size: 0.7rem;
        color: #7a7872;
        margin-top: 0.3rem;
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-weight: 500;
      }
      
      /* 任务卡片样式 */
      .task-card {
        background: rgba(255, 255, 255, 0.9);
        border-radius: 16px;
        padding: 1rem;
        margin-bottom: 0.75rem;
        border: 1px solid rgba(148, 163, 184, 0.2);
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
        transition: all 0.3s ease;
      }
      .task-card:hover {
        transform: translateX(4px);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
        border-color: rgba(102, 126, 234, 0.3);
      }
      
      /* 提示文本 */
      .stCaption {
        color: #7a7872;
        font-weight: 500;
      }
      
      /* 警告和提示框 */
      .stAlert {
        border-radius: 12px;
        border: none;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
      }
      
      /* 隐藏 Streamlit 默认菜单 */
      #MainMenu { visibility: hidden; }
      footer { visibility: hidden; }
      header { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------- 顶部：实时时钟和番茄时间钟 -----------
# 在时钟前添加负margin的空div来移除上方空白
st.markdown('<div style="margin-top: -12rem; height: 0; padding: 0; line-height: 0;"></div>', unsafe_allow_html=True)

# 添加CSS确保列顶端对齐并移除顶部空白
st.markdown("""
<style>
/* 直接针对第一个包含columns的垂直块 - 最激进的方式 */
div[data-testid="stVerticalBlock"]:has(div[data-testid="column"]):first-of-type {
    margin-top: -12rem !important;
    padding-top: 0 !important;
    margin-bottom: 0 !important;
}

/* 移除时钟区域上方的所有空白 - 更激进 */
div[data-testid="stVerticalBlock"]:has([id*="sp-time"]) {
    padding-top: 0 !important;
    margin-top: -10rem !important;
}

div[data-testid="stVerticalBlock"]:has([id*="pomo-display"]) {
    padding-top: 0 !important;
    margin-top: -10rem !important;
}

/* 移除包含时钟列的垂直块的上方空白 - 更激进 */
div[data-testid="stVerticalBlock"]:has(> div[data-testid="column"]:has([id*="sp-time"])) {
    margin-top: -10rem !important;
    padding-top: 0 !important;
}

div[data-testid="stVerticalBlock"]:has(> div[data-testid="column"]:has([id*="pomo-display"])) {
    margin-top: -10rem !important;
    padding-top: 0 !important;
}

/* 移除所有包含columns的垂直块的上方空白 */
div[data-testid="stVerticalBlock"]:has(div[data-testid="column"]) {
    margin-top: -8rem !important;
    padding-top: 0 !important;
}

/* 让两个时钟列顶端对齐，并确保底部对齐 */
div[data-testid="column"] {
    display: flex !important;
    flex-direction: column !important;
    justify-content: flex-start !important;
    align-items: stretch !important;
}
/* 确保两个时钟列的顶部对齐 */
div[data-testid="column"]:first-child,
div[data-testid="column"]:last-child {
    padding-top: 0 !important;
    margin-top: 0 !important;
    align-items: flex-start !important;
}
/* 确保两列内的第一个容器顶部对齐 */
div[data-testid="column"] > div[style*="display: flex"]:first-child {
    margin-top: 0 !important;
    padding-top: 0 !important;
}
/* 确保两列内的第一个元素（时钟和日程管理容器）顶部对齐 */
div[data-testid="column"]:first-child > div[style*="display: flex"]:first-child > iframe,
div[data-testid="column"]:last-child > div[style*="display: flex"]:first-child > div[data-testid="stVerticalBlock"]:first-child {
    margin-top: 0 !important;
    padding-top: 0 !important;
}
/* 移除st.container的默认顶部间距 */
div[data-testid="column"] > div[style*="display: flex"]:first-child > div[data-testid="stVerticalBlock"]:first-child {
    margin-top: 0 !important;
    padding-top: 0 !important;
}
/* 移除components.html iframe的默认间距 */
div[data-testid="column"]:first-child iframe {
    margin-top: 0 !important;
    padding-top: 0 !important;
    display: block !important;
}
/* 确保两列的第一个实际内容元素（时钟iframe和日程管理容器）顶部对齐 */
div[data-testid="column"]:first-child > div[style*="display: flex"]:first-child > div:first-child,
div[data-testid="column"]:last-child > div[style*="display: flex"]:first-child > div:first-child {
    margin-top: 0 !important;
    padding-top: 0 !important;
}
/* 确保st.container的顶部与时钟iframe顶部对齐 */
div[data-testid="column"]:last-child > div[style*="display: flex"]:first-child > div[data-testid="stVerticalBlock"]:first-child > div[data-testid="element-container"]:first-child {
    margin-top: 0 !important;
    padding-top: 0 !important;
}
/* 强制两列的第一个子元素顶部对齐 */
div[data-testid="column"]:first-child > *:first-child,
div[data-testid="column"]:last-child > *:first-child {
    margin-top: 0 !important;
    padding-top: 0 !important;
}
/* 确保时钟iframe和日程管理容器顶部完全对齐 - 最精确的规则 */
div[data-testid="column"]:first-child iframe[title*="streamlit"],
div[data-testid="column"]:last-child div[data-testid="stVerticalBlock"]:first-child {
    margin-top: 0 !important;
    padding-top: 0 !important;
    vertical-align: top !important;
}
/* 移除所有可能影响对齐的间距 */
div[data-testid="column"]:first-child > div:first-child,
div[data-testid="column"]:last-child > div:first-child {
    margin-top: 0 !important;
    padding-top: 0 !important;
}
/* 确保包含时钟和提醒事项的列底部对齐 */
div[data-testid="stVerticalBlock"]:has(> div[data-testid="column"]) {
    display: flex !important;
    align-items: stretch !important;
}
/* 确保列内的flex容器填充高度 */
div[data-testid="column"] > div[style*="display: flex"] {
    min-height: 100% !important;
}
</style>
""", unsafe_allow_html=True)

def _utc_iso_to_local_hour_minute(utc_iso: str):
    """将存储的 UTC ISO 转为本地 (hour, minute)，用于时间轴块的位置与显示。"""
    if not utc_iso or len(utc_iso) < 16:
        return (0, 0)
    try:
        s = utc_iso.replace("Z", "+00:00").strip()
        dt = datetime.fromisoformat(s[:26] if "+" in s else s[:19])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return (local.hour, local.minute)
    except Exception:
        return (int(utc_iso[11:13]) if len(utc_iso) >= 13 else 0, int(utc_iso[14:16]) if len(utc_iso) >= 16 else 0)


# 时间轴渲染函数（必须在调用之前定义）
def _task_to_timeline_blocks(tasks: list, day_start_h: int, day_end_h: int) -> list:
    """将任务转为纵向时间轴块：top_px, height_px, color, title, time_str, left, width。
    支持多任务并排显示，避免重叠。任务时间存 UTC，展示时转为本地时间。"""
    day_start_min = day_start_h * 60
    day_end_min = day_end_h * 60
    blocks = []
    
    # 第一步：解析所有任务（UTC -> 本地 小时/分钟）
    for t in sorted(tasks, key=lambda x: x.start_time):
        try:
            sh, sm = _utc_iso_to_local_hour_minute(t.start_time or "")
            eh, em = _utc_iso_to_local_hour_minute(t.end_time or "")
        except (ValueError, TypeError):
            continue

        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        if end_min <= start_min:
            continue

        # 裁剪到展示窗口（24小时显示）
        clipped_start = max(start_min, day_start_min)
        clipped_end = min(end_min, day_end_min)
        if clipped_end <= clipped_start:
            continue

        top_px = clipped_start - day_start_min
        height_px = max(18, clipped_end - clipped_start)

        if t.status == "done":
            color = "#d0ddd0"  # 浅色莫兰迪灰绿色
        elif t.priority == "high":
            color = "#e0d5d0"  # 浅色莫兰迪灰红色
        else:
            color = "#d0ced8"  # 浅色莫兰迪灰紫色

        st_str = f"{sh:02d}:{sm:02d}"
        et_str = f"{eh:02d}:{em:02d}"
        blocks.append(
            {
                "top": float(top_px),
                "height": float(height_px),
                "start_min": clipped_start,
                "end_min": clipped_end,
                "color": color,
                "title": t.title or "",
                "time_str": f"{st_str}–{et_str}",
                "left": 0.0,  # 待计算
                "width": 100.0,  # 待计算
            }
        )
    
    # 第二步：检测重叠并分配列（使用区间调度算法）
    if not blocks:
        return blocks
    
    # 按开始时间排序，如果开始时间相同，按结束时间排序
    blocks.sort(key=lambda x: (x["start_min"], x["end_min"]))
    
    # 为每个块分配列号
    for i, block in enumerate(blocks):
        # 查找所有与当前块重叠的已处理块
        used_columns = set()
        for j in range(i):
            other_block = blocks[j]
            # 检查是否重叠：两个区间有交集
            if not (block["end_min"] <= other_block["start_min"] or block["start_min"] >= other_block["end_min"]):
                if "column" in other_block:
                    used_columns.add(other_block["column"])
        
        # 分配第一个可用的列号（从0开始）
        column = 0
        while column in used_columns:
            column += 1
        block["column"] = column
    
    # 第三步：计算最大列数，并分配宽度和位置
    max_columns = max((b.get("column", 0) for b in blocks), default=0) + 1
    if max_columns == 0:
        max_columns = 1
    
    for block in blocks:
        column = block.get("column", 0)
        # 计算宽度：根据总列数平均分配
        block["width"] = 100.0 / max_columns
        # 计算左边距
        block["left"] = column * block["width"]
    
    return blocks


def _render_timeline_blocks(view_d: date, tasks: list, start_h: int, end_h: int) -> str:
    """生成时间轴 HTML：左侧时间刻度，右侧色块。"""
    blocks = _task_to_timeline_blocks(tasks, start_h, end_h)
    total_height = (end_h - start_h) * 60
    diff_h = end_h - start_h
    
    # 计算所有时间块的最大底部位置，确保容器高度足够
    max_bottom = total_height
    if blocks:
        for b in blocks:
            block_bottom = b["top"] + b["height"]
            if block_bottom > max_bottom:
                max_bottom = block_bottom
        # 添加一些额外的底部 padding，确保时间块不被裁剪
        max_bottom += 20
    
    hour_labels = "".join(
        [
            f'<div class="tl-hour" style="height:{(60 if i < diff_h else 0)}px" title="{start_h + i:02d}:00">{start_h + i:02d}:00</div>'
            for i in range(diff_h + 1)
        ]
    )
    hour_lines = "".join([f'<div class="tl-gridline" style="top:{i*60}px;"></div>' for i in range(0, diff_h + 1)])
    block_divs = "".join(
        [
            (
                f'<div class="tl-block" style="top:{b["top"]:.0f}px;left:calc({b["left"]:.2f}% + 8px);width:calc({b["width"]:.2f}% - 16px);height:{b["height"]:.0f}px;background:{b["color"]};"'
                f' title="{b["time_str"]}">'
                f'  <div class="tl-block-title">{(b["title"] or "")}</div>'
                f'  <div class="tl-block-time">{b["time_str"]}</div>'
                f"</div>"
            )
            for b in blocks
        ]
    )
    # 设置滚动容器的高度（4小时 = 240px，可滚动查看完整24小时）
    scroll_height = 240  # 4小时的高度
    return f"""
    <div class="timeline-wrap" style="font-family:system-ui,sans-serif;font-size:13px;height:{scroll_height}px;overflow:hidden;position:relative;">
      <div class="tl-axis-scroll" style="width:75px;flex-shrink:0;padding-top:2px;height:{scroll_height}px;overflow-y:auto;overflow-x:visible;scrollbar-width:thin;scrollbar-color:rgba(148,163,184,0.3) transparent;">
        <div class="tl-axis" style="width:100%;min-width:75px;">
          {hour_labels}
        </div>
      </div>
      <div class="tl-track-scroll" style="flex:1;position:relative;height:{scroll_height}px;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;scrollbar-color:rgba(148,163,184,0.3) transparent;">
        <div class="tl-track" style="position:relative;height:{max_bottom:.0f}px;min-height:{total_height}px;background:linear-gradient(180deg, rgba(245,250,247,0.95) 0%, rgba(240,248,242,0.9) 50%, rgba(245,250,247,0.95) 100%);border-radius:16px;border:2px solid rgba(210,225,215,0.3);box-shadow:inset 0 2px 8px rgba(0,0,0,0.03), 0 4px 16px rgba(0,0,0,0.05);backdrop-filter:blur(10px);">
          {hour_lines}
          {block_divs}
        </div>
      </div>
    </div>
    <script>
      // 同步滚动：时间轴和任务区域同步滚动
      (function() {{
        const axisScroll = document.querySelector('.tl-axis-scroll');
        const trackScroll = document.querySelector('.tl-track-scroll');
        
        if (axisScroll && trackScroll) {{
          // 时间轴滚动时，同步任务区域
          axisScroll.addEventListener('scroll', function() {{
            trackScroll.scrollTop = axisScroll.scrollTop;
          }});
          
          // 任务区域滚动时，同步时间轴
          trackScroll.addEventListener('scroll', function() {{
            axisScroll.scrollTop = trackScroll.scrollTop;
          }});
        }}
      }})();
    </script>
    <style>
      .timeline-wrap {{ 
        display:flex; 
        gap:8px; 
        align-items:flex-start;
        border-radius: 16px;
        background: rgba(255,255,255,0.1);
        padding: 8px;
      }}
      .tl-axis-scroll {{
        user-select:none;
      }}
      .tl-axis-scroll::-webkit-scrollbar {{
        width: 6px;
      }}
      .tl-axis-scroll::-webkit-scrollbar-track {{
        background: transparent;
      }}
      .tl-axis-scroll::-webkit-scrollbar-thumb {{
        background: rgba(148,163,184,0.3);
        border-radius: 3px;
      }}
      .tl-axis-scroll::-webkit-scrollbar-thumb:hover {{
        background: rgba(148,163,184,0.5);
      }}
      .tl-track-scroll::-webkit-scrollbar {{
        width: 8px;
      }}
      .tl-track-scroll::-webkit-scrollbar-track {{
        background: rgba(248,250,252,0.5);
        border-radius: 4px;
      }}
      .tl-track-scroll::-webkit-scrollbar-thumb {{
        background: rgba(148,163,184,0.4);
        border-radius: 4px;
      }}
      .tl-track-scroll::-webkit-scrollbar-thumb:hover {{
        background: rgba(148,163,184,0.6);
      }}
      .tl-axis {{ user-select:none; }}
      .tl-hour {{
        height:60px;
        color:#7a7872;
        line-height:1.2;
        font-variant-numeric: tabular-nums;
        display:flex;
        align-items:flex-start;
        justify-content:flex-end;
        padding-right:10px;
        padding-left:4px;
        padding-top:4px;
        box-sizing:border-box;
        font-weight: 600;
        font-size: 12px;
        white-space: nowrap;
        overflow: visible;
        min-width: 71px;
        width: 100%;
        text-align: right;
      }}
      .tl-gridline {{
        position:absolute;
        left:0;
        right:0;
        height:1px;
        background: rgba(200,198,195,0.2);
        pointer-events:none;
      }}
      .tl-block {{
        position:absolute;
        border-radius:12px;
        color:#6b6a65;
        padding:8px 10px;
        box-sizing:border-box;
        overflow:hidden;
        box-shadow: 0 2px 8px rgba(0,0,0,0.08),
                    0 1px 3px rgba(0,0,0,0.05),
                    inset 0 1px 0 rgba(255,255,255,0.3);
        border: 1px solid rgba(220,218,214,0.4);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
        cursor: pointer;
        margin: 0 2px;
      }}
      .tl-block:hover {{
        transform: translateY(-2px) scale(1.02);
        box-shadow: 0 4px 16px rgba(0,0,0,0.12),
                    0 2px 6px rgba(0,0,0,0.08),
                    inset 0 1px 0 rgba(255,255,255,0.4);
      }}
      .tl-block-title {{
        font-weight:600;
        font-size:13px;
        line-height:1.3;
        overflow:hidden;
        text-overflow:ellipsis;
        white-space:nowrap;
        color: #6b6a65;
        text-shadow: 0 1px 1px rgba(255,255,255,0.5);
      }}
      .tl-block-time {{
        font-size:10px;
        opacity:0.85;
        line-height:1.3;
        margin-top:3px;
        font-variant-numeric: tabular-nums;
        font-weight: 500;
        color: #6b6a65;
        text-shadow: 0 1px 1px rgba(255,255,255,0.5);
      }}
    </style>
    """

clock_col, pomodoro_col = st.columns([1, 1])
with clock_col:
    st.markdown('<div style="display: flex; flex-direction: column; justify-content: flex-start; align-items: stretch; margin-top: 0; padding-top: 0;">', unsafe_allow_html=True)
    components.html(
        """
        <div style="
          width: 100%;
          max-width: 100%;
          box-sizing: border-box;
          margin-left: auto;
          padding: 10px 14px;
          border: 1px solid rgba(220, 218, 214, 0.5);
          border-radius: 20px;
          font-family: 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          line-height: 1.3;
          text-align: center;
          background: rgba(245, 250, 247, 0.9);
          backdrop-filter: blur(20px);
          box-shadow: 0 4px 20px rgba(0, 0, 0, 0.04),
                      0 1px 4px rgba(0, 0, 0, 0.02);
          display: flex;
          flex-direction: column;
          justify-content: center;
          align-items: center;
          transition: transform 0.3s ease, box-shadow 0.3s ease;
          overflow: visible;
          height: 140px;
        " onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 6px 24px rgba(0, 0, 0, 0.08), 0 2px 6px rgba(0, 0, 0, 0.04)';"
          onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='0 4px 20px rgba(0, 0, 0, 0.04), 0 1px 4px rgba(0, 0, 0, 0.02)';">
          <div id="sp-date" style="font-size:13px; color: #7a7872; font-weight: 600; letter-spacing: 0.5px; white-space: nowrap; overflow: visible;"></div>
          <div id="sp-time" style="font-size:24px; font-weight:800; color: #6b6a65; margin-top: 6px; letter-spacing: -0.5px; white-space: nowrap; overflow: visible;"></div>
        </div>
        <script>
          function pad(n){ return String(n).padStart(2,'0'); }
          function tick(){
            const d = new Date();
            const yyyy = d.getFullYear();
            const mm = pad(d.getMonth()+1);
            const dd = pad(d.getDate());
            const hh = pad(d.getHours());
            const mi = pad(d.getMinutes());
            const ss = pad(d.getSeconds());
            const days = ['周日','周一','周二','周三','周四','周五','周六'];
            document.getElementById('sp-date').textContent = `${yyyy}-${mm}-${dd}  ${days[d.getDay()]}`;
            document.getElementById('sp-time').textContent = `${hh}:${mi}:${ss}`;
          }
          tick();
          setInterval(tick, 1000);
        </script>
""",
        height=140,
    )
    st.markdown('</div>', unsafe_allow_html=True)
    
    # ---------- 番茄时钟（左侧列，实时时钟下方） ----------
    st.markdown("<div style='margin-top: 1rem;'></div>", unsafe_allow_html=True)
    
    # 初始化番茄时间钟状态
    if "pomodoro_time" not in st.session_state:
        st.session_state.pomodoro_time = 25 * 60  # 默认25分钟
    if "pomodoro_running" not in st.session_state:
        st.session_state.pomodoro_running = False
    if "pomodoro_remaining" not in st.session_state:
        st.session_state.pomodoro_remaining = st.session_state.pomodoro_time
    if "pomodoro_start_time" not in st.session_state:
        st.session_state.pomodoro_start_time = None
    if "pomodoro_custom_minutes" not in st.session_state:
        st.session_state.pomodoro_custom_minutes = 25  # 默认自定义时长为25分钟
    if "pomodoro_use_custom" not in st.session_state:
        st.session_state.pomodoro_use_custom = False  # False表示使用预设时间，True表示使用自定义时间
    
    # 处理番茄时间钟操作（通过URL参数）
    if "pomo_action" in st.query_params:
        action = st.query_params.get("pomo_action")
        if action == "set_time":
            minutes = int(st.query_params.get("pomo_minutes", 25))
            st.session_state.pomodoro_time = minutes * 60
            st.session_state.pomodoro_remaining = minutes * 60
            st.session_state.pomodoro_running = False
            st.session_state.pomodoro_start_time = None
            st.query_params.clear()
            st.rerun()
        elif action == "start":
            st.session_state.pomodoro_running = True
            st.session_state.pomodoro_start_time = datetime.now().timestamp() * 1000
            st.query_params.clear()
            st.rerun()
        elif action == "pause":
            if st.session_state.pomodoro_start_time:
                elapsed = int((datetime.now().timestamp() * 1000 - st.session_state.pomodoro_start_time) / 1000)
                st.session_state.pomodoro_remaining = max(0, st.session_state.pomodoro_remaining - elapsed)
            st.session_state.pomodoro_running = False
            st.session_state.pomodoro_start_time = None
            st.query_params.clear()
            st.rerun()
        elif action == "reset":
            st.session_state.pomodoro_remaining = st.session_state.pomodoro_time
            st.session_state.pomodoro_running = False
            st.session_state.pomodoro_start_time = None
            st.query_params.clear()
            st.rerun()
    
    # 如果正在运行，更新剩余时间
    if st.session_state.pomodoro_running and st.session_state.pomodoro_start_time:
        elapsed = int((datetime.now().timestamp() * 1000 - st.session_state.pomodoro_start_time) / 1000)
        st.session_state.pomodoro_remaining = max(0, st.session_state.pomodoro_remaining - elapsed)
        # 如果时间到了，停止计时
        if st.session_state.pomodoro_remaining <= 0:
            st.session_state.pomodoro_running = False
            st.session_state.pomodoro_start_time = None
            st.session_state.pomodoro_remaining = 0
    
    # 番茄时间钟显示和控制
    pomodoro_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{
                    margin: 0;
                    padding: 0;
                    font-family: 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                }}
                .pomo-preset-btn {{
                    padding: 3px 6px;
                    margin: 0;
                    border: 1px solid rgba(180, 178, 175, 0.4);
                    border-radius: 6px;
                    background: rgba(220, 218, 214, 0.5);
                    color: #6b6a65;
                    font-size: 9px;
                    font-weight: 500;
                    cursor: pointer;
                    transition: all 0.2s;
                    flex: 1;
                    max-width: 50px;
                }}
                .pomo-preset-btn:hover {{
                    background: rgba(230, 228, 225, 0.6);
                }}
                .pomo-control-btn {{
                    padding: 4px 8px;
                    margin: 0;
                    border: 1px solid rgba(210, 208, 205, 0.4);
                    border-radius: 6px;
                    background: rgba(230, 228, 225, 0.6);
                    color: #6b6a65;
                    font-size: 10px;
                    font-weight: 500;
                    cursor: pointer;
                    transition: all 0.2s;
                    flex: 1;
                }}
                .pomo-control-btn:hover {{
                    background: rgba(220, 218, 215, 0.7);
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
                }}
                .pomo-control-btn.primary {{
                    background: rgba(210, 205, 200, 0.7);
                    color: #5a5954;
                }}
            </style>
        </head>
        <body>
            <div style="
                width: 100%;
                box-sizing: border-box;
                padding: 10px 14px;
                border: 1px solid rgba(220, 218, 214, 0.5);
                border-radius: 20px;
                text-align: center;
                background: rgba(245, 250, 247, 0.9);
                backdrop-filter: blur(20px);
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.04),
                            0 1px 4px rgba(0, 0, 0, 0.02);
                transition: transform 0.3s ease, box-shadow 0.3s ease;
                display: flex;
                flex-direction: column;
                justify-content: center;
                height: 100px;
            " onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 6px 24px rgba(0, 0, 0, 0.08), 0 2px 6px rgba(0, 0, 0, 0.04)';"
              onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='0 4px 20px rgba(0, 0, 0, 0.04), 0 1px 4px rgba(0, 0, 0, 0.02)';">
                <div>
                    <div style="font-size: 11px; color: #7a7872; font-weight: 600; margin-bottom: 3px;">🍅 番茄时间</div>
                    <div id="pomo-display" style="font-size: 20px; font-weight: 800; color: #6b6a65; letter-spacing: -0.5px;">
                        {st.session_state.pomodoro_remaining // 60:02d}:{st.session_state.pomodoro_remaining % 60:02d}
                    </div>
                    <div id="pomo-status" style="font-size: 10px; color: #7a7872; margin-top: 3px; margin-bottom: 6px;">
                        {('运行中' if st.session_state.pomodoro_running else '已暂停')}
                    </div>
                </div>
            </div>
            <script>
                (function() {{
                    const isRunning = {str(st.session_state.pomodoro_running).lower()};
                    const remaining = {st.session_state.pomodoro_remaining};
                    const totalTime = {st.session_state.pomodoro_time};
                    let currentRemaining = remaining;
                    let running = isRunning;
                    let startTime = null;
                    
                    if (running) {{
                        // 如果正在运行，计算已过去的时间
                        const startTimestamp = {st.session_state.pomodoro_start_time if st.session_state.pomodoro_start_time else 'Date.now()'};
                        const elapsed = Math.floor((Date.now() - startTimestamp) / 1000);
                        currentRemaining = Math.max(0, remaining - elapsed);
                    }}
                    
                    function formatTime(seconds) {{
                        const mins = Math.floor(seconds / 60);
                        const secs = seconds % 60;
                        return `${{String(mins).padStart(2, '0')}}:${{String(secs).padStart(2, '0')}}`;
                    }}
                    
                    function updateDisplay() {{
                        const displayEl = document.getElementById('pomo-display');
                        const statusEl = document.getElementById('pomo-status');
                        
                        if (displayEl) {{
                            displayEl.textContent = formatTime(currentRemaining);
                            
                            // 时间快到了，改变颜色
                            if (currentRemaining <= 60 && currentRemaining > 0) {{
                                displayEl.style.color = '#ef4444';
                            }} else if (currentRemaining === 0) {{
                                displayEl.style.color = '#dc2626';
                            }} else {{
                                displayEl.style.color = '#6b6a65';
                            }}
                        }}
                        
                        if (statusEl) {{
                            statusEl.textContent = running ? '运行中' : '已暂停';
                        }}
                    }}
                    
                    function checkNotification() {{
                        if (currentRemaining === 0 && running) {{
                            running = false;
                            
                            // 浏览器通知
                            if ('Notification' in window && Notification.permission === 'granted') {{
                                new Notification('🍅 番茄时间到！', {{
                                    body: '时间到了，该休息一下了！',
                                    icon: '🍅',
                                    tag: 'pomodoro',
                                    requireInteraction: true
                                }});
                            }} else if ('Notification' in window && Notification.permission !== 'denied') {{
                                Notification.requestPermission().then(permission => {{
                                    if (permission === 'granted') {{
                                        new Notification('🍅 番茄时间到！', {{
                                            body: '时间到了，该休息一下了！',
                                            icon: '🍅',
                                            tag: 'pomodoro',
                                            requireInteraction: true
                                        }});
                                    }}
                                }});
                            }}
                            
                            // 播放提示音（使用Web Audio API生成提示音）
                            try {{
                                const audioContext = new (window.AudioContext || window.webkitAudioContext)();
                                const oscillator = audioContext.createOscillator();
                                const gainNode = audioContext.createGain();
                                
                                oscillator.connect(gainNode);
                                gainNode.connect(audioContext.destination);
                                
                                oscillator.frequency.value = 800;
                                oscillator.type = 'sine';
                                
                                gainNode.gain.setValueAtTime(0.3, audioContext.currentTime);
                                gainNode.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.5);
                                
                                oscillator.start(audioContext.currentTime);
                                oscillator.stop(audioContext.currentTime + 0.5);
                                
                                // 播放3次
                                setTimeout(() => {{
                                    const osc2 = audioContext.createOscillator();
                                    const gain2 = audioContext.createGain();
                                    osc2.connect(gain2);
                                    gain2.connect(audioContext.destination);
                                    osc2.frequency.value = 800;
                                    osc2.type = 'sine';
                                    gain2.gain.setValueAtTime(0.3, audioContext.currentTime);
                                    gain2.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.5);
                                    osc2.start(audioContext.currentTime);
                                    osc2.stop(audioContext.currentTime + 0.5);
                                }}, 600);
                                
                                setTimeout(() => {{
                                    const osc3 = audioContext.createOscillator();
                                    const gain3 = audioContext.createGain();
                                    osc3.connect(gain3);
                                    gain3.connect(audioContext.destination);
                                    osc3.frequency.value = 800;
                                    osc3.type = 'sine';
                                    gain3.gain.setValueAtTime(0.3, audioContext.currentTime);
                                    gain3.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.5);
                                    osc3.start(audioContext.currentTime);
                                    osc3.stop(audioContext.currentTime + 0.5);
                                }}, 1200);
                            }} catch (e) {{
                                console.log('音频播放失败:', e);
                            }}
                            
                            // 闪烁提示
                            const displayEl = document.getElementById('pomo-display');
                            if (displayEl) {{
                                let flashCount = 0;
                                const flashInterval = setInterval(() => {{
                                    displayEl.style.opacity = displayEl.style.opacity === '0.5' ? '1' : '0.5';
                                    flashCount++;
                                    if (flashCount >= 10) {{
                                        clearInterval(flashInterval);
                                        displayEl.style.opacity = '1';
                                    }}
                                }}, 200);
                            }}
                        }}
                    }}
                    
                    // 初始化显示
                    updateDisplay();
                    
                    // 如果正在运行，每秒更新
                    if (running && currentRemaining > 0) {{
                        const interval = setInterval(() => {{
                            if (currentRemaining > 0) {{
                                currentRemaining--;
                                updateDisplay();
                                checkNotification();
                            }} else {{
                                clearInterval(interval);
                            }}
                        }}, 1000);
                    }}
                    
                    // 按钮控制函数 - 使用 postMessage 通信
                    function setTime(minutes) {{
                        console.log('设置时间:', minutes);
                        // 方法1: 尝试使用 postMessage
                        try {{
                            if (window.parent && window.parent !== window) {{
                                window.parent.postMessage({{
                                    type: 'pomo_action',
                                    action: 'set_time',
                                    minutes: minutes
                                }}, '*');
                            }}
                        }} catch (e) {{
                            console.log('postMessage 失败:', e);
                        }}
                        
                        // 方法2: 直接修改 URL（备用方案）
                        try {{
                            let targetWindow = window;
                            // 尝试访问父窗口
                            try {{
                                if (window.top && window.top !== window) {{
                                    targetWindow = window.top;
                                }} else if (window.parent && window.parent !== window) {{
                                    targetWindow = window.parent;
                                }}
                            }} catch (e) {{
                                // 跨域限制，使用当前窗口
                            }}
                            
                            const currentUrl = targetWindow.location.href;
                            const url = new URL(currentUrl);
                            url.searchParams.set('pomo_action', 'set_time');
                            url.searchParams.set('pomo_minutes', minutes.toString());
                            
                            // 立即跳转
                            targetWindow.location.href = url.toString();
                        }} catch (e) {{
                            console.error('设置时间失败:', e);
                            // 最后的备用方案
                            try {{
                                const currentUrl = window.location.href;
                                const baseUrl = currentUrl.split('?')[0];
                                window.location.href = baseUrl + '?pomo_action=set_time&pomo_minutes=' + minutes;
                            }} catch (e2) {{
                                console.error('完全失败:', e2);
                            }}
                        }}
                    }}
                    
                    function toggleTimer() {{
                        let targetWindow = window;
                        try {{
                            if (window.top && window.top !== window) {{
                                targetWindow = window.top;
                            }} else if (window.parent && window.parent !== window) {{
                                targetWindow = window.parent;
                            }}
                        }} catch (e) {{
                            targetWindow = window;
                        }}
                        
                        try {{
                            const currentUrl = targetWindow.location.href;
                            const url = new URL(currentUrl);
                            url.searchParams.set('pomo_action', running ? 'pause' : 'start');
                            targetWindow.location.href = url.toString();
                        }} catch (e) {{
                            try {{
                                const currentUrl = targetWindow.location.href;
                                const baseUrl = currentUrl.split('?')[0];
                                const separator = baseUrl.includes('?') ? '&' : '?';
                                targetWindow.location.href = baseUrl + separator + 'pomo_action=' + (running ? 'pause' : 'start');
                            }} catch (e2) {{
                                console.error('无法切换计时器:', e2);
                            }}
                        }}
                    }}
                    
                    function resetTimer() {{
                        let targetWindow = window;
                        try {{
                            if (window.top && window.top !== window) {{
                                targetWindow = window.top;
                            }} else if (window.parent && window.parent !== window) {{
                                targetWindow = window.parent;
                            }}
                        }} catch (e) {{
                            targetWindow = window;
                        }}
                        
                        try {{
                            const currentUrl = targetWindow.location.href;
                            const url = new URL(currentUrl);
                            url.searchParams.set('pomo_action', 'reset');
                            targetWindow.location.href = url.toString();
                        }} catch (e) {{
                            try {{
                                const currentUrl = targetWindow.location.href;
                                const baseUrl = currentUrl.split('?')[0];
                                const separator = baseUrl.includes('?') ? '&' : '?';
                                targetWindow.location.href = baseUrl + separator + 'pomo_action=reset';
                            }} catch (e2) {{
                                console.error('无法重置计时器:', e2);
                            }}
                        }}
                    }}
                }})();
            </script>
        </body>
        </html>
    """
    # 使用容器包装，统一高度并顶端对齐
    st.markdown('<div style="display: flex; flex-direction: column; justify-content: flex-start; align-items: stretch;">', unsafe_allow_html=True)
    components.html(pomodoro_html, height=100, scrolling=False)
 
    # 番茄钟控制区（仅作用于左侧番茄钟，避免影响全局按钮）
    st.markdown(
        """
        <div class="pomo-controls">
        <style>
          .pomo-controls button {
            padding: 0.02rem 0.24rem !important;
            font-size: 0.58rem !important;
            min-height: 18px !important;
            height: 18px !important;
            line-height: 1.0 !important;
            border-radius: 8px !important;
          }
          .pomo-controls div[data-testid="stButton"] > button {
            min-height: 18px !important;
            height: 18px !important;
          }
          .pomo-controls div[data-testid="column"] button {
            margin: 0.05rem 0 !important;
          }
          /* 缩小滑块标签与组件间距 */
          .pomo-controls [data-testid="stSlider"] label {
            font-size: 0.66rem !important;
            margin-bottom: 0.05rem !important;
          }
          .pomo-controls [data-testid="stSlider"] {
            padding-top: 0.08rem !important;
            padding-bottom: 0.08rem !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )
    
    # 番茄钟控制区：紧凑铺满（不留左右空白）
    # 预设时间按钮 - 一行显示（3列布局）
    preset_col1, preset_col2, preset_col3 = st.columns(3)

    # 确定当前选中的预设按钮（用于高亮显示）
    current_preset = None
    if not st.session_state.pomodoro_use_custom:
        current_minutes = st.session_state.pomodoro_time // 60
        if current_minutes == 25:
            current_preset = 25
        elif current_minutes == 5:
            current_preset = 5
        elif current_minutes == 15:
            current_preset = 15

    with preset_col1:
        button_type = "primary" if current_preset == 25 else "secondary"
        if st.button("25分", key="pomo_25", use_container_width=True, type=button_type):
            st.session_state.pomodoro_time = 25 * 60
            st.session_state.pomodoro_remaining = 25 * 60
            st.session_state.pomodoro_running = False
            st.session_state.pomodoro_start_time = None
            st.session_state.pomodoro_use_custom = False
            st.rerun()
    with preset_col2:
        button_type = "primary" if current_preset == 5 else "secondary"
        if st.button("5分", key="pomo_5", use_container_width=True, type=button_type):
            st.session_state.pomodoro_time = 5 * 60
            st.session_state.pomodoro_remaining = 5 * 60
            st.session_state.pomodoro_running = False
            st.session_state.pomodoro_start_time = None
            st.session_state.pomodoro_use_custom = False
            st.rerun()
    with preset_col3:
        button_type = "primary" if current_preset == 15 else "secondary"
        if st.button("15分", key="pomo_15", use_container_width=True, type=button_type):
            st.session_state.pomodoro_time = 15 * 60
            st.session_state.pomodoro_remaining = 15 * 60
            st.session_state.pomodoro_running = False
            st.session_state.pomodoro_start_time = None
            st.session_state.pomodoro_use_custom = False
            st.rerun()

    # 自定义时长一行：标题 + 滑块 + 数值 + 应用（不留左右空白）
    st.markdown("<div style='height: 0.15rem;'></div>", unsafe_allow_html=True)
    custom_label_col, custom_slider_col, custom_value_col, custom_apply_col = st.columns([1.2, 5.2, 1.2, 1.6])
    with custom_label_col:
        st.markdown(
            '<div style="font-size:0.66rem;color:#64748b;font-weight:600;line-height:1.1;margin-top:0.55rem;">自定义</div>',
            unsafe_allow_html=True,
        )
    with custom_slider_col:
        custom_minutes_slider = st.slider(
            "自定义时长",
            min_value=1,
            max_value=120,
            value=st.session_state.pomodoro_custom_minutes,
            step=1,
            key="pomo_custom_slider",
            label_visibility="collapsed",
        )
    with custom_value_col:
        st.markdown(
            f"""
            <div style="
              text-align:center;
              font-size:0.66rem;
              font-weight:700;
              color:{'#6366f1' if st.session_state.pomodoro_use_custom else '#64748b'};
              padding:0.15rem 0.2rem;
              background:{'rgba(99, 102, 241, 0.10)' if st.session_state.pomodoro_use_custom else 'rgba(226, 232, 240, 0.30)'};
              border-radius:6px;
              margin-top:0.45rem;
              line-height:1.1;
            ">
              {custom_minutes_slider}分
            </div>
            """,
            unsafe_allow_html=True,
        )
    with custom_apply_col:
        if st.button(
            "应用",
            key="pomo_apply_custom",
            use_container_width=True,
            type="primary" if st.session_state.pomodoro_use_custom else "secondary",
        ):
            st.session_state.pomodoro_custom_minutes = custom_minutes_slider
            st.session_state.pomodoro_time = custom_minutes_slider * 60
            st.session_state.pomodoro_remaining = custom_minutes_slider * 60
            st.session_state.pomodoro_use_custom = True
            if st.session_state.pomodoro_running:
                st.session_state.pomodoro_running = False
                st.session_state.pomodoro_start_time = None
            st.rerun()

    # 显示当前选择的时长（更紧凑）
    current_minutes_display = st.session_state.pomodoro_time // 60
    st.markdown(
        f"""
        <div style="
          text-align:center;
          font-size:0.64rem;
          color:#64748b;
          padding:0.10rem;
          margin-top:0.15rem;
          margin-bottom:0.15rem;
        ">
          当前：{current_minutes_display} 分钟
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 控制按钮 - 一行显示（紧凑铺满）
    control_col1, control_col2 = st.columns(2)
    with control_col1:
        if st.session_state.pomodoro_running:
            if st.button("⏸️ 暂停", key="pomo_pause", use_container_width=True):
                if st.session_state.pomodoro_start_time:
                    elapsed = int((datetime.now().timestamp() * 1000 - st.session_state.pomodoro_start_time) / 1000)
                    st.session_state.pomodoro_remaining = max(0, st.session_state.pomodoro_remaining - elapsed)
                st.session_state.pomodoro_running = False
                st.session_state.pomodoro_start_time = None
                st.rerun()
        else:
            if st.button("▶️ 开始", key="pomo_start", use_container_width=True, type="primary"):
                st.session_state.pomodoro_running = True
                st.session_state.pomodoro_start_time = datetime.now().timestamp() * 1000
                st.rerun()
    with control_col2:
        if st.button("🔄 重置", key="pomo_reset", use_container_width=True):
            st.session_state.pomodoro_remaining = st.session_state.pomodoro_time
            st.session_state.pomodoro_running = False
            st.session_state.pomodoro_start_time = None
            st.rerun()
 
    # 关闭番茄钟控制区
    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    # 关闭左侧列的flex容器
    st.markdown("<div style='margin-top: 1rem;'></div>", unsafe_allow_html=True)
    render_activity_matrix(365)
    st.markdown('</div>', unsafe_allow_html=True)

with pomodoro_col:
    st.markdown('<div style="display: flex; flex-direction: column; justify-content: flex-start; align-items: stretch; margin-top: 0; padding-top: 0;">', unsafe_allow_html=True)
    # ---------- 日程管理（时钟右侧） ----------
    # 模块1：标题和日期选择 - 紧凑布局
    st.markdown('<style>div[data-testid="column"]:last-child > div[style*="display: flex"]:first-child > div[data-testid="stVerticalBlock"]:first-child { margin-top: 0 !important; padding-top: 0 !important; } div[data-testid="column"]:last-child > div[style*="display: flex"]:first-child > div[data-testid="stVerticalBlock"]:first-child > div[data-testid="element-container"]:first-child { margin-top: 0 !important; padding-top: 0 !important; }</style>', unsafe_allow_html=True)
    with st.container(border=True):
        header_col1, header_col2 = st.columns([2.5, 1])
        with header_col1:
            st.markdown("""
            <div style="margin: 0; padding: 0;">
              <h2 style="margin: 0; color: #6b6a65; font-weight: 700; font-size: 1.1rem;">📅 日程管理</h2>
            </div>
            """, unsafe_allow_html=True)
        with header_col2:
            try:
                view_d = st.date_input("查看日期", value=st.session_state.view_date, key="view_date_input", label_visibility="collapsed")
                st.session_state.view_date = view_d
                all_tasks = get_tasks_for_date(view_d)
            except Exception as e:
                st.error("请先配置 SUPABASE_URL 和 SUPABASE_KEY")
                all_tasks = []
                view_d = date.today()
    
    # 模块2：搜索栏 - 单独一行
    with st.container(border=True):
        search_col1, search_col2 = st.columns([5, 1])
        with search_col1:
            search_keyword = st.text_input(
                "搜索任务",
                value=st.session_state.search_keyword,
                key="search_input",
                placeholder="🔍 搜索任务...",
                label_visibility="collapsed"
            )
            st.session_state.search_keyword = search_keyword
        with search_col2:
            if st.button("清除", use_container_width=True):
                st.session_state.search_keyword = ""
                st.rerun()
    
    # 模块3：任务统计 - 单独一行，4列均匀分布
    with st.container(border=True):
        # 获取统计数据
        try:
            stats = get_task_statistics(view_d)
        except Exception:
            stats = {"total": 0, "done": 0, "pending": 0, "high_priority": 0, "completion_rate": 0.0, "total_hours": 0.0}
        
        # 使用4列均匀布局，确保统计卡片完整显示
        stat_cols = st.columns(4)
        with stat_cols[0]:
            st.metric("总任务", stats["total"] if stats["total"] > 0 else 0)
        with stat_cols[1]:
            st.metric("已完成", stats["done"] if stats["total"] > 0 else 0)
        with stat_cols[2]:
            st.metric("待办", stats["pending"] if stats["total"] > 0 else 0)
        with stat_cols[3]:
            st.metric("总时长", f"{stats['total_hours']}h" if stats["total"] > 0 else "0h")
    
    # 获取任务（支持搜索）
    if search_keyword and search_keyword.strip():
        tasks = search_tasks_by_keyword(view_d, search_keyword.strip())
    else:
        tasks = all_tasks
    
    # 模块4：今日概览面板（利用右侧空闲空间）
    st.markdown("<div style='height: 0.5rem;'></div>", unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("""
        <div style="margin-bottom: 0.75rem;">
          <span style="font-weight: 600; color: #475569; font-size: 0.875rem;">📊 今日概览</span>
        </div>
        """, unsafe_allow_html=True)
        
        # 计算概览数据
        done_tasks = [t for t in tasks if t.status == "done"]
        high_priority_tasks = [t for t in tasks if t.priority == "high" and t.status == "pending"]
        
        # 计算每小时工作时长（基于已完成任务）
        hourly_duration = {}  # {hour: minutes}
        task_duration_by_title = {}  # {title: minutes}
        
        for t in done_tasks:
            try:
                start = datetime.fromisoformat(t.start_time.replace("Z", "+00:00"))
                end = datetime.fromisoformat(t.end_time.replace("Z", "+00:00"))
                duration_minutes = int((end - start).total_seconds() / 60)
                
                # 按任务标题统计时长
                if t.title not in task_duration_by_title:
                    task_duration_by_title[t.title] = 0
                task_duration_by_title[t.title] += duration_minutes
                
                # 按小时统计工作时长
                current = start.replace(minute=0, second=0, microsecond=0)
                while current < end:
                    hour = current.hour
                    # 计算这个小时内的工作时长
                    hour_start = max(current, start)
                    hour_end = min(current + timedelta(hours=1), end)
                    minutes_in_hour = int((hour_end - hour_start).total_seconds() / 60)
                    
                    if hour not in hourly_duration:
                        hourly_duration[hour] = 0
                    hourly_duration[hour] += minutes_in_hour
                    
                    # 移动到下一个小时
                    current = current + timedelta(hours=1)
            except Exception:
                pass
        
        # 计算总时长
        total_minutes = sum(hourly_duration.values())
        total_hours = total_minutes // 60
        total_remaining_minutes = total_minutes % 60
        
        # 每小时工作时长柱状图（纵坐标=分钟，悬停显示数值）
        max_hour_duration = max(hourly_duration.values()) if hourly_duration else 1  # 避免除零错误
        max_height = 120  # 最大高度（像素）
        # 纵坐标刻度：0、中间、最大值（分钟）
        y_ticks = [0]
        if max_hour_duration > 0:
            mid = max_hour_duration // 2
            if mid != 0 and mid != max_hour_duration:
                y_ticks.insert(0, mid)
            y_ticks.insert(0, max_hour_duration)
        y_ticks_str = "".join(
            f'<span style="font-size: 0.65rem; color: #64748b;">{v}分钟</span>' for v in y_ticks
        )
        
        # 柱状图横向滚动：固定内容宽度、单层 overflow，避免闪烁；滚轮横向/Shift+竖向 滚动容器
        bar_col_width = 22
        bar_row_min_width = 24 * bar_col_width
        chart_html = f"""
        <div class="hour-chart-root" style="margin-bottom: 1rem; width: 100%;">
          <div style="font-size: 0.75rem; color: #64748b; margin-bottom: 0.5rem; font-weight: 600;">
            总工作时长：{total_hours}小时{total_remaining_minutes}分钟
          </div>
          <div style="display: flex; flex-direction: row; align-items: stretch; width: 100%; min-width: 0;">
            <div style="display: flex; flex-direction: column; justify-content: space-between; align-items: flex-end; height: {max_height + 40}px; padding-right: 6px; border-right: 1px solid #e2e8f0; min-width: 42px; flex-shrink: 0;">
              {y_ticks_str}
            </div>
            <div class="hour-chart-scroll" style="
              flex: 1; min-width: 0; overflow-x: auto; overflow-y: hidden;
              -webkit-overflow-scrolling: touch; scrollbar-width: thin; contain: layout;
            ">
              <div style="min-width: {bar_row_min_width}px;">
                <div style="display: flex; align-items: flex-end; gap: 2px; height: {max_height + 40}px; min-height: {max_height + 40}px; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px;">
        """
        
        for hour in range(24):
            minutes = hourly_duration.get(hour, 0)
            if max_hour_duration > 0:
                height = max(1, int((minutes / max_hour_duration * max_height)))
            else:
                height = 0
            bar_color = "#6366f1" if minutes > 0 else "#e2e8f0"
            tooltip_text = f"{hour}时: {minutes}分钟"
            
            chart_html += f"""
            <div class="bar-wrap" style="width: {bar_col_width - 2}px; min-width: {bar_col_width - 2}px; flex-shrink: 0; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; position: relative;">
              <span class="bar-tip" style="
                position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
                white-space: nowrap; background: #334155; color: #f1f5f9; padding: 4px 8px;
                border-radius: 4px; font-size: 0.7rem; margin-top: 2px;
                display: none; pointer-events: none; z-index: 10;
              ">{tooltip_text}</span>
              <div class="bar-col" style="
                width: 100%; background: {bar_color}; height: {height}px; min-height: {height}px;
                max-height: {height}px; border-radius: 2px 2px 0 0;
                cursor: {'pointer' if minutes > 0 else 'default'}; opacity: {1 if minutes > 0 else 0};
              " title="{tooltip_text}"></div>
            </div>
            """
        
        # 横坐标 0～24 时：与 24 根柱一一对齐，刻度 0/6/12/18/24 时
        axis_cell_style = f"width: {bar_col_width - 2}px; min-width: {bar_col_width - 2}px; flex-shrink: 0; font-size: 0.6rem; color: #94a3b8; text-align: center; line-height: 16px;"
        axis_labels_html = "".join(
            f'<div style="{axis_cell_style}">{h}时</div>' if h in (0, 6, 12, 18) else (f'<div style="{axis_cell_style}">24时</div>' if h == 23 else f'<div style="{axis_cell_style}"></div>')
            for h in range(24)
        )
        chart_html += f"""
                </div>
                <div style="display: flex; gap: 2px; margin-top: 2px; padding-bottom: 4px;">
                  {axis_labels_html}
                </div>
              </div>
            </div>
          </div>
          <script>
            (function(){{
              var scrollEl = document.querySelector('.hour-chart-scroll');
              if (scrollEl) {{
                scrollEl.addEventListener('wheel', function(e) {{
                  var dx = e.deltaX, dy = e.deltaY;
                  if (dx !== 0) {{
                    scrollEl.scrollLeft += dx;
                    e.preventDefault();
                  }} else if (e.shiftKey && dy !== 0) {{
                    scrollEl.scrollLeft += dy;
                    e.preventDefault();
                  }}
                }}, {{ passive: false }});
              }}
              document.querySelectorAll('.bar-wrap').forEach(function(wrap){{
                wrap.addEventListener('mouseenter', function(){{ var t = this.querySelector('.bar-tip'); if(t) t.style.display = 'block'; }});
                wrap.addEventListener('mouseleave', function(){{ var t = this.querySelector('.bar-tip'); if(t) t.style.display = 'none'; }});
              }});
            }})();
          </script>
        </div>
        """
        # 使用 components.html 渲染HTML，确保正确解析
        components.html(chart_html, height=200, scrolling=False)
        
        # 按任务类别的时长统计（右侧）
        if task_duration_by_title:
            st.markdown("""
            <div style="margin-bottom: 0.5rem;">
              <span style="font-size: 0.75rem; color: #64748b; font-weight: 600;">📋 任务时长统计</span>
            </div>
            """, unsafe_allow_html=True)
            
            # 按时长排序
            sorted_tasks = sorted(task_duration_by_title.items(), key=lambda x: x[1], reverse=True)
            
            for title, minutes in sorted_tasks[:5]:  # 最多显示5个
                hours = minutes // 60
                mins = minutes % 60
                duration_str = f"{hours}小时{mins}分钟" if hours > 0 else f"{mins}分钟"
                
                task_stat_html = f"""
                <div style="
                  background: rgba(99, 102, 241, 0.05);
                  border-radius: 6px;
                  padding: 0.5rem 0.75rem;
                  margin-bottom: 0.4rem;
                  display: flex;
                  justify-content: space-between;
                  align-items: center;
                ">
                  <span style="font-size: 0.75rem; color: #475569; font-weight: 500; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                    {title}
                  </span>
                  <span style="font-size: 0.7rem; color: #64748b; font-weight: 600; margin-left: 0.5rem; white-space: nowrap;">
                    {duration_str}
                  </span>
                </div>
                """
                st.markdown(task_stat_html, unsafe_allow_html=True)

        # 正在执行的任务（放到今日概览卡片内）
        st.markdown("""
        <div style="margin-top: 0.75rem; margin-bottom: 0.35rem;">
          <span style="font-size: 0.75rem; color: #64748b; font-weight: 600;">⚡ 正在执行的任务</span>
        </div>
        """, unsafe_allow_html=True)

        # 获取今天的任务并找出正在计时的任务
        today = date.today()
        today_tasks = get_tasks_for_date(today)

        if "current_executing_task_id" not in st.session_state:
            st.session_state.current_executing_task_id = None

        executing_task = None
        is_timer_running = False

        # 优先使用 session_state 里的任务
        if st.session_state.current_executing_task_id:
            for task in today_tasks:
                if task.id == st.session_state.current_executing_task_id:
                    executing_task = task
                    if task.memo and "timer_start:" in str(task.memo):
                        is_timer_running = True
                    break

        # 否则在所有任务里找 timer_start
        if not executing_task:
            for task in today_tasks:
                if task.memo and "timer_start:" in str(task.memo):
                    executing_task = task
                    is_timer_running = True
                    st.session_state.current_executing_task_id = task.id
                    break

        if executing_task:
            import re

            sh, sm = _utc_iso_to_local_hour_minute(executing_task.start_time or "")
            eh, em = _utc_iso_to_local_hour_minute(executing_task.end_time or "")
            st_str, et_str = f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"

            total_work_minutes = 0
            if executing_task.memo:
                work_match = re.search(r"total_work_minutes:\s*(\d+)", str(executing_task.memo))
                if work_match:
                    total_work_minutes = int(work_match.group(1))

            current_elapsed_minutes = 0
            start_time_iso = ""
            if executing_task.memo:
                start_match = re.search(r"timer_start:\s*([^|]+)", str(executing_task.memo))
                if start_match:
                    start_time_iso = start_match.group(1).strip()

            if is_timer_running and start_time_iso:
                try:
                    start_time = datetime.fromisoformat(start_time_iso.replace("Z", "+00:00"))
                    if start_time.tzinfo:
                        start_time = start_time.astimezone().replace(tzinfo=None)
                    now_dt = datetime.now()
                    current_elapsed_minutes = int((now_dt - start_time).total_seconds() / 60)
                except Exception:
                    current_elapsed_minutes = 0

            total_display_minutes = total_work_minutes + current_elapsed_minutes
            work_time_str = (
                f"{total_display_minutes // 60}小时{total_display_minutes % 60}分钟"
                if total_display_minutes > 0
                else "0分钟"
            )
            timer_status = "⏱️ 计时中" if is_timer_running else "⏸️ 已暂停"

            if is_timer_running and start_time_iso:
                # 计时中：用 JS 实时刷新累计时间（卡片更紧凑）
                timer_html = f"""
                <!DOCTYPE html>
                <html>
                <head>
                  <style>
                    body {{ margin:0; padding:0; font-family: system-ui, -apple-system, sans-serif; }}
                  </style>
                </head>
                <body>
                  <div style="
                    background: rgba(240, 248, 242, 0.95);
                    border: 1px solid rgba(221, 214, 254, 0.35);
                    border-radius: 10px;
                    padding: 0.65rem 0.75rem;
                    box-shadow: 0 1px 6px rgba(0, 0, 0, 0.04);
                  ">
                    <div style="display:flex; align-items:flex-start; gap:0.55rem;">
                      <span style="font-size:1.05rem; line-height:1;">⚡</span>
                      <div style="flex:1;">
                        <div style="font-weight:700; color:#6b6a65; font-size:0.85rem;">{executing_task.title}</div>
                        <div style="color:#7a7872; font-size:0.72rem; margin-top:0.2rem;">
                          🕐 {st_str}–{et_str} | {timer_status} | 累计：<span id="work-time-display-{executing_task.id}">{work_time_str}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                  <script>
                    (function() {{
                      const startTime = new Date('{start_time_iso}');
                      const baseMinutes = {total_work_minutes};
                      function updateDisplay() {{
                        const now = new Date();
                        const elapsedMinutes = Math.floor((now - startTime) / 60000);
                        const totalMinutes = baseMinutes + Math.max(0, elapsedMinutes);
                        const h = Math.floor(totalMinutes / 60);
                        const m = totalMinutes % 60;
                        const s = h > 0 ? `${{h}}小时${{m}}分钟` : `${{m}}分钟`;
                        const el = document.getElementById('work-time-display-{executing_task.id}');
                        if (el) el.textContent = s;
                      }}
                      updateDisplay();
                      setInterval(updateDisplay, 1000);
                    }})();
                  </script>
                </body>
                </html>
                """
                components.html(timer_html, height=88, scrolling=False)
            else:
                # 已暂停：静态卡片
                st.markdown(
                    f"""
                    <div style="
                      background: rgba(240, 248, 242, 0.95);
                      border: 1px solid rgba(221, 214, 254, 0.35);
                      border-radius: 10px;
                      padding: 0.65rem 0.75rem;
                      box-shadow: 0 1px 6px rgba(0, 0, 0, 0.04);
                    ">
                      <div style="display:flex; align-items:flex-start; gap:0.55rem;">
                        <span style="font-size:1.05rem; line-height:1;">⚡</span>
                        <div style="flex:1;">
                          <div style="font-weight:700; color:#6b6a65; font-size:0.85rem;">{executing_task.title}</div>
                          <div style="color:#7a7872; font-size:0.72rem; margin-top:0.2rem;">
                            🕐 {st_str}–{et_str} | {timer_status} | 累计：{work_time_str}
                          </div>
                        </div>
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(
                '<div style="color:#9a9892;font-size:0.75rem;padding:0.35rem 0;text-align:center;">暂无正在执行的任务</div>',
                unsafe_allow_html=True,
            )
        
        # 高优先级任务列表
        if high_priority_tasks:
            st.markdown("""
            <div style="margin-bottom: 0.5rem;">
              <span style="font-size: 0.75rem; color: #64748b; font-weight: 600;">🔴 高优先级待办</span>
            </div>
            """, unsafe_allow_html=True)
            
            for idx, t in enumerate(high_priority_tasks[:3]):  # 最多显示3个
                sh, sm = _utc_iso_to_local_hour_minute(t.start_time or "")
                eh, em = _utc_iso_to_local_hour_minute(t.end_time or "")
                st_str, et_str = f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"
                
                task_card_html = f"""
                <div style="
                  background: rgba(239, 68, 68, 0.05);
                  border-left: 3px solid #ef4444;
                  border-radius: 6px;
                  padding: 0.5rem 0.75rem;
                  margin-bottom: 0.5rem;
                ">
                  <div style="font-size: 0.8rem; font-weight: 600; color: #475569; margin-bottom: 0.25rem;">
                    {t.title}
                  </div>
                  <div style="font-size: 0.7rem; color: #64748b;">
                    {st_str} - {et_str}
                  </div>
                </div>
                """
                st.markdown(task_card_html, unsafe_allow_html=True)
        else:
            st.markdown("""
            <div style="
              background: rgba(16, 185, 129, 0.05);
              border-radius: 8px;
              padding: 0.75rem;
              text-align: center;
              color: #64748b;
              font-size: 0.75rem;
            ">
              ✅ 暂无高优先级待办
            </div>
            """, unsafe_allow_html=True)
    
    # 关闭右侧列的flex容器（时间轴和任务列表将移到外面，各自占满宽度）
    st.markdown('</div>', unsafe_allow_html=True)

# ---------- 时间轴视图（全宽） ----------
st.markdown("<div style='height: 0.5rem;'></div>", unsafe_allow_html=True)
with st.container(border=True):
        if not tasks:
            # 没有任务时，显示空的时间轴和引导信息
            st.markdown("""
            <div style="margin-bottom: 0.5rem;">
              <span style="font-weight: 600; color: #475569; font-size: 0.875rem;">📊 时间轴视图</span>
            </div>
            """, unsafe_allow_html=True)
            # 显示空的时间轴（24小时），固定高度240px（4小时），支持滚动查看完整24小时
            start_h = 0
            end_h = 24
            html = _render_timeline_blocks(view_d, [], start_h, end_h)
            # 固定高度260px（240px内容 + 20px边距），内部可滚动查看完整24小时
            components.html(html, height=260)
            st.markdown("""
            <div style="
              background: rgba(255, 255, 255, 0.9);
              padding: 0.5rem 0.75rem;
              border-radius: 10px;
              margin-top: 0.5rem;
              margin-bottom: 1rem;
              border: 1px solid rgba(148, 163, 184, 0.2);
            ">
              <span style="color: #7a7872; font-weight: 500; font-size: 0.75rem;">
                💡 <span style="color: #7a7872;">🔵 普通</span> | 
                <span style="color: #ef4444;">🔴 高优</span> | 
                <span style="color: #10b981;">🟢 已完成</span>
              </span>
            </div>
            """, unsafe_allow_html=True)
            
            # 显示引导信息
            if search_keyword and search_keyword.strip():
                st.info(f"🔍 未找到包含「{search_keyword}」的任务")
            else:
                st.markdown("""
                <div style="
                  background: linear-gradient(135deg, rgba(102, 126, 234, 0.1), rgba(118, 75, 162, 0.1));
                  border: 2px dashed rgba(102, 126, 234, 0.3);
                  border-radius: 16px;
                  padding: 2rem;
                  text-align: center;
                  margin-top: 1rem;
                ">
                  <div style="font-size: 3rem; margin-bottom: 1rem;">📅</div>
                  <h3 style="color: #6b6a65; margin-bottom: 0.5rem;">该日暂无任务</h3>
                  <p style="color: #7a7872; margin-bottom: 1rem;">
                    在左侧输入框输入指令，例如：
                  </p>
                  <div style="
                    background: rgba(255, 255, 255, 0.9);
                    border-radius: 12px;
                    padding: 1rem;
                    margin: 0.5rem 0;
                    text-align: left;
                    border: 1px solid rgba(148, 163, 184, 0.2);
                  ">
                    <div style="color: #7a7872; font-weight: 600; margin-bottom: 0.5rem;">💡 示例指令：</div>
                    <div style="color: #7a7872; font-size: 0.9rem; line-height: 1.8;">
                      • 添加 下午三点 写代码<br/>
                      • 安排 明天上午9点 开会 2小时<br/>
                      • 添加 晚上8点 运动 1小时
                    </div>
                  </div>
                    </div>
                    """, unsafe_allow_html=True)
        else:
            # 有任务时，显示时间轴
            st.markdown("""
            <div style="margin-bottom: 0.5rem;">
              <span style="font-weight: 600; color: #475569; font-size: 0.875rem;">📊 时间轴视图</span>
            </div>
            """, unsafe_allow_html=True)
            # 支持24小时显示（0-24点），固定高度240px（4小时），支持滚动查看完整24小时
            start_h = 0
            end_h = 24
            html = _render_timeline_blocks(view_d, tasks, start_h, end_h)
            # 固定高度260px（240px内容 + 20px边距），内部可滚动查看完整24小时
            components.html(html, height=260)
            st.markdown("""
            <div style="
              background: rgba(255, 255, 255, 0.9);
              padding: 0.4rem 0.6rem;
              border-radius: 8px;
              margin-top: 0.5rem;
              margin-bottom: 0.5rem;
              border: 1px solid rgba(148, 163, 184, 0.2);
            ">
              <span style="color: #64748b; font-weight: 500; font-size: 0.7rem;">
                💡 <span style="color: #7a7872;">🔵 普通</span> | 
                <span style="color: #ef4444;">🔴 高优</span> | 
                <span style="color: #10b981;">🟢 已完成</span>
              </span>
            </div>
            """, unsafe_allow_html=True)

# ---------- 任务列表（全宽） ----------
st.markdown("<div style='height: 0.5rem;'></div>", unsafe_allow_html=True)
if tasks:
    with st.container(border=True):
        st.markdown(f"""
        <div style="margin-bottom: 0.5rem;">
          <span style="font-weight: 600; color: #475569; font-size: 0.875rem;">📋 任务列表 ({len(tasks)} 项)</span>
        </div>
        """, unsafe_allow_html=True)
        # 任务列表（带操作按钮和进度条）
        from datetime import datetime, timezone
        import re
        now = datetime.now(timezone.utc)
            
        # 使用三列布局显示任务列表，充分利用全宽空间
        task_cols = st.columns(3)
        task_list = sorted(tasks, key=lambda x: x.start_time)

        for idx, t in enumerate(task_list):
            # 交替分配到三列
            col_idx = idx % 3
            with task_cols[col_idx]:
                # 使用容器包裹任务卡片和按钮，使它们在同一块内
                with st.container(border=True):
                    sh, sm = _utc_iso_to_local_hour_minute(t.start_time or "")
                    eh, em = _utc_iso_to_local_hour_minute(t.end_time or "")
                    st_str, et_str = f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"
                    
                    # 进度与展示：status=done 时一律显示「已完成」和 100%
                    # 待办任务：仅当用户点了「开始」后才按计时动态更新进度；暂停或未开始时只显示 memo 中保存的进度，不按日历时间自动更新
                    user_progress = None
                    if t.memo:
                        progress_matches = re.findall(r'progress:\s*(\d+)', str(t.memo))
                        if progress_matches:
                            user_progress = int(progress_matches[-1])
                    
                    if t.status == "done":
                        progress = 100
                        display_as_done = True
                    else:
                        display_as_done = False
                        has_timer_start = bool(t.memo and "timer_start:" in str(t.memo))
                        if has_timer_start:
                            # 正在计时：按 timer_start + total_work_minutes 动态计算进度
                            try:
                                start_dt = datetime.fromisoformat(t.start_time.replace("Z", "+00:00"))
                                end_dt = datetime.fromisoformat(t.end_time.replace("Z", "+00:00"))
                                if start_dt.tzinfo:
                                    start_dt = start_dt.astimezone().replace(tzinfo=None)
                                if end_dt.tzinfo:
                                    end_dt = end_dt.astimezone().replace(tzinfo=None)
                                task_duration_min = (end_dt - start_dt).total_seconds() / 60
                                total_work_match = re.search(r'total_work_minutes:\s*(\d+)', str(t.memo))
                                total_work_minutes = int(total_work_match.group(1)) if total_work_match else 0
                                timer_start_match = re.search(r'timer_start:\s*([^|\s]+)', str(t.memo))
                                if timer_start_match and task_duration_min > 0:
                                    timer_start = datetime.fromisoformat(timer_start_match.group(1).strip().replace("Z", "+00:00"))
                                    if timer_start.tzinfo:
                                        timer_start = timer_start.astimezone().replace(tzinfo=None)
                                    now_elapsed = datetime.now()
                                    if now_elapsed.tzinfo:
                                        now_elapsed = now_elapsed.astimezone().replace(tzinfo=None)
                                    elapsed_since_start = (now_elapsed - timer_start).total_seconds() / 60
                                    total_work_minutes += int(elapsed_since_start)
                                    progress = min(100, max(0, int((total_work_minutes / task_duration_min) * 100)))
                                else:
                                    progress = user_progress if user_progress is not None else 0
                            except Exception:
                                progress = user_progress if user_progress is not None else 0
                        else:
                            # 未开始或已暂停：只显示 memo 中保存的进度，不按当前时间推算
                            progress = user_progress if user_progress is not None else 0
                    if display_as_done:
                        color = "🟢"
                        progress_class = "done"
                        progress_text = "已完成"
                    else:
                        if user_progress is not None:
                            progress_text = "进度"
                        elif progress >= 100:
                            progress_text = "已过期"
                        elif progress == 0:
                            progress_text = "未开始"
                        else:
                            progress_text = "进度"
                        if t.priority == "high":
                            color = "🔴"
                            progress_class = "high"
                        else:
                            color = "🔵"
                            progress_class = "pending"

                    # 任务卡片标题行（Streamlit 原生，便于与下方进度条同页保存）
                    st.markdown(f"""
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.35rem;">
                      <span style="font-size: 1rem;">{color}</span>
                      <span style="font-weight: 700; font-size: 0.95rem; color: #6b6a65; flex: 1; min-width: 80px;">{t.title}</span>
                      <span style="font-size: 0.75rem; color: #7a7872; font-weight: 600;">{st_str}–{et_str}</span>
                      <span style="font-size: 0.7rem; padding: 0.2rem 0.5rem; border-radius: 8px; font-weight: 600; background: {'rgba(16,185,129,0.1)' if display_as_done else 'rgba(59,130,246,0.1)'}; color: #7a7872;">{'已完成' if display_as_done else '待办'}</span>
                    </div>
                    """, unsafe_allow_html=True)
                    # 使用 Streamlit 原生进度条：已完成任务禁用拖动
                    progress_slider_disabled = display_as_done
                    new_progress = st.slider(
                        "进度",
                        min_value=0,
                        max_value=100,
                        value=progress,
                        key=f"progress_slider_{t.id}_{t.status}",
                        disabled=progress_slider_disabled,
                        label_visibility="collapsed",
                    )
                    # 仅对待办任务根据滑块写回进度；已完成任务不根据滑块更新，避免 stale widget 状态(0)与 progress(100) 不等导致无限 rerun 刷屏
                    if not display_as_done and new_progress != progress:
                        ok, msg = update_task_progress(str(t.id), new_progress)
                        st.session_state.last_feedback = msg
                        st.rerun()
                    st.caption(f"{progress_text} {new_progress}%")

                    # 操作按钮行 - 在容器内，使用 Streamlit 按钮（3个按钮：开始、完成、删除）
                    # 检查任务是否正在计时
                    is_task_timer_running = False
                    if t.memo and 'timer_start:' in str(t.memo):
                        is_task_timer_running = True
                    
                    task_col1, task_col2, task_col3 = st.columns([1, 1, 1])
                    with task_col1:
                        if not is_task_timer_running:
                            if st.button("▶️ 开始", key=f"tasklist_start_{t.id}", use_container_width=True):
                                ok, msg = start_task_timer(str(t.id))
                                st.session_state.last_feedback = msg
                                st.session_state.current_executing_task_id = str(t.id)
                                st.rerun()
                        else:
                            if st.button("⏸️ 暂停", key=f"tasklist_pause_{t.id}", use_container_width=True):
                                ok, msg = pause_task_timer(str(t.id))
                                st.session_state.last_feedback = msg
                                st.rerun()
                    with task_col2:
                        button_label = "✓ 完成" if t.status == "pending" else "↩ 待办"
                        if st.button(button_label, key=f"tasklist_toggle_{t.id}", use_container_width=True):
                            ok, msg = toggle_task_status(str(t.id))
                            st.session_state.last_feedback = msg
                            st.rerun()
                    with task_col3:
                        if st.button("🗑 删除", key=f"tasklist_delete_{t.id}", use_container_width=True):
                            ok, msg = delete_task_by_id(str(t.id))
                            st.session_state.last_feedback = msg
                            st.rerun()

# 对话与反馈区域 - 单独占据整个页面宽度
st.markdown("<div style='height: 0.5rem;'></div>", unsafe_allow_html=True)
with st.container(border=True):
        st.markdown("""
        <div style="
          display: flex;
          align-items: center;
          gap: 0.5rem;
          margin-bottom: 1rem;
        ">
          <h2 style="margin: 0; color: #6b6a65; font-weight: 700;">💬 对话与反馈</h2>
        </div>
        """, unsafe_allow_html=True)
        st.text_area(
            "反馈",
            value=st.session_state.last_feedback or "",
            height=180,
            disabled=True,
            label_visibility="collapsed",
            help="系统反馈信息将显示在这里"
        )
        if st.session_state.get("llm_reasoning"):
            with st.expander("🧠 LLM 思考过程", expanded=True):
                st.markdown(st.session_state.llm_reasoning)
        if st.session_state.get("overflow_warning"):
            st.warning(st.session_state.overflow_warning)
            st.session_state.overflow_warning = None
        prompt = st.chat_input("💡 输入指令，如：把下午两点的会推迟一小时")
        if prompt:
            try:
                # 保存用户输入到对话历史
                conversation_history = st.session_state.get("conversation_history", [])
                reply, warn, pending, reasoning = handle_user_command(prompt, conversation_history)
                
                # 保存对话历史（用户输入 + 系统回复）
                st.session_state.conversation_history.append({
                    "role": "user",
                    "content": prompt
                })
                st.session_state.conversation_history.append({
                    "role": "assistant", 
                    "content": reply
                })
                # 只保留最近 10 轮对话（20 条消息）
                if len(st.session_state.conversation_history) > 20:
                    st.session_state.conversation_history = st.session_state.conversation_history[-20:]
                
                st.session_state.last_feedback = reply
                st.session_state.pending_plan = pending
                st.session_state.llm_reasoning = reasoning or ""
                if warn:
                    st.session_state.overflow_warning = warn
            except Exception as e:
                error_msg = f"执行失败：{e}"
                st.session_state.last_feedback = error_msg
                st.session_state.conversation_history.append({
                    "role": "user",
                    "content": prompt
                })
                st.session_state.conversation_history.append({
                    "role": "assistant",
                    "content": error_msg
                })
            st.rerun()

        # ---- 需要确认的预览计划 ----
        if st.session_state.get("pending_plan") is not None:
            plan = st.session_state.pending_plan
            st.markdown("### 📋 预览变更")
            st.caption("💡 如果不满意此计划，点击「取消」后可在上方输入框继续描述您的需求，我会根据您的反馈重新调整。")
            st.markdown("**修改前 → 修改后：**")
            
            # 显示详细的修改前后信息
            for update in plan.updates:
                old_start = update.get("old_start", "")
                old_end = update.get("old_end", "")
                new_start = update.get("new_start", "")
                new_end = update.get("new_end", "")
                title = update.get("title", "")
                
                old_start_str = old_start[11:16] if len(old_start) >= 16 else old_start
                old_end_str = old_end[11:16] if len(old_end) >= 16 else old_end
                new_start_str = new_start[11:16] if len(new_start) >= 16 else new_start
                new_end_str = new_end[11:16] if len(new_end) >= 16 else new_end
                
                st.markdown(f"""
                **{title}**  
                🕐 {old_start_str}–{old_end_str} → 🕐 {new_start_str}–{new_end_str}
                """)
            
            if plan.overflow_warning:
                st.warning(plan.overflow_warning)
            
            st.markdown("---")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ 确认执行", use_container_width=True, type="primary"):
                    try:
                        reply, warn = apply_pending_plan(plan)
                        st.session_state.last_feedback = reply
                        st.session_state.pending_plan = None
                        # 更新对话历史
                        st.session_state.conversation_history.append({
                            "role": "assistant",
                            "content": reply
                        })
                        if warn:
                            st.session_state.overflow_warning = warn
                    except Exception as e:
                        st.session_state.last_feedback = f"执行失败：{e}"
                    st.rerun()
            with c2:
                if st.button("❌ 取消", use_container_width=True):
                    st.session_state.pending_plan = None
                    st.session_state.last_feedback = "已取消本次调整。您可以在上方输入框继续输入新的指令，我会根据您的反馈重新调整计划。"
                    st.session_state.conversation_history.append({
                        "role": "assistant",
                        "content": "已取消本次调整。您可以继续告诉我您的需求，我会重新为您调整计划。"
                    })
                    st.rerun()

        # ---- 系统日志回溯 ----
        if st.session_state.get("show_logs", False):
            with st.expander("系统日志", expanded=True):
                logs = get_recent_logs(limit=200)
                if not logs:
                    st.info("暂无日志记录")
                else:
                    # 按时间倒序显示（最新的在前）
                    logs_reversed = list(reversed(logs))
                    log_text = "\n".join([
                        f"[{log['time']}] {log['level']:8s} {log['name']:20s} - {log['message']}"
                        for log in logs_reversed
                    ])
                    st.text_area(
                        "最近200条日志",
                        value=log_text,
                        height=400,
                        disabled=True,
                        label_visibility="collapsed",
                    )

# 自动检查未完成任务（只在首次加载时执行一次）
if not st.session_state.auto_checked_overdue:
    st.session_state.auto_checked_overdue = True
    try:
        count, msg = check_and_reschedule_overdue_tasks()
        if count > 0:
            st.session_state.last_feedback = msg
    except Exception as e:
        # 静默失败，不影响应用启动
        pass

# 主区域布局已完成：时钟在左侧列，日程管理在右侧列（pomodoro_col）

# ---------- 提醒事项栏目（全宽） ----------
st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)
with st.container(border=True):
        st.markdown("""
        <div style="margin: 0; padding: 0;">
          <h2 style="margin: 0; color: #6b6a65; font-weight: 700; font-size: 1.1rem;">📌 提醒事项</h2>
        </div>
        """, unsafe_allow_html=True)
        
        # 获取所有提醒事项
        try:
            reminders = repo.all_reminders()
        except Exception as e:
            st.error(f"加载提醒事项失败: {e}")
            reminders = []
        
        # 添加新提醒事项
        new_reminder_col1, new_reminder_col2 = st.columns([4, 1])
        with new_reminder_col1:
            new_reminder_text = st.text_input(
                "添加提醒事项",
                key="new_reminder_input",
                placeholder="输入重要事项...",
                label_visibility="collapsed"
            )
        with new_reminder_col2:
            if st.button("➕ 添加", key="add_reminder", use_container_width=True):
                if new_reminder_text and new_reminder_text.strip():
                    try:
                        repo.insert_reminder(new_reminder_text.strip())
                        st.rerun()
                    except Exception as e:
                        st.error(f"添加失败: {e}")
        
        # 显示提醒事项列表
        if reminders:
            st.markdown("<div style='margin-top: 0.75rem;'></div>", unsafe_allow_html=True)
            for reminder in reminders:
                reminder_col1, reminder_col2, reminder_col3 = st.columns([1, 8, 1])
                with reminder_col1:
                    # 复选框：打对勾
                    checked = st.checkbox(
                        "完成",
                        value=reminder.is_completed,
                        key=f"reminder_check_{reminder.id}",
                        label_visibility="collapsed"
                    )
                    if checked != reminder.is_completed:
                        try:
                            repo.update_reminder(str(reminder.id), {"is_completed": checked})
                            st.rerun()
                        except Exception as e:
                            st.error(f"更新失败: {e}")
                
                with reminder_col2:
                    # 显示提醒内容（已完成则添加删除线）
                    style = "text-decoration: line-through; color: #9a9892; opacity: 0.6;" if reminder.is_completed else "color: #6b6a65;"
                    st.markdown(f'<div style="{style} font-size: 0.9rem; padding: 0.5rem 0;">{reminder.content}</div>', unsafe_allow_html=True)
                
                with reminder_col3:
                    # 删除按钮
                    if st.button("🗑️", key=f"delete_reminder_{reminder.id}", help="删除"):
                        try:
                            repo.delete_reminder(str(reminder.id))
                            st.rerun()
                        except Exception as e:
                            st.error(f"删除失败: {e}")
        else:
            st.markdown('<div style="color: #9a9892; font-size: 0.85rem; padding: 1rem 0; text-align: center;">暂无提醒事项</div>', unsafe_allow_html=True)

# 报表栏目：放在最后，占据整个宽度
st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)
st.markdown("""
<div style="margin-bottom: 0.75rem;">
  <span style="font-weight: 600; color: #7a7872; font-size: 0.875rem;">📊 报表</span>
</div>
""", unsafe_allow_html=True)

# 日报栏目
if st.session_state.daily_report_text:
    with st.container(border=True):
        st.markdown("### 📝 日报")
        st.write(st.session_state.daily_report_text)
        st.download_button(
            "📥 导出日报",
            data=st.session_state.daily_report_text,
            file_name=f"daily_report_{date.today().isoformat()}.txt",
            mime="text/plain",
            use_container_width=True,
        )

# 周报栏目
if st.session_state.weekly_report_text:
    with st.container(border=True):
        st.markdown("### 📊 周报")
        st.write(st.session_state.weekly_report_text)
        st.download_button(
            "📥 导出周报",
            data=st.session_state.weekly_report_text,
            file_name=f"weekly_report_{date.today().isoformat()}.txt",
            mime="text/plain",
            use_container_width=True,
        )

# 月报栏目
if st.session_state.monthly_report_text:
    with st.container(border=True):
        st.markdown("### 📅 月报")
        st.write(st.session_state.monthly_report_text)
        st.download_button(
            "📥 导出月报",
            data=st.session_state.monthly_report_text,
            file_name=f"monthly_report_{date.today().year}-{date.today().month:02d}.txt",
            mime="text/plain",
            use_container_width=True,
        )

# ---------- 快捷操作（全宽，移至最底部） ----------
st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)
st.markdown("""
<div style="margin-bottom: 0.75rem;">
  <span style="font-weight: 600; color: #7a7872; font-size: 0.875rem;">⚙️ 快捷操作</span>
</div>
""", unsafe_allow_html=True)

# 功能按钮 - 两行布局，每行3个按钮
btn_col1, btn_col2, btn_col3 = st.columns(3)
with btn_col1:
    if st.button("📊 周报", use_container_width=True):
        cache_key = f"weekly_{date.today().isoformat()}"
        if cache_key in st.session_state.report_cache:
            st.session_state.weekly_report_text = st.session_state.report_cache[cache_key]
            st.session_state.last_feedback = "周报已生成（从缓存加载，见下方「周报」栏目）。"
        else:
            try:
                text = get_weekly_report_text()
                st.session_state.weekly_report_text = text
                st.session_state.report_cache[cache_key] = text
                st.session_state.last_feedback = "周报已生成（见下方「周报」栏目）。"
            except Exception as e:
                st.session_state.last_feedback = f"周报生成失败：{e}"
        st.rerun()
with btn_col2:
    if st.button("📅 月报", use_container_width=True):
        cache_key = f"monthly_{date.today().year}-{date.today().month:02d}"
        if cache_key in st.session_state.report_cache:
            st.session_state.monthly_report_text = st.session_state.report_cache[cache_key]
            st.session_state.last_feedback = "月报已生成（从缓存加载，见下方「月报」栏目）。"
        else:
            try:
                text = get_monthly_report_text()
                st.session_state.monthly_report_text = text
                st.session_state.report_cache[cache_key] = text
                st.session_state.last_feedback = "月报已生成（见下方「月报」栏目）。"
            except Exception as e:
                st.session_state.last_feedback = f"月报生成失败：{e}"
        st.rerun()
with btn_col3:
    if st.button("📝 日报", use_container_width=True):
        today_str = date.today().isoformat()
        try:
            # 每次查询都重新生成并更新日报内容
            content = run_daily_archive(date.today())
            st.session_state.daily_report_text = content
            cache_key = f"daily_{today_str}"
            st.session_state.report_cache[cache_key] = content
            st.session_state.last_feedback = "今日日报已归档（见下方「日报」栏目）。"
        except Exception as e:
            st.session_state.last_feedback = f"归档失败：{e}"
        st.rerun()

btn_col4, btn_col5, btn_col6 = st.columns(3)
with btn_col4:
    if st.button("↩ 撤销", use_container_width=True):
        ok, msg = undo_last()
        st.session_state.last_feedback = msg
        st.rerun()
with btn_col5:
    if st.button("📋 日志", use_container_width=True):
        st.session_state.show_logs = not st.session_state.get("show_logs", False)
        st.rerun()
with btn_col6:
    if st.button("⏰ 延期", use_container_width=True):
        try:
            count, msg = check_and_reschedule_overdue_tasks()
            st.session_state.last_feedback = msg
        except Exception as e:
            st.session_state.last_feedback = f"检查失败：{e}"
        st.rerun()

# ---------- 长周期任务管理（全宽，添加到最后） ----------
st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)
st.markdown("""
<div style="margin-bottom: 0.75rem;">
  <span style="font-weight: 700; color: #6b6a65; font-size: 0.95rem;">🧭 长周期任务管理</span>
  <span style="color:#94a3b8; font-size:0.8rem; margin-left:0.5rem;">项目 / 里程碑 / 子任务（监督进度与风险）</span>
</div>
""", unsafe_allow_html=True)

with st.container(border=True):
    # --- 新建长周期任务 ---
    st.markdown("<div style='height: 0.25rem;'></div>", unsafe_allow_html=True)
    st.markdown("#### ➕ 新建长周期任务")
    try:
        _lt_all = repo.list_long_tasks(limit=200)
    except Exception as e:
        _lt_all = []
        st.error(f"加载长周期任务失败：{e}")

    parent_options = [("（无父任务）", None)] + [(f"{t.title} [{t.status} {t.progress}%]", str(t.id)) for t in _lt_all if t.id]
    c1, c2, c3, c4 = st.columns([3.2, 1.2, 1.2, 1.4])
    with c1:
        lt_title = st.text_input("标题", key="lt_new_title", placeholder="例如：长期项目第一阶段交付", label_visibility="collapsed")
        lt_desc = st.text_area("描述", key="lt_new_desc", placeholder="可选：范围、验收标准、关键风险…", height=70, label_visibility="collapsed")
    with c2:
        lt_priority = st.selectbox("优先级", ["high", "normal", "low"], index=1, key="lt_new_priority")
        lt_status = st.selectbox("状态", ["todo", "doing", "blocked", "done", "cancelled"], index=0, key="lt_new_status")
    with c3:
        lt_start = st.date_input("开始日期", value=None, key="lt_new_start")
        lt_due = st.date_input("截止日期", value=None, key="lt_new_due")
    with c4:
        parent_label = st.selectbox(
            "父任务（可选）",
            options=[x[0] for x in parent_options],
            index=0,
            key="lt_new_parent",
        )
        lt_tags = st.text_input("标签", key="lt_new_tags", placeholder="逗号分隔，如：项目,客户,交付")
        if st.button("创建任务", key="lt_new_create", type="primary", use_container_width=True):
            title = (lt_title or "").strip()
            if not title:
                st.warning("请先填写标题。")
            else:
                parent_id = None
                for lbl, pid in parent_options:
                    if lbl == parent_label:
                        parent_id = pid
                        break
                tags = [x.strip() for x in (lt_tags or "").split(",") if x.strip()]
                try:
                    from db.models import LongTask
                    created = repo.insert_long_task(LongTask(
                        id=None,
                        parent_id=parent_id,
                        title=title,
                        description=(lt_desc or "").strip() or None,
                        status=lt_status,
                        priority=lt_priority,
                        progress=0,
                        start_date=lt_start.isoformat() if lt_start else None,
                        due_date=lt_due.isoformat() if lt_due else None,
                        tags=tags or [],
                    ))
                    st.session_state.last_feedback = f"已创建长周期任务：{created.title}"
                    st.rerun()
                except Exception as e:
                    st.error(f"创建失败：{e}")

    st.markdown("---")

    # --- 列表与监督 ---
    st.markdown("#### 📌 任务列表与监督")
    filter_c1, filter_c2, filter_c3 = st.columns([2, 2, 1.2])
    with filter_c1:
        status_filter = st.multiselect(
            "状态筛选",
            ["todo", "doing", "blocked", "done", "cancelled"],
            default=["todo", "doing", "blocked"],
            key="lt_filter_status",
        )
    with filter_c2:
        kw = st.text_input("关键词", key="lt_filter_kw", placeholder="按标题/描述搜索")
    with filter_c3:
        show_done = st.checkbox("显示已完成/取消", value=False, key="lt_filter_show_done")

    lt_list = _lt_all
    if status_filter:
        lt_list = [t for t in lt_list if (t.status in status_filter)]
    if not show_done:
        lt_list = [t for t in lt_list if t.status not in ("done", "cancelled")]
    if kw and kw.strip():
        k = kw.strip().lower()
        lt_list = [t for t in lt_list if (k in (t.title or "").lower()) or (k in (t.description or "").lower())]

    if not lt_list:
        st.info("暂无符合条件的长周期任务。")
    else:
        from datetime import date as _date
        today = _date.today().isoformat()

        for t in lt_list:
            tid = str(t.id or "")
            due_txt = t.due_date or ""
            overdue = bool(due_txt and due_txt < today and t.status not in ("done", "cancelled"))
            badge = "⚠️ 逾期" if overdue else ("🚧 阻塞" if t.status == "blocked" else "")
            header = f"{t.title}  ·  {t.status}  ·  {t.progress}%"
            if due_txt:
                header += f"  ·  截止 {due_txt}"
            if badge:
                header += f"  ·  {badge}"

            with st.expander(header, expanded=False):
                top1, top2, top3, top4 = st.columns([2.2, 1.2, 1.2, 1.2])
                with top1:
                    st.caption("描述")
                    st.write(t.description or "（无）")
                    st.caption("标签")
                    st.write(", ".join(t.tags or []) if t.tags else "（无）")
                    if t.parent_id:
                        st.caption("父任务")
                        st.write(t.parent_id)
                with top2:
                    new_status = st.selectbox(
                        "状态",
                        ["todo", "doing", "blocked", "done", "cancelled"],
                        index=["todo", "doing", "blocked", "done", "cancelled"].index(t.status),
                        key=f"lt_status_{tid}",
                    )
                    new_priority = st.selectbox(
                        "优先级",
                        ["high", "normal", "low"],
                        index=["high", "normal", "low"].index(t.priority),
                        key=f"lt_pri_{tid}",
                    )
                with top3:
                    new_progress = st.slider("进度", 0, 100, int(t.progress or 0), key=f"lt_prog_{tid}")
                    new_due = st.date_input("截止日期", value=None if not t.due_date else _date.fromisoformat(t.due_date), key=f"lt_due_{tid}")
                with top4:
                    note = st.text_input("更新/备注", key=f"lt_note_{tid}", placeholder="例如：已完成需求评审；阻塞点…")
                    btn_save = st.button("保存变更", key=f"lt_save_{tid}", use_container_width=True, type="primary")
                    btn_del = st.button("删除任务", key=f"lt_del_{tid}", use_container_width=True)

                if btn_del:
                    try:
                        repo.delete_long_task(tid)
                        st.session_state.last_feedback = f"已删除长周期任务：{t.title}"
                        st.rerun()
                    except Exception as e:
                        st.error(f"删除失败：{e}")

                if btn_save:
                    updates = {}
                    if new_status != t.status:
                        updates["status"] = new_status
                    if new_priority != t.priority:
                        updates["priority"] = new_priority
                    if int(new_progress) != int(t.progress or 0):
                        updates["progress"] = int(new_progress)
                    # date_input 返回 date 对象或 None
                    due_iso = new_due.isoformat() if new_due else None
                    if due_iso != (t.due_date or None):
                        updates["due_date"] = due_iso
                    if updates:
                        try:
                            repo.update_long_task(tid, updates)
                            if "status" in updates:
                                repo.add_long_task_log(tid, "status", f"状态变更：{t.status} → {updates['status']}")
                            if "progress" in updates:
                                repo.add_long_task_log(tid, "progress", f"进度更新：{t.progress}% → {updates['progress']}%")
                            if "priority" in updates:
                                repo.add_long_task_log(tid, "note", f"优先级调整：{t.priority} → {updates['priority']}")
                        except Exception as e:
                            st.error(f"保存失败：{e}")
                    if note and note.strip():
                        try:
                            repo.add_long_task_log(tid, "note", note.strip())
                        except Exception:
                            pass
                    st.session_state.last_feedback = "长周期任务已更新。"
                    st.rerun()

                # --- 最近日志 ---
                try:
                    logs = repo.logs_for_long_task(tid, limit=10)
                except Exception:
                    logs = []
                if logs:
                    st.markdown("**最近更新（10条）**")
                    for lg in logs:
                        tm = (lg.created_at or "")[:19].replace("T", " ")
                        st.markdown(f"- `{tm}` **{lg.kind}**：{lg.content}")
                else:
                    st.caption("暂无更新日志。")

# ---------- 未来日程日历（苹果风格，全宽，放在最后） ----------
def _escape_html(s: str) -> str:
    if not s:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)
st.markdown("""
<div style="margin-bottom: 0.75rem;">
  <span style="font-weight: 700; color: #1e293b; font-size: 1.05rem;">📅 日程日历</span>
  <span style="color:#94a3b8; font-size:0.8rem; margin-left:0.5rem;">可查看本周/本月（含往期）或未来一周/一月</span>
</div>
""", unsafe_allow_html=True)

with st.container(border=True):
    today = date.today()
    view_mode = st.radio(
        "显示范围",
        ["本周", "未来一周", "本月", "未来一月"],
        horizontal=True,
        key="future_schedule_view",
        label_visibility="collapsed",
    )
    if view_mode == "本周":
        # 本周一～本周日，含往期
        start_d = today - timedelta(days=today.weekday())
        end_d = start_d + timedelta(days=6)
    elif view_mode == "未来一周":
        start_d = today
        end_d = today + timedelta(days=6)
    elif view_mode == "本月":
        # 本月 1 号～月末，含往期
        start_d = today.replace(day=1)
        _last = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        end_d = _last
    else:
        # 未来一月
        start_d = today
        end_d = today + timedelta(days=30)

    try:
        future_tasks = repo.tasks_for_date_range(start_d, end_d)
    except Exception:
        future_tasks = []

    def _task_display_date_and_time(t: Task) -> tuple[str, str]:
        """
        返回 (显示用日期 YYYY-MM-DD, 显示用时间 HH:MM–HH:MM)。
        兼容旧数据：若当时把「本地时间」误存为 UTC（转成本地后日期会变），则用存储的日期+时分显示。
        """
        s, e = t.start_time or "", t.end_time or ""
        stored_date = s[:10] if len(s) >= 10 else ""
        stored_time = f"{s[11:16]}–{e[11:16]}" if len(s) >= 16 and len(e) >= 16 else ""
        if not stored_date:
            return stored_date, stored_time
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            local_dt = _to_naive_local(dt)
            local_date = local_dt.date().isoformat()
            local_time = f"{local_dt.strftime('%H:%M')}–{_to_naive_local(datetime.fromisoformat(e.replace('Z', '+00:00'))).strftime('%H:%M')}" if len(e) >= 16 else stored_time
            # 旧数据：存的是「本地时间当 UTC」时，转成本地会变成次日 0 点附近，日期错一天
            if stored_date != local_date and len(s) >= 16:
                try:
                    hour = int(s[11:13])
                    if 6 <= hour <= 23:
                        return stored_date, stored_time
                except Exception:
                    pass
            return local_date, local_time
        except Exception:
            return stored_date, stored_time

    def _task_local_date_iso(t: Task) -> str:
        return _task_display_date_and_time(t)[0]

    def _task_local_time_range(t: Task) -> str:
        return _task_display_date_and_time(t)[1]

    tasks_by_day: dict[str, list] = {}
    for t in future_tasks:
        try:
            day_key = _task_local_date_iso(t)
            if day_key:
                tasks_by_day.setdefault(day_key, []).append(t)
        except Exception:
            continue
    for day_key in tasks_by_day:
        tasks_by_day[day_key].sort(key=lambda x: x.start_time or "")

    weekdays_cn = ["一", "二", "三", "四", "五", "六", "日"]

    # 苹果风格 CSS
    st.markdown("""
<style>
.cal-apple { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
.cal-week { display: grid; grid-template-columns: repeat(7, 1fr); gap: 0; border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; }
.cal-day { min-height: 120px; border-right: 1px solid #e2e8f0; padding: 8px; background: #fafafa; }
.cal-day:last-child { border-right: none; }
.cal-day--today { background: #eff6ff; }
.cal-day-num { font-size: 0.85rem; color: #64748b; margin-bottom: 6px; }
.cal-day-num--today { color: #2563eb; font-weight: 700; }
.cal-day-num--today .n { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; border-radius: 50%; background: #2563eb; color: #fff; }
.cal-event { font-size: 0.75rem; padding: 4px 6px; margin-bottom: 4px; border-radius: 4px; border-left: 3px solid #3b82f6; background: #fff; color: #334155; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cal-event--high { border-left-color: #ff6b6b; }
.cal-event--low { border-left-color: #94a3b8; }
.cal-event-time { color: #64748b; font-size: 0.7rem; margin-right: 4px; }
.cal-month { display: grid; grid-template-columns: repeat(7, 1fr); gap: 0; border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; }
.cal-month-head { background: #f1f5f9; font-size: 0.75rem; color: #64748b; font-weight: 600; padding: 8px 4px; text-align: center; border-bottom: 1px solid #e2e8f0; }
.cal-month-cell { min-height: 72px; padding: 6px; border-right: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; background: #fff; }
.cal-month-cell:nth-child(7n) { border-right: none; }
.cal-month-cell--other { background: #f8fafc; color: #cbd5e1; }
.cal-month-cell--today { background: #eff6ff; }
.cal-month-num { font-size: 0.9rem; margin-bottom: 4px; }
.cal-month-num--today .n { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 50%; background: #2563eb; color: #fff; font-weight: 700; }
.cal-month-events { font-size: 0.7rem; color: #475569; }
.cal-month-event { padding: 2px 4px; margin-bottom: 2px; border-radius: 2px; border-left: 2px solid #3b82f6; background: #f0f9ff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cal-month-event--high { border-left-color: #ff6b6b; background: #fff5f5; }
.cal-month-event--low { border-left-color: #94a3b8; background: #f1f5f9; }
</style>
""", unsafe_allow_html=True)

    if view_mode in ("本周", "未来一周"):
        # 一周视图：7 列对应 start_d～start_d+6（本周=周一～周日，未来一周=今天～今天+6）
        head = "".join([f'<div class="cal-month-head">{w}</div>' for w in ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]])
        body_parts = []
        for col in range(7):
            d = start_d + timedelta(days=col)
            day_key = d.isoformat()
            day_tasks = tasks_by_day.get(day_key, [])
            is_today = d == today
            today_cls = " cal-day--today" if is_today else ""
            num_cls = " cal-day-num--today" if is_today else ""
            num_inner = f'<span class="n">{d.day}</span>' if is_today else str(d.day)
            num_html = f'<div class="cal-day-num{num_cls}">{d.month}月 {num_inner}</div>'
            events_html = ""
            for t in day_tasks:
                time_range = _task_local_time_range(t)
                pr = (t.priority or "normal").lower()
                pr_cls = " cal-event--high" if pr == "high" else (" cal-event--low" if pr == "low" else "")
                title_esc = _escape_html(t.title or "（无标题）")
                events_html += f'<div class="cal-event{pr_cls}"><span class="cal-event-time">{time_range}</span>{title_esc}</div>'
            empty_label = '<span style="color:#94a3b8;font-size:0.75rem;">无安排</span>'
            body_parts.append(f'<div class="cal-day{today_cls}">{num_html}<div class="cal-day-events">{events_html or empty_label}</div></div>')
        grid_html = f'<div class="cal-apple cal-week"><div style="grid-column:1/-1;display:grid;grid-template-columns:repeat(7,1fr);">{head}</div>' + "".join(body_parts) + "</div>"
        st.markdown(grid_html, unsafe_allow_html=True)
    else:
        # 一月视图：月历网格，从 start_d 所在周的周一开始到 end_d 所在周的周日
        start_align = start_d - timedelta(days=start_d.weekday())
        end_align = end_d + timedelta(days=(6 - end_d.weekday()))
        cur = start_align
        weeks: list[list[date]] = []
        while cur <= end_align:
            week = [cur + timedelta(days=j) for j in range(7)]
            weeks.append(week)
            cur = week[-1] + timedelta(days=1)
        head = "".join([f'<div class="cal-month-head">周{w}</div>' for w in weekdays_cn])
        cells_html = []
        for week_row in weeks:
            for d in week_row:
                day_key = d.isoformat()
                day_tasks = tasks_by_day.get(day_key, [])
                is_today = d == today
                in_range = start_d <= d <= end_d
                other_cls = " cal-month-cell--other" if not in_range else ""
                today_cell_cls = " cal-month-cell--today" if is_today else ""
                num_cls = " cal-month-num--today" if is_today else ""
                num_inner = f'<span class="n">{d.day}</span>' if is_today else str(d.day)
                num_html = f'<div class="cal-month-num{num_cls}">{num_inner}</div>'
                events_html = ""
                for t in day_tasks[:5]:
                    pr = (t.priority or "normal").lower()
                    pr_cls = " cal-month-event--high" if pr == "high" else (" cal-month-event--low" if pr == "low" else "")
                    title_esc = _escape_html((t.title or "（无）")[:14])
                    events_html += f'<div class="cal-month-event{pr_cls}">{title_esc}</div>'
                if len(day_tasks) > 5:
                    events_html += f'<div style="font-size:0.65rem;color:#94a3b8;">+{len(day_tasks)-5}项</div>'
                cells_html.append(f'<div class="cal-month-cell{other_cls}{today_cell_cls}">{num_html}<div class="cal-month-events">{events_html}</div></div>')
        grid_html = f'<div class="cal-apple cal-month"><div style="grid-column:1/-1;display:grid;grid-template-columns:repeat(7,1fr);">{head}</div>' + "".join(cells_html) + "</div>"
        st.markdown(grid_html, unsafe_allow_html=True)
    st.caption("今日以蓝底圆点标出；事件条左侧颜色表示优先级（红=高，蓝=普通，灰=低）。")
