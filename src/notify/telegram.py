"""Telegram Bot 通知（每日決策報告）。

設定分兩處（WebUI 設定中心「📨 通知」分頁皆可填）：
  .env            TELEGRAM_BOT_TOKEN（@BotFather 取得）、TELEGRAM_CHAT_ID
                  ——個人資訊，不進 git
  settings.yaml   notify.telegram.enabled（僅開關）

send_daily_report() 由每日流程末尾呼叫：未設定則靜默跳過，發送失敗只記 log，
絕不影響交易流程本身。
"""
from __future__ import annotations

import html
import os

import requests

from src.config import get_settings
from src.logging_setup import get_logger

log = get_logger("notify.telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_LEN = 4096  # Telegram 單則訊息上限


def _tg_cfg() -> dict:
    try:
        return dict(get_settings()["notify"]["telegram"])
    except Exception:  # noqa: BLE001 — 設定檔缺區塊視為未設定
        return {}


def is_configured() -> bool:
    """enabled 開啟且 token / chat_id 皆已設定。"""
    return (bool(_tg_cfg().get("enabled"))
            and bool((os.getenv("TELEGRAM_CHAT_ID") or "").strip())
            and bool((os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()))


def _split_by_lines(text: str, limit: int) -> list[str]:
    """把長訊息在「行邊界」切成多段（每段 ≤ limit）。單行超長才硬切，避免砍斷
    <b>…</b>/<pre> 標籤造成 Telegram parse 400（越長＝交易越活躍的日子越容易中）。"""
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(line) > limit:  # 極端單行：硬切（罕見，report 以行組裝）
            if buf:
                chunks.append(buf); buf = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf); buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks or [""]


def _post(token: str, chat_id: str, text: str, parse_mode: str | None) -> dict:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = requests.post(_API.format(token=token), json=payload, timeout=15)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Telegram API 回應異常（HTTP {r.status_code}）")
    return data


def send_message(text: str) -> None:
    """發送 HTML 格式訊息到設定的 chat_id。失敗拋例外（含 API 錯誤描述）。

    超過單則上限時在行邊界分段多則發送（不硬截斷砍斷標籤）；某段 HTML parse
    失敗（如標題含未跳脫字元）時該段降級為純文字重送一次，不讓整份報告丟失。
    """
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token:
        raise RuntimeError("未設定 TELEGRAM_BOT_TOKEN（.env）")
    if not chat_id:
        raise RuntimeError("未設定 TELEGRAM_CHAT_ID（.env）")
    for chunk in _split_by_lines(text, _MAX_LEN):
        data = _post(token, chat_id, chunk, "HTML")
        if not data.get("ok") and "parse" in str(data.get("description", "")).lower():
            data = _post(token, chat_id, chunk, None)  # HTML 解析失敗 → 純文字重送
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API 錯誤：{data.get('description')}")


def _money(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _stock_names(ids: list[str]) -> dict[str, str]:
    """批次查股名（stock_info）。查不到不影響報告，只顯示代號。"""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    try:
        from src.data import database as db
        with db.connect(get_settings().db_path) as conn:
            ph = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT stock_id, stock_name FROM stock_info WHERE stock_id IN ({ph})",
                ids).fetchall()
        return {r[0]: (r[1] or "") for r in rows}
    except Exception as e:  # noqa: BLE001 — 股名只是加值資訊
        log.warning("查股名失敗：%s", e)
        return {}


def format_daily_report(summary: dict) -> str:
    """把 run_daily() 的 summary 排版成 Telegram HTML 報告。"""
    names = _stock_names(
        [x.get("stock_id") for key in ("morning_fills", "exits", "decisions", "orders")
         for x in (summary.get(key) or [])])

    def label(sid) -> str:
        name = names.get(sid, "")
        return f"{sid} {html.escape(name)}" if name else str(sid)

    lines = [f"📊 <b>每日交易報告</b>　{summary.get('as_of', '')}"]

    eq = summary.get("equity") or {}
    if eq:
        lines.append(f"💰 總權益 <b>{_money(eq.get('equity'))}</b>"
                     f"（現金 {_money(eq.get('cash'))}／持倉 {_money(eq.get('positions_value'))}）")

    fills = summary.get("morning_fills") or []
    if fills:
        lines += ["", "☀️ <b>開盤撮合</b>"]
        for f in fills:
            price = f.get("price")
            lines.append(f"・{label(f.get('stock_id'))}　{html.escape(str(f.get('status', '?')))}"
                         + (f" @{price}" if price else ""))

    exits = summary.get("exits") or []
    if exits:
        lines += ["", "🛡️ <b>風控出場</b>"]
        for e in exits:
            pnl = e.get("pnl") or 0
            icon = "🔴" if pnl < 0 else "🟢"
            lines.append(f"{icon} {label(e.get('stock_id'))}　{html.escape(str(e.get('reason')))}"
                         f" @{e.get('price')}　損益 {_money(pnl)}")

    if not summary.get("trading_enabled", True):
        lines += ["", "⛔ <b>交易已緊急停止</b>——今日僅保護性出場，不開新倉"]

    decisions = summary.get("decisions") or []
    if decisions:
        lines += ["", "🤖 <b>盤後決策</b>"]
        for d in decisions:
            conf = d.get("confidence")
            conf_s = f"　信心 {conf:.2f}" if isinstance(conf, (int, float)) else ""
            note_s = f"　— {html.escape(str(d['note']))}" if d.get("note") else ""
            icon = "✅" if d.get("ordered") else "▫️"
            lines.append(f"{icon} {label(d.get('stock_id'))}　{str(d.get('action', '?')).upper()}{conf_s}{note_s}")

    orders = summary.get("orders") or []
    lines.append("")
    if orders:
        lines.append(f"📋 <b>明日委託 {len(orders)} 筆</b>")
        for o in orders:
            lines.append(f"・{label(o.get('stock_id'))}　{o.get('shares'):,} 股 @≤{o.get('limit')}"
                         f"（損 {o.get('stop')}／標 {o.get('target')}）")
    else:
        lines.append("📋 明日無新委託")

    refl = summary.get("reflection")
    if refl:
        ev = (refl.get("evaluation") or {}).get("evaluated", 0)
        lines.append(f"🪞 週反思：評估 {ev} 筆、新增規則 {refl.get('rules_added', 0)} 條")

    return "\n".join(lines)


def send_error_alert(title: str, detail: str = "") -> None:
    """流程崩潰告警（best effort）：未設定則跳過，發送失敗只記 log。"""
    if not is_configured():
        return
    try:
        text = f"❌ <b>{html.escape(title)}</b>"
        if detail:
            text += f"\n<pre>{html.escape(detail[:1000])}</pre>"
        send_message(text)
    except Exception as e:  # noqa: BLE001 — 告警失敗不能再拋，避免蓋掉原始例外
        log.error("Telegram 告警發送失敗：%s", e)


def send_daily_report(summary: dict) -> None:
    """每日流程完成後推送報告。未啟用/未設定則跳過；失敗只記 log。"""
    if not is_configured():
        # 明確記下缺哪一項，否則「沒收到通知」無從除錯
        missing = []
        if not _tg_cfg().get("enabled"):
            missing.append("settings.yaml notify.telegram.enabled 未開")
        if not (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip():
            missing.append("TELEGRAM_BOT_TOKEN 未設定（.env）")
        if not (os.getenv("TELEGRAM_CHAT_ID") or "").strip():
            missing.append("TELEGRAM_CHAT_ID 未設定（.env）")
        log.warning("Telegram 每日報告跳過：%s", "；".join(missing) or "未知原因")
        return
    try:
        send_message(format_daily_report(summary))
        log.info("📨 Telegram 每日報告已發送")
    except Exception as e:  # noqa: BLE001 — 通知失敗不影響交易流程
        log.error("Telegram 每日報告發送失敗：%s", e)
