"""역량 추출 Lambda 함수 (담당: 조호성)  — 배포: python pipeline/extract_lambda_deploy.py

입력 3가지
  1) SQS 메시지 (주 경로)    : 메시지 1개 = 공고 1건.  본문 {"id": "JA-307006"}  (선택: "force": true)
  2) 직접 호출 (테스트/Step Functions Task) : {"id": "JA-307006"}
  3) 합치기                  : {"action": "merge"}  → 공고별 결과를 processed/extractions.json 한 파일로

출력 (S3)
  processed/extractions/<공고ID>.json  ← 공고 1건 결과 (여러 Lambda가 동시에 써도 안 겹침)
  processed/extractions.json           ← merge 때 갱신 (팀원들이 쓰는 기존 파일과 같은 형식)

규칙
  - 이미 같은 프롬프트 버전으로 Bedrock 추출한 공고는 건너뜀 (돈 아끼기). 다시 하려면 "force": true
  - 모의 모드(BEDROCK_MOCK=1) 결과는 Bedrock 결과를 덮어쓰지 않음
  - 합칠 때 공고끼리 표기만 다른 역량명(EXCEL/Excel, 띄어쓰기 차이 등)을 하나로 통일
  - SQS 처리 중 실패한 메시지만 다시 시도되고, 3번 실패하면 실패 대기열(DLQ)로 감
"""
import json
import os
import sys
import time
import types

import boto3

# ── 공용 설정(labutil.config) 대신 환경변수로 설정을 넣어 줌 ─────────────────────────────
# config.py 는 불러올 때 outputs 폴더를 만드는데, Lambda 코드 폴더는 읽기 전용이라 실패함.
# 공용 파일을 고치지 않으려고 같은 이름의 설정을 여기서 먼저 등록함.
_cfg = types.ModuleType("labutil.config")
_cfg.AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
_cfg.AWS_PROFILE = None                                   # Lambda 는 실행 역할(IAM Role) 권한 사용
_cfg.MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
_cfg.FAST_MODEL_ID = _cfg.MODEL_ID
_cfg.BEDROCK_MOCK = os.environ.get("BEDROCK_MOCK", "0") == "1"
import labutil  # noqa: E402

sys.modules["labutil.config"] = _cfg
labutil.config = _cfg

from extract_competencies import PROMPT_VERSION, extract_one, unify_names   # noqa: E402

BUCKET = os.environ.get("BUCKET", "")
POSTINGS_KEY = os.environ.get("POSTINGS_KEY", "processed/postings_jobalio.json")
OUT_PREFIX = "processed/extractions/"
MERGED_KEY = "processed/extractions.json"

s3 = boto3.client("s3")
_postings = None   # 같은 Lambda 가 다시 쓰일 때(웜 스타트) 공고 파일을 다시 받지 않도록 기억


def log(**kw):
    print(json.dumps(kw, ensure_ascii=False))           # CloudWatch 로그에 한 줄 JSON


def get_json(key):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def put_json(key, obj):
    s3.put_object(Bucket=BUCKET, Key=key, ContentType="application/json; charset=utf-8",
                  Body=json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"))


def postings():
    global _postings
    if _postings is None:
        t0 = time.time()
        _postings = {p["id"]: p for p in get_json(POSTINGS_KEY) or []}
        log(event="postings_loaded", count=len(_postings), seconds=round(time.time() - t0, 2))
    return _postings


def process(msg: dict) -> dict:
    """공고 1건 추출 → S3 저장."""
    pid = (msg.get("id") or msg.get("posting_id") or "").strip()
    if not pid:
        raise ValueError(f"메시지에 공고 id 가 없습니다: {str(msg)[:200]}")
    posting = msg if msg.get("text") else postings().get(pid)
    if posting is None:
        raise ValueError(f"공고 {pid} 를 {POSTINGS_KEY} 에서 찾지 못했습니다")

    key = f"{OUT_PREFIX}{pid}.json"
    mode = "mock" if _cfg.BEDROCK_MOCK else "bedrock"
    if not msg.get("force"):
        prev = get_json(key)
        meta = (prev or {}).get("meta", {})
        if meta.get("mode") == "bedrock" and (mode == "mock" or meta.get("prompt_version") == PROMPT_VERSION):
            log(event="skip", id=pid, reason="이미 추출됨", prompt_version=meta.get("prompt_version"))
            return {"id": pid, "status": "skipped"}

    r = extract_one(posting, _cfg.MODEL_ID, _cfg.BEDROCK_MOCK)
    r["meta"]["runner"] = "lambda"
    put_json(key, r)
    log(event="extracted", id=pid, mode=mode, competencies=len(r["competencies"]),
        input_tokens=r["meta"]["input_tokens"], output_tokens=r["meta"]["output_tokens"], seconds=r["meta"]["seconds"])
    return {"id": pid, "status": "ok", "competencies": len(r["competencies"]),
            "input_tokens": r["meta"]["input_tokens"], "output_tokens": r["meta"]["output_tokens"]}


def merge() -> dict:
    """공고별 결과(processed/extractions/*.json)를 합본(processed/extractions.json)에 반영."""
    merged = {r["id"]: r for r in (get_json(MERGED_KEY) or [])}
    before, added, updated = len(merged), 0, 0
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=OUT_PREFIX)
    for page in pages:
        for obj in page.get("Contents", []):
            r = get_json(obj["Key"])
            if not r or "id" not in r:
                continue
            old = merged.get(r["id"])
            if old and old.get("meta", {}).get("mode") == "bedrock" and r.get("meta", {}).get("mode") != "bedrock":
                continue                                   # 모의 결과로 Bedrock 결과를 덮지 않음
            if old is None:
                added += 1
            elif old != r:
                updated += 1
            merged[r["id"]] = r
    rows = sorted(merged.values(), key=lambda r: r.get("posted_at") or "", reverse=True)
    renamed = unify_names(rows)                        # 공고끼리 표기만 다른 역량명 통일 (EXCEL/Excel 등)
    put_json(MERGED_KEY, rows)
    out = {"action": "merge", "before": before, "after": len(rows), "added": added, "updated": updated,
           "renamed": len(renamed)}
    log(event="merged", **out)
    return out


def handler(event, context=None):
    if not BUCKET:
        raise RuntimeError("환경변수 BUCKET 이 비어 있습니다")
    # 1) SQS: 실패한 메시지만 다시 시도되게 batchItemFailures 로 알려 줌
    if isinstance(event, dict) and "Records" in event:
        failures, results = [], []
        for rec in event["Records"]:
            try:
                results.append(process(json.loads(rec["body"])))
            except Exception as e:  # noqa: BLE001
                log(event="error", message_id=rec.get("messageId"), error=str(e)[:500])
                failures.append({"itemIdentifier": rec["messageId"]})
        return {"batchItemFailures": failures, "results": results}
    # 2) 합치기
    if event.get("action") == "merge":
        return merge()
    # 3) 직접 호출 (공고 1건)
    return process(event)
