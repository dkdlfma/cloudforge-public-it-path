"""[AgentCore Runtime] 추천 상담 에이전트 서버   담당: 강민경(배포) · 김현진(에이전트)

AgentCore Runtime 규약(HTTP): 0.0.0.0:8080 에서
  GET  /ping         → {"status": "Healthy"}
  POST /invocations  → 요청 {"prompt": "...", "posting_id": "JA-...", "profile": {...}}
                       응답 {"response": 답변, "status": "success", "stage": 검사 결과, "tools": [쓴 도구]}
현진님 recommend/agent.py 의 guarded_chat(입구 가드레일 → Claude 도구 호출 → 출구 가드레일)을 그대로 부릅니다.
표준 라이브러리 서버만 써서 추가 패키지가 필요 없습니다. 데이터는 S3 processed/ 최신본을 /tmp 로 받아 씁니다.
"""
import json
import os
import sys
import threading
import time
import traceback
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TMP = Path("/tmp/agent")
BUCKET = os.environ.get("BUCKET", "")
DATA_FILES = [
    "postings_jobalio.json", "extractions.json", "skill_ncs_map.json", "demand_overall.csv",
    "demand_by_region.csv", "stack_map.json", "training_courses.json", "ncs_courses.json",
]
_lock = threading.Lock()
_state = {"agent": None, "loaded_at": 0.0, "error": None}


def log(**kw):
    print(json.dumps(kw, ensure_ascii=False, default=str), flush=True)


def _setup_config():
    """labutil.config 는 코드 폴더에 outputs 를 만들려 함 → 같은 이름의 설정을 먼저 등록 (/tmp 사용)"""
    for p in (ROOT, ROOT / "src"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    import labutil
    cfg = types.ModuleType("labutil.config")
    cfg.ROOT, cfg.DATA, cfg.OUTPUTS = ROOT, ROOT / "data", TMP / "outputs"
    cfg.OUTPUTS.mkdir(parents=True, exist_ok=True)
    cfg.AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
    cfg.AWS_PROFILE = None                       # 실행 역할(IAM Role) 권한 사용
    cfg.MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    cfg.FAST_MODEL_ID = cfg.MODEL_ID
    cfg.EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
    cfg.BEDROCK_MOCK = os.environ.get("BEDROCK_MOCK", "0") == "1"
    cfg.GOYONG24_API_KEY = cfg.DATA_GO_KR_API_KEY = ""
    sys.modules["labutil.config"] = cfg
    labutil.config = cfg


def load(refresh_seconds=3600):
    """처음 요청 때(또는 1시간마다) S3 processed/ 를 /tmp 로 받고 에이전트 모듈을 준비"""
    with _lock:
        if _state["agent"] and time.time() - _state["loaded_at"] < refresh_seconds:
            return _state["agent"]
        _setup_config()
        from recommend import agent, catalog, courses, data_io, engine, stacks
        folder = TMP / "processed"
        folder.mkdir(parents=True, exist_ok=True)
        got = []
        if BUCKET:
            import boto3
            s3 = boto3.client("s3")
            for name in DATA_FILES:
                try:
                    s3.download_file(BUCKET, "processed/" + name, str(folder / name))
                    got.append(name)
                except Exception as e:  # noqa: BLE001
                    log(event="data_missing", file=name, error=str(e)[:120])
            data_io.PROCESSED = folder
        data_io.OUTPUTS = TMP / "outputs"
        for m in (data_io, engine, catalog, courses, stacks):
            for v in vars(m).values():
                if hasattr(v, "cache_clear"):
                    v.cache_clear()
        _state.update(agent=agent, loaded_at=time.time(), error=None)
        log(event="data_loaded", files=got, mock=sys.modules["labutil.config"].BEDROCK_MOCK)
        return agent


def invoke(payload):
    question = str(payload.get("prompt") or "").strip()
    if not question:
        return 400, {"status": "error", "response": "prompt 가 필요합니다."}
    posting_id = payload.get("posting_id") or "JA-306979"
    profile = payload.get("profile") or {}
    agent = load()
    t0 = time.time()
    answer, trace = agent.guarded_chat(question, posting_id, profile)
    tools = [t[0] for t in trace.get("trace", [])]
    log(event="invocation", posting_id=posting_id, stage=trace.get("stage"), tools=tools,
        seconds=round(time.time() - t0, 1))
    return 200, {"response": answer, "status": "success", "stage": trace.get("stage"), "tools": tools,
                 "posting_id": posting_id}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/ping"):
            return self._send(200, {"status": "Healthy"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/invocations"):
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            code, body = invoke(payload if isinstance(payload, dict) else {})
        except json.JSONDecodeError:
            code, body = 400, {"status": "error", "response": "본문이 JSON 형식이 아닙니다."}
        except Exception as e:  # noqa: BLE001
            log(event="error", error=repr(e)[:500], trace=traceback.format_exc()[-1500:])
            code, body = 500, {"status": "error", "response": "에이전트 오류 — CloudWatch 로그를 확인하세요."}
        self._send(code, body)

    def log_message(self, fmt, *args):      # 기본 접속 로그는 끔 (/ping 이 자주 옴)
        pass


def _safe_preload():
    try:
        load()
    except Exception as e:  # noqa: BLE001
        log(event="preload_failed", error=repr(e)[:300])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=lambda: _safe_preload(), daemon=True).start()
    log(event="server_start", port=port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
