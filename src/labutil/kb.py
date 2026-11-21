"""Bedrock Knowledge Base 도우미 (6주차) — 문서 만들기 · 동기화 · 검색 · RAG
모의 모드(BEDROCK_MOCK=1)에서는 outputs/kb_docs 의 로컬 문서를 모의 임베딩으로 검색합니다."""
import json
import time
from pathlib import Path

from . import config

DOCS_DIR = config.OUTPUTS / "kb_docs"          # 로컬 사본 (S3 kb/ 와 같은 구조)
INDUSTRIES = ["IT서비스", "금융", "공공", "유통", "제조"]


# ---------------------------------------------------------------- 데이터 준비
def load_programs():
    """보완 교육 프로그램 샘플 30개 (가상 데이터)"""
    return json.load(open(config.DATA / "week6" / "programs_sample.json", encoding="utf-8"))["data"]


def ensure_week5_outputs():
    """5주차 결과(gap_table.csv, week5_gap_report.md)가 없으면 샘플로 다시 만듭니다."""
    gap_csv = config.OUTPUTS / "gap_table.csv"
    report = config.OUTPUTS / "week5_gap_report.md"
    if gap_csv.exists() and report.exists():
        return gap_csv, report
    from . import data, gap
    df, courses = data.load_postings(), data.load_courses()
    demand, supply = gap.demand_rates(df), gap.supply_rates(courses)
    table = gap.long_table(demand, supply)
    table.to_csv(gap_csv, index=False, encoding="utf-8-sig")
    G = gap.gap_table(demand, supply)
    lines = [f"| {ind} | " + ", ".join(f"{s} ({v:+.1f}%p)" for s, v in sorted(sk.items(), key=lambda x: -x[1])[:3]) + " |"
             for ind, sk in G.items()]
    report.write_text("# 5주차 산출물 — Skills Gap 분석\n\n| 산업 | Gap Top 3 |\n|---|---|\n" + "\n".join(lines) + "\n",
                      encoding="utf-8")
    print("ℹ 5주차 결과가 없어 샘플로 다시 만들었습니다:", gap_csv.name, report.name)
    return gap_csv, report


def ncs_doc(u):
    """NCS 능력단위 1개 → (문서 본문, 메타데이터)"""
    text = (f"# {u['unit_name']} (NCS 능력단위 {u['unit_code']})\n"
            f"- 분야: {u['category']}\n- 정의: {u['description']}\n- 수행 내용: {u['detail']}\n")
    meta = {"type": "ncs", "id": u["unit_code"], "title": u["unit_name"], "category": u["category"]}
    return text, meta


def program_doc(p):
    """교육 프로그램 1개 → (문서 본문, 메타데이터)  ※ 07 노트북 실습 1의 정답과 같습니다"""
    text = (f"# {p['title']} (교육 프로그램 {p['program_id']})\n"
            f"- 대상 기술: {', '.join(p['skills'])}\n- 수준: {p['level']} · {p['hours']}시간 · {p['format']} · {p['cost']}\n"
            f"- 선수 지식: {p['prerequisite']}\n- 설명: {p['description']}\n")
    meta = {"type": "program", "id": p["program_id"], "title": p["title"], "level": p["level"], "skills": p["skills"]}
    return text, meta


def write_doc(rel_path, text, meta):
    """로컬에 문서와 메타데이터 파일(<파일>.metadata.json)을 함께 저장"""
    path = DOCS_DIR / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    Path(str(path) + ".metadata.json").write_text(
        json.dumps({"metadataAttributes": meta}, ensure_ascii=False), encoding="utf-8")
    return path


def build_all_docs():
    """(강사·모의 모드용) 문서 전체를 라이브러리 함수로 다시 만들기"""
    from . import data
    for u in data.load_ncs_units().to_dict("records"):
        write_doc(f"ncs/{u['unit_code']}.md", *ncs_doc(u))
    for p in load_programs():
        write_doc(f"programs/{p['program_id']}.md", *program_doc(p))
    _, report = ensure_week5_outputs()
    write_doc("report/week5_gap_report.md", report.read_text(encoding="utf-8"),
              {"type": "report", "id": "GAP", "title": "5주차 Skills Gap 보고서"})
    return sorted(p for p in DOCS_DIR.rglob("*.md"))


def upload_docs(bucket):
    """outputs/kb_docs 아래 파일을 S3 팀 버킷 kb/ 아래로 그대로 올립니다."""
    files = sorted(p for p in DOCS_DIR.rglob("*") if p.is_file())
    if config.BEDROCK_MOCK:
        print(f"모의 모드: 업로드 생략 ({len(files)}개 파일)")
        return len(files)
    from .bedrock import client
    s3 = client("s3")
    for p in files:
        s3.upload_file(str(p), bucket, "kb/" + p.relative_to(DOCS_DIR).as_posix())
    return len(files)


# ---------------------------------------------------------------- 동기화 (S3 → 청킹 → 임베딩 → 벡터 저장)
def sync(kb_id, ds_id, poll=5):
    """수집(Ingestion) 작업을 시작하고 끝날 때까지 기다립니다. 반환: 통계 dict"""
    if config.BEDROCK_MOCK:
        n = len(list(DOCS_DIR.rglob("*.md")))
        print("모의 모드: 로컬 문서로 검색합니다.")
        return {"numberOfDocumentsScanned": n, "numberOfNewDocumentsIndexed": n, "numberOfDocumentsFailed": 0}
    from .bedrock import client
    ag = client("bedrock-agent")
    try:
        job = ag.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)["ingestionJob"]
    except ag.exceptions.ConflictException:
        print("이미 동기화가 진행 중입니다 → 그 작업을 기다립니다.")
        job = ag.list_ingestion_jobs(knowledgeBaseId=kb_id, dataSourceId=ds_id, maxResults=1,
                                     sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"})["ingestionJobSummaries"][0]
    t0 = time.time()
    while True:
        j = ag.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job["ingestionJobId"])["ingestionJob"]
        print(f"  {time.time() - t0:4.0f}초  상태: {j['status']}")
        if j["status"] in ("COMPLETE", "FAILED", "STOPPED"):
            break
        time.sleep(poll)
    if j.get("failureReasons"):
        print("❌ 실패 이유:", j["failureReasons"][:3])
    return j.get("statistics", {})


# ---------------------------------------------------------------- 검색
def retrieve(kb_id, query, k=5, doc_type=None):
    """Knowledge Base 검색 → Bedrock Retrieve API 응답 그대로 (dict)"""
    if config.BEDROCK_MOCK:
        return _mock_retrieve(query, k, doc_type)
    from .bedrock import client
    vcfg = {"numberOfResults": k}
    if doc_type:
        vcfg["filter"] = {"equals": {"key": "type", "value": doc_type}}
    return client("bedrock-agent-runtime").retrieve(
        knowledgeBaseId=kb_id, retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": vcfg})


def rows(resp):
    """Retrieve 응답 → [(점수, 문서 ID, 제목)] (07 노트북 실습 2의 정답과 같은 일)"""
    out = []
    for r in resp["retrievalResults"]:
        meta = r.get("metadata", {})
        out.append((round(r.get("score", 0), 3), meta.get("id"), meta.get("title")))
    return out


def _model_arn(model_id):
    """추론 프로필 ID(global.…) → ARN (RetrieveAndGenerate 는 ARN 을 받습니다)"""
    if model_id.startswith("arn:"):
        return model_id
    from .bedrock import client
    account = client("sts").get_caller_identity()["Account"]
    if model_id.split(".")[0] in ("global", "apac", "us", "eu", "jp", "au"):
        return f"arn:aws:bedrock:{config.AWS_REGION}:{account}:inference-profile/{model_id}"
    return f"arn:aws:bedrock:{config.AWS_REGION}::foundation-model/{model_id}"


def rag(kb_id, question, model_id=None, k=5):
    """검색 + 답변 생성을 한 번에 (RetrieveAndGenerate) → (답변, [출처 파일명])"""
    if config.BEDROCK_MOCK:
        res = _mock_retrieve(question, k, None)
        src = [r["location"]["s3Location"]["uri"].split("/")[-1] for r in res["retrievalResults"]]
        return "(모의 답변) 검색된 문서: " + ", ".join(src[:3]), src
    from .bedrock import client
    r = client("bedrock-agent-runtime").retrieve_and_generate(
        input={"text": question},
        retrieveAndGenerateConfiguration={"type": "KNOWLEDGE_BASE", "knowledgeBaseConfiguration": {
            "knowledgeBaseId": kb_id, "modelArn": _model_arn(model_id or config.FAST_MODEL_ID),
            "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": k}}}})
    src = []
    for c in r.get("citations", []):
        for ref in c.get("retrievedReferences", []):
            name = ref["location"]["s3Location"]["uri"].split("/")[-1]
            if name not in src:
                src.append(name)
    return r["output"]["text"], src


# ---------------------------------------------------------------- 모의 검색 (AWS 없이 흐름 연습)
_MOCK_INDEX = None


def _mock_retrieve(query, k, doc_type):
    global _MOCK_INDEX
    from .bedrock import embed
    from .vector import cosine
    if _MOCK_INDEX is None or not _MOCK_INDEX:
        if not list(DOCS_DIR.rglob("*.md")):
            build_all_docs()
        _MOCK_INDEX = []
        for p in sorted(DOCS_DIR.rglob("*.md")):
            meta = json.load(open(str(p) + ".metadata.json", encoding="utf-8"))["metadataAttributes"]
            text = p.read_text(encoding="utf-8")
            _MOCK_INDEX.append((embed(text), text, meta, "s3://mock-bucket/kb/" + p.relative_to(DOCS_DIR).as_posix()))
    q = embed(query)
    scored = sorted(((cosine(q, v), t, m, u) for v, t, m, u in _MOCK_INDEX if not doc_type or m["type"] == doc_type),
                    key=lambda x: -x[0])[:k]
    return {"retrievalResults": [{"content": {"text": t}, "location": {"type": "S3", "s3Location": {"uri": u}},
                                  "score": s, "metadata": m} for s, t, m, u in scored]}
