"""
バックテスト・エンジン（仮想100万円 / 実際の値動き・FX主要JPYクロス）
------------------------------------------------------------
実売買・送金は一切行わない。ロジック検証用のシミュレーションであり、投資助言ではない。

crypto_sim の backtest.py と機構は同一（look-ahead bias対策も同じ）。

実行:
  python backtest.py                 # 全期間（既定 2018-01-01〜）で全戦略を比較
  python backtest.py --split         # 検証用(2018-2022) と 検証外(2023-) を分けて比較
  python backtest.py --start 2021-01-01 --end 2023-12-31
  python backtest.py --force         # 価格データを取り直す

look-ahead bias 対策:
  「i日目の終値まで」を見て決めた目標ウェイトを「i+1日目の始値」で執行する。
  終値で判定して終値で約定させると未来を覗いたことになり、成績は全部嘘になる。
"""

import argparse
import csv
import os

import pandas as pd

import data as data_mod
import metrics as metrics_mod
from broker import Broker
from strategies import (
    Context, BuyHoldUSDJPY, EqualWeight, DCA,
    DonchianTrend, SMACross, MomentumRotation, RSIMeanReversion,
    RegimeSwitching,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, "results")

CAPITAL = 1_000_000          # 元手（円）
DEFAULT_START = "2018-01-01"
IS_PERIOD = ("2018-01-01", "2022-12-31")   # 作り込んでよい期間（in-sample）
OOS_PERIOD = ("2023-01-01", None)          # 最後に一度だけ見る期間（out-of-sample）
# 参考: USD/JPYは1996年〜、EUR/GBP/AUD/JPYは2003年〜のデータがある
# （crypto_simより長期）。期間を拡張する場合はIS/OOSの境界を引き直すこと。


def run(strategy, panel: dict, capital: float = CAPITAL,
        kill_dd: float | None = None, cooldown: int = 30):
    """
    1戦略を回して (資産曲線, 建玉率, Broker) を返す。
    kill_dd: 資産のピークからこの割合下げたら全撤退して cooldown 日休む（例 0.25）。
    """
    dates = panel["close"].index
    broker = Broker(cash=capital)
    pending = None
    equity_curve, exposure_curve = [], []
    peak = capital
    halt_until = -1

    for i, date in enumerate(dates):
        # 1) 前日に決めた注文を「今日の始値」で執行
        if pending is not None:
            opens = panel["open"].iloc[i].to_dict()
            closes_now = panel["close"].iloc[i].to_dict()
            # 始値が欠損している銘柄は当日終値で代用（データ欠けの保険）
            prices = {s: (opens[s] if opens[s] == opens[s] else closes_now[s])
                      for s in opens}
            broker.rebalance(date, prices, pending)

        # 2) 今日の終値で評価
        closes = panel["close"].iloc[i].to_dict()
        eq = broker.equity(closes)
        equity_curve.append(eq)
        exposure_curve.append(broker.position_value(closes) / eq if eq > 0 else 0.0)
        peak = max(peak, eq)

        # 3) 今日の終値までを見て、明日の目標ウェイトを決める
        if kill_dd is not None and i < halt_until:
            pending = {}                                   # 休止中は現金100%
            continue
        if kill_dd is not None and eq / peak - 1.0 <= -kill_dd:
            pending = {}
            halt_until = i + cooldown
            peak = eq                                      # 再開後の基準を引き直す
            continue

        ctx = Context(panel, i, broker, eq)
        pending = strategy.targets(ctx)

    idx = pd.DatetimeIndex(dates)
    return (pd.Series(equity_curve, index=idx),
            pd.Series(exposure_curve, index=idx),
            broker)


def strategy_factories(capital: float) -> dict:
    """毎回まっさらな戦略インスタンスを作るためのファクトリ一覧。"""
    return {
        # --- ベンチマーク（これに勝てないなら意味がない） ---
        "USD/JPY買い持ち": lambda: BuyHoldUSDJPY(),
        "均等分散(月次)": lambda: EqualWeight(),
        "分割投入(12ヶ月)": lambda: DCA(capital),
        # --- 検証対象 ---
        "ドンチャン20/10": lambda: DonchianTrend(entry=20, exit=10),
        "ドンチャン55/20": lambda: DonchianTrend(entry=55, exit=20),
        "移動平均20/60": lambda: SMACross(20, 60),
        "移動平均5/25": lambda: SMACross(5, 25),
        "モメンタム上位2": lambda: MomentumRotation(90, 2),
        "RSI逆張り": lambda: RSIMeanReversion(),
        # --- レジーム切替（実運用候補） ---
        "レジーム切替": lambda: RegimeSwitching(),
    }


def save_trades(name: str, broker: Broker, tag: str) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    safe = name.replace("/", "-")
    path = os.path.join(RESULTS_DIR, f"trades_{tag}_{safe}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日付", "銘柄", "売買", "数量", "約定単価", "金額(円)", "実現損益(円)", "理由"])
        for t in broker.trades:
            w.writerow([t.date.date(), t.symbol, t.side, f"{t.qty:.4f}",
                        f"{t.price:,.3f}", f"{t.amount_jpy:,.0f}",
                        f"{t.realized_jpy:,.0f}" if t.side == "売" else "", t.reason])


def run_period(label: str, start: str | None, end: str | None,
               force: bool = False, kill_dd: float | None = None,
               quiet: bool = False) -> tuple[dict, dict]:
    """戦略を一通り回して (指標一覧, 資産曲線一覧) を返す。"""
    panel = data_mod.load_panel(start=start, end=end, force=force)
    dates = panel["close"].index
    if not quiet:
        print(f"\n{'='*100}")
        print(f"■ {label}   {dates[0].date()} 〜 {dates[-1].date()}  （{len(dates)}日）"
              f"   元手 {CAPITAL:,}円")
        if kill_dd:
            print(f"  キルスイッチ: ピークから -{kill_dd*100:.0f}% で全撤退し30日休止")
        print("=" * 100)

    results, curves = {}, {}
    for name, factory in strategy_factories(CAPITAL).items():
        strat = factory()
        # ベンチマークにキルスイッチは掛けない（素の比較対象として残す）
        kd = None if name in ("USD/JPY買い持ち", "均等分散(月次)", "分割投入(12ヶ月)") else kill_dd
        eq, exp, broker = run(strat, panel, CAPITAL, kill_dd=kd)
        results[name] = metrics_mod.summarize(eq, broker.trades, exp, CAPITAL)
        curves[name] = eq
        if not quiet:
            save_trades(name, broker, label)

    if not quiet:
        print(metrics_mod.format_table(results, benchmark="USD/JPY買い持ち"))
        os.makedirs(RESULTS_DIR, exist_ok=True)
        curve_path = os.path.join(RESULTS_DIR, f"equity_{label}.csv")
        pd.DataFrame(curves).round(2).to_csv(curve_path, encoding="utf-8-sig")
        print(f"\n資産曲線: {curve_path}")
    return results, curves


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=None)
    ap.add_argument("--split", action="store_true",
                    help="検証用(2018-2022)と検証外(2023-)を分けて表示")
    ap.add_argument("--force", action="store_true", help="価格データを取り直す")
    ap.add_argument("--kill-dd", type=float, default=None,
                    help="キルスイッチ発動DD（例 0.25）。既定は無効")
    args = ap.parse_args()

    print("コスト前提: スプレッド相当0.01%（要検証・概算） / 現物のみ（レバレッジなし）")
    print("※ これは架空の資金によるシミュレーションです。実際の売買は行いません。")

    if args.split:
        run_period("in-sample_2018-2022", IS_PERIOD[0], IS_PERIOD[1], args.force, args.kill_dd)
        run_period("out-of-sample_2023-", OOS_PERIOD[0], OOS_PERIOD[1], False, args.kill_dd)
    else:
        run_period("全期間", args.start, args.end, args.force, args.kill_dd)


if __name__ == "__main__":
    main()
