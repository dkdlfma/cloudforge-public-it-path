"""잡알리오(공공기관 채용정보) 정보통신 공고 전체 수집 → 표준 형식으로 저장

실행:
    python pipeline/collect_jobalio.py                # 전체 수집
    python pipeline/collect_jobalio.py --max-pages 2  # 테스트 (200건만)

동작:
    1) 100건씩 페이지를 받아 data/raw/jobalio_pages/page_001.json ... 으로 저장
       - 이미 받은 페이지는 건너뜀 → 중간에 끊겨도 다시 실행하면 이어서 받음
    2) 모든 페이지를 합쳐 중복 제거 → 표준 형식으로 변환
    3) data/processed/postings_jobalio.json (+ 확인용 .csv) 저장
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "jobalio_pages"
OUT_DIR = ROOT / "data" / "processed"

NCS_IT = "R600020"   # NCS 대분류 20 = 정보통신
ROWS = 100           # 한 번에 받을 건수 (API 최대값 기준)


# ------------------------------------------------------------------ 1) 수집
def load_settings():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    url = os.getenv("JOBALIO_URL")
    key = os.getenv("DATA_GO_KR_API_KEY")
    if not url or not key:
        sys.exit("❌ .env 에 JOBALIO_URL 과 DATA_GO_KR_API_KEY 가 있어야 합니다.")
    return url, unquote(key)


def fetch_page(url, key, page, retries=3):
    """페이지 1개 요청 → 응답 dict. 실패하면 이유를 출력하고 종료."""
    import requests
    params = {"serviceKey": key, "pageNo": page, "numOfRows": ROWS,
              "resultType": "json", "ncsCdLst": NCS_IT}
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.text.lstrip().startswith("<"):          # 포털 공통 오류는 XML로 옴
                sys.exit(f"❌ API 오류 (페이지 {page}):\n{r.text[:500]}")
            data = r.json()
            if data.get("resultCode") != 200:
                sys.exit(f"❌ API 오류 (페이지 {page}): {data.get('resultCode')} {data.get('resultMsg')}")
            return data
        except (requests.RequestException, ValueError) as e:
            print(f"  ⚠ 페이지 {page} 재시도 {i + 1}/{retries}: {str(e)[:100]}")
            time.sleep(2 ** i)
    sys.exit(f"❌ 페이지 {page} 를 받지 못했습니다. 잠시 후 다시 실행하면 이어서 받습니다.")


def collect(max_pages=None):
    url, key = load_settings()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    first_path = RAW_DIR / "page_001.json"
    if first_path.exists():
        first = json.load(open(first_path, encoding="utf-8"))
    else:
        first = fetch_page(url, key, 1)
        json.dump(first, open(first_path, "w", encoding="utf-8"), ensure_ascii=False)

    total = int(first["totalCount"])
    pages = (total + ROWS - 1) // ROWS
    if max_pages:
        pages = min(pages, max_pages)
    print(f"📥 정보통신 공고 {total:,}건 → {pages}페이지 수집")

    for p in range(2, pages + 1):
        path = RAW_DIR / f"page_{p:03d}.json"
        if path.exists():
            continue
        data = fetch_page(url, key, p)
        json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        if p % 10 == 0 or p == pages:
            print(f"  {p}/{pages} 페이지 완료")
        time.sleep(0.3)   # 서버 부담 줄이기
    return pages


# ------------------------------------------------------------------ 2) 표준 형식 변환
def _split(v):
    return [s.strip() for s in str(v or "").split(",") if s.strip()]


def _clean(v):
    s = str(v or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return "" if s in ("None", "null") else s


def _date(ymd):
    s = str(ymd or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else None


def to_text(item):
    """기술 추출에 넣을 본문. 실습 코드(textprep.sections)가 읽는 [구역] 형식으로 맞춤."""
    parts = [f"[공고제목]\n{_clean(item.get('recrutPbancTtl'))}"]
    for title, key in (("자격요건", "aplyQlfcCn"), ("우대사항", "prefCn"), ("우대조건", "prefCondCn")):
        body = _clean(item.get(key))
        if body:
            parts.append(f"[{title}]\n{body}")
    return "\n\n".join(parts)


def normalize(item):
    ncs = _split(item.get("ncsCdNmLst"))
    return {
        "id": f"JA-{item.get('recrutPblntSn')}",
        "source": "jobalio",
        "company": _clean(item.get("instNm")),
        "title": _clean(item.get("recrutPbancTtl")),
        "ncs": ncs,                                   # NCS 대분류 이름 목록
        "it_only": ncs == ["정보통신"],               # 정보통신 단독 공고인지 (여러 분야 묶음 공고 구분용)
        "region": _clean(item.get("workRgnNmLst")),
        "hire_type": _clean(item.get("hireTypeNmLst")),
        "career": _clean(item.get("recrutSeNm")),
        "education": _clean(item.get("acbgCondNmLst")),
        "headcount": _clean(item.get("recrutNope")),
        "posted_at": _date(item.get("pbancBgngYmd")),
        "close_at": _date(item.get("pbancEndYmd")),
        "ongoing": item.get("ongoingYn") == "Y",
        "url": _clean(item.get("srcUrl")),
        "text": to_text(item),
    }


def build():
    items = {}
    for path in sorted(RAW_DIR.glob("page_*.json")):
        for it in json.load(open(path, encoding="utf-8")).get("result", []):
            items[it.get("recrutPblntSn")] = it          # 같은 공고가 여러 페이지에 나오면 하나만
    rows = [normalize(it) for it in items.values()]
    rows.sort(key=lambda r: r["posted_at"] or "", reverse=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "postings_jobalio.json"
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:                                                 # 엑셀로 열어보기용 (pandas가 있으면)
        import pandas as pd
        df = pd.DataFrame(rows)
        df["ncs"] = df["ncs"].map(", ".join)
        df.drop(columns=["text"]).to_csv(OUT_DIR / "postings_jobalio.csv", index=False, encoding="utf-8-sig")
    except ImportError:
        pass
    return rows, out


def summary(rows):
    print(f"\n✅ 저장 완료: 공고 {len(rows):,}건")
    print(f"  - 정보통신 단독 공고: {sum(r['it_only'] for r in rows):,}건")
    print(f"  - 진행 중 공고: {sum(r['ongoing'] for r in rows):,}건")
    years = Counter((r["posted_at"] or "????")[:4] for r in rows)
    print("  - 연도별:", dict(sorted(years.items())))
    print("  - 지역 Top 10:", Counter(r["region"] for r in rows).most_common(10))
    print("  - 고용형태 Top 5:", Counter(r["hire_type"] for r in rows).most_common(5))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, help="테스트용: 이 페이지 수까지만 수집")
    ap.add_argument("--build-only", action="store_true", help="수집 없이 저장된 페이지로 변환만")
    args = ap.parse_args()
    if not args.build_only:
        collect(args.max_pages)
    rows, out = build()
    summary(rows)
    print("📄", out.relative_to(ROOT))
