"""추천 에이전트 — Claude가 도구를 골라 쓰는 루프 (수업 08 노트북 my_agent 와 같은 구조)
도구는 Lambda 대신 이 PC의 파이썬 함수로 실행합니다 (통합 단계에서 팀장님 Lambda로 옮기기 쉬움).
모의 모드(BEDROCK_MOCK=1): 정해진 순서(요구사항 → 추천 → 저장)로 도구를 부르고 답을 만듭니다."""
import json
import re

from . import data_io, engine, guardrail  # data_io 가 src 경로를 등록
from labutil import config

SYSTEM = """당신은 '공공기관 IT 직무 취업 준비생'을 돕는 학습경로 상담 에이전트입니다.
- 반드시 도구 결과에 근거해 답하고, 공고에 없는 요건을 지어내지 마세요.
- 공고가 '인정 자격증 중 1개 이상'을 요구하면 여러 개를 다 따라고 하지 말고, 사용자에게 가장 효율적인 1개와 순서를 추천하세요.
- 공공기관은 블라인드 채용입니다. 출신 학교·성별·나이 등 개인 속성으로 판단하지 마세요.
- 합격이나 취업을 보장하거나 확률을 숫자로 약속하지 마세요.
- 응시자격·시험 일정은 바뀔 수 있으니 Q-Net 등 시행기관에서 확인하라고 안내하세요.
- 답변은 반드시 '입력하신 프로필 기준'으로 근거를 대세요: recommend_path 결과의 profile_check 에서
  요건마다 충족/미충족/확인 필요와 공고 근거 문장(따옴표)을 함께 말하세요. 예) "보유하신 정보처리기사로 '인정 자격증 중 1개' 요건은 충족"
- 추천 자격증은 cert_evidence 의 공고 수(필수·선택필수·우대)를 근거로 함께 말하세요.
- 한국어로, 판정 → 프로필 기준 요건 점검 → 추천 자격증과 이유 → 학습 순서(주 단위) → 주의사항 순서로 간결하게 답하세요."""

TOOLS = [
    {"toolSpec": {
        "name": "get_requirements",
        "description": "공고 ID로 그 공고가 요구하는 자격증·전공·경력을 필수/선택필수/우대로 나눠 근거 문장과 함께 돌려줍니다.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "posting_id": {"type": "string", "description": "공고 ID (예: JA-267524)"}},
            "required": ["posting_id"]}}}},
    {"toolSpec": {
        "name": "recommend_path",
        "description": "공고 ID와 사용자 프로필로 지원 가능 여부, 추천 자격증 1개와 대안, 주 단위 학습경로를 계산합니다.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "posting_id": {"type": "string"},
            "position": {"type": "string", "description": "모집 분야 (공고에 여러 분야가 있을 때)"},
            "major_it": {"type": "boolean", "description": "IT 관련 전공 여부"},
            "experience_years": {"type": "number", "description": "관련 실무 경력(년)"},
            "completed": {"type": "array", "items": {"type": "string"}, "description": "이수한 교육·훈련과정"},
            "certs": {"type": "array", "items": {"type": "string"}, "description": "이미 가진 자격증"},
            "interests": {"type": "array", "items": {"type": "string", "enum": ["개발", "DB", "보안", "데이터", "네트워크", "운영", "관리"]}},
            "weeks": {"type": "integer", "description": "준비 가능 기간(주)"}},
            "required": ["posting_id"]}}}},
    {"toolSpec": {
        "name": "save_plan",
        "description": "사용자가 원하면 추천된 학습경로를 저장합니다.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "posting_id": {"type": "string"}, "student": {"type": "string", "description": "저장용 별칭"}},
            "required": ["posting_id", "student"]}}}},
]


def call_tool(name, args, profile):
    """도구 실행 (로컬 함수). 프로필 값은 화면 입력을 기본값으로 씁니다."""
    if name == "get_requirements":
        return engine.get_requirements(args["posting_id"], args.get("position") or profile.get("position"))
    if name == "recommend_path":
        prof = {**profile, **{k: v for k, v in args.items() if k != "posting_id"}}
        rec = engine.recommend(args["posting_id"], prof)
        return _compact(rec)
    if name == "save_plan":
        rec = engine.recommend(args["posting_id"], profile)
        return engine.save_plan(rec, args.get("student"))
    return {"error": f"알 수 없는 도구: {name}"}


def _compact(rec):
    """모델에게 넘길 때 필요한 것만 (토큰 절약)"""
    if not rec.get("ok"):
        return rec
    pick = lambda c: c and {k: c.get(k) for k in ("canonical", "feasibility", "prep_weeks", "eligibility", "reasons")}
    return {"verdict": rec["verdict"], "satisfied_by": rec["satisfied_by"], "position": rec.get("position"),
            "profile_check": [{k: r[k] for k in ("요건", "구분", "판정", "내 프로필", "공고 근거")} for r in rec.get("profile_check", [])],
            "cert_evidence": [{"cert": e["cert"], "scope": e["scope"], "total": e["total"], "n": e["n"], "counts": e["counts"],
                               "postings": [{k: p[k] for k in ("company", "requirement", "evidence", "close_at")} for p in e["postings"][:3]]}
                              for e in rec.get("cert_evidence", [])],
            "picks": [{"one_of": pk["size"], "pick": pick(pk["pick"]), "alternatives": [pick(a) for a in pk["alternatives"]]}
                      for pk in rec.get("picks", [])],
            "must_missing": [m["canonical"] for m in rec.get("must_missing", [])],
            "path": [{k: s[k] for k in ("order", "name", "phase", "weeks")} for s in rec["path"]],
            "total_weeks": rec["total_weeks"],
            "courses": [{"cert": c["cert"], "subjects": [x["교과목"] for x in c["courses"]]} for c in rec.get("courses", [])], "next_step": rec["next_step"], "warnings": rec["warnings"],
            "stacks": {"held": (rec.get("stacks") or {}).get("held", []),
                       "to_learn": [{"name": s["name"], "requirement": s["requirement"], "weeks": s["weeks"]}
                                    for s in (rec.get("stacks") or {}).get("path", [])]}}


def _find_posting_id(text, default):
    m = re.search(r"JA-\d+", text)
    return m.group(0) if m else default


def run(question, posting_id, profile, history=None, max_turns=6):
    """에이전트 루프 → (최종 답변, 도구 호출 기록 [(도구, 입력, 결과)])"""
    pid = _find_posting_id(question, posting_id)
    if config.BEDROCK_MOCK:
        return _mock_run(question, pid, profile)
    from labutil.agent import converse_tools
    context = f"(현재 선택된 공고: {pid}, 사용자 프로필: {json.dumps(profile, ensure_ascii=False)})\n"
    messages = list(history or []) + [{"role": "user", "content": [{"text": context + question}]}]
    trace = []
    for _ in range(max_turns):
        resp = converse_tools(messages, tools=TOOLS, system=SYSTEM)
        msg = resp["output"]["message"]
        messages.append(msg)
        if resp["stopReason"] != "tool_use":
            return "".join(b.get("text", "") for b in msg["content"]), trace
        results = []
        for block in msg["content"]:
            if "toolUse" in block:
                tu = block["toolUse"]
                out = call_tool(tu["name"], tu["input"], profile)
                trace.append((tu["name"], tu["input"], out))
                results.append({"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"json": out}]}})
        messages.append({"role": "user", "content": results})
    return "(도구 호출 횟수 제한에 걸렸습니다)", trace


def _mock_run(question, pid, profile):
    """모의 모드: Claude 대신 정해진 순서로 도구를 부릅니다 (흐름 확인용, 비용 0)"""
    trace = []
    req = call_tool("get_requirements", {"posting_id": pid}, profile)
    trace.append(("get_requirements", {"posting_id": pid}, {"ok": req.get("ok"), "groups": {k: len(v) for k, v in req.get("groups", {}).items()}}))
    if not req.get("ok"):
        return req.get("error", "공고를 찾지 못했습니다."), trace
    rec_args = {"posting_id": pid}
    rec = call_tool("recommend_path", rec_args, profile)
    trace.append(("recommend_path", rec_args, rec))
    if re.search(r"저장|기록", question):
        args = {"posting_id": pid, "student": profile.get("student", "demo")}
        trace.append(("save_plan", args, call_tool("save_plan", args, profile)))
    answer = engine.summary_text(engine.recommend(pid, profile))
    answer += "\n\n※ 응시자격·시험 일정은 Q-Net 등 시행기관에서 꼭 확인하세요. (모의 모드 답변)"
    return answer, trace


def guarded_chat(question, posting_id, profile, history=None):
    """입구 검사 → 에이전트 → 출구 검사 → (답변, 기록 dict)"""
    log = {"input": guardrail.check(question, "INPUT")}
    if log["input"]["action"] == "GUARDRAIL_INTERVENED" and guardrail.blocked(log["input"]):
        log["stage"] = "입구 차단"
        return guardrail.BLOCKED_MSG, log
    q = log["input"]["text"]                      # 개인정보는 가려진 문장으로 진행
    answer, trace = run(q, posting_id, profile, history)
    log["trace"] = trace
    log["output"] = guardrail.check(answer, "OUTPUT")
    if log["output"]["action"] == "GUARDRAIL_INTERVENED" and guardrail.blocked(log["output"]):
        log["stage"] = "출구 차단"
        return guardrail.BLOCKED_MSG, log
    log["stage"] = "통과"
    return log["output"]["text"], log
