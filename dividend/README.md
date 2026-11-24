# dividend マスターデータ（🐯簡単配当日記）

🐯簡単配当日記（YutaiNote）向けの配当・銘柄マスターデータドメインです。

## ファイル一覧

| ファイル | 内容 | 更新方針 |
| --- | --- | --- |
| `dividend_master.json` | 日本高配当株・J-REIT・ETFおよび米国高配当ETF/株の予想配当マスターデータ | 週1回 GitHub Actions（Gemini API 構造化出力）により自動更新・自動コミット |
| `meta.json` | 最終更新日時・件数・免責事項メタ情報 | `dividend_master.json` 更新時に同時出力 |

## 配信スキーマ (`dividend_master.json`)

- トップレベル共通項目: `schemaVersion` (`1`), `updatedAt` (ISO 8601 UTC)
- `meta`:
  - `schema_version`: `"1.0"`
  - `updated_at`: ISO 8601 JST (`+09:00`)
  - `source_note`: AI自動集計に関する免責事項（日本語）
  - `source_note_en`: 免責事項（英語）
  - `total_stocks`: 収録銘柄数
- `stocks[]`:
  - `ticker`: 証券コード / ティッカー（日本株は `.T` 付き、例: `"8306.T"`, `"VYM"`）
  - `market`: `"JP"` | `"US"`
  - `name_ja` / `name_en`: 日本語名 / 英語名
  - `sector`: 業種・セクター
  - `annual_dividend_forecast`: 1株あたり年間予想配当額
  - `currency`: `"JPY"` | `"USD"`
  - `dividend_yield_percent`: 参考配当利回り（%）
  - `ex_dividend_months`: 権利確定月の配列（例: `[3, 9]`）
  - `payment_months`: 配当支払予定月の配列（例: `[6, 12]`）
  - `consecutive_years`: 連続増配・非減配年数（不明時は `null`）
  - `ai_summary_ja`: 配当方針・増減配動向の要約（100文字以内）

## 運用・セキュリティ原則

- **APIキー非露出**: Gemini API キーは GitHub Secrets（`GEMINIAPI`）として Actions 実行環境内でのみ参照され、配信 JSON やクライアントアプリには一切含まれません。
- **異常値保護**: 配当利回り `< 0%` または `> 25%`、必須項目の欠落・型不整合などが検知された場合、該当銘柄は前回生成済みの正常レコードへフォールバックされます。
- **免責事項**: 本データはAIによる自動収集・集計結果であり、正確性を保証するものではありません。投資判断や確定申告の際は必ず各企業のIR情報・証券会社の報告書をご確認ください。
