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


def _check_secret():
    if WEBHOOK_SECRET is None:
        return None
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    return None


# ---------------------------------------------------------------------------
# 공통 로직
# ---------------------------------------------------------------------------

def _resolve_birth(body: dict) -> dict:
    """body 에서 생년월일시를 뽑는다. text 가 있으면 파싱, 아니면 구조화 필드 사용."""
    if body.get("text"):
        info = parse_birth_info(body["text"])
        if not is_complete(info):
            raise ValueError("생년월일을 인식하지 못했습니다. 예) '1990년 7월 10일 오전 5시 남자'")
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

    out = first_reading(name, saju, category=category)
    sessions.append_message(user_id, "user", f"({category} 첫 풀이 요청)")
    sessions.append_message(user_id, "assistant", out["reply"])
    return {"user_id": user_id, "reply": out["reply"], "quick_replies": out["quick_replies"], "saju": saju}


def _continue_session(body: dict) -> dict:
    user_id = str(body.get("user_id") or "")
    message = (body.get("message") or "").strip()
    if not user_id or not message:
        raise ValueError("user_id 와 message 는 필수입니다.")
    s = sessions.get(user_id)
    if not s:
        raise LookupError("세션이 없습니다. 먼저 생년월일시로 상담을 시작해주세요.")

    out = answer(s["name"], s["saju"], s["history"], message)
    sessions.append_message(user_id, "user", message)
    sessions.append_message(user_id, "assistant", out["reply"])
    return {"user_id": user_id, "reply": out["reply"], "quick_replies": out["quick_replies"]}


def _handle_message(body: dict) -> dict:
    """단일 엔드포인트로 '첫 연락(생년월일시 파싱→세션생성→첫풀이)'과
    '후속 질문(세션 이어서 답변)'을 모두 처리. 매니챗 쪽에 automation을
    두 개(트리거용/대화용) 따로 안 만들어도 되게 하기 위함."""
    user_id = str(body.get("user_id") or "")
    text = (body.get("text") or body.get("message") or "").strip()
    name = (body.get("name") or "").strip() or None
    if not user_id or not text:
        raise ValueError("user_id 와 text(또는 message) 는 필수입니다.")

    s = sessions.get(user_id)
    if s:
        out = answer(s["name"], s["saju"], s["history"], text)
        sessions.append_message(user_id, "user", text)
        sessions.append_message(user_id, "assistant", out["reply"])
        return {"user_id": user_id, "reply": out["reply"], "quick_replies": out["quick_replies"], "new_session": False}

    info = parse_birth_info(text)
    if not is_complete(info):
        return {
            "user_id": user_id,
            "reply": "생년월일시를 알려주시면 만세력으로 사주를 봐드릴게요. 예) '1990년 7월 10일 오전 5시 여성'처럼 편하게 보내주세요 🔮",
            "quick_replies": [],
            "new_session": False,
        }

    result = calculate(
        year=info["year"], month=info["month"], day=info["day"],
        hour=info.get("hour", 12), minute=info.get("minute", 0),
        city=body.get("city") or DEFAULT_CITY, gender=info.get("gender"), is_lunar=info.get("is_lunar", False),
    )
    saju = result.to_prompt_dict()
    sessions.create(user_id, name, saju)
    out = first_reading(name, saju, category=body.get("category") or "종합운")
    sessions.append_message(user_id, "user", f"(생년월일시 제공: {text})")
    sessions.append_message(user_id, "assistant", out["reply"])
    return {"user_id": user_id, "reply": out["reply"], "quick_replies": out["quick_replies"], "new_session": True, "saju": saju}


def _manychat(result: dict) -> dict:
    """매니챗 External Request 'Dynamic Content' 응답 포맷.
    추천질문은 v1 에서는 본문에 덧붙인다(버튼 배선은 매니챗 플로우에서 별도)."""
    text = result["reply"]
    qs = result.get("quick_replies") or []
    if qs:
        text += "\n\n💬 이어서 물어보실 수 있어요:\n" + "\n".join(f"· {q}" for q in qs)
    return {
        "version": "v2",
        "content": {"messages": [{"type": "text", "text": text}]},
        # 매니챗 커스텀 필드로도 받고 싶을 때 매핑용:
        "user_id": result.get("user_id"),
        "quick_replies": qs,
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
        return jsonify(_start_session(request.get_json(silent=True) or {}))
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
        return jsonify(_continue_session(request.get_json(silent=True) or {}))
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
    body: {"user_id": "...", "text": "...", "name"?, "category"?}"""
    if (u := _check_secret()):
        return u
    try:
        return jsonify(_handle_message(request.get_json(silent=True) or {}))
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except ChatError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"사주 계산 실패: {e}"}), 400


@app.post("/api/session/reset")
def api_reset():
    if (u := _check_secret()):
        return u
    body = request.get_json(silent=True) or {}
    if body.get("user_id"):
        sessions.reset(str(body["user_id"]))
    return jsonify({"status": "ok"})


@app.post("/api/manychat/session")
def manychat_session():
    if (u := _check_secret()):
        return u
    try:
        return jsonify(_manychat(_start_session(request.get_json(silent=True) or {})))
    except (ValueError, ChatError) as e:
        return jsonify({"version": "v2", "content": {"messages": [{"type": "text", "text": f"⚠️ {e}"}]}})
    except Exception as e:  # noqa: BLE001
        return jsonify({"version": "v2", "content": {"messages": [{"type": "text", "text": f"⚠️ 계산에 문제가 생겼어요. ({e})"}]}})


@app.post("/api/manychat/chat")
def manychat_chat():
    if (u := _check_secret()):
        return u
    try:
        return jsonify(_manychat(_continue_session(request.get_json(silent=True) or {})))
    except (ValueError, LookupError, ChatError) as e:
        return jsonify({"version": "v2", "content": {"messages": [{"type": "text", "text": f"⚠️ {e}"}]}})


@app.post("/webhook/raw-text")
def webhook_raw_text():
    """단발 테스트용: 자유 텍스트 → 파싱 → 첫 풀이 1회 (세션 저장 안 함)."""
    if (u := _check_secret()):
        return u
    body = request.get_json(silent=True) or {}
    info = parse_birth_info(body.get("text", ""))
    if not is_complete(info):
        return jsonify({"error": "생년월일을 인식하지 못했습니다.", "parsed": info}), 422
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
    payload = request.get_json(silent=True) or {}
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
                reply = f"생년월일시를 '1990년 7월 10일 오전 5시 남자' 형식으로 보내주세요 🙏 ({e})"
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
