"""팀원 결과물 읽기 — 공고(postings) · 추출(extractions) · NCS 매칭 · 수요표"""
import csv
import json
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
OUTPUTS = ROOT / "outputs"
SRC = ROOT / "src"
if str(SRC) not in sys.path:          # labutil(수업 공통 코드)을 불러오기 위해
    sys.path.insert(0, str(SRC))


def _read_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache
def extractions():
    """조호성: extractions.json → {공고ID: 추출 결과}"""
    return {e["id"]: e for e in _read_json(PROCESSED / "extractions.json", [])}


@lru_cache
def _postings():
    """강민경: postings_jobalio.json → {공고ID: 원본 공고} (8천여 건, 한 번만 읽음)"""
    return {p["id"]: p for p in _read_json(PROCESSED / "postings_jobalio.json", [])}


def posting(pid):
    return _postings().get(pid, {})


@lru_cache
def ncs_map():
    """신성훈: skill_ncs_map.json → {자격증 이름: [NCS 능력단위 후보]}"""
    return _read_json(PROCESSED / "skill_ncs_map.json", {})


@lru_cache
def demand_overall():
    """신성훈: demand_overall.csv → {역량 이름: {posting_count, analyzed, demand_pct}}"""
    path = PROCESSED / "demand_overall.csv"
    rows = {}
    if path.exists():
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows[r["skill"]] = {
                    "type": r.get("type", ""),
                    "required": int(float(r.get("required_posting_count") or 0)),
                    "alternative": int(float(r.get("alternative_required_posting_count") or 0)),
                    "preferred": int(float(r.get("preferred_posting_count") or 0)),
                    "posting_count": int(float(r.get("posting_count") or 0)),
                    "analyzed": int(float(r.get("analyzed_posting_count") or 0)),
                    "demand_pct": float(r.get("demand_pct") or 0),
                }
    return rows


def today():
    """한국 시간 기준 오늘 (YYYY-MM-DD)"""
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")


def close_info(pid):
    """→ (마감일, 진행 중 여부). 원본 공고의 마감일로 판단하고, 없으면 추출 결과의 ongoing 값을 씀"""
    raw = posting(pid)
    close = str(raw.get("close_at") or "")
    if close:
        return close, close >= today()
    return "", bool((extractions().get(pid) or {}).get("ongoing"))


def list_postings():
    """화면 선택용 공고 목록 — 진행 중 · 신입 가능 · IT 관련 공고가 위로"""
    items = []
    for e in extractions().values():
        raw = posting(e["id"])
        close, ongoing = close_info(e["id"])
        items.append({
            "id": e["id"], "company": e.get("company", ""), "title": e.get("title", ""),
            "close_at": close, "ongoing": ongoing,
            "regions": [r.strip() for r in str(raw.get("region", "")).split(",") if r.strip()],
            "hire_type": raw.get("hire_type", ""),
            "entry_level_ok": e.get("entry_level_ok", False), "it_relevant": e.get("it_relevant"),
            "n_comp": len(e.get("competencies", [])),
        })
    items.sort(key=lambda x: (not x["ongoing"], not x["entry_level_ok"], x["it_relevant"] is not True, -x["n_comp"]))
    return items


@lru_cache
def demand_by_region():
    """신성훈: demand_by_region.csv → {(지역, 역량): {posting_count, analyzed, demand_pct}}"""
    path = PROCESSED / "demand_by_region.csv"
    rows = {}
    if path.exists():
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows[(r["region"], r["skill"])] = {
                    "posting_count": int(float(r.get("posting_count") or 0)),
                    "analyzed": int(float(r.get("analyzed_posting_count") or 0)),
                    "demand_pct": float(r.get("demand_pct") or 0),
                }
    return rows
