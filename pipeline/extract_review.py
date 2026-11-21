"""정답셋 검수 도우미 (담당: 조호성)

정답 초안과 Bedrock 결과가 '다른 것만' 골라서 검수 목록을 만들고, 판정 결과를 정답셋에 반영합니다.

    python pipeline/extract_review.py            # 1) 검수 목록 만들기 → data/eval/review_todo.csv / .md
    (엑셀로 review_todo.csv 의 '판정' 칸 채우기)
    python pipeline/extract_review.py --apply    # 2) 판정을 정답셋(gold_labels.csv)에 반영
    python pipeline/extract_review.py --agreed        # (추가) 초안·Bedrock 일치 공고만 확인 목록 → review_agreed.csv
    python pipeline/extract_review.py --apply-agreed  #       확인 결과 반영

'판정' 칸에 적는 법
    초안에만     (Bedrock이 놓친 것)    → 정답 / 오답
    Bedrock에만  (초안에 없는 것)       → 정답 / 정답:요구유형 / 오답 / 같음:초안이름
                                         (정답 → 정답셋에 추가, 같음 → 초안 항목의 다른 표기로 등록)
    요구유형 다름                       → 초안 / Bedrock / 직접 적기 (예: 필수|선택필수)
    초안 확인    (Bedrock 결과 없는 공고) → 정답 / 오답
    ※ 판정은 '누가 뽑았나'가 아니라 공고 원문을 보고 정하기
'사람확인' 칸: 판정을 사람이 확인했으면 O → 정답셋 검수 칸에 O, 아니면 'AI'로 기록됨
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "pipeline"))

from extract_competencies import prepare_text          # noqa: E402
from extract_eval import EVAL_DIR, SKIP_TYPES, keys, read_csv   # noqa: E402

POSTINGS = ROOT / "data" / "processed" / "postings_jobalio.json"
EXTRACTIONS = ROOT / "data" / "processed" / "extractions.json"
TODO_CSV = EVAL_DIR / "review_todo.csv"
TODO_MD = EVAL_DIR / "review_todo.md"
LABEL_COLS = ["posting_id", "name", "type", "requirement", "aliases", "검수", "메모"]
TODO_COLS = ["번호", "posting_id", "기관", "구분", "name", "type", "초안_요구유형", "Bedrock_요구유형",
             "판정", "판정근거", "사람확인", "메모"]


def gold_keys(row):
    ks = keys(row["name"], row["type"])
    for a in (row.get("aliases") or "").split("|"):
        if a.strip():
            ks |= keys(a, row["type"])
    return ks


def compare(gold_rows, preds):
    """같은 공고 안에서 초안(gold)과 Bedrock 결과를 짝지음 → (일치, 초안에만, Bedrock에만, 요구유형 다름)"""
    used, same, req_diff, bed_only = set(), [], [], []
    for p in preds:
        if p.get("type") in SKIP_TYPES:
            continue
        pk = keys(p.get("name"), p.get("type")) | keys(p.get("raw_name"), p.get("type"))
        hit = next((i for i, g in enumerate(gold_rows) if i not in used and pk & g["_keys"]), None)
        if hit is None:
            if not any(pk & g["_keys"] for g in gold_rows):
                bed_only.append(p)
            continue
        used.add(hit)
        g = gold_rows[hit]
        if p.get("requirement") in set(g["requirement"].split("|")):
            same.append(g)
        else:
            req_diff.append((g, p))
    gold_only = [g for i, g in enumerate(gold_rows) if i not in used]
    return same, gold_only, bed_only, req_diff


def make(force=False):
    if TODO_CSV.exists() and not force:
        done = [t for t in read_csv(TODO_CSV) if t.get("판정", "").strip()]
        if done:
            sys.exit(f"⛔ review_todo.csv 에 이미 판정 {len(done)}개가 적혀 있어서 덮어쓰지 않았습니다.\n"
                     "   판정을 반영하려면: python pipeline/extract_review.py --apply\n"
                     "   정말 새로 만들려면(기존 판정·사람확인 기록 삭제): --force")
    posts = {p["id"]: p for p in json.load(open(POSTINGS, encoding="utf-8"))}
    ext = {r["id"]: r for r in json.load(open(EXTRACTIONS, encoding="utf-8"))}
    plist = read_csv(EVAL_DIR / "gold_postings.csv")
    labels = read_csv(EVAL_DIR / "gold_labels.csv")
    by_pid = {}
    for r in labels:
        if r.get("검수", "").strip().upper() == "X" or r["type"].strip() in SKIP_TYPES:
            continue
        r["_keys"] = gold_keys(r)
        by_pid.setdefault(r["posting_id"].strip(), []).append(r)

    blocks = []
    for p in plist:
        pid = p["posting_id"].strip()
        gold_rows = by_pid.get(pid, [])
        bed = ext.get(pid)
        if not bed or bed.get("meta", {}).get("mode") != "bedrock":
            rows = [("초안 확인", g["name"], g["type"], g["requirement"], "") for g in gold_rows]
            blocks.append((pid, p, rows, [], "Bedrock 결과 없음 → 초안 전체 확인"))
            continue
        same, gold_only, bed_only, req_diff = compare(gold_rows, bed["competencies"])
        rows = [("초안에만", g["name"], g["type"], g["requirement"], "") for g in gold_only]
        rows += [("Bedrock에만", b.get("name"), b.get("type"), "", b.get("requirement")) for b in bed_only]
        rows += [("요구유형 다름", g["name"], g["type"], g["requirement"], b.get("requirement")) for g, b in req_diff]
        blocks.append((pid, p, rows, same, ""))

    # 쉬운 공고(검수할 줄이 적은 것)부터
    blocks.sort(key=lambda b: (len(b[2]) == 0, len(b[2])))
    todo, md, n = [], [HEADER], 0
    for pid, p, rows, same, note in blocks:
        x = posts.get(pid, {})
        md.append(f"\n---\n\n## {pid} · {p['기관']} · [{p['평가유형']}] · 검수할 것 {len(rows)}개 {note}\n")
        md.append("```\n" + prepare_text(x.get("text", "")) + "\n```\n")
        if same:
            md.append("**✅ 일치 (훑어보기만)**: " + ", ".join(f"{g['name']}({g['requirement']})" for g in same) + "\n\n")
        if not rows:
            md.append("**검수할 것 없음** (초안과 Bedrock이 완전히 같음)\n")
        for kind, name, typ, greq, breq in rows:
            n += 1
            todo.append({"번호": n, "posting_id": pid, "기관": p["기관"], "구분": kind, "name": name, "type": typ,
                         "초안_요구유형": greq, "Bedrock_요구유형": breq, "판정": "", "판정근거": "",
                         "사람확인": "", "메모": ""})
            extra = {"초안에만": f"초안: {greq}", "Bedrock에만": f"Bedrock: {breq}",
                     "요구유형 다름": f"초안 {greq} vs Bedrock {breq}", "초안 확인": f"초안: {greq}"}[kind]
            md.append(f"- **[{n}] {kind}**: {name} ({typ}) — {extra}\n")
    with open(TODO_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=TODO_COLS)
        w.writeheader()
        w.writerows(todo)
    TODO_MD.write_text("".join(md), encoding="utf-8")
    kinds = {}
    for t in todo:
        kinds[t["구분"]] = kinds.get(t["구분"], 0) + 1
    print(f"✅ 검수 목록 {len(todo)}개 → data/eval/review_todo.csv (보기용: review_todo.md)")
    for k, v in kinds.items():
        print(f"   {k}: {v}개")


def apply():
    todo = read_csv(TODO_CSV)
    labels = read_csv(EVAL_DIR / "gold_labels.csv")
    plist = read_csv(EVAL_DIR / "gold_postings.csv")
    undecided = [t for t in todo if not t.get("판정", "").strip()]
    if undecided:
        print(f"⚠ 판정이 비어 있는 줄 {len(undecided)}개는 건너뜀 (예: 번호 {undecided[0]['번호']})")

    def find(pid, name):
        return next((r for r in labels if r["posting_id"].strip() == pid and r["name"].strip() == name.strip()), None)

    added = changed = 0
    for t in todo:
        verdict, pid, kind = t.get("판정", "").strip(), t["posting_id"].strip(), t["구분"]
        if not verdict:
            continue
        who = "O" if t.get("사람확인", "").strip().upper() == "O" else "AI"
        word, _, arg = verdict.partition(":")
        word, arg = word.strip(), arg.strip()
        row = find(pid, t["name"])
        if kind in ("초안에만", "초안 확인") and row:
            row["검수"] = who if word == "정답" else "X"
            changed += 1
        elif kind == "Bedrock에만" and word == "정답" and not row:
            labels.append({"posting_id": pid, "name": t["name"], "type": t["type"],
                           "requirement": arg or t["Bedrock_요구유형"], "aliases": "", "검수": who,
                           "메모": "검수 때 추가 " + t.get("판정근거", "")})
            added += 1
        elif kind == "Bedrock에만" and word == "같음":
            target = find(pid, arg)
            if target is None:
                print(f"⚠ [{t['번호']}] '같음:{arg}' 의 초안 항목을 찾지 못함 → 건너뜀")
                continue
            al = [a for a in (target.get("aliases") or "").split("|") if a.strip()]
            if t["name"] not in al:
                target["aliases"] = "|".join(al + [t["name"]])
            changed += 1
        elif kind == "요구유형 다름" and row:
            row["requirement"] = {"초안": row["requirement"], "Bedrock": t["Bedrock_요구유형"]}.get(word, verdict)
            row["검수"] = who
            changed += 1
    # 검수완료: 공고의 모든 판정을 사람이 확인했으면 O, AI 판정만 있으면 AI
    pending = {t["posting_id"].strip() for t in undecided}
    ai_only = {t["posting_id"].strip() for t in todo if t.get("사람확인", "").strip().upper() != "O"}
    has_rows = {t["posting_id"].strip() for t in todo}
    for p in plist:
        pid = p["posting_id"].strip()
        if pid in pending:
            continue
        if pid not in has_rows:
            p["검수완료"] = "일치"          # 초안과 Bedrock이 완전히 같아서 판정할 게 없던 공고
        else:
            p["검수완료"] = "AI" if pid in ai_only else "O"
    for path, rows, cols in ((EVAL_DIR / "gold_labels.csv", labels, LABEL_COLS),
                             (EVAL_DIR / "gold_postings.csv", plist, list(plist[0].keys()))):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    done = sum(p["검수완료"] == "O" for p in plist)
    n_ai = sum(1 for t in todo if t.get("판정", "").strip() and t.get("사람확인", "").strip().upper() != "O")
    print(f"✅ 반영 완료: 수정 {changed}개, 추가 {added}개 / 사람이 확인 끝낸 공고 {done}/{len(plist)}건")
    if n_ai:
        print(f"   ⚠ 사람확인이 비어 있는 판정 {n_ai}개는 'AI 판정'으로 기록됨")
    print("   다음: python pipeline/extract_eval.py --details")


AGREED_CSV = EVAL_DIR / "review_agreed.csv"
AGREED_MD = EVAL_DIR / "review_agreed.md"


def make_agreed():
    """초안과 Bedrock이 완전히 같았던 공고(검수완료='일치')만 모아 사람 확인 목록 만들기."""
    posts = {p["id"]: p for p in json.load(open(POSTINGS, encoding="utf-8"))}
    plist = read_csv(EVAL_DIR / "gold_postings.csv")
    labels = read_csv(EVAL_DIR / "gold_labels.csv")
    agreed = [p for p in plist if p.get("검수완료", "").strip() == "일치"]
    if not agreed:
        sys.exit("'일치' 상태인 공고가 없습니다 (이미 전부 확인됨).")
    if AGREED_CSV.exists() and any(r.get("사람확인", "").strip() for r in read_csv(AGREED_CSV)):
        sys.exit("⛔ review_agreed.csv 에 이미 확인 표시가 있어 덮어쓰지 않았습니다. 반영: --apply-agreed")
    rows, md = [], [AGREED_HEADER]
    for n, p in enumerate(sorted(agreed, key=lambda p: p["posting_id"]), 1):
        pid = p["posting_id"].strip()
        labs = [r for r in labels if r["posting_id"].strip() == pid and r.get("검수", "").strip().upper() != "X"]
        summary = ", ".join(f"{r['name']}({r['requirement']})" for r in labs) or "(정답 없음)"
        rows.append({"번호": n, "posting_id": pid, "기관": p["기관"], "평가유형": p["평가유형"],
                     "정답수": len(labs), "정답목록": summary, "사람확인": "", "메모": ""})
        md.append(f"\n---\n\n## [{n}] {pid} · {p['기관']} · [{p['평가유형']}] · 정답 {len(labs)}개\n")
        md.append("```\n" + prepare_text(posts.get(pid, {}).get("text", "")) + "\n```\n")
        md.append("**정답 목록** (초안과 Bedrock이 똑같이 뽑은 것)\n")
        md.append("".join(f"- {r['name']} · {r['type']} · {r['requirement']}\n" for r in labs) or
                  "- (없음: 이 공고에서 뽑을 역량 없음)\n")
    with open(AGREED_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    AGREED_MD.write_text("".join(md), encoding="utf-8")
    print(f"✅ 확인할 공고 {len(rows)}건 (정답 {sum(r['정답수'] for r in rows)}개) → data/eval/review_agreed.csv "
          "(보기용: review_agreed.md)")


def apply_agreed():
    """사람확인=O 인 공고 → 정답셋 검수완료 O, 그 공고의 정답 검수 O."""
    checked = {r["posting_id"].strip() for r in read_csv(AGREED_CSV) if r.get("사람확인", "").strip().upper() == "O"}
    labels = read_csv(EVAL_DIR / "gold_labels.csv")
    plist = read_csv(EVAL_DIR / "gold_postings.csv")
    n_lab = 0
    for r in labels:
        if r["posting_id"].strip() in checked and r.get("검수", "").strip().upper() != "X":
            r["검수"] = "O"
            n_lab += 1
    for p in plist:
        if p["posting_id"].strip() in checked:
            p["검수완료"] = "O"
    for path, rows, cols in ((EVAL_DIR / "gold_labels.csv", labels, LABEL_COLS),
                             (EVAL_DIR / "gold_postings.csv", plist, list(plist[0].keys()))):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    done = sum(p["검수완료"] == "O" for p in plist)
    print(f"✅ 반영 완료: 공고 {len(checked)}건, 정답 {n_lab}개 확인 / 사람이 확인 끝낸 공고 {done}/{len(plist)}건")
    print("   다음: python pipeline/extract_eval.py")


AGREED_HEADER = """# 정답셋 확인 목록 — 초안과 Bedrock이 완전히 같았던 공고

- 공고 원문을 보고 **정답 목록이 맞으면** `review_agreed.csv`의 **사람확인** 칸에 `O`
- 틀린 게 있으면: `gold_labels.csv`에서 그 공고 줄을 직접 고치고(추가·삭제·수정), 사람확인 `O`
- "정답 없음" 공고는 원문에 가점·결격·"공고문 참조"만 있는지 확인하면 끝
- 다 적으면: `python pipeline/extract_review.py --apply-agreed`
"""


HEADER = """# 정답셋 검수 목록 (초안과 Bedrock이 다른 것만)

- 판정은 `review_todo.csv`(엑셀)의 **판정** 칸에 적기. 이 파일은 읽기용.
- 초안에만 / Bedrock에만 / 초안 확인 → `정답` 또는 `오답`
- 요구유형 다름 → `초안` / `Bedrock` / 직접 적기(예: `필수|선택필수`)
- **원문을 보고** 판정하기 (누가 뽑았는지로 정하지 않기)
- 라벨 기준: 가점·결격·연령·병역·주소지·"공고문 참조"·이름 없는 "관련 자격증"·직무 설명·조건부 인정은 역량 아님
- 다 적으면: `python pipeline/extract_review.py --apply`
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="정답셋 검수 도우미")
    ap.add_argument("--apply", action="store_true", help="판정 결과를 정답셋에 반영")
    ap.add_argument("--force", action="store_true", help="판정이 적힌 review_todo.csv 도 새로 덮어쓰기")
    ap.add_argument("--agreed", action="store_true", help="초안·Bedrock 일치 공고 확인 목록 만들기")
    ap.add_argument("--apply-agreed", action="store_true", help="일치 공고 확인 결과 반영")
    a = ap.parse_args()
    if a.agreed:
        make_agreed()
    elif a.apply_agreed:
        apply_agreed()
    else:
        apply() if a.apply else make(force=a.force)
