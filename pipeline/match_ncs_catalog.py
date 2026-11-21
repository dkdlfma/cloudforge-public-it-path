"""한국산업인력공단의 정보통신 NCS 능력단위를 매칭용 JSON으로 변환합니다.

  python pipeline/match_ncs_catalog.py
  python pipeline/match_ncs_catalog.py --csv <공공데이터포털-원본.csv>
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree.ElementTree import ParseError


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "ncs" / "ncs_units_official.json"
MAJOR_CLASS = "20"
UNIT_CODE = re.compile(r"20\d{8}_\d{2}v\d+", re.IGNORECASE)
USAGE_API = "https://apis.data.go.kr/B490007/ncsUsage/openapi17"
USAGE_SOURCE = "https://www.data.go.kr/data/15086446/openapi.do"
TRAINING_SOURCE = "https://www.data.go.kr/data/15175394/fileData.do"
ACCOUNT_SOURCE = "https://www.data.go.kr/data/15151089/fileData.do?recommendDataYn=Y"


def unit(code, name, category, description, detail, source, source_url):
    if not UNIT_CODE.fullmatch(code) or not name:
        raise ValueError("정보통신 능력단위의 버전 포함 코드 또는 명칭이 올바르지 않습니다.")
    return {
        "unit_code": code, "unit_name": name, "category": category,
        "description": description, "detail": detail, "source": source, "source_url": source_url,
    }


def distinct(units):
    """훈련 시설·장비나 계좌 차수에 의해 반복되는 능력단위를 한 번만 저장합니다."""
    by_code = {}
    for row in units:
        code = row["unit_code"]
        previous = by_code.setdefault(code, row)
        if previous["unit_name"] != row["unit_name"]:
            raise ValueError(f"동일한 NCS 분류번호의 명칭이 서로 다릅니다 ({code}).")
        for field in ("description", "detail", "category"):
            previous[field] = max(previous[field], row[field], key=lambda value: (len(value), value))
    if not by_code:
        raise ValueError("대분류 20 정보통신에 해당하는 능력단위가 없습니다.")
    return [by_code[code] for code in sorted(by_code)]


def unit_from_usage(row):
    category = " / ".join(row[level] for level in ("대분류", "중분류", "소분류", "세분류") if row.get(level))
    detail = "\n".join(
        f"{label} {row[field]}" for field, label in (
            ("relQualfMtr", "관련 자격"), ("befDutyExprc", "이전 직무 경험"), ("dutyExperPerd", "직무 숙련 기간"),
        ) if row.get(field)
    )
    # 활용자료 API에는 능력단위 정의 필드가 없습니다. 공식 문서의 직무 분류와 관련 자격 등을 검색에 사용합니다.
    return unit(row.get("ncsClCd", ""), row.get("compeUnitName", ""), category, category, detail,
                "한국산업인력공단 NCS 활용자료", USAGE_SOURCE)


def fetch_units(api_key, parse, http_get, *, page_size=100, timeout=20):
    """NCS 활용자료 XML의 정보통신 페이지를 모두 읽고 버전 포함 코드로 중복을 제거합니다."""
    import requests

    if not api_key:
        raise ValueError("DATA_GO_KR_API_KEY를 설정하거나 공공데이터포털 CSV를 --csv로 지정하세요.")

    def fetch_page(page):
        params = {"ServiceKey": api_key, "pageNo": page, "numOfRows": page_size,
                  "returnType": "xml", "ncsLclasCd": MAJOR_CLASS}
        try:
            response = http_get(USAGE_API, params=params, timeout=timeout)
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else "알 수 없음"
            raise RuntimeError(f"공공데이터포털 NCS 활용자료 요청 실패 (HTTP {status}, {page}쪽). "
                               "서비스 활용신청과 일반 인증키를 확인하세요.") from None
        except requests.RequestException:
            raise RuntimeError(f"공공데이터포털 NCS 활용자료에 접속할 수 없습니다 ({page}쪽).") from None
        try:
            rows, info = parse(response.text)
        except ParseError:
            raise RuntimeError(f"NCS 활용자료가 올바른 XML을 반환하지 않았습니다 ({page}쪽).") from None
        except RuntimeError as error:
            code = re.match(r"\[(\d{1,4})\]", str(error))
            code_label = f"오류 코드 {code[1]}, " if code else ""
            raise RuntimeError(f"NCS 활용자료 조회 실패 ({code_label}{page}쪽). "
                               "서비스 활용신청과 일반 인증키를 확인하세요.") from None
        if not rows or not str(info.get("totalPage", "")).isdecimal():
            raise RuntimeError(f"NCS 활용자료에 조회 결과 또는 페이지 정보가 없습니다 ({page}쪽).")
        if info.get("pageNo") is not None and str(info["pageNo"]).lstrip("0") != str(page):
            raise RuntimeError(f"NCS 활용자료가 요청한 페이지 번호를 반환하지 않았습니다 ({page}쪽).")
        return rows, int(info["totalPage"])

    rows, pages = fetch_page(1)
    if pages < 1:
        raise RuntimeError("NCS 활용자료의 페이지 수가 올바르지 않습니다.")
    for page in range(2, pages + 1):
        page_rows, total_pages = fetch_page(page)
        if total_pages != pages:
            raise RuntimeError("조회 중 NCS 활용자료의 페이지 수가 바뀌었습니다. 다시 실행하세요.")
        rows.extend(page_rows)
    return distinct(unit_from_usage(row) for row in rows)


def units_from_csv(path):
    """직무능력은행 또는 능력단위별 훈련 정보의 원본 CSV를 변환합니다."""
    for encoding in ("utf-8-sig", "cp949"):
        try:
            with path.open(encoding=encoding, newline="") as stream:
                return read_csv_units(stream)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 인코딩은 UTF-8 또는 CP949가 필요합니다.")


def read_csv_units(stream):
    rows = csv.DictReader(stream)
    rows.fieldnames = [name.strip() for name in (rows.fieldnames or [])]
    headers = set(rows.fieldnames)
    training = {"NCS세분류코드", "세분류명", "능력단위분류번호", "능력단위명", "훈련목표"}
    account = {"NCS대분류코드", "NCS세분류코드명", "NCS분류코드", "능력단위명", "능력단위정의"}
    if training <= headers:
        return distinct(
            unit(row["능력단위분류번호"], row["능력단위명"], row["세분류명"], "",
                 f"훈련 목표 {row['훈련목표']}" if row["훈련목표"] else "",
                 "한국산업인력공단 NCS 능력단위별 훈련 정보", TRAINING_SOURCE)
            for row in clean_rows(rows) if row["NCS세분류코드"].startswith(MAJOR_CLASS)
        )
    if account <= headers:
        return distinct(
            unit(row["NCS분류코드"], row["능력단위명"], row["NCS세분류코드명"], row["능력단위정의"], "",
                 "한국산업인력공단 직무능력은행 NCS분류별 계좌 통계 정보", ACCOUNT_SOURCE)
            for row in clean_rows(rows) if row["NCS대분류코드"] == MAJOR_CLASS and row["NCS분류코드"]
        )
    raise ValueError("지원하는 CSV는 'NCS 능력단위별 훈련 정보'(15175394) 또는 "
                     "'직무능력은행 NCS분류별 계좌 통계 정보'(15151089)의 원본 파일입니다.")


def clean_rows(rows):
    for row in rows:
        if None in row:
            raise ValueError("CSV 헤더보다 열이 많은 행이 있습니다.")
        yield {key: (value or "").strip() for key, value in row.items()}


def main():
    parser = argparse.ArgumentParser(description="한국산업인력공단 정보통신 NCS 능력단위 카탈로그 생성")
    parser.add_argument("--csv", type=Path, help="공공데이터포털 원본 CSV로 변환 (인증키 불필요)")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="능력단위 JSON 저장 경로")
    args = parser.parse_args()
    try:
        if args.csv:
            if args.csv.resolve() == args.output.resolve():
                raise ValueError("--output을 원본 CSV와 다른 경로로 지정하세요.")
            units = units_from_csv(args.csv)
        else:
            import requests

            sys.path.insert(0, str(ROOT / "src"))
            from labutil import config, ncs_api

            units = fetch_units(config.DATA_GO_KR_API_KEY, ncs_api.parse, requests.get)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=args.output.parent) as temp:
            staged = Path(temp) / args.output.name
            staged.write_text(json.dumps(units, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            staged.replace(args.output)
    except (ImportError, OSError, RuntimeError, ValueError, csv.Error) as error:
        parser.error(str(error))
    print(f"정보통신 NCS 능력단위 {len(units):,}개 저장: {args.output}")


if __name__ == "__main__":
    main()
