# recommend/ — 추천 에이전트 (담당: 김현진)

공고 1건 + 사용자 프로필 → 지원 가능 여부 · 추천 자격증 1개 · 주 단위 학습경로

## 실행
- 터미널 데모: `python -m recommend.demo JA-267524`
- 화면(3탭): `python -m streamlit run app.py`
- 교육과정 받기: `python -m recommend.fetch_courses` → data/processed/ncs_courses.json (.env 의 DATA_GO_KR_API_KEY 필요, 없으면 샘플 사용)

## 입력 (팀원 결과물, data/processed/)
| 파일 | 만든 사람 | 쓰는 곳 |
|---|---|---|
| postings_jobalio.json | 강민경 | 공고 기본 정보·원문 |
| extractions.json | 조호성 | competencies(name·type·requirement·evidence), entry_level_ok |
| skill_ncs_map.json | 신성훈 | 자격증 → NCS 능력단위 (추천 이유에 표시) |
| demand_overall.csv | 신성훈 | 자격증을 인정하는 공고 비율 (추천 점수에 반영) |

## 파일
- `engine.py` 핵심 규칙: 선택필수(인정 자격증 중 1개)면 가장 효율적인 1개 + 대안 2개, 필수는 모두, 선행 자격증 포함 순서
- `cert_catalog.json` 자격증 정보 (신입 취득 현실성·준비 기간은 **팀 가정값** → 발표 전 Q-Net 확인)
- `agent.py` Claude 도구 호출 루프 (도구: get_requirements · recommend_path · save_plan), 모의 모드는 고정 순서
- `courses.py` 추천 자격증 → NCS 교과목 연결 (1순위: 교과목의 취득가능자격증 일치, 2순위: 내용 키워드)
- `guardrail.py` 입구·출구 검사: 블라인드 채용(개인 속성 판단 요청) · 합격 보장 · 프롬프트 공격 차단, 이메일 가리기

## 아직 남은 것 (통합 단계)
- AgentCore: `infra/deploy_agentcore.py` 로 배포·호출 완료 (agentcore CLI 방식인 `agentcore_app.py` 는 참고용)
- Cognito 로그인: 연결 완료 (.env 값은 팀장이 공유)
- save_plan: RDS 스키마 맞춤 완료, 화면 저장은 로컬 폴백 (RDS 가 프라이빗 서브넷이라 PC 에서 직접 연결 불가)
- 실제 모드 가드레일: .env 에 GUARDRAIL_ID / GUARDRAIL_VERSION (만들기는 팀 계정 예산 확인 후)
- Knowledge Base: 자격증 카탈로그·교육과정 문서를 KB에 넣어 검색으로 교체


## 11/17 추가 (멘토링 반영)
- 프로필: 실무 경력(년) · 이수한 교육·훈련과정 추가 → 공고 경력 요건 판정, 이수 과정은 훈련과정 추천에서 제외, 교육 이수 요건은 충족 처리
- 🙋 '입력하신 프로필 기준 요건 점검' 표: 요건별 충족/미충족/확인 필요 + 공고 근거 문장 (AI 상담 답변에도 포함)
- 추천 자격증마다 공고 근거: 진행 중 공고 중 요구 건수(필수·선택필수·우대) + 공고 목록·근거 문장·원문 링크
- 공고 목록 기본값: 진행 중 공고만 (마감일 표시, 원본 공고 close_at 기준)
- `store.py`: save_plan 저장소. `.env` 에 `PLAN_STORE=rds` + `RDS_HOST/RDS_PORT/RDS_DB/RDS_USER/RDS_PASSWORD` 넣으면 RDS plans 테이블에 저장 (실패 시 로컬로 대신 저장). 테이블 예시는 `store.PLANS_DDL` — **성훈님 테이블 정의에 맞춰 수정**
- `auth.py`: `.env` 에 `COGNITO_CLIENT_ID`(+`COGNITO_REGION`) 있으면 Streamlit 로그인 화면 (시크릿 없는 앱 클라이언트, USER_PASSWORD_AUTH)
- `agentcore_app.py` + `requirements-agentcore.txt`: Bedrock AgentCore Runtime 배포용 진입점 (서울 리전 지원). 배포 명령은 파일 상단 참고
