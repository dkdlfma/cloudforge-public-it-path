"""자동 수집 Lambda 배포 · 실행 · 삭제 (CloudFormation)

먼저: aws sso login --profile kosa  (S3 버킷은 upload_s3.py 로 이미 만들어져 있어야 함)

    python infra/deploy_collect.py            # 배포 (처음엔 생성, 다음부턴 코드·설정 갱신)
    python infra/deploy_collect.py --invoke   # 지금 바로 한 번 실행해 보기 (결과 + 로그)
    python infra/deploy_collect.py --status   # 스택 상태 · 다음 실행 규칙 확인
    python infra/deploy_collect.py --history  # 매일 자동 실행이 실제로 됐는지 (날짜별 결과)
    python infra/deploy_collect.py --delete   # 전부 삭제 (Lambda·규칙·역할·로그 그룹)

하는 일 (배포):
  1) .env 의 공공데이터 인증키 → SSM Parameter Store 에 SecureString(암호화)으로 저장
  2) infra/lambda_collect.py 를 zip 으로 묶어 S3 code/ 에 업로드
  3) infra/collect-stack.yaml 로 CloudFormation 스택 생성/갱신 → 완료까지 대기
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
PARAM_NAME = "/edu-team/jobalio-key"   # aws, ssm 으로 시작하는 이름은 AWS 예약어라 쓸 수 없음


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
    return s, region, team, f"aws-edu-{team}-{account}", f"aws-edu-collect-{team}"


def put_key(s):
    key = os.getenv("DATA_GO_KR_API_KEY", "").strip()
    if not key:
        sys.exit("❌ .env 에 DATA_GO_KR_API_KEY 가 없습니다.")
    s.client("ssm").put_parameter(Name=PARAM_NAME, Value=unquote(key), Type="SecureString",
                                  Overwrite=True, Description="aws_edu 잡알리오 API 인증키")
    print(f"🔐 인증키 → SSM {PARAM_NAME} (SecureString)")


def upload_code(s, bucket):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("lambda_collect.py", date_time=(2026, 1, 1, 0, 0, 0))  # 고정 시간 → 같은 코드면 같은 zip
        info.external_attr = 0o644 << 16
        z.writestr(info, (INFRA / "lambda_collect.py").read_bytes())
    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:12]
    code_key = f"code/lambda_collect-{digest}.zip"   # 코드가 바뀌면 이름이 바뀌어 스택이 갱신됨
    s.client("s3").put_object(Bucket=bucket, Key=code_key, Body=data)
    print(f"📦 Lambda 코드 → s3://{bucket}/{code_key}")
    return code_key


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


def deploy(s, region, team, bucket, stack):
    put_key(s)
    code_key = upload_code(s, bucket)
    cfn = s.client("cloudformation")
    # 파이프라인(Step Functions)이 배포돼 있으면 수집은 파이프라인 첫 단계에서 하므로 단독 스케줄은 끔
    pipeline_on = stack_status(cfn, f"aws-edu-pipeline-{team}") is not None
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in
              {"Team": team, "BucketName": bucket, "CodeKey": code_key, "ParamName": PARAM_NAME,
               "ScheduleState": "DISABLED" if pipeline_on else "ENABLED"}.items()]
    body = (INFRA / "collect-stack.yaml").read_text(encoding="utf-8")
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
            return
        raise
    print(f"⏳ 스택 {verb} 중… (1~3분)  콘솔 → CloudFormation → {stack} → 이벤트 탭에서 진행 상황 확인")
    try:
        cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    except Exception:  # noqa: BLE001
        sys.exit(f"❌ {verb} 실패 → {first_failure(cfn, stack)}")
    out = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])}
    print(f"✅ 스택 {verb} 완료: {stack}")
    for k, v in out.items():
        print(f"   {k}: {v}")
    print("   다음: python infra/deploy_collect.py --invoke  (지금 한 번 실행해 보기)")


def invoke(s, team):
    fn = f"aws-edu-{team}-collect"
    print(f"▶ {fn} 실행 중… (최대 1~2분)")
    r = s.client("lambda").invoke(FunctionName=fn, LogType="Tail")
    payload = json.loads(r["Payload"].read() or "null")
    log = base64.b64decode(r.get("LogResult", "")).decode("utf-8", "replace")
    if r.get("FunctionError"):
        print("❌ 실행 오류:", json.dumps(payload, ensure_ascii=False)[:800])
    else:
        print("✅ 결과:", json.dumps(payload, ensure_ascii=False))
        print("   (new = 새로 추가된 공고, updated = 상태가 갱신된 기존 공고, after = 전체 공고 수)")
    print("\n--- 로그 마지막 부분 ---\n" + log[-1500:])


def status(s, team, stack):
    cfn = s.client("cloudformation")
    print("스택:", stack, "→", stack_status(cfn, stack) or "없음")
    try:
        rule = s.client("events").describe_rule(Name=f"aws-edu-{team}-collect-daily")
        print("스케줄:", rule["ScheduleExpression"], "/", rule["State"], "(UTC 기준, 18시 = 한국 새벽 3시)")
    except Exception:  # noqa: BLE001
        print("스케줄: 없음")


def history(s, team, bucket, days=14):
    """S3 에 쌓인 날짜별 원본 폴더 + CloudWatch 로그의 실행 결과로 '매일 실행됐는지' 확인"""
    import time
    s3 = s.client("s3")
    r = s3.list_objects_v2(Bucket=bucket, Prefix="raw/jobalio/daily/", Delimiter="/")
    dates = sorted(p["Prefix"].split("/")[-2] for p in r.get("CommonPrefixes", []))
    print("📁 S3 에 원본이 쌓인 날짜:", ", ".join(dates) if dates else "없음")
    logs = s.client("logs")
    start = int((time.time() - days * 86400) * 1000)
    try:
        ev = logs.filter_log_events(logGroupName=f"/aws/lambda/aws-edu-{team}-collect",
                                    startTime=start, filterPattern='"before"')["events"]
    except Exception as e:  # noqa: BLE001
        print("로그를 읽지 못했습니다:", str(e)[:150])
        return
    print(f"\n🕒 최근 {days}일 실행 결과 (한국시간)")
    from datetime import datetime, timedelta, timezone
    kst = timezone(timedelta(hours=9))
    for e in ev:
        t = datetime.fromtimestamp(e["timestamp"] / 1000, kst).strftime("%m-%d %H:%M")
        try:
            d = json.loads(e["message"])
            print(f"  {t}  새 공고 {d['new']:>3}건 · 갱신 {d['updated']:>3}건 · 전체 {d['after']:,}건")
        except (ValueError, KeyError):
            print(f"  {t}  {e['message'].strip()[:120]}")
    errs = logs.filter_log_events(logGroupName=f"/aws/lambda/aws-edu-{team}-collect",
                                  startTime=start, filterPattern="?ERROR ?Traceback ?Task timed out")["events"]
    print(f"\n{'⚠ 오류 로그 ' + str(len(errs)) + '건 — 콘솔 CloudWatch 에서 확인' if errs else '✅ 오류 로그 없음'}")


def delete(s, stack):
    cfn = s.client("cloudformation")
    if stack_status(cfn, stack) is None:
        print("이미 없는 스택입니다.")
        return
    cfn.delete_stack(StackName=stack)
    print("⏳ 삭제 중…")
    cfn.get_waiter("stack_delete_complete").wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    print("✅ 삭제 완료 (S3 데이터와 SSM 인증키는 남겨 둡니다)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--invoke", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--delete", action="store_true")
    g.add_argument("--history", action="store_true")
    a = ap.parse_args()
    s, region, team, bucket, stack = setup()
    if a.invoke:
        invoke(s, team)
    elif a.status:
        status(s, team, stack)
    elif a.delete:
        delete(s, stack)
    elif a.history:
        history(s, team, bucket)
    else:
        deploy(s, region, team, bucket, stack)
