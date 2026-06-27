"""
==============================================================
  signal_engine.py — L1(가격) + L2(거래량) 시그널 엔진
  영웅문 고급 매매 전략 문서의 지표를 그대로 구현한 순수 계산 모듈

  ※ L3(외인·기관 수급)는 여기 없음 — KIS investor API가
    "당일 데이터는 장 종료 후 제공"이라서, 장중 실시간 게이트로
    쓸 수 없기 때문. L3는 스크리닝 단계(quant_screener)에서
    전일 마감 기준으로 이미 반영된 것으로 간주한다.

  이 파일은 입력으로 OHLCV DataFrame만 받는 순수 함수 모음이라
  yfinance든 KIS 분봉이든 어디서 가져온 데이터든 그대로 사용 가능.
  → 단위 테스트하기 쉽고, KIS API 응답 포맷 변경에도 영향 안 받음.
==============================================================
"""

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════
# ① VWAP + 표준편차 밴드  (영웅문 수식관리자 코드와 동일 로직)
# ══════════════════════════════════════════════════════════
def calc_vwap_bands(df: pd.DataFrame) -> pd.DataFrame:
    """
    df 컬럼 요구: High, Low, Close, Volume
    리턴: TP, VWAP, DEV, VWAP_UP1, VWAP_DN1, VWAP_UP2, VWAP_DN2 컬럼 추가된 df

    ※ 일중(intraday) 누적 기준. 분봉 데이터를 넣으면 그날 하루 누적으로
      VWAP이 계산된다 (영웅문 N=1 옵션과 동일).
    """
    out = df.copy()
    out["TP"] = (out["High"] + out["Low"] + out["Close"]) / 3.0

    cum_vol = out["Volume"].cumsum()
    cum_tpv = (out["TP"] * out["Volume"]).cumsum()
    out["VWAP"] = cum_tpv / cum_vol.replace(0, np.nan)

    # 표준편차 (거래량 가중)
    cum_dev2 = ((out["TP"] - out["VWAP"]) ** 2 * out["Volume"]).cumsum()
    out["DEV"] = np.sqrt(cum_dev2 / cum_vol.replace(0, np.nan))

    out["VWAP_UP1"] = out["VWAP"] + out["DEV"]
    out["VWAP_DN1"] = out["VWAP"] - out["DEV"]
    out["VWAP_UP2"] = out["VWAP"] + 2 * out["DEV"]
    out["VWAP_DN2"] = out["VWAP"] - 2 * out["DEV"]
    return out


# ══════════════════════════════════════════════════════════
# ② 매물대 / Volume Profile / POC
# ══════════════════════════════════════════════════════════
def calc_volume_profile(df: pd.DataFrame, bins: int = 24, value_area_pct: float = 0.70) -> dict:
    """
    가격 구간별 거래량을 집계해 POC(최대 거래량 가격) / VAH·VAL(70% 매물 영역) 산출.

    df 컬럼 요구: High, Low, Close, Volume
    리턴: {"poc": float, "vah": float, "val": float, "profile": pd.Series}
    """
    if df.empty or df["Volume"].sum() == 0:
        return {"poc": np.nan, "vah": np.nan, "val": np.nan, "profile": pd.Series(dtype=float)}

    lo, hi = df["Low"].min(), df["High"].max()
    if hi <= lo:
        mid = float(df["Close"].iloc[-1])
        return {"poc": mid, "vah": mid, "val": mid, "profile": pd.Series(dtype=float)}

    edges = np.linspace(lo, hi, bins + 1)
    # 각 캔들의 거래량을 (고가+저가+종가)/3 가격대 bin에 배분 (간이 방식 — 분봉 단위라 충분히 정확)
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    bin_idx = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    profile = pd.Series(0.0, index=range(bins))
    for idx, vol in zip(bin_idx, df["Volume"]):
        profile[idx] += vol

    centers = (edges[:-1] + edges[1:]) / 2
    poc_bin = int(profile.idxmax())
    poc = float(centers[poc_bin])

    # Value Area: POC에서 시작해 위/아래로 거래량 비중 70% 채울 때까지 확장
    total_vol = profile.sum()
    target = total_vol * value_area_pct
    included = {poc_bin}
    acc = profile[poc_bin]
    lo_i, hi_i = poc_bin, poc_bin
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        next_lo = profile[lo_i - 1] if lo_i > 0 else -1
        next_hi = profile[hi_i + 1] if hi_i < bins - 1 else -1
        if next_hi >= next_lo:
            hi_i += 1
            acc += profile[hi_i]
            included.add(hi_i)
        else:
            lo_i -= 1
            acc += profile[lo_i]
            included.add(lo_i)

    val = float(centers[min(included)])
    vah = float(centers[max(included)])

    profile.index = centers
    return {"poc": poc, "vah": vah, "val": val, "profile": profile}


# ══════════════════════════════════════════════════════════
# ③ OBV (On-Balance Volume)
# ══════════════════════════════════════════════════════════
def calc_obv(df: pd.DataFrame) -> pd.Series:
    """df 컬럼 요구: Close, Volume"""
    direction = np.sign(df["Close"].diff()).fillna(0)
    obv = (direction * df["Volume"]).cumsum()
    obv.name = "OBV"
    return obv


# ══════════════════════════════════════════════════════════
# ④ CMF (Chaikin Money Flow)
# ══════════════════════════════════════════════════════════
def calc_cmf(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """df 컬럼 요구: High, Low, Close, Volume"""
    hl_range = (df["High"] - df["Low"]).replace(0, np.nan)
    mfm = ((df["Close"] - df["Low"]) - (df["High"] - df["Close"])) / hl_range
    mfv = mfm * df["Volume"]
    cmf = mfv.rolling(period, min_periods=max(1, period // 2)).sum() / \
          df["Volume"].rolling(period, min_periods=max(1, period // 2)).sum()
    cmf.name = "CMF"
    return cmf.fillna(0)


# ══════════════════════════════════════════════════════════
# ⑤ L1+L2 진입 게이트 (이중확인 — L3는 스크리닝 단계에서 이미 통과)
# ══════════════════════════════════════════════════════════
def evaluate_entry_gate(df: pd.DataFrame, direction: str = "BUY",
                         cmf_threshold: float = 0.05,
                         vp_bins: int = 24) -> dict:
    """
    분봉(또는 일봉) OHLCV df를 받아 L1(가격)+L2(거래량) 동시 확인 결과를 반환.

    df 컬럼 요구: Open, High, Low, Close, Volume  (시간 오름차순 정렬)
    direction: "BUY" | "SELL"

    리턴 예:
    {
      "pass": True,
      "direction": "BUY",
      "checks": {
        "vwap_position": True,   # 가격이 VWAP 위(매수) / 아래(매도)
        "obv_trend": True,       # OBV가 같은 방향으로 움직임
        "cmf_strength": True,    # CMF가 방향과 일치 + 임계값 이상
      },
      "detail": {...}            # 디버깅/로그용 실제 수치
    }
    """
    if df is None or len(df) < 10:
        return {"pass": False, "direction": direction,
                "checks": {}, "detail": {"error": "데이터 부족 (10개 미만 캔들)"}}

    vw = calc_vwap_bands(df)
    obv = calc_obv(df)
    cmf = calc_cmf(df)
    vp = calc_volume_profile(df, bins=vp_bins)

    last_close   = float(df["Close"].iloc[-1])
    last_vwap    = float(vw["VWAP"].iloc[-1])
    last_obv     = float(obv.iloc[-1])
    prev_obv     = float(obv.iloc[-min(5, len(obv))])   # 최근 5캔들 전 대비
    last_cmf     = float(cmf.iloc[-1])

    if direction == "BUY":
        vwap_ok = last_close >= last_vwap            # VWAP 위 (또는 ±σ 밴드 하단 터치 후 반등 — 운용 시 조건 조정 가능)
        obv_ok  = last_obv >= prev_obv                # OBV 상승 중
        cmf_ok  = last_cmf >= cmf_threshold           # 매수 압력 강도 충족
    else:  # SELL
        vwap_ok = last_close <= last_vwap
        obv_ok  = last_obv <= prev_obv
        cmf_ok  = last_cmf <= -cmf_threshold

    checks = {"vwap_position": bool(vwap_ok), "obv_trend": bool(obv_ok), "cmf_strength": bool(cmf_ok)}
    passed = all(checks.values())

    return {
        "pass": passed,
        "direction": direction,
        "checks": checks,
        "detail": {
            "close": last_close, "vwap": last_vwap,
            "vwap_up1": float(vw["VWAP_UP1"].iloc[-1]), "vwap_dn1": float(vw["VWAP_DN1"].iloc[-1]),
            "obv": last_obv, "obv_prev5": prev_obv,
            "cmf": round(last_cmf, 4),
            "poc": vp["poc"], "vah": vp["vah"], "val": vp["val"],
        }
    }


if __name__ == "__main__":
    # 간단 자체 테스트 (합성 데이터)
    rng = pd.date_range("2026-06-27 09:00", periods=60, freq="1min")
    np.random.seed(0)
    price = 70000 + np.cumsum(np.random.randn(60) * 50)
    test_df = pd.DataFrame({
        "Open": price, "High": price + np.random.rand(60) * 30,
        "Low": price - np.random.rand(60) * 30, "Close": price + np.random.randn(60) * 10,
        "Volume": np.random.randint(1000, 5000, 60),
    }, index=rng)

    result = evaluate_entry_gate(test_df, direction="BUY")
    print("=== 자체 테스트 (합성 데이터) ===")
    print(f"통과 여부: {result['pass']}")
    print(f"세부 체크: {result['checks']}")
    print(f"상세: {result['detail']}")
