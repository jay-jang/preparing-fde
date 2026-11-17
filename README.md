# FDE 면접과 온보딩 발표 자료

제조업 정비 지원 사례로 엔터프라이즈 AI의 데이터 통합, 근거 있는 RAG, 폐쇄망 배포, 운영 전환을 설명하는 발표 자료다. 본편 14개 프레임(약 39분)과 2부 현장 플레이북 3개 프레임(약 10분)으로 구성한다. 전체 파이프라인에서 네 기술 영역의 실패 지점과 코드로 확대한 뒤 전체 구조로 돌아오고, 2부에서는 고객 Discovery, 오답 신고의 재현, 용량 산정을 다룬다.

| 결과물 | 용도 |
| --- | --- |
| 줌 캔버스 (`build/pages/index.html`) | 실제 발표용. Prezi처럼 조감도 → 영역 → 카드 → 도식·코드로 확대하고, 면접 15분·온보딩 전체·현장 플레이북 경로와 발표자 노트·발표자 창을 제공한다. |
| 핸드아웃 (`build/pages/reader.html`, [원본 Markdown](docs/fde-prezi-ko.md)) | 프레임별 화면 핵심, 시각화 가이드, 엔지니어링 분석, 코드, 대본과 면접 Q&A·롤플레이를 읽는 문서 |
| [도식](assets/diagrams) | 프레임별 Mermaid 원본, Prezi 이관·핸드아웃용 PNG, 줌 캔버스용 SVG |
| 코드 (`fde/`, `sql/`, `deploy/`, `tests/`) | 발표의 불변식을 실행하고 검증하는 예제 |
| [자료 ZIP](deliverables/fde-prezi-kit.zip) | 위 전부와 오프라인 사본 `presentation/` |

Prezi 계정에 생성한 프레젠테이션 파일은 아니다. 줌 캔버스는 같은 구성을 브라우저에서 바로 발표할 수 있게 구현한 것이고, Prezi로 옮길 때는 PNG 도식과 각 프레임의 화면 핵심·코드를 편집 가능한 텍스트로 배치한다.

## 발표하기

외부 CDN 없이 동작한다. 다음 명령으로 빌드하고 `http://localhost:4174/`를 연다. 파일을 직접 여는 `file://`에서는 브라우저가 웹 폰트를 막을 수 있으므로, 오프라인 ZIP의 `presentation/`도 같은 방식으로 로컬 서버에서 연다.

```sh
.venv/bin/python -m pip install -r requirements-pages.txt
.venv/bin/python scripts/build_pages.py
python3 -m http.server 4174 --directory build/pages
```

| 조작 | 동작 |
| --- | --- |
| `→` · Space / `←` | 다음 · 이전 단계 (카드 → 도식 → 코드) |
| `Shift + →` / `Shift + ←` | 도식·코드를 건너뛰고 다음 · 이전 프레임 |
| `O` · `Esc` | 전체 캔버스 조감도와 복귀 |
| `1`–`5` | 기술 영역 1–4와 현장 플레이북(5)으로 이동 |
| `N` | 발표자 노트: 대본, 분석, 화면 구성, Q&A 탭과 프레임별 시간 예산·타이머 |
| `P` | 별도 발표자 창. 같은 브라우저의 청중 창과 단계가 동기화된다 |
| `F` · `T` · `?` | 전체 화면 · 밝은/어두운 테마 · 단축키 안내 |
| 클릭 · 휠 · 드래그 | 카드나 도식·코드로 확대 · 자유 확대/축소 · 이동 |

상단에서 면접 15분, 온보딩 전체, 현장 플레이북 경로를 고른다. 주소의 `#/경로/단계`(예: `#/interview/2-2.code`)로 특정 단계를 바로 연다.

소스는 `web/`(`deck.*`는 줌 캔버스, `reader.html`·`style.css`·`app.js`는 핸드아웃)와 `scripts/build_pages.py`·`scripts/deck.py`이고, 빌드 결과는 Git에서 제외한 `build/pages/`다. 두 페이지 모두 `docs/fde-prezi-ko.md` 하나에서 만든다. 각 프레임의 `화면 핵심` 블록(영문 kicker, 한 문장 메시지, 요점 세 개)이 카드의 화면 문구가 되고, `실전 코드`의 첫 코드 블록이나 `<!-- deck -->`을 앞에 둔 블록이 카드의 대표 코드가 된다. 글꼴은 SIL OFL 1.1의 Pretendard 부분 집합을 `web/fonts/`에 포함한다.

GitHub Pages 배포를 지원하는 저장소에서 Settings → Pages의 Source를 GitHub Actions로 설정하고, 저장소 변수 `PAGES_ENABLED`를 `true`로 지정하면 `.github/workflows/pages.yml`이 배포한다. 설정 전에도 build job으로 페이지와 배포 artifact를 검증할 수 있다. 이후 발표안·도식·웹 소스를 `main`에 push하면 자동으로 다시 빌드하고 배포한다. 활성화 후 배포 주소는 `https://jay-jang.github.io/preparing-fde/`다.

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
| 오답 재현 | `fde/provenance.py`: 답변 시점의 버전·위치 기록과 replay 기록을 경계 순서로 비교 |
| 용량 산정 | `fde/capacity.py`: 원시 벡터, KV cache, Little's law 동시 요청, 단일 GPU 메모리 예산의 가정 계산 |
| 발표 빌드 | `scripts/build_pages.py`, `scripts/deck.py`, `tests/test_pages.py`: 프레임 구조, 화면 문구 길이, 카메라 대상과 경로 검증 |

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

발표안의 Mermaid를 수정한 뒤 원본과 색인을 추출하고 PNG·SVG를 다시 렌더링한다. 렌더러는 Mermaid CLI 11.12.0으로 Pretendard 18px, 영역별 색(데이터 파랑, AI 청록, 보안 보라, 운영 주황, 플레이북 올리브)과 실패 경로의 붉은 상자를 적용한다. PNG는 흰 배경·2배 스케일이고, SVG는 프레임별 고유 id를 가져 줌 캔버스에 인라인으로 들어간다. 오프라인 환경에서는 승인된 내부 Mermaid CLI와 Chromium을 `MMDC`, `PUPPETEER_CONFIG`로 지정한다.

```sh
.venv/bin/python scripts/extract_diagrams.py
MMDC=/path/to/mmdc .venv/bin/python scripts/render_diagrams.py        # 전체, 또는 프레임 키 지정
.venv/bin/python scripts/build_pages.py && .venv/bin/python scripts/build_kit.py
```

16:9 카드 안에서 확대해 읽히도록 도식은 가로세로비 약 1.3~2.4, 폭 1,500px 이하로 배치한다. 긴 단일 행 대신 두세 줄의 흐름이나 위아래 배치를 사용한다. 전체 발표를 한 장의 이미지로 만들지 않으므로 확대할 코드와 제목의 가독성을 유지할 수 있다. `build_kit.py`는 발표안, 도식, 코드, 테스트, 웹 소스와 빌드된 줌 캔버스를 고정 타임스탬프로 묶는다.
