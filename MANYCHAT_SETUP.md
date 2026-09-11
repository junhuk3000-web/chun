# 매니챗(ManyChat) 연결 가이드

이 백엔드를 매니챗에 붙여, **인스타 DM 안에서** 대화형 사주 봇을 돌리는 방법.
매니챗이 이미 Meta 앱 심사를 통과한 플랫폼이라 **내 앱 심사는 필요 없다.**

## 사전 준비 (사용자가 직접)

1. **인스타 계정을 프로페셔널(비즈니스/크리에이터)로 전환** — 크리에이터도 OK.
   먼저 설정 → 계정 센터 → 계정 상태에서 제한 여부 확인, 정상화.
2. **이 백엔드 배포** (README "배포" 참고) → 공개 HTTPS URL 확보. 예: `https://xxxx.onrender.com`
   - 배포 시 `WEBHOOK_SECRET` 값 하나 정해두기 (render.yaml 은 자동 생성).
3. **매니챗 가입 → Instagram 채널 연결** (매니챗이 OAuth 로 권한 처리).
4. **매니챗 Pro 플랜** (~$15/월) — External Request 는 Pro 기능.

## 플로우 구성 (매니챗 Flow Builder)

### A. 댓글 → DM 열기
- **Trigger: Instagram Comments** → 대상 게시물(또는 전체), 키워드 = 띠 이름(쥐띠/소띠/…) 또는 전체.
- 첫 DM 메시지:
  > "OO 글에 댓글 남겨주셨네요, 반가워요! 이름이랑 태어난 연·월·일·시를 알려주시면 만세력으로 봐드릴게요.
  > 예) 서아 / 1990년 7월 10일 오전 5시 / 여성"
- (권장) 개인정보 수집·이용 동의 문구 + 동의 버튼 1개.

### B. 생년월일시 받기 → 첫 풀이
두 방법 중 하나:

**B-1. 버튼/입력 단계로 나눠 받기 (권장, 파싱 안정적)**
- User Input 스텝으로 이름 → 저장 (`{{cf_name}}`)
- 생년/월/일/시/성별을 각각 받아 커스텀 필드에 저장
- **External Request 스텝**:
  - Method: `POST`  URL: `https://<배포주소>/api/manychat/session`
  - Headers: `Content-Type: application/json`, `X-Webhook-Secret: <WEBHOOK_SECRET>`
  - Body (JSON):
    ```json
    {
      "user_id": "{{contact_id}}",
      "name": "{{cf_name}}",
      "year": "{{cf_year}}", "month": "{{cf_month}}", "day": "{{cf_day}}",
      "hour": "{{cf_hour}}", "gender": "{{cf_gender}}",
      "category": "종합운"
    }
    ```
  - Response: 매니챗 "Dynamic Content" 로 받으면 서버가 돌려준 텍스트가 그대로 DM 발송됨.
    (서버 응답은 `{"version":"v2","content":{"messages":[{"type":"text","text":"..."}]}}` 포맷)

**B-2. 자유 텍스트 한 줄로 받기**
- User Input 으로 "1990년 7월 10일 오전 5시 여성" 한 줄 받아 `{{cf_birth_text}}` 저장
- External Request Body:
  ```json
  { "user_id": "{{contact_id}}", "name": "{{cf_name}}", "text": "{{cf_birth_text}}", "category": "종합운" }
  ```
- 파싱 실패 시 서버가 "⚠️ 생년월일을 인식하지 못했습니다…" 를 돌려주니, 다시 입력받는 분기로.

### C. 이후 아무 질문이나 → 대화형 답변  ← seoa.notes 의 그 기능
- **Trigger: Default Reply** (키워드에 안 걸리는 모든 메시지)
  - 조건: `{{contact_id}}` 로 세션이 이미 있는 경우 (= 생일 입력을 마친 사람)
    → 아래 External Request 로. 세션 없으면 B 플로우로 보냄.
- **External Request 스텝**:
  - `POST https://<배포주소>/api/manychat/chat`
  - Headers: `X-Webhook-Secret: <WEBHOOK_SECRET>`
  - Body:
    ```json
    { "user_id": "{{contact_id}}", "message": "{{last_input_text}}" }
    ```
  - Response(Dynamic Content) → 답변 + "이어서 물어보실 수 있어요" 추천 질문이 본문에 포함돼 발송됨.

### D. (선택) 추천 질문을 버튼으로
v1 은 추천 질문을 텍스트로 붙여 보낸다. 진짜 퀵리플라이 버튼을 원하면:
- 서버 응답의 `quick_replies` 배열(각 문자열)을 매니챗 Response Mapping 으로 커스텀 필드에 받아
- 매니챗 Quick Reply 버튼 3개를 만들고 각 버튼 target 을 **C 의 External Request 스텝**으로 연결
- 버튼 캡션 = 매핑한 필드값. 누르면 그 텍스트가 `{{last_input_text}}` 로 다시 C 로 들어감.

## 매니챗 제약 확인 포인트

- **24시간 메시징 창**: 유저가 방금 메시지를 보낸 상태에서만 자유 발송 가능. Q&A 봇은 항상 이 안이라 문제없음.
- 매니챗 IG 채널에서 **Default Reply + External Request 조합**이 현재 플랜/정책으로 되는지
  Flow Builder 에서 직접 확인. 안 되면 **Chatfuel** 도 같은 구성 가능.
- `X-Webhook-Secret` 커스텀 헤더가 External Request 에서 지원되는지 확인 (Pro 는 지원).
  안 되면 배포 URL 에 추측 불가능한 경로를 추가하는 방식으로 최소 보호.

## turnflow.link 를 쓰는 경우

turnflow 는 댓글→DM 트리거 + 초기 멘트까지는 확실. **외부 웹훅(External Request 상당)** 지원 여부를
고객센터에 문의: "고객이 DM 으로 보낸 텍스트를 우리 서버 웹훅으로 POST 할 수 있나요?"
- 된다 → 위 B/C 와 동일하게 `/api/manychat/*`(또는 `/api/*`) 로 연결.
- 안 된다 → turnflow 는 "여기서 봐드려요 [배포주소]" 링크 발송까지만, 실제 대화는 웹 채팅(`/`)에서.
