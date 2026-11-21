"""추천 자격증 → 관련 NCS 교과목 찾기
data/processed/ncs_courses.json 이 있으면 실제 데이터, 없으면 샘플(courses_sample.json)을 씁니다."""
import json
import re
from functools import lru_cache
from pathlib import Path

from . import catalog, data_io

SAMPLE = Path(__file__).with_name("courses_sample.json")

# 자격증 이름만으로 안 걸릴 때 교과목 이름·내용에서 찾을 키워드 (카테고리별)
KEYWORDS = {
    "보안": ["보안", "암호", "침해", "해킹", "네트워크보안"],
    "개발": ["프로그래밍", "소프트웨어", "응용", "자료구조", "알고리즘", "웹"],
    "DB": ["데이터베이스", "SQL", "DB"],
    "데이터": ["데이터 분석", "빅데이터", "통계", "머신러닝", "데이터분석"],
    "네트워크": ["네트워크", "통신", "라우팅"],
    "운영": ["리눅스", "운영체제", "서버", "시스템 관리"],
    "관리": ["프로젝트 관리", "감리", "정보시스템"],
}


@lru_cache
def load():
    """→ (교과목 목록, 출처 설명). 실제 API 데이터가 적으면 샘플을 함께 씁니다 (교과목마다 출처 표시)."""
    real_path = data_io.PROCESSED / "ncs_courses.json"
    real = json.loads(real_path.read_text(encoding="utf-8")) if real_path.exists() else []
    for r in real:
        r["출처"] = "NCS 교육과정 API"
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    for r in sample:
        r["출처"] = "샘플"
    if len(real) >= 100:
        return real, f"NCS 교육과정 API ({len(real)}건)"
    return real + sample, f"NCS 교육과정 API {len(real)}건 + 샘플 {len(sample)}건"


def _cert_hit(course, cert_key):
    certs = re.split(r"[,/·\n]", course.get("취득가능자격증", ""))
    return any(cert_key and (cert_key in catalog.key(c) or catalog.key(c) in cert_key) and len(catalog.key(c)) >= 3
               for c in certs if c.strip())


def for_cert(name, k=5):
    """자격증 이름 → 관련 교과목 상위 k개. 1순위: 취득가능자격증에 그 자격증이 있음, 2순위: 키워드 일치"""
    rows, source = load()
    e = catalog.find(name) or catalog.infer(name)
    cname = e["name"] if e else name
    ck = catalog.key(re.sub(r"\(.*?\)", "", cname))
    words = KEYWORDS.get(e["category"], []) if e else []
    scored = []
    for r in rows:
        text = f"{r.get('교과목', '')} {r.get('교과목표', '')} {r.get('교육내용', '')}"
        if _cert_hit(r, ck):
            scored.append((2, r, "취득가능자격증 일치"))
        elif words and any(w in text for w in words):
            scored.append((1, r, "교과 내용 키워드 일치"))
    seen, out = set(), []
    for s, r, why in sorted(scored, key=lambda x: -x[0]):
        key = (r.get("교과목"), r.get("학과"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"교과목": r.get("교과목", ""), "학과": r.get("학과", ""), "학교구분": r.get("학교구분", ""),
                    "시수": r.get("시수", ""), "세분류": r.get("세분류", ""), "근거": why, "출처": r.get("출처", ""),
                    "교과목표": (r.get("교과목표", "") or "")[:80]})
        if len(out) >= k:
            break
    return {"cert": cname, "source": source, "courses": out}


RESOURCES = Path(__file__).with_name("resources.json")


@lru_cache
def _res():
    return json.loads(RESOURCES.read_text(encoding="utf-8"))


def resources_for(name):
    """자격증 → 공식 시험 정보 사이트 목록 (+ 공통 학습처). 등록 안 된 기능사·산업기사 등은 Q-Net 안내."""
    r = _res()
    cname = catalog.canonical(name)
    keys = r["by_cert"].get(cname)
    if not keys:
        keys = ["Q-Net"] if re.search(r"(기능사|산업기사|기사|기술사|기능장)$", cname) else []
    sites = [r["by_issuer"][k] for k in keys]
    return {"cert": cname, "official": sites, "common": r["common"]}


TITLE_WORDS = {   # 과정명에서 찾을 말 (카테고리별). 관련자격증 칸이 비어 있는 과정이 많아서 보조로 씀
    "보안": ["보안", "해킹", "침해", "모의해킹", "관제"],
    "개발": ["자바", "JAVA", "파이썬", "Python", "웹", "응용 소프트웨어", "응용SW", "풀스택", "백엔드", "프로그래밍"],
    "DB": ["SQL", "데이터베이스", "오라클", "Oracle", "DB"],
    "데이터": ["빅데이터", "데이터 분석", "데이터분석", "머신러닝", "ADsP", "통계"],
    "네트워크": ["네트워크", "CCNA", "통신"],
    "운영": ["리눅스", "Linux", "서버", "클라우드", "AWS"],
    "관리": ["PM", "프로젝트 관리", "감리"],
}


def training_for(name, regions=None, k=5, exclude=None):
    """자격증 → 고용24 내일배움카드 훈련과정 (training_courses.json 이 있을 때만).
    1순위: 과정의 관련자격증에 그 자격증, 2순위: 과정명에 자격증 이름, 3순위: 과정명에 분야 키워드. 희망 지역이 있으면 주소로 먼저 거름."""
    path = data_io.PROCESSED / "training_courses.json"
    if not path.exists():
        return {"cert": catalog.canonical(name), "available": False, "courses": []}
    rows = json.loads(path.read_text(encoding="utf-8"))
    e = catalog.find(name) or catalog.infer(name)
    cname = e["name"] if e else name
    base = re.sub(r"\(.*?\)", "", cname)
    ck = catalog.key(base)
    words = TITLE_WORDS.get(e["category"], []) if e else []
    done = {catalog.key(x) for x in (exclude or []) if x}
    scored = []
    for r in rows:
        title = str(r.get("과정명", ""))
        if catalog.key(title) in done:          # 이미 이수한 과정은 추천에서 뺌
            continue
        if _cert_hit({"취득가능자격증": r.get("관련자격증", "")}, ck):
            scored.append((3, "관련자격증 일치", r))
        elif ck and ck in catalog.key(title):
            scored.append((2, "과정명에 자격증명", r))
        elif words and any(w.lower() in title.lower() for w in words):
            scored.append((1, "과정명 분야 키워드", r))
    local_ok = None
    if regions:
        local = [x for x in scored if any(g in str(x[2].get("주소", "")) for g in regions)]
        local_ok = bool(local)
        scored = local or scored
    scored.sort(key=lambda x: (-x[0], str(x[2].get("시작일", ""))))
    out, seen = [], set()
    for sc, why, r in scored:
        if r.get("과정ID") in seen:
            continue
        seen.add(r.get("과정ID"))
        out.append({**r, "근거": why})
        if len(out) >= k:
            break
    return {"cert": cname, "available": True, "courses": out, "total": len(rows), "local_ok": local_ok}


@lru_cache
def training_titles():
    """화면의 '이수한 교육·훈련과정' 선택지 (받아 둔 고용24 과정명, 중복 제거)"""
    path = data_io.PROCESSED / "training_courses.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return sorted({str(r.get("과정명", "")).strip() for r in rows if r.get("과정명")})
