"""
評価指標
------------------------------------------------------------
「増えたか」より先に「どれだけ減る時期があったか」を見るための指標群。
100万円を長く握り続けられるかどうかは、最終リターンより最大DDで決まる。

crypto_sim の metrics.py との違いは年率換算の分母だけ。crypto_sim は
暗号資産が年中無休（365日/年）なので暦日数と観測本数が一致していたが、
FXは土日休場で年間の観測本数が暦日数と一致しない（実測で年261本前後、
data.py で取得した1996〜2026年のUSD/JPY等で確認済み）。
CAGRの年数換算（暦がどれだけ経過したか）と、ボラ・Sharpe等の年率換算
（1年あたり何回観測したか）は別物なので、意図的に定数を分けている。
ここを混同すると、ボラ・Sharpeが実際より約1.18倍（sqrt(365/261)）
過大に出てしまう。
"""

import numpy as np
import pandas as pd

CALENDAR_DAYS_PER_YEAR = 365.25   # CAGRの年数換算（暦がどれだけ経過したか）
TRADING_DAYS_PER_YEAR = 261       # ボラ・Sharpe等の年率換算（1年の観測本数）


def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timestamp | None]:
    """最大ドローダウン（負の小数）と、その底の日付。"""
    peak = equity.cummax()
    dd = equity / peak - 1.0
    if dd.empty:
        return 0.0, None
    return float(dd.min()), dd.idxmin()


def summarize(equity: pd.Series, trades: list, exposure: pd.Series,
              capital: float) -> dict:
    eq = equity.dropna()
    if len(eq) < 2:
        return {}

    years = (eq.index[-1] - eq.index[0]).days / CALENDAR_DAYS_PER_YEAR
    total_ret = float(eq.iloc[-1] / capital - 1.0)
    cagr = (eq.iloc[-1] / capital) ** (1 / years) - 1.0 if years > 0 else 0.0

    rets = eq.pct_change().dropna()
    vol = float(rets.std() * np.sqrt(TRADING_DAYS_PER_YEAR))
    sharpe = (float(rets.mean() / rets.std() * np.sqrt(TRADING_DAYS_PER_YEAR))
              if rets.std() > 0 else 0.0)
    downside = rets[rets < 0]
    sortino = (float(rets.mean() / downside.std() * np.sqrt(TRADING_DAYS_PER_YEAR))
               if len(downside) > 1 and downside.std() > 0 else 0.0)

    mdd, mdd_date = max_drawdown(eq)
    calmar = float(cagr / abs(mdd)) if mdd < 0 else 0.0

    sells = [t for t in trades if t.side == "売"]
    wins = [t for t in sells if t.realized_jpy > 0]
    losses = [t for t in sells if t.realized_jpy <= 0]
    gross_win = sum(t.realized_jpy for t in wins)
    gross_loss = -sum(t.realized_jpy for t in losses)
    pf = float(gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    return {
        "最終資産": float(eq.iloc[-1]),
        "総リターン": total_ret,
        "年率(CAGR)": float(cagr),
        "最大DD": mdd,
        "最大DD日": mdd_date,
        "年率ボラ": vol,
        "Sharpe": sharpe,
        "Sortino": sortino,
        "Calmar": calmar,
        "売買回数": len(trades),
        "決済回数": len(sells),
        "勝率": float(len(wins) / len(sells)) if sells else 0.0,
        "PF": pf,
        "平均建玉率": float(exposure.mean()),
        "期間(年)": float(years),
    }


def format_table(results: dict, benchmark: str | None = None) -> str:
    """{戦略名: summarize()の結果} を1枚の表にする。"""
    if not results:
        return "（結果なし）"

    cols = ["最終資産", "総リターン", "年率(CAGR)", "最大DD", "Calmar",
            "Sharpe", "勝率", "決済回数", "平均建玉率"]
    head = f"{'戦略':<26}" + "".join(f"{c:>12}" for c in cols)
    if benchmark:
        head += f"{'USDJPY比':>10}"
    lines = [head, "-" * len(head.encode("utf-8").decode("utf-8"))]
    lines[1] = "-" * (26 + 12 * len(cols) + (10 if benchmark else 0))

    bench_ret = results.get(benchmark, {}).get("総リターン") if benchmark else None

    for name, m in results.items():
        if not m:
            continue
        row = f"{name:<26}"
        row += f"{m['最終資産']:>11,.0f}円"
        row += f"{m['総リターン']*100:>11.1f}%"
        row += f"{m['年率(CAGR)']*100:>11.1f}%"
        row += f"{m['最大DD']*100:>11.1f}%"
        row += f"{m['Calmar']:>12.2f}"
        row += f"{m['Sharpe']:>12.2f}"
        row += f"{m['勝率']*100:>11.0f}%"
        row += f"{m['決済回数']:>12d}"
        row += f"{m['平均建玉率']*100:>11.0f}%"
        if bench_ret is not None:
            diff = (m["総リターン"] - bench_ret) * 100
            row += f"{diff:>+9.1f}pt"
        lines.append(row)
    return "\n".join(lines)
