"""추천 결과(학습계획) 저장소 — 담당: 김현진 / RDS 테이블: 신성훈
.env 의 PLAN_STORE 로 고릅니다.
  - (기본) local : outputs/plans.json 에 저장 (AWS 없이 동작)
  - rds          : RDS(PostgreSQL) plans 테이블에 저장  ← 성훈님이 테이블·접속정보 만들면 여기만 맞추면 됨
접속정보: .env 의 RDS_HOST, RDS_PORT(5432), RDS_DB, RDS_USER, RDS_PASSWORD  (또는 Secrets Manager — 통합 때 결정)"""
import json
import os

from . import data_io

PLAN_STORE = os.getenv("PLAN_STORE", "local").lower()

# RDS plans 테이블 예시 (성훈님 테이블 정의가 확정되면 그쪽 기준으로 맞춤)
PLANS_DDL = """
CREATE TABLE IF NOT EXISTS plans (
  id          SERIAL PRIMARY KEY,
  student     TEXT NOT NULL,          -- 별칭 (로그인 붙으면 Cognito 사용자 ID)
  saved_at    TIMESTAMP NOT NULL,
  posting_id  TEXT NOT NULL,
  verdict     TEXT,
  top_pick    TEXT,
  total_weeks INTEGER,
  path        JSONB,
  profile     JSONB
);"""


def save(item):
    if PLAN_STORE == "rds":
        return _save_rds(item)
    return _save_local(item)


def _save_local(item):
    path = data_io.OUTPUTS / "plans.json"
    data_io.OUTPUTS.mkdir(exist_ok=True)
    plans = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    plans.append(item)
    path.write_text(json.dumps(plans, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"saved": True, "store": "local", "count": len(plans), "item": item}


def _save_rds(item):
    try:
        import psycopg2   # pip install psycopg2-binary
    except ImportError:
        return {**_save_local(item), "warning": "psycopg2 가 없어 로컬에 저장했어요 (pip install psycopg2-binary)"}
    try:
        conn = psycopg2.connect(host=os.getenv("RDS_HOST"), port=int(os.getenv("RDS_PORT", "5432")),
                                dbname=os.getenv("RDS_DB"), user=os.getenv("RDS_USER"),
                                password=os.getenv("RDS_PASSWORD"), connect_timeout=5)
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO plans (student, saved_at, posting_id, verdict, top_pick, total_weeks, path, profile) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (item["student"], item["saved_at"], item["posting_id"], item["verdict"], item["top_pick"],
                         item["total_weeks"], json.dumps(item["path"], ensure_ascii=False),
                         json.dumps(item["profile"], ensure_ascii=False)))
            new_id = cur.fetchone()[0]
        conn.close()
        return {"saved": True, "store": "rds", "id": new_id, "item": item}
    except Exception as e:   # RDS 장애 시에도 시연이 멈추지 않게 로컬로 대신 저장
        return {**_save_local(item), "warning": f"RDS 저장 실패 → 로컬에 저장: {str(e)[:120]}"}


def list_plans(student):
    """저장한 학습계획 목록 (로컬)"""
    path = data_io.OUTPUTS / "plans.json"
    plans = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return [p for p in plans if p.get("student") == student]
