"""
==============================================================
  test_kis_endpoints.py — KIS API 엔드포인트별 동작 확인 (진단용)

  휴장일에도 안전하게 실행 가능. 각 엔드포인트가
  - 정상 응답하는지 (인증/필드명 문제 없는지)
  - 휴장일이라 빈 데이터만 오는지
  를 구분해서 출력한다. 절대 매수/매도 주문은 호출하지 않음 (조회만).

  사용법:
    python test_kis_endpoints.py --code 005930
==============================================================
"""

import argparse
import json

from kis_intraday import KISIntraday


def section(title: str):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def run(code: str):
    trader = KISIntraday()

    # ── ① 인증 ──
    section("① 토큰 발급 (인증)")
    token = trader.get_access_token()
    if token:
        print(f"  ✅ 토큰 발급 성공 (모드: {'실전' if trader.is_real else '모의'})")
    else:
        print("  ❌ 토큰 발급 실패 — kis_config.json의 app_key/app_secret부터 확인 필요")
        print("     (이게 안 되면 아래 항목들도 전부 실패합니다)")
        return

    # ── ② 잔고조회 (휴장일에도 동작해야 함) ──
    section("② 잔고조회 — 휴장일 무관하게 동작해야 함")
    bal = trader.get_balance()
    print(f"  결과: 총평가 {bal.get('총평가금액', 0):,}원 / "
          f"예수금 {bal.get('예수금총액', 0):,}원 / 보유종목 {len(bal.get('holdings', []))}개")
    print("  ✅ 호출 자체가 에러 없이 끝났다면 인증·계좌 연결은 정상" if "총평가금액" in bal else "  ❌ 응답 이상")

    # ── ③ 현재가조회 (휴장일엔 마지막 종가가 나올 것으로 예상) ──
    section(f"③ 현재가조회 ({code}) — 휴장일엔 전일 종가가 나올 것으로 예상")
    price = trader.get_current_price(code)
    print(f"  결과: {price:,}원" if price else "  ❌ 0 또는 조회 실패")

    # ── ④ 투자자(수급) — 휴장일 무관하게 최근 영업일 데이터가 나와야 함 ──
    section(f"④ 투자자(외인·기관 수급) ({code}) — 휴장일 무관 동작 예상")
    flow = trader.get_investor_flow(code)
    print(f"  결과: {json.dumps(flow, ensure_ascii=False)}")
    if flow.get("date"):
        print(f"  ✅ 최근 영업일({flow['date']}) 데이터 정상 수신 — 필드명 매핑도 문제없음")
    else:
        print("  ❌ date가 비어있음 → 필드명(frgn_ntby_qty 등)이 실제 응답과 다를 수 있음, 직접 raw 응답 확인 필요")

    flow5 = trader.get_investor_flow_accumulated(code, days=5)
    print(f"  5일 누적: {json.dumps(flow5, ensure_ascii=False)}")

    # ── ⑤ 분봉조회 — 휴장일엔 빈 데이터일 가능성 높음 ──
    section(f"⑤ 당일분봉조회 ({code}) — 휴장일엔 빈 데이터 예상 (정상적인 제약)")
    df_min = trader.get_minute_chart(code, lookback_calls=1)
    if df_min.empty:
        print("  ⚠ 빈 데이터 — 휴장일이라 그런 것일 수도, 필드명 문제일 수도 있습니다.")
        print("     장 열리는 날 09:30 이후 다시 이 스크립트를 돌려서 재확인해주세요.")
    else:
        print(f"  ✅ {len(df_min)}건 수신 — 필드명 매핑 정상")
        print(df_min.tail(3).to_string())

    section("진단 요약")
    print("  - ①②③④가 정상이면: 인증/조회 시스템은 휴장일에도 이미 검증된 것")
    print("  - ⑤(분봉)는 장 열리는 날 다시 확인 필요 (오늘 빈 데이터는 정상일 가능성 높음)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", type=str, default="005930", help="테스트할 종목코드 (기본: 삼성전자)")
    args = parser.parse_args()
    run(args.code)
