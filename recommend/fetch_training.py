"""고용24 국민내일배움카드 훈련과정 받아서 저장 (정보통신 NCS 20, 앞으로 N개월 안에 시작하는 과정)
사용: python -m recommend.fetch_training --months 3 --pages 5
결과: data/processed/training_courses.json  (.env 의 GOYONG24_API_KEY 필요)"""
import argparse
import json
from datetime import date, timedelta

from . import data_io  # noqa: F401  (src 경로 등록)
from labutil import config

URL = "https://www.work24.go.kr/cm/openApi/call/hr/callOpenApiSvcInfo310L01.do"
OUT = data_io.PROCESSED / "training_courses.json"
KEEP = {"TITLE": "과정명", "SUB_TITLE": "훈련기관", "ADDRESS": "주소", "CERTIFICATE": "관련자격증", "NCS_CD": "NCS코드",
        "TRA_START_DATE": "시작일", "TRA_END_DATE": "종료일", "COURSE_MAN": "수강비", "REAL_MAN": "실제훈련비",
        "TRAIN_TARGET": "훈련유형", "EI_EMPL_RATE3": "취업률3개월", "STDG_SCOR": "만족도", "TITLE_LINK": "링크",
        "TRPR_ID": "과정ID", "TRPR_DEGR": "회차"}


def fetch(page, size, st, ed, ncs1, gbn=None):
    import requests
    params = {"authKey": config.GOYONG24_API_KEY, "returnType": "JSON", "outType": "1", "pageNum": page,
              "pageSize": size, "srchTraStDt": st, "srchTraEndDt": ed, "sort": "ASC", "sortCol": "2",
              "srchNcs1": ncs1}
    if gbn:
        params["srchTraGbn"] = gbn
    r = requests.get(URL, params=params, timeout=30)
    r.raise_for_status()
    try:
        d = r.json()
    except ValueError:
        raise RuntimeError("JSON 응답이 아닙니다 (인증키 확인): " + r.text[:200])
    if isinstance(d, dict) and "returnJSON" in d:          # 일부 응답은 문자열로 한 번 더 감싸져 옴
        d = json.loads(d["returnJSON"]) if isinstance(d["returnJSON"], str) else d["returnJSON"]
    total = int(d.get("scn_cnt") or 0)
    rows = d.get("srchList") or []
    if isinstance(rows, dict):
        rows = rows.get("scn_list", [])
    return total, rows


def main():
    if not config.GOYONG24_API_KEY:
        raise SystemExit(".env 에 GOYONG24_API_KEY 가 없습니다. 고용24 OPEN-API 인증키를 넣어 주세요.")
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--pages", type=int, default=5, help="최대 페이지 (페이지당 100건)")
    ap.add_argument("--ncs1", default="20", help="NCS 1차 분류 (20 = 정보통신)")
    ap.add_argument("--gbn", default=None, help="훈련구분: M1001 일반(집체)과정, M1005 인터넷과정 (비우면 전체)")
    a = ap.parse_args()
    st = date.today()
    ed = st + timedelta(days=30 * a.months)
    total, rows = fetch(1, 100, st.strftime("%Y%m%d"), ed.strftime("%Y%m%d"), a.ncs1, a.gbn)
    print(f"{st}~{ed} 시작 과정 전체 {total}건, 최대 {a.pages}페이지 받기")
    for p in range(2, min((total + 99) // 100, a.pages) + 1):
        rows += fetch(p, 100, st.strftime("%Y%m%d"), ed.strftime("%Y%m%d"), a.ncs1, a.gbn)[1]
        if p % 10 == 0:
            print(f"  {p}페이지 완료 (누적 {len(rows)}건)")
    norm = lambda k: str(k).replace("_", "").lower()          # TRA_START_DATE / traStartDate 둘 다 대응
    keep = {norm(k): v for k, v in KEEP.items()}
    out = []
    for r in rows:
        item = {v: "" for v in KEEP.values()}
        for k, val in r.items():
            if norm(k) in keep and val is not None:
                item[keep[norm(k)]] = val
        out.append(item)
    if rows and not any(x["과정명"] for x in out):
        print("⚠ 필드 이름이 예상과 달라요. 첫 항목의 키:", list(rows[0].keys()))
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    days = sorted(x["시작일"] for x in out if x["시작일"])
    if days:
        print(f"  받은 과정의 시작일 범위: {days[0]} ~ {days[-1]}")
    print(f"✅ 저장: {OUT} ({len(out)}건, 관련자격증 표시 {sum(1 for x in out if x['관련자격증'])}건)")


if __name__ == "__main__":
    main()
