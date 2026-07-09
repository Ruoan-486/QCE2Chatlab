# QCE2ChatLab - QQ聊天记录自动同步工具
# Copyright (c) 2026 Ruoan
# 许可证: 自定义非商用 NC-BY-SA 协议（详见 LICENSE）
# 禁止商用 · 强制保留署名 · 衍生项目必须附带相同协议


#!/usr/bin/env python3
"""
QCE2ChatLab - QQ聊天记录自动同步工具
从 QCE (QQ Chat Exporter) 自动导出并导入到 ChatLab
"""

import json
import os
import sys
import time
import calendar
import tempfile
import threading
import subprocess
import webbrowser
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

# ── 配置 ──────────────────────────────────────────────────

APP_DIR = Path(os.environ.get("QCE2CHATLAB_HOME", Path.home() / ".qce2chatlab"))
CONFIG_FILE = APP_DIR / "config.json"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "sync.log"

DEFAULT_CONFIG = {
    "qce": {
        "base_url": "http://127.0.0.1:40653",
        "token": "",
        "export_dir": "",
        "startup_script": "",
        "startup_port": 40653,
        "qq_startup_script": "",
    },
    "chatlab": {
        "base_url": "http://127.0.0.1:3110",
        "token": "",
        "startup_script": "",
        "startup_port": 3110,
    },
    "sync": {
        "auto_sync_enabled": False,
        "schedule_type": "daily",       # daily / weekly / monthly
        "schedule_weekdays": [1],        # 每周几（1=周一）
        "schedule_day": 1,              # 每月几号
        "schedule_hour": 0,             # 几点执行（0-23）
        "schedule_minute": 0,           # 几分执行（0-59）
        "peers": [],
        "format": "json",
        "time_range_days": 7,
        "file_prefix": "",
        "file_include_date": True,
        "file_include_seq": True,
        "export_media": True,
    },
    "app": {
        "autostart": False,
        "install_dir": "",
        "port": 15520,
    },
}

app = FastAPI(title="QCE2ChatLab", version="1.0.0")

# ── 工具函数 ──────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(config: dict):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_sync": {}, "sync_history": []}

def save_state(state: dict):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def get_qce_headers(config: dict) -> dict:
    token = config.get("qce", {}).get("token", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}

def get_chatlab_headers(config: dict) -> dict:
    token = config.get("chatlab", {}).get("token", "")
    if not token:
        raise HTTPException(status_code=400, detail="请先配置 ChatLab API Token")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Windows 开机自启 ──────────────────────────────────────

def _get_startup_shortcut_path() -> Path:
    """获取 Windows 启动文件夹中的快捷方式路径"""
    startup = os.environ.get("APPDATA", "")
    if not startup:
        return Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/QCE2ChatLab.lnk"
    return Path(startup) / "Microsoft/Windows/Start Menu/Programs/Startup/QCE2ChatLab.bat"


def _get_app_exe_path() -> Path:
    """当前 Python 解释器路径或打包后的 exe 路径"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable)
    return Path(sys.executable)


_autostart_timer: threading.Timer | None = None


def enable_autostart(config: dict):
    """写入启动文件夹批处理"""
    shortcut = _get_startup_shortcut_path()
    shortcut.parent.mkdir(parents=True, exist_ok=True)

    exe_path = _get_app_exe_path()
    work_dir = Path(config["app"].get("install_dir", "")) if config["app"].get("install_dir") else exe_path.parent
    port = config["app"].get("port", 15520)

    # 写一个 bat 文件（更可靠，且可以静默启动）
    bat_content = f'''@echo off
cd /d "{work_dir}"
start "" /min "{exe_path}" --port {port} --no-browser
'''
    shortcut.write_text(bat_content, encoding="utf-8")
    log(f"开机自启已启用: {shortcut}")


def disable_autostart():
    """移除启动文件夹快捷方式"""
    shortcut = _get_startup_shortcut_path()
    if shortcut.exists():
        shortcut.unlink()
        log(f"开机自启已禁用")
    # 也尝试删除 .lnk 版本
    lnk = shortcut.with_suffix(".lnk")
    if lnk.exists():
        lnk.unlink()


def is_autostart_enabled() -> bool:
    return _get_startup_shortcut_path().exists()


# ── 定时同步调度器 ────────────────────────────────────────

_schedule_timer: threading.Timer | None = None
_schedule_running = False


import calendar

def _next_run_seconds(config: dict) -> int:
    """计算距离下次执行还有多少秒"""
    sync = config.get("sync", {})
    now = time.localtime()
    s_type = sync.get("schedule_type", "daily")
    s_hour = int(sync.get("schedule_hour", 0))
    s_minute = int(sync.get("schedule_minute", 0))

    def _secs_until(weekday: int | None = None, day: int | None = None) -> int:
        """计算到下一个指定时间点的秒数"""
        target = time.struct_time((
            now.tm_year, now.tm_mon, now.tm_mday,
            s_hour, s_minute, 0,
            now.tm_wday, now.tm_yday, now.tm_isdst
        ))
        target_ts = time.mktime(target)
        now_ts = time.mktime(now)

        if target_ts <= now_ts:
            target_ts += 86400  # 明天

        # 按类型调整
        if weekday is not None:
            # 找到下一个指定星期几
            # weekday 已是 0-6（配置中 1=周一 → 已转换为 0）
            while True:
                t = time.localtime(target_ts)
                if t.tm_wday == weekday:
                    break
                target_ts += 86400
        elif day is not None:
            # 找到下一个指定日
            while True:
                t = time.localtime(target_ts)
                if t.tm_mday == day:
                    break
                target_ts += 86400

        return int(target_ts - now_ts)

    log(f"[调度] 类型={s_type}, 时={s_hour}:{s_minute}, now={time.strftime('%Y-%m-%d %H:%M:%S',now)}")
    if s_type == "weekly":
        weekdays = sync.get("schedule_weekdays", [1])
        if weekdays:
            # 配置存的是 1=周一 ~ 7=周日，转为 0=周一 ~ 6=周日
            wdays_0based = [w - 1 for w in weekdays if 1 <= w <= 7]
            if not wdays_0based:
                wdays_0based = [0]
            secs = min(_secs_until(w) for w in wdays_0based)
            log(f"[调度] 每周{weekdays}: 下次执行 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()+secs))} ({secs}秒后)")
            return secs
        return _secs_until(weekday=0)  # 默认周一
    elif s_type == "monthly":
        s_day = int(sync.get("schedule_day", 1))
        # 如果指定日超过当月天数，用当月最后一天
        last_day = calendar.monthrange(now.tm_year, now.tm_mon)[1]
        if s_day > last_day:
            s_day = last_day
        secs = _secs_until(day=s_day)
        log(f"[调度] 每月{s_day}号: 下次执行 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()+secs))} ({secs}秒后)")
        return secs
    else:
        # daily
        secs = _secs_until()
        log(f"[调度] 下次执行: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()+secs))} (还有{secs}秒)")
        return secs


def start_schedule(config: dict):
    """启动定时同步心跳检测（每60秒检查是否到点）"""
    global _schedule_timer, _schedule_running

    def _heartbeat():
        """每60秒检查是否该同步了"""
        global _schedule_timer
        if not _schedule_running:
            return
        try:
            cfg = load_config()
            state_now = load_state()
            sync = cfg.get("sync", {})
            if not sync.get("auto_sync_enabled"):
                _schedule_running = False
                return

            last_sync_at = state_now.get("last_sync_at", 0)
            now_ts = int(time.time())

            # 计算今天的应执行时间戳
            now_local = time.localtime()
            s_hour = int(sync.get("schedule_hour", 0))
            s_minute = int(sync.get("schedule_minute", 0))
            today_target_ts = int(time.mktime(time.struct_time((
                now_local.tm_year, now_local.tm_mon, now_local.tm_mday,
                s_hour, s_minute, 0, 0, 0, -1))))

            should_sync = False
            s_type = sync.get("schedule_type", "daily")

            if s_type == "daily":
                # 今天的应执行时间已过，且上次同步早于它
                if today_target_ts <= now_ts and last_sync_at < today_target_ts:
                    should_sync = True

            elif s_type == "weekly":
                weekdays = sync.get("schedule_weekdays", [1])
                wdays = [w - 1 for w in weekdays if 1 <= w <= 7]
                if now_local.tm_wday in wdays:
                    if today_target_ts <= now_ts and last_sync_at < today_target_ts:
                        should_sync = True

            elif s_type == "monthly":
                s_day = int(sync.get("schedule_day", 1))
                import calendar
                last_day = calendar.monthrange(now_local.tm_year, now_local.tm_mon)[1]
                if s_day > last_day:
                    s_day = last_day
                # 只在当天执行
                if now_local.tm_mday == s_day:
                    if today_target_ts <= now_ts and last_sync_at < today_target_ts:
                        should_sync = True

            if should_sync:
                log(f"⏰ 定时同步触发 (类型={s_type})")
                _add_sync_log_internal("========== 定时同步 ==========")
                run_sync_sync(cfg, state_now, source="auto")
                _add_sync_log_internal("定时同步完成")
                log(f"下次同步: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + _next_run_seconds(load_config())))}")
        except Exception as e:
            log(f"⏰ 心跳异常: {e}")
        # 60秒后继续心跳
        _schedule_timer = threading.Timer(60, _heartbeat)
        _schedule_timer.daemon = True
        _schedule_timer.start()

    stop_schedule()
    _schedule_running = True
    s_type = config.get("sync", {}).get("schedule_type", "daily")
    s_hour = config.get("sync", {}).get("schedule_hour", 0)
    s_minute = config.get("sync", {}).get("schedule_minute", 0)
    # 立即计算下次执行时间并记录
    secs = _next_run_seconds(config)
    weekday_names = ["周一","周二","周三","周四","周五","周六","周日"]
    time_str = f"{s_hour:02d}:{s_minute:02d}"
    desc = f"每天 {time_str}"
    if s_type == "weekly":
        weekdays = config.get("sync", {}).get("schedule_weekdays", [1])
        day_names = [weekday_names[w-1] if 1<=w<=7 else f"周{w}" for w in weekdays]
        desc = f"每周{'、'.join(day_names)} {time_str}"
    elif s_type == "monthly":
        s_day = config.get("sync", {}).get("schedule_day", 1)
        desc = f"每月{s_day}号 {time_str}"
    log(f"定时同步已启动: {desc}")
    log(f"下次执行: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + secs))} ({secs}秒后)")

    # 60秒后开始心跳
    _schedule_timer = threading.Timer(60, _heartbeat)
    _schedule_timer.daemon = True
    _schedule_timer.start()

    # 独立线程检测漏同步
    threading.Thread(target=_check_missed_syncs, args=(config, load_state()), daemon=True).start()
    _start_missed_sync_watchdog()


def _check_missed_syncs(config: dict, state: dict):
    """检测是否有漏掉的定时同步（例如电脑关机错过时间）"""
    if not config.get("sync", {}).get("auto_sync_enabled"):
        return

    last_sync_at = state.get("last_sync_at", 0)
    changed_at = config.get("app", {}).get("schedule_changed_at", 0)
    # 只关心设置变更之后漏掉的同步
    effective_last = max(last_sync_at, changed_at) if changed_at else last_sync_at

    s_type = config.get("sync", {}).get("schedule_type", "daily")
    s_hour = int(config.get("sync", {}).get("schedule_hour", 0))
    s_minute = int(config.get("sync", {}).get("schedule_minute", 0))
    now = time.time()

    # 计算上一次应执行的时间
    if s_type == "daily":
        # 取今天的执行时间
        today_target = time.mktime(time.struct_time(time.localtime()[:3] + (s_hour, s_minute, 0, 0, 0, -1)))
        # 如果今天还没到执行时间，看上一天的
        if today_target > now:
            check_time = today_target - 86400
        else:
            check_time = today_target
        # 如果上次同步在 check_time 之前，说明漏了
        if effective_last < check_time:
            log(f"发现漏同步: 上次同步 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_sync_at))}, 应执行于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(check_time))}")
            _add_sync_log_internal("检测到漏同步，正在自动补同步...")
            config_now = load_config()
            state_now = load_state()
            try:
                run_sync_sync(config_now, state_now, source="auto")
            except Exception as e:
                _add_sync_log_internal(f"补同步异常: {e}")

    elif s_type == "weekly":
        weekdays = config.get("sync", {}).get("schedule_weekdays", [1])
        if weekdays:
            wdays = [w - 1 for w in weekdays if 1 <= w <= 7]  # 转 0-6
            # 检查过去7天里有没有应执行的日子
            for days_ago in range(1, 8):
                past = now - days_ago * 86400
                pt = time.localtime(past)
                if pt.tm_wday in wdays:
                    # 这个日子是应执行日，检查是否已经同步过
                    past_target = time.mktime(time.struct_time((pt.tm_year, pt.tm_mon, pt.tm_mday, s_hour, s_minute, 0, 0, 0, -1)))
                    if effective_last < past_target:
                        log(f"发现漏同步: 上周 {['周一','周二','周三','周四','周五','周六','周日'][pt.tm_wday]}")
                        _add_sync_log_internal("检测到漏同步，正在自动补同步...")
                        config_now = load_config()
                        state_now = load_state()
                        try:
                            run_sync_sync(config_now, state_now, source="auto")
                        except:
                            pass
                        break

    elif s_type == "monthly":
        # 检查上个月应执行日
        import calendar
        last_month = (time.localtime().tm_mon - 2) % 12 + 1  # 上个月
        last_month_year = time.localtime().tm_year if last_month < time.localtime().tm_mon else time.localtime().tm_year - 1
        last_day = calendar.monthrange(last_month_year, last_month)[1]
        s_day = min(int(config.get("sync", {}).get("schedule_day", 1)), last_day)
        past_target = time.mktime(time.struct_time((last_month_year, last_month, s_day, s_hour, s_minute, 0, 0, 0, -1)))
        if effective_last == 0 or effective_last < past_target:
            log(f"发现漏同步: 上个月{s_day}号")
            _add_sync_log_internal("检测到漏同步，正在自动补同步...")
            config_now = load_config()
            state_now = load_state()
            try:
                run_sync_sync(config_now, state_now, source="auto")
            except:
                pass


_missed_watchdog_timer: threading.Timer | None = None


def _start_missed_sync_watchdog():
    """每60分钟检测一次漏同步"""
    global _missed_watchdog_timer
    if _missed_watchdog_timer:
        _missed_watchdog_timer.cancel()
    _missed_watchdog_timer = threading.Timer(3600, _missed_sync_watchdog)
    _missed_watchdog_timer.daemon = True
    _missed_watchdog_timer.start()


def _missed_sync_watchdog():
    """60分钟定时器回调"""
    try:
        config = load_config()
        state = load_state()
        _check_missed_syncs(config, state)
    except Exception as e:
        log(f"漏同步检测异常: {e}")
    # 继续循环
    _start_missed_sync_watchdog()


def stop_schedule():
    global _schedule_timer, _schedule_running
    _schedule_running = False
    if _schedule_timer:
        _schedule_timer.cancel()
        _schedule_timer = None
        log("定时同步已停止")


# ── QQ 进程管理 ──────────────────────────────────────────
_qq_decision_event = threading.Event()
_qq_decision: dict | None = None  # {"action":"sync_now"|"delay"|"cancel", "delay_minutes": int}
_pending_sync_config: dict | None = None
_pending_sync_state: dict | None = None


def _find_qq_exe(config: dict) -> str | None:
    """从配置或常见路径找 QQ.exe"""
    script = config.get("qce", {}).get("qq_startup_script", "")
    if script and Path(script).exists():
        return script
    # 常见路径
    candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files"), "Tencent", "QQ", "Bin", "QQ.exe"),
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"), "Tencent", "QQ", "Bin", "QQ.exe"),
        Path.home() / "AppData/Local/Tencent/QQ/Bin/QQ.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _is_qq_running() -> bool:
    """检测 QQ 进程是否在运行"""
    try:
        if sys.platform != "win32":
            return False
        result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
                                capture_output=True, text=True, timeout=5)
        return "QQ.exe" in result.stdout
    except Exception:
        return False


def _kill_qq() -> bool:
    """终止 QQ 进程"""
    try:
        subprocess.run(["taskkill", "/F", "/IM", "QQ.exe"], capture_output=True, timeout=10)
        time.sleep(2)  # 等进程完全退出
        return True
    except Exception as e:
        log(f"终止 QQ 进程失败: {e}")
        return False


def _launch_qq(config: dict):
    """启动 QQ"""
    path = _find_qq_exe(config)
    if not path:
        log("⚠ 找不到 QQ.exe，请在系统页设置 QQ 启动脚本路径")
        return
    try:
        subprocess.Popen([path], shell=True, close_fds=True)
        log(f"已启动 QQ: {path}")
    except Exception as e:
        log(f"启动 QQ 失败: {e}")


# ── 同步逻辑 ──────────────────────────────────────────────

SYNC_LOCK = threading.Lock()
_sync_running = False
_sync_progress = {"status": "idle", "current": "", "detail": "", "log": []}


def _add_sync_log_internal(msg: str):
    _sync_progress["log"].append(msg)
    if len(_sync_progress["log"]) > 200:
        _sync_progress["log"] = _sync_progress["log"][-200:]
    log(msg)


def _convert_qce_to_chatlab(qce_json: dict, peer_info: dict) -> dict:
    """将 QCE JSON 转为 ChatLab Format"""
    chat_info = qce_json.get("chatInfo", {})
    messages = qce_json.get("messages", [])
    statistics = qce_json.get("statistics", {})

    chat_type = peer_info.get("type", "private")
    peer_name = peer_info.get("name", "")
    peer_uid = peer_info.get("uid", "")

    # 从 chatInfo 获取真实会话名
    display_name = chat_info.get("name", "")
    if not display_name:
        display_name = chat_info.get("peerName", chat_info.get("nick", ""))
    if not display_name or display_name == peer_uid:
        display_name = peer_name or peer_uid

    # 构建 UID → 用户名映射
    uid_to_name = {}
    self_uid = str(chat_info.get("selfUid", ""))
    self_name = chat_info.get("selfName", chat_info.get("selfNick", ""))
    if self_uid:
        uid_to_name[self_uid] = self_name or self_uid

    for s in statistics.get("senders", []):
        if not isinstance(s, dict):
            continue
        uid = str(s.get("uid", ""))
        if uid:
            uid_to_name[uid] = s.get("name", s.get("uin", uid))

    # peer 名字
    if peer_uid and not uid_to_name.get(peer_uid):
        uid_to_name[peer_uid] = display_name

    chatlab_data = {
        "chatlab": {"version": "0.0.2", "exportedAt": int(time.time()), "generator": "QCE2ChatLab/1.0"},
        "meta": {"name": display_name, "platform": "qq", "type": "group" if chat_type == "group" else "private", "ownerId": self_uid if self_uid else ""},
        "members": [],
        "messages": [],
    }
    if chat_type == "group":
        chatlab_data["meta"]["groupId"] = peer_uid

    # 提取头像 base64（QCE embedAvatarsAsBase64 导出时才有）
    avatars_list = qce_json.get("avatars", [])
    if not isinstance(avatars_list, list):
        avatars_list = []
    avatars_map = {}
    for av in avatars_list:
        if not isinstance(av, dict):
            continue
        av_uid = str(av.get("uin", av.get("uid", "")))
        av_b64 = av.get("base64", av.get("avatar", ""))
        if av_uid and av_b64:
            avatars_map[av_uid] = av_b64

    for uid, name in uid_to_name.items():
        member_entry = {"platformId": uid, "accountName": name}
        if uid in avatars_map:
            mime = "image/png"
            if avatars_map[uid].startswith("/9j/"):
                mime = "image/jpeg"
            member_entry["avatar"] = f"data:{mime};base64,{avatars_map[uid]}"
        chatlab_data["members"].append(member_entry)

    for msg in messages:
        # sender: 可能是对象 {uid, name} 或纯字符串
        s = msg.get("sender", "")
        if isinstance(s, dict):
            sender_id = str(s.get("uid", s.get("uin", "")))
        else:
            sender_id = str(s) if s else ""

        s_name = ""
        if isinstance(s, dict):
            s_name = s.get("name", s.get("nick", ""))

        if not sender_id:
            sender_id = str(msg.get("senderUid", msg.get("account", "")))
        if not s_name:
            s_name = uid_to_name.get(sender_id, sender_id or "SYSTEM")

        # 补充 members
        if sender_id and sender_id not in uid_to_name:
            chatlab_data["members"].append({"platformId": sender_id, "accountName": s_name})
            uid_to_name[sender_id] = s_name

        ts = msg.get("timestamp", 0)
        if ts > 1e12:
            ts = int(ts / 1000)
        else:
            ts = int(ts)

        # 提取纯文本
        c = msg.get("content", "")
        text = ""
        if isinstance(c, dict):
            text = c.get("text", "")
            if not text:
                # elements 里的 text
                for el in c.get("elements", []):
                    if el.get("type") == "text":
                        d = el.get("data", {}) if isinstance(el.get("data"), dict) else {}
                        text += d.get("text", d.get("content", ""))
        elif isinstance(c, str):
            text = c
        elif c is not None:
            text = str(c)

        if not text:
            text = "[消息]"

        mid = str(msg.get("msgId", msg.get("id", msg.get("seq", ""))))
        if not mid:
            import hashlib
            mid = hashlib.md5(f"{ts}_{sender_id}_{text[:20]}".encode()).hexdigest()[:16]

        chatlab_data["messages"].append({
            "platformMessageId": mid,
            "sender": sender_id or "SYSTEM",
            "accountName": s_name or "SYSTEM",
            "timestamp": ts,
            "type": 0,
            "content": text,
        })

    return chatlab_data, len(avatars_map)


def _append_sync_history(state: dict, peer: dict, status: str, messages: int,
                         error: str | None, time_range_days: int,
                         sync_start_ts: int, elapsed: int,
                         session_id: str = ""):
    """记录单次同步历史到 state（兼容旧格式）"""
    now_ts = int(time.time())
    elapsed = elapsed or (now_ts - sync_start_ts)
    state.setdefault("sync_history", []).append({
        "time": now_ts,
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
        "weekday": ["周一","周二","周三","周四","周五","周六","周日"][time.localtime(now_ts).tm_wday],
        "peer": peer.get("name", peer.get("uid", "?")),
        "peer_uid": peer.get("uid", ""),
        "status": status,
        "messages": messages,
        "time_range": f"最近{time_range_days}天",
        "time_range_days": time_range_days,
        "duration_seconds": elapsed,
        "session_id": session_id or str(sync_start_ts),
        "source": "auto" if session_id.startswith("auto_") else "manual",
    })
    if error:
        state["sync_history"][-1]["error"] = error
    if len(state["sync_history"]) > 500:
        state["sync_history"] = state["sync_history"][-500:]


def _save_sync_session(state: dict, results: dict, sync_start_ts: int,
                        time_range_days: int, source: str = "manual"):
    """将一次同步会话的所有peer结果保存为一条汇总记录"""
    now_ts = int(time.time())
    elapsed = now_ts - sync_start_ts
    session_id = f"{source}_{sync_start_ts}"
    total_msgs = sum(r.get("messages", 0) for r in results.get("success", []))
    peer_results = []
    for r in results.get("success", []):
        peer_results.append({"name": r["peer"], "status": "成功", "messages": r.get("messages", 0)})
    for r in results.get("failed", []):
        peer_results.append({"name": r["peer"], "status": "失败", "error": r.get("error", "")})
    all_ok = len(results.get("failed", [])) == 0
    state.setdefault("sync_history", []).append({
        "session_id": session_id,
        "time": sync_start_ts,
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sync_start_ts)),
        "weekday": ["周一","周二","周三","周四","周五","周六","周日"][time.localtime(sync_start_ts).tm_wday],
        "status": "成功" if all_ok else "部分失败",
        "source": source,
        "messages": total_msgs,
        "time_range": f"最近{time_range_days}天",
        "time_range_days": time_range_days,
        "duration_seconds": elapsed,
        "peer_count": len(peer_results),
        "success_count": len(results.get("success", [])),
        "failed_count": len(results.get("failed", [])),
        "peer_results": peer_results,
    })
    if len(state["sync_history"]) > 500:
        state["sync_history"] = state["sync_history"][-500:]


def _check_service(url: str, port: int, timeout: float = 3) -> bool:
    """通过 HTTP 请求检查服务是否已启动"""
    try:
        r = httpx.get(url, timeout=timeout)
        return True
    except Exception:
        return False


def _ensure_services_running(config: dict):
    """自动检测并启动 QCE、ChatLab；若 QCE 运行但 QQ 未运行则重启 QCE"""
    qce = config.get("qce", {})
    chatlab = config.get("chatlab", {})

    qce_url = qce.get("base_url", "http://127.0.0.1:40653")
    qce_script = qce.get("startup_script", "")
    qce_port = int(qce.get("startup_port", 40653))

    chatlab_url = chatlab.get("base_url", "http://127.0.0.1:3110")
    chatlab_script = chatlab.get("startup_script", "")
    chatlab_port = int(chatlab.get("startup_port", 3110))


    def _launch_script(script_path: str, name: str, check_url: str, check_port: int):
        """通用：启动脚本并等待服务响应"""
        if not script_path:
            log(f"⚠ {name} 未运行但未配置启动脚本")
            return False
        log(f"{name} 未响应，尝试启动: {script_path}")
        try:
            sp = str(Path(script_path))
            subprocess.Popen(sp, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log(f"已启动 {name}，等待 8 秒...")
            for _ in range(16):
                time.sleep(0.5)
                if _check_service(check_url, check_port):
                    log(f"{name} 启动成功")
                    return True
            log(f"⚠ {name} 启动后仍未响应，继续尝试同步")
            return False
        except Exception as e:
            log(f"⚠ 启动 {name} 失败: {e}")
            return False


    # 检查 QCE
    qce_ok = _check_service(qce_url, qce_port)
    if qce_ok:
        # QCE 在运行，检查 QQ 是否也在运行
        if _is_qq_running():
            log("QCE + QQ 均在运行")
        else:
            log("⚠ QCE 在运行但 QQ 已退出，QQ 可能需重新登录，重启 QCE...")
            # 先杀 QCE 进程？用户用的是 QCE.exe 吗？直接重新执行启动脚本
            # QCE 启动时自动拉起 QQ
            _launch_script(qce_script, "QCE", qce_url, qce_port)
    else:
        # QCE 未运行，尝试启动
        _launch_script(qce_script, "QCE", qce_url, qce_port)

    # 检查 ChatLab
    chatlab_ok = _check_service(chatlab_url, chatlab_port)
    if not chatlab_ok:
        _launch_script(chatlab_script, "ChatLab", chatlab_url, chatlab_port)


def run_sync_sync(config: dict, state: dict, source: str = "manual") -> dict:
    global _sync_running, _sync_progress, _qq_decision, _qq_decision_event, _pending_sync_config, _pending_sync_state

    # 先检测 QCE 是否正常运行
    qce = config.get("qce", {})
    qce_url = qce.get("base_url", "http://127.0.0.1:40653")
    qce_port = int(qce.get("startup_port", 40653))
    qce_script = qce.get("startup_script", "")
    qq_script = qce.get("qq_startup_script", "")

    qce_ok = _check_service(qce_url, qce_port)
    qq_running = _is_qq_running()

    _add_sync_log_internal(f"状态检查: QCE={'✅' if qce_ok else '❌'} QQ={'✅' if qq_running else '❌'}")

    if qce_ok and qq_running:
        # QCE+QQ 都正常运行，直接同步，无需任何操作
        _add_sync_log_internal("QCE 与 QQ 均正常运行，直接同步")

    elif qce_ok and not qq_running:
        # QCE 在运行但 QQ 已退出，重启 QCE（QCE 会自动拉 QQ）
        _add_sync_log_internal("QCE 在运行但 QQ 已退出，自动重启 QCE...")
        if qce_script:
            _ensure_services_running(config)
        else:
            _add_sync_log_internal("⚠ 未配置 QCE 启动脚本")

    elif not qce_ok:
        # QCE 没在运行，需要先启动 QCE。如果 QQ 在运行则弹窗询问
        if qq_running:
            if qq_script or qce_script:
                _add_sync_log_internal("QCE 未运行且 QQ 正在运行，需要弹窗询问用户")
                _pending_sync_config = config
                _pending_sync_state = state
                _qq_decision = None
                _qq_decision_event.clear()
                _sync_progress["status"] = "awaiting_qq"
                _sync_progress["detail"] = "QCE 未运行且 QQ 正在运行，请选择处理方式"

                waited = _qq_decision_event.wait(timeout=600)
                if not waited:
                    _add_sync_log_internal("⏰ 等待用户决策超时，同步取消")
                    _sync_progress["status"] = "idle"
                    return {"success": [], "failed": [], "cancelled": True}

                decision = _qq_decision or {}
                action = decision.get("action", "cancel")

                if action == "cancel":
                    _add_sync_log_internal("用户取消了同步")
                    _sync_progress["status"] = "idle"
                    return {"success": [], "failed": [], "cancelled": True}

                elif action == "delay":
                    delay_min = int(decision.get("delay_minutes", 5))
                    _add_sync_log_internal(f"用户选择延时 {delay_min} 分钟后同步")
                    _sync_progress["status"] = "delayed"
                    _sync_progress["detail"] = f"将在 {delay_min} 分钟后重试"

                    def _delayed_retry():
                        time.sleep(delay_min * 60)
                        run_sync_sync(config, state, source=source)

                    threading.Thread(target=_delayed_retry, daemon=True).start()
                    return {"success": [], "failed": [], "delayed": True, "delay_minutes": delay_min}

                elif action == "sync_now":
                    _add_sync_log_internal("用户选择立即同步，终止 QQ 进程...")
                    _kill_qq()
                    # 启动 QCE（会自动拉 QQ）
                    if qce_script:
                        _ensure_services_running(config)
            else:
                _add_sync_log_internal("⚠ 未配置 QCE 和 QQ 启动脚本，跳过同步")
                _sync_progress["status"] = "idle"
                return {"success": [], "failed": [], "cancelled": True}
        else:
            # QQ 也没运行，直接启动 QCE
            _add_sync_log_internal("QCE 与 QQ 均未运行，自动启动 QCE...")
            if qce_script:
                _ensure_services_running(config)
            else:
                _add_sync_log_internal("⚠ 未配置 QCE 启动脚本，跳过同步")
                _sync_progress["status"] = "idle"
                return {"success": [], "failed": [], "cancelled": True}

    # 执行实际同步
    result = _run_full_sync(config, state, source=source)

    return result


def _run_full_sync(config: dict, state: dict, source: str = "manual") -> dict:
    """实际的同步逻辑（不含 QQ 检测部分）"""
    global _sync_running, _sync_progress
    results = {"success": [], "failed": []}
    peers = config.get("sync", {}).get("peers", [])
    sync_start_ts = int(time.time())

    # 自动检测并启动 QCE 和 ChatLab
    _ensure_services_running(config)

    if not peers:
        _add_sync_log_internal("没有配置要同步的会话")
        return results

    qce_base = config["qce"]["base_url"]
    qce_headers = get_qce_headers(config)
    chatlab_base = config["chatlab"]["base_url"]
    chatlab_headers = get_chatlab_headers(config)
    sync_config = config.get("sync", {})
    time_range_days = sync_config.get("time_range_days", 7)
    export_format = sync_config.get("format", "json")

    for peer in peers:
        peer_name = peer.get("name", peer.get("uid", "unknown"))
        peer_uid = peer.get("uid", "")
        peer_type = peer.get("type", "friend")

        _sync_progress["current"] = peer_name
        _sync_progress["detail"] = "正在导出..."
        _add_sync_log_internal(f"开始同步: {peer_name}")

        try:
            # 序号：每个用户独立计数
            seq_num = int(state.get("seq_counter", {}).get(peer_uid, 0)) + 1
            state.setdefault("seq_counter", {})[peer_uid] = seq_num

            # 文件名生成
            file_cfg = sync_config
            prefix = file_cfg.get("file_prefix", "")
            inc_date = file_cfg.get("file_include_date", True)
            inc_seq = file_cfg.get("file_include_seq", True)
            export_media = file_cfg.get("export_media", True)

            name_parts = []
            if prefix:
                name_parts.append(prefix)
            if inc_date:
                name_parts.append(time.strftime("%Y-%m-%d"))
            if inc_seq:
                name_parts.append(str(seq_num).zfill(3))
            name_parts.append(peer_name)
            custom_filename = "_".join(name_parts) + f".{export_format}"
            # 清理非法文件名
            custom_filename = custom_filename.replace("/", "_").replace("\\", "_").replace(":", "_")

            export_params = {
                "peer": {
                    "chatType": 2 if peer_type == "group" else 1,
                    "peerUid": peer_uid,
                },
                "format": export_format,
                "timeRangeType": "recent",
                "timeRangeDays": time_range_days,
                "exportMedia": export_media,
                "downloadMedia": export_media,
                "options": {
                    "embedAvatarsAsBase64": True,
                },
                "fileName": custom_filename,
            }

            resp = httpx.post(
                f"{qce_base}/api/messages/export",
                headers={**qce_headers, "Content-Type": "application/json"},
                json=export_params,
                timeout=30,
            )
            task_data = resp.json()

            if not task_data.get("success"):
                _add_sync_log_internal(f"  ❌ QCE 导出请求失败: {task_data}")
                _append_sync_history(state, peer, "失败", 0, "QCE 导出请求失败",
                                    time_range_days, sync_start_ts, 0)
                results["failed"].append({"peer": peer_name, "error": "导出请求失败"})
                continue

            task_id = task_data.get("data", {}).get("taskId", "")
            if not task_id:
                _add_sync_log_internal(f"  ❌ 未获取到任务ID")
                _append_sync_history(state, peer, "失败", 0, "未获取到任务ID",
                                    time_range_days, sync_start_ts, 0)
                results["failed"].append({"peer": peer_name, "error": "未获取到任务ID"})
                continue

            _add_sync_log_internal(f"  QCE 任务ID: {task_id}")

            # 等待导出完成
            download_url = None
            export_file_name = None
            max_wait = 600
            wait_interval = 2
            waited = 0
            task_status = ""
            while waited < max_wait:
                time.sleep(wait_interval)
                waited += wait_interval
                try:
                    task_resp = httpx.get(f"{qce_base}/api/tasks/{task_id}", headers=qce_headers, timeout=15)
                    td = task_resp.json().get("data", task_resp.json())
                except Exception as e:
                    _add_sync_log_internal(f"  ⚠️ 查询任务状态失败: {e}")
                    continue

                task_status = td.get("status", "")
                progress = td.get("progress", 0)
                if progress:
                    _sync_progress["detail"] = f"导出中... {progress}%"

                if task_status == "completed":
                    download_url = td.get("downloadUrl", "")
                    export_file_name = td.get("fileName", "")
                    break
                elif task_status == "failed" or td.get("error"):
                    _add_sync_log_internal(f"  ❌ 导出失败: {td.get('error','未知')}")
                    results["failed"].append({"peer": peer_name, "error": td.get("error", "未知")})
                    break

            if not download_url or task_status == "failed":
                if task_status != "failed":
                    _add_sync_log_internal(f"  ⚠️ 导出超时")
                    _append_sync_history(state, peer, "失败", 0, "导出超时",
                                         time_range_days, sync_start_ts, 0)
                    results["failed"].append({"peer": peer_name, "error": "导出超时"})
                else:
                    _append_sync_history(state, peer, "失败", 0, "QCE 导出任务失败",
                                         time_range_days, sync_start_ts, 0)
                continue

            _add_sync_log_internal(f"  导出完成: {export_file_name}")

            # 从本地 QCE 导出目录读取文件（脚本在 Windows 上跑，直接读磁盘）
            _sync_progress["detail"] = "读取导出文件..."

            # QCE 默认导出目录：~/.qq-chat-exporter/exports/
            # 用户也可能自定义了路径
            qce_export_dir = Path.home() / ".qq-chat-exporter" / "exports"
            custom_dir = config.get("qce", {}).get("export_dir", "")
            if custom_dir:
                qce_export_dir = Path(custom_dir)

            export_file_path = qce_export_dir / export_file_name
            _add_sync_log_internal(f"  本地读取: {export_file_path}")

            if not export_file_path.exists():
                # 可能文件名不完全匹配，尝试模糊查找
                candidates = list(qce_export_dir.glob(f"*{task_id.split('_')[-1][:6]}*"))
                if candidates:
                    export_file_path = candidates[0]
                    _add_sync_log_internal(f"  模糊匹配: {export_file_path}")
                else:
                    _add_sync_log_internal(f"  ❌ 文件不存在: {export_file_path}")
                    _append_sync_history(state, peer, "失败", 0, "导出文件未找到",
                                         time_range_days, sync_start_ts, 0)
                    results["failed"].append({"peer": peer_name, "error": "导出文件未找到"})
                    continue

            raw_data = None
            try:
                raw_data = export_file_path.read_bytes()
                _add_sync_log_internal(f"  读取成功: {len(raw_data)} bytes")
            except Exception as e:
                _add_sync_log_internal(f"  ❌ 读取文件失败: {e}")
                _append_sync_history(state, peer, "失败", 0, f"读取失败: {e}",
                                     time_range_days, sync_start_ts, 0)
                results["failed"].append({"peer": peer_name, "error": f"读取失败: {e}"})
                continue

            # 解析下载内容
            try:
                qce_data = json.loads(raw_data.decode("utf-8"))
            except Exception:
                tmp_zip = os.path.join(tempfile.gettempdir(), f"qce2chatlab_{task_id}.zip")
                with open(tmp_zip, "wb") as f:
                    f.write(raw_data)
                _add_sync_log_internal(f"  ZIP 格式暂不支持: {tmp_zip}")
                _append_sync_history(state, peer, "失败", 0, "ZIP格式暂不支持",
                                     time_range_days, sync_start_ts, 0)
                results["failed"].append({"peer": peer_name, "error": "ZIP格式需手动解压"})
                continue

            _add_sync_log_internal(f"  读取到 {len(qce_data.get('messages',[]))} 条消息")

            # 转换格式
            _sync_progress["detail"] = "转换格式..."
            chatlab_data, avatar_count = _convert_qce_to_chatlab(qce_data, peer)
            _add_sync_log_internal(f"  转换完成: {len(chatlab_data['messages'])} 条消息, {len(chatlab_data['members'])} 个成员, {avatar_count} 个头像")

            # 分批导入（使用 imports/:sessionId 路由，首次自动创建会话）
            _sync_progress["detail"] = "导入 ChatLab..."
            total_msgs = len(chatlab_data["messages"])
            session_id = f"qq_{peer_uid}"
            batch_size = 5000

            import_error = None
            for i in range(0, total_msgs, batch_size):
                batch = chatlab_data["messages"][i:i + batch_size]
                is_first = (i == 0)

                import_body = {
                    "chatlab": chatlab_data["chatlab"],
                    "messages": batch,
                }
                if is_first:
                    import_body["meta"] = chatlab_data["meta"]
                    import_body["members"] = chatlab_data["members"]

                try:
                    # 调试：打印第一条消息样例
                    if is_first and batch:
                        sample = batch[0]
                        _add_sync_log_internal(f"  [DEBUG] 第一条消息: id={sample.get('platformMessageId','?')} sender={sample.get('sender','?')} name={sample.get('accountName','?')} type={sample.get('type','?')} content={repr(sample.get('content','?'))[:100]}")
                        _add_sync_log_internal(f"  [DEBUG] meta: {json.dumps(chatlab_data.get('meta',{}), ensure_ascii=False)[:200]}")
                        _add_sync_log_internal(f"  [DEBUG] members 数量: {len(chatlab_data.get('members',[]))}")
                        if chatlab_data.get("members"):
                            _add_sync_log_internal(f"  [DEBUG] 第一个成员: {json.dumps(chatlab_data['members'][0], ensure_ascii=False)}")

                    import_resp = httpx.post(
                        f"{chatlab_base}/api/v1/imports/{session_id}",
                        headers=chatlab_headers,
                        json=import_body,
                        timeout=120,
                    )
                    ir = import_resp.json()
                    if ir.get("success"):
                        created = ir.get("data", {}).get("created", False)
                        session_name = ir.get("data", {}).get("sessionId", session_id)
                        wc = ir.get("data", {}).get("batch", {}).get("writtenCount", len(batch))
                        dc = ir.get("data", {}).get("batch", {}).get("duplicateCount", 0)
                        flag = "🆕" if created else ""
                        _add_sync_log_internal(f"  ✅ 批次 {i//batch_size+1}: {flag} 写入 {wc} 条, 去重 {dc} 条, session={session_name}")
                    else:
                        err_msg = str(ir.get("error","导入返回失败"))
                        _add_sync_log_internal(f"  ❌ 导入失败: {err_msg}")
                        results["failed"].append({"peer": peer_name, "error": err_msg})
                        import_error = err_msg
                        break
                except Exception as e:
                    _add_sync_log_internal(f"  ❌ 导入失败: {e}")
                    results["failed"].append({"peer": peer_name, "error": str(e)})
                    import_error = str(e)
                    break

            now_ts = int(time.time())
            elapsed = now_ts - sync_start_ts
            state["last_sync"][peer_uid] = {"timestamp": now_ts, "name": peer_name}

            if import_error:
                # 导入失败——记录失败历史
                state.setdefault("sync_history", []).append({
                    "time": now_ts,
                    "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
                    "weekday": ["周一","周二","周三","周四","周五","周六","周日"][time.localtime(now_ts).tm_wday],
                    "peer": peer_name,
                    "peer_uid": peer_uid,
                    "status": "失败",
                    "messages": 0,
                    "time_range": f"最近{sync_config.get('time_range_days', 7)}天",
                    "time_range_days": sync_config.get('time_range_days', 7),
                    "duration_seconds": elapsed,
                    "error": import_error,
                })
                if len(state["sync_history"]) > 500:
                    state["sync_history"] = state["sync_history"][-500:]
                _add_sync_log_internal(f"  ❌ {peer_name} 导入失败: {import_error}")
                continue

            # 记录成功历史
            state.setdefault("sync_history", []).append({
                "time": now_ts,
                "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
                "weekday": ["周一","周二","周三","周四","周五","周六","周日"][time.localtime(now_ts).tm_wday],
                "peer": peer_name,
                "peer_uid": peer_uid,
                "status": "成功",
                "messages": total_msgs,
                "time_range": f"最近{sync_config.get('time_range_days', 7)}天",
                "time_range_days": sync_config.get('time_range_days', 7),
                "duration_seconds": elapsed,
            })
            if len(state["sync_history"]) > 500:
                state["sync_history"] = state["sync_history"][-500:]
            results["success"].append({"peer": peer_name, "messages": total_msgs})
            _add_sync_log_internal(f"  ✅ {peer_name} 同步完成")

        except Exception as e:
            now_ts = int(time.time())
            elapsed = now_ts - sync_start_ts
            import traceback
            err_tb = traceback.format_exc()
            state.setdefault("sync_history", []).append({
                "time": now_ts,
                "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
                "weekday": ["周一","周二","周三","周四","周五","周六","周日"][time.localtime(now_ts).tm_wday],
                "peer": peer_name,
                "peer_uid": peer_uid,
                "status": "失败",
                "messages": 0,
                "time_range": f"最近{sync_config.get('time_range_days', 7)}天",
                "time_range_days": sync_config.get('time_range_days', 7),
                "duration_seconds": elapsed,
                "error": str(e),
            })
            if len(state["sync_history"]) > 500:
                state["sync_history"] = state["sync_history"][-500:]
            _add_sync_log_internal(f"  ❌ {peer_name} 异常: {e}")
            results["failed"].append({"peer": peer_name, "error": str(e)})

    save_state(state)

    # 保存会话级汇总记录
    _save_sync_session(state, results, sync_start_ts,
                       sync_config.get('time_range_days', 7), source=source)
    save_state(state)

    # 更新全局最后同步时间
    state["last_sync_at"] = int(time.time())
    save_state(state)

    _sync_progress["status"] = "idle"
    _sync_progress["current"] = ""
    _sync_progress["detail"] = ""
    _sync_running = False
    _add_sync_log_internal(f"同步完成: 成功{len(results['success'])} / 失败{len(results['failed'])}")
    return results


# ── 前端页面 ──────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent / "frontend"


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = FRONTEND_DIR / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>QCE2ChatLab</h1><p>frontend missing</p>"


# ── 配置 API ──────────────────────────────────────────────


@app.get("/api/config")
async def api_get_config():
    return load_config()


class ConfigUpdate(BaseModel):
    config: dict


@app.post("/api/config")
async def api_save_config(body: ConfigUpdate):
    cfg = body.config
    old_cfg = load_config()
    # 检测同步设置是否改变，记录改变时间（用于漏同步检测）
    old_sync = old_cfg.get("sync", {})
    new_sync = cfg.get("sync", {})
    if (old_sync.get("schedule_type") != new_sync.get("schedule_type") or
        old_sync.get("schedule_hour") != new_sync.get("schedule_hour") or
        old_sync.get("schedule_minute") != new_sync.get("schedule_minute") or
        old_sync.get("schedule_weekdays") != new_sync.get("schedule_weekdays") or
        old_sync.get("schedule_day") != new_sync.get("schedule_day") or
        old_sync.get("auto_sync_enabled") != new_sync.get("auto_sync_enabled")):
        cfg.setdefault("app", {})["schedule_changed_at"] = int(time.time())
    save_config(cfg)

    # 开机自启
    if cfg.get("app", {}).get("autostart"):
        enable_autostart(cfg)
    else:
        disable_autostart()

    # 定时同步
    if cfg.get("sync", {}).get("auto_sync_enabled"):
        start_schedule(cfg)
    else:
        stop_schedule()

    return {"ok": True}


# ── 连接测试 ──────────────────────────────────────────────


class TestConnectionRequest(BaseModel):
    service: str


@app.post("/api/test-connection")
async def api_test_connection(body: TestConnectionRequest):
    config = load_config()
    if body.service == "qce":
        base_url = config["qce"]["base_url"]
        headers = get_qce_headers(config)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.get(f"{base_url}/health", headers=headers)
                return {"ok": resp.status_code == 200, "status": resp.status_code, "detail": "OK" if resp.status_code == 200 else f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    elif body.service == "chatlab":
        base_url = config["chatlab"]["base_url"]
        headers = get_chatlab_headers(config)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.get(f"{base_url}/api/v1/status", headers=headers)
                return {"ok": resp.status_code == 200, "status": resp.status_code, "detail": "OK" if resp.status_code == 200 else f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    return {"ok": False, "detail": "未知服务"}


# ── QCE API ───────────────────────────────────────────────


@app.get("/api/qce/friends")
async def api_qce_friends():
    config = load_config()
    base_url = config["qce"]["base_url"]
    headers = get_qce_headers(config)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(f"{base_url}/api/friends", headers=headers)
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/qce/groups")
async def api_qce_groups():
    config = load_config()
    base_url = config["qce"]["base_url"]
    headers = get_qce_headers(config)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(f"{base_url}/api/groups", headers=headers)
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/qce/export")
async def api_qce_export(body: dict):
    config = load_config()
    base_url = config["qce"]["base_url"]
    headers = get_qce_headers(config)
    export_params = {
        "peer": body.get("peer"),
        "format": body.get("format", "json"),
        "timeRangeType": body.get("timeRangeType", "recent"),
        "timeRangeDays": body.get("timeRangeDays", 7),
        "exportMedia": True,
        "downloadMedia": True,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(
                f"{base_url}/api/messages/export",
                headers={**headers, "Content-Type": "application/json"},
                json=export_params,
            )
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/qce/task/{task_id}")
async def api_qce_task_status(task_id: str):
    config = load_config()
    base_url = config["qce"]["base_url"]
    headers = get_qce_headers(config)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{base_url}/api/tasks/{task_id}", headers=headers)
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── ChatLab API ───────────────────────────────────────────


@app.get("/api/chatlab/sessions")
async def api_chatlab_sessions():
    config = load_config()
    base_url = config["chatlab"]["base_url"]
    headers = get_chatlab_headers(config)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{base_url}/api/v1/sessions", headers=headers)
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── 同步 API ──────────────────────────────────────────────


@app.post("/api/sync/start")
async def api_sync_start():
    global _sync_running, _sync_progress
    if _sync_running:
        return {"ok": False, "error": "同步正在进行中"}

    with SYNC_LOCK:
        _sync_running = True
        _sync_progress = {"status": "running", "current": "", "detail": "启动...", "log": []}

    config = load_config()
    state = load_state()
    _add_sync_log_internal("========== 开始同步 ==========")

    thread = threading.Thread(target=run_sync_sync, args=(config, state), daemon=True)
    thread.start()
    return {"ok": True}


class QQChoiceRequest(BaseModel):
    action: str  # sync_now / delay / cancel
    delay_minutes: int | None = 5


@app.post("/api/sync/qq-choice")
async def api_sync_qq_choice(body: QQChoiceRequest):
    global _qq_decision, _qq_decision_event, _sync_progress
    if body.action not in ("sync_now", "delay", "cancel"):
        return {"ok": False, "error": "无效的决策"}
    _qq_decision = {"action": body.action, "delay_minutes": body.delay_minutes or 5}
    _qq_decision_event.set()
    if body.action == "sync_now":
        _add_sync_log_internal("用户选择：立即同步")
    elif body.action == "delay":
        _add_sync_log_internal(f"用户选择：延时 {body.delay_minutes or 5} 分钟")
    else:
        _add_sync_log_internal("用户选择：取消同步")
    return {"ok": True}


@app.get("/api/sync/progress")
async def api_sync_progress():
    return _sync_progress


@app.get("/api/diagnose")
async def api_diagnose():
    """诊断文件写入和路径"""
    result = {
        "app_dir": str(APP_DIR),
        "config_file": str(CONFIG_FILE),
        "state_file": str(STATE_FILE),
        "log_file": str(LOG_FILE),
        "app_dir_exists": APP_DIR.exists(),
        "config_exists": CONFIG_FILE.exists(),
        "state_exists": STATE_FILE.exists(),
        "log_exists": LOG_FILE.exists(),
    }
    # 测试日志写入
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[诊断] {time.strftime('%Y-%m-%d %H:%M:%S')} 日志写入测试\n")
        result["log_write_ok"] = True
        # 检查写入后文件大小
        result["log_file_size"] = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    except Exception as e:
        result["log_write_ok"] = False
        result["log_write_error"] = str(e)
    return result


@app.get("/api/sync/history")
async def api_sync_history(limit: int = 100, source: str = ""):
    state = load_state()
    history = state.get("sync_history", [])
    if source:
        history = [h for h in history if h.get("source") == source]
    # 返回倒序（最新的在前）
    recent = list(reversed(history[-limit:]))
    # 补充增强字段
    for h in recent:
        # 将 time_str 和 weekday 合并为完整时间描述
        elapsed = h.get("duration_seconds", 0)
        if elapsed < 60:
            h["duration_display"] = f"{elapsed}秒"
        else:
            h["duration_display"] = f"{elapsed//60}分{elapsed%60}秒"
    return {"history": recent}


class DeleteHistoryRequest(BaseModel):
    timestamps: list[int]


@app.post("/api/sync/history/delete")
async def api_sync_history_delete(body: DeleteHistoryRequest):
    state = load_state()
    history = state.get("sync_history", [])
    delete_set = set(body.timestamps)
    state["sync_history"] = [h for h in history if h.get("time", 0) not in delete_set]
    save_state(state)
    return {"ok": True, "deleted": len(delete_set), "remaining": len(state["sync_history"])}


@app.post("/api/sync/history/clear")
async def api_sync_history_clear():
    state = load_state()
    state["sync_history"] = []
    save_state(state)
    return {"ok": True}


class SyncRetryRequest(BaseModel):
    session_id: str


@app.post("/api/sync/retry")
async def api_sync_retry(body: SyncRetryRequest):
    """重试同步失败的会话（恢复原始时间范围和失败的 peer）"""
    global _sync_running
    if _sync_running:
        return {"ok": False, "error": "同步正在进行中"}

    state = load_state()
    history = state.get("sync_history", [])
    session = None
    for h in history:
        if h.get("session_id") == body.session_id:
            session = h
            break

    if not session:
        return {"ok": False, "error": "未找到该次同步记录"}

    peer_results = session.get("peer_results", [])
    failed_peers = [p for p in peer_results if p.get("status") == "失败"]
    if not failed_peers:
        return {"ok": False, "error": "没有失败的会话需要重试"}

    time_range_days = session.get("time_range_days", 7)
    source = session.get("source", "manual")

    # 从配置中找到对应的 peers 信息
    config = load_config()
    all_peers = config.get("sync", {}).get("peers", [])
    failed_uids = set(p.get("name", "") for p in failed_peers)
    retry_peers = [p for p in all_peers if p.get("name", "") in failed_uids]
    if not retry_peers:
        return {"ok": False, "error": "配置中找不到这些会话，请确认 peers 配置无误"}

    # 创建临时配置：只同步失败的 peers，保持原时间范围
    retry_config = dict(config)
    retry_config["sync"] = dict(config.get("sync", {}))
    retry_config["sync"]["peers"] = retry_peers
    retry_config["sync"]["time_range_days"] = time_range_days

    with SYNC_LOCK:
        _sync_running = True
        _sync_progress = {"status": "running", "current": "", "detail": f"重试 {len(retry_peers)} 个失败会话...", "log": []}

    _add_sync_log_internal(f"========== 重试同步（来源: {body.session_id}） ==========")
    thread = threading.Thread(target=run_sync_sync, args=(retry_config, state), kwargs={"source": source}, daemon=True)
    thread.start()
    return {"ok": True, "message": f"正在重试 {len(retry_peers)} 个失败会话", "retry_count": len(retry_peers)}


@app.get("/api/sync/log")
async def api_sync_log(limit: int = 100):
    try:
        raw = LOG_FILE.read_bytes()
        # 尝试 UTF-8，失败则用 GBK（Windows 默认编码）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        lines = text.strip().split("\n")
        return {"log": lines[-limit:]}
    except Exception:
        return {"log": []}


# ── 开机自启 API ──────────────────────────────────────────


@app.get("/api/autostart/status")
@app.get("/api/version")
async def api_version():
    return {"version": VERSION}


CURRENT_CHANGELOG = []  # 运行时缓存


@app.get("/api/update/changelog")
async def api_changelog():
    """从服务器获取更新日志（正式版+开发版）"""
    global CURRENT_CHANGELOG
    if CURRENT_CHANGELOG:
        return {"changelog": CURRENT_CHANGELOG}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            _ts = int(time.time())
            resp = await c.get("https://ruoan486.icu/api/public-download/changelog.md?_t=" + str(_ts),
                               headers={"Cache-Control": "no-cache"})
            if resp.status_code == 200:
                text = resp.text
                CURRENT_CHANGELOG = text
                return {"changelog": text}
        return {"changelog": ""}
    except Exception as e:
        return {"changelog": f"获取更新日志失败: {e}"}


@app.post("/api/create-shortcuts")
async def api_create_shortcuts():
    """在桌面创建一键启动和后台启动的快捷方式"""
    import shutil
    try:
        desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if not desktop.exists():
            return {"ok": False, "error": "找不到桌面目录"}

        app_dir = Path(__file__).parent
        if getattr(sys, 'frozen', False):
            app_dir = Path(sys.executable).parent

        onekey = app_dir / "一键启动.bat"
        bg = app_dir / "后台启动.bat"

        if not onekey.exists():
            return {"ok": False, "error": f"找不到 {onekey}"}

        # 用 PowerShell 创建 .lnk 快捷方式
        ps_script = f'''
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("{desktop / '一键启动.lnk'}")
$sc.TargetPath = "{onekey}"
$sc.WorkingDirectory = "{app_dir}"
$sc.Description = "QCE2ChatLab - 启动全部工具"
$sc.Save()
'''
        if bg.exists():
            ps_script += f'''
$sc2 = $ws.CreateShortcut("{desktop / '后台启动.lnk'}")
$sc2.TargetPath = "{bg}"
$sc2.WorkingDirectory = "{app_dir}"
$sc2.Description = "QCE2ChatLab - 后台静默启动"
$sc2.Save()
'''

        result = subprocess.run(["powershell", "-Command", ps_script],
                                capture_output=True, text=True, timeout=15)

        if result.returncode != 0:
            return {"ok": False, "error": f"PowerShell 失败: {result.stderr}"}

        created = "一键启动.lnk"
        if bg.exists():
            created += " + 后台启动.lnk"
        return {"ok": True, "message": f"已在桌面创建: {created}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def api_autostart_status():
    return {"enabled": is_autostart_enabled()}


@app.post("/api/autostart/toggle")
async def api_autostart_toggle():
    config = load_config()
    current = config.get("app", {}).get("autostart", False)
    config.setdefault("app", {})["autostart"] = not current
    save_config(config)

    if config["app"]["autostart"]:
        enable_autostart(config)
    else:
        disable_autostart()

    return {"autostart": config["app"]["autostart"]}


# ── 安装目录 API ──────────────────────────────────────────


@app.get("/api/install-dir")
async def api_get_install_dir():
    config = load_config()
    current_dir = config.get("app", {}).get("install_dir", "")
    if not current_dir:
        if getattr(sys, 'frozen', False):
            current_dir = str(Path(sys.executable).parent)
        else:
            current_dir = str(Path(__file__).parent)
    return {
        "current": current_dir,
        "default": str(Path.home() / "QCE2ChatLab"),
        "is_frozen": getattr(sys, 'frozen', False),
    }


class SetInstallDir(BaseModel):
    path: str


@app.post("/api/install-dir")
async def api_set_install_dir(body: SetInstallDir):
    config = load_config()
    config.setdefault("app", {})["install_dir"] = body.path
    save_config(config)
    return {"ok": True, "path": body.path}


# ── 自动更新 API ─────────────────────────────────────────

VERSION = "1.4.0"
# 更新通过 portal 后台文件下载。由于 portal 需要 admin 登录，
# 改为比较本地记录的版本号与远程文件的检查方式：
# 直接下载 tar.gz 的 header (Range 请求) 看 if-modified 或者比较本地记录的版本
UPDATE_CHECK_URL = "https://ruoan486.icu/api/public-download/qce2chatlab.tar.gz"
UPDATE_DOWNLOAD_URL = UPDATE_CHECK_URL
UPDATE_DEV_CHECK_URL = "https://ruoan486.icu/api/public-download/qce2chatlab-dev.tar.gz"
UPDATE_DEV_DOWNLOAD_URL = UPDATE_DEV_CHECK_URL

_update_status = {"checking": False, "latest": VERSION, "downloading": False, "progress": ""}
_update_dev_status = {"checking": False, "latest": VERSION, "downloading": False, "progress": ""}


# ── 开发版更新 ────────────────────────────────────────


@app.get("/api/update/dev-check")
async def api_check_dev_update():
    global _update_dev_status
    _update_dev_status["checking"] = True
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            resp = await c.head(UPDATE_DEV_CHECK_URL, headers={"Cache-Control": "no-cache"})
            last_modified = resp.headers.get("last-modified", "")
            etag = resp.headers.get("etag", "")

            if resp.status_code != 200:
                _update_dev_status["checking"] = False
                return {"has_update": False, "current": VERSION, "error": f"服务器返回 {resp.status_code}"}

            remote_id = etag or last_modified
            if not remote_id:
                content_len = resp.headers.get("content-length", "0")
                remote_id = f"size={content_len}"

            _update_dev_status["latest"] = remote_id[:30] if remote_id else "unknown"
            last_id = load_config().get("app", {}).get("last_dev_update_id", "")
            has_update = (remote_id != last_id and remote_id != "")

            _update_dev_status["checking"] = False
            raw_len = resp.headers.get("content-length", "0")
            try:
                size_kb = round(int(raw_len) / 1024)
            except:
                size_kb = "?"
            return {
                "has_update": has_update,
                "current": VERSION,
                "remote_id": remote_id[:50] if remote_id else "unknown",
                "content_length": raw_len,
                "remote_size_kb": size_kb,
                "update_type": "dev",
            }
    except Exception as e:
        _update_dev_status["checking"] = False
        return {"has_update": False, "current": VERSION, "error": str(e)}


@app.post("/api/update/dev-install")
async def api_install_dev_update():
    """下载开发版并自动替换、重启"""
    global _update_dev_status
    _update_dev_status["downloading"] = True
    _update_dev_status["progress"] = "正在下载开发版..."

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            _ts = int(time.time())
            resp = await c.get(f"{UPDATE_DEV_DOWNLOAD_URL}?_t={_ts}", headers={"Cache-Control": "no-cache"})
            if resp.status_code != 200:
                _update_dev_status["downloading"] = False
                return {"ok": False, "error": f"下载失败: HTTP {resp.status_code}"}

            tar_data = resp.content
            _update_dev_status["progress"] = f"下载完成 ({len(tar_data)} bytes)，正在解压..."

        import tarfile
        import shutil

        tmp_dir = APP_DIR / "_update_tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        tmp_tar = APP_DIR / "_update_dev.tar.gz"
        tmp_tar.write_bytes(tar_data)

        with tarfile.open(tmp_tar, "r:gz") as tar:
            tar.extractall(path=tmp_dir)

        tmp_tar.unlink()

        cfg = load_config()
        install_dir = cfg.get("app", {}).get("install_dir", "")
        if install_dir:
            app_dir = Path(install_dir)
        else:
            app_dir = Path(__file__).parent
            if getattr(sys, 'frozen', False):
                app_dir = Path(sys.executable).parent

        extracted_app = tmp_dir / "qce2chatlab"
        if not extracted_app.exists():
            for d in tmp_dir.iterdir():
                if d.is_dir() and (d / "main.py").exists():
                    extracted_app = d
                    break

        if not extracted_app.exists() or not (extracted_app / "main.py").exists():
            _update_dev_status["downloading"] = False
            return {"ok": False, "error": "解压后找不到 main.py"}

        _update_dev_status["progress"] = "正在安装开发版..."
        for src in extracted_app.rglob("*"):
            rel = src.relative_to(extracted_app)
            dst = app_dir / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                if "__pycache__" not in str(rel) and not str(rel).endswith(".pyc"):
                    shutil.copy2(src, dst)

        config = load_config()
        config.setdefault("app", {})["last_dev_update_id"] = _update_dev_status.get("latest", "")
        save_config(config)

        shutil.rmtree(tmp_dir)

        _update_dev_status["progress"] = "安装完成，正在重启..."
        _update_dev_status["downloading"] = False
        log("✅ 开发版更新完成，即将重启...")

        def delayed_restart():
            time.sleep(1)
            import subprocess
            exe = sys.executable
            script = sys.argv[0] if not getattr(sys, 'frozen', False) else None
            args = sys.argv[1:] if not getattr(sys, 'frozen', False) else sys.argv[1:]
            cmd = [exe] + ([script] if script else []) + list(args)
            subprocess.Popen(cmd, close_fds=True)
            os._exit(0)

        threading.Thread(target=delayed_restart, daemon=True).start()

        return {"ok": True, "message": "开发版更新完成，服务正在重启..."}
    except Exception as e:
        _update_dev_status["downloading"] = False
        return {"ok": False, "error": str(e)}


@app.get("/api/update/check")
async def api_check_update():
    global _update_status
    _update_status["checking"] = True
    try:
        # 方法：HEAD 请求获取文件最后修改时间，与本地记录比较
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            resp = await c.head(UPDATE_CHECK_URL, headers={"Cache-Control": "no-cache"})
            last_modified = resp.headers.get("last-modified", "")
            etag = resp.headers.get("etag", "")

            # 如果返回 200，说明文件存在
            if resp.status_code != 200:
                _update_status["checking"] = False
                return {"has_update": False, "current": VERSION, "error": f"服务器返回 {resp.status_code}（可能需要先登录 portal 后台获取 cookie）"}

            remote_id = etag or last_modified
            if not remote_id:
                content_len = resp.headers.get("content-length", "0")
                remote_id = f"size={content_len}"

            _update_status["latest"] = remote_id[:30] if remote_id else "unknown"
            last_id = load_config().get("app", {}).get("last_update_id", "")
            has_update = (remote_id != last_id and remote_id != "")

            _update_status["checking"] = False
            raw_len = resp.headers.get("content-length", "0")
            try:
                size_kb = round(int(raw_len) / 1024)
            except:
                size_kb = "?"
            return {
                "has_update": has_update,
                "current": VERSION,
                "remote_id": remote_id[:50] if remote_id else "unknown",
                "content_length": raw_len,
                "remote_size_kb": size_kb,
            }
    except Exception as e:
        _update_status["checking"] = False
        return {"has_update": False, "current": VERSION, "error": str(e)}


@app.post("/api/update/install")
async def api_install_update():
    """下载新版本并自动替换、重启"""
    global _update_status
    _update_status["downloading"] = True
    _update_status["progress"] = "正在下载..."

    try:
        # 1. 下载新版本
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            _ts = int(time.time())
            resp = await c.get(f"{UPDATE_DOWNLOAD_URL}?_t={_ts}", headers={"Cache-Control": "no-cache"})
            if resp.status_code != 200:
                _update_status["downloading"] = False
                return {"ok": False, "error": f"下载失败: HTTP {resp.status_code}"}

            tar_data = resp.content
            _update_status["progress"] = f"下载完成 ({len(tar_data)} bytes)，正在解压..."

        # 2. 解压到临时目录
        import tarfile
        import shutil

        tmp_dir = APP_DIR / "_update_tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # 写 tar.gz 到临时文件
        tmp_tar = APP_DIR / "_update.tar.gz"
        tmp_tar.write_bytes(tar_data)

        # 解压
        with tarfile.open(tmp_tar, "r:gz") as tar:
            tar.extractall(path=tmp_dir)

        tmp_tar.unlink()

        # 3. 获取应用目录（优先用配置中的安装目录）
        cfg = load_config()
        install_dir = cfg.get("app", {}).get("install_dir", "")
        if install_dir:
            app_dir = Path(install_dir)
        else:
            app_dir = Path(__file__).parent
            if getattr(sys, 'frozen', False):
                app_dir = Path(sys.executable).parent

        # 4. 找到解压后的 qce2chatlab 目录
        extracted_app = tmp_dir / "qce2chatlab"
        if not extracted_app.exists():
            # 可能在子目录
            for d in tmp_dir.iterdir():
                if d.is_dir() and (d / "main.py").exists():
                    extracted_app = d
                    break

        if not extracted_app.exists() or not (extracted_app / "main.py").exists():
            _update_status["downloading"] = False
            return {"ok": False, "error": "解压后找不到 main.py"}

        # 5. 覆盖文件（跳过 __pycache__ 和运行时文件）
        _update_status["progress"] = "正在安装..."
        for src in extracted_app.rglob("*"):
            rel = src.relative_to(extracted_app)
            dst = app_dir / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                if "__pycache__" not in str(rel) and not str(rel).endswith(".pyc"):
                    shutil.copy2(src, dst)

        # 6. 更新 mtime 记录
        config = load_config()
        config.setdefault("app", {})["last_update_id"] = _update_status.get("latest", "")
        save_config(config)

        # 7. 清理临时文件
        shutil.rmtree(tmp_dir)

        _update_status["progress"] = "安装完成，正在重启..."
        _update_status["downloading"] = False
        log("✅ 更新完成，即将重启...")

        # 8. 延迟重启（让 HTTP 响应先返回）
        def delayed_restart():
            time.sleep(1)
            import subprocess
            exe = sys.executable
            script = sys.argv[0] if not getattr(sys, 'frozen', False) else None
            args = sys.argv[1:] if not getattr(sys, 'frozen', False) else sys.argv[1:]
            cmd = [exe] + ([script] if script else []) + list(args)
            subprocess.Popen(cmd, close_fds=True)
            os._exit(0)

        threading.Thread(target=delayed_restart, daemon=True).start()

        return {"ok": True, "message": "更新完成，服务正在重启..."}

    except Exception as e:
        _update_status["downloading"] = False
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


@app.get("/api/update/status")
async def api_update_status():
    return _update_status


# ── 卸载 API ─────────────────────────────────────────────

@app.post("/api/uninstall")
async def api_uninstall():
    """卸载软件：禁用开机自启、停止定时同步、可选择删除程序文件但保留聊天记录和配置"""
    import shutil

    # 1. 禁用开机自启
    try:
        disable_autostart()
    except Exception:
        pass

    # 2. 停止定时同步
    try:
        stop_schedule()
    except Exception:
        pass

    # 3. 获取安装目录
    config = load_config()
    app_dir = Path(__file__).parent

    # 4. 删除程序文件（Python 脚本可以删自己的文件）
    files_to_delete = []
    for item in app_dir.iterdir():
        if item.name == "main.py" or item.name == "frontend" or item.name == "requirements.txt":
            files_to_delete.append(item)

    deleted = []
    failed = []
    for item in files_to_delete:
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            deleted.append(str(item.name))
        except Exception as e:
            failed.append(f"{item.name}: {e}")

    # 5. 保存卸载记录（用户手动删除 C:\Users\xxx\.qce2chatlab 即可清理配置）
    log("==================== 软件已卸载 ====================")
    log(f"程序文件删除: {deleted}")
    if failed:
        log(f"删除失败: {failed}")
    log("聊天记录和配置保留在: " + str(APP_DIR))

    return {"ok": True, "deleted": deleted, "failed": failed, "config_dir": str(APP_DIR)}


# ── 启动 ──────────────────────────────────────────────────



def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--sync-once", action="store_true", help="执行一次同步后自动退出（供 QCE 调用）")
    args = parser.parse_args()

    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)

    config = load_config()

    # 恢复开机自启状态
    if config.get("app", {}).get("autostart"):
        if not is_autostart_enabled():
            enable_autostart(config)

    # 恢复定时同步
    if config.get("sync", {}).get("auto_sync_enabled"):
        start_schedule(config)

    port = args.port or config.get("app", {}).get("port", 15520)

    if not args.no_browser:
        def open_browser():
            time.sleep(1.5)
            webbrowser.open(f"http://{args.host}:{port}")
        threading.Thread(target=open_browser, daemon=True).start()

    print(f"\n=== QCE2ChatLab v{VERSION} ===")
    print(f"Local: http://{args.host}:{port}")
    print(f"Config: {APP_DIR}")
    print(f"Log: {LOG_FILE}")
    print()

    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
