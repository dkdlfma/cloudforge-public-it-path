# infra/ — AWS 배포 (담당: 강민경)

모든 명령은 `C:\aws_edu` 에서, 가상환경을 켠 뒤(`.venv\Scripts\activate`) 실행합니다.
먼저 `aws sso login --profile kosa`. 리소스 이름에는 모두 `.env` 의 `TEAM`(team04-kang)이 붙습니다.

## 배포 순서 (처음 한 번)

| 순서 | 명령 | 만드는 것 | 담당 |
|---|---|---|---|
| 0 | `python pipeline/upload_s3.py` | S3 버킷 + processed/ 데이터 | 강민경 |
| 1 | `python infra/deploy_collect.py` | 수집 Lambda (잡알리오 API, 인증키는 SSM SecureString) | 강민경 |
| 2 | `python pipeline/extract_lambda_deploy.py` | SQS(+DLQ) + 추출 Lambda (Bedrock) | 조호성 |
| 3 | `python infra/deploy_pipeline.py` | **Step Functions** 파이프라인 + 매일 03시 EventBridge 규칙 | 강민경 |
| 4 | `python infra/deploy_api.py` | **Cognito** 사용자 풀 + **API Gateway**(HTTP API, JWT) + 추천 Lambda | 강민경 |
| 5 | `python infra/deploy_rds.py --account-id <계정번호> --apply ...` | RDS PostgreSQL (README_NCS_RDS.md 참고) | 신성훈 |
| 6 | `python infra/deploy_agentcore.py` | AgentCore Runtime 상담 에이전트 (zip 배포, Docker 불필요) | 강민경 · 김현진 |

## 동작 확인 (발표 증빙 캡처용)

```powershell
python infra/deploy_pipeline.py --run          # 파이프라인 1회 실행 → 단계별 진행 + 결과 요약
python infra/deploy_pipeline.py --status       # 매일 실행 규칙 · 최근 실행 기록
python infra/deploy_api.py --create-user demo  # 로그인 계정 만들기
python infra/deploy_api.py --test              # 401 확인 → 로그인 → 공고 목록 → 추천 1건
python infra/deploy_api.py --env               # Streamlit 로그인용 .env 값 (COGNITO_CLIENT_ID 등)
python infra/deploy_agentcore.py --invoke "이 공고 준비하려면 뭐부터 해야 해요?"   # AgentCore 상담
python infra/deploy_agentcore.py --invoke "지방대 출신이면 불리한가요?"           # 가드레일 차단
```

캡처할 화면: 콘솔 → Step Functions → `aws-edu-team04-kang-pipeline` → 실행 그래프(전부 초록색),
`--test` 결과 터미널, 콘솔 → API Gateway → `aws-edu-team04-kang-api` → 경로·권한 부여자.

## 파이프라인 흐름 (pipeline-stack.yaml)

```
EventBridge(매일 03:00 KST) → Step Functions
  Collect         수집 Lambda: 새 공고 추가 + 진행 여부 갱신
  SelectTargets   진행 중인데 Bedrock 추출이 없는 공고 (하루 최대 20건, --max-per-run 으로 변경)
  SendToQueue     공고마다 SQS 메시지 {"id": ...}  → 추출 Lambda 가 대기열에서 꺼내 처리 (동시 2개)
  CheckQueue      1분마다 대기열 확인, 비면 다음 (최대 30분)
  MergeExtractions  추출 Lambda {"action":"merge"} → processed/extractions.json
  Analyze         NCS 매칭(match_ncs.py) + 수요 분석(analyze_demand.py) → processed/ 갱신
```
파이프라인을 배포하면 수집 스택의 단독 매일 실행 규칙은 꺼집니다 (하루 두 번 수집 방지).
팀원 PC 로 최신 결과 받기: `python pipeline/upload_s3.py --download`

## 추천 API (api-stack.yaml)

- `GET /postings` (진행 중 공고, `?all=1` 이면 전체) · `POST /recommend`
- 헤더 `Authorization: <Cognito ID 토큰>` 없으면 401
- 본문 예: `{"posting_id": "JA-307033", "profile": {"certs": ["정보처리기사"], "stacks": ["Java"], "experience_years": 0, "completed": [], "interests": ["개발"], "weeks": 24}}`
- 회원 가입은 막혀 있고 `--create-user` 로만 계정 생성

## 보안 · 운영 (선택 항목 반영 현황)

- IAM 최소 권한: 함수마다 전용 역할, 필요한 S3 경로·대기열·함수만 허용
- KMS: 공공데이터 인증키를 SSM SecureString(KMS 암호화)으로 보관, 수집 Lambda 만 복호화
- CloudWatch: 모든 Lambda · API 접근 로그 · Step Functions 오류 로그 (14일 보관)
- CloudWatch 경보 3개: 파이프라인 실행 실패 · 추출 실패 대기열(DLQ)에 메시지 · 보조 Lambda 오류 (`--status` 로 상태 확인)
- API 호출 제한: 초당 10회 (순간 20회)

## 비용 · 정리 (제출 후)

```powershell
python infra/deploy_agentcore.py --delete
python infra/deploy_api.py --delete
python infra/deploy_pipeline.py --delete
python pipeline/extract_lambda_deploy.py --delete
python infra/deploy_collect.py --delete
```
RDS 는 켜 두기만 해도 요금이 나갑니다 → 콘솔 CloudFormation 에서 `team04-kang-rds` 스택 삭제 (성훈님과 확인 후).
Lambda·Step Functions·SQS·API Gateway·Cognito 는 쓴 만큼만 과금되고, 비용 대부분은 Bedrock 호출(추출)입니다.
