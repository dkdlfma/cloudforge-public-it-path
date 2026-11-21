"""매일 자동 파이프라인 (Step Functions) 배포 · 실행 · 삭제   담당: 강민경

먼저 (순서대로 한 번씩):
    aws sso login --profile kosa
    python infra/deploy_collect.py                  # 수집 Lambda (강민경)
    python pipeline/extract_lambda_deploy.py        # 추출 SQS + Lambda (조호성)
    python pipeline/upload_s3.py                    # 최신 extractions.json 등 S3 에 올리기

그다음:
    python infra/deploy_pipeline.py                 # 배포 (처음엔 생성, 다음부턴 갱신)
    python infra/deploy_pipeline.py --run           # 지금 한 번 실행 → 단계별 진행 상황 출력 (추출이 있으면 수 분)
    python infra/deploy_pipeline.py --status        # 스택 · 매일 실행 규칙 · 최근 실행 5개
    python infra/deploy_pipeline.py --delete        # 파이프라인만 삭제 (수집·추출 스택과 S3 데이터는 남김)

흐름 (콘솔 → Step Functions → aws-edu-<팀>-pipeline 에서 그림으로 확인 가능):
    EventBridge(매일 03시) → [Collect] 수집 Lambda → [SelectTargets] 진행 중·미추출 공고 선정
      → [SendToQueue] 공고마다 SQS 메시지 → 추출 Lambda 가 대기열에서 꺼내 Bedrock 추출
      → [CheckQueue] 대기열이 빌 때까지 1분마다 확인 → [MergeExtractions] 결과 합치기
      → [Analyze] NCS 매칭 + 수요 분석 → S3 processed/ 갱신
"""
import argparse
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
INFRA = ROOT / "infra"
CODE_FILES = {                                          # Lambda zip 안 경로 : 내 PC 파일
    "lambda_pipeline.py": INFRA / "lambda_pipeline.py",
    "pipeline/match_ncs.py": ROOT / "pipeline" / "match_ncs.py",
    "pipeline/analyze_demand.py": ROOT / "pipeline" / "analyze_demand.py",
    "src/labutil/__init__.py": ROOT / "src" / "labutil" / "__init__.py",
    "src/labutil/bedrock.py": ROOT / "src" / "labutil" / "bedrock.py",
    "src/labutil/vector.py": ROOT / "src" / "labutil" / "vector.py",
    "data/ncs/ncs_units_official.json": ROOT / "data" / "ncs" / "ncs_units_official.json",
}


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
    return s, team, f"aws-edu-{team}-{account}", f"aws-edu-pipeline-{team}"


def outputs(cfn, stack):
    try:
        st = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    except cfn.exceptions.ClientError:
        return None
    return {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}


def stack_status(cfn, stack):
    try:
        return cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError:
        return None


def first_failure(cfn, stack):
    for e in cfn.describe_stack_events(StackName=stack)["StackEvents"][::-1]:
        if e["ResourceStatus"].endswith("FAILED"):
            return f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')[:300]}"
    return "(원인 이벤트 없음)"


def prerequisites(s, team):
    """수집 스택(강민경) · 추출 스택(조호성)이 먼저 있어야 함 → 이름·대기열 정보를 가져옴"""
    cfn = s.client("cloudformation")
    collect = outputs(cfn, f"aws-edu-collect-{team}")
    extract = outputs(cfn, f"aws-edu-extract-{team}")
    missing = []
    if not collect:
        missing.append("수집 스택 → python infra/deploy_collect.py")
    if not extract:
        missing.append("추출 스택 → python pipeline/extract_lambda_deploy.py")
    if missing:
        sys.exit("❌ 먼저 배포해야 할 스택이 있습니다:\n   " + "\n   ".join(missing))
    return {"CollectFunctionName": collect["FunctionName"], "ExtractFunctionName": extract["FunctionName"],
            "ExtractQueueUrl": extract["QueueUrl"], "ExtractQueueArn": extract["QueueArn"],
            "ExtractDLQName": extract["DeadLetterQueueUrl"].rstrip("/").rsplit("/", 1)[-1]}


def upload_code(s, bucket):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, path in CODE_FILES.items():
            if not path.exists():
                sys.exit(f"❌ 코드 파일이 없습니다: {path}")
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))   # 같은 코드면 같은 zip
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    data = buf.getvalue()
    key = f"code/lambda_pipeline-{hashlib.sha256(data).hexdigest()[:12]}.zip"
    s.client("s3").put_object(Bucket=bucket, Key=key, Body=data)
    print(f"📦 Lambda 코드 → s3://{bucket}/{key}  ({len(data) / 1e3:,.0f} KB)")
    return key


def disable_collect_schedule(s, team):
    """수집은 이제 파이프라인 첫 단계에서 하므로, 수집 스택의 단독 매일 실행 규칙은 끔 (하루 두 번 수집 방지)"""
    ev = s.client("events")
    name = f"aws-edu-{team}-collect-daily"
    try:
        if ev.describe_rule(Name=name)["State"] == "ENABLED":
            ev.disable_rule(Name=name)
            print(f"⏸ 수집 단독 스케줄 {name} 끔 → 이제 파이프라인 규칙이 매일 수집부터 실행")
    except ev.exceptions.ResourceNotFoundException:
        pass


def deploy(s, team, bucket, stack, max_per_run):
    pre = prerequisites(s, team)
    code_key = upload_code(s, bucket)
    cfn = s.client("cloudformation")
    params = {"Team": team, "BucketName": bucket, "CodeKey": code_key, "MaxPerRun": str(max_per_run), **pre}
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()]
    body = (INFRA / "pipeline-stack.yaml").read_text(encoding="utf-8")
    st = stack_status(cfn, stack)
    if st == "ROLLBACK_COMPLETE":
        print("이전 생성이 실패한 스택이 있어 지우고 다시 만듭니다.")
        cfn.delete_stack(StackName=stack)
        cfn.get_waiter("stack_delete_complete").wait(StackName=stack)
        st = None
    try:
        if st is None:
            cfn.create_stack(StackName=stack, TemplateBody=body, Parameters=params, Capabilities=["CAPABILITY_NAMED_IAM"])
            waiter, verb = "stack_create_complete", "생성"
        else:
            cfn.update_stack(StackName=stack, TemplateBody=body, Parameters=params, Capabilities=["CAPABILITY_NAMED_IAM"])
            waiter, verb = "stack_update_complete", "갱신"
    except cfn.exceptions.ClientError as e:
        if "No updates are to be performed" in str(e):
            print("✅ 바뀐 것이 없어 그대로 둡니다.")
            disable_collect_schedule(s, team)
            return
        raise
    print(f"⏳ 스택 {verb} 중… (2~4분)  콘솔 → CloudFormation → {stack} → 이벤트 탭")
    try:
        cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    except Exception:  # noqa: BLE001
        sys.exit(f"❌ {verb} 실패 → {first_failure(cfn, stack)}")
    print(f"✅ 스택 {verb} 완료: {stack}")
    for k, v in (outputs(cfn, stack) or {}).items():
        print(f"   {k}: {v}")
    disable_collect_schedule(s, team)
    print("   다음: python infra/deploy_pipeline.py --run   (지금 한 번 실행해 보기)")


def run(s, stack, poll=10):
    sfn = s.client("stepfunctions")
    out = outputs(s.client("cloudformation"), stack)
    if not out:
        sys.exit("❌ 파이프라인 스택이 없습니다. 먼저: python infra/deploy_pipeline.py")
    arn = out["StateMachineArn"]
    ex = sfn.start_execution(stateMachineArn=arn, input=json.dumps({"trigger": "manual"}))
    region = s.region_name
    print(f"▶ 실행 시작: {ex['executionArn'].split(':')[-1]}")
    print(f"   콘솔 그림: https://{region}.console.aws.amazon.com/states/home?region={region}#/v2/executions/details/{ex['executionArn']}")
    seen = set()
    while True:
        time.sleep(poll)
        hist = sfn.get_execution_history(executionArn=ex["executionArn"], reverseOrder=False, maxResults=1000)["events"]
        for h in hist:
            if h["type"] == "TaskStateEntered" or h["type"] in ("MapStateEntered", "WaitStateEntered", "ChoiceStateEntered"):
                name = (h.get("stateEnteredEventDetails") or {}).get("name")
                if name and (h["id"] not in seen):
                    seen.add(h["id"])
                    print(f"   {time.strftime('%H:%M:%S')}  → {name}")
        d = sfn.describe_execution(executionArn=ex["executionArn"])
        if d["status"] != "RUNNING":
            break
    if d["status"] != "SUCCEEDED":
        print(f"❌ {d['status']}: {d.get('error', '')} {str(d.get('cause', ''))[:600]}")
        return
    o = json.loads(d.get("output") or "{}")
    c = (o.get("collect") or {}).get("result", {})
    t = o.get("targets") or {}
    m = (o.get("merge") or {}).get("result", {})
    a = (o.get("analyze") or {}).get("result", {})
    print("\n✅ 파이프라인 성공")
    if c:
        print(f"   수집   : 새 공고 {c.get('new', 0)}건 · 갱신 {c.get('updated', 0)}건 · 전체 {c.get('after', 0):,}건")
    print(f"   대상   : 진행 중 {t.get('ongoing', '-')}건 중 미추출 {t.get('todo', '-')}건 → 이번에 {t.get('count', 0)}건 SQS 로")
    if m:
        print(f"   합치기 : 추출 결과 {m.get('before')} → {m.get('after')}건 (추가 {m.get('added')} · 갱신 {m.get('updated')})")
    if a:
        print(f"   분석   : {', '.join(a.get('uploaded', []))}  ({a.get('seconds')}초)")
    print("   팀원 PC 로 받기: python pipeline/upload_s3.py --download")


def status(s, team, stack):
    cfn = s.client("cloudformation")
    print("스택:", stack, "→", stack_status(cfn, stack) or "없음")
    ev = s.client("events")
    for name in (f"aws-edu-{team}-pipeline-daily", f"aws-edu-{team}-collect-daily"):
        try:
            r = ev.describe_rule(Name=name)
            print(f"규칙: {name}  {r['ScheduleExpression']} / {r['State']}  (UTC 18시 = 한국 03시)")
        except ev.exceptions.ResourceNotFoundException:
            pass
    try:
        alarms = s.client("cloudwatch").describe_alarms(AlarmNamePrefix=f"aws-edu-{team}-")["MetricAlarms"]
        for al in alarms:
            print(f"알람: {al['AlarmName']:<36} {al['StateValue']}")
    except Exception:  # noqa: BLE001
        pass
    out = outputs(cfn, stack)
    if out:
        sfn = s.client("stepfunctions")
        runs = sfn.list_executions(stateMachineArn=out["StateMachineArn"], maxResults=5)["executions"]
        print("\n최근 실행 (한국시간):" if runs else "\n아직 실행 기록이 없습니다.")
        from datetime import timedelta, timezone
        kst = timezone(timedelta(hours=9))
        for r in runs:
            took = (r["stopDate"] - r["startDate"]).seconds if r.get("stopDate") else None
            print(f"  {r['startDate'].astimezone(kst):%m-%d %H:%M}  {r['status']:<9}"
                  f"{f'{took // 60}분 {took % 60}초' if took is not None else '실행 중'}")


def delete(s, stack):
    cfn = s.client("cloudformation")
    if stack_status(cfn, stack) is None:
        print("이미 없는 스택입니다.")
        return
    cfn.delete_stack(StackName=stack)
    print("⏳ 삭제 중…")
    cfn.get_waiter("stack_delete_complete").wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    print("✅ 삭제 완료. 수집만 매일 돌리려면: python infra/deploy_collect.py (규칙이 다시 켜짐)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="aws_edu Step Functions 파이프라인")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--run", action="store_true", help="지금 한 번 실행")
    g.add_argument("--status", action="store_true")
    g.add_argument("--delete", action="store_true")
    ap.add_argument("--max-per-run", type=int, default=20, help="하루 최대 추출 공고 수 (기본 20)")
    a = ap.parse_args()
    s, team, bucket, stack = setup()
    if a.run:
        run(s, stack)
    elif a.status:
        status(s, team, stack)
    elif a.delete:
        delete(s, stack)
    else:
        deploy(s, team, bucket, stack, a.max_per_run)
