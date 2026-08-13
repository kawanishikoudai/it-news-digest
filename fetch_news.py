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


JP_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")


def is_japanese(text):
    return bool(JP_CHAR_RE.search(text or ""))


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
        })
    return items


def fetch_feed(url, source, limit=12):
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
        })
    return items


def call_claude_json(prompt, max_tokens=6000):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 未設定")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def enrich_items(items):
    """Claude APIで(1)日本語タイトル要約 (2)影響・注目ポイントの一言解説 をまとめて生成する。
    失敗しても例外を飲み込まず、エラー内容をリストで返す(news.jsonに記録するため)。"""
    errors = []
    if not items:
        return errors
    if not os.environ.get("ANTHROPIC_API_KEY"):
        errors.append({"source": "enrich", "error": "ANTHROPIC_API_KEY 未設定"})
        return errors

    payload = [
        {"title": it["title"], "source": it["source"], "excerpt": it.get("excerpt", "")}
        for it in items
    ]
    prompt = (
        "あなたはITニュースダイジェストの編集者です。次のJSON配列にある各ニュースについて、"
        "日本語で以下の2つを生成してください。\n"
        "1. title_ja: 見出しが日本語以外（英語など）の場合は、必ず25文字前後の日本語要約を入れてください。"
        "「短いから」「固有名詞だけだから」等の理由でnullにするのは禁止です。"
        "見出しがすでに日本語の場合のみnullにしてください。\n"
        "2. impact: このニュースがなぜ注目されるか・どんな影響がありそうかを1文、40〜60文字程度で。"
        "憶測は避け、記事内容から読み取れる範囲で簡潔に。\n"
        "出力は入力と同じ順番・同じ件数のJSON配列のみを返してください。"
        '各要素は {"title_ja": string|null, "impact": string} の形式。前置きや説明文は不要です。\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )

    try:
        results = call_claude_json(prompt)
        if len(results) != len(items):
            raise ValueError(f"件数不一致: {len(results)} != {len(items)}")
        for it, r in zip(items, results):
            if r.get("title_ja"):
                it["title_ja"] = r["title_ja"]
            it["impact"] = r.get("impact", "")
    except Exception as e:
        errors.append({"source": "enrich_primary", "error": f"{type(e).__name__}: {e}"})
        return errors  # ベースがないのでリトライはせず終了

    # 英語のままなのに title_ja が埋まらなかったものだけ、追加でリトライする
    missing = [it for it in items if not is_japanese(it["title"]) and not it.get("title_ja")]
    if not missing:
        return errors

    retry_prompt = (
        "次の英語のニュース見出しを、それぞれ必ず日本語で25文字前後に要約してください。"
        "nullや空文字は禁止です。固有名詞は日本語表記があれば使い、なければそのまま残してください。"
        "出力は入力と同じ順番・同じ件数のJSON配列（文字列の配列）のみを返してください。\n\n"
        + json.dumps([it["title"] for it in missing], ensure_ascii=False)
    )
    try:
        retried = call_claude_json(retry_prompt, max_tokens=2000)
        if len(retried) != len(missing):
            raise ValueError(f"件数不一致: {len(retried)} != {len(missing)}")
        for it, jp in zip(missing, retried):
            if jp:
                it["title_ja"] = jp
    except Exception as e:
        errors.append({"source": "enrich_retry", "error": f"{type(e).__name__}: {e}"})

    return errors


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
        ("itm", lambda: fetch_feed("https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml", "itm")),
        ("pk", lambda: fetch_feed("https://www.publickey1.jp/atom.xml", "pk")),
    ]:
        result, err = safe_fetch(name, fn)
        # ソース内の掲載順(HNは投稿ランキング順、他は新着順)を「注目度」の目安スコアに変換
        n = len(result)
        for i, it in enumerate(result):
            it["trend_score"] = round((n - i) / n, 4) if n else 0
        items += result
        if err:
            errors.append(err)

    try:
        enrich_errors = enrich_items(items)
        errors.extend(enrich_errors)
    except Exception as e:
        errors.append({"source": "enrich", "error": f"{type(e).__name__}: {e}"})

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
