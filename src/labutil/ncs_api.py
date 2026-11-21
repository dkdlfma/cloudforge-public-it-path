"""공공데이터포털 — 한국산업인력공단_NCS 교육과정 API (5~6주차)

- 엔드포인트: https://apis.data.go.kr/B490007/ncsEduCource/openapi20  (GET, 개발계정 자동승인)
- 요청: serviceKey, numOfRows, pageNo, returnType(xml), ncsLclasCd(대분류코드, 예: 20 = 정보통신)
- 응답: <root><data><row>…</row></data><dataInfo><code>000</code>…</dataInfo></root>
- .env 의 DATA_GO_KR_API_KEY 에 '일반 인증키'를 넣으세요.
"""
import xml.etree.ElementTree as ET

from . import config

URL = "https://apis.data.go.kr/B490007/ncsEduCource/openapi20"

# 응답 필드 → 우리가 쓸 이름 (전체 필드는 활용가이드 참고)
FIELDS = {
    "ncsLclasCdnm": "대분류", "ncsMclasCdnm": "중분류", "ncsSclasCdnm": "소분류", "ncsSubdCdnm": "세분류",
    "ncsSubdCd": "세분류코드", "depttName": "학과", "schosCdnm": "학제", "scholDstinCdnm": "학교구분",
    "asubjName": "교과목", "lssntimTime": "시수", "point": "학점", "complDstinCdnm": "이수구분",
    "subjGoal": "교과목표", "eduText": "교육내용", "getPosblCrtfc": "취득가능자격증", "mainCourse": "주요진로",
    "prctYn": "실습", "teampjtYn": "팀프로젝트",
}

ERRORS = {
    "30": "등록되지 않은 인증키 — 일반 인증키를 그대로 넣었는지 확인 (승인 직후라면 1~2시간 뒤 다시)",
    "31": "기한이 지난 인증키",
    "22": "호출 한도 초과 (개발계정 하루 10,000건)",
    "20": "서비스 접근 거부 — 활용신청 상태 확인",
    "10": "요청 파라미터 오류",
    "002": "조회 결과 없음 — 대분류코드 확인",
    "009": "필수 파라미터 누락",
}


def _text(el, tag):
    v = el.findtext(tag)
    return v.strip() if v else ""


def parse(xml_text: str):
    """XML 응답 → (행 목록, 정보 dict). 오류 응답이면 RuntimeError."""
    root = ET.fromstring(xml_text.strip().encode("utf-8"))
    # 포털 공통 오류 형식 (인증키 오류 등)
    hdr = root.find(".//cmmMsgHeader")
    if hdr is not None:
        code = _text(hdr, "returnReasonCode")
        raise RuntimeError(f"[{code}] {_text(hdr, 'returnAuthMsg')} → {ERRORS.get(code, '활용가이드 오류코드 확인')}")
    info_el = root.find(".//dataInfo")
    info = {c.tag: (c.text or "").strip() for c in info_el} if info_el is not None else {}
    code = info.get("code", "000")
    if code not in ("000", "00", ""):
        raise RuntimeError(f"[{code}] {info.get('message', '')} → {ERRORS.get(code, '활용가이드 오류코드 확인')}")
    rows = []
    for r in root.iter("row"):
        rec = {c.tag.strip(): (c.text or "").strip() for c in r}
        rows.append({FIELDS.get(k, k): v for k, v in rec.items()})
    return rows, info


def fetch_courses(lclas_cd: str = "20", num_rows: int = 100, page: int = 1, timeout: int = 20):
    """NCS 대분류 코드로 교육과정(교과목) 목록 조회. 기본 20 = 정보통신."""
    import requests
    if not config.DATA_GO_KR_API_KEY:
        raise RuntimeError(".env 에 DATA_GO_KR_API_KEY 가 없습니다. 공공데이터포털 마이페이지의 일반 인증키를 넣고 커널을 재시작하세요.")
    params = {"serviceKey": config.DATA_GO_KR_API_KEY, "numOfRows": num_rows, "pageNo": page,
              "returnType": "xml", "ncsLclasCd": lclas_cd}
    r = requests.get(URL, params=params, timeout=timeout)
    r.raise_for_status()
    return parse(r.text)


def fetch_all(lclas_cd: str = "20", num_rows: int = 100, max_pages: int = 50):
    """여러 페이지를 모아서 반환 (개발계정 하루 10,000건 한도 주의)."""
    rows, info = fetch_courses(lclas_cd, num_rows, 1)
    total_pages = int(info.get("totalPage") or 1)
    for p in range(2, min(total_pages, max_pages) + 1):
        rows += fetch_courses(lclas_cd, num_rows, p)[0]
    return rows
