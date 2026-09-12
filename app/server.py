# -*- coding: utf-8 -*-
"""
server.py
-----------
사주 계산(saju_engine) + 대화형 해석(chat) + 세션(sessions)을 묶은 웹 서버.

트리거 레이어(매니챗 / turnflow / Meta 공식 API / 웹 프론트)는 아래 엔드포인트에
JSON 을 POST 하기만 하면 된다 — 이 서버는 특정 플랫폼에 종속되지 않는다.

엔드포인트
----------
GET  /                       내장 웹 채팅 UI (링크로 유도하는 경우 이 페이지를 씀)
GET  /health                 헬스체크

POST /api/session            이름+생년월일시 → 사주 계산, 세션 생성, 첫 풀이 반환
                             body: {user_id?, name, category?,
                                    year,month,day,hour?,minute?,gender?,is_lunar?,city?}
                             또는 {user_id?, name, category?, text: "1990년 7월 10일 오전 5시 남자"}
                             → {user_id, reply, quick_replies, saju}
POST /api/chat               세션 이어서 질문 답변
                             body: {user_id, message} → {reply, quick_replies}
POST /api/session/reset      body: {user_id} → 세션 삭제

POST /api/manychat/session   위 /api/session 과 동일하되 매니챗 Dynamic Content 응답 포맷
POST /api/manychat/chat      위 /api/chat 과 동일하되 매니챗 Dynamic Content 응답 포맷

POST /webhook/raw-text       (단발) 자유 텍스트 → 파싱 → 첫 풀이 1회. 세션 없음. 테스트용.
GET/POST /webhook/instagram  Meta 공식 웹훅용 뼈대 — 배포 전 최신 문서와 대조 필요

보안: 모든 POST /api/*, /webhook/* 는 X-Webhook-Secret 헤더가 환경변수
WEBHOOK_SECRET 과 일치해야 처리된다. WEBHOOK_SECRET 이 비어 있으면 검증을 건너뜀(로컬).
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Optional

try:
    from dotenv import load_dotenv

    load_dotenv()  # 로컬 .env 로드. 배포 환경(Render 등)은 플랫폼 환경변수를 쓰므로 없어도 무해.
except ImportError:
    pass

from flask import Flask, jsonify, request, send_from_directory

import sessions
from birth_parser import is_complete, parse_birth_info
from chat import ChatError, answer, first_reading
from saju_engine import calculate

app = Flask(__name__)
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or None
DEFAULT_CITY = os.environ.get("DEFAULT_CITY", "Seoul")

# 고객에게 필요한 건 이름이 아니라 생년월일·태어난시각·성별 3가지뿐이다(이름은 절대
# 요구하지 않음). 표기 방식은 사람마다 제각각이라(구분자 있는/없는, 2자리/4자리 연도
# 등) birth_parser.py 가 최대한 포괄적으로 인식하므로, 안내 문구도 "형식 안 가리고
# 편하게 보내도 된다"는 걸 예시로 보여준다.
BIRTH_REQUEST_MSG = (
    "생년월일이랑 태어난 시각, 성별을 알려주면 사주 봐줄게! 이름은 몰라도 돼~ "
    "숫자 표기는 편한 대로 써도 다 인식돼 — "
    "'1990-07-10 오전 5시 여자', '900710 0505 여', '90.7.10 17시 남' 다 가능해 🔮"
)


def _check_secret():
    if WEBHOOK_SECRET is None:
        return None
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    return None


_LENIENT_FIELDS = ("user_id", "message", "text", "name", "category", "gender", "city")

# "연도로 보이는 숫자가 있는데 birth_parser가 완전한 날짜로 못 뽑아낸" 경우를 잡기 위한
# 대략적인 감지기. 완벽할 필요 없음 — 오탐(false positive)이 나도 "다시 정확히 보내줘"라는
# 무난한 안내만 나가므로 안전한 방향의 휴리스틱이다.
_YEAR_LIKE_RE = re.compile(r"(19|20)\d{2}|\d{2}\s*년")


def _parse_body_lenient(raw: bytes) -> dict:
    """일부 트리거 플랫폼(매니챗 등)은 자유 텍스트에 포함된 줄바꿈을 이스케이프하지
    않고 그대로 JSON 문자열 안에 넣어 보내서 'Invalid JSON'이 되는 경우가 있다
    (예: 고객이 생년월일시를 여러 줄로 나눠 보냄). 정식 JSON 파싱이 실패하면,
    알려진 필드들을 정규식으로 관대하게 복구해서 최소한 동작은 하게 한다."""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for key in _LENIENT_FIELDS:
        m = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
        if m:
            out[key] = m.group(1).replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    return out


def _body() -> dict:
    """request JSON body를 읽되, 파싱 실패 시 관대한 복구를 시도한다.
    거기에 더해 쿼리스트링(?user_id=...&text=...) 값이 있으면 그걸 우선 사용한다.
    매니챗 등이 자유 텍스트를 손으로 타이핑한 JSON 문자열 안에 끼워 넣을 때 줄바꿈을
    이스케이프하지 않아 깨지는 경우가 있는데, URL 쿼리 파라미터는 표준 URL 인코딩이라
    훨씬 안정적으로 들어온다. Request URL 을
      https://.../api/message?user_id={{Contact Id}}&text={{Last Text Input}}
    처럼 구성하면(Body는 비워도 됨) 이 경로로 안전하게 수신된다."""
    data = request.get_json(silent=True)
    if data is None:
        data = _parse_body_lenient(request.get_data())
    data = dict(data) if data else {}
    for key in _LENIENT_FIELDS:
        v = request.args.get(key)
        if v:
            data[key] = v
    return data


# ---------------------------------------------------------------------------
# Instagram 발송 속도 제한 대응
# ---------------------------------------------------------------------------
# Meta Instagram Messaging API 는 계정당 시간당 자동 DM 약 200건으로 제한된다.
# 게다가 이 automation 은 사람당 최소 2건(①"잠시만요..." 캔드 메시지 ②우리 답변)을
# 쓰므로 실질 처리 가능 인원은 시간당 200건보다 적다. 이 한도를 넘겨서 Meta 쪽에서
# 거부/제재당하느니, 우리 쪽에서 먼저 스스로 속도를 늦추는 게 안전하다(최대 대기 시간
# 안에서는 늦게라도 정확한 답을 준다).
#
# 구현: 최근 1시간 안에 응답한 건수를 메모리에서 슬라이딩 윈도우로 추적. 한도에 걸리면
# 여유가 생길 때까지 이 요청을 대기시킨다(최대 MAX_QUEUE_WAIT_SECONDS, 기본 5분).
# ⚠️ 단일 gunicorn 프로세스(워커 1개, 스레드 여러 개) 전제 — 워커를 여러 프로세스로
#    늘리면 이 카운터를 Redis 등 프로세스 공유 저장소로 옮겨야 정확해진다.
import threading
import time
from collections import deque

_send_times: deque = deque()
_send_lock = threading.Lock()

RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "80"))
# 200건 한도를 "잠시만요" 캔드메시지(매니챗이 우리 몰래 보냄, 카운트 불가) +
# 우리 답변, 최소 2건/명 기준으로 넉넉히 나눠서 기본값을 80으로 보수적으로 잡음.
RATE_WINDOW_SECONDS = 3600
MAX_QUEUE_WAIT_SECONDS = int(os.environ.get("MAX_QUEUE_WAIT_SECONDS", "300"))  # 5분


def _throttle_for_instagram_limit() -> None:
    deadline = time.monotonic() + MAX_QUEUE_WAIT_SECONDS
    while True:
        now = time.monotonic()
        with _send_lock:
            while _send_times and now - _send_times[0] > RATE_WINDOW_SECONDS:
                _send_times.popleft()
            if len(_send_times) < RATE_LIMIT_PER_HOUR:
                _send_times.append(now)
                return
        if time.monotonic() >= deadline:
            # 5분 넘게 기다렸으면 더는 막지 않고 진행(응답을 아예 안 주는 것보단 낫다).
            with _send_lock:
                _send_times.append(time.monotonic())
            return
        time.sleep(2)


# ---------------------------------------------------------------------------
# 무료 대화 턴 제한 (계정 초반 리스크 관리)
# ---------------------------------------------------------------------------
# 한 세션(첫 풀이 포함)에서 MAX_FREE_TURNS 번까지만 퀵리플라이 버튼을 보여준다.
# 그 이후 질문에도 답변 자체는 계속 해주되, 버튼을 더 안 띄워서 대화가 자연스럽게
# 잦아들게 한다(무제한 왕복으로 인스타 발송량이 계속 느는 걸 막는 목적도 겸함).
# 나중에 유료 랜딩페이지로 유도하고 싶으면 LANDING_PAGE_URL 환경변수만 채우면
# 자동으로 "더 보려면 여기" CTA 문구 + 링크가 붙는다(지금은 비워두면 조용히 버튼만 멈춤).
MAX_FREE_TURNS = int(os.environ.get("MAX_FREE_TURNS", "4"))
LANDING_PAGE_URL = os.environ.get("LANDING_PAGE_URL", "").strip()


def _apply_turn_limit(prior_history: list, out: dict) -> dict:
    turn_index = len(prior_history) // 2 + 1  # 이번 응답이 세션에서 몇 번째 턴인지(1부터, 첫 풀이=1)
    out = dict(out)
    if turn_index <= MAX_FREE_TURNS:
        out["show_buttons"] = True
        return out
    # 매니챗은 quick_replies 배열이 비어도 qr1/qr2/qr3 커스텀필드를 '비우지' 않고
    # 이전 값을 그대로 들고 있는다(Response mapping 이 매핑 대상이 없으면 no-op이라
    # 필드가 클리어되지 않음) — 그래서 버튼이 안 사라지고 계속 떠 있는 문제가 생긴다.
    # 대신 명시적인 show_buttons 플래그를 내려줘서, 매니챗 쪽에서 이 값으로
    # Condition(분기) 걸어 버튼 있는/없는 Send Message 블록을 나눠 타게 한다.
    out["quick_replies"] = []
    out["show_buttons"] = False
    if LANDING_PAGE_URL:
        out["reply"] = out["reply"] + f"\n\n더 자세히 보고 싶으면 여기서 확인해봐 👉 {LANDING_PAGE_URL}"
        out["cta_url"] = LANDING_PAGE_URL
    return out


# ---------------------------------------------------------------------------
# 공통 로직
# ---------------------------------------------------------------------------

def _resolve_birth(body: dict) -> dict:
    """body 에서 생년월일시를 뽑는다. text 가 있으면 파싱, 아니면 구조화 필드 사용."""
    if body.get("text"):
        info = parse_birth_info(body["text"])
        if not is_complete(info):
            raise ValueError(BIRTH_REQUEST_MSG)
        return {
            "year": info["year"], "month": info["month"], "day": info["day"],
            "hour": info.get("hour", 12), "minute": info.get("minute", 0),
            "gender": info.get("gender"), "is_lunar": info.get("is_lunar", False),
        }
    try:
        return {
            "year": int(body["year"]), "month": int(body["month"]), "day": int(body["day"]),
            "hour": int(body.get("hour", 12)), "minute": int(body.get("minute", 0)),
            "gender": body.get("gender"), "is_lunar": bool(body.get("is_lunar", False)),
        }
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"year/month/day 는 필수이며 정수여야 합니다. ({e})")


def _start_session(body: dict) -> dict:
    _throttle_for_instagram_limit()
    user_id = str(body.get("user_id") or uuid.uuid4())
    name = (body.get("name") or "").strip() or None
    category = body.get("category") or "종합운"
    city = body.get("city") or DEFAULT_CITY

    b = _resolve_birth(body)
    result = calculate(
        year=b["year"], month=b["month"], day=b["day"], hour=b["hour"], minute=b["minute"],
        city=city, gender=b["gender"], is_lunar=b["is_lunar"],
    )
    saju = result.to_prompt_dict()
    sessions.create(user_id, name, saju)

    out = _apply_turn_limit([], first_reading(name, saju, category=category))
    sessions.append_message(user_id, "user", f"({category} 첫 풀이 요청)")
    sessions.append_message(user_id, "assistant", out["reply"])
    return {
        "user_id": user_id, "reply": out["reply"], "quick_replies": out["quick_replies"],
        "show_buttons": out["show_buttons"], "saju": saju,
    }


def _continue_session(body: dict) -> dict:
    _throttle_for_instagram_limit()
    user_id = str(body.get("user_id") or "")
    message = (body.get("message") or "").strip()
    if not user_id or not message:
        raise ValueError("user_id 와 message 는 필수입니다.")
    s = sessions.get(user_id)
    if not s:
        raise LookupError("세션이 없습니다. 먼저 생년월일시로 상담을 시작해주세요.")

    out = _apply_turn_limit(s["history"], answer(s["name"], s["saju"], s["history"], message))
    sessions.append_message(user_id, "user", message)
    sessions.append_message(user_id, "assistant", out["reply"])
    return {
        "user_id": user_id, "reply": out["reply"], "quick_replies": out["quick_replies"],
        "show_buttons": out["show_buttons"],
    }


def _handle_message(body: dict) -> dict:
    """단일 엔드포인트로 '첫 연락(생년월일시 파싱→세션생성→첫풀이)'과
    '후속 질문(세션 이어서 답변)'을 모두 처리. 매니챗 쪽에 automation을
    두 개(트리거용/대화용) 따로 안 만들어도 되게 하기 위함."""
    _throttle_for_instagram_limit()
    user_id = str(body.get("user_id") or "")
    text = (body.get("text") or body.get("message") or "").strip()
    name = (body.get("name") or "").strip() or None
    if not user_id or not text:
        raise ValueError("user_id 와 text(또는 message) 는 필수입니다.")

    info = parse_birth_info(text)
    new_complete = is_complete(info)

    s = sessions.get(user_id)
    if s and new_complete:
        # 이미 세션이 있는데(예: 본인 사주 상담 중) 메시지 안에 '완전한' 생년월일이 또
        # 들어있으면, 십중팔구 다른 사람(가족/지인) 사주를 물어보려는 것이다. 이 경우
        # 기존 세션의 사주로 답변(answer())하면 안 된다 — Claude가 엉뚱한 사람 사주를
        # 기준으로 답하거나, "그 사람 정보는 없다"며 거절하는 어색한 응답이 나온다.
        # 날짜가 기존 세션과 다르면 새 사람으로 간주하고 세션을 새로 시작한다.
        same_date = s["saju"].get("생년월일시") == f"{info['year']:04d}-{info['month']:02d}-{info['day']:02d}"
        if not same_date:
            s = None

    if s and not new_complete and _YEAR_LIKE_RE.search(text):
        # 세션은 있는데(본인 상담 중) 메시지에 연도로 보이는 숫자가 있어서 새 생년월일을
        # 시도한 것 같은데, 월/일 등 일부가 파싱에 실패해 '완전한' 날짜가 안 됐다.
        # 이걸 그냥 후속질문으로 넘기면 Claude가 기존 세션 사주 기준으로 엉뚱하게 답하거나
        # "그 사람 정보 없다"고 어색하게 거절해버린다 — 명확한 재입력 안내로 대체한다.
        return {
            "user_id": user_id,
            "reply": "새 생년월일시를 알려주려는 것 같은데 일부가 인식이 안 됐어! " + BIRTH_REQUEST_MSG,
            "quick_replies": [],
            "show_buttons": False,
            "new_session": False,
        }

    if s:
        out = _apply_turn_limit(s["history"], answer(s["name"], s["saju"], s["history"], text))
        sessions.append_message(user_id, "user", text)
        sessions.append_message(user_id, "assistant", out["reply"])
        return {
            "user_id": user_id, "reply": out["reply"], "quick_replies": out["quick_replies"],
            "show_buttons": out["show_buttons"], "new_session": False,
        }

    if not new_complete:
        return {
            "user_id": user_id,
            "reply": BIRTH_REQUEST_MSG,
            "quick_replies": [],
            "show_buttons": False,
            "new_session": False,
        }

    result = calculate(
        year=info["year"], month=info["month"], day=info["day"],
        hour=info.get("hour", 12), minute=info.get("minute", 0),
        city=body.get("city") or DEFAULT_CITY, gender=info.get("gender"), is_lunar=info.get("is_lunar", False),
    )
    saju = result.to_prompt_dict()
    sessions.create(user_id, name, saju)
    out = _apply_turn_limit([], first_reading(name, saju, category=body.get("category") or "종합운"))
    sessions.append_message(user_id, "user", f"(생년월일시 제공: {text})")
    sessions.append_message(user_id, "assistant", out["reply"])
    return {
        "user_id": user_id, "reply": out["reply"], "quick_replies": out["quick_replies"],
        "show_buttons": out["show_buttons"], "new_session": True, "saju": saju,
    }


def _manychat(result: dict) -> dict:
    """매니챗 'Dynamic Content' 블록용 응답 포맷 (External Request 의 Response mapping
    필드 2개 제한을 피하려면 이 엔드포인트(/api/manychat/*)를 Dynamic Content 블록에
    연결한다 — 필드 매핑 없이 이 JSON 전체가 메시지+버튼으로 그대로 렌더링됨).

    quick_replies 는 messenger 스타일 텍스트 버튼(caption/title 둘 다 채워 호환성 확보)
    으로 최대 3개까지 실어 보낸다. 혹시 매니챗이 이 구조를 못 읽으면(버튼이 안 뜨면)
    최소한 텍스트로는 추천 질문이 보이게 본문에도 짧게 덧붙인다."""
    text = result["reply"]
    qs = (result.get("quick_replies") or [])[:3]
    if qs:
        text += "\n\n💬 " + " / ".join(qs)
    message = {"type": "text", "text": text}
    if qs:
        message["quick_replies"] = [
            {"type": "text", "caption": q, "title": q} for q in qs
        ]
    return {
        "version": "v2",
        "content": {"messages": [message]},
        "user_id": result.get("user_id"),
        "quick_replies": qs,  # 커스텀 필드로 따로 매핑하고 싶을 때 대비
    }


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    if os.path.exists(os.path.join(WEB_DIR, "index.html")):
        return send_from_directory(WEB_DIR, "index.html")
    return jsonify({"status": "ok", "hint": "web/index.html 없음 — API 만 사용 중"})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/session")
def api_session():
    if (u := _check_secret()):
        return u
    try:
        return jsonify(_start_session(_body()))
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except ChatError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"사주 계산 실패: {e}"}), 400


@app.post("/api/chat")
def api_chat():
    if (u := _check_secret()):
        return u
    try:
        return jsonify(_continue_session(_body()))
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except ChatError as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/message")
def api_message():
    """매니챗 등 단일 트리거(예: Instagram Default Reply = 모든 수신 메시지)용 통합 엔드포인트.
    세션 없으면 텍스트에서 생년월일시를 파싱해 새로 시작, 있으면 후속 질문으로 처리.
    body: {"user_id": "...", "text": "...", "name"?, "category"?}

    항상 HTTP 200 + {"reply", "quick_replies"} 형태로만 응답한다(에러 상황도 포함).
    매니챗 Response mapping 이 $.reply / $.quick_replies[0..2] 를 고정 매핑해두므로,
    그 필드가 없는 응답(에러용 {"error": ...})을 주면 "Response is null" 류로 매핑이
    깨진다 — 절대 그런 모양을 만들지 않는다."""
    if (u := _check_secret()):
        return u
    body = _body()
    try:
        return jsonify(_handle_message(body))
    except ValueError as e:
        return jsonify({
            "user_id": body.get("user_id"),
            "reply": BIRTH_REQUEST_MSG,
            "quick_replies": [],
            "show_buttons": False,
            "error": str(e),
        })
    except ChatError as e:
        return jsonify({
            "user_id": body.get("user_id"),
            "reply": "어 미안, 지금 답변 만드는 데 문제가 생겼어. 잠시 후에 다시 물어봐줄래? 🙏",
            "quick_replies": [],
            "show_buttons": False,
            "error": str(e),
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({
            "user_id": body.get("user_id"),
            "reply": "어 미안, 계산하다 오류가 났어. 생년월일시 형식 확인하고 다시 보내줄래? 🙏",
            "quick_replies": [],
            "show_buttons": False,
            "error": str(e),
        })


@app.post("/api/session/reset")
def api_reset():
    if (u := _check_secret()):
        return u
    body = _body()
    if body.get("user_id"):
        sessions.reset(str(body["user_id"]))
    return jsonify({"status": "ok"})


@app.post("/api/manychat/session")
def manychat_session():
    if (u := _check_secret()):
        return u
    try:
        return jsonify(_manychat(_start_session(_body())))
    except (ValueError, ChatError) as e:
        return jsonify({"version": "v2", "content": {"messages": [{"type": "text", "text": f"⚠️ {e}"}]}})
    except Exception as e:  # noqa: BLE001
        return jsonify({"version": "v2", "content": {"messages": [{"type": "text", "text": f"⚠️ 계산에 문제가 생겼어요. ({e})"}]}})


@app.post("/api/manychat/chat")
def manychat_chat():
    if (u := _check_secret()):
        return u
    try:
        return jsonify(_manychat(_continue_session(_body())))
    except (ValueError, LookupError, ChatError) as e:
        return jsonify({"version": "v2", "content": {"messages": [{"type": "text", "text": f"⚠️ {e}"}]}})


@app.post("/webhook/raw-text")
def webhook_raw_text():
    """단발 테스트용: 자유 텍스트 → 파싱 → 첫 풀이 1회 (세션 저장 안 함)."""
    if (u := _check_secret()):
        return u
    body = _body()
    info = parse_birth_info(body.get("text", ""))
    if not is_complete(info):
        return jsonify({"error": BIRTH_REQUEST_MSG, "parsed": info}), 422
    try:
        result = calculate(
            year=info["year"], month=info["month"], day=info["day"],
            hour=info.get("hour", 12), minute=info.get("minute", 0),
            city=body.get("city", DEFAULT_CITY), gender=info.get("gender"),
            is_lunar=info.get("is_lunar", False),
        )
        out = first_reading(body.get("name"), result.to_prompt_dict(), category=body.get("category", "종합운"))
    except ChatError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"사주 계산 실패: {e}"}), 400
    return jsonify({"reply": out["reply"], "quick_replies": out["quick_replies"], "saju": result.to_prompt_dict()})


# ---------------------------------------------------------------------------
# Meta 공식 Instagram Messaging API 연동용 뼈대 — 실제 계정으로 미검증.
# 배포 전 https://developers.facebook.com/docs/instagram-platform/ ... messaging-api/
# 최신 문서와 endpoint/payload 필드명을 반드시 대조하세요.
# ---------------------------------------------------------------------------

VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "change-me")


@app.get("/webhook/instagram")
def instagram_verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge") or "", 200
    return "forbidden", 403


@app.post("/webhook/instagram")
def instagram_receive():
    payload = _body()
    for entry in payload.get("entry", []):
        for ev in entry.get("messaging", []):
            sender_id = ev.get("sender", {}).get("id")
            text = ev.get("message", {}).get("text")
            if not sender_id or not text:
                continue
            try:
                if sessions.get(sender_id):
                    out = _continue_session({"user_id": sender_id, "message": text})
                else:
                    out = _start_session({"user_id": sender_id, "text": text})
                reply = out["reply"]
                if out.get("quick_replies"):
                    reply += "\n\n💬 " + "  /  ".join(out["quick_replies"])
            except (ValueError, LookupError) as e:
                reply = BIRTH_REQUEST_MSG
            except ChatError as e:
                reply = f"죄송해요, 지금 답변 생성에 문제가 생겼어요. ({e})"
            _send_instagram_reply(sender_id, reply)
    return jsonify({"status": "received"})


def _send_instagram_reply(recipient_id: str, text: str) -> None:
    import requests

    token = os.environ.get("META_PAGE_ACCESS_TOKEN")
    if not token:
        print(f"[DRY-RUN] {recipient_id} <- {text[:120]}")
        return
    requests.post(
        "https://graph.instagram.com/v21.0/me/messages",
        params={"access_token": token},
        json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        timeout=15,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
