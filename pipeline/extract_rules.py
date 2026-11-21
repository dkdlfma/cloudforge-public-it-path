"""규칙 기반 역량 추출기 (담당: 조호성)

용도 2가지:
  1) 모의 모드(BEDROCK_MOCK=1)일 때 Bedrock 대신 이 함수가 결과를 만듭니다 → 비용 0원으로 흐름 개발
  2) 나중에 Bedrock(LLM) 결과와 비교하는 '베이스라인' (LLM이 규칙보다 얼마나 나은지 보여주기)

출력 형식은 extract_competencies.py 의 LLM 결과와 똑같습니다.
"""
import re

# ------------------------------------------------------------------ 사전 (필요하면 계속 추가)
# 영문/특수 표기 자격증 (한글 '~기사/기술사' 형태는 정규식으로 따로 찾음)
CERTS_EXTRA = [
    "CISA", "CISSP", "CISM", "PMP", "SQLD", "SQLP", "ADsP", "ADP", "CCNA", "CCNP", "CCIE",
    "OCP", "OCJP", "ITIL", "CPPG", "ISMS-P", "리눅스마스터", "네트워크관리사", "정보보호전문가",
    "컴퓨터활용능력", "워드프로세서", "정보기술자격", "AWS 자격", "정보시스템감리사",
]
MAJORS = [
    "컴퓨터공학", "컴퓨터과학", "전산학", "전자계산학", "소프트웨어", "정보통신공학", "정보통신",
    "전자공학", "전기공학", "정보보호", "정보보안", "산업공학", "통계학", "데이터사이언스",
    "경영정보", "인공지능", "정보공학", "멀티미디어", "수학",
]
SKILLS = [
    "Java", "Python", "JavaScript", "SQL", "Oracle", "MySQL", "PostgreSQL", "Linux", "Unix",
    "Spring", "전자정부프레임워크", "AWS", "Azure", "클라우드", "빅데이터", "인공지능", "머신러닝",
    "딥러닝", "데이터베이스", "네트워크", "보안관제", "침해대응", "모의해킹", "GIS", "ERP",
    "가상화", "서버", "웹 개발", "앱 개발", "데이터 분석",
]
DEGREES = ["박사", "석사", "학사"]

# 이 단어가 들어간 줄은 직무 역량이 아님 (법정가점·결격사유·연령 등)
NOISE = re.compile(
    r"가점|유공자|장애인|지역인재|보훈|결격|병역|정년|연령|만\s*\d+세|취업지원|취업보호|"
    r"저소득|북한이탈|다문화|의사상자|국외여행|비위면직"
)
CHOICE = re.compile(r"(중|하나|요건).{0,6}(한\s*가지|1개|하나|일)\s*(이상|에\s*해당)")
CERT_RE = re.compile(r"([가-힣]{2,12})\s?(기술사|산업기사|기사|기능사)(?![가-힣])")
BAD_CERT_PREFIX = {"신문", "관련", "해당", "등의", "이상", "의료"}  # '관련 기사' 같은 오탐 방지
YEARS_RE = re.compile(r"(\d{1,2})\s*년\s*이상")


def _sections(text):
    """'[자격요건]' 같은 머리말로 본문을 나눔 → {구역: [줄...]}"""
    out, cur = {}, "기타"
    for line in text.splitlines():
        m = re.match(r"\s*\[(.+?)\]\s*$", line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
        elif line.strip():
            out.setdefault(cur, []).append(line.strip())
    return out


def _requirement(section, line, choice_mode):
    if section in ("우대사항", "우대조건") or "우대" in line:
        return "우대"
    return "선택필수" if choice_mode else "필수"


def _find_in(line, words):
    hits = []
    for w in words:
        if re.search(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", line, re.I):
            hits.append(w)
    return hits


def rule_extract(posting: dict) -> dict:
    """공고 1건(dict) → 역량 추출 결과(dict). LLM 결과와 같은 형식."""
    comps, seen = [], set()

    def add(name, ctype, req, evidence):
        key = (name, ctype)
        if key in seen:
            return
        seen.add(key)
        comps.append({"name": name, "type": ctype, "requirement": req,
                      "position": "", "evidence": evidence.strip()[:60]})

    years = []
    for sec, lines in _sections(posting.get("text", "")).items():
        if sec == "공고제목":
            continue
        choice_mode = False
        for line in lines:
            if CHOICE.search(line):
                choice_mode = True           # "다음 중 1개 이상" 이후 줄은 선택필수
            if NOISE.search(line):
                continue
            req = _requirement(sec, line, choice_mode)
            for m in CERT_RE.finditer(line):
                pre = m.group(1)
                if pre not in BAD_CERT_PREFIX and not re.search(r"(분야|관련|산업|또는|및|이상)$", pre):
                    add(m.group(1) + m.group(2), "자격증", req, line)
            for w in _find_in(line, CERTS_EXTRA):
                add(w, "자격증", req, line)
            for w in _find_in(line, MAJORS):
                if "전공" in line or "학과" in line or "공학" in line or "계열" in line:
                    add(w, "전공", req, line)
            for w in _find_in(line, SKILLS):
                add(w, "기술", req, line)
            for w in DEGREES:
                if w + "학위" in line or w + " 학위" in line or w + "이상" in line:
                    add(w, "학력", req, line)
            if "경력" in line:
                for y in YEARS_RE.findall(line):
                    years.append(int(y))
                    add(f"관련 경력 {y}년 이상", "경력", req, line)

    career = posting.get("career", "")
    return {
        "job_areas": [],                                   # 규칙으로는 판단 어려움 → LLM이 채움
        "it_relevant": None,
        "min_experience_years": min(years) if years else (0 if career == "신입" else None),
        "entry_level_ok": None if not career else ("신입" in career),
        "competencies": comps,
    }


if __name__ == "__main__":   # 간단 확인: python pipeline/extract_rules.py
    demo = {"career": "신입+경력", "text": "[공고제목]\n테스트\n\n[자격요건]\n다음 중 1개 이상 해당하는 자\n"
            "- 정보처리기사 자격 소지자\n- 컴퓨터공학 전공 학사학위 소지자\n\n[우대사항]\n- 정보보안기사, SQLD 보유자\n- 국가유공자 가점"}
    import json
    print(json.dumps(rule_extract(demo), ensure_ascii=False, indent=1))
