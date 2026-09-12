# -*- coding: utf-8 -*-
"""
birth_parser.py
------------------
DM으로 사용자가 자유 텍스트로 보낸 생년월일시를 최대한 파싱해본다.
이름은 절대 요구하지 않는다 — 필요한 건 생년월일 / 태어난 시각 / 성별 뿐이고,
그마저도 시각·성별은 없으면 기본값(정오/성별불명)으로 계산 가능하다.

사람마다 숫자 표기 습관이 다 다르므로(예: "900710", "90.07.10", "1990년7월10일",
"90-7-10" 등) 최대한 포괄적으로 인식하도록 날짜/시간 패턴을 여러 개 순서대로
시도한다. 정규식만으로 완벽할 수는 없으니, 값이 애매하면(월 13, 일 35 등)
그 패턴은 버리고 다음 패턴으로 넘어간다 — 틀린 값을 억지로 만들어내는 것보다
"인식 실패 → 재입력 요청"이 훨씬 안전하다.
"""

from __future__ import annotations

import re
from typing import Optional, TypedDict


class BirthInfo(TypedDict, total=False):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    gender: Optional[str]
    is_lunar: bool
    raw_text: str


_DAY_PERIOD_AM = {"오전", "새벽", "아침"}
_DAY_PERIOD_PM = {"오후", "저녁", "밤", "낮"}


def _valid_ymd(year: int, month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= 31


def _parse_date(text: str) -> Optional[tuple[int, int, int]]:
    """날짜를 여러 표기 방식으로 순서대로 시도. 성공하면 (year, month, day)."""
    # 1) 구분자가 있는 형식: "1990년 7월 10일" / "1990-07-10" / "1990.07.10" /
    #    "1990/07/10" / "96년 5월 12일"(2자리+년) — 연도 2~4자리, 구분자
    #    년/./-// 를 자유롭게 섞어 써도(붙여쓰기 포함) 인식한다.
    m = re.search(r"(\d{2,4})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})\s*일?", text)
    if m:
        y_raw, mo_raw, d_raw = m.groups()
        year, month, day = int(y_raw), int(mo_raw), int(d_raw)
        if len(y_raw) <= 2:
            # 2자리 연도(예: 90 → 1990) — 00~29는 2000년대, 30~99는 1900년대로 추정.
            year = 2000 + year if year <= 29 else 1900 + year
        if _valid_ymd(year, month, day):
            return year, month, day

    # 2) 구분자 없는 8자리 주민번호 앞자리 스타일: "19900710"
    m = re.search(r"(?<!\d)(19|20)(\d{2})(\d{2})(\d{2})(?!\d)", text)
    if m:
        century, yy, mo_raw, d_raw = m.groups()
        year, month, day = int(century + yy), int(mo_raw), int(d_raw)
        if _valid_ymd(year, month, day):
            return year, month, day

    # 3) 구분자 없는 6자리: "900710" — 주민번호 앞자리처럼 흔히 쓰는 표기.
    m = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", text)
    if m:
        yy, mo_raw, d_raw = m.groups()
        yy_i, month, day = int(yy), int(mo_raw), int(d_raw)
        year = 2000 + yy_i if yy_i <= 29 else 1900 + yy_i
        if _valid_ymd(year, month, day):
            return year, month, day

    return None


def _apply_day_period(period: Optional[str], hour: int) -> int:
    if period in _DAY_PERIOD_AM:
        return 0 if hour == 12 else hour
    if period in _DAY_PERIOD_PM:
        return 12 if hour == 12 else hour + 12
    return hour


def _parse_time(text: str) -> Optional[tuple[int, int]]:
    """시간을 여러 표기 방식으로 순서대로 시도. 성공하면 (hour, minute)."""
    if re.search(r"자정", text):
        return 0, 0
    if re.search(r"정오", text):
        return 12, 0

    # "오전/오후/새벽/아침/저녁/밤/낮 N시 (M분)?"
    m = re.search(r"(오전|오후|새벽|아침|저녁|밤|낮)?\s*(\d{1,2})\s*시\s*(\d{1,2})?\s*분?", text)
    if m:
        period, hour_s, minute_s = m.groups()
        hour = _apply_day_period(period, int(hour_s))
        if 0 <= hour <= 23:
            minute = int(minute_s) if minute_s else 0
            if 0 <= minute <= 59:
                return hour, minute

    # "HH:MM"
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 구분자 없는 4자리 HHMM(예: "0505", "1735") — 생년월일 뒤에 시간만 붙여
    # 쓰는 경우의 보조 수단. 날짜(6/8자리 연속 숫자)와는 경계(전후 숫자 없음)로
    # 구분되므로 서로 겹쳐 오인식되지 않는다.
    m = re.search(r"(?<!\d)([01]\d|2[0-3])([0-5]\d)(?!\d)", text)
    if m:
        return int(m.group(1)), int(m.group(2))

    return None


def parse_birth_info(text: str) -> BirthInfo:
    """"1990년 7월 10일 오전 5시 남자", "1990-07-10 05:00", "90.7.10 5시 여성",
    "900710 0505 여", "19900710 17시 남" 등 표기가 제각각이어도 최대한 파싱한다.

    이름은 아예 다루지 않는다(요구하지도, 추출하지도 않음) — 챗봇은 이름 없이도
    사주 계산·해석이 가능하고, 대화 맥락에서 호칭이 필요하면 대화 기록에 남은
    원문을 보고 자연스럽게 이어가면 된다.

    실패한 필드는 결과 dict에 아예 들어가지 않으므로, 호출부에서
    'year' in info 등으로 필수값 존재 여부를 확인해야 합니다.
    """
    info: BirthInfo = {"raw_text": text}

    date = _parse_date(text)
    if date:
        info["year"], info["month"], info["day"] = date

    time_ = _parse_time(text)
    if time_:
        info["hour"], info["minute"] = time_

    # 성별
    if re.search(r"남자|남성|남\b", text):
        info["gender"] = "남성"
    elif re.search(r"여자|여성|여\b", text):
        info["gender"] = "여성"

    # 음력 여부
    info["is_lunar"] = bool(re.search(r"음력", text))

    return info


def is_complete(info: BirthInfo) -> bool:
    """사주 계산에 최소로 필요한 필드(연/월/일)가 모두 있는지 확인. 시간은 없으면 정오(12:00)로 대체 가능."""
    return all(k in info for k in ("year", "month", "day"))


if __name__ == "__main__":
    samples = [
        "양력 1990년 7월 10일 오전 5시 남자",
        "1990-07-10 05:00",
        "90.7.10 5시 여성",
        "음력 1990년 7월 10일 낮 12시",
        "900710 0505 여",
        "19900710 17시 남",
        "96년 5월 12일 오후 2시 남자",
    ]
    for s in samples:
        print(s, "->", parse_birth_info(s))
