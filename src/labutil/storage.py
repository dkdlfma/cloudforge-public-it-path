"""팀 S3 버킷에 실습 결과 올리기 (5주차 → 6주차 Knowledge Base 데이터 원본)"""
import re

from . import config


def team_bucket(team: str) -> str:
    """팀 이름 → 버킷 이름 (kosa-kb-<팀>-<계정번호>). 영문 소문자·숫자·하이픈만."""
    if not re.fullmatch(r"[a-z0-9-]{3,20}", team):
        raise ValueError("TEAM은 영문 소문자·숫자·하이픈, 3~20자로 정하세요. 예: team03-lee")
    from .bedrock import client
    account = client("sts").get_caller_identity()["Account"]
    return f"kosa-kb-{team}-{account}"


def upload(team: str, files: dict):
    """files: {S3 키: 로컬 경로} → 버킷이 없으면 만들고 올립니다. 반환: (버킷, 올린 키 목록)"""
    from .bedrock import client
    s3 = client("s3")
    bucket = team_bucket(team)
    try:
        s3.head_bucket(Bucket=bucket)
    except s3.exceptions.ClientError:
        kw = {"Bucket": bucket}
        if config.AWS_REGION != "us-east-1":
            kw["CreateBucketConfiguration"] = {"LocationConstraint": config.AWS_REGION}
        s3.create_bucket(**kw)
        print("🪣 버킷 생성:", bucket)
    keys = []
    for key, path in files.items():
        s3.upload_file(str(path), bucket, key)
        keys.append(key)
    return bucket, keys


def console_url(bucket: str) -> str:
    return f"https://{config.AWS_REGION}.console.aws.amazon.com/s3/buckets/{bucket}?region={config.AWS_REGION}"
