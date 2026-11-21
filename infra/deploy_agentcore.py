"""추천 상담 에이전트 → Amazon Bedrock AgentCore Runtime 배포 · 호출 · 삭제   담당: 강민경 (에이전트: 김현진)

AgentCore 의 'direct code deploy'(zip 배포) 방식을 boto3 로 직접 부릅니다. Docker·uv·agentcore CLI 가 필요 없습니다.

먼저: aws sso login --profile kosa   (S3 processed/ 에 분석 결과가 있어야 함)

    python infra/deploy_agentcore.py                  # 배포 (실행 역할 → 코드 zip → 런타임 생성/갱신 → READY 대기, 3~6분)
    python infra/deploy_agentcore.py --invoke "이 공고 준비하려면 뭐부터 해야 해요?"
    python infra/deploy_agentcore.py --invoke "지방대 출신이면 불리한가요?"      # 가드레일 차단 확인
    python infra/deploy_agentcore.py --invoke "..." --posting JA-306979 --certs 정보처리기사
    python infra/deploy_agentcore.py --status
    python infra/deploy_agentcore.py --delete         # 런타임 + 실행 역할 삭제

만드는 것: IAM 실행 역할(aws-edu-<팀>-agentcore-role), 런타임 aws_edu_<팀>_advisor (서울 리전, arm64, Python 3.12)
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
PY_VERSION = "3.12"
RUNTIME = "PYTHON_3_12"
MODEL_ID_DEFAULT = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def setup():
    import boto3
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    region = os.getenv("AWS_REGION", "ap-northeast-2")
    team = os.getenv("TEAM", "").strip()
    if not re.fullmatch(r"[a-z0-9-]{3,20}", team):
        sys.exit("❌ .env 에 TEAM=팀이름 이 필요합니다 (예: team04-kang)")
    try:
        s = boto3.Session(profile_name=os.getenv("AWS_PROFILE") or None, region_name=region)
        account = s.client("sts").get_caller_identity()["Account"]
    except Exception as e:  # noqa: BLE001
        sys.exit("❌ AWS 로그인이 필요합니다: aws sso login --profile kosa\n   " + str(e)[:150])
    name = "aws_edu_" + team.replace("-", "_") + "_advisor"        # 런타임 이름은 영문·숫자·밑줄만
    return s, team, account, f"aws-edu-{team}-{account}", name


# ------------------------------------------------------------------ 코드 zip 만들기
def code_files():
    files = {"main.py": INFRA / "agentcore_main.py"}
    for p in sorted((ROOT / "recommend").glob("*")):
        if p.suffix in (".py", ".json") and not p.name.startswith(("fetch_", "demo", "agentcore_app")):
            files[f"recommend/{p.name}"] = p
    for p in sorted((ROOT / "src" / "labutil").glob("*.py")):
        files[f"src/labutil/{p.name}"] = p
    files["data/dict/skill_aliases.json"] = ROOT / "data" / "dict" / "skill_aliases.json"
    return files


def _add(z, arc, data, mode):
    info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
    info.external_attr = mode << 16                    # AgentCore 는 리눅스 권한 사용: 파일 644 / 폴더 755
    info.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(info, data)


def build_zip():
    """boto3 를 arm64 리눅스용으로 받아 + 우리 코드와 함께 zip (서버는 표준 라이브러리만 사용)"""
    tmp = Path(tempfile.mkdtemp(prefix="agentcore_"))
    deps = tmp / "deps"
    print("📥 boto3 (arm64 리눅스용) 내려받는 중…")
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--target", str(deps),
           "--platform", "manylinux2014_aarch64", "--python-version", PY_VERSION, "--implementation", "cp",
           "--only-binary=:all:", "boto3>=1.40"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit("❌ pip 내려받기 실패:\n" + (r.stderr or r.stdout)[-800:])
    buf = io.BytesIO()
    dirs = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(deps.rglob("*")):
            rel = p.relative_to(deps).as_posix()
            if p.is_dir() or "__pycache__" in rel or rel.endswith(".pyc") or rel.startswith("bin/"):
                continue
            parts = rel.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]) + "/")
            _add(z, rel, p.read_bytes(), 0o644)
        for arc, p in code_files().items():
            if not p.exists():
                sys.exit(f"❌ 코드 파일이 없습니다: {p}")
            parts = arc.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]) + "/")
            _add(z, arc, p.read_bytes(), 0o755 if arc == "main.py" else 0o644)
        for d in sorted(dirs):
            _add(z, d, b"", 0o40755)
    shutil.rmtree(tmp, ignore_errors=True)
    return buf.getvalue()


def upload_code(s, bucket, data):
    key = f"code/agentcore/advisor-{hashlib.sha256(data).hexdigest()[:12]}.zip"
    s.client("s3").put_object(Bucket=bucket, Key=key, Body=data)
    print(f"📦 코드 → s3://{bucket}/{key}  ({len(data) / 1e6:.1f} MB)")
    return key


# ------------------------------------------------------------------ 실행 역할 (CloudFormation)
def deploy_role(s, team, bucket, name):
    cfn = s.client("cloudformation")
    stack = f"aws-edu-agentcore-{team}"
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in
              {"Team": team, "BucketName": bucket, "AgentName": name}.items()]
    body = (INFRA / "agentcore-role.yaml").read_text(encoding="utf-8")
    try:
        st = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError:
        st = None
    if st == "ROLLBACK_COMPLETE":
        cfn.delete_stack(StackName=stack)
        cfn.get_waiter("stack_delete_complete").wait(StackName=stack)
        st = None
    try:
        if st is None:
            cfn.create_stack(StackName=stack, TemplateBody=body, Parameters=params, Capabilities=["CAPABILITY_NAMED_IAM"])
            cfn.get_waiter("stack_create_complete").wait(StackName=stack, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
            print("🔐 실행 역할 생성 완료")
            time.sleep(10)                              # IAM 역할이 퍼지는 시간
        else:
            cfn.update_stack(StackName=stack, TemplateBody=body, Parameters=params, Capabilities=["CAPABILITY_NAMED_IAM"])
            cfn.get_waiter("stack_update_complete").wait(StackName=stack, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
            print("🔐 실행 역할 갱신 완료")
    except cfn.exceptions.ClientError as e:
        if "No updates are to be performed" not in str(e):
            raise
    except Exception as e:  # noqa: BLE001
        sys.exit(f"❌ 실행 역할 스택 실패 → 콘솔 CloudFormation {stack} 이벤트 확인: {str(e)[:200]}")
    out = cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in out}["RoleArn"]


# ------------------------------------------------------------------ 런타임
def find_runtime(ctl, name):
    token = None
    while True:
        r = ctl.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for rt in r.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == name:
                return rt
        token = r.get("nextToken")
        if not token:
            return None


def wait_ready(ctl, rid):
    print("⏳ 런타임 준비 중… (보통 1~3분)")
    for _ in range(90):
        rt = ctl.get_agent_runtime(agentRuntimeId=rid)
        st = rt.get("status")
        if st == "READY":
            return rt
        if st and st.endswith("FAILED"):
            sys.exit(f"❌ {st}: {rt.get('failureReason', '')}")
        time.sleep(5)
    sys.exit("❌ 7분이 지나도 READY 가 아닙니다 → python infra/deploy_agentcore.py --status")


def deploy(s, team, account, bucket, name, mock):
    try:
        ctl = s.client("bedrock-agentcore-control")
    except Exception:  # noqa: BLE001
        sys.exit("❌ boto3 가 오래돼 AgentCore 를 모릅니다: pip install -U boto3")
    data = build_zip()
    key = upload_code(s, bucket, data)
    role_arn = deploy_role(s, team, bucket, name)
    args = dict(
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": bucket, "prefix": key}},
            "runtime": RUNTIME, "entryPoint": ["main.py"]}},
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        environmentVariables={"BUCKET": bucket, "MODEL_ID": os.getenv("MODEL_ID") or MODEL_ID_DEFAULT,
                              "BEDROCK_MOCK": "1" if mock else "0", "TEAM": team},
        description="공공기관 IT 직무 학습경로 상담 에이전트 (aws_edu)",
    )
    existing = find_runtime(ctl, name)
    for attempt in range(3):
        try:
            if existing:
                ctl.update_agent_runtime(agentRuntimeId=existing["agentRuntimeId"], **args)
                rid, verb = existing["agentRuntimeId"], "갱신"
            else:
                r = ctl.create_agent_runtime(agentRuntimeName=name, **args)
                rid, verb = r["agentRuntimeId"], "생성"
            break
        except ctl.exceptions.ValidationException as e:
            if "role" in str(e).lower() and attempt < 2:           # 막 만든 역할이 아직 안 퍼진 경우
                print("   (실행 역할 전파 대기 후 다시 시도)")
                time.sleep(15)
                continue
            sys.exit(f"❌ 런타임 {('갱신' if existing else '생성')} 실패: {str(e)[:500]}")
    rt = wait_ready(ctl, rid)
    print(f"✅ AgentCore 런타임 {verb} 완료: {name}  (모드: {'모의' if mock else '실제 Bedrock'})")
    print(f"   ARN: {rt['agentRuntimeArn']}")
    print('   다음: python infra/deploy_agentcore.py --invoke "이 공고 준비하려면 뭐부터 해야 해요?"')


def invoke(s, name, question, posting, certs):
    ctl = s.client("bedrock-agentcore-control")
    rt = find_runtime(ctl, name)
    if not rt:
        sys.exit("❌ 런타임이 없습니다. 먼저: python infra/deploy_agentcore.py")
    payload = {"prompt": question, "posting_id": posting,
               "profile": {"certs": certs, "experience_years": 0, "completed": [], "interests": ["개발"]}}
    print(f"▶ 질문: {question}  (공고 {posting}, 보유 자격증 {certs or '없음'})")
    t0 = time.time()
    r = s.client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=rt["agentRuntimeArn"], runtimeSessionId=f"aws-edu-{uuid.uuid4()}",
        payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        contentType="application/json", accept="application/json")
    raw = r["response"].read() if hasattr(r.get("response"), "read") else r.get("response")
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        print(raw)
        return
    print(f"   ({time.time() - t0:.1f}초 · 검사: {body.get('stage')} · 사용한 도구: {', '.join(body.get('tools') or []) or '-'})\n")
    print(body.get("response"))
    print("\n✅ AgentCore 호출 성공 — 이 화면을 캡처해 두면 'Bedrock + AgentCore' 동작 증빙이 됩니다.")


def status(s, name):
    ctl = s.client("bedrock-agentcore-control")
    rt = find_runtime(ctl, name)
    if not rt:
        print("런타임 없음")
        return
    d = ctl.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
    print(f"런타임: {name}  상태: {d.get('status')}  버전: {d.get('agentRuntimeVersion')}")
    print(f"ARN: {d.get('agentRuntimeArn')}")
    if d.get("failureReason"):
        print("실패 원인:", d["failureReason"])
    print(f"로그: 콘솔 → CloudWatch → 로그 그룹 /aws/bedrock-agentcore/runtimes/{rt['agentRuntimeId']}-*")


def delete(s, team, name):
    ctl = s.client("bedrock-agentcore-control")
    rt = find_runtime(ctl, name)
    if rt:
        ctl.delete_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
        print("⏳ 런타임 삭제 요청…")
        for _ in range(60):
            if not find_runtime(ctl, name):
                break
            time.sleep(5)
    cfn = s.client("cloudformation")
    cfn.delete_stack(StackName=f"aws-edu-agentcore-{team}")
    cfn.get_waiter("stack_delete_complete").wait(StackName=f"aws-edu-agentcore-{team}")
    print("✅ 런타임 · 실행 역할 삭제 완료")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="aws_edu 상담 에이전트 → AgentCore Runtime")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--invoke", metavar="질문")
    g.add_argument("--status", action="store_true")
    g.add_argument("--delete", action="store_true")
    g.add_argument("--build-only", action="store_true", help="zip 만 만들어 크기 확인 (AWS 호출 없음)")
    ap.add_argument("--posting", default="JA-306979", help="--invoke 에 쓸 공고 ID")
    ap.add_argument("--certs", nargs="*", default=[], help="--invoke 에 쓸 보유 자격증")
    ap.add_argument("--mock", action="store_true", help="모의 모드로 배포 (Bedrock 호출 안 함)")
    a = ap.parse_args()
    if a.build_only:
        data = build_zip()
        print(f"zip {len(data) / 1e6:.1f} MB, 코드 파일 {len(code_files())}개")
        sys.exit(0)
    s, team, account, bucket, name = setup()
    if a.invoke:
        invoke(s, name, a.invoke, a.posting, a.certs)
    elif a.status:
        status(s, name)
    elif a.delete:
        delete(s, team, name)
    else:
        deploy(s, team, account, bucket, name, a.mock)
