"""
==============================================================
  analyze_gate_log.py — 누적된 게이트 판정 로그 검증

  data_logger.py가 쌓아온 data/intraday/gate_log_*.jsonl +
  data/intraday/bars/{date}/{code}.csv를 합쳐서,
  "게이트가 통과했던 시점 이후 N분 동안 실제로 어떻게 움직였는지"를
  통과(pass) / 실패(fail) 그룹으로 나눠 비교한다.

  몇 주~몇 달치 데이터가 쌓이면 이 결과로:
    - 게이트가 실제로 유효한 필터인지 (통과 그룹 수익률이 더 좋은지)
    - cmf_threshold 등 파라미터를 어떻게 조정해야 할지
  를 판단할 수 있다. 데이터가 적을 때는 참고용 트렌드만 확인.

  사용법:
    python analyze_gate_log.py --forward-min 30
==============================================================
"""

import argparse
import glob
import json
import os

import pandas as pd

DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "intraday")
BARS_DIR  = os.path.join(DATA_ROOT, "bars")


def _load_all_gate_logs() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(DATA_ROOT, "gate_log_*.jsonl")))
    rows = []
    for fp in files:
        date_str = os.path.basename(fp).replace("gate_log_", "").replace(".jsonl", "")
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    rec["date"] = date_str
                    rows.append(rec)
                except json.JSONDecodeError:
                    continue
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _load_bars(date_str: str, code: str) -> pd.DataFrame:
    fp = os.path.join(BARS_DIR, date_str, f"{code}.csv")
    if not os.path.exists(fp):
        return pd.DataFrame()
    df = pd.read_csv(fp, dtype={"time": str}).set_index("time").sort_index()
    return df


def _forward_return(bars: pd.DataFrame, check_time_hhmmss: str, forward_min: int) -> float:
    """체크 시각 기준, forward_min분 뒤(또는 그날 마지막 캔들까지)의 수익률(%)."""
    if bars.empty:
        return None
    times = bars.index.tolist()
    # 체크 시각 이후 가장 가까운 캔들을 base로
    base_candidates = [t for t in times if t >= check_time_hhmmss]
    if not base_candidates:
        return None
    base_t = base_candidates[0]
    base_price = bars.loc[base_t, "Close"]

    target_minute = int(base_t[:2]) * 60 + int(base_t[2:4]) + forward_min
    target_hh = (target_minute // 60) % 24
    target_mm = target_minute % 60
    target_str = f"{target_hh:02d}{target_mm:02d}00"

    future_candidates = [t for t in times if t >= target_str]
    if future_candidates:
        future_price = bars.loc[future_candidates[0], "Close"]
    else:
        future_price = bars.loc[times[-1], "Close"]   # 그날 마지막 캔들로 대체

    if base_price == 0:
        return None
    return (future_price - base_price) / base_price * 100


def run(forward_min: int):
    log_df = _load_all_gate_logs()
    if log_df.empty:
        print("  ⚠ 누적된 게이트 로그가 아직 없습니다. quant_signal_check.py를 며칠 더 돌려주세요.")
        return

    print(f"  누적 체크 이벤트: {len(log_df)}건 "
          f"({log_df['date'].nunique()}거래일, {log_df['code'].nunique()}종목)")

    bars_cache = {}
    returns = []
    for _, rec in log_df.iterrows():
        date_str, code = rec["date"], str(rec["code"])
        key = (date_str, code)
        if key not in bars_cache:
            bars_cache[key] = _load_bars(date_str, code)
        bars = bars_cache[key]
        check_time = rec["timestamp"][-8:].replace(":", "")  # "HH:MM:SS" → "HHMMSS"
        ret = _forward_return(bars, check_time, forward_min)
        returns.append(ret)

    log_df["forward_return_pct"] = returns
    valid = log_df.dropna(subset=["forward_return_pct"])

    if valid.empty:
        print("  ⚠ forward return을 계산할 분봉 데이터가 아직 부족합니다 (당일 데이터만 있고 시간이 더 필요).")
        return

    grouped = valid.groupby("gate_pass")["forward_return_pct"].agg(["count", "mean", "median", "std"])
    print(f"\n  === 게이트 통과(True) vs 실패(False) — {forward_min}분 후 수익률(%) ===")
    print(grouped.to_string())

    if True in grouped.index and False in grouped.index:
        diff = grouped.loc[True, "mean"] - grouped.loc[False, "mean"]
        print(f"\n  통과군 평균수익률 - 실패군 평균수익률 = {diff:+.3f}%p")
        if grouped.loc[True, "count"] < 30 or grouped.loc[False, "count"] < 30:
            print("  ⚠ 표본이 아직 적습니다 (각 그룹 30건 미만) — 추세 참고만 하고 확정 판단은 보류하세요.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-min", type=int, default=30,
                         help="게이트 체크 시점 이후 몇 분 뒤 수익률을 볼지 (기본 30분)")
    args = parser.parse_args()
    run(args.forward_min)
