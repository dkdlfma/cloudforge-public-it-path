"""자격증 카탈로그 — 이름 맞추기(정규화)와 조회
cert_catalog.json 의 신입 취득 현실성·준비 기간은 '팀 가정값'입니다 (발표 전 Q-Net·시행기관 확인)."""
import json
import re
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("cert_catalog.json")


def key(name):
    """'OCP(Oracle Certified Professional)' → 'ocporaclecertifiedprofessional' (공백·괄호·기호 제거)"""
    return re.sub(r"[\s()\[\]·\-_/.,]", "", str(name)).lower()


@lru_cache
def load():
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)["certs"]


@lru_cache
def _alias_index():
    idx = []
    for e in load():
        for a in [e["name"], *e.get("aliases", [])]:
            idx.append((key(a), e))
    idx.sort(key=lambda x: -len(x[0]))          # 긴 별칭부터 (정보처리산업기사 ≠ 정보처리기사)
    return idx


def find(name):
    """추출된 이름 → 카탈로그 항목 (없으면 None)"""
    k = key(name)
    for ak, e in _alias_index():
        if ak == k:
            return e
    for ak, e in _alias_index():
        if len(ak) >= 3 and (ak in k or k in ak):
            return e
    return None


def canonical(name):
    e = find(name)
    return e["name"] if e else name


def names():
    return [e["name"] for e in load()]


def categories():
    return sorted({e["category"] for e in load()})


_VAGUE = re.compile(r"관련|분야|OA|등$|이상\)?$|^ICT$")


def infer(name):
    """카탈로그에 없는 자격증 → 이름 끝(기능사·산업기사·기사·기술사 등)으로 대략 추정한 항목 (팀 가정)"""
    n = str(name)
    if _VAGUE.search(n) and not re.search(r"(기능사|산업기사|기사|기술사|기능장)$", n):
        return {"name": n, "category": "확인 필요", "tier": "불명확", "issuer": "공고 원문 확인",
                "eligibility": "구체적인 자격증명이 아님 — 공고 원문에서 인정 목록 확인", "needs_eligibility": False,
                "entry_feasibility": "중간", "prep_weeks": 12, "phases": ["시험"], "recommended_before": [], "next": {},
                "inferred": True, "vague": True}
    cat = "전기·통신" if re.search(r"전기|통신|방송|전파|전자|배전|ICT", n) else (
        "보안" if "보안" in n else ("설비" if re.search(r"배관|냉동|에너지|설비|건축|기중기", n) else "기타"))
    for suffix, tier, feas, weeks, elig in (
            ("기술사", "기술사", "낮음", 52, "상당한 실무 경력 요건"),
            ("기능장", "기능장", "낮음", 40, "실무 경력 요건"),
            ("산업기사", "산업기사", "높음", 10, "산업기사 응시자격 요건 있음 — Q-Net 확인"),
            ("기사", "기사", "중간", 14, "기사 응시자격(학력·경력) 요건 있음 — Q-Net 확인"),
            ("기능사", "기능사", "높음", 8, "응시자격 제한 없음(대부분) — Q-Net 확인")):
        if n.endswith(suffix):
            return {"name": n, "category": cat, "tier": tier, "issuer": "Q-Net 등 시행기관 확인", "eligibility": elig,
                    "needs_eligibility": suffix in ("기사", "산업기사", "기술사", "기능장"), "entry_feasibility": feas,
                    "prep_weeks": weeks, "phases": ["필기", "실기"], "recommended_before": [], "next": {}, "inferred": True}
    return None
