"""팀 파일 형식 점검 — 각자 만든 파일이 '파일 형식 약속'대로인지, 서로 잘 연결되는지 확인합니다.

    python pipeline/check_formats.py

확인하는 것:
  - postings_jobalio.json / extractions.json / skill_ncs_map.json / demand_*.csv 의 필수 필드
  - 추출 결과가 공고 ID 와 연결되는지, mock 으로 뽑힌 건이 남아 있는지
  - 역량명 표기 흔들림 (예: '정보처리기사' vs '정보처리기사 자격증') 후보
AWS 를 쓰지 않습니다. 고칠 것이 있으면 ❌, 참고 사항은 ⚠ 로 보여 줍니다.
"""
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

P = Path(__file__).resolve().parents[1] / "data" / "processed"
problems, notes = [], []

POSTING_FIELDS = ["id", "source", "company", "title", "ncs", "it_only", "region", "hire_type", "career",
                  "education", "posted_at", "close_at", "ongoing", "url", "text"]
EXTRACT_FIELDS = ["id", "job_areas", "it_relevant", "min_experience_years", "entry_level_ok", "competencies", "meta"]
COMP_FIELDS = ["name", "type", "requirement", "position", "evidence"]
TYPES = {"자격증", "전공", "기술", "직무지식", "경력", "학력", "어학", "기타"}
REQS = {"필수", "선택필수", "우대"}
DEMAND_FIELDS = ["skill", "type", "posting_count", "required_posting_count", "alternative_required_posting_count",
                 "preferred_posting_count", "analyzed_posting_count", "posting_total", "demand_pct"]


def load(name):
    path = P / name
    if not path.exists():
        notes.append(f"{name}: 아직 없음")
        return None
    return json.load(open(path, encoding="utf-8-sig"))


def check_postings():
    rows = load("postings_jobalio.json")
    if rows is None:
        problems.append("postings_jobalio.json 이 없습니다 → python pipeline/upload_s3.py --download")
        return {}
    missing = [f for f in POSTING_FIELDS if f not in rows[0]]
    if missing:
        problems.append(f"postings: 필드 없음 {missing}")
    latest = max((r.get("posted_at") or "") for r in rows)
    print(f"📄 공고 {len(rows):,}건 (정보통신 단독 {sum(r.get('it_only') is True for r in rows):,}건, 최신 공고일 {latest})")
    return {r["id"]: r for r in rows}


def norm(name):
    s = re.sub(r"\s+", "", name)
    s = re.sub(r"(자격증|자격|소지자?|보유자?|취득자?)$", "", s)
    return s.upper()


def check_extractions(postings):
    rows = load("extractions.json")
    if rows is None:
        return
    modes = Counter((r.get("meta") or {}).get("mode", "?") for r in rows)
    print(f"🧠 추출 {len(rows):,}건  (모드별: {dict(modes)})")
    for r in rows:
        miss = [f for f in EXTRACT_FIELDS if f not in r]
        if miss:
            problems.append(f"extractions {r.get('id')}: 필드 없음 {miss}")
        if postings and r.get("id") not in postings:
            problems.append(f"extractions {r.get('id')}: 공고 파일에 없는 ID")
        for c in r.get("competencies") or []:
            if [f for f in COMP_FIELDS if f not in c] or c.get("type") not in TYPES or c.get("requirement") not in REQS:
                problems.append(f"extractions {r.get('id')}: 역량 형식 오류 {c}")
                break
    mock_ids = [r["id"] for r in rows if (r.get("meta") or {}).get("mode") == "mock"]
    if mock_ids:
        notes.append(f"mock 으로 뽑힌 {len(mock_ids)}건은 수요 분석에서 빼거나 --force 로 다시 추출: {', '.join(mock_ids[:6])}")
    variants = defaultdict(set)
    counts = Counter()
    for r in rows:
        if (r.get("meta") or {}).get("mode") != "bedrock":
            continue
        for c in r.get("competencies") or []:
            variants[(norm(c["name"]), c["type"])].add(c["name"])
            counts[(c["name"], c["type"])] += 1
    shaky = {k: v for k, v in variants.items() if len(v) > 1}
    if shaky:
        print("  ⚠ 표기 통일 필요 (같은 역량으로 보이는 다른 이름):")
        for (_, t), names in sorted(shaky.items())[:15]:
            print(f"     [{t}] " + " / ".join(sorted(names)))
    top = counts.most_common(10)
    if top:
        print("  상위 역량 (bedrock):", ", ".join(f"{n}({c})" for (n, _), c in top))


def check_ncs_map():
    for name in ("skill_ncs_map.json", "skill_ncs_map.live.json"):
        m = load(name)
        if m is None:
            continue
        if not isinstance(m, dict):
            problems.append(f"{name}: {{자격증명: [후보...]}} 형태여야 합니다")
            continue
        bad = [k for k, v in m.items() if not isinstance(v, list)
               or any(not {"unit_code", "unit_name", "score"} <= set(c) for c in v)]
        if bad:
            problems.append(f"{name}: 후보 형식 오류 {bad[:3]}")
        sample = sum(any(c.get("catalog") == "kosa_sample" for c in v) for v in m.values() if isinstance(v, list))
        print(f"🔗 {name}: 역량 {len(m)}종, 후보 있음 {sum(bool(v) for v in m.values())}종"
              + (f"  ⚠ 실습 샘플 카탈로그 결과 {sample}종" if sample else ""))


def check_demand():
    files = sorted(P.glob("demand_*.csv"))
    if not files:
        notes.append("demand_*.csv: 아직 없음")
        return
    for f in files:
        with open(f, encoding="utf-8-sig") as s:
            header = next(csv.reader(s), [])
            n = sum(1 for _ in s)
        if f.name != "demand_coverage.csv":
            miss = [x for x in DEMAND_FIELDS if x not in header]
            if miss:
                problems.append(f"{f.name}: 열 없음 {miss}")
        print(f"📊 {f.name}: {n:,}행")


if __name__ == "__main__":
    postings = check_postings()
    check_extractions(postings)
    check_ncs_map()
    check_demand()
    print()
    for n in notes:
        print("⚠", n)
    for p in problems[:30]:
        print("❌", p)
    print("\n" + ("✅ 형식 문제 없음" if not problems else f"❌ 고칠 것 {len(problems)}건"))
