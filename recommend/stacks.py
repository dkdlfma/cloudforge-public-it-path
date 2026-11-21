"""공고의 요구 기술 스택 → 보유/부족 판정 + 학습 순서 + 관련 NCS 능력단위·훈련과정   (담당: 강민경)
data/processed/stack_map.json (pipeline/embed_stacks.py 결과)을 읽습니다. 없으면 정규화·판정만 합니다."""
import json
from functools import lru_cache

from . import data_io

REQ_ORDER = {"필수": 0, "선택필수": 1, "우대": 2}
STACK_TYPES = ("기술", "직무지식")
# 같은 요건 수준 안에서는 기초 → 응용 순서로 (팀 가정)
CATEGORY_ORDER = ["프로그래밍 언어", "웹", "운영체제", "개발 도구", "데이터베이스", "웹 프레임워크", "백엔드 프레임워크",
                  "프론트엔드 프레임워크", "클라우드·DevOps", "데이터 엔지니어링", "AI·데이터", "네트워크", "아키텍처", "업무 시스템"]


def _skills():
    from labutil import skills   # data_io 가 src 를 sys.path 에 넣어 둠
    return skills


@lru_cache
def stack_map():
    path = data_io.PROCESSED / "stack_map.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@lru_cache
def _courses_by_title():
    path = data_io.PROCESSED / "training_courses.json"
    rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    out = {}
    for r in rows:
        out.setdefault(str(r.get("과정명", "")), []).append(r)
    return out


def names():
    """화면의 '보유 기술 스택' 선택지 (사전의 IT 스택 대표명)"""
    return sorted(k for k, v in _skills().load_aliases().items() if v.get("embed", True))


def normalize(name):
    return _skills().normalize(name) or name


def _training(entry, regions, k=3):
    rows = []
    for t in entry.get("training_titles", []):
        for r in _courses_by_title().get(t["과정명"], []):
            rows.append((t["score"], r))
    if regions:
        local = [x for x in rows if any(g in str(x[1].get("주소", "")) for g in regions)]
        rows = local or rows
    rows.sort(key=lambda x: (-x[0], str(x[1].get("시작일", ""))))
    out, seen = [], set()
    for s, r in rows:
        if r.get("과정명") in seen:
            continue
        seen.add(r.get("과정명"))
        out.append({"과정명": r.get("과정명", ""), "훈련기관": r.get("훈련기관", ""), "주소": r.get("주소", ""),
                    "시작일": r.get("시작일", ""), "실제훈련비": r.get("실제훈련비", ""), "링크": r.get("링크", ""),
                    "유사도": round(s, 3)})
        if len(out) >= k:
            break
    return out


def _order(missing, info):
    """선행 스택이 먼저 오도록 정렬 (예: Java → Spring Boot, Docker → Kubernetes). 선행이 부족 목록에 없으면 무시."""
    names_ = [m["canonical"] for m in missing]
    done, seq = set(), []

    def visit(n, stack=()):
        if n in done or n in stack:
            return
        for p in info.get(n, {}).get("prereq", []):
            if p in names_:
                visit(p, stack + (n,))
        done.add(n)
        seq.append(n)

    cat = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    for m in sorted(missing, key=lambda x: (REQ_ORDER.get(x["requirement"], 3), cat.get(x["category"], 99), x["canonical"])):
        visit(m["canonical"])
    return seq


def for_posting(posting_id, position=None, held=None, regions=None):
    """→ {"required": [...], "held": [...], "missing": [...], "path": [...], "total_weeks": n, "other": [...]}"""
    sk = _skills()
    aliases = sk.load_aliases()
    ext = data_io.extractions().get(posting_id) or {}
    held_set = {normalize(h) for h in (held or [])}
    smap = stack_map()
    req = {}
    for c in ext.get("competencies", []):
        if c.get("type") not in STACK_TYPES:
            continue
        if position and c.get("position") and c["position"] != position:
            continue
        canon = sk.normalize(c["name"])
        if not canon:
            continue
        r = c.get("requirement", "우대")
        prev = req.get(canon)
        if prev is None or REQ_ORDER.get(r, 3) < REQ_ORDER.get(prev["requirement"], 3):
            info = aliases[canon]
            req[canon] = {"canonical": canon, "raw": c["name"], "requirement": r, "evidence": c.get("evidence", ""),
                          "category": info["category"], "field": info.get("field", "기타"),
                          "it": info.get("embed", True), "weeks": info.get("weeks")}
    required = sorted(req.values(), key=lambda x: (REQ_ORDER.get(x["requirement"], 3), x["canonical"]))
    it_req = [x for x in required if x["it"]]
    held_list = [x for x in it_req if x["canonical"] in held_set]
    missing = []
    for x in it_req:
        if x["canonical"] in held_set:
            continue
        e = smap.get(x["canonical"], {})
        missing.append({**x, "ncs": e.get("ncs", [])[:3], "training": _training(e, regions),
                        "demand": e.get("demand", 0), "backend": e.get("backend")})
    seq = _order(missing, aliases)
    by = {m["canonical"]: m for m in missing}
    path, cum = [], 0
    for i, n in enumerate(seq, 1):
        w = by[n]["weeks"] or 2
        cum += w
        pre = [p for p in aliases[n].get("prereq", []) if p in by]
        path.append({"order": i, "name": n, "weeks": w, "cum_weeks": cum, "requirement": by[n]["requirement"],
                     "after": pre})
    return {"required": required, "held": [x["canonical"] for x in held_list], "missing": missing,
            "path": path, "total_weeks": cum, "other": [x for x in required if not x["it"]],
            "has_map": bool(smap), "backend": (smap.get("_meta") or {}).get("backend")}
