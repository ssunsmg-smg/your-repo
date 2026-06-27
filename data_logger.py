"""
==============================================================
  data_logger.py — 장중 분봉 + 게이트 판정 결과 누적 로거

  목적: KIS API는 과거 분봉을 제공하지 않으므로(당일만), 우리가
  직접 매 실행마다 조회한 분봉과 그 시점의 게이트 판정 결과를
  쌓아서 "진짜 데이터"를 만든다. 몇 주~몇 달 쌓이면 이 데이터로
  '게이트 통과 시점 이후 실제로 가격이 어떻게 움직였는지'를
  역산해서 게이트 로직 자체를 검증/튜닝할 수 있다.

  저장 구조:
    data/intraday/bars/{YYYYMMDD}/{code}.csv
      → 그날 그 종목의 분봉 누적 (중복 시각은 덮어쓰지 않고 dedup)
    data/intraday/gate_log_{YYYYMMDD}.jsonl
      → 매 체크 시점의 판정 스냅샷 (1줄 = 1체크 이벤트, append-only)
==============================================================
"""

import json
import os
from datetime import datetime

import pandas as pd

DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "intraday")
BARS_DIR  = os.path.join(DATA_ROOT, "bars")
GATE_LOG_TMPL = os.path.join(DATA_ROOT, "gate_log_{date}.jsonl")


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


# ══════════════════════════════════════════════════════════
# ① 분봉 누적 저장 (중복 시각 dedup)
# ══════════════════════════════════════════════════════════
def log_minute_bars(code: str, df_min: pd.DataFrame, date_str: str = None) -> None:
    """
    df_min: index=시각(HHMMSS 문자열), 컬럼 Open/High/Low/Close/Volume
    같은 날 여러 번 호출돼도 겹치는 시각은 중복 없이 누적된다.
    """
    if df_min is None or df_min.empty:
        return
    date_str = date_str or _today_str()
    day_dir = os.path.join(BARS_DIR, date_str)
    os.makedirs(day_dir, exist_ok=True)
    fpath = os.path.join(day_dir, f"{code}.csv")

    new_df = df_min.copy()
    new_df.index.name = "time"

    if os.path.exists(fpath):
        try:
            old_df = pd.read_csv(fpath, dtype={"time": str}).set_index("time")
            combined = pd.concat([old_df, new_df.reset_index().astype({"time": str}).set_index("time")])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()
        except Exception as e:
            print(f"  ⚠ [데이터로거] {code} 기존 파일 병합 실패({e}) → 새 데이터로 덮어씀")
            combined = new_df
    else:
        combined = new_df

    combined.to_csv(fpath, index=True, index_label="time")


# ══════════════════════════════════════════════════════════
# ② 게이트 판정 스냅샷 로그 (jsonl, append-only)
# ══════════════════════════════════════════════════════════
def log_gate_check(code: str, name: str, gate_result: dict,
                    current_price: float, date_str: str = None) -> None:
    """
    매 체크 시점의 판정 결과를 한 줄(jsonl)로 누적.
    나중에 이 로그의 timestamp + code로 data/intraday/bars/에서
    그 이후 가격 흐름을 찾아 forward return을 계산하는 식으로 검증한다.
    """
    date_str = date_str or _today_str()
    os.makedirs(DATA_ROOT, exist_ok=True)
    path = GATE_LOG_TMPL.format(date=date_str)

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": code,
        "name": name,
        "current_price": current_price,
        "gate_pass": gate_result.get("pass"),
        "checks": gate_result.get("checks"),
        "detail": gate_result.get("detail"),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ══════════════════════════════════════════════════════════
# ③ 누적 데이터 현황 확인 (지금까지 며칠치 쌓였는지)
# ══════════════════════════════════════════════════════════
def status_summary() -> dict:
    if not os.path.isdir(BARS_DIR):
        return {"days": 0, "dates": []}
    dates = sorted(d for d in os.listdir(BARS_DIR) if os.path.isdir(os.path.join(BARS_DIR, d)))
    total_files = sum(len(os.listdir(os.path.join(BARS_DIR, d))) for d in dates)
    return {"days": len(dates), "dates": dates, "total_stock_day_files": total_files}


if __name__ == "__main__":
    s = status_summary()
    print(f"누적 일수: {s['days']}일")
    print(f"날짜 목록: {s.get('dates', [])}")
    print(f"종목×일 파일 수: {s.get('total_stock_day_files', 0)}")
