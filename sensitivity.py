"""
パラメータ感度の検査
------------------------------------------------------------
「1つずらして崩れるなら偶然」— crypto_sim で立てたルールをそのまま踏襲し、
レジーム切替戦略の主要パラメータを1つずつ動かして、結果が滑らかに変わるか
特定の値でだけ跳ねるかを見る。

crypto_sim の sensitivity.py との違いはボラ閾値の掃引範囲だけ。
FXは暗号資産よりボラティリティが1桁小さいため
（USD/JPY 30日年率ボラの実測: 中央値9.2%・90%点14.5%）、
crypto_simの掃引値（60%〜120%）をそのまま使うと常に「落ち着いている」
判定になり感度そのものが測れない。

実行:
  python sensitivity.py            # 人が読むための一覧表示
  python sensitivity.py --check    # 崖検出だけを行い、見つかれば終了コード1
"""

import argparse
import statistics
import sys

import backtest as bt
import data as data_mod
import metrics as metrics_mod
import regime as regime_mod
from strategies.regime_switch import RegimeSwitching

PERIODS = [
    ("2018-2022", bt.IS_PERIOD[0], bt.IS_PERIOD[1]),
    ("2023-2026", bt.OOS_PERIOD[0], bt.OOS_PERIOD[1]),
]

# 崖検出のしきい値。「隣同士の差」が「典型的な隣同士の差」の何倍を超えたら
# 疑うか（相対）と、それでも小さすぎる変化はノイズとして無視する下限（絶対、Calmar換算）。
# 統計的な検定ではなく粗いふるいで、目的は「人が出力を見直すきっかけ」を作ることだけ。
CLIFF_REL_MULT = 4.0
CLIFF_ABS_FLOOR = 0.5


def evaluate(factory, panel) -> dict:
    eq, exp, broker = bt.run(factory(), panel, bt.CAPITAL)
    return metrics_mod.summarize(eq, broker.trades, exp, bt.CAPITAL)


def sweep(title: str, setter, values: list, panels: dict, quiet: bool = False) -> dict:
    """
    values を1つずつ設定して評価する。戻り値は {期間ラベル: [Calmarのリスト]}
    （valuesと同じ順）。quiet=True なら表を印字しない（--check専用）。
    """
    if not quiet:
        print(f"\n{title}")
        print(f"  {'値':<10}" + "".join(f"{lbl+' CAGR':>14}{'最大DD':>10}{'Calmar':>9}"
                                        for lbl, _, _ in PERIODS))
    base = setter(None)                       # 現在値を控えておく
    calmars: dict[str, list[float]] = {lbl: [] for lbl, _, _ in PERIODS}
    for v in values:
        setter(v)
        row = f"  {str(v):<10}"
        for lbl, _, _ in PERIODS:
            m = evaluate(RegimeSwitching, panels[lbl])
            calmars[lbl].append(m["Calmar"])
            mark = "*" if v == base else " "
            row += (f"{m['年率(CAGR)']*100:>13.1f}%{m['最大DD']*100:>9.1f}%"
                    f"{m['Calmar']:>8.2f}{mark}")
        if not quiet:
            print(row)
    setter(base)                              # 必ず元に戻す
    return calmars


def find_cliffs(title: str, values: list, calmars: dict) -> list[str]:
    """崖（隣同士だけ突出して変化した箇所）を見つけたら説明文のリストを返す。"""
    warnings = []
    for lbl, series in calmars.items():
        diffs = [abs(series[i + 1] - series[i]) for i in range(len(series) - 1)]
        if len(diffs) < 2:
            continue
        typical = statistics.median(diffs)
        worst = max(diffs)
        if worst > CLIFF_ABS_FLOOR and worst > CLIFF_REL_MULT * max(typical, 1e-9):
            i = diffs.index(worst)
            warnings.append(
                f"{title} / {lbl}: {values[i]} → {values[i + 1]} でCalmarが"
                f"{series[i]:.2f} → {series[i + 1]:.2f}（差{worst:.2f}）と、"
                f"他の隣同士の変化（典型{typical:.2f}）に比べて突出しています。")
    return warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="崖検出だけを行い、見つかれば終了コード1")
    args = ap.parse_args()

    panels = {lbl: data_mod.load_panel(start=s, end=e) for lbl, s, e in PERIODS}
    if not args.check:
        print("=" * 96)
        print("レジーム切替戦略のパラメータ感度   * は現在の設定値")
        print("結果が値の変化に対して滑らかなら頑健、特定の値でだけ跳ねるなら偶然を拾っている")
        print("=" * 96)

    all_warnings: list[str] = []

    def set_ma(v):
        old = regime_mod.TREND_MA
        if v is not None:
            regime_mod.TREND_MA = v
        return old
    values = [100, 150, 200, 250, 300]
    calmars = sweep("■ 大局トレンドの移動平均（日）", set_ma, values, panels, quiet=args.check)
    all_warnings += find_cliffs("大局トレンドの移動平均", values, calmars)

    def set_vol(v):
        old = regime_mod.HIGH_VOL
        if v is not None:
            regime_mod.HIGH_VOL = v
        return old
    # crypto_sim（0.6〜1.2）ではなく、FXの実測分布（中央値9.2%・90%点14.5%）に
    # 合わせた範囲。0.15（初期値）を挟むように取っている
    values = [0.08, 0.11, 0.15, 0.19, 0.25]
    calmars = sweep("■ 「荒れている」と見なす年率ボラ", set_vol, values, panels, quiet=args.check)
    all_warnings += find_cliffs("荒れていると見なす年率ボラ", values, calmars)

    import strategies.regime_switch as rs

    def set_atr(v):
        old = rs.ATR_MULT
        if v is not None:
            rs.ATR_MULT = v
            # __defaults__ は右詰め（末尾の引数から）で対応する。4引数すべてを
            # 渡さないと先頭(max_weight)の既定値が消えてTypeErrorになる
            # （実際に踏んだバグ。3要素だけ渡すとmax_weightが必須引数化する）。
            RegimeSwitching.__init__.__defaults__ = (rs.MAX_WEIGHT, v, True, regime_mod.COOLDOWN_DAYS)
        return old
    values = [2.0, 2.5, 3.0, 3.5, 4.0]
    calmars = sweep("■ ATRトレーリングストップの幅（ATRの何倍）", set_atr, values, panels, quiet=args.check)
    all_warnings += find_cliffs("ATRトレーリングストップの幅", values, calmars)

    def set_mw(v):
        old = rs.MAX_WEIGHT
        if v is not None:
            rs.MAX_WEIGHT = v
            RegimeSwitching.__init__.__defaults__ = (v, rs.ATR_MULT, True, regime_mod.COOLDOWN_DAYS)
        return old
    values = [0.3, 0.4, 0.5, 0.6, 0.8]
    sweep("■ 1銘柄あたりの上限ウェイト", set_mw, values, panels, quiet=args.check)

    if args.check:
        if all_warnings:
            print("[崖検出] 以下のパラメータでCalmarが不自然に跳ねています"
                  "（過剰最適化のサインの可能性）。sensitivity.py を実行して目視確認してください:")
            for w in all_warnings:
                print(f"  - {w}")
            return 1
        print("[崖検出] 異常なし（TREND_MA・ATR_MULTとも滑らかに変化）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
