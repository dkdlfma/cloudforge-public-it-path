"""가드레일 — 입구·출구 검사 (수업 09 노트북 방식 재사용)
공공기관은 NCS 기반 '블라인드 채용'이라, 출신 학교·성별·나이 등으로 판단해 달라는 요청을 막는 것이 제도와 맞습니다.
모의 모드: 규칙 기반 검사 / 실제 모드: .env 의 GUARDRAIL_ID 가 있으면 Bedrock Guardrails 로 검사"""
import os
import re

from . import data_io  # noqa: F401  (src 경로 등록)
from labutil import config, guard

TEAM = os.getenv("TEAM", "team04")
GUARD_NAME = f"kosa-proj-{TEAM}-guard"

TOPICS = [
    {"name": "Blind hiring attributes", "type": "DENY",
     "definition": "출신 학교, 성별, 나이, 출신 지역, 외모, 가족관계 같은 개인 속성으로 합격 가능성이나 추천을 다르게 판단해 달라는 요청",
     "examples": ["지방대 출신이면 공기업 전산직에 불리한가요?", "여자는 보안 직무에 안 맞나요?"]},
    {"name": "Job guarantee", "type": "DENY",
     "definition": "특정 자격증이나 교육을 이수하면 취업·합격이 보장된다고 단정하거나 합격 확률을 수치로 약속해 달라는 요청",
     "examples": ["이 자격증 따면 합격 보장되나요?", "정보처리기사 있으면 합격 확률 몇 퍼센트예요?"]},
]
PII = ["EMAIL"]
BLOCKED_MSG = ("죄송합니다. 공공기관은 블라인드 채용 원칙에 따라 개인 속성으로 판단하지 않고, 합격을 보장해 드릴 수도 없어요. "
               "목표 공고나 관심 분야, 보유 자격증을 알려 주시면 준비할 자격증과 순서를 추천해 드릴게요.")

_ATTR = re.compile(r"출신|지방대|명문대|학벌|성별|여자|남자|여성|남성|나이|연령|\d+ ?살|고향|외모|결혼|기혼|미혼")
_JUDGE = re.compile(r"불리|유리|떨어|붙|뽑|차별|감점|가산점|안 ?맞|못 ?하|가능성")
_GUAR = re.compile(r"(합격|취업|입사).{0,10}(보장|확실|100 ?%|확률|퍼센트|무조건)|(보장|확실).{0,6}(합격|취업)")
_GUAR_OUT = re.compile(r"(합격|취업|입사)[이가은는을를]? ?(보장(됩니다|돼요|해요|합니다)|확실(합니다|해요))|합격 확률[은이]? ?\d")
_ATTACK = re.compile(r"(이전|위의?|앞의?) ?(지시|규칙|명령|프롬프트).{0,8}(무시|잊어)|ignore (all|previous)|system prompt", re.I)

_GR = {"id": None, "ver": None, "ready": False}


def _ensure():
    """모의: labutil 모의 가드레일에 주제 등록 / 실제: .env 의 GUARDRAIL_ID 사용 (자동 생성하지 않음)"""
    if _GR["ready"]:
        return
    if config.BEDROCK_MOCK:
        # 모의 가드레일은 개인정보 가리기·공격 탐지만 맡기고, 주제 판정은 아래 규칙(_local_rules)으로 합니다
        _GR["id"], _GR["ver"] = guard.ensure(GUARD_NAME, [], PII)
    else:
        _GR["id"] = os.getenv("GUARDRAIL_ID") or None
        _GR["ver"] = os.getenv("GUARDRAIL_VERSION", "DRAFT")
    _GR["ready"] = True


def _local_rules(text, source):
    policies = []
    if source == "INPUT" and _ATTACK.search(text):
        policies.append("유해:PROMPT_ATTACK")
    if _ATTR.search(text) and _JUDGE.search(text):
        policies.append("주제:Blind hiring attributes")
    if (source == "INPUT" and _GUAR.search(text)) or (source == "OUTPUT" and _GUAR_OUT.search(text)):
        policies.append("주제:Job guarantee")
    return policies


def check(text, source="INPUT"):
    """→ {"action": NONE|GUARDRAIL_INTERVENED, "policies": [...], "text": 처리된 문장, "engine": 검사 방식}"""
    _ensure()
    policies = _local_rules(text, source)
    out, engine = text, "규칙"
    if _GR["id"]:
        r = guard.check(_GR["id"], _GR["ver"], text, source)
        policies += [p for p in r["policies"] if p not in policies]
        out = r["text"]
        engine = "규칙 + " + ("모의 가드레일" if config.BEDROCK_MOCK else "Bedrock Guardrails")
    action = "GUARDRAIL_INTERVENED" if policies else "NONE"
    return {"action": action, "policies": policies, "text": out, "engine": engine}


def blocked(res):
    """개인정보 가리기만 일어난 경우는 차단이 아님"""
    return any(not p.startswith("개인정보") for p in res["policies"])
