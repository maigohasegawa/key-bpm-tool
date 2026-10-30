# キー/BPM 照合ツール

Spotify のプレイリストURLを貼るだけで、収録曲の **BPM とキー** を楽曲データベースから調べ、
**マッシュアップに使える組み合わせ** を相性の良い順に並べるツールです。音源は使いません（曲名とアーティスト名から調べます）。

紹介ページ: https://maigohasegawa.github.io/key-bpm-tool/

## できること

- **プレイリストURLだけで読み込み** — Spotify の「共有 → リンクをコピー」で取れるURLを貼るだけ
- **マッシュアップ候補** — 全曲の総当たりで、テンポの伸縮率と移調量が小さい組み合わせを表示（倍・半分テンポも対応）
- **複数データベースで照合** — データベースどうしで値がずれている曲（BPMの倍/半分、平行調の取り違えなど）に「要確認」の印
- **基準曲との比較** — 1曲を基準に、全曲のBPM差・キー差・Camelot・相性を一覧。CSV保存も可

## 使い方

1. 右上の **Code → Download ZIP** でダウンロードして解凍
2. Mac は `起動（Mac）.command`、Windows は `起動（Windows）.bat` をダブルクリック
3. ブラウザに画面が開くので、プレイリストのURLを貼って「解析する」

Python 3 が必要です（追加のインストールは不要）。詳しくは `はじめにお読みください.txt` を見てください。

Mac で初回に「開けません」と出た場合は、ファイルを右クリック →「開く」。それでも開けない場合は、
システム設定 →「プライバシーとセキュリティ」の「このまま開く」を押してください。

## データの取得元

| データベース | 役割 | 設定 |
|---|---|---|
| [GetSongBPM](https://getsongbpm.com) | BPM・キーの標準の取得元 | 無料の APIキーが必要（画面右上の「設定」から登録） |
| [ReccoBeats](https://reccobeats.com) | GetSongBPM に見つからない曲を補う／照合 | 不要 |
| [SoundStat](https://soundstat.info) | 3つ目の照合先（任意） | APIキーが必要 |

BPM・キーのデータは [GetSongBPM.com](https://getsongbpm.com) と [ReccoBeats](https://reccobeats.com) の提供によるものです。

## 開発メモ

画面は `src/app.html`、サーバーは `src/key_bpm_app.py` です。編集したら `python3 src/build.py` で、
ルートの `key_bpm_app.py`（画面を埋め込んだ配布用の1ファイル）を作り直します。

作者: 長谷川迷子
