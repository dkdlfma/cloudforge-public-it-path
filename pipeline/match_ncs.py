"""자격증, 기술, 직무지식에서 NCS 능력단위 후보를 검색합니다.

기본 카탈로그는 match_ncs_catalog.py가 만든 공식 정보통신 능력단위 JSON입니다.
기본 실행은 진행 중 공고의 Bedrock 추출본과 KOSA 로컬 코사인 검색을 사용합니다.
AWS에서는 --create-index, --seed-index, --check-index, --live 순서로 사용할 수 있습니다.
작은 실호출 테스트에는 --unit-code로 1~2개 능력단위를 골라 적재하고 검색합니다.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import date
from numbers import Real
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__:
    from .analyze_demand import is_ongoing, load_by_id, matches_mode, today_kst
else:
    from analyze_demand import is_ongoing, load_by_id, matches_mode, today_kst


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
# KOSA 실습 폴더 대신 프로젝트 안의 data/ncs를 사용합니다 (팀원 PC 어디서나 실행되도록).
NCS_DIR = ROOT / "data" / "ncs"
SAMPLE_CATALOG = NCS_DIR / "ncs_units_sample.json"
OFFICIAL_CATALOG = NCS_DIR / "ncs_units_official.json"
TITAN_CACHE = ROOT / "outputs" / "ncs_titan_cache.json"
REGION = "ap-northeast-2"
MODEL_ID = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 512
TOP_K = 5
MIN_SIMILARITY = 0.40
CATALOG_TYPES = ("자격증", "기술", "직무지식")
INDEX_ARN = re.compile(rf"arn:aws:s3vectors:{REGION}:\d{{12}}:bucket/[a-z0-9.-]+/index/[a-z0-9.-]+")

# KOSA 5주차의 확장 질의 방식으로 샘플 카탈로그 안의 관련 분야를 설명합니다.
# 이 사전은 검색 키워드로 쓰이며 자격증과 NCS 간 공식 대응표를 뜻하지 않습니다.
SW = "서버 프로그램 구현, 관계형 데이터베이스 구현과 SQL, 업무 로직과 API 설계 및 테스트"
SECURITY = "정보보안 관리, 정보보안 정책, 접근 통제, 취약점 점검, 침해사고 대응, 권한과 암호화"
DATABASE = "관계형 데이터베이스 구현, SQL로 테이블을 조회하고 인덱스와 트랜잭션을 관리"
ANALYTICS = "데이터 분석 질의와 집계, 데이터 수집·정제, 분석 기획과 통계"
QUERY_DETAILS = {
    "정보처리기사": SW,
    "정보처리산업기사": SW,
    "정보처리기능사": SW,
    "정보보안기사": SECURITY,
    "정보보안산업기사": SECURITY,
    "정보보안기술사": SECURITY,
    "정보통신기사": "통신망 구축, 네트워크 설계와 운영, 통신설비 점검 및 장애 처리",
    "정보통신산업기사": "통신망 구축, 네트워크 설계와 운영, 통신설비 점검 및 장애 처리",
    "정보통신기술사": "통신망 설계와 운영, 정보통신설비 구축 및 유지보수",
    "CISSP": SECURITY,
    "CISM": SECURITY,
    "CISA": "정보시스템 감사, 로그 감사와 정보보안 정책, 접근 통제 점검",
    "SIS": SECURITY,
    "CPPG": "개인정보 보호 관리, 정보보안 정책, 접근 통제, 권한 관리",
    "ISMS-P": "정보보안 관리, 정보보호 및 개인정보보호 관리체계, 정책과 접근 통제",
    "빅데이터분석기사": "대용량 데이터 수집·정제·분산 처리, 데이터 분석 질의, 예측 모델 개발과 검증",
    "SQLD": DATABASE,
    "SQLP": DATABASE + ", 분석 데이터 질의와 집계",
    "OCM": "Oracle " + DATABASE,
    "OCP": "Oracle " + DATABASE,
    "ADP": ANALYTICS + ", 예측 모델 개발 및 검증",
    "ADSP": ANALYTICS,
    "리눅스마스터": "리눅스 시스템 운영, Linux 사용자 권한과 파일 시스템, 서비스 운영",
}

TECH_DETAILS = {
    "DOCKER": "컨테이너 이미지 빌드, 애플리케이션 배포, 컨테이너 플랫폼 운영",
    "KUBERNETES": "컨테이너 오케스트레이션, 통합 배포, 컨테이너 플랫폼 운영과 모니터링",
    "CI/CD": "빌드와 테스트 자동화, 배포 파이프라인, 형상 관리",
    "JAVA": "객체지향 애플리케이션, 서버 프로그램 구현, 인터페이스 구현",
    "SPRING BOOT": "Java 기반 웹 서버, REST API, 서버 프로그램 구현",
    "REACT.JS": "화면 구현, 웹 프론트엔드, 사용자 인터페이스",
    "MYSQL": "SQL, 관계형 데이터베이스 구현, 데이터베이스 운영",
    "MONGODB": "NoSQL 문서 데이터베이스, 데이터 저장과 운영",
    "SQL 튜닝": "SQL 실행 계획, 데이터베이스 성능, 질의 최적화",
    "보안관제": "정보보안 관제, 보안 이벤트 탐지, 침해 사고 분석과 대응",
    "정보시스템 운영·유지관리": "정보시스템 운영, 장애 대응, 운영 모니터링과 유지보수",
}


def read_json(path):
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def catalog_from(path):
    rows = read_json(path)
    if not isinstance(rows, list) or not rows:
        raise ValueError("NCS 카탈로그에는 한 개 이상의 능력단위가 필요합니다.")
    units = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("NCS 카탈로그에는 능력단위 객체 배열이 필요합니다.")
        code, name = (row.get(field) for field in ("unit_code", "unit_name"))
        if not all(isinstance(value, str) and value.strip() for value in (code, name)):
            raise ValueError("각 능력단위에 unit_code와 unit_name이 필요합니다.")
        code, name = code.strip(), name.strip()
        if code in units:
            raise ValueError(f"NCS 카탈로그 코드 중복: {code}")
        desc = row.get("description") or ""
        detail = row.get("detail") or ""
        if not isinstance(desc, str) or not isinstance(detail, str):
            raise ValueError(f"능력단위 {code}의 description과 detail은 문자열이어야 합니다.")
        units[code] = (name, f"{name}. {desc} {detail}".strip())
    return units


def query_for(name, kind, sample_catalog):
    if kind != "자격증":
        details = TECH_DETAILS.get(name.upper())
        return f"{name}. {details}" if details else name
    short = re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()
    short = re.sub(r"\s+(?:[1-9]급|[1-9]등급)$", "", short).strip()
    short = re.sub(r"\s*\([^()]*\)\s*$", "", short).strip()
    key = short.upper()
    if re.search(r"\(SIS\)", name, flags=re.I):
        key = "SIS"
    # Oracle의 OCP/OCM에는 여러 트랙이 있어 트랙이 명시된 SQL/DB 후보만 제시합니다.
    if key in {"OCP", "OCM"} and not re.search(r"database|\bDB\b|데이터베이스", name, re.I):
        return None
    details = QUERY_DETAILS.get(key)
    if details:
        return f"{key} 자격증. {details}"
    if not sample_catalog:
        return f"{name} 자격증"
    return None


def matching_scope(args, as_of):
    extractions = load_by_id(args.extractions)
    if args.include_closed and not args.postings.exists():
        # 이전 ZIP에서도 명시적인 전체 이력 조회를 지원합니다.
        scope = set(extractions)
        unlinked = None
    else:
        postings = load_by_id(args.postings)
        scope = {pid for pid, posting in postings.items() if args.include_closed or is_ongoing(posting, as_of)}
        unlinked = sorted(extractions.keys() - postings.keys())
    if args.posting_id:
        requested = set(args.posting_id)
        if requested - scope:
            missing = ", ".join(sorted(requested - scope))
            raise ValueError(f"검색 범위에 없는 공고: {missing}. 공고 파일 또는 --include-closed 옵션을 확인하세요.")
        scope = requested
    selected = {pid: extractions[pid] for pid in sorted(scope & extractions.keys())
                if matches_mode(extractions[pid], args.extraction_mode)}
    if args.posting_id and scope - selected.keys():
        missing = ", ".join(sorted(scope - selected.keys()))
        raise ValueError(f"선택한 추출 모드에 결과가 없는 공고: {missing}")
    return sorted(scope), selected, unlinked


def competencies_by_posting(rows):
    by_posting = {}
    for posting_id, row in rows.items():
        competencies = row.get("competencies")
        if not isinstance(competencies, list):
            raise ValueError(f"공고 {posting_id}: competencies는 배열이어야 합니다.")
        items = []
        for competency in competencies:
            if not isinstance(competency, dict):
                raise ValueError(f"공고 {posting_id}: competencies는 객체 배열이어야 합니다.")
            name, kind = competency.get("name"), competency.get("type")
            if not all(isinstance(value, str) and value.strip() for value in (name, kind)):
                raise ValueError(f"공고 {posting_id}: 역량의 이름과 유형이 필요합니다.")
            items.append((" ".join(name.split()), kind.strip(), competency.get("requirement")))
        by_posting[posting_id] = items
    return by_posting


def titan_cache_key(text):
    # KOSA labutil.bedrock.embed의 Titan 캐시 키와 동일합니다.
    return hashlib.sha1(f"{MODEL_ID}|{DIMENSIONS}|False|{text}".encode("utf-8")).hexdigest()


def valid_vector(value):
    return (isinstance(value, list) and len(value) == DIMENSIONS and any(value)
            and all(isinstance(number, Real) and math.isfinite(number) for number in value))


def titan_vectors(catalog, cache_path, embed_limit, embed):
    cache = read_json(cache_path) if cache_path.exists() else {}
    if not isinstance(cache, dict):
        raise ValueError("Titan 캐시는 키와 벡터가 있는 JSON 객체여야 합니다.")
    texts = {titan_cache_key(text): text for _, text in catalog.values()}
    missing = [key for key in texts if not valid_vector(cache.get(key))]
    updated = 0
    try:
        for key in missing[:embed_limit]:
            value = embed(texts[key])
            if not valid_vector(value):
                raise ValueError("Titan 응답이 유효한 512차원 벡터가 아닙니다.")
            cache[key] = value
            updated += 1
    finally:
        if updated:
            write_mapping(cache_path, cache, indent=None)
    remaining = len(missing) - updated
    if remaining:
        raise ValueError(f"Titan 벡터 {updated}개를 캐시에 저장했습니다. 아직 {remaining}개가 필요합니다. "
                         "같은 명령을 다시 실행하거나 --unit-code로 실호출 범위를 선택하세요.")
    return {code: cache[titan_cache_key(text)] for code, (_, text) in catalog.items()}


def ensure_index(client, bucket, name):
    locator = {"vectorBucketName": bucket, "indexName": name}
    try:
        index = client.get_index(**locator)["index"]
    except client.exceptions.NotFoundException:
        try:
            return client.create_index(**locator, dataType="float32", dimension=DIMENSIONS,
                                       distanceMetric="cosine")["indexArn"]
        except client.exceptions.ConflictException:
            # 다른 실행이 같은 이름의 인덱스를 먼저 준비한 경우입니다.
            index = client.get_index(**locator)["index"]
    check_index(index)
    return index["indexArn"]


def check_index(index):
    if (index["dataType"], index["dimension"], index["distanceMetric"]) != ("float32", DIMENSIONS, "cosine"):
        raise ValueError("S3 Vectors 인덱스에는 float32, 512차원, cosine 설정이 필요합니다.")
    if "ncs_catalog_id" in index.get("metadataConfiguration", {}).get("nonFilterableMetadataKeys", []):
        raise ValueError("S3 Vectors에서 ncs_catalog_id는 필터링 가능해야 합니다.")


class S3Search:
    def __init__(self, client, index_arn, catalog):
        self.client = client
        self.index_arn = index_arn
        signature = json.dumps(catalog, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.catalog_id = hashlib.sha256(signature).hexdigest()
        self.keys = {f"{self.catalog_id}:{code}": code for code in catalog}
        check_index(self.client.get_index(indexArn=index_arn)["index"])

    def seed(self, vectors):
        items = list(self.keys.items())
        for start in range(0, len(items), 500):
            self.client.put_vectors(
                indexArn=self.index_arn,
                vectors=[{"key": key, "data": {"float32": vectors[code]},
                          "metadata": {"ncs_catalog_id": self.catalog_id}}
                         for key, code in items[start:start + 500]],
            )

    def count_indexed(self):
        keys = list(self.keys)
        indexed = set()
        for start in range(0, len(keys), 100):
            response = self.client.get_vectors(indexArn=self.index_arn, keys=keys[start:start + 100])
            indexed.update(item["key"] for item in response["vectors"])
        return len(indexed & self.keys.keys())

    def find(self, query_vector):
        response = self.client.query_vectors(
            indexArn=self.index_arn, queryVector={"float32": query_vector},
            topK=min(TOP_K, len(self.keys)), returnDistance=True,
            filter={"ncs_catalog_id": {"$eq": self.catalog_id}},
        )
        ranked = [(1 - hit["distance"], self.keys[hit["key"]]) for hit in response["vectors"]]
        if not ranked:
            raise ValueError("이 카탈로그의 벡터가 없습니다. 해당 인덱스에 --seed-index를 먼저 실행하세요.")
        return sorted(ranked, reverse=True)


def write_mapping(path, mapping, indent=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as temporary:
        staging = Path(temporary) / path.name
        with staging.open("w", encoding="utf-8") as stream:
            json.dump(mapping, stream, ensure_ascii=False, indent=indent)
            stream.write("\n")
        staging.replace(path)


def cli():
    parser = argparse.ArgumentParser(description="자격증·기술·직무지식의 NCS 능력단위 후보 매칭")
    parser.add_argument("--extractions", type=Path, default=PROCESSED / "extractions.json")
    parser.add_argument("--postings", type=Path, default=PROCESSED / "postings_jobalio.json")
    parser.add_argument("--as-of", type=date.fromisoformat, help="진행 여부 기준일 YYYY-MM-DD (기본: 한국 날짜)")
    parser.add_argument("--include-closed", action="store_true", help="마감 공고도 검색 범위에 포함")
    parser.add_argument("--extraction-mode", choices=("bedrock", "mock", "all"), default="bedrock",
                        help="검색할 추출 결과 모드 (기본: bedrock)")
    catalogs = parser.add_mutually_exclusive_group()
    catalogs.add_argument("--catalog", type=Path, help="NCS 능력단위 JSON (기본: data/ncs/ncs_units_official.json)")
    catalogs.add_argument("--sample", action="store_true", help="KOSA 실습용 능력단위 사용")
    parser.add_argument("--output", type=Path, help="결과 JSON 경로")
    parser.add_argument("--coverage-output", type=Path, help="공고·역량별 누락 점검 JSON 경로")
    parser.add_argument("--posting-id", nargs="+", help="이 공고에서 추출된 역량만 선택")
    parser.add_argument("--types", choices=CATALOG_TYPES, nargs="+", default=CATALOG_TYPES,
                        help="검색할 추출 유형 (기본: 자격증, 기술, 직무지식)")
    names = parser.add_mutually_exclusive_group()
    names.add_argument("--skill", action="append", help="추출 JSON의 역량명 지정 (반복 가능)")
    names.add_argument("--certificate", action="append", help="추출 JSON의 자격증명 지정 (반복 가능)")
    parser.add_argument("--unit-code", action="append", help="능력단위 코드로 카탈로그 범위 제한 (반복 가능)")
    parser.add_argument("--limit", type=int, help="검색할 역량 수. 실호출 시 1 또는 2")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Titan 및 팀 S3 Vectors 검색")
    mode.add_argument("--seed-index", action="store_true", help="Titan 임베딩을 만들어 팀 인덱스에 적재")
    mode.add_argument("--check-index", action="store_true", help="선택 카탈로그의 S3 Vectors 적재 수 확인")
    mode.add_argument("--create-index", action="store_true", help="서울 리전의 기존 팀 벡터 버킷에 인덱스 준비")
    parser.add_argument("--index-arn", help="서울 리전 팀 S3 Vectors 인덱스 ARN (또는 NCS_VECTOR_INDEX_ARN)")
    parser.add_argument("--vector-bucket", help="--create-index에 사용할 기존 팀 벡터 버킷 이름")
    parser.add_argument("--index-name", default="team04-kang-ncs", help="--create-index 인덱스 이름")
    parser.add_argument("--titan-cache", type=Path, default=TITAN_CACHE)
    parser.add_argument("--embed-limit", type=int, default=2,
                        help="--seed-index 실행당 새 Titan 임베딩 최대 수 (기본: 2, 0은 캐시만 사용)")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit은 1 이상이어야 합니다.")
    if args.embed_limit < 0:
        parser.error("--embed-limit은 0 이상이어야 합니다.")
    if args.live and args.limit not in {1, 2}:
        parser.error("--live 검증에는 --limit 1 또는 --limit 2를 지정하세요.")
    if args.create_index and not args.vector_bucket:
        parser.error("--create-index에는 서울 리전의 팀 벡터 버킷 이름을 --vector-bucket으로 지정하세요.")
    return parser, args


def select_queries(args, rows, has_sample):
    kinds = ("자격증",) if args.certificate else args.types
    skills = sorted({(name, kind) for items in rows.values() for name, kind, _ in items if kind in kinds})
    if args.skill or args.certificate:
        selected = {" ".join(name.split()) for name in (args.skill or args.certificate)}
        missing = selected - {name for name, _ in skills}
        if missing:
            raise ValueError(f"선택한 추출 결과에 없는 역량: {', '.join(sorted(missing))}")
        skills = [(name, kind) for name, kind in skills if name in selected]
    queries = [(name, kind, query_for(name, kind, has_sample)) for name, kind in skills]
    if args.limit is not None:
        queries = [item for item in queries if item[2]][:args.limit]
    return queries


def coverage_for(scope, competencies, queries, mapping, args, as_of, unlinked):
    by_query = {(name, kind): query for name, kind, query in queries}
    states = Counter({state: 0 for state in (
        "matched", "no_candidate", "query_unavailable", "not_selected", "outside_ncs_scope",
    )})
    postings = []
    for pid in scope:
        items = []
        for name, kind, requirement in competencies.get(pid, []):
            codes = [hit["unit_code"] for hit in mapping.get(name, []) if kind in hit["competency_types"]]
            key = (name, kind)
            if kind not in CATALOG_TYPES:
                status = "outside_ncs_scope"
            elif key not in by_query:
                status = "not_selected"
            elif not by_query[key]:
                status = "query_unavailable"
            else:
                status = "matched" if codes else "no_candidate"
            states[status] += 1
            items.append({"name": name, "type": kind, "requirement": requirement, "status": status,
                          "ncs_codes": codes if status == "matched" else []})
        postings.append({"id": pid, "has_extraction": pid in competencies, "competencies": items})
    return {
        "as_of": as_of.isoformat(), "timezone": "Asia/Seoul", "include_closed": args.include_closed,
        "extraction_mode": args.extraction_mode, "backend": "titan_s3vectors" if args.live else "mock_cosine",
        "posting_total": len(scope), "analyzed_posting_count": len(competencies),
        "missing_extraction_ids": [pid for pid in scope if pid not in competencies],
        "unlinked_extraction_ids": unlinked, "competency_counts": dict(states), "postings": postings,
    }


def results_for(queries, catalog, search, embed, live):
    backend = "titan_s3vectors" if live else "mock_cosine"
    by_query = {query: search(embed(query)) for query in sorted({query for _, _, query in queries if query})}
    by_name = {}
    for name, kind, query in queries:
        candidates = by_name.setdefault(name, {})
        for score, code in by_query.get(query, []):
            if score < MIN_SIMILARITY:
                continue
            best_score, best_kind, kinds = candidates.get(code, (score, kind, set()))
            kinds.add(kind)
            if score > best_score:
                best_score, best_kind = score, kind
            candidates[code] = (best_score, best_kind, kinds)
    return {
        name: [
            {"unit_code": code, "unit_name": catalog[code][0], "score": round(score, 4),
             "backend": backend, "catalog": "kosa_sample" if code.startswith("SAMPLE-") else "provided",
             "competency_type": kind, "competency_types": sorted(kinds)}
            for code, (score, kind, kinds) in sorted(candidates.items(), key=lambda hit: (-hit[1][0], hit[0]))[:TOP_K]
        ]
        for name, candidates in by_name.items()
    }


def main():
    parser, args = cli()
    catalog_path = args.catalog or (SAMPLE_CATALOG if args.sample else OFFICIAL_CATALOG)

    sys.path.insert(0, str(ROOT / "src"))
    from labutil import bedrock, config, vector

    def embed(text):
        return bedrock.embed_raw(text, DIMENSIONS, model_id=MODEL_ID)["embedding"]

    try:
        aws_mode = args.live or args.seed_index or args.create_index or args.check_index
        if aws_mode and (config.AWS_REGION != REGION or not config.AWS_PROFILE):
            raise ValueError("AWS_REGION=ap-northeast-2와 팀 AWS_PROFILE을 설정하세요.")
        if args.live or args.seed_index or args.create_index:
            if config.BEDROCK_MOCK:
                raise ValueError("AWS 실행에는 BEDROCK_MOCK=0을 설정하세요.")
        if args.live or args.seed_index or args.check_index:
            args.index_arn = args.index_arn or os.getenv("NCS_VECTOR_INDEX_ARN", "").strip()
            if not INDEX_ARN.fullmatch(args.index_arn):
                raise ValueError("서울 리전 인덱스 ARN을 --index-arn 또는 NCS_VECTOR_INDEX_ARN으로 지정하세요.")
        if args.create_index:
            client = bedrock.client("s3vectors")
            index_arn = ensure_index(client, args.vector_bucket, args.index_name)
            print(f"S3 Vectors 인덱스 ARN: {index_arn}")
            return

        if not catalog_path.exists() and args.catalog is None and not args.sample:
            raise ValueError("공식 NCS 카탈로그가 없습니다. python pipeline/match_ncs_catalog.py를 먼저 실행하거나 "
                             "팀의 카탈로그를 --catalog로 지정하세요. 실습 확인은 --sample을 사용하세요.")
        catalog = catalog_from(catalog_path)
        if args.unit_code:
            missing = set(args.unit_code) - catalog.keys()
            if missing:
                raise ValueError(f"카탈로그에 없는 능력단위 코드: {', '.join(sorted(missing))}")
            catalog = {code: item for code, item in catalog.items() if code in args.unit_code}

        if args.seed_index or args.check_index:
            search = S3Search(bedrock.client("s3vectors"), args.index_arn, catalog)
            if args.check_index:
                indexed = search.count_indexed()
                print(f"S3 Vectors 적재 확인: 능력단위 {indexed:,}개 / 선택 카탈로그 {len(catalog):,}개")
                if indexed != len(catalog):
                    raise ValueError("이 카탈로그의 벡터가 일부 또는 전부 없습니다. 동일 범위로 --seed-index를 실행하세요.")
                return
            if args.titan_cache.resolve() in {catalog_path.resolve(), args.extractions.resolve()}:
                raise ValueError("--titan-cache에 입력 파일과 다른 경로를 지정하세요.")
            vectors = titan_vectors(catalog, args.titan_cache, args.embed_limit, embed)
            search.seed(vectors)
            print(f"S3 Vectors 능력단위 {len(vectors):,}개 적재")
            return

        has_sample = any(code.startswith("SAMPLE-") for code in catalog)
        suffix = (".live" if args.live else "") + (".sample" if has_sample else "")
        limited = bool(args.posting_id or args.skill or args.certificate or args.limit is not None
                       or args.unit_code or tuple(args.types) != CATALOG_TYPES)
        if (limited and not args.live) or args.include_closed or args.extraction_mode != "bedrock":
            suffix += ".selected"
        output = args.output or PROCESSED / f"skill_ncs_map{suffix}.json"
        input_paths = {args.extractions.resolve(), args.postings.resolve(), catalog_path.resolve()}
        if output.resolve() in input_paths:
            raise ValueError("--output에 입력 파일과 다른 경로를 지정하세요.")
        coverage_path = args.coverage_output
        if coverage_path is None and not suffix:
            coverage_path = output.with_name("ncs_match_coverage.json")
        if coverage_path is not None and coverage_path.resolve() in input_paths | {output.resolve()}:
            raise ValueError("--coverage-output을 입력 또는 매칭 결과와 다른 경로로 지정하세요.")

        as_of = args.as_of or today_kst()
        scope, rows, unlinked = matching_scope(args, as_of)
        competencies = competencies_by_posting(rows)
        queries = select_queries(args, competencies, has_sample)
        if args.live and not queries:
            raise ValueError("선택한 공고에 검색 가능한 역량이 없습니다.")
        config.BEDROCK_MOCK = not args.live
        if args.live:
            search = S3Search(bedrock.client("s3vectors"), args.index_arn, catalog).find
        else:
            index = [(code, embed(text)) for code, (_, text) in catalog.items()]

            def search(query_vector):
                return vector.search(query_vector, index, k=TOP_K)

        mapping = results_for(queries, catalog, search, embed, args.live)
        write_mapping(output, mapping)
        if coverage_path:
            report = coverage_for(scope, competencies, queries, mapping, args, as_of, unlinked)
            write_mapping(coverage_path, report)
            print(f"{as_of} 기준 추출 완료 공고 {len(competencies):,}건 / 대상 {len(scope):,}건")
            if unlinked:
                print(f"공고 원본과 연결되지 않은 추출 {len(unlinked):,}건 (점검 JSON 참조)")
            counts = report["competency_counts"]
            print(f"역량 항목별 후보 있음 {counts['matched']:,}건 / 후보 없음 {counts['no_candidate']:,}건 / "
                  f"질의 없음 {counts['query_unavailable']:,}건 / 범위 외 {counts['outside_ncs_scope']:,}건")
            print(f"누락 점검 저장: {coverage_path}")
    except (ImportError, OSError, ValueError) as error:
        parser.error(str(error))

    mapped = sum(bool(candidates) for candidates in mapping.values())
    print(f"NCS 후보가 있는 역량 {mapped:,}종 / 출력 역량 {len(mapping):,}종")
    print(f"저장: {output}")


if __name__ == "__main__":
    main()
