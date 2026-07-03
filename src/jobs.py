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
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", *args],
        cwd=ROOT,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # 與 WebUI 進程脫鉤，關頁面也不中斷
    )
    pid_path.write_text(str(proc.pid))
    return True


def is_running(name: str) -> bool:
    _, pid_path = _paths(name)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)  # 不送訊號，只探測存活
        return True
    except OSError:
        return False


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
