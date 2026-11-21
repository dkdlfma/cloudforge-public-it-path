"""팀 S3 버킷과 데이터 주고받기

S3 구조 (s3://aws-edu-<팀>-<계정번호>/):
    processed/postings_jobalio.json|csv   ← 공고. Lambda 가 매일 갱신 (로컬에서 올리지 않음!)
    processed/extractions.json            ← 역량 추출 (조호성)
    processed/skill_ncs_map*.json         ← NCS 매칭 (신성훈)
    processed/demand_*.csv                ← 수요 분석 (신성훈)
    raw/jobalio/pages/, raw/jobalio/daily/<날짜>/  ← API 원본
    code/                                 ← Lambda 코드 zip

실행 (먼저 aws sso login --profile kosa):
    python pipeline/upload_s3.py --download   # S3 → 내 PC (최신 공고 + 팀 결과물)
    python pipeline/upload_s3.py              # 내 PC → S3 (팀 결과물만. 공고 파일은 안 올림)
    python pipeline/upload_s3.py --list       # S3 에 뭐가 있는지 보기
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
RAW_PAGES = ROOT / "data" / "raw" / "jobalio_pages"
LAMBDA_OWNED = {"postings_jobalio.json", "postings_jobalio.csv"}   # Lambda 가 관리 → 로컬에서 올리면 최신본이 덮어써짐


def session():
    import boto3
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    region = os.getenv("AWS_REGION", "ap-northeast-2")
    team = os.getenv("TEAM", "").strip()
    if not re.fullmatch(r"[a-z0-9-]{3,20}", team):
        sys.exit("❌ .env 에 TEAM=팀이름 을 넣으세요 (예: team04-kang). 팀 전원이 같은 값을 씁니다.")
    try:
        s = boto3.Session(profile_name=os.getenv("AWS_PROFILE") or None, region_name=region)
        account = s.client("sts").get_caller_identity()["Account"]
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "token" in msg or "sso" in msg or "expired" in msg:
            sys.exit("❌ AWS 로그인이 필요합니다. 터미널에서: aws sso login --profile kosa")
        sys.exit(f"❌ AWS 접속 실패: {str(e)[:200]}")
    return s, region, f"aws-edu-{team}-{account}"


def ensure_bucket(s3, bucket, region):
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except s3.exceptions.ClientError:
        pass
    kw = {"Bucket": bucket}
    if region != "us-east-1":
        kw["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kw)
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    print(f"🪣 버킷 생성: {bucket}")


def upload(s, region, bucket, include_postings=False, include_raw=False, only=None):
    s3 = s.client("s3")
    ensure_bucket(s3, bucket, region)
    files = sorted(p for p in PROCESSED.glob("*") if p.is_file() and p.suffix in (".json", ".csv"))
    if only:
        files = [p for p in files if p.name in set(only)]
    n = 0
    for p in files:
        if p.name in LAMBDA_OWNED and not include_postings:
            print(f"  ⏭ {p.name}: Lambda 가 매일 갱신하는 파일이라 올리지 않습니다")
            continue
        s3.upload_file(str(p), bucket, "processed/" + p.name)
        print(f"  ⬆ {p.name} → s3://{bucket}/processed/{p.name}  ({p.stat().st_size / 1e3:,.0f} KB)")
        n += 1
    if include_raw and RAW_PAGES.exists():
        pages = sorted(RAW_PAGES.glob("*.json"))
        for p in pages:
            s3.upload_file(str(p), bucket, "raw/jobalio/pages/" + p.name)
        print(f"  ⬆ 원본 페이지 {len(pages)}개 → raw/jobalio/pages/")
    print(f"✅ 업로드 완료: {n}개 파일")


def download(s, bucket, force=False):
    s3 = s.client("s3")
    PROCESSED.mkdir(parents=True, exist_ok=True)
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="processed/"):
        for obj in page.get("Contents", []):
            rest = obj["Key"][len("processed/"):]
            if not rest or "/" in rest:      # processed/extractions/<공고ID>.json 같은 하위 폴더(Lambda 중간 결과)는 받지 않음
                continue
            name = rest
            local = PROCESSED / name
            if local.exists() and not force and name not in LAMBDA_OWNED:
                local_time = datetime.fromtimestamp(local.stat().st_mtime, timezone.utc)
                if local_time > obj["LastModified"]:          # 내가 방금 만든 결과를 옛날 S3 본으로 덮지 않기
                    print(f"  ⏭ {name}: 내 PC 파일이 더 최신이라 건너뜀 (덮어쓰려면 --force)")
                    continue
            s3.download_file(bucket, obj["Key"], str(local))
            print(f"  ⬇ {name}  ({obj['Size'] / 1e3:,.0f} KB, S3 갱신 {obj['LastModified']:%m-%d %H:%M} UTC)")
            n += 1
    print(f"✅ 내려받기 완료: {n}개 파일" if n else "⚠ 받은 파일이 없습니다.")


def list_files(s, bucket):
    s3 = s.client("s3")
    groups = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            k = o["Key"]
            g = "processed/" if k.startswith("processed/") else "/".join(k.split("/")[:3]) + "/"
            groups.setdefault(g, []).append(o)
    for g, objs in sorted(groups.items()):
        size = sum(o["Size"] for o in objs) / 1e6
        last = max(o["LastModified"] for o in objs)
        print(f"{g:28s} {len(objs):4d}개 {size:7.1f} MB   최근 {last:%m-%d %H:%M} UTC")
        if g == "processed/":
            for o in sorted(objs, key=lambda o: o["Key"]):
                print(f"   - {o['Key'][10:]:36s} {o['Size'] / 1e3:9,.0f} KB  {o['LastModified']:%m-%d %H:%M}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true", help="S3 processed/ → data/processed")
    ap.add_argument("--list", action="store_true", help="버킷 안 파일 목록")
    ap.add_argument("--force", action="store_true", help="--download 때 내 PC 파일이 더 최신이어도 덮어쓰기")
    ap.add_argument("--only", nargs="+", help="이 파일만 올리기 (예: --only extractions.json)")
    ap.add_argument("--include-postings", action="store_true", help="(주의) 공고 파일도 올리기 — Lambda 최신본을 덮어씀")
    ap.add_argument("--include-raw", action="store_true", help="원본 페이지도 올리기")
    a = ap.parse_args()
    s, region, bucket = session()
    print(f"버킷: s3://{bucket}  (리전 {region})")
    if a.download:
        download(s, bucket, a.force)
    elif a.list:
        list_files(s, bucket)
    else:
        upload(s, region, bucket, a.include_postings, a.include_raw, a.only)
