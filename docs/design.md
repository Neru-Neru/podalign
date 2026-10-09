# podalign 設計書

対象: 別録り(ダブルエンダー)3人体制 Podcast の編集自動化 Web アプリ / 最終更新: 2026-08-25

本書は podalign の設計を、採用判断の根拠(実測値を含む)とともに記述する。実装前に行った
設計レビューで確定した判断の経緯は [design-review.md](design-review.md) に記録している。

---

## 1. 背景と目的

複数人 Podcast で毎回発生する定型作業(音合わせ、ノイズ除去、コンプ、ノーマライズ、
リミッター、BGM ループ)は、素材構成が毎回同じであるにもかかわらず手作業では
1本あたりの編集コストが大きい。これをパイプライン化して自動処理し、各工程を
聴いて確認しながら配信可能なマスターまで到達できる Web アプリを構築する。

### 1.1 入力素材

| ロール | 内容 | 用途 |
|---|---|---|
| `speaker_a/b/c` | 各自のローカル録音 3本 | 最終ミックスの実体 |
| `reference` | 全員の声が入った通話録音 1本 | **同期とTrim確認に使用。ミックスには含めない** |
| `jingle` | イントロ 1本 | 冒頭に配置 |
| `bgm` | 1本 | ループ + ダッキング |

### 1.2 前提条件

- **リモート収録・各自ローカル録音**。マイク間クロストークは無いが、**機材ごとのクロック差によるドリフト補正が必須**
- ステージごとに人間が聴いて承認してから次へ進む
- デプロイ前提の Web アプリ。認証・マルチユーザーはスコープ外
- 出力目標: **-16 LUFS ステレオ / True Peak -1.0 dBTP**

---

## 2. 要件定義

### 2.1 機能要件

| ID | 要件 |
|---|---|
| F-1 | 6ファイルをロール指定でアップロードできる(大容量・レジューム対応) |
| F-2 | リファレンスを基準に3トラックを時間軸で同期する(オフセット+ドリフト) |
| F-2a | Sync後に4トラック(話者A/B/C・reference)を同じ範囲で手動Trimし、承認後にCleanupへ進む |
| F-3 | トラックごとにノイズ除去・整音を行う |
| F-4 | コンプレッサー・EQ でダイナミクスを整える |
| F-5 | ジングル配置、BGM ループ、サイドチェインダッキングを含むミックスを行う |
| F-6 | ラウドネスノーマライズとリミッターでマスタリングする |
| F-7 | WAV / MP3 / AAC で書き出し、QC レポートを出す |
| F-8 | 各ステージの結果を波形・数値・A/B 再生で確認し、承認して次へ進める |
| F-9 | パラメータを変更して任意のステージ以降だけ再実行できる |
| F-10 | プロジェクト容量を確認し、中間成果物を破棄できる |

### 2.2 非機能要件

| ID | 要件 | 根拠 |
|---|---|---|
| N-1 | 1時間素材1エピソードの全処理を **15分以内** | 実測ベンチ(§7.1)より達成可能 |
| N-2 | 1エピソードのサーバ容量を **4GB 以内** | 素朴実装の 16.1GB に対する設計目標(§6) |
| N-3 | 同期精度: 残差 **±1ms 以内**、ドリフト補正誤差 **±1ppm 以内** | 検証済み手法で達成可能(§7 Stage 1) |
| N-4 | 同時実行ステージ数 1、トラック並列度 2 | 想定サーバは CPU 2コア |
| N-5 | 処理は決定論的(同入力・同パラメータ→同出力) | 中間成果物の破棄と再生成を成立させるため |

### 2.3 実測環境

本書の実測値(処理速度・容量・フィルタ挙動)はすべて次の環境で取得した:
**CPU 2コア / RAM 9GB / ffmpeg 4.4.2 フルビルド(librubberband 有効)/ Python 3.10**。
これを最小想定サーバとしても扱う。

実装の前提として次を実機検証済み:

- `ffmpeg -f f32le -` で raw float32 を stdout にパイプでき `numpy.frombuffer` で受け取れる → `soundfile`/libsndfile 依存を排除できる
- フィルタ引数の **`dB` サフィックスは全フィルタで有効**(`agate=threshold=-45dB`, `alimiter=limit=-1dB` 等)。`agate`/`acompressor`/`sidechaincompress` の threshold は内部的に線形振幅(0–1)だが dB 表記で記述可能
- `arnndn` は `.rnnn` モデルファイルの別途配布が必要 → NR の既定は同梱不要な `afftdn`

---

## 3. システム構成

### 3.1 構成図

```
ブラウザ  React + TypeScript + Vite(波形は自前 Canvas — §9)
   │  HTTP + JSON (進捗は1秒ポーリング)
FastAPI (uvicorn)
   │  ワーカースレッド + threading.Lock(同時1ステージ)
pipeline/*.py  ── numpy(解析のみ)
   │  subprocess
ffmpeg  ── 音の加工はすべてここ
   │
data/<project>/  project.json + ステージ成果物
```

### 3.2 技術選定と根拠

**バックエンドは必須。** 1時間素材を Web Audio API でデコードすると、話者トラック(mono) 659MB / BGM・ジングル(stereo) 1,318MB、**素材6本で 5,273MB** となりブラウザのタブ上限(2〜4GB)を超える。加えて `afftdn` や2パス `loudnorm` を ffmpeg.wasm で回すと実用速度に届かない。

**DSP は全て ffmpeg、numpy は解析専用。** numpy でサンプル処理を書くと 2コアでは速度が出ない。Python は「どのパラメータで ffmpeg を叩くか」を決めることに徹する。

**採用しなかった構成要素:**

| 要素 | 判断理由 |
|---|---|
| DB (SQLite/SQLAlchemy) | `project.json` 1枚で足りる。マイグレーションが不要になる |
| ジョブキュー (Celery/Redis) | ステージ承認制のため同時に1つしか走らない。ワーカースレッド + 単一ロックで十分 |
| SSE / WebSocket | ステージは分単位。1秒ポーリングで足りる |
| soundfile / librosa / pydub | ffmpeg パイプ + numpy で代替可能(§2.3) |
| audalign(同期ライブラリ) | 固定オフセット専用で**ドリフト補正を持たない**。librosa/pydub/matplotlib を引き込む |

**Python 依存は 3つのみ**: `fastapi` / `uvicorn` / `numpy`(チャンクアップロードは `application/octet-stream` の raw body で受けるため `python-multipart` は不要)

---

## 4. データ設計

### 4.1 ディレクトリ構造

```
data/<project-id>/
├─ project.json              # 状態はすべてここ(tmp+rename でアトミック書き込み。
│                            #   read-modify-write はプロジェクト単位の threading.Lock 内でのみ行う —
│                            #   並行アップロード・ステージ完了・承認の競合で更新が巻き戻るのを防ぐ。
│                            #   ステージ実行はワーカースレッドのため asyncio.Lock は使えない)
├─ assets/                   # 原素材(受信完了時に FLAC 化。転送中は <role>.upload)
└─ stages/
   ├─ 00_ingest/ … 06_export/
   │    ├─ <track>.flac          # 中間成果物 48kHz/24bit(GC 対象)
   │    ├─ preview_<track>.opus  # ブラウザ再生用 96kbps(cleanup のみ 128kbps — §13.2。永続)
   │    ├─ peaks_<track>.json    # 波形表示用(永続)
   │    └─ report.json           # 測定値・警告(永続)
```

### 4.2 `project.json` スキーマ

```jsonc
{
  "id": "p-1a2b3c4d5e6f",              // サーバ生成 ID(ユーザー入力をパスにしない — §13.3)
  "name": "ep-042",
  "created_at": "2026-08-06T12:00:00Z",
  "assets": {
    "speaker_a": {
      "path": "assets/speaker_a.flac", "bytes": 0, "received": 0,
      "status": "uploading|processing|ready|failed",
      "sha256": "…",                   // 原本バイトのハッシュ = 指紋の種
      "probe": {...}
    },
    "reference": {...}, "jingle": {...}, "bgm": {...}
  },
  "stage_order": ["ingest","sync","trim","cleanup","dynamics","mix","master","export"],
  "stages": {
    "sync": {
      "params": { "drift_threshold_ppm": 5.0, "n_segments": 10 },
      "input_fingerprint": "sha256:…",   // 再帰定義: sha256(上流ステージの指紋 + params の正規化JSON)。
                                         // ingest のみ原素材バイトの sha256 を種にする。
                                         // 成果物バイトは含めない(GC 後も計算可能・再ハッシュ不要。N-5 の決定性が前提)
      "status": "approved",              // pending|running|done|failed|approved。
                                         // stale は保存せず、読み出し時に指紋照合で導出して
                                         // effective_status として返す(保存すると指紋と二重管理になる)
      "artifacts_evicted": false,        // フル解像度を GC 済みか
      "started_at": "...", "finished_at": "...",
      "progress": "sync 推定: speaker_b",  // 実行中の進捗表示(1秒ポーリングで UI へ)
      "log_tail": ["..."],
      "report": {
        "offsets_ms":  { "speaker_a": 1234.5, ... },
        "drift_ppm":   { "speaker_a": 12.3, ... },
        "residual_ms": { "speaker_a": 0.4, ... },
        "warnings": []
      }
    },
    "trim": {
      "params": { "start_s": 0.0, "end_s": null },
      "report": {
        "requested_start_s": 0.0, "requested_end_s": null,
        "applied_start_s": 0.0, "applied_end_s": 3600.0,
        "output_length_s": 3600.0, "start_sample": 0, "end_sample": 172800000
      }
    }
  }
}
```

**指紋を成果物バイトではなく再帰定義にする理由**: 中間 FLAC は GC 対象(§6.2)なので、
成果物ハッシュを指紋に使うと GC 後に下流の指紋が計算できず stale 判定が崩壊する。
決定性(N-5)を前提に「上流の指紋 + パラメータ」で定義すれば、(a) GC 後も指紋が計算可能、
(b) ステージごとに数百 MB(FLAC 化後でも約 1GB)を毎回再ハッシュするコストも消える。

### 4.3 ステージ状態遷移と stale 判定

```
pending ──run──> running ──成功──> done ──approve──> approved
                    └────失敗────> failed
   ▲                                                    │
   └──────── 上流変更で input_fingerprint 不一致 ────────┘  → stale
```

**設計の要**: 各ステージを「入力指紋 + パラメータ」で決まる純粋関数として扱う。上流成果物やパラメータが変われば `input_fingerprint` が変わり、**下流ステージが自動的に stale になる**。これにより「Trimのパラメータだけ直して、そこから下だけ再実行」が静かに壊れない。DB なしでもこの性質は保てる。

この決定性は §6 の中間成果物 GC の前提でもある(消しても上流から再生成できる)。

---

## 5. 同期後の共通タイムラインと手動Trim

Stage 1 の出力仕様として明示する: **reference の t=0 を共通原点とし、3話者を同一長
(3本の補正後長の最大)へ無音パド/トリムして出力する。** 以降の全ステージ・波形表示・
A/B 比較はこの共通時間軸に乗る。

これを仕様化しないと、Stage 5 の `amix`(既定 `duration=longest`)が暗黙にパドして
動いてしまう一方、話者ごとの録音開始・終了は数十秒ずれうるため、A/B 比較や波形表示の
時間軸が揃わない。

新規プロジェクトでは Sync の後に独立した Trim ステージを置く。Trim の params は
`start_s`(既定 0) と `end_s`(既定は Sync の `program_length_samples`)で、サーバー側で
`0 <= start_s < end_s <= program_length_samples / 48000` を検証し、48kHzサンプル単位へ丸める。
共通時間軸の長さは丸めた秒数ではなく整数サンプルで保存する。
4トラックへ同じ `[start_s, end_s)` を `atrim` と `asetpts=PTS-STARTPTS` で適用し、
`speaker_a.flac` / `speaker_b.flac` / `speaker_c.flac` / `reference.flac` と
プレビュー・peaksを生成する。Trimを承認するまでCleanup以降は実行できない。
旧 `project.json` は旧 `stage_order` のまま扱い、移行は行わない。

---

## 6. ストレージ設計

**生成物はすべてサーバのディスクに載り、利用者のローカルPCには残らない。** 1エピソード(1時間)あたりの実測見積:

| 方針 | 容量 |
|---|---|
| 素朴に全ステージを 32bit float で保持 | **16.1 GB**(うち中間成果物 11.2GB) |
| 中間フル解像度をキャッシュ扱いで破棄 | 5.2 GB |
| **さらに原素材も FLAC 化** | **3.5 GB** ← 設計目標 |

### 6.1 中間フォーマットを FLAC 24bit に

可逆圧縮・ffmpeg ネイティブなのでパイプラインの形は変わらない。実測では合成音声(flite)で 31.8% まで縮んだが、これはノイズフロアが無い理想信号のため、**実録音では 50〜60% を見込む**(上表はこの前提)。

float32 の「クリップしない」利点は失うが、各ステージで -20 LUFS 管理されておりピークは -3dBFS 程度に収まるため 24bit(144dB) で足りる。ただし **Stage 5 の3本合算のみ最大 +9.5dB 増えうる**ため、mix 段の入力側でゲインを引く。

### 6.2 中間フル解像度を「キャッシュ」として扱う

| 種別 | 扱い |
|---|---|
| 原素材 / preview.opus / peaks.json / report.json / 最終書き出し | **永続化** |
| 各ステージの中間 FLAC | **GC 可能**。再実行時に必要な分だけ上流から再生成 |

GC ポリシー: 直近2ステージ分は残す(再実行が速い)。それ以前はエピソード完了後に自動 purge。UI にプロジェクト容量を表示し手動 purge も可能にする。

### 6.3 アップロード

原素材が 48k/24bit stereo なら **4本で約4GB**。素の multipart 一発では回線切断やリバースプロキシのタイムアウトで失敗する。ブラウザ側 `File.slice()` で分割 → `PUT /api/projects/{id}/assets/{role}/chunk?offset=`(raw octet-stream)に追記し、`project.json` の `received` バイト数でレジュームする(冪等な位置指定書き込みのため PUT)。受信後に FLAC 化して原本 WAV は破棄。

---

## 7. 処理設計(パイプライン)

各ステージ共通インターフェース: `run(ctx, params) -> report`(`ctx` = プロジェクトのスナップショット + パス解決 + 進捗コールバック)。入力は上流ステージの成果物、出力は `stages/<nn>_<name>/`。

### Stage 0 — Ingest(取り込み・検証)

- `ffprobe` で全ファイルのサンプルレート / チャンネル / 長さ / コーデックを取得
- 作業フォーマットへ統一: **話者・reference は 48kHz / モノ / FLAC 24bit**(話者は本質的にモノ、reference は同期解析とTrim確認用)。**BGM・ジングルは 2ch に固定**(モノ素材が来ても Stage 5 の acrossfade/amix でチャンネル数が揃うように)
- QC 検出: クリッピング率、DC オフセット、無音率、長さの不一致
- `peaks.json` と `preview.opus` を生成

### Stage 1 — Sync(音合わせ)— 技術的中核

リモート録音では**固定オフセット**と**クロックドリフト**の両方が出る。民生機の水晶は ±20〜50ppm、2台間では最大100ppm差。50ppm なら1時間で180msずれるため、ドリフト補正は省略できない。

**手法: GCC-PHAT(Generalized Cross-Correlation with Phase Transform)** — 時間差推定(TDOA)の教科書的標準手法。MATLAB にも `gccphat` として組み込まれている。`numpy.fft` で約25行:

```python
X1, X2 = fft(a), fft(b)
G = X1 * conj(X2)
R = ifft(G / abs(G))    # PHAT 重み付け = 位相のみ残す
lag = argmax(R)
```

**処理手順**

1. **エンベロープ生成** — 各トラックと reference について 20ms ホップの RMS を dB 化し平均減算(レベル差の影響を消す)→ 50Hz の1次元信号
   - 48kHz 生波形のフル相関は 2コアでは重い。50Hz なら1時間で18万サンプルで済む
2. **粗探索** — エンベロープ同士を**素の相互相関**でフルレンジ相関 → ±20ms 精度
   - **エンベロープ段に PHAT は使わない(実測)**: PHAT の白色化は「その話者が写っていない区間」の不一致成分まで等重み化し、reference に他話者の声が乗る本ケースでは相関がほぼ平坦になり偽ピークに負ける。素の相関はバーストのエネルギーで自然に重み付けされ頑健
3. **微調整** — 粗オフセット周辺(±max(50ms, ドリフト累積分)) で 8kHz にダウンサンプルした波形を GCC-PHAT → サンプル精度(放物線補間でサブサンプル)
   - **両端に半ハン窓テーパー必須(実測)**: ゼロパディング境界の段差は広帯域コヒーレントで、PHAT が「lag=切り出し長差」に真のピークより高い偽ピークを立てる
4. **ドリフト推定** — 収録を約10区間に分割し各区間で独立にオフセット推定 → 「時間 vs ずれ」を**外れ値除去付きロバストフィット**。傾き = ドリフト率 `s`[秒/秒]、切片 = 初期オフセット `d0`
   - その話者が喋っていない区間の除外は2条件(実測で確定): (a) 話者側 RMS が最大区間より 35dB 以上低い(物理的な無音判定)、(b) PHAT ピーク/RMS 比が乱数水準(≈5.5)に近い
   - **「最大ピークの 0.25 倍」のような相対足切りは使わない** — 実発話区間のピークは数百に達し、正常区間まで除外してフィットを壊す(test_silence_robustness で実証)
   - 初期フィットは **Theil–Sen**(ペアワイズ傾きの中央値)で残存外れ値に耐え、MAD 基準の除外を挟んで信頼度重み付き最小二乗で磨く
5. **補正適用** — サンプル単位で行う(`adelay` の ms 整数指定は ±0.5ms の量子化誤差を持ち、残差予算 ±1ms の半分を適用段で使い切るため)
   - オフセット: `adelay=<n>S:all=1`(正)/ `atrim=start_sample=<n>`(負)→ 適用誤差 ±0.5 サンプル(±10µs)
   - ドリフト: `|s| > 閾値`(既定 5ppm)のとき **`rubberband=tempo=<1/(1+s)>`**
   - 出力タイムラインは §5 の共通時間軸に揃える
6. **検証** — 補正後に再度相関し残差が ±1ms 以内か確認。超えたら UI に警告(原因候補として reference=VoIP 録音のジッタバッファ起因の非線形ジャンプを提示 — §13.1)

**ドリフト補正手段の検証結果(重要)**

100秒信号に 12ppm 補正を掛けて実効値を実測:

| 手法 | 実効値 | 判定 |
|---|---|---|
| `atempo=1.000012` | **0.00 ppm** | **無効**。ppm 級では全く効かない |
| `atempo=1.001`(1000ppm検証) | 997.71 ppm | 2.3ppm 誤差。閾値と同オーダー |
| `asetrate` 直接 | — | 整数のため 48kHz では **20.8ppm 刻み**。5ppm 閾値を表現不能 |
| 10倍/192kHz 領域で `asetrate` | -12.50 / -10.42 ppm | 量子化する上に高コスト |
| **`rubberband=tempo=1.000012`** | **11.87 ppm** | **採用**。誤差 0.13ppm = 1時間で 0.47ms |

**UI 確認**: 補正前後の「時間 vs ずれ」グラフ、3トラック重ね波形、代表箇所の同時再生。

### Stage 2 — Trim(収録前後の手動切り出し)

- 話者A/B/Cとreferenceの4本を同一時間軸で波形表示する
- 範囲ドラッグまたは左右ハンドルで共通範囲を編集し、再実行時に全トラックへ同じ範囲を適用する
- 自動無音検出、間の詰め、トラック別範囲、開始・終了の余白は扱わない
- 指定値、サンプル単位へ丸めた実適用値、出力長をレポートへ保存する

### Stage 3 — Cleanup(話者ごとの整音)

**順序が音質を決める。** 各話者トラックに順に適用:

1. **ハイパス 80Hz** — `highpass=f=80:p=2`(空調・机の振動・ポップ)。**DC オフセットもここで落ちる**ため独立ステップは持たない(DC は Stage 0 の QC 測定のみ)
2. **ハムノイズ除去** — FFT で 50/60Hz の基本波を検出し、倍音3本まで `bandreject` でノッチ。**日本は東西で商用電源周波数が違うため自動検出する**
3. **ディクリック / ディクリップ** — `adeclick` / `adeclip`(Stage 0 の検出結果に応じて)
4. **ノイズリダクション** — RMS 下位5%の 0.5秒窓群の中央値をノイズフロア推定とし
   `afftdn=nr=<パラメータ>:nf=<推定dB>:tn=1`
   - **削減量は 12dB で上限クランプ**(`nr` の既定値と同じ)。かけすぎると声が水中のような音になり、これが自動NRの最大の失敗モード
5. **ディエッサー** — `deesser=i=0.3:m=0.5:f=0.5`(`i`/`m`/`f` はいずれも 0–1 の正規化値。`f` は Hz ではない)
6. **ノイズゲート** — `agate=threshold=<ノイズフロア+6dB>:range=-12dB:attack=10:release=250`
   - **完全カットせず `range` で -12dB 減衰に留める**(切りすぎると呼吸が消えて不自然)
7. **話者間ラウドネス整合** — `ebur128` で Integrated を測定し **`volume=<-20−測定値>dB` の純線形ゲインで各トラックを -20 LUFS に揃える**
   - これを Stage 4 の前にやらないと、元レベル差のせいで話者ごとにコンプの効きがバラつく
   - **loudnorm は使わない**: linear=true でも TP 制約に当たると無告知でダイナミックモードに落ち中間段で音を潰す。かつ最重フィルタ(53x — §7.1)を3本×2パス節約できる(約1.1分短縮)。ピーク管理は §6.1 の mix 入力ゲインと Stage 6 が担う。loudnorm の 2パス運用は最終マスタリング(Stage 6)専用

### Stage 4 — Dynamics(コンプ・EQ)

1. **コンプ1段目(グルー)**: `acompressor=threshold=-20dB:ratio=2.5:attack=10:release=150`
2. **コンプ2段目(ピーク)**: `acompressor=threshold=-12dB:ratio=6:attack=2:release=60`
   - **2段に分ける理由**: 1段で強く掛けるより明確に自然。放送・Podcast の定番手法
3. **EQ(任意・既定は控えめ)**: 3–5kHz を +2dB(明瞭度)、200–300Hz を -2dB(こもり除去)

### Stage 5 — Mix

1. **ステレオ配置** — 3話者を L(-30%) / C / R(+30%) に**薄く**パン。深くパンするとモノ再生で破綻する
2. **話者バス合成** — `amix=inputs=3:normalize=0`
   - **`normalize=0` が必須。** 既定の `normalize=1` は入力数で自動的に割るため勝手に -9.5dB される
   - 24bit 中間のため、合算前に入力ゲインを引いてクリップを避ける
3. **ジングル** — 冒頭配置、本編と `acrossfade` でつなぐ
4. **BGM ループ** — 2段構え
   - まず **BGM 単体でシームレスなループ単位を作る**(末尾 N 秒を先頭にクロスフェードで重ねる)。継ぎ目のプチノイズを防ぐ
   - それを **`-stream_loop -1` で必要長まで繰り返し `-t` で切る**
   - **`aloop` は使わない。** `size` サンプル分をメモリに保持する仕様のため、1時間ステレオで 1.3GB を消費し RAM 9GB の環境では危険
5. **サイドチェイン・ダッキング** — `sidechaincompress=threshold=-30dB:ratio=8:attack=20:release=300` で話者バスをキーに BGM を -10〜-14dB 下げる
   - **実装注意(実測)**: sidechaincompress は両入力に `aformat=channel_layouts=stereo` の明示が必要(未確定レイアウトでフィルタグラフ初期化に失敗)。また `-stream_loop -1` の無限入力は `atrim` では EOF が出ず終了しないため、**入力側 `-t` で読み込みを打ち切る**
   - **無いと BGM が声を潰す。Podcast では必須**
6. **BGM ベースレベル** — 話者バスに対し -22〜-26 LUFS 相当

### Stage 6 — Master

1. **ラウドネスノーマライズ** — `loudnorm` を**必ず2パス**
   - 1パス目: `loudnorm=print_format=json` で `measured_I / measured_LRA / measured_TP / measured_thresh` を取得
   - 2パス目: 測定値を渡して `loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:measured_I=…`
   - **`linear=true` が重要。** 1パスのダイナミックモードは音を潰す。2パスにして初めて linear が使える
   - **loudnorm は内部 192kHz で出力する** → 直後に `aresample=48000` で作業レートへ戻す
2. **セーフティリミッター** — `alimiter=limit=-1dB:attack=5:release=50:asc=1:level=0`
   - **`level=0` 必須(実測)**: 既定の auto-level はリミット後に 1/limit (+1.0dB) のゲインを掛け、直前の loudnorm のラウドネス目標を壊す
   - **alimiter はオーバーサンプリングしないサンプルピーク・リミッター**であり、真の TP 制御は loudnorm 2パス目の `TP=-1.5` が担う。alimiter はその後段の安全網で、-1.5 → -1.0 の 0.5dB マージンでインターサンプルピークを吸収する
   - TP ≤ -1.0 dBTP の最終保証は §12.2 の `ebur128` 実測 assert が受け持つ
   - **-1.0 dBTP にする理由**: MP3/AAC エンコード時にピークが上がるため 0 dBFS だと再生側でクリップする
3. **最終測定** — `ebur128` で I / LRA / TP / 位相相関を測り目標との差分をレポート

### Stage 7 — Export

WAV 48kHz/24bit(保管用)、MP3 192kbps CBR、AAC 128kbps、メタデータ埋め込み、QC レポート JSON。

### 7.1 処理時間の実測

ピンクノイズ60秒に対する実測(§2.3 の環境、シングルスレッド):

| 処理 | 実時間比 | 1時間素材あたり |
|---|---|---|
| ベースライン(`anull`) | 637x | 0.1分 |
| `afftdn` | 154x | 0.4分 |
| `rubberband` | 74x | 0.8分 |
| **`loudnorm`(1パス)** | **53x** | **1.1分** ← 最も重い |

**ボトルネックは `afftdn` ではなく2パスの `loudnorm`。** 話者3本を2並列で回す前提で、**1エピソード合計 10〜15分**の見込み(要件 N-1 を満たす)。

---

## 8. API 設計

| メソッド | パス | 用途 |
|---|---|---|
| `GET` | `/api/projects` | プロジェクト一覧 |
| `POST` | `/api/projects` | 新規作成 |
| `GET` | `/api/projects/{id}` | `project.json` をそのまま返す(1秒ポーリング先) |
| `DELETE` | `/api/projects/{id}` | 削除 |
| `POST` | `/api/projects/{id}/assets/{role}/init` | アップロード開始(総バイト数を登録) |
| `PUT` | `/api/projects/{id}/assets/{role}/chunk?offset=` | チャンク追記(レジューム対応) |
| `POST` | `/api/projects/{id}/stages/{stage}/run` | パラメータを渡して実行 |
| `POST` | `/api/projects/{id}/stages/{stage}/approve` | 承認 |
| `GET` | `/api/projects/{id}/stages/{stage}/preview?name=` | `preview_<track>.opus` を配信(トラック名はホワイトリスト照合) |
| `GET` | `/api/projects/{id}/stages/{stage}/peaks?name=` | `peaks_<track>.json` |
| `GET` | `/api/projects/{id}/download/{artifact}` | 成果物ダウンロード(名前はホワイトリスト照合 — §13.3) |
| `GET` | `/api/projects/{id}/usage` | 容量集計 |
| `POST` | `/api/projects/{id}/gc` | 中間成果物の purge |

**中間 FLAC はブラウザに送らない**(1時間で数百MB)。配信するのは `preview.opus`(1時間41MB)と `peaks.json` のみ。

---

## 9. 画面設計

**画面1: プロジェクト** — 一覧・新規作成・6ファイルにロールを割り当ててアップロード(チャンク進捗バー、レジューム表示)

**画面2: パイプライン(メイン)** — ステージが縦に並ぶ

- 各ステージ: パラメータフォーム / 実行ボタン / 進捗 / 結果(波形・測定値・プレイヤー)/「承認して次へ」
- 承認済みは折りたたみ。上流変更で stale になったステージに赤バッジ
- **A/B 比較プレイヤー**(処理前/後を同じ再生位置のまま切替)← **整音の良し悪しはこれ無しに判断できない。最優先で実装する**
- Stage 1 は専用ビュー: 「時間 vs ずれ」グラフ、3トラック重ね波形、残差表示
- Stage 2 は専用ビュー: 4トラックの共通時間軸、範囲ドラッグ・左右ハンドル、指定時刻表示
- サイドに **プロジェクト容量 + purge ボタン**

波形は事前計算済み `peaks.json` を自前の Canvas コンポーネント(約60行)で描画する。使う機能が「peaks 描画+クリックシーク+再生カーソル」のみで、wavesurfer.js の残り機能(ズーム・リージョン・デコード)は不要のため依存を持たない。Vite ビルド成果物を FastAPI が配信し、Docker 1コンテナで完結させる。

---

## 10. ディレクトリ構成

```
podalign/
├─ pyproject.toml            # uv / fastapi・uvicorn・numpy
├─ app/
│  ├─ main.py                # FastAPI: ルーティング + static配信
│  ├─ api.py                 # projects / assets(chunk) / stages(run,approve) / gc
│  ├─ project.py             # project.json の read/write(atomic) + 指紋 + stale判定
│  ├─ storage.py             # パス解決・容量集計・GC
│  ├─ runner.py              # ワーカースレッド + threading.Lock、GC 済み上流の自動再生成
│  └─ pipeline/
│     ├─ base.py             # Stage 共通インターフェース
│     ├─ ffmpeg.py           # ffmpeg/ffprobe ラッパ, filter_complex builder, f32leパイプ
│     ├─ analysis.py         # GCC-PHAT / ノイズプロファイル / ラウドネス測定 / peaks生成
│     └─ s0_ingest.py … s6_export.py / s2_trim.py
├─ web/                      # React + TS + Vite
│  └─ src/components/{ProjectList,Pipeline,StagePanel,ABPlayer,Waveform,SyncView,TrimView,Uploader}.tsx
├─ presets/default.json      # 既定パラメータ(YAML パーサ依存を避け JSON)
├─ tests/
├─ Dockerfile                # python + ffmpeg + ビルド済み web
└─ data/                     # プロジェクト作業ディレクトリ
```

---

## 11. 異常系

| 事象 | 対応 |
|---|---|
| 同期の残差が ±1ms を超える | ステージを `done` にしつつ warning を出し、承認前に UI で警告表示 |
| ある話者が長時間無音で相関が立たない | 低信頼区間として除外。全区間が低信頼ならエラーで停止し手動オフセット入力を促す |
| ドリフトが 100ppm を大きく超える | 素材の取り違え等を疑いエラー停止 |
| アップロード中の切断 | `received` バイト数からレジューム |
| ffmpeg 異常終了 | stderr 末尾を `log_tail` に保存し `failed` に遷移。上流成果物は保持 |
| ディスク不足 | 実行前に必要容量を見積もりチェック。不足時は GC を促す |
| GC 済みステージの再実行要求 | 上流から自動で再生成してから実行 |

---

## 12. テスト

### 12.1 Sync のユニットテスト(最重要)— `tests/test_sync.py`

**既知のオフセットとドリフトを人工的に与えた合成トラックを作り、推定値が復元できるかを assert する。**

- ffmpeg で音声素材に +1.234s のオフセットと +12ppm のドリフトを与えて合成
- オフセットを **±1ms 以内**、ドリフトを **±1ppm 以内**で復元できること
- **補正の符号が正しいこと**(rubberband 往復テストで固定。ずれを off(t)=話者時刻−ref時刻=d0+s·t と定義したとき **`rubberband=tempo=(1+s)`・オフセット d0/(1+s)** が正)
- 「片方の話者が長時間無音」でもフィットが破綻しないこと(低信頼区間の除外が効いているか)

これが通れば Stage 1 の信頼性は担保できる。

### 12.2 ラウドネスの自動検証 — `tests/test_pipeline_e2e.py`

出力を `ffmpeg -af ebur128` で測定し **Integrated -16 ±0.5 LUFS、True Peak ≤ -1.0 dBTP** を assert。

### 12.3 ストレージの検証 — `tests/test_pipeline_e2e.py`

1エピソード通した後の `data/<project>/` 実容量を測り GC の効果を確認(目標 3.5GB 前後)。GC 済みステージを再実行して上流から正しく再生成されることも確認する。

### 12.4 stale 判定の検証 — `tests/test_stale.py`

Trim のパラメータを変更したとき、Trim自身とCleanup以降が stale になり、Ingest/Sync は影響を受けないことを確認。

### 12.5 Trim の検証

全範囲・指定範囲・`start >= end`・範囲外を検証し、48kHzサンプル境界、全トラックの同一長、
出力の先頭が0秒であることを確認する。E2EではTrimの実行・承認後にCleanup以降を実行する。

### 12.6 実素材エンドツーエンド

実素材6本を投入し全ステージを承認しながら通す。処理時間を実測し UI の見積もり表示に反映(合成素材の E2E は 12.2/12.3 で自動化済み。実素材での検証は運用で実施)。

---

## 13. 既知の制約と運用上の注意

### 13.1 reference は VoIP 録音である

通話録音はジッタバッファ適応による非線形な時間ジャンプが入りうる。線形ドリフトモデル+
外れ値除去付きフィットで概ね吸収できるが、残差 >±1ms 警告の原因候補として UI の警告文言に
含める。区間別オフセット系列に不連続(>20ms の段差)があれば個別に警告する。

### 13.2 preview.opus のビットレート

Opus 96kbps は音声にはほぼ透明だが、A/B の目的が「NR アーティファクト検出(水中音)」である
cleanup ステージだけは判定余裕が薄い。cleanup のみ 128kbps とし、パラメータ化しておく。

### 13.3 パス・トラバーサル対策

`GET /download/{artifact}` などファイル名を受ける API は、名前を `project.json` に記録された
成果物名のホワイトリストとの照合のみで解決し、パス結合をしない。project-id もユーザー入力を
そのままパスにせずサーバ生成 ID を使う。

### 13.4 将来の Web サービス化への影響

認証はリバースプロキシ層で後付け可能、ストレージはプロジェクトディレクトリ単位で独立して
いるためオブジェクトストレージへ移行可能、API は既に REST。現設計のまま拡張できる。

---

## 14. スコープ外

自動無音検出・自動的な間の詰め・フィラー除去 / トラック別Trim / 開始・終了の余白指定 /
文字起こし・SRT・チャプター / 認証・マルチユーザー。

ステージは共通インターフェース(§7 冒頭)で定義し `project.json` の `stage_order` に順序を
持たせるため、後から工程を差し込める(文字起こし系は GPU 前提になるため足す場合は要件再検討)。
