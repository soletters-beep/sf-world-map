#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_data.py
=========================================================================
政治・金融・災害・事故・病気・テクノロジーなどのニュースを取得し、
本文/タイトルから地名を推定して緯度経度を付与し、同フォルダの
data.json (index.htmlが5秒ごとにポーリングするファイル) に上書き保存する。

■ 設計方針(ジャンルを増やしやすくするため)
  - CATEGORY_CONFIG にジャンルを1つ追加するだけで拡張できる。
  - 取得元(ソース)は "fetcher" というプラグイン関数の形にしてあり、
    デフォルトは無料の Google News RSS。Genspark 等の有料APIを使いたい
    場合は fetch_from_genspark() を実装して SOURCES に差し込むだけでよい
    (Genspark側の公開API仕様は未確認のため、ここではプレースホルダの
    まま無効化してある。実際のエンドポイント/認証方式が分かり次第、
    fetch_from_genspark() の中身を実装してください)。

■ 必要なライブラリ
    pip install requests feedparser

■ 使い方
    python fetch_data.py                 # 1回だけ実行して data.json を更新
    python fetch_data.py --interval 300  # 5分おきに継続実行(Ctrl+Cで停止)
    python fetch_data.py --output ./data.json --limit-per-category 8

  index.html 側は5秒おきに data.json を読みに行くだけなので、本スクリプトは
  別プロセス(cron や --interval ループ)で数分おきに回すのが現実的です。
=========================================================================
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

try:
    import feedparser
except ImportError:
    print("feedparser が見つかりません。 pip install feedparser を実行してください。", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_data")

USER_AGENT = "CommandGlobeNewsBot/1.0 (+https://example.com/contact)"
GEOCODE_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geocode_cache.json")


# =========================================================================
# 1. ジャンル定義(★ここに追加するだけで新ジャンルを増やせる★)
# =========================================================================
def google_news_rss(query, hl="ja", gl="JP", ceid="JP:ja"):
    """Google News の検索RSSのURLを組み立てる(無料・APIキー不要)"""
    return f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={ceid}"


# 直接購読できる主要メディアのRSS(動作確認済み・APIキー不要)
NHK_TOP = "https://www3.nhk.or.jp/rss/news/cat0.xml"          # NHK 主要ニュース
BBC_WORLD = "https://feeds.bbci.co.uk/news/world/rss.xml"       # BBC 国際ニュース
BBC_BUSINESS = "https://feeds.bbci.co.uk/news/business/rss.xml"  # BBC 経済
BBC_HEALTH = "https://feeds.bbci.co.uk/news/health/rss.xml"      # BBC 健康・医療
BBC_TECH = "https://feeds.bbci.co.uk/news/technology/rss.xml"    # BBC テクノロジー
BBC_SCIENCE = "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"  # BBC 科学・環境(災害系にも活用)
ITMEDIA_BURSTS = "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml"  # ITmedia 速報

CATEGORY_CONFIG = {
    "politics": {
        "label": "POLITICS",
        "sources": [
            {"type": "rss", "url": google_news_rss("政治"), "source_name": "Google News (日本語)"},
            {"type": "rss", "url": google_news_rss("politics", "en-US", "US", "US:en"), "source_name": "Google News (English)"},
            {"type": "rss", "url": BBC_WORLD, "source_name": "BBC News"},
        ],
    },
    "finance": {
        "label": "FINANCE",
        "sources": [
            {"type": "rss", "url": google_news_rss("株価 OR 金融 OR 為替"), "source_name": "Google News (日本語)"},
            {"type": "rss", "url": google_news_rss("markets OR finance", "en-US", "US", "US:en"), "source_name": "Google News (English)"},
            {"type": "rss", "url": BBC_BUSINESS, "source_name": "BBC News"},
        ],
    },
    "disaster": {
        "label": "DISASTER",
        "sources": [
            {"type": "rss", "url": google_news_rss("地震 OR 台風 OR 豪雨 OR 災害"), "source_name": "Google News (日本語)"},
            {"type": "rss", "url": google_news_rss("earthquake OR flood OR disaster", "en-US", "US", "US:en"), "source_name": "Google News (English)"},
            {"type": "rss", "url": NHK_TOP, "source_name": "NHKニュース"},
            {"type": "rss", "url": BBC_SCIENCE, "source_name": "BBC News"},
        ],
    },
    "accident": {
        "label": "ACCIDENT",
        "sources": [
            {"type": "rss", "url": google_news_rss("事故"), "source_name": "Google News (日本語)"},
            {"type": "rss", "url": google_news_rss("accident crash", "en-US", "US", "US:en"), "source_name": "Google News (English)"},
            {"type": "rss", "url": NHK_TOP, "source_name": "NHKニュース"},
        ],
    },
    "disease": {
        "label": "DISEASE",
        "sources": [
            {"type": "rss", "url": google_news_rss("感染症 OR 病気 OR 疫病"), "source_name": "Google News (日本語)"},
            {"type": "rss", "url": google_news_rss("disease outbreak epidemic", "en-US", "US", "US:en"), "source_name": "Google News (English)"},
            {"type": "rss", "url": BBC_HEALTH, "source_name": "BBC News"},
        ],
    },
    "technology": {
        "label": "TECHNOLOGY",
        "sources": [
            {"type": "rss", "url": google_news_rss("テクノロジー OR IT OR AI"), "source_name": "Google News (日本語)"},
            {"type": "rss", "url": google_news_rss("technology OR AI", "en-US", "US", "US:en"), "source_name": "Google News (English)"},
            {"type": "rss", "url": BBC_TECH, "source_name": "BBC News"},
            {"type": "rss", "url": ITMEDIA_BURSTS, "source_name": "ITmedia"},
        ],
    },
    # --- ジャンル追加例 ------------------------------------------------
    # "sports": {
    #     "label": "SPORTS",
    #     "sources": [
    #         {"type": "rss", "url": google_news_rss("スポーツ"), "source_name": "Google News (日本語)"},
    #         {"type": "genspark", "query": "sports breaking news"},  # 実装後に有効化
    #     ],
    # },
    # --------------------------------------------------------------------
}


# =========================================================================
# 2. 取得元(fetcher)プラグイン
# =========================================================================
# Google Newsの記事タイトルは "本文タイトル - 配信元名" という形式で末尾に
# 実際のパブリッシャー名が入っているため、それを抽出して source_name として使う。
_GOOGLE_NEWS_SOURCE_SUFFIX = re.compile(r"^(.*?)\s+-\s+([^-]{2,40})$")


def fetch_from_rss(url, timeout=10, source_label=None):
    """RSSフィードを取得して正規化済みアイテムのリストを返す(配信元名つき)"""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"RSS取得失敗: {url} ({e})")
        return []

    parsed = feedparser.parse(resp.content)
    feed_title = getattr(parsed.feed, "title", None) if hasattr(parsed, "feed") else None

    items = []
    for entry in parsed.entries:
        title_raw = getattr(entry, "title", "").strip()
        summary = re.sub("<[^<]+?>", "", getattr(entry, "summary", "")).strip()  # 簡易HTMLタグ除去
        link = getattr(entry, "link", "")
        published = None
        if getattr(entry, "published_parsed", None):
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

        # Google News集約フィードの場合はタイトル末尾から実際の配信元名を分離する
        title = title_raw
        source_name = source_label or feed_title
        if source_label and "Google News" in source_label:
            m = _GOOGLE_NEWS_SOURCE_SUFFIX.match(title_raw)
            if m:
                title, source_name = m.group(1).strip(), m.group(2).strip()

        items.append({
            "title": title,
            "summary": summary[:280],
            "link": link,
            "published": published,
            "source_name": source_name or "UNKNOWN SOURCE",
        })
    return items


def fetch_from_genspark(query, api_key=None, timeout=10):
    """
    Genspark API 用のプレースホルダ。
    公開エンドポイント/認証方式が確定していないため、ここでは何もせず
    空リストを返す(=無効化)。実際のAPI仕様が分かったら実装してください。

    実装イメージ:
        api_key = api_key or os.environ.get("GENSPARK_API_KEY")
        if not api_key:
            log.warning("GENSPARK_API_KEY が未設定のためスキップします")
            return []
        resp = requests.get(
            "https://api.genspark.example/v1/news",
            params={"q": query},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {"title": a["title"], "summary": a.get("summary", ""),
             "link": a["url"], "published": parse_datetime(a.get("published_at"))}
            for a in data.get("articles", [])
        ]
    """
    log.debug(f"[genspark] 未実装のためスキップ: query={query!r}")
    return []


FETCHERS = {
    "rss": lambda src: fetch_from_rss(src["url"], source_label=src.get("source_name")),
    "genspark": lambda src: fetch_from_genspark(src.get("query", ""), src.get("api_key")),
}


# =========================================================================
# 3. 地名 → 緯度経度の解決
# =========================================================================
# 3-1. 内蔵ガゼッター(オフラインでも動く一次ソース。日本の主要都市 + 世界主要都市)
GAZETTEER = {
    # --- 日本 ---
    "札幌": (43.0642, 141.3469), "仙台": (38.2682, 140.8694), "東京": (35.6812, 139.7671),
    "横浜": (35.4437, 139.6380), "名古屋": (35.1815, 136.9066), "京都": (35.0116, 135.7681),
    "大阪": (34.6937, 135.5023), "神戸": (34.6901, 135.1955), "広島": (34.3853, 132.4553),
    "福岡": (33.5904, 130.4017), "那覇": (26.2124, 127.6809), "新潟": (37.9026, 139.0232),
    "金沢": (36.5613, 136.6562), "静岡": (34.9756, 138.3827), "岡山": (34.6551, 133.9195),
    "熊本": (32.7898, 130.7417), "鹿児島": (31.5966, 130.5571), "松山": (33.8392, 132.7657),
    "盛岡": (39.7036, 141.1527), "金沢": (36.5613, 136.6562), "長野": (36.6513, 138.1810),
    # --- 世界 ---
    "ニューヨーク": (40.7128, -74.0060), "New York": (40.7128, -74.0060),
    "ワシントン": (38.9072, -77.0369), "Washington": (38.9072, -77.0369),
    "ロサンゼルス": (34.0522, -118.2437), "Los Angeles": (34.0522, -118.2437),
    "ロンドン": (51.5074, -0.1278), "London": (51.5074, -0.1278),
    "パリ": (48.8566, 2.3522), "Paris": (48.8566, 2.3522),
    "ベルリン": (52.5200, 13.4050), "Berlin": (52.5200, 13.4050),
    "モスクワ": (55.7558, 37.6173), "Moscow": (55.7558, 37.6173),
    "北京": (39.9042, 116.4074), "Beijing": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737), "Shanghai": (31.2304, 121.4737),
    "香港": (22.3193, 114.1694), "Hong Kong": (22.3193, 114.1694),
    "ソウル": (37.5665, 126.9780), "Seoul": (37.5665, 126.9780),
    "台北": (25.0330, 121.5654), "Taipei": (25.0330, 121.5654),
    "バンコク": (13.7563, 100.5018), "Bangkok": (13.7563, 100.5018),
    "シンガポール": (1.3521, 103.8198), "Singapore": (1.3521, 103.8198),
    "ジャカルタ": (-6.2088, 106.8456), "Jakarta": (-6.2088, 106.8456),
    "マニラ": (14.5995, 120.9842), "Manila": (14.5995, 120.9842),
    "デリー": (28.7041, 77.1025), "New Delhi": (28.6139, 77.2090),
    "ムンバイ": (19.0760, 72.8777), "Mumbai": (19.0760, 72.8777),
    "シドニー": (-33.8688, 151.2093), "Sydney": (-33.8688, 151.2093),
    "トロント": (43.6532, -79.3832), "Toronto": (43.6532, -79.3832),
    "メキシコシティ": (19.4326, -99.1332), "Mexico City": (19.4326, -99.1332),
    "サンパウロ": (-23.5505, -46.6333), "Sao Paulo": (-23.5505, -46.6333),
    "リオデジャネイロ": (-22.9068, -43.1729), "Rio de Janeiro": (-22.9068, -43.1729),
    "ブエノスアイレス": (-34.6037, -58.3816), "Buenos Aires": (-34.6037, -58.3816),
    "カイロ": (30.0444, 31.2357), "Cairo": (30.0444, 31.2357),
    "ラゴス": (6.5244, 3.3792), "Lagos": (6.5244, 3.3792),
    "ナイロビ": (-1.2921, 36.8219), "Nairobi": (-1.2921, 36.8219),
    "ヨハネスブルグ": (-26.2041, 28.0473), "Johannesburg": (-26.2041, 28.0473),
    "イスタンブール": (41.0082, 28.9784), "Istanbul": (41.0082, 28.9784),
    "テヘラン": (35.6892, 51.3890), "Tehran": (35.6892, 51.3890),
    "リヤド": (24.7136, 46.6753), "Riyadh": (24.7136, 46.6753),
    "ドバイ": (25.2048, 55.2708), "Dubai": (25.2048, 55.2708),
    "テルアビブ": (32.0853, 34.7818), "Tel Aviv": (32.0853, 34.7818),
    "キーウ": (50.4501, 30.5234), "Kyiv": (50.4501, 30.5234),
    "ワルシャワ": (52.2297, 21.0122), "Warsaw": (52.2297, 21.0122),
    "ローマ": (41.9028, 12.4964), "Rome": (41.9028, 12.4964),
    "マドリード": (40.4168, -3.7038), "Madrid": (40.4168, -3.7038),
    "アムステルダム": (52.3676, 4.9041), "Amsterdam": (52.3676, 4.9041),
    "ブリュッセル": (50.8503, 4.3517), "Brussels": (50.8503, 4.3517),
    "ジュネーブ": (46.2044, 6.1432), "Geneva": (46.2044, 6.1432),
    "ストックホルム": (59.3293, 18.0686), "Stockholm": (59.3293, 18.0686),
}
# 長い地名を先にマッチさせる(部分一致誤爆を減らすため)
_GAZETTEER_KEYS_SORTED = sorted(GAZETTEER.keys(), key=len, reverse=True)
_ASCII_NAME_RE = re.compile(r"^[A-Za-z .]+$")


def _name_in_text(text, name):
    if _ASCII_NAME_RE.match(name):
        return re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE) is not None
    return name in text


def lookup_gazetteer(text):
    for name in _GAZETTEER_KEYS_SORTED:
        if _name_in_text(text, name):
            return GAZETTEER[name]
    return None


# 3-2. ガゼッターで見つからない場合のオンラインフォールバック(OSM Nominatim・無料)
def _load_geocode_cache():
    if os.path.exists(GEOCODE_CACHE_PATH):
        try:
            with open(GEOCODE_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_geocode_cache(cache):
    try:
        with open(GEOCODE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning(f"geocodeキャッシュの保存に失敗: {e}")


_geocode_cache = _load_geocode_cache()
_last_nominatim_call = 0.0


def lookup_nominatim(place_guess, enable_online=True):
    """Nominatim(OpenStreetMap)への軽量な地名検索。1秒間隔を守る。"""
    global _last_nominatim_call
    if not enable_online or not place_guess:
        return None
    if place_guess in _geocode_cache:
        cached = _geocode_cache[place_guess]
        return tuple(cached) if cached else None

    elapsed = time.time() - _last_nominatim_call
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place_guess, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=8,
        )
        _last_nominatim_call = time.time()
        resp.raise_for_status()
        results = resp.json()
        if results:
            coords = (float(results[0]["lat"]), float(results[0]["lon"]))
            _geocode_cache[place_guess] = list(coords)
            return coords
        _geocode_cache[place_guess] = None
        return None
    except (requests.RequestException, ValueError, KeyError) as e:
        log.debug(f"Nominatim検索失敗 ({place_guess}): {e}")
        return None


# 地名っぽい単語をタイトルから拾うための簡易パターン(カタカナ連続 or 英大文字始まりの単語)
_CANDIDATE_PATTERN = re.compile(r"[ァ-ヴー]{3,}|[A-Z][a-zA-Z]{3,}")


def guess_location(title, summary, enable_online=True):
    text = f"{title} {summary}"

    # 1) 内蔵ガゼッターで即判定(オフラインで確実)
    hit = lookup_gazetteer(text)
    if hit:
        return hit

    # 2) 見つからなければタイトルから地名っぽい単語を抜き出しオンライン検索
    if enable_online:
        for candidate in _CANDIDATE_PATTERN.findall(title):
            coords = lookup_nominatim(candidate, enable_online=True)
            if coords:
                return coords

    return None


def jitter(lat, lng, spread=0.06):
    """同一都市に複数ピンが重なるのを避けるための微小なランダムオフセット"""
    return (lat + random.uniform(-spread, spread), lng + random.uniform(-spread, spread))


# =========================================================================
# 4. アイテム正規化・重複除去
# =========================================================================
def make_id(link, category):
    digest = hashlib.md5(link.encode("utf-8")).hexdigest()[:10]
    return f"news-{category}-{digest}"


def build_news_items(category_key, config, limit, enable_online_geocode):
    label = config["label"]
    seen_links = set()
    raw_items = []

    for src in config["sources"]:
        fetcher = FETCHERS.get(src["type"])
        if not fetcher:
            log.warning(f"未知のソースタイプ: {src['type']}")
            continue
        for item in fetcher(src):
            if not item.get("link") or item["link"] in seen_links:
                continue
            seen_links.add(item["link"])
            raw_items.append(item)

    # 新しい順に並べて上限件数まで採用
    raw_items.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    news_items = []
    unresolved = 0
    for item in raw_items[: limit * 4]:  # 仕入れ先が増えた分、位置特定に失敗する分を見込んで多めに走査
        if len(news_items) >= limit:
            break
        coords = guess_location(item["title"], item["summary"], enable_online=enable_online_geocode)
        if not coords:
            unresolved += 1
            continue
        lat, lng = jitter(*coords)
        published = item["published"] or datetime.now(timezone.utc)
        news_items.append({
            "id": make_id(item["link"], category_key),
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "title": item["title"],
            "category": label,
            "time": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "summary": item["summary"],
            "source_name": item.get("source_name", "UNKNOWN SOURCE"),
            "source_url": item["link"],
        })

    if unresolved:
        log.info(f"[{label}] 位置特定できず除外: {unresolved}件")
    log.info(f"[{label}] {len(news_items)}件のニュースを取得")
    return news_items


# =========================================================================
# 5. data.json 書き出し(既存の vehicles は保持、news のみ更新)
# =========================================================================
def load_existing(output_path):
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("既存data.jsonの読み込みに失敗。新規作成します。")
    return {}


def write_data_json(output_path, news_items, existing):
    payload = {
        "news": news_items,
        "vehicles": existing.get("vehicles", []),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp_path = f"{output_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, output_path)  # アトミックに置き換え(読み込み中の破損を防ぐ)
    log.info(f"data.json を更新しました ({len(news_items)}件) -> {output_path}")


# =========================================================================
# 6. メイン処理
# =========================================================================
def run_once(output_path, limit_per_category, enable_online_geocode):
    existing = load_existing(output_path)
    all_news = []
    for category_key, config in CATEGORY_CONFIG.items():
        try:
            all_news.extend(build_news_items(category_key, config, limit_per_category, enable_online_geocode))
        except Exception as e:
            log.error(f"[{category_key}] 取得中にエラー: {e}")

    write_data_json(output_path, all_news, existing)
    _save_geocode_cache(_geocode_cache)


def main():
    parser = argparse.ArgumentParser(description="ニュースを取得してdata.jsonを更新する")
    parser.add_argument("--output", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json"),
                         help="出力するdata.jsonのパス")
    parser.add_argument("--limit-per-category", type=int, default=6, help="ジャンルごとの最大件数")
    parser.add_argument("--interval", type=int, default=0,
                         help="指定秒数ごとに継続実行する(0の場合は1回だけ実行)")
    parser.add_argument("--no-online-geocode", action="store_true",
                         help="Nominatimへのオンライン地名検索を無効化(内蔵ガゼッターのみ使用)")
    args = parser.parse_args()

    enable_online_geocode = not args.no_online_geocode

    if args.interval > 0:
        log.info(f"{args.interval}秒間隔で継続実行します(Ctrl+Cで停止)")
        while True:
            run_once(args.output, args.limit_per_category, enable_online_geocode)
            time.sleep(args.interval)
    else:
        run_once(args.output, args.limit_per_category, enable_online_geocode)


if __name__ == "__main__":
    main()
