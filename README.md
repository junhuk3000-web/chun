# 천명재 사주 챗봇 백엔드

고객이 **이름 + 생년월일시**를 넣으면 만세력을 계산해 첫 풀이를 주고,
이후 **아무 질문이나 대화형으로** 답하는 사주 상담 봇. (`seoa.notes` 계정과 같은 구조)

이 저장소는 **[2] 두뇌**만 담당한다. [1] 댓글→DM 트리거는 매니챗 등이 맡는다.

```
[1] 트리거      인스타 댓글("토끼띠") → DM 발송 (매니챗 / turnflow)   ← 사용자가 연결
       │  고객이 이름·생일 + 질문을 보냄
       ▼
[2] 이 백엔드   생년월일시 → 만세력 계산 → 세션 저장 → Claude 로 대화형 해석
       ▼
[3] Claude API  "이미 계산된 값은 바꾸지 말고 문장만 써라" 가드로 해석 생성
       ▼
      고객에게 답장 (매니챗이 DM 으로 재발송 / 또는 웹 채팅에서)
```

한 백엔드로 **매니챗 DM 봇**과 **웹 채팅(링크 유도용)** 을 동시에 커버한다.

---

## 폴더

```
app/
  sajupy_vendor/    만세력 계산 라이브러리 (오픈소스 sajupy, MIT)  ← 그대로
  saju_engine.py     생년월일시 → 사주원국 + 오행/십신/합충원진      ← 그대로 (검증됨)
  birth_parser.py    자유 텍스트 → 생년월일시 구조화                 ← 그대로
  chat.py            사주 사실 + 대화기록 + 질문 → Claude 해석 (SDK, 캐싱, 멀티턴)
  sessions.py        고객별 세션 저장 (sqlite: 이름·사주·대화기록)
  server.py          Flask 서버 (엔드포인트는 파일 상단 주석 참고)
  web/index.html     내장 웹 채팅 UI (링크로 유도할 때 이 페이지)
  test_pipeline.py   로컬 검증 (계산·세션·라우팅. API 키 없이 통과)
  requirements.txt / .env.example
Procfile / render.yaml    배포 설정
MANYCHAT_SETUP.md         매니챗 플로우 연결 가이드
```

## 로컬 실행

```bash
cd app
python -m pip install -r requirements.txt
cp .env.example .env          # ANTHROPIC_API_KEY 채우기 (모델은 claude-haiku-4-5 권장)

python test_pipeline.py       # 계산·세션·라우팅 검증 (Claude 호출은 건너뜀)

# 서버 (Windows 는 UTF-8 강제 권장)
set PYTHONUTF8=1 && python server.py     # http://localhost:8000  → 웹 채팅 UI
```

빠른 API 테스트:

```bash
# 1) 세션 시작 → 첫 풀이
curl -s -X POST http://localhost:8000/api/session -H "Content-Type: application/json" \
  -d '{"name":"서아","year":1990,"month":7,"day":10,"hour":5,"gender":"여성","category":"금전운"}'
# → {"user_id":"...","reply":"...","quick_replies":["...","..."],"saju":{...}}

# 2) 그 user_id 로 이어서 질문
curl -s -X POST http://localhost:8000/api/chat -H "Content-Type: application/json" \
  -d '{"user_id":"<위 user_id>","message":"투자 재개는 몇 월이 좋아요?"}'
```

## 배포

- **Render.com**: `render.yaml` 로 Blueprint 배포 (무료 티어, 비활성 시 슬립 / 상시가동은 $7/월).
  배포 후 대시보드에서 `ANTHROPIC_API_KEY` 입력, 자동 생성된 `WEBHOOK_SECRET` 복사.
- **Railway / Fly.io**: `Procfile` 사용. `gunicorn --chdir app server:app --workers 1`.
- 세션 sqlite 는 영구 디스크에 둘 것 (`SESSION_DB=/var/data/sessions.db`).
- **워커는 1개** (`--workers 1`). 트래픽이 커지면 `sessions.py` 를 Postgres/Redis 로 교체.

## 비용 (Claude API)

- 답변 1건 ≈ 입력 수백~1천 토큰 + 출력 ~500 토큰.
  `claude-haiku-4-5`($1/$5 per 1M) 기준 **건당 대략 0.3~0.7센트**.
- 사주 사실 JSON + 시스템 프롬프트는 세션 내내 고정 → **prompt caching** 이 걸려 있어
  후속 질문의 입력 토큰 비용이 크게 줄어든다 (`usage.cache_read_input_tokens` 로 확인).
- 모델은 `.env` 의 `ANTHROPIC_MODEL` 한 줄로 교체 (haiku-4-5 / sonnet-5 / opus-5).

## 운영 주의

- 생년월일시 수집 → **개인정보처리방침 + 수집·이용 동의** 필요. 웹 UI/DM 첫 메시지에 고지.
- 봇 답변은 경향·조언 톤. `chat.py` 의 `SYSTEM_PROMPT` 가 특정 투자 종목·진단명·면책문구를 금지.
- 계산과 생성이 분리돼 있으니, 주기적으로 실제 답변을 샘플 검수할 것.
