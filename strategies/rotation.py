"""
ローテーション系・逆張り系
------------------------------------------------------------
crypto_sim の strategies/rotation.py と機構は同一（銘柄に依存しない）。
"""

from .base import Strategy, Context, rsi, is_month_start


class MomentumRotation(Strategy):
    """
    月初に「直近 lookback 日の騰落率」で並べ替え、上位 top_n を均等保有。
    騰落率がマイナスの通貨ペアは買わない（＝全部下げていれば現金退避）。
    """

    def __init__(self, lookback: int = 90, top_n: int = 2):
        self.lookback, self.top_n = lookback, top_n
        self.warmup = lookback + 2
        self.name = f"モメンタム上位{top_n}(月次/{lookback}日)"
        self._prev = None
        self._held = {}

    def targets(self, ctx: Context) -> dict:
        if ctx.i < self.warmup:
            return {}
        if is_month_start(self._prev, ctx.date):
            closes = ctx.hist("close")
            scores = {}
            for sym in ctx.available:
                c = closes[sym].dropna()
                if len(c) < self.lookback + 1:
                    continue
                ret = float(c.iloc[-1]) / float(c.iloc[-self.lookback]) - 1.0
                if ret > 0:
                    scores[sym] = ret
            picks = sorted(scores, key=scores.get, reverse=True)[: self.top_n]
            self._held = {s: 1.0 / len(picks) for s in picks} if picks else {}
        self._prev = ctx.date
        return self._held


class RSIMeanReversion(Strategy):
    """
    RSI(14) の逆張り。売られすぎ(<low)で買い、戻り(>high)で手仕舞い。
    トレンドフォローと対極の挙動を見る比較用。
    """

    def __init__(self, n: int = 14, low: float = 30.0, high: float = 55.0, cap: float = 0.33):
        self.n, self.low, self.high, self.cap = n, low, high, cap
        self.warmup = n + 2
        self.name = f"RSI逆張り({n}/{low:.0f}-{high:.0f})"

    def targets(self, ctx: Context) -> dict:
        if ctx.i < self.warmup:
            return {}
        closes = ctx.hist("close")
        held = ctx.weights
        out = {}
        for sym in ctx.available:
            c = closes[sym].dropna()
            if len(c) < self.n + 1:
                continue
            r = rsi(c, self.n)
            if r != r:
                continue
            in_pos = held.get(sym, 0.0) > 0.001
            if in_pos and r < self.high:
                out[sym] = self.cap
            elif not in_pos and r < self.low:
                out[sym] = self.cap
        return out
