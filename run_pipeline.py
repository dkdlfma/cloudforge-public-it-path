"""전체 파이프라인 한 번에 실행 (통합 테스트용)   담당: 강민경

  [1] S3 에서 최신 데이터 받기   pipeline/upload_s3.py --download
  [2] 역량 추출 (Bedrock)        pipeline/extract_competencies.py   ← --extract N 을 줄 때만 (비용 발생)
  [3] NCS 매칭                   pipeline/match_ncs.py
      + 기술 스택 임베딩            pipeline/embed_stacks.py   (스택 → NCS·훈련과정, --live-embed 면 Titan)
  [4] 수요 분석                  pipeline/analyze_demand.py --mode bedrock
  [5] 형식 점검                  pipeline/check_formats.py
  [6] S3 에 결과 올리기          pipeline/upload_s3.py              ← --upload 를 줄 때만

실행 예:
  python run_pipeline.py                         # [1][3][4][5] — 비용 0, 지금 데이터로 분석만 다시
  python run_pipeline.py --extract 20 --upload   # 새 공고 20건 추출까지 하고 결과를 S3 에 올리기
  python run_pipeline.py --offline               # [1] 없이 내 PC 파일로만 (AWS 로그인 불필요)
  python run_pipeline.py --only 4 5              # 원하는 단계만

각 단계는 팀원이 만든 스크립트를 그대로 부릅니다 (코드를 고치지 않음). 한 단계가 실패하면 멈춥니다.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


def step(no, title, args):
    print(f"\n━━━━ [{no}] {title} ━━━━\n$ python {' '.join(args)}")
    t0 = time.time()
    r = subprocess.run([PY, *args], cwd=ROOT)
    if r.returncode != 0:
        sys.exit(f"\n❌ [{no}] {title} 실패 (종료 코드 {r.returncode}). 위 메시지를 확인하세요.")
    print(f"✔ [{no}] 완료 ({time.time() - t0:.1f}초)")


def main():
    ap = argparse.ArgumentParser(description="aws_edu 전체 파이프라인")
    ap.add_argument("--extract", type=int, default=0, metavar="N", help="새 공고 N건 역량 추출 (기본 0 = 건너뜀)")
    ap.add_argument("--all-postings", action="store_true", help="추출 대상을 정보통신 단독·신입 공고로 제한하지 않음")
    ap.add_argument("--demand-mode", choices=["bedrock", "all", "mock"], default="bedrock",
                    help="수요 분석에 넣을 추출 결과 (기본 bedrock = 실제 AI 결과만)")
    ap.add_argument("--live-embed", action="store_true", help="기술 스택 임베딩을 실제 Titan 으로 (비용 소액)")
    ap.add_argument("--upload", action="store_true", help="마지막에 결과를 S3 에 올리기")
    ap.add_argument("--offline", action="store_true", help="S3 받기/올리기 없이 내 PC 파일로만")
    ap.add_argument("--only", nargs="+", type=int, help="이 단계 번호만 실행 (예: --only 4 5)")
    a = ap.parse_args()

    want = set(a.only) if a.only else {1, 3, 4, 5} | ({2} if a.extract else set()) | ({6} if a.upload else set())
    if a.offline:
        want -= {1, 6}

    if 1 in want:
        step(1, "S3 에서 최신 데이터 받기", ["pipeline/upload_s3.py", "--download"])
    if 2 in want:
        ex = ["pipeline/extract_competencies.py", "--limit", str(a.extract or 1)]
        if not a.all_postings:
            ex += ["--it-only", "--newbie"]
        if a.extract > 5:
            ex.append("--yes")
        step(2, f"역량 추출 {a.extract or 1}건", ex)
    if 3 in want:
        step(3, "NCS 매칭", ["pipeline/match_ncs.py"])
        step("3+", "기술 스택 임베딩 매칭", ["pipeline/embed_stacks.py"] + (["--live"] if a.live_embed else []))
    if 4 in want:
        step(4, f"수요 분석 (mode={a.demand_mode})", ["pipeline/analyze_demand.py", "--mode", a.demand_mode])
    if 5 in want:
        step(5, "형식 점검", ["pipeline/check_formats.py"])
    if 6 in want:
        step(6, "S3 에 결과 올리기", ["pipeline/upload_s3.py"])
    print("\n✅ 파이프라인 끝")


if __name__ == "__main__":
    main()
