#!/usr/bin/env python3
"""1xBet の「Japan」リーグを自動で見つけ、新しい試合(マーケット)が出たら Discord に通知する。
標準ライブラリのみ。1回実行 = 1回チェック。cron / GitHub Actions から定期実行する。
  python3 watch.py            通常実行
  python3 watch.py --test     Discord にテスト投稿だけして終了

仕組み:
  1. サイトマップ(1時間に1回)から、名前に keywords を含むバスケ/サッカーのリーグを検出
  2. config.json の exclude_league_ids に入っているリーグは無視
  3. 新しいリーグが現れたら「新リーグ出現」を通知
  4. 監視中のリーグごとに試合一覧を取り、前回なかった試合IDを「新規マーケット」として通知
"""
import gzip, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "state.json")
JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
META = "_meta"


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
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def log(msg):
    print(f"[{datetime.now(JST):%F %T}] {msg}", file=sys.stderr)


def title_from_slug(slug):
    return " ".join(w.upper() if len(w) <= 2 else w.capitalize() for w in slug.split("-"))


def discover(base, cfg):
    """サイトマップから対象リーグを返す: {league_id(str): {"sport":..., "slug":...}}"""
    raw = http(f"{base}/sitemaps/sitemap_en_line.xml.gz")
    try:
        raw = gzip.decompress(raw)
    except Exception:
        pass
    sports = "|".join(cfg["sports"])
    kws = [k.lower() for k in cfg["keywords"]]
    found = {}
    for loc in re.findall(r"<loc>([^<]+)</loc>", raw.decode("utf-8", "ignore")):
        m = re.search(rf"/line/({sports})/(\d+)-([a-z0-9-]+)$", loc)
        if m and any(k in m.group(3) for k in kws):
            found[m.group(2)] = {"sport": m.group(1), "slug": m.group(3)}
    return found


def fetch_games(base, lid):
    q = urllib.parse.urlencode({"champ": lid, "lng": "en", "country": 218, "partner": 1195, "gr": 1195})
    d = json.loads(http(f"{base}/service-api/LineFeed/GetChampZip?{q}"))
    if not d.get("Success"):
        raise RuntimeError(f"API error: {d.get('Error')}")
    return (d.get("Value") or {}).get("G") or []


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def post(webhook, payload):
    http(webhook, json.dumps(payload).encode(), {"Content-Type": "application/json"})


def fmt_start(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%m/%d(%a) %H:%M")


def notify_game(webhook, base, lid, info, g):
    sport = info.get("sport", "basketball")
    url = f"{base}/en/line/{sport}/{lid}-{info.get('slug', slug(g.get('LE', '')))}/{g['I']}-{slug(g['O1'] + ' ' + g['O2'])}"
    post(webhook, {"embeds": [{
        "title": f"{g['O1']} vs {g['O2']}",
        "url": url,
        "description": f"**{g.get('LE') or title_from_slug(info.get('slug', ''))}** の新しいマーケットがオープンしました",
        "color": 0xE60012,
        "fields": [{"name": "試合開始 (JST)", "value": fmt_start(g["S"]), "inline": True}],
        "footer": {"text": f"game id {g['I']}"},
    }]})


def notify_new_league(webhook, base, lid, info, games):
    sport = info["sport"]
    lines = [f"{fmt_start(g['S'])}  {g['O1']} vs {g['O2']}" for g in sorted(games, key=lambda x: x["S"])[:10]]
    desc = "\n".join(lines) if lines else "(試合はまだありません)"
    if len(games) > 10:
        desc += f"\n…ほか {len(games) - 10} 試合"
    post(webhook, {"embeds": [{
        "title": f"🆕 新リーグ出現: {title_from_slug(info['slug'])}",
        "url": f"{base}/en/line/{sport}/{lid}-{info['slug']}",
        "description": desc,
        "color": 0x1E90FF,
        "footer": {"text": f"{sport} / league id {lid}  ※不要なら config.json の exclude_league_ids に追加"},
    }]})


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return None


def main():
    env = load_env()
    webhook = env.get("DISCORD_WEBHOOK_URL")
    base = env.get("BASE_URL", "https://1xbetjap.com")
    if "--test" in sys.argv:
        post(webhook, {"content": "✅ マーケット監視のテスト通知です"})
        print("test sent")
        return
    cfg = json.load(open(os.path.join(DIR, "config.json"), encoding="utf-8"))
    exclude = set(cfg["exclude_league_ids"])
    state = load_state()
    first_run = state is None
    state = state or {}
    meta = state.get(META)
    migrating = meta is None  # 旧形式の state、または初回
    meta = meta or {"leagues": {}, "sitemap_ts": 0}
    state[META] = meta
    now = time.time()

    # --- 1. リーグ検出 (1時間に1回) ---
    if now - meta.get("sitemap_ts", 0) >= cfg["sitemap_refresh_sec"]:
        try:
            found = discover(base, cfg)
            meta["sitemap_ts"] = now
            for lid, info in found.items():
                if lid in exclude or lid in meta["leagues"]:
                    continue
                meta["leagues"][lid] = info
                print(f"NEW LEAGUE {info['slug']} ({lid})")
                if migrating or first_run:
                    continue  # 初期登録は通知しない
                try:
                    notify_new_league(webhook, base, lid, info, fetch_games(base, lid))
                except Exception as e:
                    del meta["leagues"][lid]  # 通知失敗なら次回やり直し
                    log(f"league notify failed {lid}: {e}")
        except Exception as e:
            log(f"discover failed: {e}")

    # --- 2. 各リーグの新規試合を検知 ---
    targets = {lid: info for lid, info in meta["leagues"].items() if lid not in exclude}
    failed = 0
    for lid, info in targets.items():
        try:
            games = fetch_games(base, lid)
        except Exception as e:
            failed += 1
            log(f"fetch failed {info['slug']}: {e}")
            continue
        seeding = lid not in state  # このリーグを初めて見た = 既存試合は通知しない
        known = state.setdefault(lid, {})
        for g in games:
            gid = str(g["I"])
            if gid in known:
                continue
            known[gid] = g["S"]
            print(f"NEW GAME {info['slug']}: {g['O1']} vs {g['O2']}")
            if seeding or first_run:
                continue
            try:
                notify_game(webhook, base, lid, info, g)
            except Exception as e:
                known.pop(gid, None)  # 通知失敗なら次回再試行
                log(f"discord failed: {e}")
        for gid in [k for k, s in known.items() if s < now - 3 * 86400]:
            del known[gid]

    # 除外したリーグの古い記録は掃除
    for lid in list(state):
        if lid != META and lid in exclude:
            del state[lid]
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if first_run:
        print("初回実行: 現在の状態を登録しました(通知なし)")
    if targets and failed == len(targets):
        sys.exit(1)


if __name__ == "__main__":
    main()
