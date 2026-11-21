"""CloudFormation 스택 배포·조회·삭제 (6주차) — 템플릿 한 장으로 AWS 자원 여러 개를 한 번에"""
import time

from . import config

# 자원 종류 → (한 줄 설명, 콘솔에서 찾는 곳)
RESOURCE_GUIDE = {
    "AWS::S3Vectors::VectorBucket": ("벡터 전용 S3 버킷", "S3 → 왼쪽 메뉴 'Vector buckets'"),
    "AWS::S3Vectors::Index": ("벡터 인덱스 (512차원·코사인)", "S3 → Vector buckets → 버킷 → Indexes"),
    "AWS::IAM::Role": ("서비스가 대신 일할 때 쓰는 권한 역할", "IAM → 역할(Roles)"),
    "AWS::Bedrock::KnowledgeBase": ("지식 기반 (문서 검색 + 답변)", "Amazon Bedrock → Knowledge Bases"),
    "AWS::Bedrock::DataSource": ("지식 기반의 원본 (S3 팀 버킷 kb/)", "Knowledge Base 상세 → Data source"),
    "AWS::DynamoDB::Table": ("추천 기록 저장 테이블", "DynamoDB → 테이블 → 항목 탐색"),
    "AWS::Logs::LogGroup": ("Lambda 실행 로그", "CloudWatch → 로그 그룹"),
    "AWS::Lambda::Function": ("에이전트 도구 함수", "Lambda → 함수"),
    "AWS::CloudWatch::Dashboard": ("관측 대시보드", "CloudWatch → 대시보드"),
}


def _cfn():
    from .bedrock import client
    return client("cloudformation")


def status(stack_name):
    """스택 상태 문자열 (없으면 None)"""
    cfn = _cfn()
    try:
        return cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError:
        return None


def deploy(stack_name, template_path, params: dict):
    """스택이 없으면 만들고 완료까지 기다립니다. 이미 있으면 그대로 씁니다. 반환: Outputs dict"""
    if config.BEDROCK_MOCK:
        print("모의 모드: 배포를 건너뜁니다.")
        return mock_outputs(stack_name)
    cfn = _cfn()
    st = status(stack_name)
    if st is None:
        t0 = time.time()
        cfn.create_stack(StackName=stack_name, TemplateBody=open(template_path, encoding="utf-8").read(),
                         Capabilities=["CAPABILITY_NAMED_IAM"],
                         Parameters=[{"ParameterKey": k, "ParameterValue": str(v)} for k, v in params.items()])
        print("⏳ 배포 중… (3~5분) 콘솔 CloudFormation → 스택 → '이벤트' 탭에서 진행 상황을 볼 수 있습니다.")
        try:
            cfn.get_waiter("stack_create_complete").wait(StackName=stack_name, WaiterConfig={"Delay": 10, "MaxAttempts": 90})
        except Exception:
            print("❌ 배포 실패. 원인(첫 실패 이벤트):")
            for e in cfn.describe_stack_events(StackName=stack_name)["StackEvents"][::-1]:
                if e["ResourceStatus"].endswith("FAILED"):
                    print("  ", e["LogicalResourceId"], "→", e.get("ResourceStatusReason", "")[:300])
                    break
            raise
        print(f"✅ 배포 완료 ({time.time() - t0:.0f}초)")
    elif st in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        print("이미 배포된 스택을 그대로 사용합니다:", stack_name)
    else:
        raise RuntimeError(f"스택 상태가 {st} 입니다. 실패 상태면 정리(delete) 후 다시 배포하세요.")
    return outputs(stack_name)


def outputs(stack_name):
    """스택 Outputs → dict"""
    if config.BEDROCK_MOCK:
        return mock_outputs(stack_name)
    s = _cfn().describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])}


def mock_outputs(stack_name):
    team = stack_name.replace("kosa-week6-", "")
    return {"KnowledgeBaseId": "MOCKKB0001", "DataSourceId": "MOCKDS0001", "ToolsFunctionName": f"kosa-w6-{team}-tools",
            "PlansTableName": f"kosa-w6-{team}-plans", "DashboardName": f"kosa-w6-{team}",
            "VectorBucketName": f"kosa-w6-{team}-vec-000000000000", "DataBucketName": f"kosa-kb-{team}-000000000000"}


def template_resources(template_path):
    """템플릿에 적힌 자원 목록 (배포 전 미리 보기) → [(논리 이름, 종류, 설명)]"""
    import re
    text = open(template_path, encoding="utf-8").read()
    body = text.split("\nResources:", 1)[1].split("\nOutputs:", 1)[0]
    found = re.findall(r"^  (\w+):\n    Type: (\S+)", body, re.M)
    return [(name, typ, RESOURCE_GUIDE.get(typ, ("", ""))[0]) for name, typ in found]


def resources(stack_name):
    """배포된 실제 자원 → [{종류, 이름(실제 ID), 설명, 콘솔}]"""
    if config.BEDROCK_MOCK:
        return []
    out = []
    for r in _cfn().describe_stack_resources(StackName=stack_name)["StackResources"]:
        desc, where = RESOURCE_GUIDE.get(r["ResourceType"], ("", ""))
        out.append({"종류": r["ResourceType"], "실제 이름/ID": r["PhysicalResourceId"][-60:], "역할": desc, "콘솔에서": where})
    return out


def delete(stack_name):
    """스택과 그 안의 자원을 모두 삭제 (Knowledge Base·벡터·Lambda·DynamoDB·역할·대시보드)"""
    if config.BEDROCK_MOCK:
        print("모의 모드: 삭제를 건너뜁니다.")
        return
    cfn = _cfn()
    if status(stack_name) is None:
        print("이미 없는 스택입니다:", stack_name)
        return
    cfn.delete_stack(StackName=stack_name)
    print("⏳ 삭제 중… (2~4분)")
    cfn.get_waiter("stack_delete_complete").wait(StackName=stack_name, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    print("✅ 삭제 완료:", stack_name)


def console(kind, name="", region=None):
    """자주 쓰는 콘솔 주소"""
    r = region or config.AWS_REGION
    base = f"https://{r}.console.aws.amazon.com"
    return {
        "cloudformation": f"{base}/cloudformation/home?region={r}#/stacks",
        "kb": f"{base}/bedrock/home?region={r}#/knowledge-bases",
        "lambda": f"{base}/lambda/home?region={r}#/functions/{name}",
        "dynamodb": f"{base}/dynamodbv2/home?region={r}#item-explorer?table={name}",
        "dashboard": f"{base}/cloudwatch/home?region={r}#dashboards/dashboard/{name}",
        "logs": f"{base}/cloudwatch/home?region={r}#logsV2:log-groups",
        "guardrails": f"{base}/bedrock/home?region={r}#/guardrails",
        "agentcore": f"{base}/bedrock-agentcore/home?region={r}#/",
    }[kind]


# ---------------------------------------------------------------- 권한 점검 · 상태 저장
def precheck():
    """오늘 쓰는 AWS 서비스를 하나씩 '조회'만 해 보고 ✅/❌ 를 출력합니다 (자원을 만들지 않음)"""
    if config.BEDROCK_MOCK:
        print("모의 모드: 권한 점검을 건너뜁니다.")
        return {}
    from .bedrock import client, explain
    checks = [
        ("S3 Vectors (벡터 저장소)", "s3vectors", lambda c: c.list_vector_buckets(maxResults=1)),
        ("Bedrock Knowledge Bases", "bedrock-agent", lambda c: c.list_knowledge_bases(maxResults=1)),
        ("Bedrock Guardrails", "bedrock", lambda c: c.list_guardrails(maxResults=1)),
        ("AWS Lambda", "lambda", lambda c: c.list_functions(MaxItems=1)),
        ("Amazon DynamoDB", "dynamodb", lambda c: c.list_tables(Limit=1)),
        ("AWS CloudFormation", "cloudformation", lambda c: c.list_stacks()),
        ("Amazon CloudWatch", "cloudwatch", lambda c: c.list_dashboards()),
        ("CloudWatch Logs", "logs", lambda c: c.describe_log_groups(limit=1)),
    ]
    out = {}
    for label, svc, fn in checks:
        try:
            fn(client(svc))
            out[label] = "✅"
        except Exception as e:  # noqa: BLE001
            if "Unknown service" in str(e):
                out[label] = "❌ boto3가 오래됨 → 터미널에서 pip install -U boto3 후 커널 재시작"
            else:
                out[label] = "❌ " + (explain(e) or str(e)[:120])
        print(f"{out[label][:1]} {label:28s} {out[label][2:]}")
    return out


def save_state(**kw):
    """여러 노트북·앱이 함께 쓰는 값(팀, 스택, 가드레일 ID)을 outputs/week6_state.json 에 저장"""
    import json
    path = config.OUTPUTS / "week6_state.json"
    state = json.load(open(path, encoding="utf-8")) if path.exists() else {}
    state.update(kw)
    json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return state


def load_state():
    import json
    path = config.OUTPUTS / "week6_state.json"
    return json.load(open(path, encoding="utf-8")) if path.exists() else {}
