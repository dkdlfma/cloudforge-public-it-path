"""추천 엔진 — 공고 1건 + 사용자 프로필 → 학습경로 1건
AWS 없이 돌아가는 규칙 기반 핵심 로직입니다. 에이전트(agent.py)도 이 함수를 '도구'로 씁니다."""
import json
from datetime import datetime

from . import catalog, courses, data_io, stacks

FEAS_SCORE = {"높음": 3.0, "중간": 2.0, "낮음": 0.5}
DOWNGRADE = {"높음": "중간", "중간": "낮음", "낮음": "낮음"}

DEFAULT_PROFILE = {
    "student": "demo",       # 저장용 이름(별칭)
    "major_it": True,        # IT 관련 전공 여부 (기사급 응시자격 판단에 사용)
    "experience_years": 0,   # 관련 실무 경력(년) — 공고 경력 요건 판정
    "completed": [],         # 이수한 교육·훈련과정 (과정명) — 추천에서 제외, 공고의 교육 이수 요건 판정
    "certs": [],             # 이미 가진 자격증
    "stacks": [],            # 이미 다룰 줄 아는 기술 스택 (Java, MySQL …)
    "interests": [],         # 관심 분야: 개발 · DB · 보안 · 데이터 · 네트워크 · 운영 · 관리
    "weeks": 24,             # 준비 가능 기간(주)
}

ASSUMPTIONS = [
    "자격증의 신입 취득 현실성·준비 기간은 팀 가정값입니다 (응시자격·일정은 Q-Net/시행기관 확인).",
    "수요 비중은 현재 추출이 끝난 공고만 기준이라, 추출 건수가 늘면 순위가 바뀔 수 있습니다.",
    "NCS 연결은 공식 NCS 정보통신 능력단위 1,214개와의 유사도 매칭 결과입니다.",
    "기술 스택의 학습 기간·선행 순서는 팀 가정값이며, 스택↔NCS·훈련과정 연결은 임베딩 유사도 기반 후보입니다.",
]


def _profile(profile):
    p = {**DEFAULT_PROFILE, **(profile or {})}
    p["held"] = {catalog.canonical(c) for c in p.get("certs", [])}
    return p


def _posting_summary(ext):
    raw = data_io.posting(ext["id"])
    return {
        "id": ext["id"], "company": ext.get("company", ""), "title": ext.get("title", ""),
        "career": ext.get("career", ""), "education": ext.get("education", ""),
        "region": raw.get("region", ""), "hire_type": raw.get("hire_type", ""),
        "close_at": raw.get("close_at", ""), "url": raw.get("url", ""),
        "entry_level_ok": ext.get("entry_level_ok"), "min_experience_years": ext.get("min_experience_years"),
        "job_areas": ext.get("job_areas", []), "it_relevant": ext.get("it_relevant"),
    }


def positions(posting_id):
    """공고 안의 모집 분야 목록 (여러 분야를 한 공고에 뽑는 경우)"""
    ext = data_io.extractions().get(posting_id) or {}
    cnt = {}
    for c in ext.get("competencies", []):
        if c.get("position"):
            cnt[c["position"]] = cnt.get(c["position"], 0) + 1
    return sorted(cnt, key=lambda x: -cnt[x])


def get_requirements(posting_id, position=None):
    """공고의 요구 역량을 필수/선택필수/우대로 묶어 돌려줍니다 (에이전트 도구 1).
    모집 분야가 여러 개면 position 분야 + 공통 요건만 봅니다 (없으면 요건이 가장 많은 분야)."""
    ext = data_io.extractions().get(posting_id)
    if not ext:
        return {"ok": False, "error": f"추출 결과에 없는 공고입니다: {posting_id}"}
    pos_list = positions(posting_id)
    if pos_list and position not in pos_list:
        position = pos_list[0]
    groups = {"필수": [], "선택필수": [], "우대": []}
    for c in ext.get("competencies", []):
        if c.get("position") and c["position"] != position:
            continue
        groups.setdefault(c.get("requirement", "우대"), []).append(
            {"name": c["name"], "type": c.get("type", ""), "evidence": c.get("evidence", "")})
    return {"ok": True, "posting": _posting_summary(ext), "groups": groups,
            "positions": pos_list, "position": position if pos_list else None}


def _score_cert(name, prof):
    """자격증 1개의 추천 점수와 이유"""
    e = catalog.find(name) or catalog.infer(name)
    score, reasons = 0.0, []
    info = {"name": name, "canonical": e["name"] if e else name}
    if e and e.get("vague"):
        score -= 1.0
        reasons.append("구체적인 자격증명이 아님 — 공고 원문에서 인정 목록 확인")
    elif e and e.get("inferred"):
        reasons.append(f"카탈로그 미등록 — '{e['tier']}' 등급으로 추정")
    if e:
        feas = e["entry_feasibility"]
        if not prof["major_it"] and e.get("needs_eligibility"):
            feas = DOWNGRADE[feas]
            reasons.append("비전공자는 응시자격 요건을 먼저 확인해야 함")
        score += FEAS_SCORE[feas]
        reasons.append(f"신입 취득 현실성 '{feas}'")
        if e["category"] in prof["interests"]:
            score += 1.5
            reasons.append(f"관심 분야({e['category']})와 일치")
        if e["prep_weeks"] > prof["weeks"]:
            score -= 2.0
            reasons.append(f"준비 기간 약 {e['prep_weeks']}주 → 목표 {prof['weeks']}주 초과")
        else:
            score += 0.5
        info.update(category=e["category"], tier=e["tier"], issuer=e["issuer"], eligibility=e["eligibility"],
                    feasibility=feas, prep_weeks=e["prep_weeks"])
    else:
        score += 1.0
        reasons.append("카탈로그 미등록 — 정보 확인 필요")
    d = data_io.demand_overall().get(name)
    regional = [(g, data_io.demand_by_region().get((g, name))) for g in prof.get("regions") or []]
    regional = [(g, x) for g, x in regional if x and x["analyzed"]]
    if regional:                                   # 희망 지역이 있으면 그 지역 수요를 우선 반영 (신성훈 지역별 수요표)
        g, x = max(regional, key=lambda t: t[1]["posting_count"] / t[1]["analyzed"])
        score += x["posting_count"] / x["analyzed"]
        reasons.append(f"{g} 분석 공고 {x['analyzed']}건 중 {x['posting_count']}건이 인정")
        info["demand"] = x
    elif d and d["analyzed"]:
        score += d["posting_count"] / d["analyzed"]
        reasons.append(f"분석 공고 {d['analyzed']}건 중 {d['posting_count']}건이 인정")
        info["demand"] = d
    info["ncs"] = [u["unit_name"] for u in data_io.ncs_map().get(name, [])][:3]
    if info["ncs"]:
        reasons.append("NCS 연결: " + ", ".join(info["ncs"]))
    info["score"] = round(score, 2)
    info["reasons"] = reasons
    return info


def _phase_weeks(total, phases):
    if len(phases) <= 1:
        return [total]
    first = round(total * 0.6)
    rest = total - first
    return [first] + [max(1, round(rest / (len(phases) - 1)))] * (len(phases) - 1)


def build_path(targets, prof):
    """목표 자격증 목록 → 선행 자격증을 포함한 순서 있는 학습경로"""
    steps, seen = [], set(prof["held"])

    def add(name, why):
        e = catalog.find(name) or catalog.infer(name)
        cname = e["name"] if e else name
        if cname in seen:
            return
        if e:
            for pre in e.get("recommended_before", []):
                if catalog.canonical(pre) not in seen:
                    add(pre, f"{cname} 준비 전 기초 다지기")
        seen.add(cname)
        phases = e.get("phases", ["시험"]) if e else ["시험"]
        weeks = _phase_weeks(e["prep_weeks"] if e else 8, phases)
        for ph, w in zip(phases, weeks):
            steps.append({"name": cname, "phase": ph, "weeks": w, "why": why})

    for name, why in targets:
        add(name, why)
    t = 0
    for i, s in enumerate(steps, 1):
        s["order"] = i
        t += s["weeks"]
        s["cum_weeks"] = t
    return steps, t


def cert_evidence(name, ongoing_only=True, k=5):
    """자격증 → 이 자격증을 요구한 공고 목록 (구분·근거 문장·원문 링크) + 필수/선택필수/우대 공고 수"""
    ck = catalog.canonical(name)
    rows, cnt = [], {"필수": 0, "선택필수": 0, "우대": 0}
    for pid, e in data_io.extractions().items():
        close, ongoing = data_io.close_info(pid)
        if ongoing_only and not ongoing:
            continue
        hit = next((c for c in e.get("competencies", []) if c.get("type") == "자격증"
                    and catalog.canonical(c["name"]) == ck), None)
        if not hit:
            continue
        cnt[hit.get("requirement", "우대")] = cnt.get(hit.get("requirement", "우대"), 0) + 1
        rows.append({"id": pid, "company": e.get("company", ""), "title": e.get("title", ""),
                     "requirement": hit.get("requirement", ""), "evidence": hit.get("evidence", ""),
                     "close_at": close, "url": data_io.posting(pid).get("url", "")})
    order = {"필수": 0, "선택필수": 1, "우대": 2}
    rows.sort(key=lambda r: (order.get(r["requirement"], 3), r["close_at"] or "9999"))
    total = sum(1 for pid in data_io.extractions() if not ongoing_only or data_io.close_info(pid)[1])
    return {"cert": ck, "counts": cnt, "postings": rows[:k], "n": len(rows), "total": total,
            "scope": "진행 중 공고" if ongoing_only else "추출된 전체 공고"}


def _years(text):
    """'6개월 이상', '경력 2년 이상' → 0.5, 2.0 (못 찾으면 None)"""
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*(년|개월)", str(text))
    if not m:
        return None
    v = float(m.group(1))
    return v / 12 if m.group(2) == "개월" else v


def _exp_items(g, prof):
    """공고의 경력 요건들 → [(요건 dict, 필요 년수 또는 None, 충족 여부 또는 None)]"""
    mine = float(prof.get("experience_years") or 0)
    out = []
    for level in ("필수", "선택필수"):
        for c in g[level]:
            if c.get("type") != "경력":
                continue
            need = _years(c.get("evidence", "")) if _years(c["name"]) is None else _years(c["name"])
            out.append(({**c, "_req": level}, need, None if need is None else mine >= need))
    return out


def _completed_hit(text, prof):
    """공고의 교육 이수 요건이 사용자가 입력한 이수 과정에 있는지 (글자 일부 일치)"""
    key = catalog.key(text)
    for c in prof.get("completed", []):
        ck = catalog.key(c)
        if ck and len(ck) >= 3 and (ck in key or key in ck):
            return c
    return None


def profile_check(g, post, prof, stk=None, cert_groups=None):
    """'입력하신 프로필 기준' 요건별 충족 여부 — 공고 근거 문장과 함께"""
    rows = []
    for grp in cert_groups or []:
        have = [c["name"] for c in grp if catalog.canonical(c["name"]) in prof["held"]]
        label = grp[0]["name"] if len(grp) == 1 else f"인정 자격증 {len(grp)}종 중 1개"
        rows.append({"요건": label, "구분": grp[0].get("_req", "선택필수"), "유형": "자격증",
                     "내 프로필": ", ".join(have) if have else "보유 자격증 없음",
                     "판정": "충족" if have else "미충족", "공고 근거": grp[0].get("evidence", "")})
    mine = float(prof.get("experience_years") or 0)
    items = _exp_items(g, prof)
    need = float(post.get("min_experience_years") or 0)
    if need > 0 and not any(n is not None and abs(n - need) < 1e-6 for _, n, _ in items):
        ev = next((c["evidence"] for c, _, _ in items), "")
        rows.append({"요건": f"경력 {need:g}년 이상", "구분": "필수", "유형": "경력", "내 프로필": f"경력 {mine:g}년",
                     "판정": "충족" if mine >= need else "미충족", "공고 근거": ev})
    seen = set()
    for c, n, ok in items:
        if c.get("evidence") in seen:
            continue
        seen.add(c.get("evidence"))
        label = f"경력 {n * 12:g}개월 이상" if n is not None and n < 1 else (f"경력 {n:g}년 이상" if n is not None else c["name"])
        rows.append({"요건": label, "구분": c["_req"], "유형": "경력", "내 프로필": f"경력 {mine:g}년",
                     "판정": "확인 필요" if ok is None else ("충족" if ok else "미충족"), "공고 근거": c.get("evidence", "")})
    for c in g["필수"] + g["선택필수"]:
        t = c.get("type", "")
        if t in ("자격증", "경력", "기술", "직무지식"):
            continue
        hit = _completed_hit(c["name"], prof)
        rows.append({"요건": c["name"], "구분": c.get("_req") or ("필수" if c in g["필수"] else "선택필수"), "유형": t,
                     "내 프로필": f"이수: {hit}" if hit else "입력 없음",
                     "판정": "충족" if hit else "확인 필요", "공고 근거": c.get("evidence", "")})
    for m in (stk or {}).get("required", []):
        if m.get("requirement") not in ("필수", "선택필수") or not m.get("it", True):
            continue
        held = m.get("canonical") in (stk or {}).get("held", []) or m.get("raw") in (stk or {}).get("held", [])
        rows.append({"요건": m.get("canonical") or m.get("raw"), "구분": m["requirement"], "유형": "기술 스택",
                     "내 프로필": "보유" if held else "보유 스택에 없음", "판정": "충족" if held else "미충족",
                     "공고 근거": m.get("evidence", "")})
    return rows


def recommend(posting_id, profile=None):
    """공고 1건 → 추천 1건 (화면·에이전트·데모가 모두 이 함수를 씁니다)"""
    prof = _profile(profile)
    req = get_requirements(posting_id, prof.get("position"))
    if not req["ok"]:
        return req
    g = req["groups"]
    warnings = []
    post = req["posting"]
    if post.get("it_relevant") is False:
        warnings.append("IT 직무가 아닌 공고입니다.")
    if len(req["positions"]) > 1:
        warnings.append(f"모집 분야가 {len(req['positions'])}개예요. 지금은 '{req['position']}' 분야 기준입니다.")
    need_exp = float(post.get("min_experience_years") or 0)
    exp_ok = float(prof.get("experience_years") or 0) >= need_exp
    if need_exp > 0 and not exp_ok:
        warnings.append(f"경력 요건 {need_exp:g}년 이상인 공고입니다 (입력하신 경력 {float(prof.get('experience_years') or 0):g}년).")
    stk = stacks.for_posting(posting_id, req["position"], prof.get("stacks"), prof.get("regions"))
    stack_names = {x["raw"] for x in stk["required"]}
    if stk["total_weeks"] > prof.get("weeks", 24):
        warnings.append(f"요구 기술 스택을 모두 익히려면 약 {stk['total_weeks']}주가 걸려요 (준비 가능 {prof.get('weeks')}주) "
                        "— 필수·선택필수 스택과 학습 순서 앞쪽부터 준비하세요.")
    exp_ok_ev = {c.get("evidence") for c, n, ok in _exp_items(g, prof) if ok}
    for c in g["필수"] + g["선택필수"]:
        if c["type"] not in ("자격증", "경력") and c["name"] not in stack_names \
                and not _completed_hit(c["name"], prof) and c.get("evidence") not in exp_ok_ev:   # 기술 스택은 'stacks', 이수한 교육은 충족으로 처리
            warnings.append(f"자격증 외 요건 확인: {c['name']} — {c['evidence'][:60]}")

    def is_cert(c):
        return c["type"] == "자격증"

    def held(name):
        return catalog.canonical(name) in prof["held"]

    # 자격증 요건을 '그룹'으로 묶음: 그룹마다 1개만 있으면 됨
    #  - 선택필수: 모집 분야 안에서 하나의 그룹 (등급별로 나열돼도 '이 중 1개')
    #  - 필수인데 3개 이상이거나 근거 문장이 같으면 → 목록형('이 중 1개')으로 해석
    def by_evidence(items):
        groups = {}
        for c in items:
            groups.setdefault(c.get("evidence", ""), []).append(c)
        return list(groups.values())

    alt_items = [{**c, "_req": "선택필수"} for c in g["선택필수"] if is_cert(c)]
    alt_groups = [alt_items] if alt_items else []
    must_items = [{**c, "_req": "필수"} for c in g["필수"] if is_cert(c)]
    if len(must_items) >= 3:
        alt_groups.append(must_items)
        must_items = []
        warnings.append("추출 결과는 '필수'인 자격증이 3개 이상이라 '이 중 1개'로 해석했어요 — 공고 원문 확인")
    else:
        for grp in by_evidence(must_items):
            if len(grp) >= 2:
                alt_groups.append(grp)
                must_items = [c for c in must_items if c not in grp]

    # 선택필수 안에 '경력'이 함께 있으면 (예: 기사·산업기사 또는 관련 경력 6개월) 경력으로도 충족
    exp_alt = [(c, n) for c, n, ok in _exp_items(g, prof) if c["_req"] == "선택필수" and ok]
    picks, alt_held_all = [], []
    if exp_alt and alt_items:
        c, n = exp_alt[0]
        alt_held_all.append(f"경력 {float(prof.get('experience_years') or 0):g}년 (요건: {c.get('evidence', '')[:30]})")
        alt_groups = [grp for grp in alt_groups if grp is not alt_items]
    for grp in alt_groups:
        have = [c["name"] for c in grp if held(c["name"])]
        if have:
            alt_held_all += have
            continue
        ranked = sorted((_score_cert(c["name"], prof) for c in grp), key=lambda x: (-x["score"], x.get("prep_weeks", 99)))
        picks.append({"pick": ranked[0], "alternatives": ranked[1:3], "size": len(grp)})
    alt_held = alt_held_all
    top_pick = picks[0]["pick"] if picks else None
    alternatives = picks[0]["alternatives"] if picks else []
    alt = alt_groups[0] if alt_groups else []

    # 필수 자격증(단독): 모두 필요
    must_missing = [_score_cert(c["name"], prof) for c in must_items if not held(c["name"])]

    # ③ 우대 자격증: 상위 2개만 '여유 있으면'
    pref = sorted((_score_cert(c["name"], prof) for c in g["우대"] if is_cert(c)
                   and catalog.canonical(c["name"]) not in prof["held"]), key=lambda x: -x["score"])[:2]

    targets = [(m["canonical"], "필수 자격증") for m in must_missing]
    for pk in picks:
        targets.append((pk["pick"]["canonical"], f"인정 자격증 {pk['size']}종 중 1개만 있으면 지원 가능 — 가장 효율적인 선택"))
    path, total = build_path(targets, prof)

    # 다음 단계(성장): 관심 분야에 맞는 상위 자격증
    next_step = None
    base = top_pick["canonical"] if top_pick else (catalog.canonical(alt_held[0]) if alt_held else None)
    if base:
        e = catalog.find(base)
        for it in prof["interests"]:
            if e and it in e.get("next", {}):
                next_step = {"name": e["next"][it], "why": f"'{it}' 분야로 성장하기 위한 다음 단계"}
                break

    if not exp_ok:
        verdict = f"경력 {need_exp:g}년 요건 미충족"
    else:
        n = len(must_missing) + len(picks)
        others = [c for c in g["필수"] + g["선택필수"] if c["type"] not in ("자격증", "경력")
                  and c["name"] not in stack_names and not _completed_hit(c["name"], prof)
                  and c.get("evidence") not in exp_ok_ev]
        need_stack = [m for m in stk["missing"] if m["requirement"] in ("필수", "선택필수")]
        if n and need_stack:
            verdict = f"자격증 {n}개 · 기술 스택 {len(need_stack)}개 준비 후 지원 가능"
        elif need_stack:
            verdict = f"기술 스택 {len(need_stack)}개 준비 후 지원 가능"
        elif n:
            verdict = f"자격증 {n}개 취득 후 지원 가능"
        elif others:
            verdict = f"자격증 요건 충족 · 기타 요건 {len(others)}개 확인"
        else:
            verdict = "지금 지원 가능"

    targets_all = [pk["pick"] for pk in picks] + must_missing
    study = [courses.for_cert(x["canonical"], 3) for x in targets_all]
    links = [courses.resources_for(x["canonical"]) for x in targets_all]
    training = [courses.training_for(x["canonical"], prof.get("regions"), exclude=prof.get("completed")) for x in targets_all]
    cert_ev = []
    for x in targets_all:          # 진행 중 공고에 없으면 추출된 전체 공고 기준으로
        ev = cert_evidence(x["canonical"])
        cert_ev.append(ev if ev["n"] else cert_evidence(x["canonical"], ongoing_only=False))
    must_groups = [[m] for m in must_items]
    checks = profile_check(g, post, prof, stk, ([alt_items] if alt_items else []) + [x for x in alt_groups if x is not alt_items] + must_groups)
    if exp_alt:   # 인정 자격증 그룹은 경력으로 대신 충족
        for r in checks:
            if r["유형"] == "자격증" and r["구분"] == "선택필수" and r["판정"] == "미충족":
                r["판정"], r["내 프로필"] = "충족", "자격증 대신 경력으로 충족"

    return {
        "ok": True,
        "courses": study,
        "cert_evidence": cert_ev,
        "profile_check": checks,
        "links": links,
        "training": training,
        "posting": post,
        "profile": {k: v for k, v in prof.items() if k != "held"},
        "verdict": verdict,
        "satisfied_by": alt_held,
        "top_pick": top_pick,
        "alternatives": alternatives,
        "must_missing": must_missing,
        "preferred": pref,
        "path": path,
        "total_weeks": total,
        "next_step": next_step,
        "alt_count": len(alt),
        "positions": req["positions"], "position": req["position"],
        "picks": picks,
        "warnings": warnings,
        "stacks": stk,
        "evidence": sorted({c["evidence"] for c in g["필수"] + g["선택필수"] if c.get("evidence")}),
        "assumptions": ASSUMPTIONS,
    }


def summary_text(rec):
    """추천 결과 → 사람이 읽는 문장 (모의 모드 에이전트 답변·로그용)"""
    if not rec.get("ok"):
        return rec.get("error", "추천을 만들 수 없습니다.")
    p = rec["posting"]
    lines = [f"[{p['company']}] {p['title']}", f"판정: {rec['verdict']}"]
    if rec.get("profile_check"):
        lines.append("입력하신 프로필 기준:")
        for r in rec["profile_check"]:
            ev = f" — 공고: \"{r['공고 근거'][:50]}\"" if r.get("공고 근거") else ""
            lines.append(f"  · {r['요건']} [{r['구분']}] → {r['판정']} ({r['내 프로필']}){ev}")
    for x in rec["satisfied_by"]:
        if x.startswith("경력"):
            lines.append(f"입력하신 {x.split(' (')[0]}이 공고의 경력 요건을 충족해 인정 자격증 없이도 지원 가능합니다.")
        else:
            lines.append(f"이미 인정 자격증({x})을 갖고 있어 자격 요건을 충족합니다.")
    for pk in rec.get("picks", []):
        t = pk["pick"]
        lines.append(f"추천 자격증({pk['size']}종 중 1개): {t['canonical']} — " + "; ".join(t["reasons"][:3]))
        ev = next((x for x in rec.get("cert_evidence", []) if x["cert"] == t["canonical"]), None)
        if ev and ev["n"]:
            c = ev["counts"]
            lines.append(f"  근거: {ev['scope']} {ev['total']}건 중 {ev['n']}건이 요구 (필수 {c['필수']} · 선택필수 {c['선택필수']} · 우대 {c['우대']})")
        if pk["alternatives"]:
            lines.append("  대안: " + ", ".join(a["canonical"] for a in pk["alternatives"]))
    for m in rec.get("must_missing", []):
        lines.append(f"필수 자격증: {m['canonical']}")
    if rec["path"]:
        lines.append(f"학습경로 (총 약 {rec['total_weeks']}주):")
        for s in rec["path"]:
            lines.append(f"  {s['order']}. {s['name']} {s['phase']} — {s['weeks']}주 ({s['why']})")
    for c in rec.get("courses", []):
        if c["courses"]:
            lines.append(f"{c['cert']} 준비에 도움 되는 교과목: " + ", ".join(x["교과목"] for x in c["courses"]))
    stk = rec.get("stacks") or {}
    if stk.get("held"):
        lines.append("이미 가진 요구 스택: " + ", ".join(stk["held"]))
    if stk.get("path"):
        lines.append(f"기술 스택 학습 순서 (총 약 {stk['total_weeks']}주):")
        for s in stk["path"]:
            m = next(x for x in stk["missing"] if x["canonical"] == s["name"])
            ncs = ", ".join(n["unit_name"] for n in m["ncs"][:2])
            lines.append(f"  {s['order']}. {s['name']} [{s['requirement']}] — {s['weeks']}주" + (f" (관련 NCS: {ncs})" if ncs else ""))
    if rec["next_step"]:
        lines.append(f"다음 단계: {rec['next_step']['name']} ({rec['next_step']['why']})")
    for w in rec["warnings"]:
        lines.append(f"주의: {w}")
    return "\n".join(lines)


def save_plan(rec, student=None):
    """추천 결과 저장 — 에이전트 도구. 저장소는 recommend/store.py 가 정함 (.env PLAN_STORE=rds 면 RDS, 아니면 로컬 파일)"""
    from . import store
    return store.save(_plan_item(rec, student))


def _plan_item(rec, student=None):
    return {
        "student": student or rec.get("profile", {}).get("student", "demo"),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "posting_id": rec["posting"]["id"], "verdict": rec["verdict"],
        "top_pick": rec["top_pick"]["canonical"] if rec.get("top_pick") else None,
        "path": [f"{s['name']} {s['phase']}" for s in rec["path"]], "total_weeks": rec["total_weeks"],
        "profile": {k: rec.get("profile", {}).get(k) for k in ("certs", "stacks", "major_it", "experience_years",
                                                             "completed", "interests", "weeks", "regions")},
    }
