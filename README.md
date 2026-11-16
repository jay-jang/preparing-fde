# FDE 면접과 온보딩 발표 자료

[한국어 발표안](docs/fde-prezi-ko.md)은 제조업 정비 지원 사례를 사용한 14개 프레임, 약 39분 분량의 Prezi 구성안이다. 전체 파이프라인에서 네 기술 영역으로 확대하고, 구현 코드와 실패 사례를 살핀 뒤 전체 구조로 돌아온다. 프레임별 시각화 가이드, Mermaid, 시스템 구성요소, 실무 트레이드오프, 코드와 구어체 발표 대본이 들어 있다.

Prezi 계정에 생성한 프레젠테이션 파일은 아니다. 캔버스 배치와 카메라 경로를 따라 옮길 수 있는 발표 설계안이다. [도식 폴더](assets/diagrams)에는 프레임별 Mermaid 원본과 PNG를 제공한다. 제목과 코드는 Prezi의 편집 가능한 텍스트로 배치한다.

## 실행

Python 3.12, OpenSSL, PostgreSQL server 바이너리가 필요하다. Ubuntu에서는 `python3-venv`, `postgresql` 패키지로 준비할 수 있다. Python 의존성은 다음과 같이 설치한다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/check.py --all
```

`--all`은 비특권 OS 사용자로 실행한다. 개인 임시 디렉터리의 Unix socket만 사용하는 PostgreSQL을 만들고, 실제 reader·ingest LOGIN 계정으로 검사한 뒤 서버와 데이터를 제거한다. 기존 DB나 PostgreSQL 서비스는 사용하지 않는다. 이 격리된 시험 서버의 trust 인증을 운영 DB 설정으로 복사하지 않는다.

PostgreSQL 없이 순수 로직만 확인하려면 `.venv/bin/python scripts/check.py`를 사용한다. 이때 DB 통합 검사는 생략되므로 전체 검증과 구분한다. 현재 실행 결과와 범위는 [검증 기록](docs/verification.md)에 정리한다.

## 코드 탐색

| 경계 | 구현과 확인할 부분 |
| --- | --- |
| 데이터 통합 | `fde/ingestion.py`와 `sql/schema.sql`: 명시적 schema adapter, source authority, receipt·state·outbox 원자성, 역순·삭제 처리 |
| 검색과 권한 | `fde/retrieval.py`: 실제 DB RLS 후 BM25·cosine·RRF, 응답 전 권한·문서 재검증 |
| 생성과 에이전트 | `fde/guardrails.py`: 엄격한 출력 계약, 인용 provenance, read-only 도구와 예산 |
| 개인정보와 감사 | `fde/privacy.py`: 필드 최소화, 제한된 패턴 마스킹, HMAC 가명처리 |
| 반입과 배포 | `fde/bundle.py`, `deploy/render.py`: 외부에서 배포한 키로 서명 검증, image digest와 제한된 workload 설정 |
| 운영 게이트 | `fde/release_gate.py`, `deploy/alerts.yaml`: 평가 결과 판정과 Prometheus 알림 규칙 |

`sql/schema.sql`은 깨끗한 실습 DB에 관리자 권한으로 한 번 적용하는 schema다. 발표안의 DB 호출 예제를 별도 환경에서 실행할 때는 권위 identity 매핑, membership·청크와 최소 권한 LOGIN 계정을 먼저 준비한다. `FDE_INGEST_DSN`과 `FDE_READER_DSN`에 해당 자격증명을 전달하며, 운영 환경에서는 TLS 검증과 고객의 비밀 관리 체계를 사용한다. 자동 통합 검사는 이 준비와 정리를 모두 수행한다.

## 배포와 평가 예제

검토된 실제 이미지가 있을 때 `APPROVED_IMAGE`에 `registry.internal/경로@sha256:실제digest`를 지정하고 실행한다.

```sh
.venv/bin/python -m deploy.render "$APPROVED_IMAGE" > workload.yaml
```

renderer는 실제 API 이미지를 빌드하지 않는다. API의 8080·health endpoint, 내부 IdP/gateway·DB·모델 서버·DNS·OTel, Secret과 PVC, TLS·CNI·인접 workload의 정책을 준비한 환경에서 생성물을 검증한다. Kubernetes API audit policy는 control-plane 설정이고, `alerts.yaml`은 Prometheus 규칙이다. 같은 `kubectl apply` 입력으로 무조건 묶지 않는다.

실제 모델과 고정된 holdout으로 독립 평가를 수행한 뒤 결과 JSON을 승격 판정기에 전달한다.

```sh
.venv/bin/python -m fde.release_gate /path/to/real-evaluation-report.json
```

결과 JSON의 최상위 필드는 `dataset_version`, `artifact_digest`, `rows`다. `artifact_digest`는 `sha256:`과 64자리 소문자 16진수이며, row마다 고유 `id`, `kind`(`factoid`, `semantic`, `unanswerable`, `forbidden`), 네 boolean(`retrieval_hit`, `supported`, `abstained`, `leaked`)과 유한한 `latency_ms`가 필요하다. `retrieval_hit`은 top-5 내 정답 근거 존재 여부이고, `supported`는 답변의 모든 핵심 주장이 근거로 지지되는지의 독립 판정이다. 검증용 합성 row로 실제 모델 성능을 주장하지 않는다.

## Prezi 도식 원본

발표안의 Mermaid를 수정한 뒤 `.venv/bin/python scripts/extract_diagrams.py`로 원본과 색인을 갱신한다. PNG는 Mermaid CLI 11.12.0으로 흰 배경·2배 스케일로 렌더링했다. 다시 렌더링할 때는 승인된 내부 도구나 별도로 설치한 Mermaid CLI를 사용한다.

```sh
mmdc -i assets/diagrams/frame-0-1.mmd -o assets/diagrams/frame-0-1.png -b white -w 2400 -s 2
```

각 PNG는 한 프레임의 도식이다. 전체 발표를 한 장의 이미지로 만들지 않으므로 확대할 코드와 제목의 가독성을 유지할 수 있다.
