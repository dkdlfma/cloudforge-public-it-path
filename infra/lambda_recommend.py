"""[Lambda] 추천 API — API Gateway(HTTP API) + Cognito 로그인 뒤에서 동작   담당: 강민경

  GET  /postings                진행 중 공고 목록 (?all=1 이면 마감 공고 포함)
  POST /recommend               {"posting_id": "JA-...", "profile": {...}} → 학습경로 (현진님 engine.recommend 그대로)

프로필 필드 (recommend/engine.py DEFAULT_PROFILE 과 같음):
  major_it(bool) · experience_years(숫자) · completed[이수 과정] · certs[보유 자격증] · stacks[기술 스택]
  interests[개발·DB·보안·데이터·네트워크·운영·관리] · weeks(준비 기간, 주) · position(모집 분야, 선택)

데이터는 S3 processed/ 의 최신 파일을 처음 실행 때 /tmp 로 받아 씁니다 (파이프라인이 매일 갱신).
"""
import json
import os
import sys
import time
import types
from pathlib import Path

import boto3

BUCKET = os.environ.get("BUCKET", "")
TASK = Path(__file__).resolve().parent                    # /var/task
TMP = Path("/tmp/recommend")
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "3600"))   # 1시간마다 S3 최신본 다시 받기
DATA_FILES = [
    "postings_jobalio.json", "extractions.json", "skill_ncs_map.json", "demand_overall.csv",
    "demand_by_region.csv", "stack_map.json", "training_courses.json", "ncs_courses.json",
]
PROFILE_KEYS = {"major_it", "experience_years", "completed", "certs", "stacks", "interests", "weeks",
                "position", "regions"}

s3 = boto3.client("s3")
_loaded_at = 0.0
_engine = None


def log(**kw):
    print(json.dumps(kw, ensure_ascii=False, default=str))


def _setup_modules():
    """recommend 패키지 불러오기 — labutil.config 는 /var/task 에 폴더를 만들려 해서 같은 이름의 설정을 먼저 등록"""
    for p in (TASK, TASK / "src"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    import labutil
    cfg = types.ModuleType("labutil.config")
    cfg.ROOT, cfg.DATA, cfg.OUTPUTS = TASK, TASK / "data", TMP / "outputs"
    cfg.AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
    cfg.AWS_PROFILE = None
    cfg.BEDROCK_MOCK = True
    cfg.MODEL_ID = cfg.FAST_MODEL_ID = cfg.EMBED_MODEL_ID = ""
    cfg.GOYONG24_API_KEY = cfg.DATA_GO_KR_API_KEY = ""
    sys.modules["labutil.config"] = cfg
    labutil.config = cfg
    from recommend import data_io, engine
    return data_io, engine


def _clear_caches(*mods):
    for m in mods:
        for v in vars(m).values():
            if hasattr(v, "cache_clear"):
                v.cache_clear()


def load():
    """처음 실행(콜드 스타트) 또는 1시간이 지나면 S3 processed/ 최신 파일을 /tmp 로 받음"""
    global _loaded_at, _engine
    if _engine and time.time() - _loaded_at < REFRESH_SECONDS:
        return _engine
    data_io, engine = _setup_modules()
    from recommend import catalog, courses, stacks
    folder = TMP / "processed"
    folder.mkdir(parents=True, exist_ok=True)
    got = []
    for name in DATA_FILES:
        try:
            s3.download_file(BUCKET, "processed/" + name, str(folder / name))
            got.append(name)
        except Exception as e:  # noqa: BLE001  (없는 파일은 엔진이 샘플·빈 값으로 처리)
            log(event="data_missing", file=name, error=str(e)[:120])
    data_io.PROCESSED, data_io.OUTPUTS = folder, TMP / "outputs"
    _clear_caches(data_io, engine, catalog, courses, stacks)
    _loaded_at, _engine = time.time(), (data_io, engine)
    log(event="data_loaded", files=got)
    return _engine


def resp(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(body, ensure_ascii=False, default=str)}


def user_of(event):
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    return claims.get("cognito:username") or claims.get("username") or claims.get("sub")


def handler(event, context=None):
    if not BUCKET:
        raise RuntimeError("환경변수 BUCKET 이 비어 있습니다")
    route = event.get("routeKey", "")
    user = user_of(event)
    try:
        data_io, engine = load()
        if route == "GET /postings":
            q = event.get("queryStringParameters") or {}
            items = data_io.list_postings()
            if q.get("all") != "1":
                items = [p for p in items if p["ongoing"]]
            return resp(200, {"count": len(items), "postings": items})
        if route == "POST /recommend":
            body = json.loads(event.get("body") or "{}")
            pid = str(body.get("posting_id") or "").strip()
            if not pid:
                return resp(400, {"error": "posting_id 가 필요합니다 (예: JA-307033)"})
            profile = {k: v for k, v in (body.get("profile") or {}).items() if k in PROFILE_KEYS}
            profile["student"] = user or "api"
            r = engine.recommend(pid, profile)
            log(event="recommend", user=user, posting_id=pid, ok=r.get("ok"), verdict=r.get("verdict"))
            return resp(200 if r.get("ok") else 404, r)
        return resp(404, {"error": f"없는 경로: {route}"})
    except json.JSONDecodeError:
        return resp(400, {"error": "본문이 JSON 형식이 아닙니다"})
    except Exception as e:  # noqa: BLE001
        log(event="error", route=route, error=repr(e)[:500])
        return resp(500, {"error": "서버 오류 — CloudWatch 로그를 확인하세요"})
