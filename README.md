# FDE 면접과 온보딩 발표 자료

제조업 정비 지원 사례로 엔터프라이즈 AI의 데이터 통합, 근거 있는 RAG, 폐쇄망 배포, 운영 전환을 설명하는 발표 자료다. 34개 프레임을 세 부로 나눈다. 1부 기술 아키텍처(22개, 약 63분)는 전체 파이프라인에서 네 기술 영역의 실패 지점과 코드로 확대한 뒤 전체 구조로 돌아온다. 2부 현장 플레이북(6개, 약 19분)은 고객 Discovery, 결정 기록, 오답 신고의 재현, 용량 산정, 장애 대응, 운영 이관을 다룬다. 3부 규모 확장(6개, 약 18분)은 한 공장의 파일럿을 전 공장으로 넓힐 때 먼저 깨지는 곳과 그 측정을 다룬다. 모든 프레임에는 출처를 구분한 근거 데이터 표가 있다.

| 결과물 | 용도 |
| --- | --- |
| 줌 캔버스 (`build/pages/index.html`) | 실제 발표용 Prezi식 캔버스. 허브와 주제 원 6개, 주제 안의 프레임 원, 프레임 안의 도식·코드·데이터 창으로 확대·회전하며 들어가고, 멀리서는 숨어 있던 내용이 확대할수록 드러난다. 면접 15분·면접 심화 30분·1부·2부·3부·온보딩 전체 경로, 경로 보기, 발표자 노트·발표자 창을 제공한다. |
| 핸드아웃 (`build/pages/reader.html`, [원본 Markdown](docs/fde-prezi-ko.md)) | 프레임별 화면 핵심, 시각화 가이드, 엔지니어링 분석, 근거 데이터, 코드, 대본과 면접 Q&A·롤플레이·용어집을 읽는 문서 |
| [도식](assets/diagrams) | 프레임별 Mermaid 원본, Prezi 이관·핸드아웃용 PNG, 줌 캔버스용 SVG |
| 코드 (`fde/`, `sql/`, `deploy/`, `tests/`) | 발표의 불변식을 실행하고 검증하는 예제 |
| [자료 ZIP](deliverables/fde-prezi-kit.zip) | 위 전부와 오프라인 사본 `presentation/` |

Prezi 계정에 생성한 프레젠테이션 파일은 아니다. 줌 캔버스는 Prezi의 주제·하위 주제·확대 공개 구조를 브라우저에서 바로 발표할 수 있게 구현한 것이고, Prezi로 옮길 때는 주제를 topic, 프레임을 subtopic으로 만들고 PNG 도식과 각 프레임의 화면 핵심·코드·근거 데이터를 편집 가능한 텍스트로 배치한다.

## 발표하기

외부 CDN 없이 동작한다. 다음 명령으로 빌드하고 `http://localhost:4174/`를 연다. 파일을 직접 여는 `file://`에서는 브라우저가 웹 폰트를 막을 수 있으므로, 오프라인 ZIP의 `presentation/`도 같은 방식으로 로컬 서버에서 연다.

```sh
.venv/bin/python -m pip install -r requirements-pages.txt
.venv/bin/python scripts/build_pages.py
python3 -m http.server 4174 --directory build/pages
```

| 조작 | 동작 |
| --- | --- |
| `→` · Space / `←` | 경로의 다음 · 이전 단계 (자유 탐색 중이면 `←`는 경로로 복귀) |
| `Shift + →` / `Shift + ←` | 도식·코드·데이터를 건너뛰고 다음 · 이전 프레임 |
| 클릭 | 주제·프레임·창을 눌러 그 안으로 확대 |
| `Esc` · 빈 곳 클릭 | 한 단계 축소 (세부 → 프레임 → 주제 → 조감도) |
| `O` | 조감도와 복귀 |
| `0`–`6` | 허브(0)와 주제 1–6으로 이동 |
| 휠 · 드래그 · 두 손가락 | 자유 확대·축소와 이동. 확대한 위치에 맞춰 내용이 드러난다 |
| `N` | 발표자 노트: 대본, 분석, 데이터, 화면 구성, Q&A 탭과 경로별 시간 예산·타이머 |
| `P` | 별도 발표자 창. 같은 브라우저의 청중 창과 단계·타이머가 동기화된다 |
| `F` · `T` · `?` | 전체 화면 · 밝은/어두운 테마 · 단축키 안내 |

상단의 경로 버튼에서 면접 15분, 면접 심화 30분, 1부·2부·3부, 온보딩 전체를 고른다. 조감도에는 선택한 경로가 방문하는 프레임 순서가 번호로 표시된다. 주소의 `#경로~단계`(예: `#interview~2-2.code`)로 특정 단계를 바로 연다. 이전 형식 `#/interview/2-2.code`도 받아들인다.

소스는 `web/`(`atlas.*`는 줌 캔버스, `reader.html`·`style.css`·`app.js`는 핸드아웃)와 `scripts/build_pages.py`·`scripts/atlas.py`이고, 빌드 결과는 Git에서 제외한 `build/pages/`다. 두 페이지 모두 `docs/fde-prezi-ko.md` 하나에서 만든다. 각 프레임의 `화면 핵심` 블록(영문 kicker와 짧은 제목, 한 문장 메시지, 요점 세 개)이 프레임 원의 화면 문구가 되고, `근거 데이터` 표가 데이터 창, `실전 코드`의 첫 코드 블록이나 `<!-- deck -->`을 앞에 둔 블록이 코드 창이 된다. `scripts/atlas.py`가 모든 카메라 대상의 위치·크기·회전을 계산해 넘기므로 화면은 DOM을 측정하지 않는다. 글꼴은 SIL OFL 1.1의 Pretendard 부분 집합을 `web/fonts/`에 포함한다.

GitHub Pages 배포를 지원하는 저장소에서 Settings → Pages의 Source를 GitHub Actions로 설정하고, 저장소 변수 `PAGES_ENABLED`를 `true`로 지정하면 `.github/workflows/pages.yml`이 배포한다. 설정 전에도 build job으로 페이지와 배포 artifact를 검증할 수 있다. 이후 발표안·도식·웹 소스를 `main`에 push하면 자동으로 다시 빌드하고 배포한다. 활성화 후 배포 주소는 `https://jay-jang.github.io/preparing-fde/`다.

## 실행

Python 3.12, OpenSSL, PostgreSQL server 바이너리가 필요하다. Ubuntu에서는 `python3-venv`, `postgresql` 패키지로 준비할 수 있다. Python 의존성은 다음과 같이 설치한다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-pages.txt
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
| 오답 재현 | `fde/provenance.py`: 답변 시점의 버전·위치 기록과 replay 기록을 경계 순서로 비교 |
| 용량 산정 | `fde/capacity.py`: 원시 벡터, KV cache, Little's law 동시 요청, 단일 GPU 메모리 예산의 가정 계산 |
| 문서 구조와 데이터 품질 | `fde/chunking.py`, `fde/contracts.py`: 절·표·점검 단계를 끊지 않는 청킹, 원천 계약 위반의 격리와 재처리 |
| 검색 평가와 승인된 쓰기 | `fde/metrics.py`, `fde/actions.py`: span 라벨 기반 Hit@k·MRR·nDCG와 paired bootstrap, 초안·승인·멱등 실행 |
| 공급망과 키 | `fde/sbom.py`, `fde/keyring.py`: 오프라인 SBOM·advisory·VEX 판정, 회전해도 과거 기록을 검증하는 keyring |
| 배포 판정과 에러 예산 | `fde/canary.py`, `fde/error_budget.py`: Wilson 구간 기반 canary 판정, 다중 창 burn rate와 예산 정책 |
| 현장 운영 | `fde/decisions.py`, `fde/incident.py`, `fde/handover.py`: RACI·ADR 검증, 장애 타임라인과 postmortem, 운영 이관 증적 |
| 규모 확장 | `fde/scaleout.py`, `fde/ann.py`, `sql/partitioning.sql`, `fde/queueing.py`, `fde/reindex.py`, `fde/cost.py`: 첫 병목, 필터 ANN 탐색량, 파티션과 RLS, Erlang C, 무중단 재인덱싱, TCO |
| 발표 빌드 | `scripts/build_pages.py`, `scripts/atlas.py`, `tests/test_pages.py`: 프레임 구조, 화면 문구와 근거 데이터 형식, 중첩 배치와 카메라 대상, 경로, 질문·롤플레이·용어집·출처 개수 검증 |

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

## 도식과 자료 ZIP

발표안의 Mermaid를 수정한 뒤 원본과 색인을 추출하고 PNG·SVG를 다시 렌더링한다. 렌더러는 Mermaid CLI 11.12.0으로 Pretendard 18px, 영역별 색(데이터 파랑, AI 청록, 보안 보라, 운영 주황, 플레이북 올리브, 규모 확장 남색)과 실패 경로의 붉은 상자를 적용한다. PNG는 흰 배경·2배 스케일이고, SVG는 프레임별 고유 id를 가져 줌 캔버스에 인라인으로 들어간다. 오프라인 환경에서는 승인된 내부 Mermaid CLI와 Chromium을 `MMDC`, `PUPPETEER_CONFIG`로 지정한다.

```sh
.venv/bin/python scripts/extract_diagrams.py
MMDC=/path/to/mmdc .venv/bin/python scripts/render_diagrams.py        # 전체, 또는 프레임 키 지정
.venv/bin/python scripts/build_pages.py && .venv/bin/python scripts/build_kit.py
```

프레임의 도식 창(16:9)에서 확대해 읽히도록 도식은 가로세로비 약 1.3~2.8, 폭 1,500px 이하로 배치한다. 긴 단일 행 대신 두세 줄의 흐름이나 위아래 배치를 사용한다. 전체 발표를 한 장의 이미지로 만들지 않으므로 확대할 코드와 제목의 가독성을 유지할 수 있다. `build_kit.py`는 발표안, 도식, 코드, 테스트, 웹 소스와 빌드된 줌 캔버스를 고정 타임스탬프로 묶는다.
