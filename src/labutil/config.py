"""실습 설정 — 값은 프로젝트 루트의 .env 파일에서 읽습니다 (.env.example 참고)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUTPUTS = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
AWS_PROFILE = os.getenv("AWS_PROFILE") or None
# 수업 당일 강사가 안내하는 모델 ID로 바꾸세요. (00_환경점검 노트북에서 사용 가능한 ID를 확인)
MODEL_ID = os.getenv("MODEL_ID", "global.anthropic.claude-sonnet-4-6")
FAST_MODEL_ID = os.getenv("FAST_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
EMBED_MODEL_ID = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
# 1 이면 Bedrock을 실제로 호출하지 않고 모의 응답을 돌려줍니다 (AWS 계정이 없을 때 흐름 연습용)
BEDROCK_MOCK = os.getenv("BEDROCK_MOCK", "0") == "1"
GOYONG24_API_KEY = os.getenv("GOYONG24_API_KEY", "")
DATA_GO_KR_API_KEY = os.getenv("DATA_GO_KR_API_KEY", "")


def show():
    print(f"리전          : {AWS_REGION}")
    print(f"프로파일      : {AWS_PROFILE or '(기본)'}")
    print(f"모델          : {MODEL_ID}")
    print(f"빠른 모델     : {FAST_MODEL_ID}")
    print(f"임베딩 모델   : {EMBED_MODEL_ID}")
    print(f"모의 모드     : {'켜짐 (Bedrock 호출 안 함)' if BEDROCK_MOCK else '꺼짐'}")
    print(f"고용24 API 키 : {'설정됨' if GOYONG24_API_KEY else '없음 → 샘플 데이터 사용'}")
    print(f"공공데이터 키 : {'설정됨' if DATA_GO_KR_API_KEY else '없음 (5주차 전까지 신청)'}")
