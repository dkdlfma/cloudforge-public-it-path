# 진행 중 공고 수요 분석과 RDS 연동

## 결과를 다시 만들기

`aws_edu`에서 기존 가상환경의 Python을 사용합니다. AWS SSO에 로그인한 뒤 팀 S3에서 공고와 추출 결과를 받은 다음 아래 순서대로 실행합니다.

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py -m pip install -r requirements.txt
& $py pipeline/upload_s3.py --download
& $py pipeline/match_ncs.py
& $py pipeline/analyze_demand.py
& $py pipeline/check_formats.py
& $py pipeline/load_rds.py --dry-run
```

매칭과 수요 집계 모두 기본값은 **한국 날짜 기준 진행 중 공고 + Bedrock 추출본**입니다. 두 스크립트의 `--as-of YYYY-MM-DD`로 기준일을 고정할 수 있고, `--include-closed`로 과거 공고까지 분석할 수 있습니다. 진행 여부는 공고 게시일과 마감일로 판정하고, 마감일을 읽을 수 없으면 원본의 `ongoing` 플래그를 사용합니다. 최신 추출본이 동봉된 파일보다 새로운지 여부는 팀 S3에서 확인하세요. 다운로드 스크립트는 로컬 수정 시각이 더 최근인 파일을 남깁니다.

- `skill_ncs_map.json`에는 자격증·기술·직무지식별 NCS 후보가 들어 있습니다. 기본 검색은 공식 정보통신 능력단위를 대상으로 하는 KOSA 모의 코사인 검색입니다.
- `ncs_match_coverage.json`에는 대상 공고마다 추출 유무와 역량별 `matched`(후보 있음), `no_candidate`(후보 없음), `query_unavailable`(질의 구성에 필요한 정보 부족), `outside_ncs_scope`(경력·전공 등 매칭 대상 외)가 들어 있습니다. `--coverage-output`으로 범위를 좁힌 실험의 점검 파일도 따로 저장할 수 있습니다.
- `demand_*.csv` 5개에는 전체·지역별·고용형태별·지역×고용형태별 수요와 추출 범위가 들어 있습니다. 수요율은 그룹의 Bedrock 추출 완료 공고 수를 분모로 씁니다.

동봉한 결과는 팀 S3의 공고 8,911건과 Bedrock 추출 78건을 2026-11-18(KST) 기준으로 연결해 만들었습니다. 진행 중 공고 29건 중 26건에 Bedrock 추출이 있습니다. 추출을 기다리는 `JA-306997`, `JA-307067`, `JA-307071`은 점검표에 표시했습니다. 추출 26건의 역량 225개를 확인했고, 자격증·기술·직무지식 137개 중 39개에 로컬 NCS 후보가 나왔습니다. 11월 17일 기준 동일 원본에는 진행 중 공고 32건과 Bedrock 추출 완료 공고 29건이 있었습니다.

## PostgreSQL 적재와 추천 저장

서울 리전에 `team04-kang-rds` 스택과 `team04-kang-rds-postgres` 인스턴스를 생성했습니다. `infra/rds-network.yaml`은 팀 VPC를 만든 `team04-kang-rds-network` 스택의 템플릿입니다. 이 스택은 프라이빗 서브넷 2개와, 팀 클라이언트 보안 그룹에서만 TCP 5432를 허용하는 DB 보안 그룹을 제공합니다. 서브넷·보안 그룹 ID는 해당 스택 출력에서 확인할 수 있습니다. DB는 PostgreSQL 17, Single-AZ, `db.t4g.micro`, gp3 20 GiB이고 저장소 암호화와 RDS 관리형 관리자 자격 증명을 사용합니다. [RDS VPC·서브넷](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html) · [RDS CloudFormation 명세](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-rds-dbinstance.html)

```powershell
$accountId = '팀 AWS 계정 번호'
$subnetA = '첫 번째 프라이빗 서브넷 ID'
$subnetB = '두 번째 프라이빗 서브넷 ID'
$dbSg = 'DB 보안 그룹 ID'
& $py infra/deploy_rds.py --profile kosa --account-id $accountId
& $py infra/deploy_rds.py --profile kosa --account-id $accountId --apply --subnet-ids $subnetA $subnetB --security-group-id $dbSg
```

적재 스크립트를 실행할 환경에만 PostgreSQL 드라이버를 설치합니다. RDS 스택 출력 `DbEndpoint`, `DbPort`, `DbName`, `DbSecretArn`의 값을 각각 `RDS_HOST`, `RDS_PORT`, `RDS_DB`, `RDS_SECRET_ARN`에 설정합니다. `RDS_USER`와 `RDS_PASSWORD`로 접속할 수도 있습니다. 기본 `RDS_SSLMODE=require`로 TLS를 사용합니다. `RDS_SSLMODE=verify-full`로 서버 인증서와 호스트를 검증하려면 RDS CA 번들의 경로를 `RDS_SSLROOTCERT`에 지정합니다. 적재 계정에는 테이블 갱신 권한이 필요합니다. 팀 클라이언트 보안 그룹에서 DB로 연결할 수 있습니다. 이 팀 VPC의 프라이빗 서브넷에서 `RDS_SECRET_ARN`을 사용하려면 실행 역할의 시크릿 조회 권한과 Secrets Manager VPC 엔드포인트가 필요합니다. [RDS PostgreSQL TLS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html) · [Secrets Manager VPC 엔드포인트](https://docs.aws.amazon.com/secretsmanager/latest/userguide/vpc-endpoint-overview.html)

```powershell
& $py pipeline/load_rds.py --dry-run --as-of 2026-11-18
& $py -m pip install 'psycopg2-binary>=2.9,<3'
& $py pipeline/load_rds.py --as-of 2026-11-18
```

새 공고로 다시 분석했다면 그때의 기준일을 사용합니다. 적재기는 `postings`, `competencies`, `ncs_map`, `demand`와 보조 테이블을 한 트랜잭션으로 갱신합니다. 분석 데이터를 다시 적재한 뒤에도 이미 저장한 학습계획은 남습니다.

팀 원본의 `save_plan`을 RDS에서 사용하려면 추천 실행 환경에 PostgreSQL 드라이버를 설치하고 `PLAN_STORE=rds`, `RDS_HOST`, `RDS_PORT`, `RDS_DB`, `RDS_USER`, `RDS_PASSWORD`를 설정합니다. 추천 실행 계정에는 `plans` 저장 권한이 필요합니다. 배포 후 `save_plan`의 반환값 `store`가 `rds`인지 확인합니다.

2026-11-18(KST) 스냅샷을 팀 PostgreSQL에 적재하고 SQL로 `postings` 29행, `posting_extractions` 26행, `competencies` 225행, `ncs_map` 107행, `demand` 926행을 확인했습니다. DB 호스트와 인증서는 `verify-full`로 검증했습니다. `save_plan`의 실제 저장·조회·테스트 행 삭제는 이전 통합 검증판에서 확인했습니다. 이 공유본은 팀 원본의 추천 저장 코드를 사용합니다.

## S3 Vectors 실호출 1건

`team04-kang`용 서울 리전 벡터 버킷이 있으면 `--create-index` 기본 이름 `team04-kang-ncs`로 인덱스를 준비합니다. 기존 인덱스 ARN이 있다면 그 ARN을 사용합니다. 인덱스는 Titan V2와 동일한 float32·512차원·cosine 설정을 사용합니다. 적재·확인·검색에는 같은 카탈로그와 `--unit-code` 목록을 사용하세요. 권한은 해당 인덱스의 `GetIndex`, `PutVectors`, `GetVectors`, `QueryVectors`와 Titan `bedrock:InvokeModel`입니다. 생성하는 경우 `CreateIndex`가 추가됩니다. [S3 Vectors 검색 권한](https://docs.aws.amazon.com/AmazonS3/latest/API/API_S3VectorBuckets_QueryVectors.html)

```powershell
$env:BEDROCK_MOCK = '0'
$bucket = '팀 S3 벡터 버킷 이름'
& $py pipeline/match_ncs.py --create-index --vector-bucket $bucket

$env:NCS_VECTOR_INDEX_ARN = 'arn:aws:s3vectors:ap-northeast-2:539257524664:bucket/kosa-w6-team04-kang-vec-539257524664/index/team04-kang-ncs'
$unit = '2001020405_19v4'
& $py pipeline/match_ncs.py --seed-index --unit-code $unit --embed-limit 1
& $py pipeline/match_ncs.py --check-index --unit-code $unit
& $py pipeline/match_ncs.py --live --limit 1 --as-of 2026-11-18 --unit-code $unit --posting-id JA-306987 --skill MySQL
$env:BEDROCK_MOCK = '1'
```

`--check-index`는 선택한 능력단위가 보이는지 확인하며 Titan을 호출하지 않습니다. 실호출 결과는 `data/processed/skill_ncs_map.live.json`에 저장됩니다. 인덱스 검사와 1건 질의는 전체 카탈로그의 실호출 결과와 구분해 기록합니다. [Titan Text Embeddings V2](https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html)

실검증에서는 `team04-kang-ncs` 인덱스에 공식 능력단위 1개를 넣고, 공고 `JA-306987`의 `MySQL`을 조회했습니다. `데이터베이스 구현`(`2001020405_19v4`)이 Titan 유사도 0.441로 반환됐습니다. `skill_ncs_map.live.json`에 이 결과가 들어 있습니다.
