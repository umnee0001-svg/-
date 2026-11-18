"""업로드 연결부.

이 프로그램은 유튜브에 직접 접속하지 않는다. 대신 사용자가 이미 쓰고 있는 업로드
도구(예: Volcano Kit)를 `studio.config.json` 에 적힌 명령으로 실행한다.

    "upload": {
      "command": ["volcano", "upload", "{video}", "--title", "{title}",
                  "--description-file", "{description_file}", "--thumbnail", "{thumbnail}"]
    }

명령 안에 쓸 수 있는 자리표시자:
    {video} {thumbnail} {title} {description} {description_file}
    {tags} (쉼표로 이은 태그) {privacy} {publish_at} {meta} (전체 정보 JSON 경로)

명령은 셸을 거치지 않고 목록 그대로 실행하므로 제목에 따옴표나 특수문자가 있어도 안전하다.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "studio.config.json"
EXAMPLE_CONFIG_PATH = ROOT / "studio.config.example.json"

PRIVACY = ("private", "unlisted", "public")
PLACEHOLDERS = ("video", "thumbnail", "title", "description", "description_file",
                "tags", "privacy", "publish_at", "meta")

# 유튜브 제한
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 500


class UploadError(ValueError):
    pass


@dataclass
class UploadInfo:
    video: str
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail: str | None = None
    privacy: str = "private"
    publish_at: str | None = None   # 예약 공개 시각 (예: 2026-12-01T18:00)

    def validate(self) -> None:
        if not Path(self.video).is_file():
            raise UploadError(f"영상 파일이 없습니다: {self.video}")
        if not self.title.strip():
            raise UploadError("제목을 입력하세요.")
        if len(self.title) > TITLE_MAX:
            raise UploadError(f"제목은 {TITLE_MAX}자까지입니다 (지금 {len(self.title)}자).")
        if "<" in self.title or ">" in self.title:
            raise UploadError("유튜브 제목에는 < > 문자를 쓸 수 없습니다.")
        if len(self.description) > DESCRIPTION_MAX:
            raise UploadError(f"설명은 {DESCRIPTION_MAX}자까지입니다 (지금 {len(self.description)}자).")
        if len(",".join(self.tags)) > TAGS_TOTAL_MAX:
            raise UploadError(f"태그는 합쳐서 {TAGS_TOTAL_MAX}자까지입니다.")
        if self.thumbnail and not Path(self.thumbnail).is_file():
            raise UploadError(f"썸네일 파일이 없습니다: {self.thumbnail}")
        if self.privacy not in PRIVACY:
            raise UploadError(f"공개 범위는 {', '.join(PRIVACY)} 중 하나여야 합니다.")


def parse_tags(text: str) -> list[str]:
    """'a, b,#c' -> ['a', 'b', 'c'] (중복 제거, 순서 유지)."""
    seen: list[str] = []
    for raw in text.replace("\n", ",").split(","):
        tag = raw.strip().lstrip("#").strip()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def load_config() -> dict:
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    if not path.exists():
        return {"upload": {"command": []}}
    with path.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("upload", {}).setdefault("command", [])
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_package(info: UploadInfo) -> dict[str, str]:
    """영상 옆에 업로드 정보 파일을 쓴다: <영상>.upload.json, <영상>.description.txt"""
    video = Path(info.video)
    desc_path = video.with_suffix(".description.txt")
    desc_path.write_text(info.description, encoding="utf-8")
    meta_path = video.with_suffix(".upload.json")
    data = asdict(info)
    data["description_file"] = str(desc_path)
    meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"meta": str(meta_path), "description_file": str(desc_path)}


def build_command(template: list[str], info: UploadInfo, files: dict[str, str]) -> list[str]:
    """명령 템플릿의 {자리표시자} 를 채운다. 비어 있는 값만 들어가는 인자는 앞 옵션과 함께 뺀다."""
    if not template:
        raise UploadError(
            "업로드 명령이 아직 설정되지 않았습니다. "
            "'업로드 설정' 에서 지금 터미널에서 쓰는 명령을 적어 주세요."
        )
    values = {
        "video": str(Path(info.video).resolve()),
        "thumbnail": str(Path(info.thumbnail).resolve()) if info.thumbnail else "",
        "title": info.title,
        "description": info.description,
        "description_file": files["description_file"],
        "tags": ",".join(info.tags),
        "privacy": info.privacy,
        "publish_at": info.publish_at or "",
        "meta": files["meta"],
    }
    out: list[str] = []
    for part in template:
        try:
            filled = part.format(**values)
        except KeyError as exc:
            raise UploadError(
                f"모르는 자리표시자 {exc} 가 있습니다. 쓸 수 있는 것: "
                + ", ".join("{" + p + "}" for p in PLACEHOLDERS)
            ) from None
        if part != filled and filled == "":
            # 예: ["--thumbnail", "{thumbnail}"] 에서 썸네일이 없으면 둘 다 뺀다
            if out and out[-1].startswith("-"):
                out.pop()
            continue
        out.append(filled)

    exe = out[0]
    if not Path(exe).is_file() and not shutil.which(exe):
        raise UploadError(f"업로드 프로그램 '{exe}' 을(를) 찾을 수 없습니다. 설정의 명령을 확인하세요.")
    return out
