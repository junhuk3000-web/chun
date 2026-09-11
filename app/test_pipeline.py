# -*- coding: utf-8 -*-
"""
test_pipeline.py
-------------------
계산 → 파싱 → 세션 → (Claude 해석은 mock) → 엔드포인트 라우팅을 로컬에서 빠르게 검증.
ANTHROPIC_API_KEY 가 있으면 마지막에 실제 Claude 호출 1회도 테스트한다.

  python test_pipeline.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass
# 테스트는 임시 sqlite 파일 사용
os.environ["SESSION_DB"] = os.path.join(tempfile.gettempdir(), "cmj_test_sessions.db")
if os.path.exists(os.environ["SESSION_DB"]):
    os.remove(os.environ["SESSION_DB"])

from birth_parser import is_complete, parse_birth_info  # noqa: E402
from saju_engine import calculate  # noqa: E402


def test_saju_engine():
    r = calculate(1990, 7, 10, 5, 0)
    assert r.pillars == {"year": "庚午", "month": "癸未", "day": "丙子", "hour": "庚寅"}, r.pillars
    assert r.day_master == "丙"
    assert r.sipsin["day_branch"] == "편관", r.sipsin  # 丙(양)+子(양,水)=관성+동일음양=편관
    assert sum(r.element_counts.values()) == 8
    print("[OK] saju_engine:", r.pillars)


def test_birth_parser():
    info = parse_birth_info("양력 1990년 7월 10일 오전 5시 남자")
    assert is_complete(info)
    assert (info["year"], info["month"], info["day"], info["hour"], info["gender"]) == (1990, 7, 10, 5, "남성")
    print("[OK] birth_parser:", info)


def test_session_store():
    import sessions
    s = sessions.create("u1", "서아", {"일간": "丙"})
    assert s["name"] == "서아" and s["history"] == []
    sessions.append_message("u1", "user", "금전운?")
    sessions.append_message("u1", "assistant", "...")
    assert len(sessions.get("u1")["history"]) == 2
    sessions.reset("u1")
    assert sessions.get("u1") is None
    print("[OK] sessions: create/append/reset")


def test_endpoints_with_mock_claude():
    import server as srv

    def fake_first(name, saju, category="종합운"):
        return {"reply": f"[MOCK 첫풀이:{category}] 일간={saju['일간']}", "quick_replies": ["질문A", "질문B"]}

    def fake_answer(name, saju, history, question):
        return {"reply": f"[MOCK 답변] '{question}' (기록 {len(history)}턴)", "quick_replies": ["다음질문"]}

    with mock.patch.object(srv, "first_reading", fake_first), mock.patch.object(srv, "answer", fake_answer):
        c = srv.app.test_client()

        r = c.post("/api/session", json={
            "name": "서아", "year": 1990, "month": 7, "day": 10, "hour": 5, "category": "금전운",
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        d = r.get_json()
        assert "MOCK 첫풀이:금전운" in d["reply"] and d["user_id"] and d["quick_replies"]
        uid = d["user_id"]
        print("[OK] /api/session ->", d["reply"])

        r2 = c.post("/api/chat", json={"user_id": uid, "message": "투자 재개는 언제가 좋아요?"})
        assert r2.status_code == 200, r2.get_data(as_text=True)
        d2 = r2.get_json()
        assert "MOCK 답변" in d2["reply"]
        print("[OK] /api/chat ->", d2["reply"])

        r3 = c.post("/api/chat", json={"user_id": "nope", "message": "x"})
        assert r3.status_code == 404
        print("[OK] 세션 없음 -> 404")

        r4 = c.post("/api/session", json={"name": "x", "text": "안녕하세요"})
        assert r4.status_code == 422
        print("[OK] 생일 파싱 실패 -> 422")

        r5 = c.post("/api/manychat/chat", json={"user_id": uid, "message": "결혼운도 봐주세요"})
        assert r5.status_code == 200 and r5.get_json()["version"] == "v2"
        print("[OK] /api/manychat/chat -> v2 포맷")


def test_live_claude_if_key():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[SKIP] ANTHROPIC_API_KEY 없음 - 실제 Claude 호출 생략")
        return
    from chat import first_reading
    r = calculate(1990, 7, 10, 5, 0)
    out = first_reading("서아", r.to_prompt_dict(), category="종합운")
    print("[OK] 실제 Claude 첫 풀이:\n", out["reply"], "\n추천:", out["quick_replies"])


if __name__ == "__main__":
    test_saju_engine()
    test_birth_parser()
    test_session_store()
    test_endpoints_with_mock_claude()
    test_live_claude_if_key()
    print("\n전체 통과 ✅")
