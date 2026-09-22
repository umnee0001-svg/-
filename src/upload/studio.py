"""YouTube Studio 업로드 자동화 (Playwright).

MultiLogin 이 띄운 브라우저에 CDP 로 붙어서, 업로드 마법사를 순서대로 밟습니다.
제목 / 설명 / 해시태그 / 태그 / 시청자층 / 예약 공개 시각 / 쇼핑 제품 태그.

화면 요소는 전부 selectors.json 에 있습니다. 유튜브가 UI 를 바꾸면
이 파일이 아니라 selectors.json 을 고치세요.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# 유튜브가 강제하는 상한
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_MAX_CHARS = 500
# 설명에 넣어도 제목 위에 노출되는 건 앞 3개뿐이고, 15개를 넘으면 전부 무시됩니다.
HASHTAG_MAX = 15

SELECTORS_PATH = Path(__file__).with_name("selectors.json")


class UploadError(RuntimeError):
    """업로드 도중 화면을 못 찾았거나 유튜브가 거절했을 때."""


def load_selectors(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or SELECTORS_PATH).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 텍스트 만들기

def normalize_hashtags(tags: list[str]) -> list[str]:
    """#을 붙이고 공백을 빼고 중복을 없앱니다. 15개를 넘으면 잘라냅니다."""
    out: list[str] = []
    for tag in tags:
        tag = tag.strip().lstrip("#").replace(" ", "")
        if not tag:
            continue
        tag = "#" + tag
        if tag not in out:
            out.append(tag)
    return out[:HASHTAG_MAX]


def render(job: dict, channel: dict, *, when: datetime | None = None) -> dict:
    """템플릿을 채워 실제로 입력할 제목/설명/태그를 만듭니다."""
    defaults = channel["defaults"]
    hashtags = normalize_hashtags(job["hashtags"] or defaults["hashtags"])
    when = when or datetime.now()

    fields = {
        "title": job["title"],
        "description": job["description"],
        "hashtags": " ".join(hashtags),
        "date": when.strftime("%Y-%m-%d"),
        "time": when.strftime("%H:%M"),
        "channel": channel["name"],
    }

    try:
        title = defaults["title_template"].format(**fields).strip()
        description = defaults["description_template"].format(**fields).strip()
    except KeyError as exc:
        raise UploadError(
            f"[{channel['id']}] 템플릿에 알 수 없는 치환자 {exc} 가 있습니다. "
            f"쓸 수 있는 건 {sorted(fields)} 입니다."
        ) from exc

    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX]
    if len(description) > DESCRIPTION_MAX:
        description = description[:DESCRIPTION_MAX]

    # 태그 칸은 전체 500자 제한이라 넘치는 건 버립니다.
    tags, budget = [], TAGS_MAX_CHARS
    for tag in (t.lstrip("#") for t in hashtags):
        if len(tag) + 1 > budget:
            break
        tags.append(tag)
        budget -= len(tag) + 1

    return {"title": title, "description": description,
            "tags": tags, "hashtags": hashtags}


# ---------------------------------------------------------------- 화면 다루기

def _first(page, candidates: list[str], *, timeout: float = 15000,
           required: bool = True):
    """후보 셀렉터를 차례로 시도해서 먼저 보이는 요소를 돌려줍니다."""
    last_error = None
    per_try = max(timeout / max(len(candidates), 1), 2000)
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=per_try)
            return locator
        except Exception as exc:  # noqa: BLE001 - playwright 예외 종류가 다양함
            last_error = exc
    if not required:
        return None
    raise UploadError(
        f"화면 요소를 찾지 못했습니다. 시도한 셀렉터: {candidates}\n"
        f"  마지막 오류: {type(last_error).__name__}\n"
        f"  → 유튜브 화면이 바뀌었을 수 있습니다. selectors.json 을 고치세요."
    )


def _fill_rich_text(locator, text: str) -> None:
    """Studio 의 제목/설명 칸은 input 이 아니라 contenteditable 이라 지우고 다시 씁니다."""
    locator.click()
    locator.press("Control+a")
    locator.press("Delete")
    if text:
        locator.type(text, delay=8)


def _click_next(page, sel: dict, times: int) -> None:
    for _ in range(times):
        _first(page, sel["next_button"]).click()
        page.wait_for_timeout(700)


# ---------------------------------------------------------------- 업로드 단계

def _wait_for_processing(page, sel: dict, log, timeout_ms: int) -> None:
    """업로드/처리 진행률을 지켜봅니다. 다 못 기다려도 예약 발행은 되므로 경고만 남깁니다."""
    markers = sel["processing_done_markers"]
    waited = 0
    step = 3000
    last_seen = ""
    while waited < timeout_ms:
        label = _first(page, sel["upload_progress"], timeout=3000, required=False)
        text = (label.inner_text().strip() if label else "")
        if text and text != last_seen:
            log(f"업로드 진행: {text}")
            last_seen = text
        if any(marker in text for marker in markers):
            return
        page.wait_for_timeout(step)
        waited += step
    log(f"처리 완료 표시를 {timeout_ms // 1000}초 안에 못 봤습니다. "
        f"그대로 진행합니다(예약 발행은 영향 없음).", "warn")


def _set_schedule(page, sel: dict, when: datetime, settings: dict, log) -> None:
    """예약 공개 시각을 채웁니다. 날짜 표기는 채널 계정의 언어 설정을 따릅니다."""
    _first(page, sel["schedule_radio"]).click()
    page.wait_for_timeout(500)

    date_format = settings.get("studio", {}).get("date_format", "%Y. %m. %d.")
    time_format = settings.get("studio", {}).get("time_format", "%H:%M")

    trigger = _first(page, sel["schedule_date_trigger"], required=False)
    if trigger:
        trigger.click()
        page.wait_for_timeout(400)
    date_input = _first(page, sel["schedule_date_input"])
    date_input.click()
    date_input.press("Control+a")
    date_input.type(when.strftime(date_format), delay=30)
    date_input.press("Enter")
    page.wait_for_timeout(500)

    time_trigger = _first(page, sel["schedule_time_trigger"], required=False)
    if time_trigger:
        time_trigger.click()
        page.wait_for_timeout(400)
    time_input = _first(page, sel["schedule_time_input"])
    time_input.click()
    time_input.press("Control+a")
    time_input.type(when.strftime(time_format), delay=30)
    time_input.press("Enter")
    page.wait_for_timeout(500)
    log(f"예약 공개 시각 설정: {when.strftime(date_format)} {when.strftime(time_format)}")


def _set_visibility(page, sel: dict, job: dict, when: datetime | None,
                    settings: dict, log) -> None:
    visibility = job["visibility"]
    if visibility == "scheduled":
        if when is None:
            raise UploadError("visibility 가 scheduled 인데 publish_at 이 없습니다.")
        _set_schedule(page, sel, when, settings, log)
        return
    key = f"visibility_{visibility}"
    if key not in sel:
        raise UploadError(f"알 수 없는 공개 상태입니다: {visibility}")
    _first(page, sel[key]).click()
    log(f"공개 상태: {visibility}")


def _add_shopping_tags(page, sel: dict, products: list[str], log) -> None:
    """쇼핑 제품 태그. 자격이 없는 채널은 메뉴 자체가 없어서 조용히 건너뜁니다."""
    shop = sel["shopping"]
    entry = _first(page, shop["shopping_section"], timeout=8000, required=False)
    if entry is None:
        entry = _first(page, shop["monetization_tab"], timeout=5000, required=False)
        if entry is None:
            log("이 채널에는 쇼핑(제품 태그) 메뉴가 없습니다. 건너뜁니다. "
                "YPP 수익화와 스토어 연동/제휴 자격을 확인하세요.", "warn")
            return
    entry.click()
    page.wait_for_timeout(1000)

    search = _first(page, shop["product_search_input"], timeout=8000, required=False)
    if search is None:
        log("쇼핑 제품 검색창을 찾지 못했습니다. 제품 태그를 건너뜁니다.", "warn")
        return

    added = 0
    for product in products:
        search.click()
        search.press("Control+a")
        search.press("Delete")
        search.type(product, delay=25)
        page.wait_for_timeout(1800)

        result = _first(page, shop["product_result_item"], timeout=6000, required=False)
        if result is None:
            log(f"제품을 찾지 못했습니다: {product}", "warn")
            continue
        button = result.locator(shop["product_add_button"][0]).first
        try:
            button.click(timeout=4000)
        except Exception:  # noqa: BLE001
            result.click()
        page.wait_for_timeout(800)
        added += 1
        log(f"제품 태그 추가: {product}")

    if added:
        save = _first(page, shop["save_button"], timeout=6000, required=False)
        if save:
            save.click()
            page.wait_for_timeout(1200)
    log(f"제품 태그 {added}/{len(products)}개 적용")


def upload(page, job: dict, channel: dict, settings: dict, log) -> str:
    """업로드 한 건. 성공하면 영상 URL(못 읽으면 빈 문자열)을 돌려줍니다."""
    sel = load_selectors(settings.get("studio", {}).get("selectors_path"))
    video = Path(job["video_path"])
    if not video.exists():
        raise UploadError(f"영상 파일이 없습니다: {video}")

    when = datetime.fromisoformat(job["publish_at"]) if job["publish_at"] else None
    text = render(job, channel, when=when)

    log(f"업로드 시작: {video.name} → {channel['name']}")
    page.goto(sel["upload_url"], wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(2500)

    if _first(page, sel["signed_out_marker"], timeout=4000, required=False):
        raise UploadError(
            f"[{channel['id']}] 이 MultiLogin 프로필이 유튜브에 로그인돼 있지 않습니다. "
            f"프로필을 직접 열어 로그인한 뒤 다시 실행하세요."
        )

    # 1) 파일 넣기 — 파일 선택 창을 띄우지 않고 input 에 바로 꽂습니다.
    file_input = page.locator(sel["file_input"][0]).first
    try:
        file_input.wait_for(state="attached", timeout=20000)
    except Exception:  # noqa: BLE001
        file_input = _first(page, sel["file_input"])
    file_input.set_input_files(str(video))
    page.wait_for_timeout(4000)

    # 2) 제목 / 설명
    _fill_rich_text(_first(page, sel["title"], timeout=60000), text["title"])
    page.wait_for_timeout(400)
    _fill_rich_text(_first(page, sel["description"]), text["description"])
    log(f"제목: {text['title']}")
    log(f"해시태그: {' '.join(text['hashtags']) or '(없음)'}")

    # 3) 시청자층 — 안 고르면 '다음'이 막힙니다.
    kids_key = "made_for_kids" if channel["defaults"]["made_for_kids"] \
        else "not_made_for_kids"
    _first(page, sel[kids_key]).click()

    # 4) 태그 (자세히 보기 안에 있음)
    if text["tags"]:
        more = _first(page, sel["show_more"], timeout=6000, required=False)
        if more:
            more.click()
            page.wait_for_timeout(700)
        tags_input = _first(page, sel["tags_input"], timeout=8000, required=False)
        if tags_input:
            tags_input.click()
            tags_input.type(",".join(text["tags"]) + ",", delay=15)
            log(f"태그 {len(text['tags'])}개 입력")
        else:
            log("태그 입력칸을 찾지 못했습니다. 해시태그는 설명에 들어갔습니다.", "warn")

    # 5) 쇼핑 제품 태그 (있는 채널만)
    products = job["products"] or channel["defaults"]["shopping"]["products"]
    if channel["defaults"]["shopping"]["enabled"] and products:
        _add_shopping_tags(page, sel, products, log)

    # 6) 동영상 요소 → 검토 → 공개 상태
    _click_next(page, sel, 3)

    # 7) 공개 상태 / 예약
    _set_visibility(page, sel, job, when, settings, log)

    # 8) 처리 대기 후 완료
    _wait_for_processing(
        page, sel, log,
        int(settings.get("studio", {}).get("processing_timeout_seconds", 900)) * 1000,
    )
    _first(page, sel["done_button"]).click()
    page.wait_for_timeout(3000)

    link = _first(page, sel["video_link"], timeout=8000, required=False)
    url = ""
    if link:
        url = (link.get_attribute("href") or link.inner_text() or "").strip()
    close = _first(page, sel["close_dialog"], timeout=5000, required=False)
    if close:
        close.click()

    log(f"업로드 완료: {url or '(URL 을 읽지 못함)'}")
    return url
