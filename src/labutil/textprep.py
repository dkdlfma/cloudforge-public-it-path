"""한국어 텍스트 전처리 (Kiwi 형태소 분석기 — Java 설치 불필요)"""
import re
from functools import lru_cache


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")          # HTML 태그 제거
    text = re.sub(r"&[a-z]+;", " ", text)              # &nbsp; 등
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


@lru_cache
def _kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()


def nouns(text: str):
    """명사(NNG, NNP)와 영문(SL)만 추출"""
    return [t.form for t in _kiwi().tokenize(text) if t.tag in ("NNG", "NNP", "SL")]


def sections(text: str):
    """[주요업무]/[자격요건]/[우대사항] 블록으로 나누기"""
    out, cur = {}, "기타"
    for line in text.splitlines():
        m = re.match(r"\s*\[(.+?)\]", line)
        if m:
            cur = m.group(1)
            out[cur] = []
        elif line.strip():
            out.setdefault(cur, []).append(line.strip("- ").strip())
    return out
