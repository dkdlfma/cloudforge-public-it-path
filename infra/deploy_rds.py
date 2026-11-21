"""team04-kang RDS 배포와 상태 조회. 스택 출력에는 접속 정보와 시크릿 ARN이 표시됩니다.

  python infra/deploy_rds.py --account-id <팀계정번호>
  python infra/deploy_rds.py --account-id <팀계정번호> --apply \
    --subnet-ids <프라이빗서브넷A> <프라이빗서브넷B> --security-group-id <DB보안그룹>
"""

import argparse
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError, WaiterError


REGION = "ap-northeast-2"
TEAM = "team04-kang"
STACK = f"{TEAM}-rds"
TEMPLATE = Path(__file__).with_name("rds-stack.yaml")
PORT = 5432
TAGS = {"Team": TEAM, "Project": "aws_edu"}
UPDATEABLE = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE",
              "IMPORT_ROLLBACK_COMPLETE"}


def describe_stack(cfn):
    try:
        return cfn.describe_stacks(StackName=STACK)["Stacks"][0]
    except ClientError as error:
        response = error.response["Error"]
        if response["Code"] == "ValidationError" and "does not exist" in response["Message"].lower():
            return None
        raise


def check_network(ec2, subnet_ids, security_group_id):
    subnets = ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]
    if {subnet["SubnetId"] for subnet in subnets} != set(subnet_ids):
        raise ValueError("선택한 서브넷을 모두 조회할 수 없습니다.")
    vpcs = {subnet["VpcId"] for subnet in subnets}
    azs = {subnet["AvailabilityZone"] for subnet in subnets}
    if len(vpcs) != 1 or len(azs) < 2:
        raise ValueError("동일 VPC에서 가용 영역이 다른 프라이빗 서브넷을 두 개 이상 지정하세요.")
    if any(subnet["State"] != "available" or subnet["MapPublicIpOnLaunch"] or subnet.get("Ipv6Native")
           for subnet in subnets):
        raise ValueError("퍼블릭 IP 자동 할당이 꺼진 사용 가능한 IPv4 프라이빗 서브넷을 지정하세요.")
    vpc_id, = vpcs
    check_private_routes(ec2, vpc_id, subnet_ids)
    group = ec2.describe_security_groups(GroupIds=[security_group_id])["SecurityGroups"][0]
    if group["VpcId"] != vpc_id:
        raise ValueError("DB 보안 그룹과 서브넷은 같은 VPC에 있어야 합니다.")

    postgres_rules = [rule for rule in group["IpPermissions"] if allows_postgres(rule)]
    for rule in postgres_rules:
        if (any(item["CidrIp"] == "0.0.0.0/0" for item in rule.get("IpRanges", []))
                or any(item["CidrIpv6"] == "::/0" for item in rule.get("Ipv6Ranges", []))):
            raise ValueError("DB 보안 그룹의 5432 인바운드는 승인된 클라이언트로 제한해야 합니다.")
    if not postgres_rules:
        raise ValueError("DB 보안 그룹에 승인된 클라이언트에서 오는 TCP 5432 인바운드가 필요합니다.")
    return azs


def check_private_routes(ec2, vpc_id, subnet_ids):
    paginator = ec2.get_paginator("describe_route_tables")
    route_tables = paginator.paginate(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    main_table = None
    by_subnet = {}
    for table in route_tables.search("RouteTables[]"):
        for association in table.get("Associations", []):
            if association.get("Main"):
                main_table = table
            elif association.get("SubnetId"):
                by_subnet[association["SubnetId"]] = table
    for subnet_id in subnet_ids:
        table = by_subnet.get(subnet_id, main_table)
        if not table:
            raise ValueError("서브넷의 라우팅 테이블을 확인할 수 없습니다.")
        if any(route.get("GatewayId", "").startswith("igw-") and route["State"] == "active"
               for route in table["Routes"]):
            raise ValueError("인터넷 게이트웨이로 직접 라우팅하지 않는 프라이빗 서브넷을 지정하세요.")


def allows_postgres(rule):
    protocol = rule["IpProtocol"]
    if protocol == "-1":
        return True
    return protocol in {"tcp", "6"} and rule.get("FromPort", PORT + 1) <= PORT <= rule.get("ToPort", PORT - 1)


def check_orderable(rds, instance_class, availability_zones):
    paginator = rds.get_paginator("describe_orderable_db_instance_options")
    for page in paginator.paginate(Engine="postgres", DBInstanceClass=instance_class, Vpc=True):
        for option in page["OrderableDBInstanceOptions"]:
            option_zones = {zone["Name"] for zone in option["AvailabilityZones"]}
            if (option["EngineVersion"].split(".", 1)[0] == "17" and option["StorageType"] == "gp3"
                    and option["SupportsStorageEncryption"] and option["MinStorageSize"] <= 20 <= option["MaxStorageSize"]
                    and availability_zones & option_zones):
                return
    raise ValueError(f"선택한 서울 리전 가용 영역에서 {instance_class} PostgreSQL 17 gp3 20GiB를 주문할 수 없습니다.")


def check_changes(changes):
    expected = {"DatabaseSubnets", "Database"}
    for change in changes:
        resource = change["ResourceChange"]
        action = resource["Action"]
        if (resource["LogicalResourceId"] not in expected
                or action not in {"Add", "Modify"}
                or resource.get("Replacement") in {"True", "Conditional"}):
            raise ValueError(f"삭제·교체 또는 예상 범위 밖 변경이 포함되어 배포를 중단했습니다 ({resource['LogicalResourceId']}).")


def apply(cfn, stack, parameters):
    status = stack["StackStatus"] if stack else None
    creating = status in {None, "REVIEW_IN_PROGRESS"}
    if not creating:
        tags = {item["Key"]: item["Value"] for item in stack.get("Tags", [])}
        if any(tags.get(key) != value for key, value in TAGS.items()):
            raise ValueError("동일 이름 스택의 팀 또는 프로젝트 태그가 다릅니다.")
        if status not in UPDATEABLE:
            raise ValueError(f"기존 스택의 상태를 먼저 확인하세요 ({status}).")

    name = f"rds-{uuid4().hex}"
    changeset = cfn.create_change_set(
        StackName=STACK, ChangeSetName=name, ClientToken=name,
        ChangeSetType="CREATE" if creating else "UPDATE",
        TemplateBody=TEMPLATE.read_text(encoding="utf-8"), Parameters=parameters,
        Tags=[{"Key": key, "Value": value} for key, value in TAGS.items()],
    )
    change_id = changeset["Id"]
    try:
        cfn.get_waiter("change_set_create_complete").wait(
            ChangeSetName=change_id, WaiterConfig={"Delay": 5, "MaxAttempts": 60},
        )
    except WaiterError as error:
        reason = error.last_response.get("StatusReason", "")
        if not creating and ("didn't contain changes" in reason.lower() or "no updates are to be performed" in reason.lower()):
            cfn.delete_change_set(ChangeSetName=change_id)
            print("스택에 적용할 변경이 없습니다.")
            return describe_stack(cfn)
        raise RuntimeError(f"CloudFormation 변경 세트 생성 중단. {reason or 'AWS 콘솔에서 변경 세트 상태를 확인하세요.'}") from None

    try:
        for page in cfn.get_paginator("describe_change_set").paginate(ChangeSetName=change_id):
            check_changes(page.get("Changes", []))
    except ValueError:
        cfn.delete_change_set(ChangeSetName=change_id)
        raise

    cfn.execute_change_set(ChangeSetName=change_id, ClientRequestToken=name)
    print(f"{STACK} {'생성' if creating else '갱신'} 중입니다. 완료까지 최대 약 30분 확인합니다.", flush=True)
    waiter = "stack_create_complete" if creating else "stack_update_complete"
    try:
        cfn.get_waiter(waiter).wait(StackName=changeset["StackId"], WaiterConfig={"Delay": 15, "MaxAttempts": 120})
    except WaiterError as error:
        observed = error.last_response.get("Stacks", [])
        status = observed[0].get("StackStatus", "알 수 없음") if observed else "알 수 없음"
        raise RuntimeError(f"완료를 확인하지 못했습니다 ({status}). 같은 명령에서 --apply 없이 상태를 확인하세요.") from None
    return describe_stack(cfn)


def print_status(stack):
    print(f"{STACK} ({REGION}) {stack['StackStatus'] if stack else '없음'}")
    if not stack:
        return
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
    for output, variable in (("DbEndpoint", "RDS_HOST"), ("DbPort", "RDS_PORT"), ("DbName", "RDS_DB"),
                             ("DbSecretArn", "RDS_SECRET_ARN")):
        if output in outputs:
            print(f"{variable}={outputs[output]}")


def main():
    import boto3
    from dotenv import load_dotenv

    load_dotenv(TEMPLATE.parents[1] / ".env")
    parser = argparse.ArgumentParser(description="서울 리전 team04-kang RDS 스택 관리")
    parser.add_argument("--profile", default=os.getenv("AWS_PROFILE") or "kosa", help="팀 AWS SSO 프로필")
    parser.add_argument("--account-id", required=True, help="승인된 팀 AWS 계정 번호")
    parser.add_argument("--apply", action="store_true", help="스택 생성 또는 갱신. 생략하면 상태만 조회")
    parser.add_argument("--subnet-ids", nargs="+", help="팀 VPC 프라이빗 서브넷 ID 두 개 이상")
    parser.add_argument("--security-group-id", help="팀 VPC의 기존 DB 보안 그룹 ID")
    parser.add_argument("--instance-class", choices=("db.t4g.micro", "db.t3.micro"),
                        help="기존 클래스 유지. 새 배포 기본값은 db.t4g.micro")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{12}", args.account_id):
        parser.error("--account-id에 승인된 12자리 팀 계정 번호를 입력하세요.")
    if args.apply and (len(set(args.subnet_ids or [])) < 2 or not args.security_group_id):
        parser.error("--apply에는 서로 다른 --subnet-ids 두 개 이상과 --security-group-id가 필요합니다.")

    try:
        session = boto3.Session(profile_name=args.profile, region_name=REGION)
        if session.client("sts").get_caller_identity()["Account"] != args.account_id:
            raise ValueError("AWS SSO 프로필의 계정이 승인된 팀 계정과 일치하지 않습니다.")
        cfn = session.client("cloudformation")
        stack = describe_stack(cfn)
        if args.apply:
            subnets = sorted(set(args.subnet_ids))
            zones = check_network(session.client("ec2"), subnets, args.security_group_id)
            previous = {item["ParameterKey"]: item["ParameterValue"] for item in (stack or {}).get("Parameters", [])}
            instance_class = args.instance_class or previous.get("DatabaseInstanceClass", "db.t4g.micro")
            check_orderable(session.client("rds"), instance_class, zones)
            values = {"PrivateSubnetIds": ",".join(subnets), "DatabaseSecurityGroupId": args.security_group_id,
                      "DatabaseInstanceClass": instance_class}
            parameters = [{"ParameterKey": key, "ParameterValue": value} for key, value in values.items()]
            stack = apply(cfn, stack, parameters)
        print_status(stack)
        return 0
    except (BotoCoreError, ClientError, OSError, RuntimeError, ValueError) as error:
        print(f"RDS 작업 실패. {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
