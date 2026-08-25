"""
戦略の共通土台
------------------------------------------------------------
戦略は「今日の終値までを見て、明日の目標ウェイトを返す」関数として書く。
未来のバーには一切アクセスできないよう、Context が i 日目までに切ったデータしか渡さない。
この決定は backtest.py が「翌日の始値」で執行する（look-ahead bias の防止）。

crypto_sim の strategies/base.py と同一（銘柄に依存しない共通部品のため）。
"""

import pandas as pd


class Context:
    """i 日目時点で戦略が見てよい情報だけをまとめたもの。"""

    def __init__(self, panel: dict, i: int, broker, equity: float):
        self._panel = panel
        self.i = i
        self.date = panel["close"].index[i]
        self.broker = broker
        self.equity = equity

    def hist(self, field: str = "close", lookback: int | None = None) -> pd.DataFrame:
        """i 日目「まで」の履歴。lookback を指定すると直近その本数。"""
        df = self._panel[field].iloc[: self.i + 1]
        return df if lookback is None else df.iloc[-lookback:]

    @property
    def symbols(self) -> list[str]:
        return list(self._panel["close"].columns)

    @property
    def prices(self) -> dict:
        row = self._panel["close"].iloc[self.i]
        return {s: float(row[s]) for s in row.index}

    @property
    def available(self) -> list[str]:
        """今日の終値が取れている＝売買できる銘柄。データ開始前は除外される。"""
        row = self._panel["close"].iloc[self.i]
        return [s for s in row.index if row[s] == row[s] and row[s] > 0]

    @property
    def weights(self) -> dict:
        return self.broker.weights(self.prices)


class Strategy:
    name = "base"
    warmup = 0          # これだけのバーが貯まるまでは何もしない

    def targets(self, ctx: Context) -> dict:
        """{銘柄: 目標ウェイト} を返す。空 dict なら全部現金。"""
        raise NotImplementedError

    def __str__(self) -> str:
        return self.name


# ---- 指標 ----

def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> float:
    """直近の ATR（平均トゥルーレンジ）。データ不足なら NaN。"""
    if len(close) < n + 1:
        return float("nan")
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()],
                   axis=1).max(axis=1)
    return float(tr.iloc[-n:].mean())


def rsi(close: pd.Series, n: int = 14) -> float:
    if len(close) < n + 1:
        return float("nan")
    diff = close.diff().iloc[-n:]
    gain = diff.clip(lower=0).mean()
    loss = (-diff.clip(upper=0)).mean()
    if loss == 0:
        return 100.0
    return float(100 - 100 / (1 + gain / loss))


def is_month_start(prev_date, date) -> bool:
    return prev_date is None or prev_date.month != date.month
