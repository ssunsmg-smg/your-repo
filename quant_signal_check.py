"""
==============================================================
  quant_signal_check.py — 장중 L1(가격)+L2(거래량) 시그널 체크
  → 통과한 종목만 KIS 매수 실행

  실행 흐름:
    1) 전날 저녁 스크리닝(quant_daily.yml)이 만든 매매신호_KR_*.json 로드
       (이미 L3 수급 필터까지 반영된 "매수 후보" top_n)
    2) 후보 중 "오늘 아직 안 산" 종목만 골라 분봉 데이터로 L1+L2 게이트 체크
    3) 게이트 통과 → 매수 실행 (기존 KISAutoTrader.place_order 재사용)
       게이트 실패 → pending 상태 유지, 다음 실행(예: 10분 후)에 재시도
    4) 마감 컷오프 시각 이후엔 더 이상 신규 매수 시도 안 함

  실행 주기: VPS cron → cron-job.org 또는 GitHub Actions workflow_dispatch
            (장중 09:05~15:00 사이 10~15분 간격을 권장 — 너무 짧으면 API
             rate limit/비용 부담, 너무 길면 타이밍 의미 희석)

  사용법:
    python quant_signal_check.py --output-dir .
==============================================================
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import pandas as pd

# 기존 스크리너 파일과 같은 폴더에 있어야 함
from quant_screener_v41f import (
    BASE_DIR, TRADE_DIR,
    _find_latest_signal_json, _load_signal_json_as_df,
)
from kis_intraday import KISIntraday
from signal_engine import evaluate_entry_gate
import data_logger

PENDING_PATH_TMPL = os.path.join(TRADE_DIR, "intraday_pending_{date}.json")
MAX_TRIES_PER_STOCK = 30          # 하루 최대 재시도 횟수 (너무 오래 들고 있지 않도록)
CUTOFF_HOUR_MIN = (14, 50)        # 이 시각 이후엔 신규 매수 시도 중단 (장 마감 대비 여유)


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _load_pending(date_str: str) -> dict:
    path = PENDING_PATH_TMPL.format(date=date_str)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": date_str, "candidates": {}}


def _save_pending(state: dict, date_str: str):
    path = PENDING_PATH_TMPL.format(date=date_str)
    os.makedirs(TRADE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _past_cutoff() -> bool:
    now = datetime.now()
    return (now.hour, now.minute) >= CUTOFF_HOUR_MIN


def _already_held(trader: KISIntraday, code: str) -> bool:
    """메모리(self.positions)가 아니라 실제 계좌 잔고를 직접 조회해 판단."""
    bal = trader.get_balance()
    for h in bal.get("holdings", []):
        if str(h.get("pdno", "")) == str(code) and int(h.get("hldg_qty", 0) or 0) > 0:
            return True
    return False


def run(args):
    date_str = _today_str()
    print(f"\n  [시그널체크] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 실행")

    if _past_cutoff():
        print(f"  ⏰ 컷오프 시각({CUTOFF_HOUR_MIN[0]:02d}:{CUTOFF_HOUR_MIN[1]:02d}) 이후 — 신규 매수 시도 안 함")
        return

    json_path = _find_latest_signal_json(args.output_dir)
    if not json_path:
        print(f"  ⚠ {args.output_dir} 안에 매매신호_KR_*.json 파일이 없습니다. (전날 스크리닝 먼저 필요)")
        return

    df_top = _load_signal_json_as_df(json_path)
    if df_top.empty:
        print("  ⚠ 신호 파일에 유효 후보 없음")
        return

    sig_series = df_top["매매시그널"].astype(str)
    buy_candidates = df_top[sig_series.str.contains("매수", na=False)]
    if buy_candidates.empty:
        print("  ⚠ 매수 시그널 종목 없음")
        return

    state = _load_pending(date_str)
    cands_state = state["candidates"]

    trader = KISIntraday()
    if not trader._is_configured():
        print("  ⚠ KIS 앱키 미설정 → 중단")
        return

    bal = trader.get_balance()
    total_cap = bal.get("순자산", 10_000_000)
    base_amt = trader.cfg.get("base_invest_amount", 10_000_000)
    buy_top_n = trader.cfg.get("buy_top_n", 20)

    pending_codes = [c for c in buy_candidates.index if str(c) not in cands_state or
                     cands_state.get(str(c), {}).get("status") == "pending"]

    print(f"  [시그널체크] 매수 후보 {len(buy_candidates)}종목 중 미해결 {len(pending_codes)}종목 체크")

    bought_today = [c for c, v in cands_state.items() if v.get("status") == "bought"]
    remaining_budget_targets = max(1, buy_top_n - len(bought_today))
    per_stock_budget = (trader._reinvest_pool or base_amt) / remaining_budget_targets

    results = []
    for code in pending_codes:
        code = str(code)
        rec = cands_state.get(code, {"status": "pending", "tries": 0, "first_seen": date_str})

        if rec.get("tries", 0) >= MAX_TRIES_PER_STOCK:
            rec["status"] = "expired"
            cands_state[code] = rec
            continue

        if _already_held(trader, code):
            rec["status"] = "bought"
            cands_state[code] = rec
            print(f"  ℹ {code} 이미 보유 중 → 스킵")
            continue

        df_min = trader.get_minute_chart(code, lookback_calls=2)
        time.sleep(0.4)   # KIS rate limit 여유
        gate = evaluate_entry_gate(df_min, direction="BUY")
        rec["tries"] = rec.get("tries", 0) + 1
        rec["last_check"] = datetime.now().strftime("%H:%M:%S")
        rec["last_detail"] = gate.get("detail", {})

        # ── 분봉 + 게이트 판정 누적 로깅 (백테스트용 데이터 축적, B안) ──
        # 매수 여부와 무관하게 "체크한 모든 시점"을 남겨야 나중에
        # "통과했다면 어떻게 됐을지" / "탈락했는데 사실 올랐는지"를 다 검증할 수 있다.
        row_name = str(buy_candidates.loc[code].get("종목명", "")) if code in buy_candidates.index else ""
        approx_price = float(df_min["Close"].iloc[-1]) if not df_min.empty else 0.0
        data_logger.log_minute_bars(code, df_min, date_str)
        data_logger.log_gate_check(code, row_name, gate, approx_price, date_str)

        if gate["pass"]:
            row = buy_candidates.loc[code]
            cur = trader.get_current_price(code)
            time.sleep(0.4)
            if cur <= 0:
                rec["status"] = "pending"
                cands_state[code] = rec
                continue

            qty = max(1, int(per_stock_budget / cur))
            reason = (f"L1+L2게이트 통과 | VWAP:{gate['detail'].get('vwap'):.0f} "
                      f"CMF:{gate['detail'].get('cmf')} | {row.get('매매시그널','')}")
            r = trader.place_order(code, "BUY", qty, reason=reason)
            if r.get("success"):
                rec["status"] = "bought"
                rec["buy_price"] = r.get("price")
                rec["buy_time"] = datetime.now().strftime("%H:%M:%S")
                results.append({"code": code, "name": row.get("종목명", ""), "action": "BUY",
                                 "price": r.get("price"), "qty": qty})
                print(f"  ✅ {code} 게이트 통과 → 매수 {qty}주 @{r.get('price'):,}원")
            else:
                rec["status"] = "pending"
                print(f"  ⚠ {code} 게이트 통과했지만 주문 실패: {r.get('msg','')}")
        else:
            failed = [k for k, v in gate["checks"].items() if not v]
            print(f"  ⏳ {code} 게이트 미통과 (실패: {', '.join(failed)}) — {rec['tries']}회차, 재시도 대기")

        cands_state[code] = rec

    state["candidates"] = cands_state
    _save_pending(state, date_str)

    bought_n = sum(1 for v in cands_state.values() if v.get("status") == "bought")
    pending_n = sum(1 for v in cands_state.values() if v.get("status") == "pending")
    expired_n = sum(1 for v in cands_state.values() if v.get("status") == "expired")
    print(f"\n  [시그널체크] 완료 — 매수:{bought_n} 대기:{pending_n} 만료:{expired_n} "
          f"(이번 실행 신규매수: {len(results)}건)")

    data_status = data_logger.status_summary()
    print(f"  [데이터누적] 지금까지 {data_status['days']}거래일치 분봉/게이트 로그 축적됨 "
          f"(종목×일 파일 {data_status.get('total_stock_day_files', 0)}개)")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="장중 L1+L2 시그널 게이트 체크 → KIS 매수")
    parser.add_argument("--output-dir", type=str, default=BASE_DIR)
    args = parser.parse_args()
    run(args)
