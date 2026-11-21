"""공공 채용정보 API 수집기 (템플릿)

※ 고용24 OpenAPI의 실제 요청 주소·파라미터·응답 필드는 발급받은 API 안내 문서를 기준으로
  BASE_URL / PARAMS / 필드 매핑을 수정하세요. 키가 없으면 실습용 샘플 데이터로 자동 전환합니다.
"""
import json
from . import config

BASE_URL = "https://<고용24 OpenAPI 안내 문서의 채용정보 목록 URL>"   # TODO: 발급 문서 기준으로 수정


def fetch(keyword: str = "클라우드", pages: int = 1, per_page: int = 100):
    if not config.GOYONG24_API_KEY or BASE_URL.startswith("https://<"):
        print("⚠ API 키 또는 URL이 설정되지 않아 샘플 데이터를 사용합니다.")
        return load_sample()
    import requests
    items = []
    for page in range(1, pages + 1):
        params = {"authKey": config.GOYONG24_API_KEY, "keyword": keyword, "startPage": page, "display": per_page, "returnType": "JSON"}
        r = requests.get(BASE_URL, params=params, timeout=20)
        r.raise_for_status()
        items += r.json().get("items", [])   # TODO: 실제 응답 구조에 맞게 수정
    return items


def load_sample():
    raw = json.load(open(config.DATA / "raw" / "job_postings_raw.json", encoding="utf-8"))
    return raw["response"]["items"]
