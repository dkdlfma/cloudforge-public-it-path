"""[Lambda] 잡알리오 정보통신 공고 매일 자동 수집 → S3 processed/ 갱신

흐름:
  1) SSM Parameter Store 에서 인증키(암호화)를 꺼냄
  2) S3 processed/postings_jobalio.json (기존 공고) 을 읽음
  3) API 를 최신순으로 1페이지씩 받아 새 공고를 추가하고, 이미 있는 공고는 최신 상태로 갱신
     → 한 페이지에 새 공고가 하나도 없으면 멈춤 (보통 하루 1~2페이지)
  4) 받은 원본은 raw/jobalio/daily/<날짜>/ 에, 합친 결과는 processed/ 에 저장
외부 라이브러리 없이 표준 라이브러리 + boto3(Lambda 기본 포함)만 씁니다.
"""
import csv
import io
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

BUCKET = os.environ["BUCKET"]
API_URL = os.environ["API_URL"]
PARAM_NAME = os.environ["PARAM_NAME"]
NCS_CD = os.environ.get("NCS_CD", "R600020")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
ROWS = 100
KEY_JSON = "processed/postings_jobalio.json"
KEY_CSV = "processed/postings_jobalio.csv"
KST = timezone(timedelta(hours=9))

s3 = boto3.client("s3")
ssm = boto3.client("ssm")


# ------------------------------------------------------------------ 표준 형식 (pipeline/collect_jobalio.py 와 동일)
def _split(v):
    return [s.strip() for s in str(v or "").split(",") if s.strip()]


def _clean(v):
    s = str(v or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return "" if s in ("None", "null") else s


def _date(ymd):
    s = str(ymd or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else None


def to_text(item):
    parts = [f"[공고제목]\n{_clean(item.get('recrutPbancTtl'))}"]
    for title, key in (("자격요건", "aplyQlfcCn"), ("우대사항", "prefCn"), ("우대조건", "prefCondCn")):
        body = _clean(item.get(key))
        if body:
            parts.append(f"[{title}]\n{body}")
    return "\n\n".join(parts)


def normalize(item):
    ncs = _split(item.get("ncsCdNmLst"))
    return {
        "id": f"JA-{item.get('recrutPblntSn')}", "source": "jobalio",
        "company": _clean(item.get("instNm")), "title": _clean(item.get("recrutPbancTtl")),
        "ncs": ncs, "it_only": ncs == ["정보통신"],
        "region": _clean(item.get("workRgnNmLst")), "hire_type": _clean(item.get("hireTypeNmLst")),
        "career": _clean(item.get("recrutSeNm")), "education": _clean(item.get("acbgCondNmLst")),
        "headcount": _clean(item.get("recrutNope")),
        "posted_at": _date(item.get("pbancBgngYmd")), "close_at": _date(item.get("pbancEndYmd")),
        "ongoing": item.get("ongoingYn") == "Y", "url": _clean(item.get("srcUrl")),
        "text": to_text(item),
    }


# ------------------------------------------------------------------ API · S3
def fetch_page(key, page):
    qs = urllib.parse.urlencode({"serviceKey": key, "pageNo": page, "numOfRows": ROWS,
                                 "resultType": "json", "ncsCdLst": NCS_CD})
    for i in range(3):
        try:
            with urllib.request.urlopen(f"{API_URL}?{qs}", timeout=30) as r:
                body = r.read().decode("utf-8")
            if body.lstrip().startswith("<"):
                raise RuntimeError("API 오류 응답: " + body[:300])
            data = json.loads(body)
            if data.get("resultCode") != 200:
                raise RuntimeError(f"API 오류: {data.get('resultCode')} {data.get('resultMsg')}")
            return data
        except Exception as e:  # noqa: BLE001
            print(f"page {page} 재시도 {i + 1}/3: {str(e)[:200]}")
            if i == 2:
                raise
            time.sleep(2 ** i)


def load_existing():
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=KEY_JSON)
        return {r["id"]: r for r in json.loads(obj["Body"].read())}
    except s3.exceptions.NoSuchKey:
        return {}


def save(rows):
    rows.sort(key=lambda r: r["posted_at"] or "", reverse=True)
    s3.put_object(Bucket=BUCKET, Key=KEY_JSON, ContentType="application/json",
                  Body=json.dumps(rows, ensure_ascii=False, indent=1).encode("utf-8"))
    cols = [c for c in rows[0] if c != "text"] if rows else []
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({**r, "ncs": ", ".join(r["ncs"])})
    s3.put_object(Bucket=BUCKET, Key=KEY_CSV, ContentType="text/csv",
                  Body=buf.getvalue().encode("utf-8-sig"))


# ------------------------------------------------------------------ 진입점
def handler(event, context):
    key = ssm.get_parameter(Name=PARAM_NAME, WithDecryption=True)["Parameter"]["Value"]
    rows = load_existing()
    before = len(rows)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    new_total, updated, pages = 0, 0, 0

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(key, page)
        items = data.get("result", [])
        s3.put_object(Bucket=BUCKET, Key=f"raw/jobalio/daily/{today}/page_{page:03d}.json",
                      Body=json.dumps(data, ensure_ascii=False).encode("utf-8"))
        pages += 1
        new_here = 0
        for it in items:
            r = normalize(it)
            if r["id"] in rows:
                updated += 1          # 진행 여부 등 최신 상태로 갱신
            else:
                new_here += 1
            rows[r["id"]] = r
        new_total += new_here
        if new_here == 0 or len(items) < ROWS:
            break
        time.sleep(0.3)

    save(list(rows.values()))
    result = {"date": today, "pages": pages, "before": before, "after": len(rows),
              "new": new_total, "updated": updated}
    print(json.dumps(result, ensure_ascii=False))
    return result
