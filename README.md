# health-trivia-shorts

健康雑学 YouTube 動画を毎日 1 本自動生成するパイプライン。投稿は手動。

## 動画スペック

| 項目 | 値 |
|---|---|
| 形式 | 16:9 1920x1080 |
| 尺 | 約 3 分 |
| 構成 | 5-6 トピック × 30 秒詰め込み |
| 音声 | VOICEVOX 青山龍星 (speaker_id=13) |
| BGM | `assets/bgm/Escort.mp3` シームレスループ |
| 画像 | いらすとや（HTML パース）、ヒット 0 件は上位キーワード再検索 |
| テロップ | 画面上部に焼き込み太字黒テロップ（全文表示） |
| 末尾 | 医療免責定型文を自動付与 |

## パイプライン

```
collect_topics.py   Google 検索上位記事を Claude が複数参照しトピック候補生成
        ↓
generate_script.py  Claude API で 5-6 トピック × 30 秒の台本生成
        ↓
fetch_irasutoya.py  シーンキーワードからいらすとや画像を取得
        ↓
synth_voice.py      VOICEVOX 青山龍星でナレーション合成
        ↓
make_video.py       ffmpeg で動画組み立て（テロップ焼き込み + BGM ループ）
        ↓
make_thumbnail.py   いらすとや画像 + テロップでサムネ生成
        ↓
build.py            上記を順次実行、output/ に成果物を出力
```

## ローカル実行

```powershell
# 事前: VOICEVOX エンジンをローカル起動 (http://localhost:50021)
# 事前: ANTHROPIC_API_KEY を環境変数にセット

python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python src\build.py
```

成果物: `output\YYYY-MM-DD_<title>.mp4` と同フォルダのサムネ画像

## GitHub Actions

- 毎日 22:00 JST に cron で自動生成（artifact 保存）
- 手動トリガー (`workflow_dispatch`) も可
- 投稿はせず、artifact を Boss がダウンロードして手動投稿

## 規約遵守

- いらすとや: 1 動画あたり 20 点以内（重複は 1 点換算）。商用利用扱い前提
- タイトル・説明欄に「いらすとや」表記を入れないこと（コラボ誤解禁止条項回避）
- 医療情報: 末尾免責文言を自動付与（YouTube 医療誤情報ポリシー対策）
