"""AI 체크: 제목 · 설명 · 태그 · 썸네일을 Claude 가 보고 점수와 고칠 점을 알려준다.

필요한 것:
    pip install anthropic
    API 키 (https://console.anthropic.com 에서 발급) — 스튜디오 '설정' 화면에 붙여넣거나
    환경변수 ANTHROPIC_API_KEY 로 지정

명령어로 써보기:
    python src/ai_review.py --title "제목" --description-file 설명.txt --thumbnail output/x_thumb.jpg
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

MODEL = "claude-opus-5-5"

# 모든 항목이 반드시 채워지도록 응답 형식을 고정한다 (structured outputs).
_SECTION = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0~100"},
        "good": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "good", "issues", "suggestions"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_score": {"type": "integer", "description": "0~100"},
        "summary": {"type": "string"},
        "title": {
            "type": "object",
            "properties": {
                **_SECTION["properties"],
                "alternatives": {"type": "array", "items": {"type": "string"}},
            },
            "required": [*_SECTION["required"], "alternatives"],
            "additionalProperties": False,
        },
        "description": {
            "type": "object",
            "properties": {
                **_SECTION["properties"],
                "rewrite": {"type": "string"},
            },
            "required": [*_SECTION["required"], "rewrite"],
            "additionalProperties": False,
        },
        "thumbnail": {
            "type": "object",
            "properties": {
                "provided": {"type": "boolean"},
                **_SECTION["properties"],
            },
            "required": ["provided", *_SECTION["required"]],
            "additionalProperties": False,
        },
        "tags": {
            "type": "object",
            "properties": {
                "issues": {"type": "array", "items": {"type": "string"}},
                "suggested": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["issues", "suggested"],
            "additionalProperties": False,
        },
        "policy": {
            "type": "object",
            "properties": {
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["risk", "notes"],
            "additionalProperties": False,
        },
    },
    "required": ["overall_score", "summary", "title", "description", "thumbnail", "tags", "policy"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """너는 한국 유튜브 롱폼 채널을 돕는 패키징(제목·설명·썸네일) 검토자다.
업로드 직전의 제목, 설명, 태그, 썸네일을 보고 클릭률과 검색 노출, 시청자 신뢰를 기준으로 평가한다.

평가 기준:
- 제목: 핵심 키워드가 앞쪽에 있는지, 모바일에서 잘리지 않는 길이(대략 40자 안쪽)인지, 호기심과 구체성,
  과장·낚시(내용과 다른 약속)가 없는지.
- 설명: 첫 두 줄(검색 결과와 '더보기' 전에 보이는 부분)에 영상의 가치가 드러나는지, 키워드가 자연스러운지,
  타임라인(00:00 형식)·관련 링크·해시태그가 적절한지. rewrite 에는 사용자가 그대로 붙여 넣을 수 있는
  개선된 설명 전문을 쓴다. 사용자가 쓴 사실(링크, 숫자, 이름)은 지어내지 말고 그대로 살린다.
  원래 설명이 비어 있으면 제목을 바탕으로 쓰되, 확인이 필요한 부분은 [대괄호]로 비워 둔다.
- 썸네일: 휴대폰의 작은 화면에서도 글자가 읽히는지(글자 수, 크기, 대비), 시선이 갈 한 가지 초점이 있는지,
  제목과 같은 말을 반복하지 않고 서로 보완하는지, 화면이 너무 복잡하지 않은지.
  썸네일이 없으면 provided 를 false, score 를 0 으로 하고 만들 때 참고할 점을 suggestions 에 쓴다.
- 태그: 너무 많거나 관련 없는 태그, 빠진 핵심 검색어.
- policy: 유튜브 정책상 위험(오해의 소지가 있는 메타데이터, 낚시성 썸네일, 선정적·폭력적 표현, 의료·금융의
  근거 없는 단정 등)을 low/medium/high 로 판단하고 이유를 notes 에 쓴다. 문제가 없으면 low 와 빈 목록.

작성 규칙:
- 모든 문장은 한국어로, 짧고 구체적으로. "좋아요" 같은 막연한 말 대신 무엇을 어떻게 바꿀지 쓴다.
- good / issues / suggestions 는 각각 0~4개. 해당 사항이 없으면 빈 목록.
- title.alternatives 는 바로 쓸 수 있는 제목 후보 3개 (각 100자 이하, < > 문자 금지).
- tags.suggested 는 쉼표 없이 단어나 짧은 구절로 5~12개.
- 점수는 0~100 정수. overall_score 는 세 항목을 종합한 값이다.
- summary 는 가장 먼저 고칠 한두 가지를 한두 문장으로.
"""


class ReviewError(RuntimeError):
    pass


def _image_block(path: Path) -> dict:
    media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp"}.get(path.suffix.lower())
    if not media:
        raise ReviewError(f"썸네일 형식을 읽을 수 없습니다: {path.name}")
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def build_content(*, title: str, description: str, tags: list[str],
                  thumbnail: str | None, notes: str = "") -> list[dict]:
    content: list[dict] = []
    if thumbnail:
        content.append(_image_block(Path(thumbnail)))
    lines = [
        "아래 업로드 정보를 검토해 줘.",
        "",
        f"[제목] ({len(title)}자)",
        title or "(비어 있음)",
        "",
        f"[설명] ({len(description)}자)",
        description or "(비어 있음)",
        "",
        "[태그]",
        ", ".join(tags) if tags else "(없음)",
        "",
        "[썸네일]",
        "위에 첨부한 이미지 (1280x720)" if thumbnail else "(없음)",
    ]
    if notes.strip():
        lines += ["", "[채널/영상 메모]", notes.strip()]
    content.append({"type": "text", "text": "\n".join(lines)})
    return content


def review(*, title: str, description: str = "", tags: list[str] | None = None,
           thumbnail: str | None = None, notes: str = "", api_key: str | None = None) -> dict:
    """Claude 에게 검토를 맡기고 REVIEW_SCHEMA 모양의 dict 를 돌려준다."""
    try:
        import anthropic
    except ImportError:
        raise ReviewError("AI 체크에는 anthropic 패키지가 필요합니다. 명령 프롬프트에서 "
                          "'pip install anthropic' 을 실행한 뒤 스튜디오를 다시 켜 주세요.") from None

    if not title.strip() and not description.strip() and not thumbnail:
        raise ReviewError("검토할 제목·설명·썸네일 중 하나는 있어야 합니다.")

    try:
        client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    except anthropic.AnthropicError:
        raise ReviewError("API 키가 없습니다. '업로드 설정' 화면의 AI 체크 항목에 API 키를 넣어 주세요.") from None

    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": REVIEW_SCHEMA},
            },
            # 안전 분류기가 거절하면 서버가 알아서 다른 모델로 다시 시도
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": build_content(
                title=title, description=description, tags=tags or [],
                thumbnail=thumbnail, notes=notes)}],
        )
    except anthropic.AuthenticationError:
        raise ReviewError("API 키가 올바르지 않습니다. 설정에서 키를 다시 확인해 주세요.") from None
    except anthropic.PermissionDeniedError:
        raise ReviewError("이 API 키로는 해당 모델을 쓸 수 없습니다. 콘솔에서 권한·결제 상태를 확인해 주세요.") from None
    except anthropic.RateLimitError:
        raise ReviewError("요청이 너무 많습니다. 1분쯤 뒤에 다시 시도해 주세요.") from None
    except anthropic.BadRequestError as exc:
        raise ReviewError(f"요청이 거절됐습니다: {exc.message}") from None
    except anthropic.APIStatusError as exc:
        raise ReviewError(f"AI 서버 오류({exc.status_code}). 잠시 뒤 다시 시도해 주세요.") from None
    except anthropic.APIConnectionError:
        raise ReviewError("AI 서버에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.") from None

    if response.stop_reason == "refusal":
        raise ReviewError("AI 가 이 내용의 검토를 거절했습니다. 제목·설명·썸네일에 민감한 표현이 있는지 확인해 주세요.")
    if response.stop_reason == "max_tokens":
        raise ReviewError("AI 응답이 너무 길어 중간에 끊겼습니다. 설명을 줄여서 다시 시도해 주세요.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise ReviewError("AI 응답을 읽지 못했습니다. 다시 시도해 주세요.") from None
    result["model"] = response.model
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ai_review.py", description="제목·설명·썸네일 AI 체크")
    p.add_argument("--title", default="")
    p.add_argument("--description", default="")
    p.add_argument("--description-file")
    p.add_argument("--tags", default="", help="쉼표로 구분")
    p.add_argument("--thumbnail")
    p.add_argument("--notes", default="", help="채널 주제·타깃 시청자 등 참고 메모")
    args = p.parse_args(argv)
    desc = args.description
    if args.description_file:
        desc = Path(args.description_file).read_text(encoding="utf-8")
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    result = review(title=args.title, description=desc, tags=tags,
                    thumbnail=args.thumbnail, notes=args.notes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReviewError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(1)
