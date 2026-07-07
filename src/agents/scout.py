"""關鍵新聞偵察（news scout）：讓「新聞先行、量價未動」的個股進得了候選池。

量化初篩只看量價/籌碼/營收，關鍵新聞（政策補助、大額接單、擴產、併購、
財報驚喜、產業趨勢等）剛出、股價還沒動的個股會漏接。
本模組每日跑一次兩階段流程：
  ① 收集關鍵新聞素材——預設走免費 RSS（Google News 關鍵字搜尋 + Yahoo財經 +
     鉅亨網，見 src/data/news_rss.py）；RSS 全掛或設定 source=web 時，
     改用 Claude + web_search 搜尋（處理 pause_turn 續跑）。
  ② 用既有 call_structured 從素材萃取結構化候選（沿用 fallback chain 與 brain_log）。
候選經過硬性驗證（代號存在、非 ETF/處置股、價格資料足夠）後，
以「額外名額」併入深度分析名單——之後仍要通過四位分析師 + Guard，
所以 LLM 幻覺或過度樂觀會被實算數據與風控擋下。
"""
from __future__ import annotations

import re

import anthropic
from pydantic import BaseModel, Field

from src.config import get_settings
from src.data import database as db
from src.llm import client as llm
from src.logging_setup import get_logger

log = get_logger(__name__)

_WEB_TOOLS = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}]
_MAX_CONTINUATIONS = 5  # pause_turn 續跑上限
_MAX_VALIDATE_RETRIES = 3  # 驗證剔除後帶原因讓 LLM 修正重跑的上限


class ScoutCandidate(BaseModel):
    stock_id: str = Field(description="台股 4 碼代號（如 1513）；不確定代號就不要列")
    name: str = Field(description="公司名稱")
    theme: str = Field(description="新聞題材標籤（如：電網強韌計畫、AI伺服器大單、產能擴建）")
    reason: str = Field(description="為何受惠 + 依據的新聞（一兩句，繁體中文）")


class ScoutReport(BaseModel):
    candidates: list[ScoutCandidate] = Field(description="最多 8 檔，按題材強度排序")
    summary: str = Field(description="本次關鍵新聞掃描總結（繁體中文）")


def run_news_scout(as_of: str) -> list[dict]:
    """回傳驗證後的題材候選 [{stock_id, name, theme, reason}]；失敗回空清單。"""
    ncfg = get_settings().get("news") or {}
    scfg = ncfg.get("scout") or {}
    if not scfg.get("enabled", True):
        return []
    # 判讀模型設定在 llm 區塊（WebUI 設定中心可改）；舊位置 news.scout.model 向下相容
    llm_cfg = get_settings().get("llm") or {}
    model = llm_cfg.get("scout_model") or scfg.get("model") or "claude-sonnet-4-6"
    max_c = int(scfg.get("max_candidates", 3))
    source = scfg.get("source", "rss")

    notes, headlines, used = "", [], source
    if source != "web":
        notes, headlines = _rss_notes(scfg)
        used = "rss"
    if not notes:  # RSS 沒素材（全掛/被停用）→ web search 備援
        used = "web"
        try:
            notes = _search_policy_news(model, as_of)
        except Exception as e:  # noqa: BLE001 — 偵察失敗不影響量化候選
            log.error("政策新聞搜尋失敗：%s", e)
            return []
    if not notes:
        return []

    system = (
        "你是台股關鍵新聞分析師。從提供的新聞素材中找出『重大新聞驅動』的題材"
        "（政策補助、法規修訂、預算追加、公共建設、國防、能源、大額接單、擴產投資、"
        "併購合作、財報驚喜、產業趨勢轉折等），並依你對台股產業鏈的"
        "知識列出可能受惠的上市櫃個股。"
        "優先選『新聞剛出、市場可能還沒完全反應』的題材，排除已大漲多日的舊題材與 ETF。"
        "股票代號必須是你非常確定的 4 碼代號，不確定就略過該檔；"
        "reason 需引用素材中的具體新聞標題。輸出繁體中文。"
    )
    prompt = (
        f"基準日 {as_of}。以下是近日財經/產業新聞素材：\n\n{notes}\n\n"
        "請萃取受惠個股候選清單（最多 8 檔，按題材強度排序）與總結。"
        "若素材中沒有明確的關鍵新聞題材，candidates 回空清單即可，不要硬湊。"
    )
    try:
        rpt = llm.call_structured(
            model=model, system=system, user_prompt=prompt, schema=ScoutReport,
            agent="scout", as_of=as_of, max_tokens=3000,
        )
    except Exception as e:  # noqa: BLE001
        log.error("題材候選萃取失敗：%s", e)
        return []
    summary = rpt.summary

    # 驗證剔除的候選帶原因回饋 LLM 修正重跑（換代號或換股），最多 _MAX_VALIDATE_RETRIES 次
    out, rejects = _validate(rpt.candidates, as_of, max_c)
    for attempt in range(1, _MAX_VALIDATE_RETRIES + 1):
        if not rejects or len(out) >= max_c:
            break
        log.info("scout 驗證剔除 %d 檔，回饋原因重跑（第 %d/%d 次）",
                 len(rejects), attempt, _MAX_VALIDATE_RETRIES)
        retry_prompt = (
            f"{prompt}\n\n【驗證回饋】你上一輪的下列候選未通過市場資料驗證，已被剔除：\n"
            + "\n".join(f"- {r}" for r in rejects)
            + "\n請針對這些題材重新確認正確的股票代號與公司名稱後再列出，"
            "或改列其他你非常確定代號的受惠股；仍不確定的就不要列。"
            "已通過驗證的檔不必重複列出：" + ("、".join(c["stock_id"] for c in out) or "（無）")
        )
        try:
            rpt = llm.call_structured(
                model=model, system=system, user_prompt=retry_prompt, schema=ScoutReport,
                agent="scout", as_of=as_of, max_tokens=3000,
            )
        except Exception as e:  # noqa: BLE001 — 重跑失敗就用既有結果
            log.error("scout 驗證重跑失敗：%s", e)
            break
        more, rejects = _validate(rpt.candidates, as_of, max_c - len(out),
                                  taken={c["stock_id"] for c in out})
        out.extend(more)

    _save_snapshot(as_of, used, headlines, summary, out)
    if out:
        llm.log_note(
            "scout",
            "政策題材候選：" + "；".join(f"{c['stock_id']} {c['name']}（{c['theme']}）" for c in out),
            as_of=as_of,
        )
    return out


_DEFAULT_KEYWORDS = ["台股 利多", "政策 補助 產業", "接單 台廠", "擴產 投資 台股",
                     "併購 台股", "國防 軍工", "電網 台電", "AI 供應鏈 台股"]


def _rss_notes(scfg: dict) -> tuple[str, list[dict]]:
    """階段①（預設路徑）：免費 RSS 關鍵字收集新聞標題，回傳 (素材文字, 標題清單)。"""
    from src.data import news_rss

    keywords = scfg.get("keywords") or _DEFAULT_KEYWORDS
    ncfg = get_settings().get("news") or {}
    days = int(ncfg.get("lookback_days", 10))
    heads = news_rss.fetch_policy_headlines(keywords, days=min(days, 5))
    if not heads:
        log.warning("RSS 新聞來源皆無資料，改用 web search 備援")
        return "", []
    log.info("RSS 收集到 %d 則新聞標題（關鍵字 %d 組）", len(heads), len(keywords))
    return "\n".join(f"[{h['date']}] ({h['source']}) {h['title']}" for h in heads), heads


def _save_snapshot(as_of: str, source: str, headlines: list[dict],
                   summary: str, candidates: list[dict]) -> None:
    """保存每日偵察快照（同日覆蓋），供 WebUI「題材偵察」區塊展示。"""
    import datetime as dt
    import json

    try:
        with db.connect(get_settings().db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scout_log "
                "(as_of, source, headlines_json, summary, candidates_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (as_of, source, json.dumps(headlines, ensure_ascii=False), summary,
                 json.dumps(candidates, ensure_ascii=False),
                 dt.datetime.now().isoformat(timespec="seconds")),
            )
    except Exception as e:  # noqa: BLE001 — 快照失敗不影響決策
        log.error("scout 快照保存失敗：%s", e)


def _search_policy_news(model: str, as_of: str) -> str:
    """階段①：web_search 搜集政策新聞，回傳研究筆記文字。

    server-side tool 迴圈達上限會回 pause_turn——原樣帶回 assistant 內容續跑。
    """
    client = llm._client()  # noqa: SLF001 — 共用專案內的 key 防護邏輯
    system = (
        "你是台股關鍵新聞偵察員。任務：用網路搜尋找出『最近 5 天內』與台股相關的"
        "重大新聞——政府政策、法規修訂、產業補助、公共建設、國防軍工、能源、"
        "大額接單、擴產投資、併購合作、財報驚喜、產業趨勢轉折等，"
        "並研判哪些台灣上市櫃公司可能受惠。"
        "優先找『新聞剛出、市場可能還沒完全反應』的題材。"
        "輸出一份研究筆記（繁體中文）：每個題材列出 新聞標題與日期、內容摘要、"
        "可能受惠的個股（公司名 + 4 碼代號，只列你確定代號的）與受惠邏輯。"
        "排除 ETF 與已大幅上漲多日的明顯舊題材。"
    )
    user_msg = f"今天是 {as_of}。請開始搜尋並整理研究筆記。"
    messages: list[dict] = [{"role": "user", "content": user_msg}]

    resp = None
    for _ in range(_MAX_CONTINUATIONS + 1):
        resp = client.messages.create(
            model=model, max_tokens=8000, system=system,
            messages=messages, tools=_WEB_TOOLS,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": user_msg},
                    {"role": "assistant", "content": resp.content}]

    notes = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    llm._log_call("scout", model, user_msg, notes, None, as_of)  # noqa: SLF001
    return notes


_UNCERTAIN_PAT = re.compile(r"需確認|待確認|不確定|存疑|不甚確定")
_NAME_NOISE_PAT = re.compile(r"股份有限公司|控股|-KY|\*|\s")
# u4e00-u9fff = CJK 漢字區段（一～鿿）——匹配「股名（4碼）」樣式中的中文股名
_PAIR_PAT = re.compile(r"([\u4e00-\u9fffA-Za-z*\-]{2,10})[（(](\d{4})[）)]")


def _is_subsequence(short: str, long: str) -> bool:
    """short 的每個字依序出現在 long 中（可跳字）。"""
    it = iter(long)
    return all(ch in it for ch in short)


def _name_matches(db_name: str, llm_name: str) -> bool:
    """寬鬆比對 LLM 名稱與股票池名稱：去尾綴後互相包含、或短名為長名的子序列即一致。

    LLM 常回全名（漢翔航空工業）而資料庫存簡稱（漢翔），故用包含而非相等；
    台股簡稱也常從全名跳字縮寫（士林電機→士電、中國鋼鐵→中鋼），
    故再放寬到子序列。任一方為空時視為一致（無從比對，交給後續分析師層把關）。
    """
    a = _NAME_NOISE_PAT.sub("", db_name or "")
    b = _NAME_NOISE_PAT.sub("", llm_name or "")
    if not a or not b:
        return True
    if a in b or b in a:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 2 and _is_subsequence(short, long)


def _pair_conflict(text: str, info: dict) -> str | None:
    """掃描 LLM 原文中的「股名（4碼）」配對，與股票池不符時回傳描述。

    候選欄位驗過了，但 theme/reason 是 LLM 原文、會直接上 WebUI——
    裡面提到的池內代號若掛錯公司名，同樣是代號幻覺的徵兆。
    """
    for m in _PAIR_PAT.finditer(text or ""):
        llm_name, sid = m.group(1), m.group(2)
        db_name = (info.get(sid) or ("", ""))[0]
        if db_name and not _name_matches(db_name, llm_name):
            return f"「{llm_name}（{sid}）」與股票池「{db_name}」不符"
    return None


def _validate(cands: list[ScoutCandidate], as_of: str, max_c: int,
              taken: set[str] | None = None) -> tuple[list[dict], list[str]]:
    """硬性驗證：代號存在於股票池、非 ETF、代號與名稱一致、價格資料足夠深度分析。

    回傳 (通過清單, 剔除原因清單)——剔除原因會回饋給 LLM 重跑修正。
    taken：前幾輪已入選的代號（重跑時避免重複入選）。
    """
    cfg = get_settings()
    out: list[dict] = []
    seen: set[str] = set(taken or ())
    rejects: list[str] = []

    def _reject(sid: str, c: ScoutCandidate, why: str, warn: bool = False) -> None:
        (log.warning if warn else log.info)("scout 候選 %s(%s) %s，略過", sid, c.name, why)
        rejects.append(f"{sid} {c.name}：{why}")

    with db.connect(cfg.db_path) as conn:
        info = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT stock_id, stock_name, type FROM stock_info")}
        for c in cands:
            sid = (c.stock_id or "").strip()
            if len(sid) != 4 or not sid.isdigit() or sid.startswith("00"):
                _reject(sid, c, "代號無效或為 ETF")
                continue
            if sid in seen:
                log.info("scout 候選 %s(%s) 代號重複，略過", sid, c.name)
                continue
            if sid not in info or info[sid][1] not in ("twse", "tpex"):
                _reject(sid, c, "不在上市櫃股票池")
                continue
            # LLM 幻覺代號時常掛上另一家公司的名字——代號查得到但名不對，
            # 若照代號覆寫名稱會把幻覺洗白成合法候選，必須整檔剔除。
            if not _name_matches(info[sid][0], c.name):
                _reject(sid, c, f"名稱與股票池「{info[sid][0]}」不符，疑似代號幻覺", warn=True)
                continue
            if _UNCERTAIN_PAT.search(c.reason or ""):
                _reject(sid, c, f"reason 自述不確定：「{c.reason}」", warn=True)
                continue
            conflict = _pair_conflict(f"{c.theme or ''} {c.reason or ''}", info)
            if conflict:
                _reject(sid, c, f"reason 含幻覺配對：{conflict}", warn=True)
                continue
            n = conn.execute(
                "SELECT COUNT(*) FROM price_daily WHERE stock_id=?", (sid,)).fetchone()[0]
            if n < 60:
                _reject(sid, c, f"價格資料不足（{n} 天 <60）")
                continue
            seen.add(sid)
            out.append({"stock_id": sid, "name": info[sid][0] or c.name,
                        "theme": c.theme, "reason": c.reason})
            if len(out) >= max_c:
                break
    return out, rejects
