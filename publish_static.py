"""
静的ダッシュボードの生成
------------------------------------------------------------
GitHub Pages のようにサーバを置けない場所でも運用状況を見られるよう、
dashboard.py が返すのと同じ内容を JSON として書き出し、
それを読む index.html を添えて publish/ にまとめる。

実行: python publish_static.py [出力先]

ローカルの dashboard.py（動的・15秒ごと更新）とは別物で、
こちらは「生成した時点の状態」が固定される。更新はワークフローの実行時のみ。
"""

import json
import os
import shutil
import sys

import dashboard

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(BASE_DIR, "publish")

# 静的配信では /api/state や /download/... が無いので、隣に同梱した
# state.json / results/ 配下のCSVを直接読ませる
INJECT = ('<script>window.STATE_URL = "state.json";\n'
          'window.EQUITY_CSV_URL = "results/live_equity.csv";\n'
          'window.TRADES_CSV_URL = "results/live_trades.csv";</script>\n')


def build(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    state = dashboard.build_state()
    # 定期実行では常駐プロセスが居ないためロックが無く、そのままだと
    # 画面が「停止中」と出てしまう。稼働の意味が違うので明示する。
    state["mode"] = "scheduled"
    state["interval"] = int(os.environ.get("RUN_INTERVAL_SEC", 3600))
    state["running"] = True
    state["pid"] = None
    with open(os.path.join(out_dir, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

    with open(dashboard.PAGE_FILE, encoding="utf-8") as f:
        html = f.read()
    # </head> の直前に差し込む。取得先だけを差し替え、他は同じものを使う
    if "</head>" in html:
        html = html.replace("</head>", INJECT + "</head>", 1)
    else:
        html = INJECT + html
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    # 次回の実行が続きから再開できるよう、状態とログもそのまま置く
    for name in ("state", "results"):
        src = os.path.join(BASE_DIR, name)
        if os.path.isdir(src):
            dst = os.path.join(out_dir, name)
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)

    snap = state.get("snapshot") or {}
    print(f"出力: {out_dir}")
    print(f"  index.html / state.json")
    print(f"  総資産 {snap.get('equity', 0):,.0f}円 / "
          f"{snap.get('regime', '?')} → {snap.get('strategy', '?')} / "
          f"保有 {len(snap.get('positions', []))}件")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT)
