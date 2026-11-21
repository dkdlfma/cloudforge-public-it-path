"""첫 목표 확인용: 공고 1건 → 추천 1건 (터미널)
사용: python -m recommend.demo JA-267524"""
import sys

from . import engine

pid = sys.argv[1] if len(sys.argv) > 1 else "JA-267524"
profile = {"student": "demo", "major_it": True, "certs": [], "interests": ["보안"], "weeks": 24}
rec = engine.recommend(pid, profile)
print(engine.summary_text(rec))
