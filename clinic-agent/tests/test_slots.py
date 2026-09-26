from datetime import date, time, timedelta, timezone

from app.calendar import slots as S
from app.config import get_clinic
from app.scheduling import tokens
from app.timeutil import to_local
from tests.conftest import ist

SECRET = "s3cret"


def cfg():
    return get_clinic()


def local_hm(dt):
    return to_local(dt).strftime("%H:%M")


def test_day_grid_respects_hours_and_break():
    starts = S.day_slot_starts(cfg(), date(2026, 9, 28))  # Monday
    hm = [local_hm(s) for s in starts]
    assert hm[0] == "10:00" and hm[11] == "12:45"
    assert "13:00" not in hm and "16:45" not in hm  # lunch break
    assert hm[12] == "17:00" and hm[-1] == "19:45"
    assert len(starts) == 24


def test_saturday_morning_only_sunday_closed():
    assert len(S.day_slot_starts(cfg(), date(2026, 10, 3))) == 12
    assert S.day_slot_starts(cfg(), date(2026, 10, 4)) == []


def test_holiday_skipped():
    assert S.day_slot_starts(cfg(), date(2026, 10, 20)) == []
    got = S.candidate_slots(cfg(), date(2026, 10, 19), date(2026, 10, 21), ist("2026-10-18 09:00"))
    assert {to_local(s).date() for s in got} == {date(2026, 10, 19), date(2026, 10, 21)}


def test_past_and_min_lead_excluded():
    now = ist("2026-09-28 10:20")
    got = S.candidate_slots(cfg(), date(2026, 9, 28), date(2026, 9, 28), now)
    assert local_hm(got[0]) == "11:30"  # 10:20 + 60 min lead -> first slot at/after 11:20


def test_horizon():
    now = ist("2026-09-28 07:00")
    got = S.candidate_slots(cfg(), date(2026, 9, 28), date(2026, 12, 31), now)
    assert max(to_local(s).date() for s in got) <= date(2026, 9, 28) + timedelta(days=14)


def test_utc_storage_and_ist_day_boundary():
    # 10:00 IST is 04:30 UTC; an evening slot 19:45 IST is still the same local day
    starts = S.day_slot_starts(cfg(), date(2026, 9, 28))
    assert starts[0].tzinfo == timezone.utc and starts[0].hour == 4 and starts[0].minute == 30
    assert to_local(starts[-1]).date() == date(2026, 9, 28)
    # searching "today" late at night (after 18:30 UTC = next day IST) uses the IST date
    now = ist("2026-09-29 00:30")
    got = S.candidate_slots(cfg(), date(2026, 9, 29), date(2026, 9, 29), now)
    assert got and all(to_local(s).date() == date(2026, 9, 29) for s in got)


def test_busy_overlap_removed_with_buffer():
    c = cfg().model_copy(update={"buffer_minutes": 0})
    s1, s2, s3 = ist("2026-09-28 10:00"), ist("2026-09-28 10:15"), ist("2026-09-28 10:30")
    busy = [(ist("2026-09-28 10:20"), ist("2026-09-28 10:25"))]
    assert S.remove_busy(c, [s1, s2, s3], busy) == [s1.astimezone(timezone.utc), s3.astimezone(timezone.utc)] or \
        S.remove_busy(c, [s1, s2, s3], busy) == [s1, s3]
    c2 = cfg().model_copy(update={"buffer_minutes": 10})
    assert S.remove_busy(c2, [s1, s2, s3], busy) == []  # buffer on both sides reaches all three
    # touching edges do not overlap
    assert S.remove_busy(c, [s2], [(ist("2026-09-28 10:00"), ist("2026-09-28 10:15"))]) == [s2]


def test_available_slots_filters_booked_and_part_of_day():
    now = ist("2026-09-28 07:00")
    booked = [ist("2026-09-28 10:00")]
    got = S.available_slots(cfg(), date(2026, 9, 28), date(2026, 9, 28), now, [], booked, "evening")
    assert all(local_hm(s) >= "17:00" for s in got) and len(got) == 12
    got = S.available_slots(cfg(), date(2026, 9, 28), date(2026, 9, 28), now, [], booked, "morning")
    assert ist("2026-09-28 10:00") not in got and len(got) == 11
    got = S.available_slots(cfg(), date(2026, 9, 28), date(2026, 9, 28), now, [], [], None, time(18, 0))
    assert local_hm(got[0]) == "18:00"


def test_slot_id_signed_and_tamper_proof():
    s = ist("2026-09-28 10:15").astimezone(timezone.utc)
    sid = S.sign_slot_id(s, SECRET)
    assert S.verify_slot_id(sid, SECRET) == s
    body, sig = sid.split(".")
    forged = f"{int(body, 36) + 15:x}.{sig}"
    assert S.verify_slot_id(forged, SECRET) is None
    assert S.verify_slot_id(sid, "other") is None
    assert S.verify_slot_id("garbage", SECRET) is None


def test_sessions_and_tokens_from_grid():
    assert S.session_of_slot(cfg(), ist("2026-09-28 12:45"))[1] == "morning"
    assert S.session_of_slot(cfg(), ist("2026-09-28 17:00"))[1] == "evening"
    assert tokens.slot_token(cfg(), ist("2026-09-28 10:00"))[3] == "M-01"
    assert tokens.slot_token(cfg(), ist("2026-09-28 10:30"))[3] == "M-03"
    assert tokens.slot_token(cfg(), ist("2026-09-28 17:15"))[3] == "E-02"


def test_sequential_helpers():
    a, b, c = ist("2026-09-28 10:00"), ist("2026-09-28 10:45"), ist("2026-09-28 10:15")
    assert tokens.sequential_tokens([a, b, c]) == {a: 1, c: 2, b: 3}
    assert not tokens.tokens_frozen(cfg(), date(2026, 9, 29), ist("2026-09-28 19:59"))
    assert tokens.tokens_frozen(cfg(), date(2026, 9, 29), ist("2026-09-28 20:00"))
