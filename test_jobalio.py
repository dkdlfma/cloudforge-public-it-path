"""잡알리오 채용정보 API 첫 호출 테스트 — 응답 구조 확인용"""
import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()
URL = os.getenv("JOBALIO_URL")
KEY = os.getenv("DATA_GO_KR_API_KEY")

params = {
    "serviceKey": KEY,
    "pageNo": 1,
    "numOfRows": 100,          # 100건
    "resultType": "json",
    "ncsCdLst": "R600020",     # 정보통신 (코드 정의서로 확인)
}

r = requests.get(URL, params=params, timeout=20)
print("상태 코드:", r.status_code)
try:
    data = r.json()
    print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
except ValueError:
    print(r.text[:3000])   # XML이면 그대로 출력

    # ---- 응답을 파일로 저장 + 필드 구조 확인 ----
from pathlib import Path

Path("outputs").mkdir(exist_ok=True)
with open("outputs/jobalio_it.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("\n✅ outputs/jobalio_sample.json 저장 완료")

first = data["result"][0]
print("\n[필드 목록]")
for k, v in first.items():
    print(f"- {k}: {str(v)[:60]}")