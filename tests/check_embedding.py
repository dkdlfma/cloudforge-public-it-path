"""임베딩·매칭·추천이 제대로 도는지 한 번에 점검   (담당: 강민경)

  python tests/check_embedding.py          # 모의 임베딩 기준 (비용 0)
  python tests/check_embedding.py --live   # 실제 Titan 임베딩으로 같은 점검 (AWS 로그인 필요, 비용 소액)

점검 항목
  [1] 임베딩 함수: 512차원 · 길이 1(정규화) · 같은 문장은 캐시에서 같은 벡터
  [2] 스택 사전: 추출된 '기술' 이름이 전부 대표명으로 정규화되는지
  [3] 검색 품질: 정답 능력단위(gold)가 상위 5개 안에 드는 비율(hit@5)
        A = 스택 이름만으로 검색 (예전 방식)   B = 이름 + 사전 설명 (embed_stacks.py 방식)
  [4] stack_map.json: 형식과 연결 결과
  [5] 추천 엔진: 추출된 모든 공고에 대해 예외 없이 결과가 나오는지 + 스택 판정이 맞는지
실패가 하나라도 있으면 종료 코드 1.
"""
import argparse
import json
import math
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

# 스택 → 정답으로 볼 능력단위 이름 (공식 카탈로그에 있는 것만, 하나라도 상위 5 안이면 정답)
GOLD = {
    "Docker": ["클라우드 인프라스트럭쳐 서비스 구성", "클라우드 인프라스트럭쳐 서비스 배포", "PaaS 배포"],
    "Kubernetes": ["클라우드 인프라스트럭쳐 서비스 배포", "클라우드 인프라스트럭쳐 서비스 구성", "PaaS 운영"],
    "CI/CD": ["테스트 자동화", "PaaS 배포", "클라우드 솔루션 배포 방안 수립", "시스템 형상관리",
              "클라우드 인프라스트럭쳐 서비스 배포"],
    "MySQL": ["데이터베이스 구현", "물리 데이터베이스 설계", "SQL활용", "DB 운영관리"],
    "MongoDB": ["데이터베이스 구현", "물리 데이터베이스 설계", "DB 운영관리"],
    "SQL 튜닝": ["데이터베이스 성능확보", "SQL응용", "SQL활용"],
    "Spring Boot": ["PaaS 백엔드 개발", "PaaS 백엔드 설계", "서버 연동"],
    "React.js": ["PaaS 프론트엔드 개발", "PaaS 프론트엔드 설계", "UI 구현"],
    "MSA": ["SW아키텍처 설계", "클라우드 솔루션 구조 설계", "PaaS 아키텍처 수립"],
    "AWS": ["클라우드 인프라스트럭쳐 서비스 구성", "클라우드 서버 서비스 설계", "클라우드 솔루션 구조 설계"],
    "Linux": ["IT시스템 서버 운영관리", "서버 구성"],
}

results = []


def check(name, ok, detail="", warn=False):
    """warn=True 는 코드가 아니라 데이터 문제 → ⚠ 로 표시하고 실패로 세지 않음"""
    ok = bool(ok)
    if not warn:
        results.append(ok)
    print(f"  {'✅' if ok else ('⚠️' if warn else '❌')} {name}" + (f" — {detail}" if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    import numpy as np
    from labutil import bedrock, config, skills
    config.BEDROCK_MOCK = not a.live
    mode = "Titan" if a.live else "모의(n-gram)"
    print(f"임베딩 모드: {mode}\n")

    print("[1] 임베딩 함수")
    v1 = bedrock.embed("Docker 컨테이너 배포", 512)
    v2 = bedrock.embed("Docker 컨테이너 배포", 512)
    check("512차원", len(v1) == 512, f"{len(v1)}차원")
    norm = math.sqrt(sum(x * x for x in v1))
    check("정규화(길이 1)", abs(norm - 1) < 1e-3, f"길이 {norm:.4f}")
    check("캐시 일관성", v1 == v2)
    va, vb, vc = (np.array(bedrock.embed(t, 512)) for t in ("MySQL 데이터베이스", "관계형 데이터베이스 구현", "고소작업차 운전"))
    check("의미가 가까운 문장이 더 가까움", float(va @ vb) > float(va @ vc),
          f"DB↔DB {float(va @ vb):.3f} > DB↔고소작업차 {float(va @ vc):.3f}")

    print("\n[2] 스택 사전 정규화")
    ex = json.loads((ROOT / "data/processed/extractions.json").read_text(encoding="utf-8-sig"))
    raw = sorted({c["name"] for r in ex for c in r["competencies"] if c.get("type") == "기술"})
    canon, unknown = skills.normalize_list(raw)
    check(f"추출된 기술명 {len(raw)}개 → 대표명 {len(canon)}개", not unknown,
          f"사전에 없는 이름: {unknown}" if unknown else "모두 정규화됨")
    for a_, b_ in [("엑셀", "Excel"), ("EXCEL", "Excel"), ("한글(HWP)", "한글(HWP)"), ("k8s", "Kubernetes"), ("스프링부트", "Spring Boot")]:
        check(f"'{a_}' → '{b_}'", skills.normalize(a_) == b_, str(skills.normalize(a_)))

    print("\n[3] 검색 품질 hit@5 (스택 → NCS 능력단위)")
    units = json.loads((ROOT / "data/ncs/ncs_units_official.json").read_text(encoding="utf-8"))
    names = [u["unit_name"] for u in units]
    have = set(names)
    mat = np.array([bedrock.embed(f"{u['unit_name']}. {u.get('description', '')} {u.get('detail', '')}".strip(), 512)
                    for u in units], dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    aliases = skills.load_aliases()

    def top5(q):
        qv = np.array(bedrock.embed(q, 512), dtype=np.float32)
        qv /= np.linalg.norm(qv) + 1e-9
        return [names[i] for i in np.argsort(-(mat @ qv))[:5]]

    hits = {"A": 0, "B": 0}
    print(f"  {'스택':<12} {'A 이름만':^8} {'B 이름+설명':^10}  B 1위")
    for stack, gold in GOLD.items():
        gold = [g for g in gold if g in have]
        info = aliases[stack]
        ra, rb = top5(stack), top5(f"{stack} ({info['category']}). {info['desc']}")
        ha, hb = bool(set(ra) & set(gold)), bool(set(rb) & set(gold))
        hits["A"] += ha
        hits["B"] += hb
        print(f"  {stack:<12} {'○' if ha else '✕':^8} {'○' if hb else '✕':^10}  {rb[0]}")
    n = len(GOLD)
    print(f"  → A {hits['A']}/{n} ({hits['A'] / n:.0%})   B {hits['B']}/{n} ({hits['B'] / n:.0%})")
    check("설명을 붙인 검색(B)이 이름만(A)보다 같거나 좋음", hits["B"] >= hits["A"])
    check("B hit@5 ≥ 70%", hits["B"] / n >= 0.7, f"{hits['B'] / n:.0%}")
    sw = [u for u in units if u["unit_code"].startswith("20010202")]
    check("NCS 카탈로그에 응용SW엔지니어링(20010202) 포함", bool(sw),
          f"{len(sw)}개" if sw else "없음 → Java·Spring 같은 개발 스택의 정답 단위(서버프로그램 구현 등)가 빠져 있음", warn=True)

    print("\n[4] stack_map.json")
    p = ROOT / "data/processed/stack_map.json"
    if not p.exists():
        check("파일 존재", False, "python pipeline/embed_stacks.py 를 먼저 실행")
    else:
        sm = json.loads(p.read_text(encoding="utf-8"))
        meta = sm.pop("_meta", {})
        check("파일 존재", True, f"백엔드 {meta.get('backend')} · 스택 {len(sm)}종")
        emb = [v for v in sm.values() if v["embedded"]]
        check("IT 스택마다 NCS 후보 있음", all(v["ncs"] for v in emb),
              ", ".join(k for k, v in sm.items() if v["embedded"] and not v["ncs"]) or f"{len(emb)}종 모두")
        check("IT 스택마다 훈련과정 후보 있음", all(v.get("training_titles") for v in emb),
              ", ".join(k for k, v in sm.items() if v["embedded"] and not v.get("training_titles")) or f"{len(emb)}종 모두")
        check("사무·현장 스택은 임베딩 제외", all(not v["ncs"] for v in sm.values() if not v["embedded"]))

    print("\n[5] 추천 엔진")
    from recommend import engine, stacks
    fails = []
    for r in ex:
        try:
            rec = engine.recommend(r["id"], {"stacks": ["Java"]})
            assert rec["ok"] and "stacks" in rec
        except Exception:  # noqa: BLE001
            fails.append((r["id"], traceback.format_exc(limit=1).strip().splitlines()[-1]))
    check(f"추출된 공고 {len(ex)}건 모두 추천 생성", not fails, str(fails[:3]) if fails else "")
    dev = next((r["id"] for r in ex if any(c["name"] == "Spring Boot" for c in r["competencies"])), None)
    if dev:
        s = stacks.for_posting(dev, held=["Java", "mysql"])
        order = [x["name"] for x in s["path"]]
        check("보유 스택은 부족 목록에서 빠짐 (Java, mysql→MySQL)",
              "Java" not in order and "MySQL" not in order and set(s["held"]) == {"Java", "MySQL"}, str(s["held"]))
        check("선행 순서: Docker → Kubernetes", order.index("Docker") < order.index("Kubernetes"))
        check("선행 순서: Spring Boot·Docker → MSA",
              order.index("Spring Boot") < order.index("MSA") and order.index("Docker") < order.index("MSA"))
        s2 = stacks.for_posting(dev)
        o2 = [x["name"] for x in s2["path"]]
        check("Java 가 없으면 Spring Boot 보다 먼저", o2.index("Java") < o2.index("Spring Boot"))
    office = next((r["id"] for r in ex if any(c["name"] in ("Excel", "EXCEL", "엑셀") for c in r["competencies"])), None)
    if office:
        s = stacks.for_posting(office)
        check("사무 도구(Excel 등)는 학습경로가 아닌 '기타 요건'으로", not s["path"] and s["other"],
              ", ".join(x["canonical"] for x in s["other"]))

    print(f"\n결과: {sum(results)}/{len(results)} 통과")
    if a.live:
        bedrock.save_cache()
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
