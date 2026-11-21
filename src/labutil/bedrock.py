"""Amazon Bedrock 호출 래퍼 — Converse API 사용 (모델이 바뀌어도 코드는 그대로)"""
import json
import re
import time
from functools import lru_cache
from . import config


@lru_cache
def client(service="bedrock-runtime"):
    import boto3
    session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.AWS_REGION)
    return session.client(service)


# 자주 만나는 오류 → 한국어 안내 (메시지 일부, 안내)
_HINTS = [
    ("explicit deny in a service control policy",
     "팀 계정에서 허용되지 않은 리전·기능입니다. 오류 문장 속 리전이 ap-northeast-2(서울)인지 확인하고, .env의 AWS_REGION=ap-northeast-2 → 커널 재시작."),
    ("token has expired",
     "SSO 로그인이 만료되었습니다. 터미널에서 aws sso login --profile kosa 실행 → 브라우저에서 허용 → 커널 재시작."),
    ("sso session", "SSO 로그인이 필요합니다. 터미널에서 aws sso login --profile kosa 실행 → 커널 재시작."),
    ("use case details have not been submitted",
     "이 계정에서 Anthropic 모델 '사용 사례 양식'이 아직 제출되지 않았습니다. "
     "콘솔 → Amazon Bedrock → 모델 카탈로그 → Claude 모델 → 'Submit use case details' 제출 후 15분 뒤 다시 실행하세요. "
     "(팀 계정이라면 강사에게 알려 주세요)"),
    ("is not available for this account",
     "이 계정에서는 이 모델이 열려 있지 않습니다. 00 노트북의 '모델 접근 점검'에서 ✅로 나온 모델로 .env의 MODEL_ID를 바꾸세요."),
    ("end of its life", "지원이 종료된 모델입니다. 00 노트북 목록의 최신 모델로 바꾸세요."),
    ("legacy", "Legacy(지원 종료 예정) 모델입니다. 00 노트북 목록의 최신 모델로 바꾸세요."),
    ("on-demand throughput", "모델 ID 대신 'global.' 으로 시작하는 추론 프로필 ID를 쓰세요."),
    ("model identifier is invalid", "모델 ID 오타이거나 이 리전에 없는 모델입니다. .env의 MODEL_ID와 AWS_REGION을 확인하세요."),
    ("not authorized to perform", "IAM 사용자에게 Bedrock 호출 권한이 없습니다. 강사에게 알려 주세요."),
    ("security token", "Access Key가 틀렸거나 삭제되었습니다. aws configure --profile kosa 를 다시 실행하세요."),
]


def explain(err) -> str:
    """오류 객체/메시지를 받아 한국어 해결 안내를 돌려줍니다 (모르는 오류면 빈 문자열)."""
    msg = str(err).lower()
    for key, hint in _HINTS:
        if key in msg:
            return hint
    return ""


def converse(user: str, system: str = None, model_id: str = None, max_tokens: int = 1024, temperature: float = 0.0, retries: int = 3):
    """텍스트를 보내고 (응답 텍스트, 사용 토큰 dict)를 돌려줍니다."""
    if config.BEDROCK_MOCK:
        return _mock(user), {"inputTokens": 0, "outputTokens": 0}
    from botocore.exceptions import ClientError
    kwargs = {
        "modelId": model_id or config.MODEL_ID,
        "messages": [{"role": "user", "content": [{"text": user}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    for i in range(retries):
        try:
            r = client().converse(**kwargs)
            return r["output"]["message"]["content"][0]["text"], r.get("usage", {})
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ThrottlingException":
                time.sleep(2 ** i)   # 동시 호출이 많으면 잠시 쉬었다가 재시도
                continue
            hint = explain(e)
            if hint:
                print("💡 해결 방법:", hint)
            raise
    raise RuntimeError("Bedrock 호출이 계속 제한(Throttling)되고 있습니다. 1~2분 쉬었다가 다시 시도하세요.")


def check_models(models=None):
    """모델마다 짧게 한 번 호출해 보고 ✅/❌와 해결 안내를 출력합니다. (목록에 ACTIVE로 보여도 계정마다 호출 가능 여부가 다릅니다)"""
    models = models or [config.MODEL_ID, config.FAST_MODEL_ID]
    ok = []
    for m in models:
        if config.BEDROCK_MOCK:
            print("⏭  (모의 모드)", m)
            continue
        try:
            client().converse(modelId=m, messages=[{"role": "user", "content": [{"text": "hi"}]}],
                              inferenceConfig={"maxTokens": 5})
            print("✅", m)
            ok.append(m)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
            print(f"❌ {m}\n   {code}: {explain(e) or str(e)[:150]}")
    return ok


# ---------------------------------------------------------------- 임베딩 (5주차)
import hashlib
import math

_EMB_CACHE = None


def _cache_path():
    return config.OUTPUTS / "embedding_cache.json"


def _cache():
    global _EMB_CACHE
    if _EMB_CACHE is None:
        try:
            _EMB_CACHE = json.load(open(_cache_path(), encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _EMB_CACHE = {}
    return _EMB_CACHE


def save_cache():
    """지금까지 만든 임베딩을 파일로 저장 (다시 실행할 때 Bedrock을 또 부르지 않게)"""
    json.dump(_cache(), open(_cache_path(), "w", encoding="utf-8"))


def _mock_embed(text: str, dims: int):
    """모의 임베딩: 글자 2·3-gram을 해시해서 만든 벡터 (같은 글자가 많이 겹치면 가까움)"""
    v = [0.0] * dims
    t = " " + text.lower() + " "
    for n in (2, 3):
        for i in range(len(t) - n + 1):
            h = int(hashlib.md5(t[i:i + n].encode("utf-8")).hexdigest(), 16)
            v[h % dims] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def embed(text: str, dims: int = 512, model_id: str = None):
    """문장 → 숫자 벡터(길이 dims). 같은 문장은 캐시에서 꺼내 씁니다. (Titan Text Embeddings V2)"""
    model_id = model_id or config.EMBED_MODEL_ID
    key = hashlib.sha1(f"{model_id}|{dims}|{config.BEDROCK_MOCK}|{text}".encode("utf-8")).hexdigest()
    c = _cache()
    if key in c:
        return c[key]
    if config.BEDROCK_MOCK:
        vec = _mock_embed(text, dims)
    else:
        from botocore.exceptions import ClientError
        body = json.dumps({"inputText": text, "dimensions": dims, "normalize": True})
        for i in range(3):
            try:
                r = client().invoke_model(modelId=model_id, body=body, contentType="application/json", accept="application/json")
                vec = json.loads(r["body"].read())["embedding"]
                break
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ThrottlingException":
                    time.sleep(2 ** i)
                    continue
                hint = explain(e)
                if hint:
                    print("💡 해결 방법:", hint)
                raise
        else:
            raise RuntimeError("임베딩 호출이 계속 제한(Throttling)되고 있습니다. 1~2분 쉬었다가 다시 시도하세요.")
    c[key] = vec
    return vec


def embed_raw(text: str, dims: int = 512, normalize: bool = True, model_id: str = None):
    """임베딩 API를 캐시 없이 그대로 호출해 요청·응답을 보여 줍니다 (과정 들여다보기용).
    반환: {"request": 보낸 본문, "inputTextTokenCount": 토큰 수, "embedding": 벡터}"""
    model_id = model_id or config.EMBED_MODEL_ID
    req = {"inputText": text, "dimensions": dims, "normalize": normalize}
    if config.BEDROCK_MOCK:
        vec = _mock_embed(text, dims)
        return {"request": req, "inputTextTokenCount": max(1, len(text) // 2), "embedding": vec, "mock": True}
    r = client().invoke_model(modelId=model_id, body=json.dumps(req), contentType="application/json", accept="application/json")
    out = json.loads(r["body"].read())
    return {"request": req, "inputTextTokenCount": out.get("inputTextTokenCount"), "embedding": out["embedding"]}


def bedrock_usage(model_id: str = None, hours: int = 3):
    """CloudWatch에 쌓인 Bedrock 호출 지표 (최근 hours시간, 이 계정·리전 전체) → {지표: 합계}"""
    import datetime as dt
    model_id = model_id or config.EMBED_MODEL_ID
    if config.BEDROCK_MOCK:
        return {"(모의 모드)": 0}
    cw = client("cloudwatch")
    end = dt.datetime.now(dt.timezone.utc)
    out = {}
    for metric in ("Invocations", "InputTokenCount", "InvocationLatency"):
        r = cw.get_metric_statistics(Namespace="AWS/Bedrock", MetricName=metric,
                                     Dimensions=[{"Name": "ModelId", "Value": model_id}],
                                     StartTime=end - dt.timedelta(hours=hours), EndTime=end, Period=3600,
                                     Statistics=["Average" if metric == "InvocationLatency" else "Sum"])
        pts = r.get("Datapoints", [])
        if metric == "InvocationLatency":
            out["평균 지연(ms)"] = round(sum(p["Average"] for p in pts) / len(pts)) if pts else 0
        else:
            out[metric] = int(sum(p["Sum"] for p in pts))
    return out


def embed_many(texts, dims: int = 512, show=True):
    """여러 문장을 차례로 임베딩 (진행 상황 표시, 끝나면 캐시 저장)"""
    out = []
    for i, t in enumerate(texts, 1):
        out.append(embed(t, dims))
        if show and (i % 10 == 0 or i == len(texts)):
            print(f"  임베딩 {i}/{len(texts)}")
    save_cache()
    return out


def parse_json(text: str):
    """모델 응답에서 JSON 부분만 꺼내기 (```json ... ``` 감싸기 대응)"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("응답에서 JSON을 찾지 못했습니다:\n" + text[:300])
    return json.loads(m.group(0))


def _mock(user: str) -> str:
    """모의 응답: <posting> 태그 안의 공고에서 규칙 기반으로 기술명을 찾아 JSON으로 반환"""
    from .skills import rule_extract
    m = re.search(r"<posting>(.*?)</posting>", user, re.S)
    if not m:
        return "(모의 응답) Bedrock 호출이 정상이라면 여기에 모델의 답변이 나옵니다."
    found = rule_extract(m.group(1))
    return json.dumps({"skills": [{"name": s, "section": "(모의)"} for s in found]}, ensure_ascii=False)
