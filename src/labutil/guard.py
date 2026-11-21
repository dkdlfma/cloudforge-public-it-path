"""Amazon Bedrock Guardrails 도우미 (6주차) — 입구·출구 검사
Standard 등급(한국어 지원)은 교차 리전 가드레일 프로필(apac.guardrail.v1:0)을 함께 씁니다."""
import re
import time

from . import config

PROFILE = "apac.guardrail.v1:0"
BLOCKED_MSG = "죄송합니다. 이 요청은 교육 상담 정책에 맞지 않아 답할 수 없습니다. 전공·관심 분야·수준을 알려 주시면 과정을 추천해 드릴게요."


def build_config(topics, pii_types, standard=True):
    """create/update_guardrail 에 넣을 설정 dict"""
    tier = {"tierName": "STANDARD" if standard else "CLASSIC"}
    cfg = {
        "topicPolicyConfig": {"topicsConfig": topics, "tierConfig": tier},
        "contentPolicyConfig": {"tierConfig": tier, "filtersConfig": [
            {"type": t, "inputStrength": "HIGH", "outputStrength": "HIGH"} for t in ("HATE", "INSULTS", "MISCONDUCT")]
            + [{"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"}]},
        "sensitiveInformationPolicyConfig": {
            # 입력·출력 모두 명시적으로 켜고 가리기(ANONYMIZE)
            "piiEntitiesConfig": [{"type": t, "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE",
                                   "inputEnabled": True, "outputEnabled": True} for t in pii_types],
            "regexesConfig": [{"name": "kr-mobile", "description": "휴대전화 번호",
                               "pattern": r"01[016789][-. ]?\d{3,4}[-. ]?\d{4}", "action": "ANONYMIZE",
                               "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE", "inputEnabled": True, "outputEnabled": True}]},
        "blockedInputMessaging": BLOCKED_MSG,
        "blockedOutputsMessaging": BLOCKED_MSG,
    }
    if standard:
        cfg["crossRegionConfig"] = {"guardrailProfileIdentifier": PROFILE}
    return cfg


def ensure(name, topics, pii_types):
    """같은 이름의 가드레일이 있으면 설정을 갱신하고, 없으면 만듭니다. → (가드레일 ID, 'DRAFT')"""
    if config.BEDROCK_MOCK:
        _MOCK["topics"], _MOCK["pii"] = topics, pii_types
        print("모의 모드: 규칙 기반 가짜 가드레일을 씁니다.")
        return "MOCKGR", "DRAFT"
    for t in topics:                               # AWS 규칙을 미리 확인해 알아보기 쉬운 메시지로
        if not re.fullmatch(r"[0-9a-zA-Z\-_ !?.]{1,100}", t.get("name", "")):
            raise ValueError(f"주제 이름 '{t.get('name')}' — 이름은 영문·숫자·공백·-_!?. 만 됩니다 (예: 'Job guarantee'). 정의·예시는 한국어 OK")
        if len(t.get("definition", "")) > 200 or any(len(x) > 100 for x in t.get("examples", [])):
            raise ValueError(f"주제 '{t['name']}' — 정의는 200자, 예시는 각 100자 이내로 줄이세요")
    from .bedrock import client
    br = client("bedrock")
    found = [g for g in br.list_guardrails()["guardrails"] if g["name"] == name]
    for standard in (True, False):
        cfg = build_config(topics, pii_types, standard)
        try:
            if found:
                gid = found[0]["id"]
                br.update_guardrail(guardrailIdentifier=gid, name=name, **cfg)
            else:
                gid = br.create_guardrail(name=name, **cfg)["guardrailId"]
            break
        except Exception as e:  # noqa: BLE001
            if not standard:
                raise
            print("⚠ Standard 등급을 만들 수 없어 Classic 등급으로 다시 시도합니다 (한국어 주제 차단 정확도가 낮음):", str(e)[:150])
    for _ in range(30):
        st = br.get_guardrail(guardrailIdentifier=gid)["status"]
        if st == "READY":
            break
        time.sleep(2)
    print(f"✅ 가드레일 준비: {name} ({gid}, 상태 {st}, {'Standard' if standard else 'Classic'} 등급)")
    return gid, "DRAFT"


def check(gid, version, text, source="INPUT"):
    """ApplyGuardrail → {"action": NONE|GUARDRAIL_INTERVENED, "policies": [...], "text": 처리된 문장}"""
    if config.BEDROCK_MOCK:
        return _mock_check(text, source)
    from .bedrock import client
    r = client().apply_guardrail(guardrailIdentifier=gid, guardrailVersion=version, source=source,
                                 content=[{"text": {"text": text}}])
    policies = []
    for a in r.get("assessments", []):
        for t in a.get("topicPolicy", {}).get("topics", []):
            policies.append(f"주제:{t['name']}")
        for f in a.get("contentPolicy", {}).get("filters", []):
            if f.get("action") == "BLOCKED":
                policies.append(f"유해:{f['type']}")
        for p in a.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
            policies.append(f"개인정보:{p['type']}")
        for p in a.get("sensitiveInformationPolicy", {}).get("regexes", []):
            policies.append(f"개인정보:{p['name']}")
    out_text = r["outputs"][0]["text"] if r.get("outputs") else text
    return {"action": r["action"], "policies": policies, "text": out_text}


def guarded_agent(question, function_name, gid, version, **kw):
    """입구 검사 → 에이전트 → 출구 검사 → (최종 답변, 기록 dict)"""
    from .agent import run_agent
    log = {"input": check(gid, version, question, "INPUT")}
    if log["input"]["action"] == "GUARDRAIL_INTERVENED" and _blocked(log["input"]):
        log["stage"] = "입구 차단"
        return BLOCKED_MSG, log
    q = log["input"]["text"]                       # 개인정보는 가려진(ANONYMIZE) 문장으로 진행
    answer, trace = run_agent(q, function_name, **kw)
    log["trace"] = trace
    log["output"] = check(gid, version, answer, "OUTPUT")
    if log["output"]["action"] == "GUARDRAIL_INTERVENED" and _blocked(log["output"]):
        log["stage"] = "출구 차단"
        return BLOCKED_MSG, log
    log["stage"] = "통과"
    return log["output"]["text"], log


def _blocked(res):
    """개인정보 가리기(ANONYMIZE)만 일어난 경우는 차단이 아님"""
    return any(not p.startswith("개인정보") for p in res["policies"])


# ---------------------------------------------------------------- 모의 가드레일 (규칙 기반 흉내)
_MOCK = {"topics": [], "pii": []}
_ATTACK = re.compile(r"(이전|위의?) ?(지시|규칙|명령).{0,6}(무시|잊어)|ignore (all|previous)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"01[016789][-. ]?\d{3,4}[-. ]?\d{4}")


def _mock_check(text, source):
    policies, out = [], text
    if source == "INPUT" and _ATTACK.search(text):
        policies.append("유해:PROMPT_ATTACK")
    for t in _MOCK["topics"]:
        ex = " ".join(t.get("examples", [])) + t.get("definition", "")
        keys = [k for k in ("여자", "남자", "여성", "남성", "나이", "지방대", "출신", "합격", "보장", "확률") if k in ex]
        if any(k in text for k in keys) and any(k in text for k in ("안 맞", "불리", "유리", "보장", "확률", "합격", "못 ", "빼")):
            policies.append(f"주제:{t['name']}")
    if "EMAIL" in _MOCK["pii"] and _EMAIL.search(out):
        out, _ = _EMAIL.subn("{EMAIL}", out)
        policies.append("개인정보:EMAIL")
    if _PHONE.search(out):
        out, _ = _PHONE.subn("{kr-mobile}", out)
        policies.append("개인정보:kr-mobile")
    action = "GUARDRAIL_INTERVENED" if policies else "NONE"
    if action == "GUARDRAIL_INTERVENED" and _blocked({"policies": policies}):
        out = BLOCKED_MSG
    return {"action": action, "policies": policies, "text": out}


def delete(name):
    if config.BEDROCK_MOCK:
        return
    from .bedrock import client
    br = client("bedrock")
    for g in br.list_guardrails()["guardrails"]:
        if g["name"] == name:
            br.delete_guardrail(guardrailIdentifier=g["id"])
            print("✅ 가드레일 삭제:", name)
