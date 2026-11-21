"""[Lambda] Step Functions 파이프라인 보조 작업   담당: 강민경

Step Functions 가 action 을 바꿔 가며 이 함수를 부릅니다.
  {"action": "targets"}              진행 중 공고 중 아직 Bedrock 추출이 없는 공고 ID 목록 (최대 MAX_PER_RUN 건)
  {"action": "queue_depth", "round": n}   추출 대기열(SQS)에 남은 메시지 수 → 다 끝났는지 확인
  {"action": "analyze"}              NCS 매칭(match_ncs.py) + 수요 분석(analyze_demand.py) → S3 processed/ 갱신

팀원 스크립트(pipeline/match_ncs.py, pipeline/analyze_demand.py)는 고치지 않고 그대로 부릅니다.
Lambda 코드 폴더는 읽기 전용이라 입력·출력은 /tmp 에서 처리합니다.
"""
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

BUCKET = os.environ.get("BUCKET", "")
QUEUE_URL = os.environ.get("QUEUE_URL", "")
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "20"))      # 하루 최대 추출 건수 (Bedrock 비용 보호)
POSTINGS_KEY = "processed/postings_jobalio.json"
MERGED_KEY = "processed/extractions.json"
PER_POSTING_PREFIX = "processed/extractions/"
TASK = Path(__file__).resolve().parent                      # /var/task
TMP = Path("/tmp/pipeline")
KST = timezone(timedelta(hours=9))

s3 = boto3.client("s3")


def log(**kw):
    print(json.dumps(kw, ensure_ascii=False, default=str))


def today():
    return datetime.now(KST).strftime("%Y-%m-%d")


def get_json(key, default=None):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return default


def _demand():
    """진행 중 판단은 성훈님 analyze_demand.is_ongoing 을 그대로 써서 수요 분석과 기준을 맞춤"""
    if str(TASK / "pipeline") not in sys.path:
        sys.path.insert(0, str(TASK / "pipeline"))
    import analyze_demand
    return analyze_demand


# ------------------------------------------------------------------ 1) 추출 대상
def targets():
    ad = _demand()
    as_of = ad.today_kst()
    postings = get_json(POSTINGS_KEY, [])
    done = {r["id"] for r in get_json(MERGED_KEY, []) if (r.get("meta") or {}).get("mode") == "bedrock"}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PER_POSTING_PREFIX):
        for o in page.get("Contents", []):            # Lambda 가 이미 뽑았지만 아직 합치기 전인 공고 (Bedrock 결과만)
            r = get_json(o["Key"], {}) or {}
            if (r.get("meta") or {}).get("mode") == "bedrock":
                done.add(o["Key"].rsplit("/", 1)[-1].removesuffix(".json"))
    ongoing = [p for p in postings if ad.is_ongoing(p, as_of)]
    todo = [p["id"] for p in sorted(ongoing, key=lambda p: p.get("posted_at") or "", reverse=True)
            if p["id"] not in done]
    picked = todo[:MAX_PER_RUN]
    out = {"as_of": str(as_of), "ongoing": len(ongoing), "already": len(ongoing) - len(todo),
           "todo": len(todo), "count": len(picked), "ids": picked}
    log(event="targets", **{k: v for k, v in out.items() if k != "ids"}, ids=picked)
    return out


# ------------------------------------------------------------------ 2) 대기열 확인
def queue_depth(event):
    a = boto3.client("sqs").get_queue_attributes(
        QueueUrl=QUEUE_URL,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    pending = int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])
    out = {"pending": pending, "round": int(event.get("round", 0)) + 1}
    log(event="queue_depth", **out)
    return out


# ------------------------------------------------------------------ 3) NCS 매칭 + 수요 분석
def _use_lambda_config():
    """labutil.config 는 불러올 때 outputs 폴더를 만드는데 /var/task 는 읽기 전용 → 같은 이름의 설정을 먼저 등록"""
    sys.path.insert(0, str(TASK / "src"))
    sys.path.insert(0, str(TASK / "pipeline"))
    import labutil
    cfg = types.ModuleType("labutil.config")
    cfg.ROOT, cfg.DATA, cfg.OUTPUTS = TASK, TASK / "data", TMP
    cfg.AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
    cfg.AWS_PROFILE = None
    cfg.MODEL_ID = cfg.FAST_MODEL_ID = ""
    cfg.EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
    cfg.BEDROCK_MOCK = True               # 매칭은 기존과 같이 로컬 유사도 계산 (Bedrock 비용 0)
    cfg.GOYONG24_API_KEY = cfg.DATA_GO_KR_API_KEY = ""
    sys.modules["labutil.config"] = cfg
    labutil.config = cfg


def _run_script(module_name, argv):
    """팀원 스크립트의 main() 을 명령행 인자만 바꿔서 실행"""
    import importlib
    mod = importlib.import_module(module_name)
    old = sys.argv
    sys.argv = [module_name + ".py", *argv]
    try:
        mod.main()
    except SystemExit as e:               # argparse 의 parser.error() 는 SystemExit 로 끝남
        if e.code not in (0, None):
            raise RuntimeError(f"{module_name} 실패 (종료 코드 {e.code}) — 로그 확인")
    finally:
        sys.argv = old


def analyze():
    t0 = time.time()
    inp, out = TMP / "in", TMP / "out"
    inp.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    for key in (POSTINGS_KEY, MERGED_KEY):
        s3.download_file(BUCKET, key, str(inp / key.rsplit("/", 1)[-1]))
    _use_lambda_config()
    postings, extractions = str(inp / "postings_jobalio.json"), str(inp / "extractions.json")
    _run_script("match_ncs", ["--postings", postings, "--extractions", extractions,
                              "--output", str(out / "skill_ncs_map.json"),
                              "--coverage-output", str(out / "ncs_match_coverage.json")])
    _run_script("analyze_demand", ["--postings", postings, "--extractions", extractions,
                                   "--output-dir", str(out), "--mode", "bedrock"])
    uploaded = []
    for f in sorted(out.iterdir()):
        ctype = "text/csv; charset=utf-8" if f.suffix == ".csv" else "application/json; charset=utf-8"
        s3.upload_file(str(f), BUCKET, "processed/" + f.name, ExtraArgs={"ContentType": ctype})
        uploaded.append(f.name)
    cov = json.loads((out / "ncs_match_coverage.json").read_text(encoding="utf-8"))
    res = {"as_of": today(), "uploaded": uploaded, "seconds": round(time.time() - t0, 1),
           "competency_counts": cov.get("competency_counts")}
    log(event="analyzed", **res)
    return res


def handler(event, context=None):
    if not BUCKET:
        raise RuntimeError("환경변수 BUCKET 이 비어 있습니다")
    action = (event or {}).get("action")
    if action == "targets":
        return targets()
    if action == "queue_depth":
        return queue_depth(event)
    if action == "analyze":
        return analyze()
    raise ValueError(f"알 수 없는 action: {action}")
