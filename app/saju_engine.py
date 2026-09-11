# -*- coding: utf-8 -*-
"""
saju_engine.py
----------------
생년월일시(+선택적으로 성별/도시)를 입력받아 사주팔자 원국과,
해석에 필요한 구조화된 데이터(오행 분포, 십신, 지지 관계)를 계산한다.

사주팔자 자체(연/월/일/시주, 진태양시 보정)는 오픈소스 라이브러리
sajupy(https://github.com/0ssw1/sajupy, MIT License)를 vendor하여 사용한다.
이 파일은 그 위에 오행/십신/지지관계 같은 '해석용 구조화 데이터'를
얹는 역할만 한다 — 이 부분이 서비스의 핵심 로직이 된다.

주의: 이 파일에서 계산하는 것은 전통 명리학 이론에 따른 '기계적 분류'다.
운세 해석 자체(좋다/나쁘다, 길흉)는 여기서 판단하지 않는다 — 그건 다음
단계(Claude API를 이용한 interpret.py)의 역할이다. 계산과 해석을
분리해두면 나중에 해석 엔진을 바꾸거나 검증하기 쉽다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 같은 디렉터리의 sajupy_vendor 패키지를 import 경로에 추가
sys.path.insert(0, str(Path(__file__).parent))
from sajupy_vendor import calculate_saju, lunar_to_solar  # noqa: E402

# ---------------------------------------------------------------------------
# 기초 상수
# ---------------------------------------------------------------------------

STEMS = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
BRANCHES = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]

# 천간 오행 (양간/음간)
STEM_ELEMENT = {
    "甲": "木", "乙": "木",
    "丙": "火", "丁": "火",
    "戊": "土", "己": "土",
    "庚": "金", "辛": "金",
    "壬": "水", "癸": "水",
}
STEM_YINYANG = {
    "甲": "양", "丙": "양", "戊": "양", "庚": "양", "壬": "양",
    "乙": "음", "丁": "음", "己": "음", "辛": "음", "癸": "음",
}

# 지지 오행 (본기 기준 — 지장간 세분화는 하지 않음)
BRANCH_ELEMENT = {
    "子": "水", "丑": "土", "寅": "木", "卯": "木",
    "辰": "土", "巳": "火", "午": "火", "未": "土",
    "申": "金", "酉": "金", "戌": "土", "亥": "水",
}
BRANCH_YINYANG = {
    "子": "양", "寅": "양", "辰": "양", "午": "양", "申": "양", "戌": "양",
    "丑": "음", "卯": "음", "巳": "음", "未": "음", "酉": "음", "亥": "음",
}

KOREAN_ELEMENT = {"木": "목", "火": "화", "土": "토", "金": "금", "水": "수"}

# 오행 상생(生): key가 value를 생함
GENERATES = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
# 오행 상극(剋): key가 value를 극함
CONTROLS = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}

# 지지 육합 / 충 / 원진 (2자 조합, 순서 무관)
BRANCH_HAP = {frozenset(p) for p in [("子", "丑"), ("寅", "亥"), ("卯", "戌"), ("辰", "酉"), ("巳", "申"), ("午", "未")]}
BRANCH_CHUNG = {frozenset(p) for p in [("子", "午"), ("丑", "未"), ("寅", "申"), ("卯", "酉"), ("辰", "戌"), ("巳", "亥")]}
BRANCH_WONJIN = {frozenset(p) for p in [("子", "未"), ("丑", "午"), ("寅", "酉"), ("卯", "申"), ("辰", "亥"), ("巳", "戌")]}

PILLAR_LABELS_KO = {"year": "연주", "month": "월주", "day": "일주", "hour": "시주"}


def _sipsin(day_stem: str, other_element: str, other_yinyang: str) -> str:
    """일간(日干) 기준으로 다른 오행/음양 조합의 십신(十神)을 판정한다."""
    day_element = STEM_ELEMENT[day_stem]
    day_yy = STEM_YINYANG[day_stem]
    same_yy = day_yy == other_yinyang

    if other_element == day_element:
        return "비견" if same_yy else "겁재"
    if GENERATES[day_element] == other_element:  # 내가 생하는 오행 = 식상
        return "식신" if same_yy else "상관"
    if CONTROLS[day_element] == other_element:  # 내가 극하는 오행 = 재성
        return "편재" if same_yy else "정재"
    if CONTROLS[other_element] == day_element:  # 나를 극하는 오행 = 관성
        return "편관" if same_yy else "정관"
    if GENERATES[other_element] == day_element:  # 나를 생하는 오행 = 인성
        return "편인" if same_yy else "정인"
    raise ValueError(f"매핑 불가: day={day_stem}, other={other_element}/{other_yinyang}")


@dataclass
class SajuResult:
    raw: dict                       # sajupy가 반환한 원본 딕셔너리
    pillars: dict                   # {"year": "庚午", ...}
    stems: dict                     # {"year": "庚", ...}
    branches: dict                  # {"year": "午", ...}
    day_master: str                 # 일간 (예: "丙")
    day_master_element: str         # 일간의 오행 (한글, 예: "화")
    element_counts: dict            # {"목":n, "화":n, ...} 8글자(천간4+지지4) 오행 분포
    sipsin: dict                    # {"year_stem": "겁재", "year_branch": "정관", ...} (일간 제외 7글자)
    branch_relations: list = field(default_factory=list)  # [{"pillars": ["month","day"], "branches": ["未","子"], "relation": "원진"}, ...]

    def to_prompt_dict(self) -> dict:
        """Claude에게 넘길 때 쓰기 좋은 평탄한(dict) 형태로 변환."""
        return {
            "생년월일시": self.raw.get("birth_date"),
            "시각": self.raw.get("birth_time"),
            "진태양시_보정": self.raw.get("solar_correction"),
            "사주원국": self.pillars,
            "일간": self.day_master,
            "일간_오행": self.day_master_element,
            "오행_분포": self.element_counts,
            "십신": self.sipsin,
            "지지_관계": self.branch_relations,
        }


def calculate(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int = 0,
    city: str = "Seoul",
    use_solar_time: bool = True,
    utc_offset: int = 9,
    gender: Optional[str] = None,
    is_lunar: bool = False,
) -> SajuResult:
    """생년월일시를 입력받아 SajuResult(사주원국 + 오행/십신 구조화 데이터)를 반환한다.

    gender는 관성/재성 위치 해석에 참고용으로만 결과에 실어 보낸다
    (계산 로직 자체를 바꾸지는 않음 — 해석 단계에서 Claude가 활용).
    is_lunar=True면 입력된 year/month/day를 음력으로 간주해 먼저 양력으로
    변환한 뒤 계산한다 (윤달은 아직 구분하지 않음 — 필요시 확장 지점).
    """
    if is_lunar:
        solar = lunar_to_solar(year, month, day)
        year, month, day = solar["solar_year"], solar["solar_month"], solar["solar_day"]

    raw = calculate_saju(
        year=year, month=month, day=day, hour=hour, minute=minute,
        city=city, use_solar_time=use_solar_time, utc_offset=utc_offset,
    )

    pillars = {k: raw[f"{k}_pillar"] for k in ("year", "month", "day", "hour")}
    stems = {k: raw[f"{k}_stem"] for k in ("year", "month", "day", "hour")}
    branches = {k: raw[f"{k}_branch"] for k in ("year", "month", "day", "hour")}

    day_master = stems["day"]
    day_master_element_ko = KOREAN_ELEMENT[STEM_ELEMENT[day_master]]

    # 오행 분포 (8글자: 천간4 + 지지4)
    element_counts = {v: 0 for v in KOREAN_ELEMENT.values()}
    for s in stems.values():
        element_counts[KOREAN_ELEMENT[STEM_ELEMENT[s]]] += 1
    for b in branches.values():
        element_counts[KOREAN_ELEMENT[BRANCH_ELEMENT[b]]] += 1

    # 십신 (일간 자기 자신은 제외한 7글자에 대해 계산)
    sipsin = {}
    for k in ("year", "month", "hour"):  # 천간 3개 (일간 제외)
        s = stems[k]
        sipsin[f"{k}_stem"] = _sipsin(day_master, STEM_ELEMENT[s], STEM_YINYANG[s])
    for k in ("year", "month", "day", "hour"):  # 지지 4개
        b = branches[k]
        sipsin[f"{k}_branch"] = _sipsin(day_master, BRANCH_ELEMENT[b], BRANCH_YINYANG[b])

    # 지지 4개 사이의 합/충/원진 관계 (6쌍 전수 비교)
    branch_relations = []
    keys = ["year", "month", "day", "hour"]
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            k1, k2 = keys[i], keys[j]
            b1, b2 = branches[k1], branches[k2]
            pair = frozenset((b1, b2))
            if b1 == b2:
                continue
            relation = None
            if pair in BRANCH_HAP:
                relation = "육합"
            elif pair in BRANCH_CHUNG:
                relation = "충"
            elif pair in BRANCH_WONJIN:
                relation = "원진"
            if relation:
                branch_relations.append({
                    "pillars": [PILLAR_LABELS_KO[k1], PILLAR_LABELS_KO[k2]],
                    "branches": [b1, b2],
                    "relation": relation,
                })

    return SajuResult(
        raw=raw,
        pillars=pillars,
        stems=stems,
        branches=branches,
        day_master=day_master,
        day_master_element=day_master_element_ko,
        element_counts=element_counts,
        sipsin=sipsin,
        branch_relations=branch_relations,
    )


if __name__ == "__main__":
    # 빠른 확인용: DM에서 봤던 예시(1990-07-10 05:00, 서울)와 결과 비교
    r = calculate(1990, 7, 10, 5, 0)
    import json
    print(json.dumps(r.to_prompt_dict(), ensure_ascii=False, indent=2))
