"""
==============================================================
  kis_intraday.py — KIS API 분봉시세 / 투자자(외인·기관) 데이터
  기존 quant_screener_v41f.py 의 KISAutoTrader 를 그대로 상속해서
  인증(get_access_token)·주문(place_order) 코드는 손대지 않고 재사용.

  ⚠ 사용 전 확인:
    - quant_screener_v41f.py 와 같은 폴더에 둘 것 (import 때문에)
    - kis_config.json 의 app_key/app_secret 은 실거래용이므로,
      이 모듈은 "조회"만 하지만 같은 토큰을 쓰는 만큼 신중하게 다룰 것
==============================================================
"""

import time
from datetime import datetime, timedelta

import pandas as pd

from quant_screener_v41f import KISAutoTrader   # 기존 인증·주문 클래스 재사용


class KISIntraday(KISAutoTrader):
    """
    KISAutoTrader 상속 → get_access_token(), _headers(), base_url 등
    인증 로직을 그대로 쓰면서, 분봉/투자자 조회 메서드만 추가.
    """

    # ── 주식당일분봉조회 (FHKST03010200) ──
    # 1회 호출당 최대 약 30건(분) 제공. 더 긴 구간이 필요하면 FID_INPUT_HOUR_1을
    # 이전 시각으로 옮겨 재호출(페이지네이션)해야 하지만, L1/L2 게이트는
    # 최근 30~60분 정도면 충분하므로 기본은 1~2회 호출로 처리.
    def get_minute_chart(self, stock_code: str, lookback_calls: int = 2) -> pd.DataFrame:
        """
        최근 분봉 OHLCV를 시간 오름차순 DataFrame으로 반환.
        컬럼: Open, High, Low, Close, Volume  (index: 분봉 시각)
        """
        import requests

        all_rows = []
        inqr_hour = datetime.now().strftime("%H%M%S")

        for _ in range(max(1, lookback_calls)):
            try:
                resp = requests.get(
                    f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                    headers=self._headers("FHKST03010200"),
                    params={
                        "FID_ETC_CLS_CODE": "",
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": stock_code,
                        "FID_INPUT_HOUR_1": inqr_hour,
                        "FID_PW_DATA_INCU_YN": "N",
                    },
                    timeout=10,
                )
                data = resp.json()
                if data.get("rt_cd") != "0":
                    print(f"  ⚠ [분봉조회] {stock_code} 실패: {data.get('msg1', '')}")
                    break

                rows = data.get("output2", [])
                if not rows:
                    break
                all_rows.extend(rows)

                # 다음 호출은 이번에 받은 가장 이른 시각보다 더 과거로
                oldest = rows[-1].get("stck_cntg_hour", inqr_hour)
                inqr_hour = oldest
                time.sleep(0.3)
            except Exception as e:
                print(f"  ⚠ [분봉조회] {stock_code} 오류: {e}")
                break

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        # KIS 응답 필드명: stck_cntg_hour(체결시각), stck_oprc/hgpr/lwpr/prpr(시/고/저/현재가), cntg_vol(체결량)
        df = df.rename(columns={
            "stck_cntg_hour": "time", "stck_oprc": "Open", "stck_hgpr": "High",
            "stck_lwpr": "Low", "stck_prpr": "Close", "cntg_vol": "Volume",
        })
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["Close"]).drop_duplicates(subset=["time"])
        df = df.sort_values("time").reset_index(drop=True)
        df = df.set_index("time")
        return df[["Open", "High", "Low", "Close", "Volume"]]

    # ── 주식현재가 투자자 (FHKST01010900) ──
    # ⚠ 핵심 제약: 당일 데이터는 장 종료 후에만 채워진다 (KIS 공식 안내).
    #   따라서 장중에는 항상 "전일 기준" 값만 얻을 수 있다.
    #   → 스크리닝(전날 저녁) 단계에서 호출해 L3 필터로 쓰는 용도이지,
    #     장중 시그널 게이트에는 쓰지 않는다.
    def get_investor_flow(self, stock_code: str) -> dict:
        """
        최근 영업일 기준 외국인/기관 순매수(수량) 반환.
        리턴: {"date": "YYYYMMDD", "foreign_net": int, "institution_net": int, "individual_net": int}
        실패 시 모든 값 0.
        """
        import requests

        try:
            resp = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-investor",
                headers=self._headers("FHKST01010900"),
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
                timeout=10,
            )
            data = resp.json()
            if data.get("rt_cd") != "0":
                print(f"  ⚠ [투자자조회] {stock_code} 실패: {data.get('msg1', '')}")
                return {"date": None, "foreign_net": 0, "institution_net": 0, "individual_net": 0}

            rows = data.get("output", [])
            if not rows:
                return {"date": None, "foreign_net": 0, "institution_net": 0, "individual_net": 0}

            latest = rows[0]   # 최신 영업일이 첫 행
            return {
                "date": latest.get("stck_bsop_date"),
                # 필드명은 KIS 응답 기준 — frgn_ntby_qty(외국인순매수수량), orgn_ntby_qty(기관순매수수량)
                "foreign_net": int(latest.get("frgn_ntby_qty", 0) or 0),
                "institution_net": int(latest.get("orgn_ntby_qty", 0) or 0),
                "individual_net": int(latest.get("prsn_ntby_qty", 0) or 0),
            }
        except Exception as e:
            print(f"  ⚠ [투자자조회] {stock_code} 오류: {e}")
            return {"date": None, "foreign_net": 0, "institution_net": 0, "individual_net": 0}

    def get_investor_flow_accumulated(self, stock_code: str, days: int = 5) -> dict:
        """
        최근 N영업일 외국인/기관 순매수 합산 (5일 누적 — 체크리스트의 '5일 누적순매수'에 대응).
        inquire-investor는 최근 영업일 여러 건을 한 번에 반환하므로 별도 API 호출 불필요.
        """
        import requests

        try:
            resp = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-investor",
                headers=self._headers("FHKST01010900"),
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
                timeout=10,
            )
            data = resp.json()
            rows = data.get("output", [])[:days] if data.get("rt_cd") == "0" else []
            foreign_sum = sum(int(r.get("frgn_ntby_qty", 0) or 0) for r in rows)
            inst_sum    = sum(int(r.get("orgn_ntby_qty", 0) or 0) for r in rows)
            return {"days": len(rows), "foreign_net_sum": foreign_sum, "institution_net_sum": inst_sum}
        except Exception as e:
            print(f"  ⚠ [투자자누적조회] {stock_code} 오류: {e}")
            return {"days": 0, "foreign_net_sum": 0, "institution_net_sum": 0}
