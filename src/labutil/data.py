"""실습 데이터 불러오기 — 이전 주차 결과가 없어도 샘플로 다시 만들어 줍니다"""
import json

import pandas as pd

from . import config


def load_postings():
    """정제된 채용공고 100건 (4주차 01 노트북 결과). 없으면 샘플에서 다시 만듭니다."""
    path = config.OUTPUTS / "postings_clean.json"
    if path.exists():
        return pd.read_json(path)
    from . import goyong24
    from .textprep import clean
    raw = pd.DataFrame(goyong24.load_sample())
    df = raw.rename(columns={"wantedAuthNo": "id", "indTpNm": "industry", "jobsNm": "role", "regDt": "posted_at", "jobCont": "text"})
    df["posted_at"] = pd.to_datetime(df["posted_at"], format="%Y%m%d")
    df["text"] = df["text"].map(clean)
    df = df[["id", "company", "title", "industry", "role", "career", "region", "posted_at", "text"]]
    df.to_json(path, orient="records", force_ascii=False, date_format="iso", indent=1)
    print("ℹ outputs/postings_clean.json 이 없어 샘플에서 다시 만들었습니다.")
    return df


def load_ncs_units():
    """실습용 NCS 능력단위 샘플 (실제 NCS 코드와 다름)"""
    return pd.DataFrame(json.load(open(config.DATA / "ncs" / "ncs_units_sample.json", encoding="utf-8")))


def load_courses():
    """NCS 교육과정 API 형식의 대학 교과목 샘플"""
    return pd.DataFrame(json.load(open(config.DATA / "ncs" / "ncs_courses_sample.json", encoding="utf-8"))["data"])


def load_match_gold():
    """기술 → 정답 NCS 능력단위 코드 (Top-5 검증용)"""
    return json.load(open(config.DATA / "gold" / "skill_ncs_gold.json", encoding="utf-8"))
