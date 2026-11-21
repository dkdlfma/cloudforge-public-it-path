"""기술명 정규화 — '쿠버네티스', 'k8s', 'K8S'를 모두 'Kubernetes' 하나로."""
import json
import re
from functools import lru_cache
from .config import DATA


@lru_cache
def load_aliases():
    return json.load(open(DATA / "dict" / "skill_aliases.json", encoding="utf-8"))


@lru_cache
def _alias_index():
    idx = {}
    for canon, v in load_aliases().items():
        idx[_key(canon)] = canon
        for a in v["aliases"]:
            idx[_key(a)] = canon
    return idx


def _key(s: str) -> str:
    return re.sub(r"[\s\-_·./()]", "", s).lower()


def normalize(name: str):
    """별칭을 대표명으로. 사전에 없으면 None."""
    return _alias_index().get(_key(name))


def normalize_list(names):
    out, unknown = [], []
    for n in names:
        c = normalize(n)
        if c:
            if c not in out:
                out.append(c)
        else:
            unknown.append(n)
    return sorted(out), unknown


def category(canon: str) -> str:
    return load_aliases().get(canon, {}).get("category", "기타")


def rule_extract(text: str):
    """규칙 기반 베이스라인: 사전에 있는 별칭이 본문에 나오면 추출 (원문 표기 그대로 반환)."""
    found = []
    for canon, v in load_aliases().items():
        for a in sorted(v["aliases"], key=len, reverse=True):
            if re.search(r"(?<![A-Za-z가-힣])" + re.escape(a) + r"(?![A-Za-z])", text):
                found.append(a)
                break
    return found


def describe(canon: str) -> str:
    """기술 대표명 → 검색용 설명 문장 (분야와 다른 표기를 덧붙여 의미를 풍부하게)"""
    v = load_aliases().get(canon)
    if not v:
        return canon
    others = [a for a in v["aliases"] if a != canon]
    return f"{canon} 기술 ({v['category']} 분야). 다른 표기: {', '.join(others)}"
