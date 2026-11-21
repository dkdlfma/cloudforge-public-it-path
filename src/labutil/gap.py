"""Skills Gap 계산 도우미 (5주차) — 수요(채용공고) vs 공급(교육과정)"""
from collections import defaultdict

from .skills import normalize_list, rule_extract
from .textprep import sections

WEIGHT = {"자격요건": 1.0, "주요업무": 0.7, "우대사항": 0.5}


def skills_by_section(text):
    """공고 본문 → {구역: [대표 기술, ...]}"""
    return {sec: normalize_list(rule_extract("\n".join(lines)))[0] for sec, lines in sections(text).items()}


def posting_weights(text, weight=WEIGHT):
    """공고 1건 → {기술: 가중치}. 같은 기술이 여러 구역에 나오면 가장 큰 가중치."""
    out = {}
    for sec, skills in skills_by_section(text).items():
        for s in skills:
            out[s] = max(out.get(s, 0), weight.get(sec, 0.5))
    return out


def demand_rates(df, weight=WEIGHT):
    """산업별 수요율: {산업: {기술: 가중치 합 / 공고 수}}"""
    total, count = defaultdict(lambda: defaultdict(float)), defaultdict(int)
    for _, r in df.iterrows():
        count[r["industry"]] += 1
        for s, w in posting_weights(r["text"], weight).items():
            total[r["industry"]][s] += w
    return {ind: {s: v / count[ind] for s, v in sk.items()} for ind, sk in total.items()}


def supply_rates(courses):
    """기술별 공급율: 그 기술을 다루는 교과목 수 / 전체 교과목 수"""
    cnt = defaultdict(int)
    for _, r in courses.iterrows():
        text = f"{r['asubjName']} {r['eduText']} {r['subjGoal']}"
        for s in normalize_list(rule_extract(text))[0]:
            cnt[s] += 1
    return {s: c / len(courses) for s, c in cnt.items()}


def to_share(rates):
    """{기술: 값} → {기술: 전체 합에서 차지하는 비중(0~1)}"""
    total = sum(rates.values()) or 1.0
    return {k: v / total for k, v in rates.items()}


def gap_table(demand, supply):
    """비중 차이(%p): {산업: {기술: (수요 비중 - 공급 비중) × 100}}
    + 이면 시장은 원하는데 교육이 덜 다루는 기술 (6주차 추천 대상), - 이면 교육이 상대적으로 많이 다루는 기술"""
    sup = to_share(supply)
    out = {}
    for ind, sk in demand.items():
        dem = to_share(sk)
        out[ind] = {s: round((dem.get(s, 0) - sup.get(s, 0)) * 100, 1) for s in set(dem) | set(sup)}
    return out


def long_table(demand, supply):
    """대시보드·저장용 긴 표: 산업, 기술, 수요 비중(%), 공급 비중(%), Gap(%p)"""
    import pandas as pd
    sup = to_share(supply)
    rows = []
    for ind, sk in demand.items():
        dem = to_share(sk)
        for s in sorted(set(dem) | set(sup)):
            d, p = dem.get(s, 0) * 100, sup.get(s, 0) * 100
            rows.append({"industry": ind, "skill": s, "demand_pct": round(d, 1), "supply_pct": round(p, 1), "gap_pp": round(d - p, 1)})
    return pd.DataFrame(rows)
