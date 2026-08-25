"""
相場付き（レジーム）の判定 — FX / 主要JPYクロス版
------------------------------------------------------------
crypto_sim の regime.py の考え方（「リターンの優劣は相場付きでほぼ決まる」
という前提のもと、固定の戦略ではなく相場付きに応じて戦略とエクスポージャー
上限を切り替える）を移植したもの。判定の骨格（3指標だけを使う）は同じだが、
FXは暗号資産よりボラティリティが1桁小さいため、閾値は暗号資産の値を
そのまま転用せず実測データから引き直している（過剰最適化を防ぐルール
＝検証していない前提を無断で使い回さない）。

判定に使うのは3つだけ（増やすほど当てにいく＝過剰最適化になる）:
  1. USD/JPYが200日移動平均より上か下か      … 大局のトレンド
  2. 直近30日の年率ボラティリティ            … 荒れているか
  3. 上昇銘柄比率（各ペアの50日線超え）      … JPYクロス全体が伸びているか

  強気 : USD/JPYが200日線の上 / 荒れていない / 半分以上のペアが上昇  → 積極
  中立 : USD/JPYは200日線の上だが、荒れている or ペアがついてきていない → 慎重
  弱気 : USD/JPYが200日線の下                                        → 防御
"""

import math

import pandas as pd

TREND_MA = 200          # 大局トレンドの判定に使う移動平均（日）。暦の窓なので暗号資産と共通
BREADTH_MA = 50         # 各ペアの上昇判定に使う移動平均（日）
VOL_WINDOW = 30         # ボラティリティの計測窓（日）

# FXは土日休場のため、年率換算の観測本数は暗号資産(365)と異なる。
# 実測（1996〜2026年のUSD/JPY等）で年261本前後（metrics.py 参照）。
TRADING_DAYS_PER_YEAR = 261

# 年率ボラがこれを超えたら「荒れている」。
# 【初期値・要検証】1996〜2026年のUSD/JPY 30日年率ボラの実測分布は
# 中央値9.2%・75%点11.8%・90%点14.5%（コロナショック等の急変時で最大74%）。
# 90%点付近を目安に0.15としたが、sensitivity.py でFXデータに対して
# 崖の有無を確認してから確定させること（crypto_simのHIGH_VOL=0.90を
# そのまま使うと、FXでは実質「常に荒れていない」判定になり指標として機能しない）。
HIGH_VOL = 0.15

BREADTH_MIN = 0.50      # 上昇銘柄比率がこれ以上で「相場全体が伸びている」
ANCHOR = "USDJPY=X"      # 大局判定の基準銘柄

# レジーム名 -> (戦略キー, エクスポージャー上限)
PLAYBOOK = {
    "強気": ("分散保有", 1.00),
    "中立": ("ドンチャン20/10", 0.60),
    "弱気": ("ドンチャン55/20", 0.40),
}

# 1銘柄あたりの上限ウェイト。1通貨ペアへの集中を避けるための歯止め。
# 【初期値】crypto_simで採用した0.30を出発点にしているが、FX固有の値では
# ないため sensitivity.py での確認が済むまでは仮値として扱うこと。
MAX_WEIGHT = 0.30

# ストップで手仕舞ったペアを、この日数だけ買い直さない（往復売買の抑制）。
# 【初期値】crypto_sim の分析（買い直しの22.9%が翌日以内）を踏まえた10日を
# 出発点にしているが、FXのボラ・トレンド継続期間で最適値が変わりうるため仮値。
COOLDOWN_DAYS = 10

# ATRトレーリングストップのパラメータ。ATR自体がその銘柄のボラ単位で
# 表現されるためスケール非依存だが、値そのものは要検証（仮値）。
ATR_N = 14
ATR_MULT = 3.0


def classify(closes: pd.DataFrame, *, high_vol: float | None = None) -> dict:
    """
    closes: index=日付 / columns=銘柄 の終値（**今日まで**に切ってあること）。
    判定結果と、その根拠になった数値を返す。

    high_vol はシャドー判定など、別の閾値で同じ判定を再現するための差し替え口。
    省略時（None）は呼び出し時点のモジュール変数 HIGH_VOL を毎回読みに行く——
    デフォルト引数（= HIGH_VOL）にしてしまうと import 時点の値がその場で
    固定され、sensitivity.py が regime.HIGH_VOL を書き換えて再検査しても
    classify() 側には反映されないままになる（crypto_simで実際に踏んだバグ）。
    """
    if high_vol is None:
        high_vol = HIGH_VOL

    btc = closes[ANCHOR].dropna()
    price = float(btc.iloc[-1])

    ma = float(btc.iloc[-TREND_MA:].mean()) if len(btc) >= TREND_MA else float("nan")
    above = ma == ma and price > ma

    rets = btc.pct_change().dropna().iloc[-VOL_WINDOW:]
    vol = (float(rets.std() * math.sqrt(TRADING_DAYS_PER_YEAR))
           if len(rets) > 2 else float("nan"))
    calm = vol == vol and vol < high_vol

    up, total = 0, 0
    for sym in closes.columns:
        c = closes[sym].dropna()
        if len(c) < BREADTH_MA:
            continue
        total += 1
        if float(c.iloc[-1]) > float(c.iloc[-BREADTH_MA:].mean()):
            up += 1
    breadth = up / total if total else 0.0

    if not above:
        name = "弱気"
    elif calm and breadth >= BREADTH_MIN:
        name = "強気"
    else:
        name = "中立"

    strategy_key, cap = PLAYBOOK[name]
    return {
        "レジーム": name,
        "戦略": strategy_key,
        "上限": cap,
        "USDJPY価格": price,
        "200日線": ma,
        "200日線比": price / ma - 1.0 if ma == ma else float("nan"),
        "年率ボラ": vol,
        "上昇銘柄比率": breadth,
        "理由": _reason(above, calm, breadth, vol),
    }


def _reason(above: bool, calm: bool, breadth: float, vol: float) -> str:
    parts = ["USD/JPYが200日線の上" if above else "USD/JPYが200日線の下"]
    parts.append(f"年率ボラ{vol*100:.1f}%" + ("（落ち着き）" if calm else "（荒れ）")
                 if vol == vol else "ボラ不明")
    parts.append(f"上昇銘柄{breadth*100:.0f}%")
    return " / ".join(parts)
