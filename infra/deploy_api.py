"""추천 API (API Gateway + Cognito + Lambda) 배포 · 사용자 만들기 · 테스트 · 삭제   담당: 강민경

먼저: aws sso login --profile kosa   (S3 processed/ 에 분석 결과가 올라가 있어야 함)

    python infra/deploy_api.py                        # 배포 (처음엔 생성, 다음부턴 갱신) → .env 에 넣을 값 출력
    python infra/deploy_api.py --create-user demo     # 로그인 사용자 만들기 (비밀번호 입력, 8자 이상·영소문자·숫자)
    python infra/deploy_api.py --test                 # 로그인 → 토큰 없이 호출(401 확인) → 공고 목록 → 추천 1건
    python infra/deploy_api.py --test JA-307033       # 특정 공고로 추천 테스트
    python infra/deploy_api.py --env                  # .env 에 넣을 값 다시 보기 (현진님 Streamlit 로그인용)
    python infra/deploy_api.py --delete               # 전부 삭제 (Cognito 사용자도 같이 삭제됨)

만드는 것: Cognito 사용자 풀 + 앱 클라이언트(시크릿 없음), 추천 Lambda, HTTP API(JWT 인증), 로그 그룹
API:  GET /postings   ·   POST /recommend {"posting_id": "...", "profile": {...}}   (헤더 Authorization: <ID 토큰>)
"""
import argparse
import getpass
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
CODE_FILES = {"lambda_recommend.py": INFRA / "lambda_recommend.py"}
for name in ("__init__.py", "data_io.py", "engine.py", "catalog.py", "courses.py", "stacks.py", "store.py",
             "cert_catalog.json", "courses_sample.json", "resources.json"):
    CODE_FILES[f"recommend/{name}"] = ROOT / "recommend" / name
for name in ("__init__.py", "skills.py"):
    CODE_FILES[f"src/labutil/{name}"] = ROOT / "src" / "labutil" / name
CODE_FILES["data/dict/skill_aliases.json"] = ROOT / "data" / "dict" / "skill_aliases.json"

SAMPLE_PROFILE = {"major_it": True, "experience_years": 0, "completed": [], "certs": ["정보처리기사"],
                  "stacks": ["Java", "MySQL"], "interests": ["개발"], "weeks": 24}


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
    return s, team, f"aws-edu-{team}-{account}", f"aws-edu-api-{team}"


def stack_status(cfn, stack):
    try:
        return cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError:
        return None


def outputs(s, stack, required=True):
    cfn = s.client("cloudformation")
    try:
        st = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    except cfn.exceptions.ClientError:
        if required:
            sys.exit("❌ API 스택이 없습니다. 먼저: python infra/deploy_api.py")
        return None
    return {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}


def first_failure(cfn, stack):
    for e in cfn.describe_stack_events(StackName=stack)["StackEvents"][::-1]:
        if e["ResourceStatus"].endswith("FAILED"):
            return f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')[:300]}"
    return "(원인 이벤트 없음)"


def upload_code(s, bucket):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, path in CODE_FILES.items():
            if not path.exists():
                sys.exit(f"❌ 코드 파일이 없습니다: {path}")
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    data = buf.getvalue()
    key = f"code/lambda_recommend-{hashlib.sha256(data).hexdigest()[:12]}.zip"
    s.client("s3").put_object(Bucket=bucket, Key=key, Body=data)
    print(f"📦 Lambda 코드 → s3://{bucket}/{key}  ({len(data) / 1e3:,.0f} KB)")
    return key


def check_data(s, bucket):
    s3 = s.client("s3")
    need = ["postings_jobalio.json", "extractions.json", "skill_ncs_map.json", "demand_overall.csv"]
    missing = []
    for n in need:
        try:
            s3.head_object(Bucket=bucket, Key="processed/" + n)
        except Exception:  # noqa: BLE001
            missing.append(n)
    if missing:
        print("⚠ S3 processed/ 에 없는 파일:", ", ".join(missing),
              "\n   → python pipeline/upload_s3.py  (또는 파이프라인 실행) 후 다시 테스트하세요.")


def env_values(out, region):
    return {"COGNITO_CLIENT_ID": out["UserPoolClientId"], "COGNITO_REGION": region,
            "COGNITO_USER_POOL_ID": out["UserPoolId"], "RECOMMEND_API_URL": out["ApiUrl"]}


def write_env(values, path=None):
    """.env 의 같은 이름 줄은 바꾸고, 없으면 끝에 추가 (다른 줄은 그대로)"""
    path = path or ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    left = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in left:
            lines[i] = f"{key}={left.pop(key)}"
    if left:
        lines += ["", "# Cognito 로그인 · 추천 API (infra/deploy_api.py 가 자동으로 씀)"] + [f"{k}={v}" for k, v in left.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_env(out, region, write=True):
    values = env_values(out, region)
    print("\n📝 Streamlit 로그인(recommend/auth.py)이 읽는 값")
    for k, v in values.items():
        print(f"{k}={v}")
    if write:
        write_env(values)
        print("✅ .env 에 자동으로 넣었습니다 → streamlit 을 다시 켜면 로그인 화면이 나와요."
              "\n   팀원들은 위 4줄을 각자 .env 에 붙여 넣으면 됩니다 (아이디는 --create-user 로 만든 것).")


def deploy(s, bucket, stack):
    check_data(s, bucket)
    code_key = upload_code(s, bucket)
    cfn = s.client("cloudformation")
    team = stack.removeprefix("aws-edu-api-")
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in
              {"Team": team, "BucketName": bucket, "CodeKey": code_key}.items()]
    body = (INFRA / "api-stack.yaml").read_text(encoding="utf-8")
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
            print_env(outputs(s, stack), s.region_name)
            return
        raise
    print(f"⏳ 스택 {verb} 중… (2~4분)  콘솔 → CloudFormation → {stack} → 이벤트 탭")
    try:
        cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    except Exception:  # noqa: BLE001
        sys.exit(f"❌ {verb} 실패 → {first_failure(cfn, stack)}")
    out = outputs(s, stack)
    print(f"✅ 스택 {verb} 완료: {stack}")
    for k, v in out.items():
        print(f"   {k}: {v}")
    print_env(out, s.region_name)
    print("\n   다음: python infra/deploy_api.py --create-user demo  →  python infra/deploy_api.py --test")


def create_user(s, stack, username):
    out = outputs(s, stack)
    pw = getpass.getpass(f"'{username}' 비밀번호 (8자 이상, 영소문자+숫자 포함): ")
    if pw != getpass.getpass("한 번 더: "):
        sys.exit("❌ 비밀번호가 서로 다릅니다.")
    idp = s.client("cognito-idp")
    try:
        idp.admin_create_user(UserPoolId=out["UserPoolId"], Username=username, MessageAction="SUPPRESS")
    except idp.exceptions.UsernameExistsException:
        print("ℹ 이미 있는 사용자 → 비밀번호만 바꿉니다.")
    try:
        idp.admin_set_user_password(UserPoolId=out["UserPoolId"], Username=username, Password=pw, Permanent=True)
    except idp.exceptions.InvalidPasswordException as e:
        sys.exit(f"❌ 비밀번호 규칙 위반: {e.response['Error']['Message']}")
    print(f"✅ 사용자 '{username}' 준비 완료 (Streamlit 로그인 · --test 에 사용)")


def login(s, out):
    username = input("아이디: ").strip()
    pw = getpass.getpass("비밀번호: ")
    idp = s.client("cognito-idp")
    try:
        r = idp.initiate_auth(ClientId=out["UserPoolClientId"], AuthFlow="USER_PASSWORD_AUTH",
                              AuthParameters={"USERNAME": username, "PASSWORD": pw})
    except idp.exceptions.NotAuthorizedException:
        sys.exit("❌ 아이디 또는 비밀번호가 틀렸습니다. (--create-user 로 만든 계정인지 확인)")
    return r["AuthenticationResult"]["IdToken"]


def call(url, token=None, body=None):
    req = urllib.request.Request(url, method="POST" if body is not None else "GET",
                                 data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or "null")
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")[:300]


def test(s, stack, posting_id=None):
    out = outputs(s, stack)
    api = out["ApiUrl"]
    code, _ = call(f"{api}/postings")
    print(f"① 토큰 없이 호출 → {code} {'✅ (인증 막힘 확인)' if code == 401 else '⚠ 401 이 아님 — 인증 설정 확인'}")
    token = login(s, out)
    print("② Cognito 로그인 ✅ (ID 토큰 받음)")
    code, body = call(f"{api}/postings", token)
    if code != 200:
        sys.exit(f"❌ GET /postings → {code}: {body}")
    posts = body["postings"]
    print(f"③ GET /postings → 200, 진행 중 공고 {body['count']}건")
    for p in posts[:3]:
        print(f"     {p['id']}  {p['company']} — {p['title'][:30]}  (마감 {p['close_at']})")
    pid = posting_id or (posts[0]["id"] if posts else None)
    if not pid:
        sys.exit("진행 중 공고가 없습니다.")
    code, r = call(f"{api}/recommend", token, {"posting_id": pid, "profile": SAMPLE_PROFILE})
    if code != 200:
        sys.exit(f"❌ POST /recommend → {code}: {str(r)[:500]}")
    print(f"④ POST /recommend ({pid}, 예시 프로필: 정보처리기사·Java·MySQL) → 200")
    print(f"     판정: {r.get('verdict')}  ·  추천: {r.get('top_pick')}  ·  총 {r.get('total_weeks')}주")
    for row in (r.get("profile_check") or [])[:4]:
        print(f"     [{row.get('판정')}] {row.get('요건')} ← 공고 근거: {str(row.get('공고 근거'))[:40]}")
    print("\n✅ API 테스트 통과 — 이 화면을 캡처해 두면 'API Gateway + Cognito' 동작 증빙이 됩니다.")


def delete(s, stack):
    cfn = s.client("cloudformation")
    if stack_status(cfn, stack) is None:
        print("이미 없는 스택입니다.")
        return
    cfn.delete_stack(StackName=stack)
    print("⏳ 삭제 중…")
    cfn.get_waiter("stack_delete_complete").wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    print("✅ 삭제 완료 (.env 의 COGNITO_* · RECOMMEND_API_URL 줄도 지우세요)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="aws_edu 추천 API (API Gateway + Cognito)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--create-user", metavar="아이디")
    g.add_argument("--test", nargs="?", const="", metavar="공고ID")
    g.add_argument("--env", action="store_true")
    g.add_argument("--delete", action="store_true")
    a = ap.parse_args()
    s, team, bucket, stack = setup()
    if a.create_user:
        create_user(s, stack, a.create_user)
    elif a.test is not None:
        test(s, stack, a.test or None)
    elif a.env:
        print_env(outputs(s, stack), s.region_name)
    elif a.delete:
        delete(s, stack)
    else:
        deploy(s, bucket, stack)
