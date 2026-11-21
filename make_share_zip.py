"""팀 공유용 압축 파일 만들기 — 인증키(.env)와 큰 폴더는 자동으로 뺍니다.

실행:  python make_share_zip.py
결과:  share/aws_edu_YYYYMMDD_HHMM.zip  → 이 파일만 카카오톡/구글 드라이브에 올리세요.
제출:  python make_share_zip.py --submit  → share/aws_edu_submit_YYYYMMDD_HHMM.zip
       (공고 파일까지 포함 → 심사자가 AWS 없이도 python run_pipeline.py --offline / streamlit 실행 가능)
"""
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {".venv", "venv", "__pycache__", ".ipynb_checkpoints", "share", "outputs", "_backup", "_to_delete"}
SKIP_PATHS = {Path("data") / "raw"}          # API 원본 페이지 (processed 만 공유)
SKIP_FILES = {".env",                        # 인증키 — 절대 공유 금지
              "postings_jobalio.json", "postings_jobalio.csv"}   # 공고는 S3 에서 받음 (upload_s3.py --download)

out_dir = ROOT / "share"
out_dir.mkdir(exist_ok=True)
SUBMIT = "--submit" in sys.argv
if SUBMIT:                                    # 제출용: 공고 파일 포함 (인증키는 여전히 제외)
    SKIP_FILES = {".env"}
out = out_dir / f"aws_edu_{'submit_' if SUBMIT else ''}{datetime.now():%Y%m%d_%H%M}.zip"

count = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = Path(dirpath).relative_to(ROOT)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and rel_dir / d not in SKIP_PATHS]
        for name in filenames:
            if name in SKIP_FILES or name.endswith(".pyc"):
                continue
            z.write(Path(dirpath) / name, Path("aws_edu") / rel_dir / name)
            count += 1

with zipfile.ZipFile(out) as z:                # 마지막 안전 확인
    leaked = [n for n in z.namelist() if n.endswith("/.env") or n == ".env"]
if leaked:
    out.unlink()
    raise SystemExit("❌ .env 가 들어가 압축을 삭제했습니다. 코드를 확인하세요.")

print(f"✅ {out.relative_to(ROOT)}  ({count}개 파일, {out.stat().st_size / 1e6:.1f} MB)")
if SUBMIT:
    print("   제출용: 공고 파일 포함 · .env / .venv / data/raw / outputs 제외. 이 zip 을 제출하세요.")
else:
    print("   .env / .venv / data/raw / outputs / 공고 원본(S3에서 받기) 은 제외됐습니다. 이 zip 만 공유하세요.")
