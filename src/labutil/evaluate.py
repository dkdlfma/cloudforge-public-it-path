"""역량 추출 정확도 평가 — 정답셋과 비교해 정밀도·재현율·F1 계산"""


def score(pred, gold):
    p, g = set(pred), set(gold)
    tp = len(p & g)
    precision = tp / len(p) if p else 0.0
    recall = tp / len(g) if g else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "missed": sorted(g - p), "extra": sorted(p - g)}


def evaluate(preds: dict, golds: dict):
    """preds/golds: {공고ID: [대표명...]} → (요약, 공고별 결과)"""
    rows = []
    for pid, gold in golds.items():
        s = score(preds.get(pid, []), gold)
        rows.append({"id": pid, **s})
    n = len(rows) or 1
    summary = {k: round(sum(r[k] for r in rows) / n, 3) for k in ("precision", "recall", "f1")}
    return summary, rows
