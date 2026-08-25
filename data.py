"""
価格データ層 — 取得とローカルキャッシュ（FX / 主要JPYクロス）
------------------------------------------------------------
yfinance から JPYクロスの日足OHLCVを取り、cache/ にCSVで保存する。
2回目以降はキャッシュを読むので、同じデータで何度でも再現性のある検証ができる。

crypto_sim の data.py をFX向けに移植したもの。取得・キャッシュの仕組みは
同じだが、FXは土日が休場のため「日足の欠け」の扱いだけ違う
（暗号資産は年中無休なので欠け＝異常だが、FXは土日の欠けが正常）。

注意: yfinance の FX 価格はプロバイダ集計の参考値で、実際の国内FX会社の
気配（スプレッド込み）とは別物。将来 broker API 等に差し替えられるよう、
外部が触るのは fetch() / load_panel() の2つだけにしてある。
"""

import os
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")

# 監視ユニバース（主要JPYクロス）。ANCHOR（USD/JPY）を大局トレンドの基準にし、
# 「上昇銘柄比率」はこの4本の合議で決める（crypto_simの4銘柄と同じ発想）。
SYMBOLS = {
    "USDJPY=X": "米ドル/円",
    "EURJPY=X": "ユーロ/円",
    "GBPJPY=X": "英ポンド/円",
    "AUDJPY=X": "豪ドル/円",
}

CACHE_MAX_AGE_HOURS = 12  # これより古いキャッシュは取り直す


def _cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol.replace('=', '_')}_1d.csv")


def _cache_is_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))
    return age < timedelta(hours=CACHE_MAX_AGE_HOURS)


def fetch(symbol: str, force: bool = False) -> pd.DataFrame:
    """
    日足OHLCVを返す。index は tz なしの日付、列は Open/High/Low/Close/Volume。
    キャッシュが新しければ通信しない。
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol)

    if not force and _cache_is_fresh(path):
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        return df

    hist = yf.Ticker(symbol).history(period="max", interval="1d", auto_adjust=False)
    if hist.empty:
        if os.path.exists(path):  # 通信に失敗しても古いキャッシュがあれば使う
            print(f"  [警告] {symbol} の取得に失敗。既存キャッシュを使用します。")
            return pd.read_csv(path, index_col=0, parse_dates=True)
        raise RuntimeError(f"{symbol} のデータを取得できませんでした")

    df = hist[["Open", "High", "Low", "Close", "Volume"]].copy()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_csv(path)
    return df


def load_panel(symbols: list[str] | None = None,
               start: str | None = None,
               end: str | None = None,
               force: bool = False) -> dict[str, pd.DataFrame]:
    """
    全銘柄を共通の日付インデックスに揃えた {"open","high","low","close"} を返す。
    各 DataFrame は index=日付 / columns=銘柄。
    上場前などデータが無い箇所は NaN のまま（＝その日は売買不可として扱う）。
    """
    symbols = symbols or list(SYMBOLS)
    frames = {s: fetch(s, force=force) for s in symbols}

    index = None
    for df in frames.values():
        index = df.index if index is None else index.union(df.index)
    if start:
        index = index[index >= pd.Timestamp(start)]
    if end:
        index = index[index <= pd.Timestamp(end)]

    panel = {}
    for field in ("Open", "High", "Low", "Close"):
        panel[field.lower()] = pd.DataFrame(
            {s: frames[s][field].reindex(index) for s in symbols}, index=index
        )
    return panel


def find_gaps(panel: dict, days: int = 30) -> list[str]:
    """
    直近 days 日のうち、平日なのに日足が欠けている日付を返す。
    FXは土日休場なので、土曜・日曜の欠けは正常として除外する
    （crypto_sim の find_gaps との違いはここだけ。暗号資産は年中無休なので
    平日・週末を区別せず全ての欠けを異常とみなしていた）。
    指標の窓は「本数」で数えているため、平日の抜けがあると集計期間が静かにずれる。
    黙って計算を続けるのが一番まずいので、呼び出し側で表に出すこと。
    """
    idx = panel["close"].index
    if len(idx) < 2:
        return []
    start = max(idx[0], idx[-1] - pd.Timedelta(days=days))
    expected = pd.date_range(start, idx[-1], freq="D")
    expected = expected[expected.weekday < 5]     # 土(5)・日(6)を除外
    return [str(d.date()) for d in expected.difference(idx)]


def main() -> None:
    """python data.py で最新データを取り直してサマリを表示する。"""
    for s in SYMBOLS:
        df = fetch(s, force=True)
        print(f"{s:<10} {len(df):>5}本  "
              f"{df.index[0].date()} 〜 {df.index[-1].date()}  "
              f"最新終値 {df['Close'].iloc[-1]:,.3f}円")
    print(f"\nキャッシュ先: {CACHE_DIR}")


if __name__ == "__main__":
    main()
