"""잡알리오 공고와 역량 추출 결과로 공고 단위의 수요를 집계합니다.

입력: data/processed/postings_jobalio.json, data/processed/extractions.json
출력: demand_{overall,by_region,by_hire_type,by_region_hire_type,coverage}.csv
실행: python pipeline/analyze_demand.py

기본 집계 대상은 한국 날짜 기준 진행 중인 공고의 Bedrock 추출 결과입니다.
마감일 당일까지 진행 중으로 보고, 마감일이 없거나 잘못되었으면 공고의 ongoing을 사용합니다.
--as-of YYYY-MM-DD로 기준일을 지정하고, --include-closed로 마감 공고를 포함할 수 있습니다.
수요율은 해당 그룹 내 집계 대상 추출 공고 중 그 역량이 나온 공고의 비율입니다.
역량이 없는 추출 결과도 분모에 들어갑니다. 복수 지역·고용형태는 항목마다
집계하므로 그룹 간 합은 전체 공고 수와 다를 수 있습니다. 교차표는 공고에 함께
표기된 조합을 집계합니다. 필수·선택필수·우대는 공고 안에서 중복될 수 있습니다.
"""

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


PROCESSED = Path(__file__).resolve().parents[1] / "data" / "processed"
DEFAULT_MODE = "bedrock"
KST = timezone(timedelta(hours=9))
GROUPS = {
    "overall": ("demand_overall.csv", ()),
    "region": ("demand_by_region.csv", ("region",)),
    "hire_type": ("demand_by_hire_type.csv", ("hire_type",)),
    "region_hire_type": ("demand_by_region_hire_type.csv", ("region", "hire_type")),
}
DEMAND_FIELDS = (
    "skill", "type", "posting_count", "required_posting_count",
    "alternative_required_posting_count", "preferred_posting_count",
    "analyzed_posting_count", "posting_total", "demand_pct",
)
COVERAGE_FIELDS = (
    "group", "region", "hire_type", "posting_total", "analyzed_posting_count", "coverage_pct",
)


@dataclass
class GroupDemand:
    posting_total: int = 0
    analyzed_posting_count: int = 0
    skill_counts: Counter = field(default_factory=Counter)
    requirement_counts: Counter = field(default_factory=Counter)

    def add_extraction(self, skills):
        self.analyzed_posting_count += 1
        self.skill_counts.update(skills.keys())
        for (name, kind), requirements in skills.items():
            self.requirement_counts.update((name, kind, req) for req in requirements)


def load_by_id(path):
    with path.open(encoding="utf-8-sig") as stream:
        rows = json.load(stream)
    if not isinstance(rows, list):
        raise ValueError(f"{path}: JSON 공고 배열이 필요합니다.")

    by_id = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError(f"{path}: {index}번째 행에 공고 id가 없습니다.")
        if row["id"] in by_id:
            raise ValueError(f"{path}: 공고 id {row['id']}가 중복됩니다.")
        by_id[row["id"]] = row
    return by_id


def labels(value):
    raw = value if isinstance(value, list) else str(value or "").split(",")
    cleaned = {str(label).strip() for label in raw if label is not None}
    return sorted(label for label in cleaned if label.lower() not in {"", "null", "none"}) or ["미상"]


def posting_groups(posting):
    regions = labels(posting.get("region"))
    hire_types = labels(posting.get("hire_type"))
    yield "overall", "", ""
    for region in regions:
        yield "region", region, ""
        for hire_type in hire_types:
            yield "region_hire_type", region, hire_type
    for hire_type in hire_types:
        yield "hire_type", "", hire_type


def skills_in(extraction):
    competencies = extraction.get("competencies")
    if not isinstance(competencies, list):
        raise ValueError(f"공고 {extraction['id']}: competencies가 배열이어야 합니다.")

    skills = defaultdict(set)
    for competency in competencies:
        if not isinstance(competency, dict):
            raise ValueError(f"공고 {extraction['id']}: competencies에 객체가 아닌 항목이 있습니다.")
        name, kind, req = (competency.get(key) for key in ("name", "type", "requirement"))
        if not all(isinstance(value, str) and value.strip() for value in (name, kind, req)):
            raise ValueError(f"공고 {extraction['id']}: 역량의 name, type, requirement를 확인하세요.")
        skills[" ".join(name.split()), kind.strip()].add(req.strip())
    return skills


def percentage(numerator, denominator):
    return round(100 * numerator / denominator, 4) if denominator else 0.0


def today_kst():
    return datetime.now(KST).date()


def posting_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def is_ongoing(posting, as_of):
    """원본 공고의 게시일, 마감일, 진행 여부를 기준일에 적용합니다."""
    posted_at = posting_date(posting.get("posted_at"))
    if posted_at is not None and posted_at > as_of:
        return False
    close_at = posting_date(posting.get("close_at"))
    if close_at is not None:
        return close_at >= as_of
    return posting.get("ongoing") is True


def matches_mode(extraction, mode):
    meta = extraction.get("meta")
    return mode == "all" or (isinstance(meta, dict) and meta.get("mode") == mode)


def aggregate(postings, extractions, *, mode=DEFAULT_MODE, it_only=False, include_closed=False, as_of=None):
    as_of = as_of or today_kst()
    groups = defaultdict(GroupDemand, {("overall", "", ""): GroupDemand()})
    for posting_id, posting in postings.items():
        if it_only and posting.get("it_only") is not True:
            continue
        if not include_closed and not is_ongoing(posting, as_of):
            continue

        extraction = extractions.get(posting_id)
        skills = None
        if extraction is not None and matches_mode(extraction, mode):
            skills = skills_in(extraction)

        for key in posting_groups(posting):
            group = groups[key]
            group.posting_total += 1
            if skills is not None:
                group.add_extraction(skills)
    return groups


def demand_rows(groups, group_name):
    for (name, region, hire_type), group in sorted(groups.items()):
        if name != group_name:
            continue
        by_count = sorted(group.skill_counts.items(), key=lambda item: (-item[1], item[0]))
        for (skill, kind), count in by_count:
            yield {
                "region": region,
                "hire_type": hire_type,
                "skill": skill,
                "type": kind,
                "posting_count": count,
                "required_posting_count": group.requirement_counts[skill, kind, "필수"],
                "alternative_required_posting_count": group.requirement_counts[skill, kind, "선택필수"],
                "preferred_posting_count": group.requirement_counts[skill, kind, "우대"],
                "analyzed_posting_count": group.analyzed_posting_count,
                "posting_total": group.posting_total,
                "demand_pct": percentage(count, group.analyzed_posting_count),
            }


def coverage_rows(groups):
    for (group_name, region, hire_type), group in sorted(groups.items()):
        yield {
            "group": group_name,
            "region": region,
            "hire_type": hire_type,
            "posting_total": group.posting_total,
            "analyzed_posting_count": group.analyzed_posting_count,
            "coverage_pct": percentage(group.analyzed_posting_count, group.posting_total),
        }


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_results(output_dir, groups):
    output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output_dir) as temporary:
        staging = Path(temporary)
        for group_name, (filename, dimensions) in GROUPS.items():
            write_csv(staging / filename, dimensions + DEMAND_FIELDS, demand_rows(groups, group_name))
        write_csv(staging / "demand_coverage.csv", COVERAGE_FIELDS, coverage_rows(groups))
        for path in sorted(staging.iterdir()):
            path.replace(output_dir / path.name)


def main():
    parser = argparse.ArgumentParser(description="잡알리오 역량 수요와 추출 범위 집계")
    parser.add_argument("--postings", type=Path, default=PROCESSED / "postings_jobalio.json",
                        help="잡알리오 공고 JSON")
    parser.add_argument("--extractions", type=Path, default=PROCESSED / "extractions.json",
                        help="역량 추출 JSON")
    parser.add_argument("--output-dir", type=Path, default=PROCESSED, help="수요 CSV 저장 폴더")
    parser.add_argument("--mode", choices=("bedrock", "mock", "all"), default=DEFAULT_MODE,
                        help="집계에 포함할 추출 모드 (기본 bedrock)")
    parser.add_argument("--it-only", action="store_true", help="정보통신 단독 공고만 집계")
    parser.add_argument("--include-closed", action="store_true", help="진행 여부와 관계없이 전체 공고 집계")
    parser.add_argument("--as-of", type=date.fromisoformat, default=today_kst(), metavar="YYYY-MM-DD",
                        help="진행 여부 판단 기준일 (기본 한국 날짜의 오늘)")
    args = parser.parse_args()

    try:
        postings = load_by_id(args.postings)
        extractions = load_by_id(args.extractions)
        groups = aggregate(postings, extractions, mode=args.mode, it_only=args.it_only,
                           include_closed=args.include_closed, as_of=args.as_of)
        write_results(args.output_dir, groups)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    missing_count = sum(matches_mode(extractions[posting_id], args.mode)
                        for posting_id in extractions.keys() - postings.keys())
    if missing_count:
        print(f"공고 JSON과 연결되지 않은 추출 결과 {missing_count:,}건을 제외했습니다.")
    overall = groups["overall", "", ""]
    scope = "전체 공고" if args.include_closed else f"진행 중 공고 (한국 날짜 {args.as_of})"
    print(f"{scope} · 추출 모드 {args.mode}")
    print(f"추출 완료 공고 {overall.analyzed_posting_count:,}건 / 대상 공고 {overall.posting_total:,}건")
    print(f"수요 CSV 5개 저장: {args.output_dir}")


if __name__ == "__main__":
    main()
