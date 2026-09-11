# -*- coding: utf-8 -*-
"""
sessions.py
-------------
고객별 대화 세션 저장소. 한 고객(user_id)당:
  - 이름
  - 계산된 사주 원국(saju_engine.SajuResult.to_prompt_dict())  ← 한 번만 계산해서 재사용
  - 대화 기록(role/content 리스트)

저장 방식은 sqlite 파일 하나(SESSION_DB, 기본 sessions.db)라 서버를 재시작해도
진행 중이던 대화가 날아가지 않는다. 배포 시:
  - 단일 워커(gunicorn -w 1)면 그대로 OK.
  - 멀티 워커/멀티 인스턴스로 확장하면 이 모듈을 Postgres/Redis 구현으로 교체.
    (인터페이스: get / create / append_message / reset 만 맞추면 됨)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional, TypedDict

DB_PATH = os.environ.get("SESSION_DB", os.path.join(os.path.dirname(__file__), "sessions.db"))
HISTORY_MAX_TURNS = int(os.environ.get("HISTORY_MAX_TURNS", "20"))  # user+assistant 합쳐 최근 N개만 유지


class Session(TypedDict):
    user_id: str
    name: Optional[str]
    saju: dict
    history: list  # [{"role": "user"|"assistant", "content": "..."}]
    created_at: float
    updated_at: float


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _init() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                user_id     TEXT PRIMARY KEY,
                name        TEXT,
                saju_json   TEXT NOT NULL,
                history_json TEXT NOT NULL DEFAULT '[]',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
            """
        )


_init()


def _row_to_session(row: sqlite3.Row) -> Session:
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "saju": json.loads(row["saju_json"]),
        "history": json.loads(row["history_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get(user_id: str) -> Optional[Session]:
    with _conn() as c:
        row = c.execute("SELECT * FROM sessions WHERE user_id = ?", (user_id,)).fetchone()
    return _row_to_session(row) if row else None


def create(user_id: str, name: Optional[str], saju: dict) -> Session:
    """세션 생성(있으면 사주/이름 갱신하고 대화기록은 초기화)."""
    now = time.time()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO sessions (user_id, name, saju_json, history_json, created_at, updated_at)
            VALUES (?, ?, ?, '[]', ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                saju_json = excluded.saju_json,
                history_json = '[]',
                updated_at = excluded.updated_at
            """,
            (user_id, name, json.dumps(saju, ensure_ascii=False), now, now),
        )
    return get(user_id)  # type: ignore[return-value]


def append_message(user_id: str, role: str, content: str) -> None:
    s = get(user_id)
    if not s:
        raise KeyError(f"세션 없음: {user_id}")
    history = s["history"] + [{"role": role, "content": content}]
    if len(history) > HISTORY_MAX_TURNS:
        history = history[-HISTORY_MAX_TURNS:]
    with _conn() as c:
        c.execute(
            "UPDATE sessions SET history_json = ?, updated_at = ? WHERE user_id = ?",
            (json.dumps(history, ensure_ascii=False), time.time(), user_id),
        )


def reset(user_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
