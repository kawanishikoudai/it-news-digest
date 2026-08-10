# IT News Digest

Hacker News / TechCrunch / ITmedia / Publickey を毎朝自動収集し、
英語記事は Claude API で日本語要約して GitHub Pages に公開するアプリ。

## セットアップ（efootball-gacha-trackerと同じ流れ）

1. GitHubで新しいリポジトリを作成（例: `it-news-digest`）し、このフォルダの中身を丸ごとpush
   ```
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<あなたのユーザー名>/it-news-digest.git
   git push -u origin main
   ```

2. リポジトリの **Settings → Secrets and variables → Actions** で
   `ANTHROPIC_API_KEY` を登録（英語記事の日本語要約に使用）

3. **Settings → Pages** で
   - Source: `Deploy from a branch`
   - Branch: `main` / `docs`
   を選択して保存

4. **Actions** タブ → `Update IT News` → `Run workflow` で手動実行
   （初回はこれで `docs/news.json` が作られる。以降は毎日 JST 7:00 に自動実行）

5. しばらくすると `https://<あなたのユーザー名>.github.io/it-news-digest/` で公開される
   （スマホのホーム画面に追加すればアプリのように開けます）

## できること

- Hacker News（公式API）/ TechCrunch・ITmedia・Publickey（RSS/Atom）を毎日取得
- 英語タイトルはClaude Haikuで日本語に要約
- ♡ タップで「お気に入り」に保存（ブラウザのlocalStorageに保存、記事一覧が更新されても残る）
- ソースごとの絞り込み（`--hn` `--techcrunch` など）

## 注意

- いいねはブラウザ内保存なので、他の端末とは同期されません
- `news.json` は毎日上書きされるので、過去記事の一覧は残りません（お気に入りは別途保持されます）
