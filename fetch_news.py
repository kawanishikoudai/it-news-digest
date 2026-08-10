"""
IT News Digest - fetch_news.py
毎日 GitHub Actions から実行され、HN / TechCrunch / ITmedia / Publickey を取得し、
英語タイトルは Claude API で日本語に要約して docs/news.json に書き出す。
"""
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import feedparser
import requests

JST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "docs")
NEWS_JSON = os.path.join(OUT_DIR, "news.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; it-news-digest/1.0; +https://github.com/kawanishikoudai/it-news-digest)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def parse_date(s):
    if not s:
        return datetime.now(JST).isoformat()
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(JST).isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST).isoformat()


def strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def fetch_hn(limit=15):
    ids = requests.get(
        "https://hacker-news.firebaseio.com/v0/topstories.json",
        headers=HEADERS, timeout=10,
    ).json()

    items = []
    for i in ids:
        if len(items) >= limit:
            break
        r = requests.get(
            f"https://hacker-news.firebaseio.com/v0/item/{i}.json",
            headers=HEADERS, timeout=10,
        ).json()
        if not r or r.get("type") != "story" or not r.get("title"):
            continue
        items.append({
            "source": "hn",
            "title": r["title"],
            "url": r.get("url") or f"https://news.ycombinator.com/item?id={r['id']}",
            "time_iso": datetime.fromtimestamp(r.get("time", 0), tz=timezone.utc).astimezone(JST).isoformat(),
            "excerpt": f"{r.get('score', 0)} points ・ {r.get('descendants', 0)} comments",
            "needs_translation": True,
        })
    return items


def fetch_feed(url, source, limit=12, needs_translation=True):
    """RSS2.0 / Atom 両対応。多少壊れたXMLでもfeedparserが寛容にパースする。"""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)

    if not parsed.entries:
        bozo_msg = str(getattr(parsed, "bozo_exception", "")) if getattr(parsed, "bozo", 0) else ""
        raise RuntimeError(
            f"0 entries (status={resp.status_code}, len={len(resp.content)}, "
            f"content-type={resp.headers.get('content-type')}, bozo={getattr(parsed, 'bozo', 0)}, "
            f"bozo_exception={bozo_msg}, head={resp.content[:120]!r})"
        )

    items = []
    for e in parsed.entries[:limit]:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()
        if not title or not link:
            continue

        time_struct = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        if time_struct:
            dt = datetime(*time_struct[:6], tzinfo=timezone.utc)
            time_iso = dt.astimezone(JST).isoformat()
        else:
            time_iso = datetime.now(JST).isoformat()

        summary = strip_html(getattr(e, "summary", "") or "")[:160]

        items.append({
            "source": source,
            "title": title,
            "url": link,
            "time_iso": time_iso,
            "excerpt": summary,
            "needs_translation": needs_translation,
        })
    return items


def translate_titles(items):
    """英語タイトルをまとめて Claude API で日本語要約に置き換える"""
    targets = [it for it in items if it.get("needs_translation") and it["title"]]
    if not targets:
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY 未設定のため翻訳をスキップします", file=sys.stderr)
        return

    titles = [it["title"] for it in targets]
    prompt = (
        "次の英語のIT/テックニュース見出しを、それぞれ日本語で25文字前後に要約してください。"
        "固有名詞（社名・製品名・人名）は日本語表記があれば使い、なければそのまま残してください。"
        "出力は入力と同じ順番・同じ件数のJSON配列のみを返してください。前置きや説明は不要です。\n\n"
        + json.dumps(titles, ensure_ascii=False)
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        translated = json.loads(text)
        if len(translated) != len(targets):
            raise ValueError(f"件数不一致: {len(translated)} != {len(targets)}")
        for it, jp in zip(targets, translated):
            it["title_ja"] = jp
    except Exception as e:
        print(f"翻訳に失敗しました（原題のまま表示します）: {e}", file=sys.stderr)


def safe_fetch(name, fn):
    try:
        result = fn()
        print(f"[OK] {name}: {len(result)} 件")
        return result, None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[ERROR] {name}: {msg}", file=sys.stderr)
        return [], {"source": name, "error": msg}


def main():
    items = []
    errors = []

    for name, fn in [
        ("hn", lambda: fetch_hn()),
        ("tc", lambda: fetch_feed("https://techcrunch.com/feed/", "tc")),
        ("itm", lambda: fetch_feed("https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml", "itm", needs_translation=False)),
        ("pk", lambda: fetch_feed("https://www.publickey1.jp/atom.xml", "pk", needs_translation=False)),
    ]:
        result, err = safe_fetch(name, fn)
        items += result
        if err:
            errors.append(err)

    try:
        translate_titles(items)
    except Exception as e:
        errors.append({"source": "translate", "error": f"{type(e).__name__}: {e}"})

    for it in items:
        it.pop("needs_translation", None)

    items.sort(key=lambda x: x["time_iso"], reverse=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(NEWS_JSON, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(JST).isoformat(),
                "items": items,
                "errors": errors,
            },
            f, ensure_ascii=False, indent=2,
        )
    print(f"{len(items)} 件書き出しました -> {NEWS_JSON} (errors: {len(errors)})")


if __name__ == "__main__":
    main()
