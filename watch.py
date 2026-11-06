#!/usr/bin/env python3
"""1xBet の B.LEAGUE に新しい試合(マーケット)が出現したら Discord に通知する。
標準ライブラリのみ。1回実行 = 1回チェック。cron/launchd から定期実行する。
  python3 watch.py            通常実行
  python3 watch.py --test     Discord にテスト投稿だけして終了
"""
import json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "state.json")
JST = timezone(timedelta(hours=9))
LEAGUES = {3093240: "B.LEAGUE PREMIER", 3093264: "B.LEAGUE ONE", 3094197: "B.LEAGUE NEXT"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def load_env():
    env = dict(os.environ)
    p = os.path.join(DIR, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env

def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()

def fetch_league(base, lid):
    q = urllib.parse.urlencode({"champ": lid, "lng": "en", "country": 218, "partner": 1195, "gr": 1195})
    d = json.loads(http(f"{base}/service-api/LineFeed/GetChampZip?{q}"))
    if not d.get("Success"):
        raise RuntimeError(f"API error: {d.get('Error')}")
    return (d.get("Value") or {}).get("G") or []

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def game_url(base, lid, g):
    lname = g.get("LE", "")
    return f"{base}/en/line/basketball/{lid}-{slug(lname)}/{g['I']}-{slug(g['O1'] + ' ' + g['O2'])}"

def notify(webhook, base, lid, g):
    start = datetime.fromtimestamp(g["S"], JST).strftime("%m/%d(%a) %H:%M")
    embed = {
        "title": f"{g['O1']} vs {g['O2']}",
        "url": game_url(base, lid, g),
        "description": f"**{LEAGUES[lid]}** の新しいマーケットがオープンしました",
        "color": 0xE60012,
        "fields": [{"name": "試合開始 (JST)", "value": start, "inline": True}],
        "footer": {"text": f"game id {g['I']}"},
    }
    post(webhook, {"embeds": [embed]})

def post(webhook, payload):
    http(webhook, json.dumps(payload).encode(), {"Content-Type": "application/json"})

def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return None

def main():
    env = load_env()
    webhook, base = env.get("DISCORD_WEBHOOK_URL"), env.get("BASE_URL", "https://1xbetjap.com")
    if "--test" in sys.argv:
        post(webhook, {"content": "✅ B.LEAGUE マーケット監視のテスト通知です"})
        print("test sent"); return
    state = load_state()
    first_run = state is None
    state = state or {}
    now, failed = time.time(), 0
    for lid in LEAGUES:
        key = str(lid)
        try:
            games = fetch_league(base, lid)
        except Exception as e:
            failed += 1
            print(f"[{datetime.now(JST):%F %T}] fetch failed {LEAGUES[lid]}: {e}", file=sys.stderr)
            continue
        known = state.setdefault(key, {})
        for g in games:
            gid = str(g["I"])
            if gid in known:
                continue
            known[gid] = g["S"]  # 先に記録(通知失敗時の無限再通知より取りこぼしを避けるなら後でも可)
            print(f"NEW {LEAGUES[lid]}: {g['O1']} vs {g['O2']}")
            if not first_run:
                try:
                    notify(webhook, base, lid, g)
                except Exception as e:
                    known.pop(gid, None)  # 通知失敗なら次回再試行
                    print(f"discord failed: {e}", file=sys.stderr)
        # 終了から3日たった試合は state から掃除
        for gid in [k for k, s in known.items() if s < now - 3 * 86400]:
            del known[gid]
    json.dump(state, open(STATE, "w"), ensure_ascii=False)
    if first_run:
        print("初回実行: 現在の試合を既知として登録しました(通知なし)")
    if failed == len(LEAGUES):
        sys.exit(1)

if __name__ == "__main__":
    main()
