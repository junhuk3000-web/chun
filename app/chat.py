# -*- coding: utf-8 -*-
"""
chat.py
---------
saju_engine 가 계산한 '이미 확정된' 사주 데이터를 근거로, 고객의 질문에
대화형(멀티턴)으로 답하는 모듈. 공식 anthropic 파이썬 SDK 사용.

설계 원칙 (interpret 단계에서 그대로 이어받음)
------------------------------------------------
1. 계산과 해석 분리. 사주팔자/오행/십신/지지관계는 saju_engine 이 '알고리즘'으로만
   계산하고, 여기서는 그 값을 사실(fact)로 못박아 전달한 뒤 문장만 생성하게 한다.
   Claude 가 간지 관계·십신·오행을 스스로 지어내거나 라벨을 바꾸지 못하게 한다.
2. 미래를 단정("반드시 ~된다")하지 말고 경향·가능성·조언 톤.
3. 의학·법률·특정 투자 종목 같은 확정 조언성 표현 금지.
4. "재미로 보는 운세" 같은 면책 문구는 넣지 않는다(브랜드 톤).

동작
----
- first_reading(): 세션 시작 시 첫 종합 풀이 + 추천 질문
- answer(): 이후 각 질문마다 [사주 사실 + 대화기록 + 질문] → 답변 + 추천 질문
사주 사실 JSON 과 시스템 프롬프트는 세션 내내 고정 → prompt caching 으로 반복 비용 절감.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Optional, TypedDict

import anthropic

# 기본 모델. 이 워크로드(짧은 한국어 해석문, 대량·저지연)엔 haiku/sonnet 가 합리적이다.
# .env 의 ANTHROPIC_MODEL 로 덮어쓸 수 있다.
#   비용 참고(1M 토큰당 입력/출력):  haiku-4-5 $1/$5 · sonnet-5 $2/$10 · opus-5 $5/$25
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("CHAT_MAX_TOKENS", "1024"))

CHANNEL_NAME = os.environ.get("CHANNEL_NAME", "천명재")

SYSTEM_PROMPT = f"""당신은 전통 명리학(사주팔자)에 정통한, {CHANNEL_NAME}의 사주 상담가입니다.
따뜻하지만 두루뭉술하지 않게, 자세하지만 어렵지 않게 이야기합니다. 존댓말을 씁니다.

[대화 상대에 대해]
사용자 메시지에 이미 별도의 계산 엔진이 만세력 기준으로 정확히 산출한 사주 원국
데이터(사주팔자, 일간, 오행 분포, 십신, 지지 관계)가 JSON으로 주어집니다. 이 값들은
이미 검증된 사실입니다.

[반드시 지킬 것]
1. 주어진 JSON에 없는 간지 관계·십신·오행을 새로 만들거나 다르게 바꾸지 마세요.
   특히 십신 라벨(정관/편관/정재/편재/식신/상관/비견/겁재/정인/편인)은 JSON 값을
   그대로 존중해서 설명하고, 임의로 다른 십신으로 바꿔 부르지 마세요.
2. 오행 생극, 십신의 의미, 합·충·원진 같은 전통 개념으로 자연스럽게 풀되,
   미래를 확정적으로 단정("반드시 ~된다", "100% ~한다")하지 말고
   경향·가능성·조언의 톤으로 쓰세요.
3. 의학적·법적·재정적 확정 조언으로 읽힐 표현(특정 투자 종목·진단명·특정 날짜 단정 등)은
   쓰지 마세요.
4. "재미로 보는 운세", "참고용입니다" 같은 면책성 문구는 넣지 마세요.
5. 사용자가 물은 주제에만 초점을 맞춰 3~6문장으로 답하세요. 이모지는 필요하면 1개까지만.
   같은 사람과의 대화이므로 앞서 한 이야기와 모순되지 않게, 자연스럽게 이어가세요.
6. 이 답변은 인스타그램 DM/채팅 말풍선에 그대로 표시됩니다. 마크다운 문법을 쓰지 마세요
   (제목 `#`, 굵게 `**`, 목록 `-`/`1.` 금지). 소제목이나 섹션 구분 없이, 문단으로 자연스럽게
   이어지는 대화체 텍스트만 쓰세요.
6. 답변 맨 마지막 줄에는 사용자가 이어서 물어볼 만한 질문 2~3개를
   `[추천질문] 질문1 | 질문2 | 질문3`
   형식으로 정확히 한 줄 붙이세요. (이 줄은 화면에서 버튼으로 쓰입니다)
"""

CATEGORY_GUIDE = {
    "종합운": "성격, 재능, 인간관계, 전반적인 삶의 흐름을 균형 있게 종합해서 설명해주세요.",
    "연애운": "새 인연을 만날 가능성이 높은 시기·상황과, 이 사람이 연애를 대하는 성향을 중심으로.",
    "재회운": "헤어진 인연과 다시 만날 가능성, 그리고 그 시기에 영향을 주는 사주 구조를 중심으로.",
    "금전운": "재물을 모으고 불리는 방식, 주의해야 할 지출/투자 성향을 중심으로.",
    "결혼운": "배우자궁(일지)과 관성/재성의 상태를 바탕으로 결혼 인연의 시기와 특징을.",
    "직업운": "직업 적성, 조직 vs 독립 성향, 커리어 전환에 유리한 흐름을 중심으로.",
}


class ChatError(RuntimeError):
    pass


class ChatResult(TypedDict):
    reply: str
    quick_replies: list  # ["질문1", "질문2", ...]


@lru_cache(maxsize=1)
def _client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ChatError("ANTHROPIC_API_KEY가 설정되어 있지 않습니다. .env 또는 환경변수로 지정하세요.")
    return anthropic.Anthropic()


def _facts_block(name: Optional[str], saju: dict) -> str:
    payload = {"이름": name, "사주_데이터": saju}
    return "다음은 이 사람의 확정된 사주 원국 데이터입니다:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


_FOLLOWUP_RE = re.compile(r"\[추천질문\]\s*(.+)\s*$")


def _split_followups(text: str) -> ChatResult:
    m = _FOLLOWUP_RE.search(text.strip())
    if not m:
        return {"reply": text.strip(), "quick_replies": []}
    reply = text[: m.start()].strip()
    qs = [q.strip(" ·-") for q in m.group(1).split("|")]
    qs = [q for q in qs if q][:3]
    return {"reply": reply, "quick_replies": qs}


def _call(name: Optional[str], saju: dict, messages: list, model: Optional[str] = None) -> ChatResult:
    # system: [고정 지침 + 사주 사실] 두 블록 모두 캐시 → 이후 턴에서 재전송 비용 절감
    system_blocks = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _facts_block(name, saju), "cache_control": {"type": "ephemeral"}},
    ]
    try:
        resp = _client().messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            messages=messages,
        )
    except anthropic.APIStatusError as e:
        raise ChatError(f"Anthropic API 오류 {e.status_code}: {str(e)[:300]}") from e
    except anthropic.APIConnectionError as e:
        raise ChatError(f"Anthropic API 연결 실패: {e}") from e

    if resp.stop_reason == "refusal":
        raise ChatError("응답이 정책상 거부되었습니다.")
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if not text:
        raise ChatError(f"응답에서 텍스트를 찾지 못했습니다: {resp}")
    return _split_followups(text)


def first_reading(name: Optional[str], saju: dict, category: str = "종합운") -> ChatResult:
    guide = CATEGORY_GUIDE.get(category, CATEGORY_GUIDE["종합운"])
    user = (
        f"{('이 분(' + name + '님)') if name else '이 분'}의 사주를 처음 봐주세요. "
        f"먼저 '{category}'을(를) 중심으로 첫 풀이를 해주세요. 가이드: {guide}\n"
        "일간과 오행 균형에서 드러나는 기본 성향을 한 번 짚고, 주제에 맞는 흐름을 이어서 설명해주세요."
    )
    return _call(name, saju, [{"role": "user", "content": user}])


def answer(name: Optional[str], saju: dict, history: list, question: str) -> ChatResult:
    # history 는 [{"role","content"}] 형식. 마지막에 이번 질문을 붙여 호출.
    messages = list(history) + [{"role": "user", "content": question}]
    return _call(name, saju, messages)


if __name__ == "__main__":
    from saju_engine import calculate

    r = calculate(1990, 7, 10, 5, 0)
    out = first_reading("서아", r.to_prompt_dict(), category="금전운")
    print(out["reply"])
    print("추천:", out["quick_replies"])
