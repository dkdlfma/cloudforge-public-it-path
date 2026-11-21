"""팀 PostgreSQL 접속 설정. 비밀은 과정 환경 또는 Secrets Manager에서 읽습니다."""

import json
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-northeast-2"


def _load_secrets(secret_arn):
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    if not re.fullmatch(rf"arn:aws:secretsmanager:{REGION}:\d{{12}}:secret:[^\s]+", secret_arn):
        raise ValueError("RDS_SECRET_ARN은 서울 리전 Secrets Manager 비밀 ARN이어야 합니다.")

    try:
        aws = boto3.Session(profile_name=os.getenv("AWS_PROFILE") or None, region_name=REGION)
        client = aws.client("secretsmanager")
        payload = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])
    except (BotoCoreError, ClientError, KeyError, ValueError):
        raise ValueError("RDS 비밀을 읽지 못했습니다. 팀 프로필과 비밀 조회 권한을 확인하세요.") from None
    if not isinstance(payload, dict) or not all(isinstance(payload.get(key), str) and payload[key]
                                               for key in ("username", "password")):
        raise ValueError("RDS 비밀에 username과 password가 필요합니다.")
    return payload["username"], payload["password"]


def connection_options():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    host, user, password = (os.getenv(key) for key in ("RDS_HOST", "RDS_USER", "RDS_PASSWORD"))
    if not host:
        raise ValueError("팀 PostgreSQL 주소를 RDS_HOST에 설정하세요.")

    secret = os.getenv("RDS_SECRET_ARN")
    if secret and not user and not password:
        user, password = _load_secrets(secret)
    if not user or not password:
        raise ValueError("RDS_USER, RDS_PASSWORD 또는 RDS_SECRET_ARN을 설정하세요.")

    options = dict(host=host, port=int(os.getenv("RDS_PORT") or "5432"),
                   dbname=os.getenv("RDS_DB") or "aws_edu", user=user, password=password,
                   connect_timeout=5, sslmode=os.getenv("RDS_SSLMODE") or "require")
    cert = os.getenv("RDS_SSLROOTCERT")
    if cert:
        options["sslrootcert"] = cert
    return options
