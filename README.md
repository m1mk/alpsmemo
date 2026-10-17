# alpsmemo（替え歌暗記ツール）

覚えたい文章を、AI（Claude）が「アルプス一万尺」のリズムに乗る替え歌にし、VOICEVOX で WAV にします。
設計は `../替え歌暗記ツール 設計書（アルプス一万尺×VOICEVOX）.md` を参照。

## 準備（初回のみ）

1. VOICEVOX を起動する
2. Claude Code にログインする：`claude auth login`（歌詞生成は `claude -p` 経由で、claude.ai のサブスク内で動きます）
   - APIキー（従量課金）で動かしたい場合は、`config.toml` の `[ai] backend = "api"` にして `setx ANTHROPIC_API_KEY "sk-ant-..."`
3. このフォルダで仮想環境を有効にする：`.venv\Scripts\Activate.ps1`
   （作り直す場合：`py -3.14 -m venv .venv` → `pip install -e .[dev]`）
4. `alpsmemo check` で話者IDを確認し、必要なら `config.toml` を書き換える

## 使い方（GUI）

デスクトップの「替え歌暗記ツール」をダブルクリック（または `alpsmemo gui`）。

1. テーマ名を入れ、覚えたい文章を貼って「歌詞を作る」
2. 歌詞の一覧で数字・用語・読みを確認（ダブルクリックで直す、★は要確認）
3. 「音声を作る」
4. 「▶ 再生」で、今歌っている行と音を表示しながら再生
5. 「この音声を保存…」で、好きな場所に .wav で保存

歌詞の一部を消したいときは、読みから消して「足りない分を休符で埋める」を押します（＿の音は歌わずに休む）。
ブロックごと消すときは「このブロックを削除」。歌詞も音声も `out\テーマ名\` に自動で保存されます。

## 使い方（コマンド）

```
alpsmemo lyrics 入力.txt -o out/テーマ名     # 歌詞を生成 → lyrics.json / lyrics.txt
alpsmemo synth out/テーマ名                  # 音声化（既定は歌唱。--mode read で読み上げ）
alpsmemo run 入力.txt -o out/テーマ名         # 上の2つを続けて実行
alpsmemo check                               # VOICEVOXの起動確認と話者一覧
```

共通オプション：`--bpm`、`--repeat N`、`--click` / `--no-click`、`--transpose 半音`、`--melody ファイル`。
入力ファイルの代わりに `-` を指定すると、文章を貼り付けて入力できます。

### 音声化の前に必ず歌詞を確認する

`lyrics.txt` で数字・用語が原文どおりか確認し、直すときは `lyrics.json` を編集します。

- `morae` は `["イ","ツ","カ"]` のような配列のほか、`"イツカ"` のように文字列で書いても自動で分割されます
- `＿`（または `_`）は休符です。その音符では歌いません
- 自動修正できなかったブロックは `"needs_review": true` になっています。確認したら `false` にしてください
- `synth` は音数が合わないブロックをスキップして、残りを出力します

## 出力

`out/テーマ名/sing/`（読み上げは `read/`）に次のファイルを出力します。

- `block_01.wav` …：ブロックごとの音声
- `all.wav`：全ブロックの結合（ブロック間に1小節の無音）
- `all_repeatN.wav`：各ブロックをN回繰り返した版（`--repeat 2` 以上のとき）
- `timing.json`：各音の開始時刻（GUIの歌詞表示用）

## 調整

- メロディ：`melodies/alps_verse.json`（`[key, beats]`、key は MIDI 番号、休符は `null`）。耳で聴いて直してください
- AIへの指示：`prompts/lyrics_system.md`（ルール）、`lyrics_user.md`（依頼文）、`lyrics_fix.md`（作り直し依頼）
- テスト：`pytest`（VOICEVOX 起動中は実エンジンでの長さ・歌唱テストも実行）

生成音声を公開・共有する場合は、VOICEVOX と各キャラクターの利用規約・クレジット表記に従ってください。
