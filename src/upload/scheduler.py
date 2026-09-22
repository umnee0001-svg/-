"""예약 시각 배정.

채널마다 정해둔 슬롯(예: 매일 09:00, 18:00)을 앞에서부터 훑으면서
아직 아무도 안 쓴 시각을 대기중인 작업에 하나씩 붙여줍니다.

규칙 세 가지를 지킵니다.
  - weekdays 에 없는 요일은 건너뜀
  - 이미 잡힌 예약과 min_gap_minutes 보다 가깝게 붙지 않음
  - 하루에 max_per_day 개를 넘지 않음
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import queue as queue_mod

# 예약 시각을 몇 분 뒤부터 잡을지. 지금 당장으로 잡으면 업로드가 못 따라갑니다.
MIN_LEAD_MINUTES = 30

# 슬롯을 며칠 앞까지 찾아볼지. 이 안에 자리가 없으면 배정을 미룹니다.
SEARCH_DAYS = 60


def _slot_times(channel: dict, tz: ZoneInfo, start: datetime):
    """start 이후의 슬롯 시각을 시간순으로 내놓는 제너레이터."""
    slots = sorted(channel["schedule"]["slots"])
    weekdays = set(channel["schedule"]["weekdays"])
    day = start.astimezone(tz).date()
    for offset in range(SEARCH_DAYS):
        current = day + timedelta(days=offset)
        if current.weekday() not in weekdays:
            continue
        for slot in slots:
            hour, minute = (int(x) for x in slot.split(":"))
            when = datetime.combine(current, datetime.min.time(), tzinfo=tz)
            when = when.replace(hour=hour, minute=minute)
            if when >= start:
                yield when


def _conflicts(when: datetime, taken: list[datetime], gap_minutes: int,
               max_per_day: int | None) -> bool:
    if gap_minutes > 0:
        gap = timedelta(minutes=gap_minutes)
        if any(abs(when - other) < gap for other in taken):
            return True
    else:
        if any(when == other for other in taken):
            return True
    if max_per_day is not None:
        same_day = sum(1 for other in taken if other.date() == when.date())
        if same_day >= max_per_day:
            return True
    return False


def next_slots(channel: dict, tz: ZoneInfo, taken_iso: list[str], count: int,
               *, now: datetime | None = None) -> list[datetime]:
    """이 채널에서 다음으로 쓸 수 있는 예약 시각 count 개."""
    now = now or datetime.now(tz)
    start = now + timedelta(minutes=MIN_LEAD_MINUTES)
    taken = [datetime.fromisoformat(s).astimezone(tz) for s in taken_iso]

    gap = channel["schedule"]["min_gap_minutes"]
    cap = channel["schedule"]["max_per_day"]

    picked: list[datetime] = []
    for when in _slot_times(channel, tz, start):
        if len(picked) >= count:
            break
        if _conflicts(when, taken, gap, cap):
            continue
        picked.append(when)
        taken.append(when)
    return picked


def assign(conn, config: dict) -> list[tuple[int, str]]:
    """시각이 없는(queued) 작업들에 예약 시각을 붙입니다. [(job_id, 시각)] 을 돌려줍니다."""
    tz = ZoneInfo(config["timezone"])
    assigned: list[tuple[int, str]] = []

    for channel in config["channels"]:
        if not channel["enabled"]:
            continue
        waiting = [j for j in queue_mod.list_jobs(conn, channel_id=channel["id"],
                                                  status="queued")]
        if not waiting:
            continue
        taken = queue_mod.taken_slots(conn, channel["id"])
        slots = next_slots(channel, tz, taken, len(waiting))

        for job, when in zip(waiting, slots):
            stamp = when.isoformat(timespec="seconds")
            queue_mod.update_job(conn, job["id"],
                                 publish_at=stamp, status="scheduled")
            queue_mod.log(conn, job["id"], f"예약 시각 배정: {stamp}")
            assigned.append((job["id"], stamp))

        short = len(waiting) - len(slots)
        if short > 0:
            queue_mod.log(
                conn, None,
                f"[{channel['id']}] {SEARCH_DAYS}일 안에 빈 슬롯이 없어 "
                f"{short}개를 배정하지 못했습니다. slots 를 늘리거나 "
                f"min_gap_minutes 를 줄이세요.",
                level="warn",
            )
    return assigned
