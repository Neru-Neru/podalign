# podalign

**別録り(ダブルエンダー)ポッドキャストを ±1ms オフセット / ±1ppm ドリフト精度で自動同期**し、
ノイズ除去・コンプ・ミックス・マスタリングまでの定型編集をパイプライン化。各工程を
**聴いて承認しながら**配信マスター(-16 LUFS / TP -1.0 dBTP)まで到達する Web アプリ。

[English README](README.md)

> **開発状況**: 合成素材による検証は完了、実素材での検証は進行中の早期リリースです。

- 詳細設計: [docs/design.md](docs/design.md)
- 設計レビュー(実装前に行った指摘と反映): [docs/design-review.md](docs/design-review.md)

## 入力素材(6ファイル)

| ロール | 内容 |
|---|---|
| `speaker_a/b/c` | 各自のローカル録音 3本(最終ミックスの実体) |
| `reference` | 全員の声が入った通話録音(**同期の基準のみ**。ミックスには入らない) |
| `jingle` / `bgm` | イントロ 1本 / ループ用 BGM 1本 |

現状は 3人体制(speaker 3本)固定です。

## アルゴリズム概要

音の加工はすべて ffmpeg サブプロセス、Python(numpy)は解析とパラメータ決定のみ。
7つのステージが順に並び、各ステージは「上流成果物 + パラメータ → 成果物」の
**決定論的な純関数**として動く。

| Stage | 処理 |
|---|---|
| 0 Ingest | ffprobe 検証 → 48kHz/FLAC24bit へ統一(話者はモノ) → クリップ率/DC/無音率の QC |
| 1 Sync | reference を共通時間軸に、各話者の**固定オフセット+クロックドリフト**を推定・補正(下記) |
| 2 Cleanup | ハイパス80Hz → ハム自動ノッチ(50/60Hz 自動判別) → declick/declip → `afftdn` NR(≤12dB) → ディエッサー → ゲート(-12dB 減衰に留める) → **-20 LUFS へ純線形ゲイン整合** |
| 3 Dynamics | 2段コンプ(グルー 2.5:1 → ピーク 6:1)+ 控えめ EQ(4kHz +2dB / 250Hz -2dB) |
| 4 Mix | L/C/R 薄パン → `amix`(normalize=0) → BGM をシームレスループ化し**サイドチェインダッキング** → ジングルを acrossfade |
| 5 Master | **2パス `loudnorm`(linear)** で -16 LUFS / TP -1.5 → セーフティリミッター -1.0dB |
| 6 Export | WAV 24bit / MP3 192k CBR / AAC 128k + QC レポート |

### Sync(技術的中核)

リモート収録では開始ずれに加え、機材ごとの水晶クロック差(±20〜50ppm)で
1時間に最大180msのドリフトが出るため、両方を補正する。

1. **粗探索**: 各トラックの 50Hz RMS エンベロープ同士を素の相互相関 → ±20ms 精度
2. **区間別微調整**: 収録を約10区間に分け、8kHz 波形を GCC-PHAT(両端テーパー+放物線補間)
   で相関 → サブサンプル精度の「時間 vs ずれ」系列
3. **ロバストフィット**: 話者が喋っていない区間を RMS と相関信頼度で除外し、
   Theil–Sen 初期値 + 重み付き最小二乗で直線フィット → 切片=オフセット、傾き=ドリフト率
4. **補正**: オフセットはサンプル単位の `atrim`/`adelay`、ドリフトは |s|≥5ppm のとき
   `rubberband=tempo=(1+s)`(atempo は ppm 級で無効なことを実測済み)
5. **検証**: 補正後に再相関し、残差 ±1ms 以内を確認。超えたら UI に警告

### 再実行と容量管理

各ステージの入力指紋 = hash(上流ステージの指紋 + パラメータ) の再帰定義。
パラメータを変えると下流ステージが自動で stale になり、そこだけ再実行できる。
中間 FLAC は「キャッシュ」扱いで GC 可能(1エピソード目標 3.5GB)。消えていても
決定論性により上流から自動再生成される。

## 起動方法

### Docker(推奨)

```bash
docker build -t podalign .
docker run -p 8000:8000 -v $(pwd)/data:/data podalign
# → http://localhost:8000 を開く
```

導入の最大のつまずきである **librubberband 有効ビルドの ffmpeg** を同梱しています
([ライセンス](#ライセンス)参照)。

### 手動セットアップ

必要環境: **ffmpeg(librubberband 有効ビルド。Debian/Ubuntu の apt・Homebrew は有効)** /
Python 3.10+ と `uv` / Node 22(UI ビルド時のみ)

```bash
# 1) 初回のみ: 依存導入と UI ビルド
uv sync
cd web && npm install && npm run build && cd ..

# 2) 起動(UI も FastAPI が配信する)
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
# → http://localhost:8000 を開く
```

起動時に librubberband の有無をチェックし、無効な ffmpeg では明確なエラーで停止します。

フロントエンドを触る開発時はホットリロードで:

```bash
uv run uvicorn app.main:app --reload          # バックエンド :8000
cd web && npm run dev                          # UI :5173(/api は 8000 へプロキシ)
```

## ⚠️ セキュリティモデル: 認証なし・単一ユーザー前提

podalign には**認証がありません**。自分のマシン・LAN 内の自宅サーバー・
VPN(Tailscale 等)の内側で使う前提です。**インターネットへ直接公開しないでください。**
ポートに到達できる人は誰でもアップロード・ジョブ実行・プロジェクト削除ができます。

## テスト

```bash
uv run pytest -q                                      # 全13本(E2E 含む、~2分)
uv run pytest -q --ignore=tests/test_pipeline_e2e.py  # 高速テストのみ
```

- `tests/test_sync.py` — 合成トラックで**オフセット±1ms / ドリフト±1ppm** の復元と
  rubberband 往復による補正符号の固定(設計 §12.1)
- `tests/test_pipeline_e2e.py` — 合成素材6本で全ステージ実行、
  **-16±0.5 LUFS / TP≤-1.0 dBTP** の実測 assert、GC→再生成の検証

## ライセンス

**AGPL-3.0**([LICENSE](LICENSE))。個人・自番組のセルフホスト利用には実質的な
義務はなく、podalign をネットワークサービスとして第三者に提供する場合にのみ
ソース公開義務が生じます。

Docker イメージには **librubberband(GPL)入り ffmpeg** を同梱しているため、
その表記とソース入手方法を [NOTICE](NOTICE) に記載しています。
