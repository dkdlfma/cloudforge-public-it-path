"""공공기관 IT 취준생 학습경로 추천 — Streamlit 화면 (담당: 김현진)
실행: python -m streamlit run app.py"""
import re
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from recommend import agent, auth, catalog, courses, data_io, engine, stacks  # noqa: E402
from labutil import config  # noqa: E402

st.set_page_config(page_title="공공기관 IT 학습경로", page_icon="🧭", layout="wide")
st.title("🧭 공공기관 IT 직무 학습경로 추천")
st.caption("공고(잡알리오) → 역량 추출(Claude) → NCS 매칭·수요 분석 → **추천** · "
           + ("🟡 모의 모드 (AWS 호출 없음)" if config.BEDROCK_MOCK else "🟢 실제 Bedrock 모드"))

login_user = auth.gate(st)   # .env 에 COGNITO_CLIENT_ID 가 있을 때만 로그인 화면 (없으면 그냥 통과)

# ---------------------------------------------------------------- 사이드바: 공고 + 내 프로필
postings = data_io.list_postings()
if not postings:
    st.error("data/processed/extractions.json 이 없습니다. 조호성님 추출 결과를 먼저 받아 주세요.")
    st.stop()

with st.sidebar:
    st.header("① 목표 공고")
    all_regions = sorted({r for p in postings for r in p["regions"]})
    want_regions = st.multiselect("희망 지역 (비우면 전체)", all_regions, placeholder="지역 선택")
    all_orgs = sorted({p["company"] for p in postings if p["company"]})
    want_orgs = st.multiselect("희망 기관 (비우면 전체)", all_orgs, placeholder="기관 선택")
    ongoing_only = st.checkbox("진행 중 공고만 (마감 전)", value=True)
    newbie_only = st.checkbox("신입 지원 가능한 공고만", value=True)
    it_only = st.checkbox("IT 직무만 (전기·통신 현장직 제외)", value=True)
    shown = [p for p in postings
             if (not want_regions or set(want_regions) & set(p["regions"]))
             and (not want_orgs or p["company"] in want_orgs)
             and (not ongoing_only or p["ongoing"])
             and (not newbie_only or p["entry_level_ok"])
             and (not it_only or p["it_relevant"] is True)]
    if not shown:
        st.warning("조건에 맞는 공고가 없어요. 지역·기관 조건을 줄이거나 '진행 중 공고만'을 꺼 보세요.")
        st.stop()
    st.caption(f"추출된 공고 {len(postings)}건 중 {len(shown)}건")
    def _deadline(p):
        if not p["ongoing"]:
            return " · 마감"
        return f" · ~{p['close_at'][5:]}" if p["close_at"] else ""
    labels = {p["id"]: f"{p['company']} · {p['title'][:22]}" + ("" if p["entry_level_ok"] else " (경력)") + _deadline(p)
              + f" · {p['id']}" for p in shown}   # 제목을 자르면 원공고·재공고가 같은 이름이 되므로 공고 ID 를 붙임
    ids = [p["id"] for p in shown]
    default = ids.index("JA-267524") if "JA-267524" in ids else 0
    pid = st.selectbox("공고 선택", ids, index=default, format_func=lambda x: labels[x])
    pos_list = engine.positions(pid)
    position = st.selectbox("모집 분야", pos_list) if len(pos_list) > 1 else (pos_list[0] if pos_list else None)

    st.header("② 내 프로필")
    st.caption("블라인드 채용 원칙에 따라 학교·성별·나이는 묻지 않습니다.")
    student = login_user or st.text_input("별칭 (저장용)", value="demo")
    major_it = st.radio("전공", ["IT 관련 전공", "비전공"], horizontal=True) == "IT 관련 전공"
    experience = st.number_input("관련 실무 경력 (년)", min_value=0.0, max_value=30.0, value=0.0, step=0.5)
    certs = st.multiselect("보유 자격증", catalog.names(), placeholder="없으면 비워 두기")
    my_stacks = st.multiselect("다룰 줄 아는 기술 스택", stacks.names(), placeholder="예: Java, MySQL")
    interests = st.multiselect("관심 분야", ["개발", "DB", "보안", "데이터", "네트워크", "운영", "관리"],
                               placeholder="눌러서 선택 (여러 개 가능)")
    completed = st.multiselect("이수한 교육·훈련과정 (고용24 과정명)", courses.training_titles(),
                               placeholder="검색해서 선택 (없으면 비워 두기)")
    completed_etc = st.text_input("그 밖에 이수한 교육 (쉼표로 구분)", placeholder="예: 건설업 기초 안전보건교육")
    completed = completed + [x.strip() for x in completed_etc.split(",") if x.strip()]
    weeks = st.slider("준비 가능 기간 (주)", 4, 52, 24, step=4)
    profile = {"student": student, "major_it": major_it, "experience_years": experience, "completed": completed,
               "certs": certs, "stacks": my_stacks, "interests": interests, "weeks": weeks,
               "position": position, "regions": want_regions}

tab1, tab2, tab3 = st.tabs(["📋 공고 분석", "🧭 내 학습경로", "💬 AI 상담"])

# ---------------------------------------------------------------- 탭 1: 공고 분석
with tab1:
    st.info("이 탭은 **공고가 요구하는 것**이라 프로필을 바꿔도 그대로예요. 내 조건에 맞춘 추천은 **🧭 내 학습경로** 탭에서 보세요.")
    req = engine.get_requirements(pid, position)
    p = req["posting"]
    st.subheader(f"{p['company']} — {p['title']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("경력 구분", p["career"] or "-")
    c2.metric("고용형태", p["hire_type"] or "-")
    c3.metric("신입 지원", "가능" if p["entry_level_ok"] else "어려움")
    _close, _on = data_io.close_info(pid)
    c4.metric("마감일", (p["close_at"] or "-") + ("" if _on else " (마감)"))
    st.caption(f"근무지: {p['region'] or '-'} · 직무 분야: {', '.join(p['job_areas']) or '-'}"
               + (f" · [원문 공고]({p['url']})" if p["url"] else ""))

    g = req["groups"]
    m1, m2, m3 = st.columns(3)
    m1.metric("필수", len(g["필수"]))
    m2.metric("선택필수 (이 중 1개)", len(g["선택필수"]))
    m3.metric("우대", len(g["우대"]))

    rows = []
    for level in ("필수", "선택필수", "우대"):
        for c in g[level]:
            ncs = [u["unit_name"] for u in data_io.ncs_map().get(c["name"], [])][:2]
            rows.append({"구분": level, "역량": c["name"], "유형": c["type"],
                         "NCS 연결": ", ".join(ncs) or "-", "근거 문장": c["evidence"]})
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.info("추출된 요구 역량이 없습니다.")
    with st.expander("공고 원문 보기"):
        st.text(data_io.posting(pid).get("text", "(원문 없음)")[:4000])

# ---------------------------------------------------------------- 탭 2: 내 학습경로
with tab2:
    rec = engine.recommend(pid, profile)
    st.subheader(f"판정: {rec['verdict']}")
    for w in rec["warnings"]:
        st.warning(w)
    if rec["satisfied_by"]:
        _exp = [x for x in rec["satisfied_by"] if x.startswith("경력")]
        _cert = [x for x in rec["satisfied_by"] if not x.startswith("경력")]
        if _cert:
            st.success(f"이미 인정 자격증({', '.join(_cert)})을 갖고 있어 자격 요건을 충족해요.")
        if _exp:
            st.success(f"입력하신 {_exp[0].split(' (')[0]}이 공고의 경력 요건을 충족해서, 인정 자격증 없이도 지원할 수 있어요.")

    if rec.get("profile_check"):
        st.markdown("### 🙋 입력하신 프로필 기준 요건 점검")
        mark = {"충족": "✅ 충족", "미충족": "❌ 미충족", "확인 필요": "⚠ 확인 필요"}
        st.dataframe([{**r, "판정": mark.get(r["판정"], r["판정"])} for r in rec["profile_check"]],
                     column_order=["판정", "요건", "구분", "유형", "내 프로필", "공고 근거"],
                     width="stretch", hide_index=True)
        st.caption("'공고 근거'는 공고 원문에서 AI가 찾은 문장이에요. 정확한 요건은 원문 공고로 확인하세요.")

    for i, pk in enumerate(rec["picks"]):
        t = pk["pick"]
        st.markdown(f"### 🎯 추천 자격증{'' if len(rec['picks']) == 1 else f' {i + 1}'}: **{t['canonical']}**")
        st.caption(f"인정 자격증 {pk['size']}종 중 **1개만** 있으면 돼요. 지금 조건에서 가장 효율적인 1개를 골랐습니다.")
        cols = st.columns(3)
        cols[0].metric("신입 취득 현실성", t.get("feasibility", "-"))
        cols[1].metric("예상 준비 기간", f"{t.get('prep_weeks', '-')}주")
        cols[2].metric("추천 점수", t["score"])
        st.markdown("**추천 이유**\n" + "\n".join(f"- {r}" for r in t["reasons"]))
        ev = next((x for x in rec.get("cert_evidence", []) if x["cert"] == t["canonical"]), None)
        if ev and ev["n"]:
            cnt = ev["counts"]
            st.markdown(f"**공고 근거** — {ev['scope']} {ev['total']}건 중 **{ev['n']}건**이 이 자격증을 요구 "
                        f"(필수 {cnt['필수']} · 선택필수 {cnt['선택필수']} · 우대 {cnt['우대']})")
            st.dataframe([{"기관": r["company"], "공고": r["title"][:30], "구분": r["requirement"], "근거 문장": r["evidence"],
                           "마감일": r["close_at"] or "-", "원문": r["url"] or None} for r in ev["postings"]],
                         column_config={"원문": st.column_config.LinkColumn("원문", display_text="보기")},
                         width="stretch", hide_index=True)
        e = catalog.find(t["canonical"]) or {}
        if t.get("eligibility"):
            st.caption(f"응시자격: {t['eligibility']} · 시행: {t.get('issuer', '-')}"
                       + (f" · 시험: {e['exam']}" if e.get("exam") else "")
                       + (" · ✅ 2026-11-15 확인" if e.get("checked") else " · ⚠ 팀 가정값"))
        if pk["alternatives"]:
            st.markdown("**대안**")
            st.dataframe([{"자격증": a["canonical"], "현실성": a.get("feasibility", "-"), "준비(주)": a.get("prep_weeks", "-"),
                           "점수": a["score"], "이유": "; ".join(a["reasons"][:2])} for a in pk["alternatives"]],
                         width="stretch", hide_index=True)
    for m in rec["must_missing"]:
        st.markdown(f"### 📌 필수 자격증: **{m['canonical']}**")
        st.caption("; ".join(m["reasons"][:3]))

    if rec["path"]:
        st.markdown(f"### 🗓️ 학습 순서 (총 약 {rec['total_weeks']}주)")
        for s in rec["path"]:
            st.markdown(f"**{s['order']}. {s['name']} {s['phase']}** — {s['weeks']}주 (누적 {s['cum_weeks']}주)  \n"
                        f"<span style='color:gray'>{s['why']}</span>", unsafe_allow_html=True)
    stk = rec.get("stacks") or {}
    if stk.get("required"):
        st.markdown("### 🧰 요구 기술 스택")
        if stk["held"]:
            st.success("이미 가진 스택: " + ", ".join(stk["held"]))
        if stk["path"]:
            st.markdown(f"**부족한 스택 학습 순서 (총 약 {stk['total_weeks']}주 · 팀 가정값)**")
            by = {m["canonical"]: m for m in stk["missing"]}
            for s_ in stk["path"]:
                m = by[s_["name"]]
                after = f" · {', '.join(s_['after'])} 다음에" if s_["after"] else ""
                with st.expander(f"{s_['order']}. {s_['name']} [{s_['requirement']}] — {s_['weeks']}주 (누적 {s_['cum_weeks']}주){after}"):
                    st.caption(f"공고 표기: {m['raw']} · 분야: {m['category']}" + (f" · 근거: {m['evidence'][:80]}" if m["evidence"] else ""))
                    if m["ncs"]:
                        st.markdown("관련 NCS 능력단위: " + ", ".join(f"{n['unit_name']} ({n['score']:.2f})" for n in m["ncs"]))
                    if m["training"]:
                        st.dataframe(m["training"], column_config={"링크": st.column_config.LinkColumn("고용24", display_text="보기")},
                                     width="stretch", hide_index=True)
            st.caption(f"스택 ↔ NCS·훈련과정 연결: 임베딩 유사도 ({stk.get('backend') or '매칭 파일 없음'}) — "
                       "pipeline/embed_stacks.py 결과. 숫자는 코사인 유사도입니다.")
        if stk["other"]:
            st.caption("사무·현장 요건: " + ", ".join(f"{x['canonical']} [{x['requirement']}]" for x in stk["other"]))

    if rec.get("links"):
        st.markdown("### 📎 시험 정보 · 학습처")
        for l in rec["links"]:
            if l["official"]:
                st.markdown(f"**{l['cert']}** — " + " · ".join(f"[{x['name']}]({x['url']}) ({x['what']})" for x in l["official"]))
        st.caption("함께 보면 좋은 곳: " + " · ".join(f"[{x['name']}]({x['url']})" for x in rec["links"][0]["common"])
                   + " — 시험 일정·응시자격은 반드시 공식 사이트에서 확인하세요.")
    if rec.get("training") and rec["training"][0]["available"]:
        st.markdown("### 🏫 내일배움카드 훈련과정 (고용24)")
        for t in rec["training"]:
            if t["courses"]:
                st.markdown(f"**{t['cert']}** 대비")
                if t.get("local_ok") is False:
                    st.caption("희망 지역에는 관련 과정이 없어 다른 지역 과정을 보여드려요.")
                st.dataframe([{"과정명": x["과정명"], "훈련기관": x["훈련기관"], "시작일": x["시작일"], "종료일": x["종료일"],
                               "실제훈련비": x["실제훈련비"], "주소": x["주소"], "근거": x["근거"],
                               "링크": x["링크"]} for x in t["courses"]],
                             column_config={"링크": st.column_config.LinkColumn("고용24", display_text="보기")},
                             width="stretch", hide_index=True)
            else:
                st.caption(f"{t['cert']}: 관련 훈련과정을 찾지 못했어요.")
        st.caption(f"출처: 고용24 국민내일배움카드 훈련과정 API (받아 둔 {rec['training'][0].get('total', 0)}건 기준)")
    if rec.get("courses"):
        st.markdown("### 📚 함께 들으면 좋은 NCS 교과목")
        for c in rec["courses"]:
            if c["courses"]:
                st.markdown(f"**{c['cert']}** 준비")
                st.dataframe([{k: x[k] for k in ("교과목", "학과", "학교구분", "시수", "근거", "출처")} for x in c["courses"]],
                             width="stretch", hide_index=True)
            else:
                st.caption(f"{c['cert']}: 연결된 교과목을 찾지 못했어요.")
        st.caption("출처: " + rec["courses"][0]["source"])
    if rec["next_step"]:
        st.info(f"다음 단계: **{rec['next_step']['name']}** — {rec['next_step']['why']}")
    if rec["preferred"]:
        st.caption("여유가 있다면(우대): " + ", ".join(x["canonical"] for x in rec["preferred"]))

    if st.button("💾 이 학습경로 저장"):
        r = engine.save_plan(rec, student)
        where = "RDS" if r.get("store") == "rds" else f"outputs/plans.json, 누적 {r.get('count')}건"
        st.success(f"저장했어요 ({where}).")
        if r.get("warning"):
            st.warning(r["warning"])
    with st.expander("가정과 한계 (발표용)"):
        st.markdown("\n".join(f"- {a}" for a in rec["assumptions"]))

# ---------------------------------------------------------------- 탭 3: AI 상담 (가드레일)
with tab3:
    st.caption("질문 → 🛡️ 입구 검사 → 에이전트(도구: 요구사항 조회 · 학습경로 계산 · 저장) → 🛡️ 출구 검사 → 답변")
    if "chat" not in st.session_state:
        st.session_state.chat = []
    ex1, ex2, ex3 = st.columns(3)
    sample = None
    if ex1.button("예시: 정상 상담"):
        sample = "이 공고 준비하려면 뭐부터 해야 해요?"
    if ex2.button("예시: 블라인드 위반 질문"):
        sample = "지방대 출신이면 공기업 전산직에 불리한가요?"
    if ex3.button("예시: 합격 보장 질문"):
        sample = "정보처리기사 따면 합격 보장되나요?"

    for m in st.session_state.chat:
        with st.chat_message(m["role"]):
            st.markdown(re.sub(r"(?m)^(\s*)(\d+)\.", r"\1\2\\.", str(m["text"])).replace("\n", "  \n"))   # 줄바꿈 유지, "1." 목록 변환 방지
            if m.get("log"):
                with st.expander(f"🛡️ 검사 결과: {m['log']['stage']}"):
                    st.json(m["log"])

    q = st.chat_input("공고나 자격증에 대해 물어보세요") or sample
    if q:
        st.session_state.chat.append({"role": "user", "text": q})
        with st.spinner("추천을 만드는 중..."):
            answer, log = agent.guarded_chat(q, pid, profile)
        log_view = {"stage": log["stage"], "input": log["input"],
                    "tools": [t[0] for t in log.get("trace", [])], "output": log.get("output")}
        st.session_state.chat.append({"role": "assistant", "text": answer, "log": log_view})
        st.rerun()
    if st.session_state.chat and st.button("대화 지우기"):
        st.session_state.chat = []
        st.rerun()
