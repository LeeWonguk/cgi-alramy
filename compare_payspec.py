#!/usr/bin/env python3
"""우리가 만든 결제 판매정보를 **실제 관측과 대조**한다. 아무것도 보내지 않는다.

    python3 compare_payspec.py                 # logs/payspec/ 의 최신 관측
    python3 compare_payspec.py <파일> [<파일>…]  # 특정 관측
    python3 compare_payspec.py --all           # 전부

`booking.pay_mov_block`이 만든 것과 CGV가 실제로 보낸 `paymInfoCont.mov`를 필드
단위로 비교해 어긋난 자리를 알려 준다.

**왜 보내지 않고 대조부터 하는가.** paymInfoCont는 PG 콜백이 매출을 만들 때 읽는
판매정보다. 이게 틀리면 사용자가 카카오페이 승인을 누른 뒤 CGV가 그걸 못 읽는다
— 돈은 나가고 표는 안 나온다. 선점은 틀려도 좌석을 못 잡고 마는 것과 무게가
다르다. 그래서 "만들어서 보낸다"가 아니라 "만들어서 맞는지 본다"부터 한다.

차이가 0인 것을 서로 다른 예매 여럿에서 확인한 뒤에야 실제로 보낼지 정한다.
관측이 전부 같은 영화·극장·인원이면 0이 나와도 아직 근거가 얇다 — 다른 조합의
예매가 한 번은 있어야 한다.

상영표와 좌석맵은 지금 CGV에서 다시 받는다(조회뿐이라 부작용이 없다). 그래서
CGV 로그인이 되어 있어야 하고, 상영이 이미 지난 회차는 대조할 수 없다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from envfile import load_env

ROOT = Path(__file__).resolve().parent
SPEC_DIR = ROOT / "logs" / "payspec"


def _entry(entries: list, mark: str, side: str) -> dict | None:
    return next((e for e in entries
                 if mark in e["url"] and e["쪽"] == side), None)


def load_capture(path: Path) -> dict | None:
    """관측 하나에서 대조에 필요한 것만 꺼낸다. 모자라면 None."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    entries = doc.get("오간것") or []
    sale = _entry(entries, "insertIssSalProcTempInfo", "요청")
    hold = _entry(entries, "seatTempPrmp", "요청")
    price = _entry(entries, "MovAtktSeatPrc", "응답")
    grouped = _entry(entries, "searchGroupedPaymdList", "응답")
    cards = _entry(entries, "searchCrdCocdList", "응답")
    if not (sale and hold and price):
        missing = [name for name, got in
                   (("판매정보", sale), ("선점요청", hold), ("가격조회", price))
                   if not got]
        print(f"  건너뜀 ({', '.join(missing)}이 없습니다) — "
              f"선점 단계 관찰을 붙이기 전 관측일 수 있습니다")
        return None
    real = json.loads(sale["body"]["paymInfoCont"])
    pay_id = _entry(entries, "commonGetPayId", "요청")
    auth = _entry(entries, "onlineAuthRequestReserve", "요청")
    return {
        "real_mov": real["mov"],
        "real_info": real,
        "real_pay_id": pay_id["body"] if pay_id else None,
        "real_auth": auth["body"] if auth else None,
        "hold": hold["body"],
        "prices": (price["body"] or {}).get("data") or [],
        "seats": doc.get("seats") or [],
        "grouped": grouped["body"] if grouped else None,
        "cards": cards["body"] if cards else None,
    }


def build_ours(session, cap: dict) -> dict:
    """같은 예매를 우리 코드로 다시 만들어 본다."""
    import booking
    import seats as seats_mod

    hold = cap["hold"]
    site_no, ymd = hold["siteNo"], hold["scnYmd"]
    scns_no, sseq = hold["scnsNo"], str(hold["scnSseq"])
    mov_no = cap["real_mov"]["movNo"]

    rows = session.showtimes(site_no, mov_no, ymd)
    row = next((r for r in rows if str(r.get("scnsNo")) == scns_no
                and str(r.get("scnSseq")) == sseq), None)
    if row is None:
        raise LookupError(f"상영표에 그 회차가 없습니다 ({ymd} {scns_no}/{sseq}) "
                          f"— 이미 지난 회차일 수 있습니다")

    parsed = seats_mod.parse_seats(
        session.seat_map(site_no=site_no, scns_no=scns_no, ymd=ymd,
                         scn_sseq=sseq))
    # **선점 요청에 적힌 순서 그대로** 좌석을 세운다. 순서가 어긋나면 좌석마다
    # 값이 조용히 뒤바뀌어, 진짜 차이와 구분할 수 없는 소음이 된다.
    order = [s["seatLocNo"] for s in hold["seatPrmpDataList"]]
    by_loc = {s["seat_loc_no"]: s for s in parsed}
    picked = [by_loc[loc] for loc in order if loc in by_loc]
    if len(picked) != len(order):
        raise LookupError(f"좌석맵에서 {len(order)}석 중 {len(picked)}석만 찾았습니다")

    adnc = session.adnc_seat_info(site_no, scns_no, ymd, sseq, mov_no)
    mov_atkt_no = cap["real_mov"]["sellProductsList"][0]["movAtktNo"]
    ctx = {"scn_ymd": ymd, "site_no": site_no, "row": row}
    return booking.pay_mov_block(ctx, {"cust_no": hold.get("custNo")},
                                 picked, cap["prices"], mov_atkt_no, adnc)


# **CGV가 자기 자신과 어긋나는 자리.** 여기 적힌 것은 우리 잘못이 아니다.
#
#   ticketProducts.scnsNm — 관측 8건 중 7건은 상영관 이름이 들어 있고 1건만
#   null이었다(2026-09-07 14:22). 같은 극장·같은 상영관인데도 그랬으니, CGV의
#   화면이 그 값을 늘 채우지는 않는다는 뜻이다. 우리는 채우는 쪽으로 두었다 —
#   7건이 그랬고 뜻으로도 맞다.
#
# **숨기지 않고 따로 센다.** 조용히 빼면 "차이 0"이 무엇을 뜻하는지 흐려지고,
# 그냥 두면 매번 뜨는 소음이 진짜 문제를 덮는다.
KNOWN_DIVERGENCES = ("ticketProducts.scnsNm",)


def _is_known(diff: str) -> bool:
    return any(mark in diff for mark in KNOWN_DIVERGENCES)


def compare_one(session, path: Path) -> int | None:
    """관측 하나를 대조한다. 차이 개수를 돌려준다. 못 하면 None."""
    import booking

    print(f"\n── {path.name}")
    cap = load_capture(path)
    if cap is None:
        return None
    try:
        ours = build_ours(session, cap)
    except Exception as exc:  # noqa: BLE001 - 한 건이 안 돼도 나머지는 본다
        print(f"  건너뜀 ({exc})")
        return None

    diffs = booking.compare_pay_body(ours, cap["real_mov"], path="mov")
    diffs += _compare_info(cap, ours)
    diffs += _compare_requests(cap, ours)
    known = [d for d in diffs if _is_known(d)]
    diffs = [d for d in diffs if not _is_known(d)]
    missing = [d for d in diffs if "없음)" in d]
    wrong = [d for d in diffs if "없음)" not in d]
    tail = f" · 알려진 불일치 {len(known)}건" if known else ""
    print(f"  좌석 {cap['seats']} · 차이 {len(diffs)}개 "
          f"(값 불일치 {len(wrong)} · 필드 누락 {len(missing)}){tail}")
    for d in wrong:
        print(f"    값 다름  {d}")
    for d in missing:
        print(f"    누락    {d}")
    for d in known:
        print(f"    (알려진) {d} — CGV가 자기 자신과 어긋나는 자리")
    return len(diffs)


def _compare_info(cap: dict, mov: dict) -> list[str]:
    """판매정보 **전체**를 대조한다. `mov`는 이미 따로 봤으니 여기서는 뺀다.

    계정 정보(cust·cjOneUser·ipAddress)는 관측 파일에서 가려져 있어 값 대조가
    되지 않는다 — 그 셋은 실제 세션으로 따로 확인했다(암호문 일치).
    """
    import booking

    real = dict(cap["real_info"])
    info = booking.pay_info_content(
        {"row": {"siteNo": real.get("siteNo"), "prodNm": mov.get("movNm")},
         "site_nm": real.get("siteNm"), "site_no": real.get("siteNo"),
         "cash_receipt_no": real.get("cashReceiptInfo")},
        {}, mov,
        {"ipAddress": real.get("ipAddress"), "cust": real.get("cust"),
         "cjOneUser": real.get("cjOneUser")},
        paym_no=real.get("paymNo") or "", verify_no=real.get("paymVrifyNo") or "",
        pay_method=booking.pick_pay_method(cap.get("grouped")),
        credit_card=booking.pick_credit_card(cap.get("cards")))
    # mov는 위에서 따로 대조했다 — 두 번 세면 개수가 부풀려진다.
    for body in (info, real):
        body.pop("mov", None)
    return booking.compare_pay_body(info, real, path="paymInfoCont")


def _compare_requests(cap: dict, mov: dict) -> list[str]:
    """결제번호 요청과 결제 요청도 함께 대조한다.

    이 둘은 판매정보보다 훨씬 단순하지만, 금액을 가르는 규칙(부가세)이 여기
    들어 있어서 틀리면 결제창에 다른 금액이 뜬다.
    """
    import booking

    info = cap["real_info"]
    total = int(info.get("amountTotal") or 0)
    count = len(mov.get("sellProductsList") or [])
    ctx = {"row": {"siteNo": info.get("siteNo"),
                   "prodNm": mov.get("movNm")},
           "site_nm": info.get("siteNm"), "site_no": info.get("siteNo")}
    out: list[str] = []

    real_id = cap.get("real_pay_id")
    if real_id:
        ours = booking.pay_id_body(
            ctx, {"user_id": real_id.get("userId"),
                  "user_nm": real_id.get("userName")},
            total, count, today=real_id.get("saleDt"))
        out += booking.compare_pay_body(ours, real_id, path="commonGetPayId")

    real_auth = cap.get("real_auth")
    if real_auth:
        ours = booking.pay_auth_body(
            real_auth["paymNo"], _verify_no(real_auth), total,
            user_phone=real_auth.get("userPhone"),
            today=real_auth.get("expireDate"))
        out += booking.compare_pay_body(ours, real_auth,
                                        path="onlineAuthRequestReserve")
    return out


def _verify_no(auth: dict) -> str:
    """실제 요청에 실린 대조번호를 리다이렉트 주소에서 도로 꺼낸다.

    난수라 우리가 만든 것과 같을 수 없다 — 대조에서는 **같은 값을 넣고** 나머지
    조립이 맞는지만 본다.
    """
    import re
    found = re.search(r"paymVrifyNo=([^&]*)", auth.get("redirectUrl") or "")
    return found.group(1) if found else ""


def main() -> int:
    load_env()
    import cgv_login
    import store
    import watch

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--all" in sys.argv:
        paths = sorted(SPEC_DIR.glob("*_pay.json"))
    elif args:
        paths = [Path(a) for a in args]
    else:
        found = sorted(SPEC_DIR.glob("*_pay.json"))
        paths = found[-1:]
    paths = [p for p in paths if "_masked" not in p.name]
    if not paths:
        print(f"대조할 관측이 없습니다 ({SPEC_DIR}). 자동 결제를 켠 감시가 "
              f"한 번 결제하면 생깁니다.")
        return 1

    with store.pool().connection() as conn:
        row = conn.execute("select owner_id from cgv_accounts"
                           " order by owner_id limit 1").fetchone()
    owner = row["owner_id"] if row else None
    if owner is None:
        print("연동된 CGV 계정이 없습니다 — 상영표·좌석맵을 받을 수 없습니다.")
        return 1

    print(f"관측 {len(paths)}건 대조 (owner {owner})")
    totals = []
    with watch.CgvSession(headless=True) as session:
        session.use(owner)
        if not cgv_login.ensure_logged_in(owner, session):
            print("CGV 로그인에 실패했습니다.")
            return 1
        for path in paths:
            got = compare_one(session, path)
            if got is not None:
                totals.append(got)

    print()
    if not totals:
        print("대조한 관측이 없습니다.")
        return 1
    if set(totals) == {0}:
        print(f"차이 없음 — {len(totals)}건 모두 일치했습니다.")
        print("서로 다른 영화·극장·인원의 관측에서도 0이면 그때 보낼지 정합니다.")
        return 0
    print(f"차이가 남아 있습니다: {totals} (관측 {len(totals)}건)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
