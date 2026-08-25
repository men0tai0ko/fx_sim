"""
トレンドフォロー系
------------------------------------------------------------
crypto_sim の strategies/trend.py と機構は同一（銘柄に依存しない）。
FXは暗号資産よりボラティリティが低く、トレンドの持続期間や振れ幅の前提が
異なる可能性があるため、entry/exit・ATR倍率などのパラメータは
sensitivity.py でFXデータに対して別途検証すること（crypto_simの値を転用しない）。
"""

from .base import Strategy, Context, atr


class DonchianTrend(Strategy):
    """
    ドンチャン・ブレイクアウト ＋ ATRトレーリングストップ。

      買い : 終値が「今日を除く直近 entry 日の高値」を上抜け
      売り : 終値が「今日を除く直近 exit 日の安値」を下抜け、または
             建玉後の最高終値から ATR×atr_mult 下げた（トレーリングストップ）
      枚数 : 1トレードの想定損失が総資産の risk% になるよう ATR で逆算
             （＝荒れている通貨ペアほど小さく持つ）

    ストップは終値で判定する。日中の値動きは追えないので、実際より不利な方向
    （＝逃げ遅れる方向）に見積もっていることになる。
    """
    name = "ドンチャン+ATRストップ"

    def __init__(self, entry: int = 20, exit: int = 10, atr_n: int = 14,
                 atr_mult: float = 3.0, risk: float = 0.02, cap: float = 0.40):
        self.entry, self.exit = entry, exit
        self.atr_n, self.atr_mult = atr_n, atr_mult
        self.risk, self.cap = risk, cap
        self.warmup = max(entry, exit, atr_n) + 2
        self.state = {}     # 銘柄 -> {"peak": 建玉後の最高終値}
        self.name = f"ドンチャン{entry}/{exit}+ATRストップ"

    def targets(self, ctx: Context) -> dict:
        if ctx.i < self.warmup:
            return {}
        highs, lows, closes = ctx.hist("high"), ctx.hist("low"), ctx.hist("close")
        held = ctx.weights
        out = {}

        for sym in ctx.available:
            c = closes[sym].dropna()
            h = highs[sym].dropna()
            l = lows[sym].dropna()
            if len(c) < self.warmup:
                continue
            price = float(c.iloc[-1])
            a = atr(h, l, c, self.atr_n)
            if a != a or a <= 0:
                continue

            in_pos = held.get(sym, 0.0) > 0.001
            if in_pos:
                st = self.state.setdefault(sym, {"peak": price})
                st["peak"] = max(st["peak"], price)
                stop = st["peak"] - self.atr_mult * a
                broke_low = price < float(l.iloc[-(self.exit + 1):-1].min())
                if price <= stop or broke_low:
                    self.state.pop(sym, None)
                    continue                      # 手仕舞い（ウェイト0）
                out[sym] = self._size(price, a)
            else:
                if price > float(h.iloc[-(self.entry + 1):-1].max()):
                    self.state[sym] = {"peak": price}
                    out[sym] = self._size(price, a)

        return out

    def _size(self, price: float, a: float) -> float:
        stop_dist = self.atr_mult * a / price      # ストップまでの距離（価格比）
        if stop_dist <= 0:
            return 0.0
        return min(self.cap, self.risk / stop_dist)


class FilteredEqualWeight(Strategy):
    """
    自分の移動平均を上回っている通貨ペアだけを均等保有する。
    強気相場で「持っているだけ」に近い挙動をしつつ、崩れたペアは外す。
    """

    def __init__(self, ma: int = 50):
        self.ma = ma
        self.warmup = ma + 2
        self.name = f"分散保有({ma}日線超え)"

    def targets(self, ctx: Context) -> dict:
        if ctx.i < self.warmup:
            return {}
        closes = ctx.hist("close")
        picks = []
        for sym in ctx.available:
            c = closes[sym].dropna()
            if len(c) < self.ma + 1:
                continue
            if float(c.iloc[-1]) > float(c.iloc[-self.ma:].mean()):
                picks.append(sym)
        return {s: 1.0 / len(picks) for s in picks} if picks else {}


class SMACross(Strategy):
    """移動平均クロスの順張り。短期>長期の通貨ペアを均等保有。"""

    def __init__(self, short: int = 20, long: int = 60):
        self.short, self.long = short, long
        self.warmup = long + 2
        self.name = f"移動平均クロス{short}/{long}"

    def targets(self, ctx: Context) -> dict:
        if ctx.i < self.warmup:
            return {}
        closes = ctx.hist("close")
        picks = []
        for sym in ctx.available:
            c = closes[sym].dropna()
            if len(c) < self.long + 1:
                continue
            if float(c.iloc[-self.short:].mean()) > float(c.iloc[-self.long:].mean()):
                picks.append(sym)
        return {s: 1.0 / len(picks) for s in picks} if picks else {}
