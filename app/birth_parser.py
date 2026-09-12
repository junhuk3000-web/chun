# -*- coding: utf-8 -*-
"""
birth_parser.py
------------------
DM으로 사용자가 자유 텍스트로 보낸 생년월일시를 최대한 파싱해본다.

주의: 자연어 파싱은 완벽할 수 없습니다. 실제 서비스에서는 이 파서에만
의존하지 말고, 가능하면 프론트(자동화 툴)에서 '생년월일 선택 → 시간 선택
→ 성별 선택' 같은 버튼/퀵리플라이 단계로 나눠 받는 것을 권장합니다.
(관찰한 경쟁 계정도 결국 "아래 버튼을 누르거나" 식으로 버튼 입력을
우선 유도하고 있었습니다 — 자유 텍스트는 보조 수단으로만 쓰는 게 안전합니다.)
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


_AMPM_HOUR = {
    "오전": lambda h: 0 if h == 12 else h,
    "오후": lambda h: 12 if h == 12 else h + 12,
}


def parse_birth_info(text: str) -> BirthInfo:
    """"1990년 7월 10일 오전 5시 남자", "1990-07-10 05:00", "90.7.10 5시 여성" 등을 파싱.

    실패한 필드는 결과 dict에 아예 들어가지 않으므로, 호출부에서
    'year' in info 등으로 필수값 존재 여부를 확인해야 합니다.
    """
    info: BirthInfo = {"raw_text": text}

    # 1) 날짜: YYYY년 M월 D일 / YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD / YY년 M월 D일(2자리+'년') 등
    # 연도는 2~4자리, 구분자는 년/./-//를 자유롭게 섞어 써도(붙여쓰기 포함) 인식한다.
    # 실제 사용자들은 "96년 5월 12일"처럼 2자리+'년'을 아주 흔하게 쓰는데, 예전 버전은
    # 2자리 연도는 점/대시/슬래시 구분자에서만 인식해서 이 흔한 표기를 놓치는 문제가 있었다.
    m = re.search(r"(\d{2,4})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})\s*일?", text)
    if m:
        y_raw, mo, d = m.groups()
        year = int(y_raw)
        if len(y_raw) <= 2:
            # 2자리 연도(예: 90 → 1990) — 00~29는 2000년대, 30~99는 1900년대로 추정.
            # 애매한 경계값이므로 가능하면 4자리 연도 입력을 권장.
            year = 2000 + year if year <= 29 else 1900 + year
        info["year"], info["month"], info["day"] = year, int(mo), int(d)

    # 2) 시간: "오전/오후 N시", "N시 M분", "HH:MM"
    m = re.search(r"(오전|오후)?\s*(\d{1,2})\s*시\s*(\d{1,2})?\s*분?", text)
    if m:
        ampm, hour_s, minute_s = m.groups()
        hour = int(hour_s)
        if ampm:
            hour = _AMPM_HOUR[ampm](hour)
        info["hour"] = hour
        info["minute"] = int(minute_s) if minute_s else 0
    else:
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
        if m:
            info["hour"], info["minute"] = int(m.group(1)), int(m.group(2))

    # 3) 성별
    if re.search(r"남자|남성|남\b", text):
        info["gender"] = "남성"
    elif re.search(r"여자|여성|여\b", text):
        info["gender"] = "여성"

    # 4) 음력 여부
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
    ]
    for s in samples:
        print(s, "->", parse_birth_info(s))
