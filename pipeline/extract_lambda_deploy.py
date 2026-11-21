"""역량 추출 Lambda 배포 · 테스트 · 삭제 (CloudFormation)   담당: 조호성
팀장님 infra/deploy_collect.py 와 같은 방식입니다.

먼저: aws sso login --profile kosa

    python pipeline/extract_lambda_deploy.py --mock          # 배포 (모의 모드: Bedrock 안 부름, 비용 0) ← 처음엔 이걸로
    python pipeline/extract_lambda_deploy.py                 # 배포 (실제 Bedrock)
    python pipeline/extract_lambda_deploy.py --invoke JA-307006   # 공고 1건 바로 실행해 보기 (결과 + 로그)
    python pipeline/extract_lambda_deploy.py --send JA-307006 JA-306985   # SQS 에 메시지 보내기 (Lambda 가 알아서 처리)
    python pipeline/extract_lambda_deploy.py --send-ongoing  # 진행 중 공고 중 아직 안 뽑은 것 전부 SQS 로
    python pipeline/extract_lambda_deploy.py --merge         # 공고별 결과 → processed/extractions.json 합치기
    python pipeline/extract_lambda_deploy.py --status        # 스택·대기열·실패 대기열 상태
    python pipeline/extract_lambda_deploy.py --logs          # 최근 30분 로그
    python pipeline/extract_lambda_deploy.py --delete        # 전부 삭제 (S3 결과는 남김)

만드는 것 (이름에 팀 이름이 붙음):
    SQS  aws-edu-<팀>-extract-queue (+ 실패 대기열 -dlq)
    Lambda aws-edu-<팀>-extract,  IAM 역할 aws-edu-<팀>-extract-role,  로그 그룹
팀장님 Step Functions 연결: 대기열에 {"id": "<공고ID>"} 메시지를 보내거나, Lambda 를 {"id": ...} 로 직접 호출
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STACK_FILE = ROOT / "pipeline" / "extract_lambda_stack.yaml"
CODE_FILES = {                                      # Lambda zip 안 경로 : 내 PC 파일
    "extract_lambda.py": ROOT / "pipeline" / "extract_lambda.py",
    "extract_competencies.py": ROOT / "pipeline" / "extract_competencies.py",
    "extract_rules.py": ROOT / "pipeline" / "extract_rules.py",
    "labutil/__init__.py": ROOT / "src" / "labutil" / "__init__.py",
    "labutil/bedrock.py": ROOT / "src" / "labutil" / "bedrock.py",
}
REAL_CONFIRM_OVER = 5


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
    return s, team, f"aws-edu-{team}-{account}", f"aws-edu-extract-{team}"


def upload_code(s, bucket):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, path in CODE_FILES.items():
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))   # 같은 코드면 같은 zip
            info.external_attr = 0o644 << 16
            z.writestr(info, path.read_bytes())
    data = buf.getvalue()
    code_key = f"code/extract_lambda-{hashlib.sha256(data).hexdigest()[:12]}.zip"
    s.client("s3").put_object(Bucket=bucket, Key=code_key, Body=data)
    print(f"📦 Lambda 코드 ({len(CODE_FILES)}개 파일) → s3://{bucket}/{code_key}")
    return code_key


def stack_status(cfn, stack):
    try:
        return cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError:
        return None


def outputs(s, stack):
    st = s.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}


def first_failure(cfn, stack):
    for e in cfn.describe_stack_events(StackName=stack)["StackEvents"][::-1]:
        if e["ResourceStatus"].endswith("FAILED"):
            return f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')[:300]}"
    return "(원인 이벤트 없음)"


def deploy(s, team, bucket, stack, mock):
    code_key = upload_code(s, bucket)
    cfn = s.client("cloudformation")
    model = os.getenv("FAST_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in {
        "Team": team, "BucketName": bucket, "CodeKey": code_key, "ModelId": model,
        "BedrockMock": "1" if mock else "0"}.items()]
    body = STACK_FILE.read_text(encoding="utf-8")
    st = stack_status(cfn, stack)
    if st == "ROLLBACK_COMPLETE":
        print("이전 생성이 실패한 스택이 있어 지우고 다시 만듭니다.")
        cfn.delete_stack(StackName=stack)
        cfn.get_waiter("stack_delete_complete").wait(StackName=stack)
        st = None
    try:
        kw = dict(StackName=stack, TemplateBody=body, Parameters=params, Capabilities=["CAPABILITY_NAMED_IAM"])
        if st is None:
            cfn.create_stack(**kw)
            waiter, verb = "stack_create_complete", "생성"
        else:
            cfn.update_stack(**kw)
            waiter, verb = "stack_update_complete", "갱신"
    except cfn.exceptions.ClientError as e:
        if "No updates are to be performed" in str(e):
            print("✅ 바뀐 것이 없어 그대로 둡니다.")
            return
        raise
    print(f"⏳ 스택 {verb} 중… (1~3분)  모드: {'모의 (Bedrock 호출 없음)' if mock else '실제 Bedrock — ' + model}")
    try:
        cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    except Exception:  # noqa: BLE001
        sys.exit(f"❌ {verb} 실패 → {first_failure(cfn, stack)}")
    print(f"✅ 스택 {verb} 완료: {stack}")
    for k, v in outputs(s, stack).items():
        print(f"   {k}: {v}")
    print("   다음: python pipeline/extract_lambda_deploy.py --invoke JA-307006  (공고 1건 실행해 보기)")


def lambda_mode(s, team):
    env = s.client("lambda").get_function_configuration(FunctionName=f"aws-edu-{team}-extract")
    return "mock" if env.get("Environment", {}).get("Variables", {}).get("BEDROCK_MOCK") == "1" else "bedrock"


def invoke(s, team, payload):
    fn = f"aws-edu-{team}-extract"
    print(f"▶ {fn} 실행 ({lambda_mode(s, team)} 모드): {json.dumps(payload, ensure_ascii=False)}")
    r = s.client("lambda").invoke(FunctionName=fn, LogType="Tail", Payload=json.dumps(payload).encode())
    out = json.loads(r["Payload"].read() or "null")
    log = base64.b64decode(r.get("LogResult", "")).decode("utf-8", "replace")
    print(("❌ 실행 오류: " if r.get("FunctionError") else "✅ 결과: ") + json.dumps(out, ensure_ascii=False)[:800])
    print("\n--- 로그 마지막 부분 ---\n" + log[-1500:])


def send(s, team, stack, ids, yes):
    if lambda_mode(s, team) == "bedrock" and len(ids) > REAL_CONFIRM_OVER and not yes:
        sys.exit(f"⛔ 실제 Bedrock 모드에서 {len(ids)}건은 비용이 듭니다 (1건 약 6원). 진행하려면 --yes")
    url = outputs(s, stack)["QueueUrl"]
    sqs = s.client("sqs")
    for i in range(0, len(ids), 10):                  # SQS 는 한 번에 10개까지
        chunk = ids[i:i + 10]
        sqs.send_message_batch(QueueUrl=url, Entries=[
            {"Id": str(n), "MessageBody": json.dumps({"id": pid})} for n, pid in enumerate(chunk)])
    print(f"📨 메시지 {len(ids)}건 → {url.rsplit('/', 1)[-1]}")
    print("   Lambda 가 알아서 처리합니다. 진행 상황: --status / --logs,  다 끝나면: --merge")


def ongoing_todo(s, bucket):
    """S3 공고 중 진행 중인데 Lambda 결과(processed/extractions/)가 없는 공고 ID"""
    s3 = s.client("s3")
    posts = json.loads(s3.get_object(Bucket=bucket, Key="processed/postings_jobalio.json")["Body"].read())
    done = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="processed/extractions/"):
        done |= {o["Key"].rsplit("/", 1)[-1].removesuffix(".json") for o in page.get("Contents", [])}
    merged = {}
    try:
        merged = {r["id"]: r for r in json.loads(
            s3.get_object(Bucket=bucket, Key="processed/extractions.json")["Body"].read())}
    except s3.exceptions.NoSuchKey:
        pass
    return [p["id"] for p in posts if p.get("ongoing") and p["id"] not in done
            and merged.get(p["id"], {}).get("meta", {}).get("mode") != "bedrock"]


def status(s, team, stack):
    cfn = s.client("cloudformation")
    print("스택:", stack, "→", stack_status(cfn, stack) or "없음")
    if not stack_status(cfn, stack):
        return
    out = outputs(s, stack)
    sqs = s.client("sqs")
    names = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
    q = sqs.get_queue_attributes(QueueUrl=out["QueueUrl"], AttributeNames=names)["Attributes"]
    d = sqs.get_queue_attributes(QueueUrl=out["DeadLetterQueueUrl"], AttributeNames=names[:1])["Attributes"]
    print(f"Lambda 모드: {lambda_mode(s, team)}")
    print(f"대기열: 기다리는 메시지 {q[names[0]]}건 · 처리 중 {q[names[1]]}건")
    print(f"실패 대기열(DLQ): {d[names[0]]}건" + ("  ⚠ --logs 로 오류 확인" if d[names[0]] != "0" else ""))


def logs(s, team, minutes=30):
    ev = s.client("logs").filter_log_events(
        logGroupName=f"/aws/lambda/aws-edu-{team}-extract", startTime=int((time.time() - minutes * 60) * 1000))["events"]
    print(f"최근 {minutes}분 로그 {len(ev)}줄")
    for e in ev[-60:]:
        msg = e["message"].strip()
        if msg.startswith(("START", "END", "REPORT")):
            continue
        print("  " + msg[:300])


def delete(s, stack):
    cfn = s.client("cloudformation")
    if stack_status(cfn, stack) is None:
        print("이미 없는 스택입니다.")
        return
    cfn.delete_stack(StackName=stack)
    print("⏳ 삭제 중…")
    cfn.get_waiter("stack_delete_complete").wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    print("✅ 삭제 완료 (S3 추출 결과는 남겨 둡니다)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="역량 추출 Lambda 배포·테스트")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--invoke", metavar="ID", help="공고 1건 바로 실행")
    g.add_argument("--send", nargs="+", metavar="ID", help="SQS 에 공고 ID 메시지 보내기")
    g.add_argument("--send-ongoing", action="store_true", help="진행 중 공고 중 안 뽑은 것 전부 SQS 로")
    g.add_argument("--merge", action="store_true", help="공고별 결과 → extractions.json 합치기")
    g.add_argument("--status", action="store_true")
    g.add_argument("--logs", action="store_true")
    g.add_argument("--delete", action="store_true")
    ap.add_argument("--mock", action="store_true", help="배포할 때 모의 모드로 (Bedrock 호출 없음)")
    ap.add_argument("--yes", action="store_true", help=f"실제 모드에서 {REAL_CONFIRM_OVER}건 초과 보내기 허용")
    a = ap.parse_args()
    s, team, bucket, stack = setup()
    if a.invoke:
        invoke(s, team, {"id": a.invoke})
    elif a.send:
        send(s, team, stack, a.send, a.yes)
    elif a.send_ongoing:
        todo = ongoing_todo(s, bucket)
        print(f"진행 중 공고 중 아직 안 뽑은 것: {len(todo)}건")
        if todo:
            send(s, team, stack, todo, a.yes)
    elif a.merge:
        invoke(s, team, {"action": "merge"})
    elif a.status:
        status(s, team, stack)
    elif a.logs:
        logs(s, team)
    elif a.delete:
        delete(s, stack)
    else:
        deploy(s, team, bucket, stack, a.mock)
