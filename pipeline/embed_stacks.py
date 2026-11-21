"""공고가 요구하는 기술 스택을 임베딩해 NCS 능력단위·내일배움카드 훈련과정과 연결합니다.   담당: 강민경

  1) extractions.json 의 '기술'(+ 스택 이름인 '직무지식') → data/dict/skill_aliases.json 으로 정규화
     (Excel·EXCEL·엑셀 → Excel, 쿠버네티스·k8s → Kubernetes)
  2) 스택마다 검색 문장 = "대표명 (분야). 사전 설명"  ← 이름만 넣으면 'Docker' 와 한글 NCS 문장이 거의 안 겹침
  3) NCS 능력단위(data/ncs/ncs_units_official.json) 와 훈련과정 과정명(training_courses.json)을 같은 모델로 임베딩
  4) 코사인 유사도 상위 k개 저장. 훈련과정은 과정명에 스택 별칭이 직접 나오면 +0.15 (하이브리드 검색)

실행:
  python pipeline/embed_stacks.py            # 모의 임베딩 (BEDROCK_MOCK=1, 비용 0)
  python pipeline/embed_stacks.py --live     # Titan Text Embeddings V2 (outputs/embedding_cache.json 에 캐시)
출력: data/processed/stack_map.json
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PROCESSED = ROOT / "data" / "processed"
NCS_CATALOG = ROOT / "data" / "ncs" / "ncs_units_official.json"
OUTPUT = PROCESSED / "stack_map.json"
DIMS = 512
TOP_K = 5
NCS_MIN = 0.30          # 이보다 낮으면 '관련 능력단위 없음'으로 봄 (모의 임베딩 기준 경험값)
TRAIN_MIN = 0.30
ALIAS_BONUS = 0.15      # 과정명에 스택 별칭이 그대로 있으면 가산
STACK_TYPES = ("기술", "직무지식")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def collect_stacks(extractions, skills):
    """→ {대표명: {"raw": {원문 표기}, "postings": {공고ID: 요건}}}, [사전에 없는 기술명]"""
    found, unknown = defaultdict(lambda: {"raw": set(), "postings": {}}), set()
    for row in extractions:
        for c in row.get("competencies", []):
            if c.get("type") not in STACK_TYPES:
                continue
            canon = skills.normalize(c["name"])
            if not canon:
                if c["type"] == "기술":
                    unknown.add(c["name"])
                continue
            found[canon]["raw"].add(c["name"])
            found[canon]["postings"].setdefault(row["id"], c.get("requirement", ""))
    return found, sorted(unknown)


def query_text(canon, info):
    return f"{canon} ({info['category']}). {info.get('desc', '')}".strip()


def matrix(texts, embed):
    m = np.array([embed(t) for t in texts], dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
    return m


def top(qvec, mat, k):
    scores = mat @ qvec
    idx = np.argsort(-scores)[:k]
    return [(float(scores[i]), int(i)) for i in idx]


def main():
    ap = argparse.ArgumentParser(description="요구 기술 스택 임베딩 → NCS·훈련과정 연결")
    ap.add_argument("--live", action="store_true", help="실제 Titan 임베딩 (BEDROCK_MOCK=0, AWS 로그인 필요)")
    ap.add_argument("--no-training", action="store_true", help="훈련과정 연결 생략")
    ap.add_argument("--output", type=Path, default=OUTPUT)
    a = ap.parse_args()

    from labutil import bedrock, config, skills
    config.BEDROCK_MOCK = not a.live
    backend = "titan" if a.live else "mock"

    def embed(t):
        return bedrock.embed(t, DIMS)

    found, unknown = collect_stacks(read_json(PROCESSED / "extractions.json"), skills)
    aliases = skills.load_aliases()
    targets = [s for s in sorted(found) if aliases[s].get("embed", True)]
    print(f"요구 스택 {len(found)}종 (임베딩 대상 {len(targets)}종, 사무·현장 {len(found) - len(targets)}종)"
          + (f" · 사전에 없는 기술명 {len(unknown)}개: {unknown}" if unknown else ""))

    units = read_json(NCS_CATALOG)
    ncs_mat = matrix([f"{u['unit_name']}. {u.get('description', '')} {u.get('detail', '')}".strip() for u in units], embed)
    print(f"NCS 능력단위 {len(units):,}개 임베딩 ({backend})")

    courses, titles, train_mat = [], [], None
    tpath = PROCESSED / "training_courses.json"
    if not a.no_training and tpath.exists():
        courses = read_json(tpath)
        titles = sorted({str(r.get("과정명", "")) for r in courses if r.get("과정명")})
        train_mat = matrix(titles, embed)
        print(f"훈련과정 과정명 {len(titles):,}종 임베딩")

    out = {}
    for canon in sorted(found):
        info = aliases[canon]
        f = found[canon]
        entry = {"canonical": canon, "category": info["category"], "field": info.get("field", "기타"),
                 "raw_names": sorted(f["raw"]), "postings": f["postings"], "demand": len(f["postings"]),
                 "weeks": info.get("weeks"), "prereq": info.get("prereq", []),
                 "embedded": info.get("embed", True), "backend": backend, "ncs": [], "training": []}
        if entry["embedded"]:
            q = query_text(canon, info)
            entry["query"] = q
            qv = np.array(embed(q), dtype=np.float32)
            qv /= np.linalg.norm(qv) + 1e-9
            entry["ncs"] = [{"unit_code": units[i]["unit_code"], "unit_name": units[i]["unit_name"],
                             "category": units[i].get("category", ""), "score": round(s, 4)}
                            for s, i in top(qv, ncs_mat, TOP_K) if s >= NCS_MIN]
            if train_mat is not None:
                pat = re.compile("|".join(re.escape(x) for x in info["aliases"] if len(x) >= 2), re.I)
                scores = train_mat @ qv
                scores = scores + np.array([ALIAS_BONUS if pat.search(t) else 0.0 for t in titles], dtype=np.float32)
                best = [(float(scores[i]), titles[i]) for i in np.argsort(-scores)[:TOP_K * 3] if scores[i] >= TRAIN_MIN]
                entry["training_titles"] = [{"과정명": t, "score": round(s, 4), "별칭일치": bool(pat.search(t))}
                                            for s, t in best[:TOP_K]]
        out[canon] = entry

    meta = {"_meta": {"backend": backend, "dims": DIMS, "embed_model": config.EMBED_MODEL_ID if a.live else "mock-ngram",
                      "ncs_catalog": NCS_CATALOG.name, "ncs_units": len(units), "training_titles": len(titles),
                      "unknown_stacks": unknown}}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps({**meta, **out}, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.live:
        bedrock.save_cache()
    linked = sum(bool(v["ncs"]) for v in out.values())
    print(f"NCS 후보가 있는 스택 {linked}/{len(targets)}종 → 저장: {a.output}")


if __name__ == "__main__":
    main()
