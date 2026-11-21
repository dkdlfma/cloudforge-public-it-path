"""NCS 교육과정(교과목) 받아서 저장 — 공공데이터포털 '한국산업인력공단_NCS 교육과정' API
사용: python -m recommend.fetch_courses            (정보통신 대분류 20, 최대 30페이지)
      python -m recommend.fetch_courses --pages 5  (테스트용으로 조금만)
결과: data/processed/ncs_courses.json  (.env 의 DATA_GO_KR_API_KEY 필요)"""
import argparse
import json

from . import data_io  # noqa: F401  (src 경로 등록)
from labutil import ncs_api

OUT = data_io.PROCESSED / "ncs_courses.json"
KEEP = ["대분류", "중분류", "소분류", "세분류", "세분류코드", "학교구분", "학제", "학과", "교과목", "시수", "학점",
        "이수구분", "교과목표", "교육내용", "취득가능자격증", "주요진로", "실습", "팀프로젝트"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lclas", default="20", help="NCS 대분류 코드 (20 = 정보통신)")
    ap.add_argument("--pages", type=int, default=30, help="최대 페이지 수 (페이지당 100건)")
    ap.add_argument("--scan", action="store_true", help="대분류 01~24 코드별 건수만 확인 (저장 안 함)")
    a = ap.parse_args()
    if a.scan:
        for code in [f"{i:02d}" for i in range(1, 25)]:
            try:
                rows, info = ncs_api.fetch_courses(code, 1, 1)
                name = rows[0].get("대분류", "") if rows else ""
                print(f"  {code} {name:10s} {info.get('totCnt', '0')}건")
            except Exception as e:
                print(f"  {code} 오류: {str(e)[:60]}")
        return

    rows, info = ncs_api.fetch_courses(a.lclas, 100, 1)
    total_pages = int(info.get("totalPage") or 1)
    print(f"전체 {info.get('totCnt', '?')}건 / {total_pages}페이지 중 최대 {a.pages}페이지 받기")
    for p in range(2, min(total_pages, a.pages) + 1):
        rows += ncs_api.fetch_courses(a.lclas, 100, p)[0]
        print(f"  {p}페이지 완료 (누적 {len(rows)}건)")
    rows = [{k: r.get(k, "") for k in KEEP} for r in rows]
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    with_cert = sum(1 for r in rows if r["취득가능자격증"])
    print(f"✅ 저장: {OUT}  ({len(rows)}건, 취득가능자격증 표시된 교과목 {with_cert}건)")


if __name__ == "__main__":
    main()
