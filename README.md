# kuma-master-data

**佐々木熊太郎**が公開しているアプリ群（🐧簡単コンテナ管理、🦅簡単サービス診断、🐯簡単配当日記、🐿️簡単収納管理 など）のための、読み取り専用マスターデータ（JSON）リポジトリです。

## 設計原則

- **秘密情報なし・完全公開**：このリポジトリには API キーや個人情報、ユーザー環境データは一切含まれません。
- **人間のレビューまたは自動検証**：`penguin` の定期更新は Pull Request 経由で管理者が確認してマージします。`dividend` は週次バッチのサニティチェック・前回値フォールバック検証を通過した差分のみ自動コミットします。
- **完全ローカル照合**：各アプリは起動時（1日1回まで／または手動更新時）に自分のドメインの JSON と `apps.json` のみを取得し、端末内のローカル SQLite に保存してオフラインでも診断・照合を行います。
- **ユーザーデータ非送信**：アプリ側からユーザー環境情報や診断結果を外部へ送信することはありません。

## ディレクトリ構成

```text
kuma-master-data/
├── apps.json                # 各アプリの最新安定版バージョン定義
├── penguin/
│   ├── engines.json         # Docker / Podman / nerdctl の最新版・リリース履歴
│   ├── runtimes.json        # コンテナ内・ローカルランタイムのブランチ別 EOL / 最新パッチ / 緊急 advisory
│   └── README.md            # 情報源・更新方針・緊急 advisory の手動追加手順
├── dividend/
│   ├── dividend_master.json # 配当・銘柄マスターデータ（🐯簡単配当日記用）
│   ├── meta.json            # 配当マスターのメタ情報・免責事項・更新日時
│   └── README.md            # スキーマ・免責事項・運用方針
├── storage/
│   ├── templates.json       # 見取り図SVGテンプレート追加配信データ（🐿️簡単収納管理用）
│   └── README.md            # SVGテンプレートのスキーマ・配色・タグ制約ルール
├── scripts/
│   ├── update_penguin.py        # penguin 定期更新スクリプト（Python 標準ライブラリのみ）
│   ├── update_dividends.py      # dividend 定期更新スクリプト（Gemini API 構造化出力）
│   ├── dividend_tickers.json    # 配当マスター更新対象のJP/US銘柄リスト
│   └── requirements-dividend.txt# dividend 更新スクリプト用依存パッケージ
└── .github/workflows/
    ├── update-penguin.yml       # penguin 週次自動更新ワークフロー（PR 作成）
    └── update-dividend-data.yml # dividend 週次自動更新ワークフロー（毎週土曜 06:00 JST）
```

## スキーマ共通ルール

すべての JSON ファイルはトップレベルに以下を持ちます。

- `schemaVersion`: 整数（初版は `1`）。アプリが解釈できる版より新しい JSON は無視し、既存キャッシュで続行します。
- `updatedAt`: ISO 8601 UTC 日時（例：`2026-11-19T00:00:00Z`）。データ本体に差分があるときのみ更新されます。
