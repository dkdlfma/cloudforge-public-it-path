"""역량 추출 성능 평가 (담당: 조호성)

정답셋(data/eval/gold_*.csv)과 비교해서 규칙 기반 vs Bedrock 점수를 계산합니다. AI 호출 없음(비용 0).

    python pipeline/extract_eval.py              # 전체 점수 + 유형별 점수
    python pipeline/extract_eval.py --details    # 틀린 항목(놓친 것 / 잘못 뽑은 것)까지 출력

점수 뜻
    Precision(정밀도) : 뽑은 것 중 맞은 비율   → 엉뚱한 걸 덜 뽑을수록 높음
    Recall(재현율)    : 정답 중 찾아낸 비율    → 빠뜨린 게 적을수록 높음
    F1               : 둘의 조화평균 (종합 점수)
    Accuracy(요구유형): 맞게 찾은 역량 중 필수/선택필수/우대를 맞힌 비율

채점 규칙
    - 같은 공고 안에서 이름이 같으면 맞음 (띄어쓰기·괄호·등급(1급/2급) 무시, 정답의 aliases 표기도 인정)
    - 경력은 연수만 비교 ("관련 경력 3년 이상" = "경력 3년 이상")
    - 직무지식은 사람마다 판단이 달라 채점에서 제외
결과: data/eval/eval_report.md (발표 자료용 표)
"""
import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "pipeline"))

from extract_competencies import normalize_name   # noqa: E402
from extract_rules import rule_extract            # noqa: E402

EVAL_DIR = ROOT / "data" / "eval"
POSTINGS = ROOT / "data" / "processed" / "postings_jobalio.json"
EXTRACTIONS = ROOT / "data" / "processed" / "extractions.json"
SKIP_TYPES = {"직무지식"}
CATS = ["일반", "기술명 표현 다양", "필수·우대 복잡", "단순 언급", "부정·조건부", "새 기술"]


def read_csv(path):
    for enc in ("utf-8-sig", "cp949"):          # 엑셀이 저장 형식을 바꿔도 읽히게
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    sys.exit(f"❌ {path} 를 읽지 못했습니다. 엑셀에서 'CSV UTF-8' 형식으로 저장해 주세요.")


def key(name, ctype=""):
    """비교용 이름: 띄어쓰기·괄호·등급 무시, 경력은 연수만."""
    s = str(name or "").strip()
    if ctype == "자격증":
        s = normalize_name(s, "자격증")
    s = s.lower()
    if ctype == "경력" or re.match(r"^(관련\s*)?(업무\s*)?경력", s):
        m = re.search(r"(\d+)\s*(년|개월)", s)
        return "경력" + (m.group(1) + m.group(2) if m else "")
    s = re.sub(r"\d\s*급(\s*이상)?", "", s)                 # 1급/2급 무시
    s = re.sub(r"\s*(자격증|자격|소지자|보유자|취득자|학위|이상|수료|이수자|이수)\s*$", "", s.strip())
    s = re.sub(r"[\s()\[\]·,./\-_+]", "", s)
    return s


def keys(name, ctype=""):
    """괄호 안 설명을 뺀 이름도 같이 비교 ('운전면허증(1종 보통 이상)' = '운전면허증')."""
    out = {key(name, ctype)}
    bare = re.sub(r"\([^)]*\)", "", str(name or "")).strip()
    if bare and bare != name:
        out.add(key(bare, ctype))
    return out - {""}


def load_gold():
    posts = read_csv(EVAL_DIR / "gold_postings.csv")
    labels = read_csv(EVAL_DIR / "gold_labels.csv")
    gold = {p["posting_id"].strip(): {"cat": p["평가유형"].strip(), "items": []} for p in posts}
    for r in labels:
        pid = r["posting_id"].strip()
        if pid not in gold or r.get("검수", "").strip().upper() == "X":
            continue
        if r["type"].strip() in SKIP_TYPES:
            continue
        ks = keys(r["name"], r["type"].strip())
        for a in (r.get("aliases") or "").split("|"):
            if a.strip():
                ks |= keys(a, r["type"].strip())
        reqs = {x.strip() for x in r["requirement"].split("|") if x.strip()}
        gold[pid]["items"].append({"name": r["name"], "keys": ks, "reqs": reqs})
    return gold


def score_posting(gold_items, preds):
    """같은 공고 안에서 예측과 정답을 1:1로 짝지음."""
    used, matched, fps = set(), [], []
    for p in preds:
        if p.get("type") in SKIP_TYPES:
            continue
        pk = keys(p.get("name"), p.get("type")) | keys(p.get("raw_name"), p.get("type"))
        hit = next((i for i, g in enumerate(gold_items) if i not in used and pk & g["keys"]), None)
        if hit is None:
            # 같은 정답을 두 번 뽑은 경우(중복)는 오답으로 치지 않음
            if not any(pk & g["keys"] for g in gold_items):
                fps.append(p)
            continue
        used.add(hit)
        matched.append((gold_items[hit], p))
    fns = [g for i, g in enumerate(gold_items) if i not in used]
    return matched, fps, fns


def evaluate(gold, predict):
    tot = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "req_ok": 0, "n": 0})
    details = []
    for pid, g in gold.items():
        preds = predict(pid)
        if preds is None:
            continue
        matched, fps, fns = score_posting(g["items"], preds)
        req_ok = sum(1 for gi, p in matched if p.get("requirement") in gi["reqs"])
        for c in ("전체", g["cat"]):
            t = tot[c]
            t["tp"] += len(matched); t["fp"] += len(fps); t["fn"] += len(fns)
            t["req_ok"] += req_ok; t["n"] += 1
        details.append((pid, g["cat"], matched, fps, fns))
    return tot, details


def width(text):
    """화면 폭: 한글·전각 문자는 2칸."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def md_table(headers, rows, right_from=99):
    """칸 너비를 맞춘 마크다운 표 (메모장·터미널에서도 줄이 맞게). right_from 번째 열부터 오른쪽 정렬."""
    cols = list(zip(headers, *rows))
    ws = [max(width(v) for v in col) for col in cols]

    def cell(v, i):
        pad = " " * (ws[i] - width(v))
        return pad + str(v) if i >= right_from else str(v) + pad

    def line(vals):
        return "| " + " | ".join(cell(v, i) for i, v in enumerate(vals)) + " |"

    sep = "|" + "|".join(("-" * (w + 1) + ":") if i >= right_from else ("-" * (w + 2)) for i, w in enumerate(ws)) + "|"
    return [line(headers), sep] + [line(r) for r in rows]


def prf(t):
    p = t["tp"] / (t["tp"] + t["fp"]) if t["tp"] + t["fp"] else 0.0
    r = t["tp"] / (t["tp"] + t["fn"]) if t["tp"] + t["fn"] else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    a = t["req_ok"] / t["tp"] if t["tp"] else 0.0
    return p, r, f, a


def main():
    ap = argparse.ArgumentParser(description="역량 추출 성능 평가 (규칙 vs Bedrock)")
    ap.add_argument("--details", action="store_true", help="틀린 항목까지 출력")
    args = ap.parse_args()

    gold = load_gold()
    posts = {p["id"]: p for p in json.load(open(POSTINGS, encoding="utf-8"))}
    ext = {r["id"]: r for r in json.load(open(EXTRACTIONS, encoding="utf-8"))}
    missing = [pid for pid in gold if pid not in posts]
    if missing:
        print(f"⚠ 공고 파일에 없는 정답셋 공고 {len(missing)}건은 빼고 채점: {missing}")
    no_bed = [pid for pid in gold if ext.get(pid, {}).get("meta", {}).get("mode") != "bedrock"]
    if no_bed:
        print(f"⚠ Bedrock 결과가 없는 공고 {len(no_bed)}건은 두 방식 모두 채점에서 제외 (같은 공고로 비교하려고): {no_bed}")
        print("   → BEDROCK_MOCK=0 상태에서: python pipeline/extract_competencies.py --id " + " ".join(no_bed))

    systems = {
        "규칙 기반": lambda pid: rule_extract(posts[pid])["competencies"] if pid in posts and pid not in no_bed else None,
        "Bedrock (Haiku)": lambda pid: ext[pid]["competencies"] if pid not in no_bed else None,
    }
    reviewed = sum(1 for p in read_csv(EVAL_DIR / "gold_postings.csv") if p.get("검수완료", "").strip().upper() == "O")
    n_items = sum(len(g["items"]) for g in gold.values())
    marks = [r.get("검수", "").strip().upper() for r in read_csv(EVAL_DIR / "gold_labels.csv")
             if r.get("검수", "").strip().upper() != "X"]
    human, ai_only = marks.count("O"), marks.count("AI")

    scored = len(gold) - len(set(no_bed) | set(missing))
    out = ["# 역량 추출 성능 평가 (규칙 기반 vs Bedrock)\n",
           f"- 정답셋: 공고 {len(gold)}건, 정답 역량 {n_items}개 (사람이 확인 끝낸 공고 {reviewed}/{len(gold)}건)",
           f"- 채점한 공고: {scored}건 (두 방식 모두 같은 공고로 비교)",
           f"- 정답 역량 판정: 사람 확인 {human}개 / AI 판정만 {ai_only}개 / 판정 없음(초안·Bedrock 일치) {len(marks) - human - ai_only}개",
           "- 정답셋 작성: AI 초안 → 초안과 Bedrock이 다른 항목만 판정 / 직무지식은 채점 제외\n",
           "## 전체 점수\n"]
    results, rows = {}, []
    for name, fn in systems.items():
        tot, det = evaluate(gold, fn)
        results[name] = (tot, det)
        p, r, f, a = prf(tot["전체"])
        rows.append([name, f"{p:.2f}", f"{r:.2f}", f"{f:.2f}", f"{a:.2f}"])
    out += md_table(["방식", "Precision", "Recall", "F1", "요구유형 Accuracy"], rows, right_from=1)

    out.append("\n## 유형별 F1\n")
    rows = []
    for c in CATS:
        n = results["규칙 기반"][0][c]["n"]
        if n:
            rows.append([c, str(n)] + [f"{prf(results[s][0][c])[2]:.2f}" for s in results])
    out += md_table(["유형", "공고 수"] + list(results), rows, right_from=1)

    report = "\n".join(out) + "\n"
    print(report)
    (EVAL_DIR / "eval_report.md").write_text(report, encoding="utf-8")
    print(f"✅ 저장: data/eval/eval_report.md")

    if args.details:
        for name, (_, det) in results.items():
            print(f"\n================ {name}: 틀린 항목 ================")
            for pid, cat, matched, fps, fns in det:
                if not fps and not fns:
                    continue
                print(f"\n[{pid}] ({cat})")
                for g in fns:
                    print(f"   놓침   : {g['name']}")
                for p in fps:
                    print(f"   잘못뽑음: {p.get('name')} ({p.get('type')})")


if __name__ == "__main__":
    main()
