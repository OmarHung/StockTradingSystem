"""ChromaDB 向量記憶庫（LLM_trader 的三 collection 設計）。

- experiences：每筆已評估的交易決策（情境+決策+結果+報酬）
- rules      ：反思合成的「有效規則」與「反模式」（可停用）
- blocked    ：被 Guard 駁回的交易（friction，供檢討風控鬆緊）

決策時以語意相似度檢索 experiences+rules 注入交易員 prompt——
這是「持續檢討、學習、優化」閉環的記憶基座。
"""
from __future__ import annotations

import datetime as dt
import threading
from functools import lru_cache

from src.config import ROOT
from src.logging_setup import get_logger

log = get_logger(__name__)

CHROMA_DIR = ROOT / "data" / "chroma"


# lru_cache 未命中時不互斥——FastAPI threadpool 可能並發首次初始化，
# ChromaDB 1.x 同路徑共用內部單例，並發建立會把半初始化的實例永久卡在
# 其內部快取（症狀：RustBindingsAPI 無 bindings → tenant 連不上）。
_client_lock = threading.Lock()


@lru_cache(maxsize=1)
def _locked_client():
    import chromadb
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def _client():
    with _client_lock:
        return _locked_client()


def _col(name: str):
    return _client().get_or_create_collection(name)


# ---------- experiences ----------
def add_experience(exp_id: str, situation: str, metadata: dict) -> None:
    """寫入一筆交易經驗。situation 為情境描述文字（檢索鍵），metadata 存結果數據。"""
    _col("experiences").upsert(ids=[exp_id], documents=[situation], metadatas=[metadata])


def query_experiences(situation: str, n: int = 3) -> list[dict]:
    """語意檢索最相似的歷史經驗。回傳 [{text, meta, distance}]。"""
    col = _col("experiences")
    if col.count() == 0:
        return []
    r = col.query(query_texts=[situation], n_results=min(n, col.count()))
    out = []
    for doc, meta, dist in zip(r["documents"][0], r["metadatas"][0], r["distances"][0]):
        out.append({"text": doc, "meta": meta or {}, "distance": round(dist, 3)})
    return out


def count_experiences() -> int:
    return _col("experiences").count()


def recent_experiences(n: int = 50) -> list[dict]:
    col = _col("experiences")
    if col.count() == 0:
        return []
    r = col.get(limit=n, include=["documents", "metadatas"])
    rows = [{"id": i, "text": d, "meta": m or {}}
            for i, d, m in zip(r["ids"], r["documents"], r["metadatas"])]
    rows.sort(key=lambda x: x["meta"].get("evaluated_at", ""), reverse=True)
    return rows


# ---------- rules ----------
def add_rule(rule_id: str, text: str, kind: str, evidence: str = "") -> None:
    """kind: effective（有效規則）| anti_pattern（反模式）。"""
    _col("rules").upsert(
        ids=[rule_id], documents=[text],
        metadatas=[{"kind": kind, "evidence": evidence, "active": True,
                    "created_at": dt.datetime.now().isoformat(timespec="seconds")}],
    )


def list_rules(include_inactive: bool = True) -> list[dict]:
    col = _col("rules")
    if col.count() == 0:
        return []
    r = col.get(include=["documents", "metadatas"])
    rows = [{"id": i, "text": d, **(m or {})}
            for i, d, m in zip(r["ids"], r["documents"], r["metadatas"])]
    if not include_inactive:
        rows = [x for x in rows if x.get("active")]
    rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return rows


def set_rule_active(rule_id: str, active: bool) -> None:
    col = _col("rules")
    r = col.get(ids=[rule_id], include=["documents", "metadatas"])
    if not r["ids"]:
        return
    meta = r["metadatas"][0] or {}
    meta["active"] = active
    col.upsert(ids=[rule_id], documents=[r["documents"][0]], metadatas=[meta])


def query_rules(situation: str, n: int = 5) -> list[dict]:
    """檢索與當前情境相關的『啟用中』規則。"""
    col = _col("rules")
    if col.count() == 0:
        return []
    r = col.query(query_texts=[situation], n_results=min(n * 2, col.count()))
    out = []
    for rid, doc, meta in zip(r["ids"][0], r["documents"][0], r["metadatas"][0]):
        if (meta or {}).get("active"):
            out.append({"id": rid, "text": doc, "kind": meta.get("kind", "")})
        if len(out) >= n:
            break
    return out


# ---------- blocked（friction 鏡像）----------
def add_blocked(block_id: str, text: str, metadata: dict) -> None:
    _col("blocked").upsert(ids=[block_id], documents=[text], metadatas=[metadata])


def count_all() -> dict:
    return {
        "experiences": _col("experiences").count(),
        "rules": _col("rules").count(),
        "blocked": _col("blocked").count(),
    }


def clear_all() -> dict:
    """清空三個 collection（反思記憶重置）。回傳清除前的筆數。"""
    counts = count_all()
    client = _client()
    for name in ("experiences", "rules", "blocked"):
        try:
            client.delete_collection(name)
        except Exception:  # noqa: BLE001 — collection 不存在時略過
            pass
    return counts
