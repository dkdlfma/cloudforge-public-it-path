"""벡터 계산·검색 (5주차) — 원리를 보려고 직접 만든 아주 작은 벡터 검색"""
import math


def cosine(a, b):
    """코사인 유사도: 방향이 같을수록 1, 무관할수록 0"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def search(query_vec, index, k=5):
    """index: [(id, 벡터), ...] → 가장 가까운 k개 [(점수, id), ...]"""
    scored = [(cosine(query_vec, v), i) for i, v in index]
    return sorted(scored, reverse=True)[:k]


def hit_at_k(results, gold, k=5):
    """results: {질의: [id, ...]} / gold: {질의: [정답 id, ...]} → 상위 k개 안에 정답이 하나라도 있는 비율"""
    hits = sum(1 for q, ids in results.items() if set(ids[:k]) & set(gold.get(q, [])))
    return hits / len(results) if results else 0.0
