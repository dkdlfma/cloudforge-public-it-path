"""Bedrock AgentCore Runtime 진입점 — 추천 상담 에이전트를 AWS에 올릴 때 쓰는 파일 (담당: 김현진)
요청(payload) 예: {"prompt": "이 공고 뭐부터 준비해요?", "posting_id": "JA-267524",
                   "profile": {"experience_years": 0, "completed": [], "certs": [], "interests": ["보안"]}}
응답: {"answer": "...", "stage": "통과|입구 차단|출구 차단", "tools": [...]}

배포 (프로젝트 폴더 C:\\aws_edu 에서, 서울 리전):
  pip install bedrock-agentcore bedrock-agentcore-starter-toolkit
  agentcore configure -e recommend/agentcore_app.py -r ap-northeast-2 --requirements-file recommend/requirements-agentcore.txt
  agentcore launch --env BEDROCK_MOCK=0 --env AWS_REGION=ap-northeast-2
  agentcore invoke '{"prompt":"이 공고 뭐부터 준비해요?","posting_id":"JA-267524"}'
※ AWS 리소스 이름에 team04-kang 을 붙이고, 실행 역할은 Bedrock 호출·S3 읽기만 허용 (팀장님과 확인)"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from recommend import agent  # noqa: E402

try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
    app = BedrockAgentCoreApp()
    entrypoint = app.entrypoint
except ImportError:            # 로컬에서 SDK 없이 테스트할 때
    app = None
    entrypoint = lambda f: f   # noqa: E731


@entrypoint
def invoke(payload):
    question = (payload or {}).get("prompt", "")
    posting_id = (payload or {}).get("posting_id", "JA-267524")
    profile = (payload or {}).get("profile") or {}
    answer, log = agent.guarded_chat(question, posting_id, profile)
    return {"answer": answer, "stage": log.get("stage"),
            "tools": [t[0] for t in log.get("trace", [])]}


if __name__ == "__main__":
    if app:
        app.run()
    else:
        import json
        print(json.dumps(invoke({"prompt": "이 공고 준비하려면 뭐부터 해야 해요?", "posting_id": "JA-267524"}),
                         ensure_ascii=False, indent=1))
