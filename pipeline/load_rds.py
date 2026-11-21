"""분석 스냅샷을 팀 PostgreSQL에 적재합니다.

    python pipeline/load_rds.py --dry-run --as-of 2026-11-17
    python pipeline/load_rds.py --as-of 2026-11-17
    python pipeline/load_rds.py --schema-only

기본 범위는 지정한 날짜의 모집 중 공고입니다. --include-closed는 공고와
역량 이력도 적재하며, 분석 CSV와 active_postings는 같은 모집 중 범위를 씁니다.
학습계획은 독립적으로 보관합니다. --plans로 기존 로컬 plans.json을 병합합니다.
"""

import argparse
from contextlib import closing
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from labutil.rds import connection_options
from pipeline.analyze_demand import COVERAGE_FIELDS, DEMAND_FIELDS, GROUPS, is_ongoing, matches_mode, posting_date, today_kst


PROCESSED = ROOT / "data" / "processed"
SCHEMA = ROOT / "infra" / "rds_schema.sql"
LOAD_LOCK = 2_060_843_110


def json_rows(path):
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def records_by_id(path):
    rows = json_rows(path)
    if not isinstance(rows, list):
        raise ValueError(f"{path.name}: 공고 객체 배열이 필요합니다.")
    records = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError(f"{path.name}: id가 있는 공고 객체가 필요합니다.")
        if row["id"] in records:
            raise ValueError(f"{path.name}: 중복 공고 ID {row['id']}")
        records[row["id"]] = row
    return records


def json_value(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def load_postings(data_dir, postings_file, as_of, include_closed):
    postings = records_by_id(postings_file)
    extracts = {pid: row for pid, row in records_by_id(data_dir / "extractions.json").items()
                if matches_mode(row, "bedrock")}
    if not postings:
        raise ValueError(f"{postings_file.name}: 적재할 공고가 없습니다.")
    active_ids = {pid for pid, row in postings.items() if is_ongoing(row, as_of)}
    posting_rows, extraction_rows, competency_rows = [], [], {}
    for pid, row in postings.items():
        if not include_closed and pid not in active_ids:
            continue
        posting_rows.append((
            pid, row.get("source"), row.get("company"), row.get("title"), json_value(row.get("ncs") or []),
            row.get("it_only"), row.get("region"), row.get("hire_type"), row.get("career"), row.get("education"),
            row.get("headcount"), posting_date(row.get("posted_at")), posting_date(row.get("close_at")),
            row.get("ongoing"), as_of, pid in active_ids, row.get("url"), row.get("text"),
        ))
        ext = extracts.get(pid)
        if ext is None:
            continue
        extraction_rows.append((
            pid, json_value(ext.get("job_areas") or []), ext.get("it_relevant"), ext.get("min_experience_years"),
            ext.get("entry_level_ok"), json_value(ext.get("meta") or {}),
        ))
        skills = ext.get("competencies")
        if not isinstance(skills, list):
            raise ValueError(f"{pid}: competencies 배열이 필요합니다.")
        for skill in skills:
            if not isinstance(skill, dict) or any(not isinstance(skill.get(key), str) or not skill[key].strip()
                                                  for key in ("name", "type", "requirement")):
                raise ValueError(f"{pid}: name, type, requirement가 있는 역량 객체가 필요합니다.")
            key = (pid, skill["name"], skill["type"], skill["requirement"], skill.get("position") or "")
            competency_rows.setdefault(key, key + (skill.get("raw_name"), skill.get("evidence")))
    unlinked = sorted(extracts.keys() - postings.keys())
    out_of_scope = len(extracts) - len(extraction_rows) - len(unlinked)
    return posting_rows, extraction_rows, list(competency_rows.values()), active_ids, unlinked, out_of_scope


def number(value, maximum):
    result = Decimal(str(value))
    if not result.is_finite() or abs(result) > maximum:
        raise ValueError(f"[-{maximum}, {maximum}] 범위의 수치가 필요합니다.")
    return result


def load_ncs(data_dir):
    skill_rows, candidate_rows = [], []
    filename = "skill_ncs_map.json"
    mapping = json_rows(data_dir / filename)
    if not isinstance(mapping, dict):
        raise ValueError(f"{filename}: 역량명을 키로 갖는 JSON 객체가 필요합니다.")
    for skill, candidates in mapping.items():
        if not skill.strip() or not isinstance(candidates, list):
            raise ValueError(f"{filename}: 역량명과 NCS 후보 배열이 필요합니다.")
        skill_rows.append((skill,))
        codes = set()
        for rank, item in enumerate(candidates, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"{filename}: {skill}의 NCS 후보는 객체여야 합니다.")
            code, name = item.get("unit_code"), item.get("unit_name")
            if not isinstance(code, str) or not code or code in codes or not isinstance(name, str) or not name:
                raise ValueError(f"{filename}: {skill}의 NCS 후보 코드 또는 이름이 유효하지 않습니다.")
            kind = item.get("competency_type")
            kinds = item.get("competency_types", [kind] if kind else [])
            if not isinstance(kinds, list) or not all(isinstance(value, str) for value in kinds):
                raise ValueError(f"{filename}: {skill}의 competency_types는 문자열 배열이어야 합니다.")
            codes.add(code)
            candidate_rows.append((skill, rank, code, name, number(item.get("score"), 1),
                                   item.get("backend"), item.get("catalog"), kind, json_value(kinds)))
    return skill_rows, candidate_rows


def load_ncs_coverage(data_dir, as_of, active_ids, extracted_ids):
    path = data_dir / "ncs_match_coverage.json"
    if not path.exists():
        return []
    report = json_rows(path)
    if (not isinstance(report, dict) or report.get("as_of") != as_of.isoformat()
            or report.get("include_closed") or report.get("extraction_mode") != "bedrock"):
        raise ValueError(f"{path.name}: 같은 --as-of와 모집 중 범위로 NCS 매핑 결과를 다시 생성하세요.")
    total = report.get("posting_total")
    analyzed = report.get("analyzed_posting_count")
    reported = report.get("postings")
    if (not isinstance(reported, list) or not all(isinstance(row, dict) and isinstance(row.get("id"), str)
                                                  and type(row.get("has_extraction")) is bool for row in reported)):
        raise ValueError(f"{path.name}: 공고별 추출 완료 목록이 필요합니다.")
    if ({row["id"] for row in reported} != active_ids or total != len(active_ids) or len(reported) != total
            or {row["id"] for row in reported if row["has_extraction"]} != extracted_ids
            or analyzed != len(extracted_ids)):
        raise ValueError(f"{path.name}: 모집 중 공고 또는 추출 범위와 일치하지 않습니다.")
    return [(as_of, total, analyzed, json_value(report))]


def csv_rows(path, fields):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(fields).issubset(reader.fieldnames or []):
            raise ValueError(f"{path.name}: 필수 열이 없습니다.")
        yield from reader


def dimension_key(scope, row):
    return scope, row.get("region", ""), row.get("hire_type", "")


def load_demand(data_dir, active_count, analyzed_count):
    coverage, rows = {}, {}
    for row in csv_rows(data_dir / "demand_coverage.csv", COVERAGE_FIELDS):
        key = dimension_key(row["group"], row)
        total, analyzed = int(row["posting_total"]), int(row["analyzed_posting_count"])
        pct = number(row["coverage_pct"], 100)
        if key in coverage or key[0] not in GROUPS or not 0 <= analyzed <= total or pct < 0:
            raise ValueError(f"demand_coverage.csv: 집계 그룹 {key}가 유효하지 않습니다.")
        coverage[key] = total, analyzed, pct
    if coverage.get(("overall", "", ""), ())[:2] != (active_count, analyzed_count):
        raise ValueError(f"demand_coverage.csv 범위가 모집 중 공고 {active_count:,}건·추출 {analyzed_count:,}건과 일치하지 않습니다. "
                         "같은 공고 파일과 --as-of로 수요 CSV를 다시 생성하세요.")
    for scope, (filename, dimensions) in GROUPS.items():
        for row in csv_rows(data_dir / filename, (*dimensions, *DEMAND_FIELDS)):
            group = dimension_key(scope, row)
            key = (*group, row["skill"], row["type"])
            count, required, alternative, preferred, analyzed, total = (int(row[field]) for field in DEMAND_FIELDS[2:-1])
            pct = number(row["demand_pct"], 100)
            if key in rows or group not in coverage or coverage[group][:2] != (total, analyzed):
                raise ValueError(f"{filename}: 집계 범위나 분석 건수가 중복 또는 불일치합니다: {key}")
            if not row["skill"] or not row["type"] or not (
                0 <= count <= analyzed and all(0 <= n <= count for n in (required, alternative, preferred)) and pct >= 0
            ):
                raise ValueError(f"{filename}: 역량명·역량 유형·수요 건수 또는 수요율이 유효하지 않습니다: {key}")
            rows[key] = (*key, count, required, alternative, preferred, analyzed, total, pct)
    return [(*key, *values) for key, values in coverage.items()], list(rows.values())


def load_plans(path):
    if path is None:
        return []
    items = json_rows(path)
    if not isinstance(items, list):
        raise ValueError("학습계획 JSON은 배열이어야 합니다.")
    plans = {}
    for item in items:
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip()
                                             for key in ("student", "saved_at", "posting_id")):
            raise ValueError("학습계획에 student, saved_at, posting_id가 필요합니다.")
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        key = hashlib.sha256(encoded).hexdigest()
        plans[key] = (item["student"], datetime.fromisoformat(item["saved_at"]), item["posting_id"],
                      item.get("verdict"), item.get("top_pick"), item.get("total_weeks"),
                      json_value(item.get("path")), json_value(item.get("profile")), key)
    return list(plans.values())


def apply_snapshot(cursor, batches, plans, write_rows):
    # 외래 키의 CASCADE로 이전 스냅샷의 하위 행도 같은 트랜잭션에서 교체됩니다.
    if batches is not None:
        for table in ("postings", "ncs_map_skills", "ncs_match_coverage", "demand_coverage"):
            cursor.execute(f"DELETE FROM {table}")
        for statement, rows in batches:
            if rows:
                write_rows(cursor, statement, rows, page_size=200)
    if plans:
        write_rows(cursor, """INSERT INTO plans
            (student, saved_at, posting_id, verdict, top_pick, total_weeks, path, profile, import_key)
            VALUES %s ON CONFLICT (import_key) DO NOTHING""", plans, page_size=200)


def make_batches(postings, extractions, competencies, skills, ncs, ncs_coverage, coverage, demand):
    return [
        ("""INSERT INTO postings
            (id, source, company, title, ncs, it_only, region, hire_type, career, education, headcount,
             posted_at, close_at, source_ongoing, active_as_of, is_active, url, body) VALUES %s""", postings),
        ("""INSERT INTO posting_extractions
            (posting_id, job_areas, it_relevant, min_experience_years, entry_level_ok, meta) VALUES %s""", extractions),
        ("""INSERT INTO competencies
            (posting_id, name, competency_type, requirement, position, raw_name, evidence) VALUES %s""", competencies),
        ("INSERT INTO ncs_map_skills (skill) VALUES %s", skills),
        ("""INSERT INTO ncs_map
            (skill, rank, unit_code, unit_name, score, backend, catalog, competency_type, competency_types) VALUES %s""", ncs),
        ("""INSERT INTO ncs_match_coverage
            (as_of, posting_total, analyzed_posting_count, report) VALUES %s""", ncs_coverage),
        ("""INSERT INTO demand_coverage
            (scope, region, hire_type, posting_total, analyzed_posting_count, coverage_pct) VALUES %s""", coverage),
        ("""INSERT INTO demand
            (scope, region, hire_type, skill, competency_type, posting_count, required_posting_count,
             alternative_required_posting_count, preferred_posting_count, analyzed_posting_count,
             posting_total, demand_pct) VALUES %s""", demand),
    ]


def main():
    args = argparse.ArgumentParser(description="팀 PostgreSQL 스키마와 분석 스냅샷 적재")
    args.add_argument("--data-dir", type=Path, default=PROCESSED, help="팀 JSON·CSV가 있는 폴더")
    args.add_argument("--postings", type=Path, help="개별 공고 JSON 경로 (기본: --data-dir 아래 postings_jobalio.json)")
    args.add_argument("--as-of", type=date.fromisoformat, default=today_kst(), help="모집 중 판정 기준일 (KST)")
    args.add_argument("--include-closed", action="store_true", help="마감 공고와 그 역량 이력도 적재")
    args.add_argument("--plans", type=Path, help="병합할 로컬 학습계획 JSON")
    mode = args.add_mutually_exclusive_group()
    mode.add_argument("--schema-only", action="store_true", help="테이블과 뷰만 준비")
    mode.add_argument("--dry-run", action="store_true", help="DB/AWS 연결 없이 입력 확인")
    options = args.parse_args()
    if options.schema_only and options.plans:
        args.error("--schema-only와 --plans를 함께 사용할 수 없습니다.")

    try:
        plans = load_plans(options.plans)
        batches = None
        if not options.schema_only:
            posting_path = options.postings or options.data_dir / "postings_jobalio.json"
            postings, extracts, comps, active_ids, unlinked, outside = load_postings(
                options.data_dir, posting_path, options.as_of, options.include_closed,
            )
            skills, ncs = load_ncs(options.data_dir)
            extracted_active_ids = {row[0] for row in extracts} & active_ids
            active = len(active_ids)
            ncs_coverage = load_ncs_coverage(options.data_dir, options.as_of, active_ids, extracted_active_ids)
            coverage, demand = load_demand(options.data_dir, active, len(extracted_active_ids))
            batches = make_batches(postings, extracts, comps, skills, ncs, ncs_coverage, coverage, demand)
            print(f"{options.as_of} 기준 모집 중 {active:,}건 / 공고 적재 {len(postings):,}건 / 역량 {len(comps):,}행")
            print(f"추출 {len(extracts):,}건 / NCS 후보 {len(ncs):,}행 / 수요 {len(demand):,}행 / 학습계획 병합 {len(plans):,}건")
            if unlinked:
                print(f"공고 ID가 연결되지 않은 추출 {len(unlinked):,}건 제외 ({', '.join(unlinked[:8])})")
            if outside:
                print(f"공고 범위 밖 추출 {outside:,}건 제외")
        if options.dry_run:
            print("입력 검증 완료")
            return 0
        ddl = SCHEMA.read_text(encoding="utf-8")
        connection = connection_options()
    except (ImportError, OSError, ValueError, InvalidOperation, TypeError) as error:
        args.error(str(error))

    try:
        import psycopg2
        from psycopg2.extras import execute_values
    except ImportError:
        args.error("PostgreSQL 연결은 psycopg2-binary가 필요합니다.")
    try:
        with closing(psycopg2.connect(**connection)) as conn:
            with conn, conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LOAD_LOCK,))
                cursor.execute(ddl)
                apply_snapshot(cursor, batches, plans, execute_values)
    except psycopg2.Error as error:
        # 서버 오류 메시지에는 연결 정보나 행의 민감한 내용이 포함될 수 있습니다.
        sqlstate = error.pgcode or type(error).__name__
        args.exit(1, f"PostgreSQL 적재 실패 ({sqlstate}). 연결 설정, 권한, 입력 스키마를 확인하세요.\n")
    print("PostgreSQL 스키마 적용 완료" if options.schema_only else "PostgreSQL 스냅샷 적재 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
