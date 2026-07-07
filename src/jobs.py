"""背景任務管理：讓 WebUI 觸發長時間 CLI 任務（如全市場回補）並輪詢進度。

因 Streamlit 每次互動都會 rerun，無法在記憶體持有 Popen handle，
故以檔案系統追蹤：每個 job 有一個 log 檔與 pid 檔（放 logs/jobs/）。
WebUI 靠讀 log 檔顯示進度、靠 pid 是否存活判斷是否完成。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "logs" / "jobs"

# 本程序啟動的 job handle（poll() 會正確 reap 子程序，避免殭屍誤判存活）
_PROCS: dict[str, subprocess.Popen] = {}


def _paths(name: str) -> tuple[Path, Path]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DIR / f"{name}.log", JOBS_DIR / f"{name}.pid"


def start_job(name: str, args: list[str]) -> bool:
    """以背景程序執行 `python -m <args>`，stdout/stderr 導入 log 檔。

    若同名 job 仍在執行則不重複啟動，回傳 False。
    """
    if is_running(name):
        return False
    log_path, pid_path = _paths(name)
    if log_path.exists():
        # 保留上一輪輸出（含崩潰 traceback）供除錯，否則覆寫後死無對證
        log_path.replace(log_path.with_suffix(".prev.log"))
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", *args],
        cwd=ROOT,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # 與 WebUI 進程脫鉤，關頁面也不中斷
    )
    _PROCS[name] = proc
    pid_path.write_text(str(proc.pid))
    return True


def is_running(name: str) -> bool:
    # 1) 我們自己啟動的：用 poll()（會 reap，殭屍不會誤判為存活）
    proc = _PROCS.get(name)
    if proc is not None:
        if proc.poll() is not None:
            _PROCS.pop(name, None)
            return False
        return True

    # 2) 跨程序 fallback：讀 pid 檔
    _, pid_path = _paths(name)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)  # 不送訊號，只探測存活
    except OSError:
        return False
    # pid 存活但可能是殭屍（已死、父程序未 reap）→ 嘗試收屍確認
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False  # 剛收掉的殭屍
    except ChildProcessError:
        pass  # 不是我們的子程序，無法 waitpid，維持存活判定
    return True


def stop_job(name: str) -> bool:
    _, pid_path = _paths(name)
    if not is_running(name):
        return False
    pid = int(pid_path.read_text().strip())
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    return True


def read_log(name: str, tail: int = 30) -> str:
    """讀 log 尾端數行。tqdm 進度用 \\r 更新，這裡正規化成換行再取尾段。"""
    log_path, _ = _paths(name)
    if not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="replace").replace("\r", "\n")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-tail:])
