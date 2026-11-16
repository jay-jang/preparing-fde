# FDE 발표 자료 검증 기록

2026년 11월 16일 작업 폴더의 실제 코드와 생성물을 기준으로 확인했다. [발표안](fde-prezi-ko.md)의 제안 목표와 실제 검증 결과를 구분한다.

## 요구사항과 결과

| 요구사항 | 확인한 결과 |
| --- | --- |
| 거시 구조에서 코드로 확대하는 Prezi 내러티브 | Level 1 프레임 2개, Level 2 프레임 4개, Level 3 프레임 8개. 좌표·프레임 크기·이동 순서·조감도 복귀 위치 포함 |
| 네 기술 영역 | 데이터 통합과 온톨로지, hybrid RAG와 agent, 폐쇄망·보안·감사, 현장 MVP와 운영 전환을 각각 3개 프레임으로 구성 |
| 프레임별 출력 형식 | 14개 모두 번호·줌 레벨·시각화·엔지니어링 분석·실전 코드·발표자 대본 확인 |
| 구성요소와 고려사항 | 모든 프레임에 구성요소와 핵심 고려사항을 명시. 각 영역에서 지연·확장성·정합성·권한·복구의 선택 근거와 한계 분석 |
| Mermaid | 14개 원본 추출과 PNG 렌더링 성공. 조감도·권한 sequence·폐쇄망 도식의 글자와 선을 표본으로 육안 확인 |
| 실행 코드 | 트랜잭션 수집, 실제 DB RLS와 hybrid ranking, 인용·도구 제한, 개인정보 최소화, 서명 검증, 품질 gate 구현 |
| 발표안 코드 문법 | Python 코드 블록 11개 AST 파싱, YAML 코드 블록 3개 파싱. SQL 2개는 대응 schema와 대조 |
| 자동 검증 | 실제 임시 PostgreSQL을 포함한 36개 테스트, skip 없이 모두 통과 |
| 운영 규칙 | `promtool check rules deploy/alerts.yaml` 성공, 알림 2개 확인 |
| 근거 자료 | 발표안의 분석 근처와 마지막 자료 표에 공식 문서 17개 링크 첨부 |

## 실행 환경과 명령

| 구성 | 실제 버전 |
| --- | --- |
| Python | 3.12.3 |
| PostgreSQL | 16.15 |
| psycopg | 3.2.13 |
| PyYAML | 6.0.3 |
| Mermaid CLI | 11.12.0 |
| promtool | 2.45.3 |

```sh
.venv/bin/python scripts/check.py --all
```

실행 결과는 `Ran 36 tests in 1.523s`, `OK`다. [전체 테스트 출력](test-output.txt)에 각 케이스를 기록했다. DB 검사는 개인 임시 Unix socket과 실제 비특권 LOGIN 계정을 사용했고, 종료 후 해당 서버와 데이터를 제거했다.

중요한 검증에는 동시 중복 전달, 역순 이벤트와 tombstone, 동일 event ID의 payload 변경, 감사 insert 실패 시 전체 rollback, tenant·role 경계, 요청 간 identity 초기화, 생성 중 권한 철회·문서 변경의 재검증, 실제 RSA 서명과 변조 실패가 포함된다. 품질 gate는 합성 입력으로 누락·잘못된 값·슬라이스 회귀·과도한 보류·누출·느린 응답을 판정하는 로직을 확인했다.

Prometheus 규칙은 다음과 같이 별도 검사했다.

```text
Checking deploy/alerts.yaml
  SUCCESS: 2 rules found
```

## 검증의 적용 범위

로컬 결과는 핵심 로직과 명시된 경계를 검증한 것이다. 실제 고객 데이터의 품질, 한국어 embedding·reranker·LLM 성능, 대규모 검색의 recall과 latency를 측정한 결과는 아니다. `guardrails`는 인용 provenance를 확인하지만 의미적 support를 증명하지 않으며, 이 한계를 확인하는 테스트도 포함한다.

Kubernetes와 CI 설정은 예제다. 실제 cluster의 admission·CNI 통신, 내부 TLS와 IdP, 인터넷 차단 cold start, WORM 감사 보관, backup/PITR·canary와 원격 GitHub Actions 실행은 수행하지 않았다. 전체 고객 플랫폼 배포나 네이티브 Prezi 프레젠테이션 생성을 완료했다고 주장하지 않는다. 사용자는 발표안과 PNG를 Prezi에 배치할 수 있다.

Pages 연결의 저장 결과는 도구가 `page_creation_outcome_unknown`으로 반환했다. 동일한 생성 키와 원래 인자로 복구를 시도했지만 성공을 확인하지 못해 Page 링크를 제공하지 않는다. 전체 발표안·코드·도식은 로컬 파일과 ZIP에 들어 있다.
