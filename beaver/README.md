# beaver マスターデータ（🦫簡単自作PC管理）

🦫簡単自作PC管理（EasyCustomPC）向けのマザーボード仕様マスターデータドメインです。

## ファイル一覧

| ファイル | 内容 | 更新方針 |
| --- | --- | --- |
| `boards.json` | 管理対象マザーボードの入力リスト（ID・メーカー・型番・仕様URL・`raw_spec_text`） | 基板追加・編集時に手動更新 |
| `motherboard_master.json` | アプリ側パラメトリックSVG生成器が参照する確定済み基板マスターデータ | GitHub Actions（`workflow_dispatch`）で生成し PR 経由でマージ |
| `meta.json` | 最終更新日時・収録件数・スキーマバージョン | `motherboard_master.json` 更新時に同時出力 |

## 基板を追加・更新する手順

1. **`beaver/boards.json` にエントリを追加**します：
   ```json
   {
     "id": "gigabyte-b650-aorus-elite-ax",
     "maker": "GIGABYTE",
     "model": "B650 AORUS ELITE AX",
     "spec_url": "https://www.gigabyte.com/jp/Motherboard/B650-AORUS-ELITE-AX-rev-10-11/sp",
     "is_premium": true,
     "raw_spec_text": ""
   }
   ```
   - **メーカーサイトが SPA や 403 ブロックで取得できない場合**：公式スペック表のテキストをコピーして `raw_spec_text` に貼り付けておくと、スクリプトは HTTP フェッチをスキップして `raw_spec_text` を優先利用します。
2. `beaver/boards.json` を `main` ブランチへコミット・プッシュします。
3. GitHub リポジトリの **Actions** タブから **`Update beaver motherboard master data`** を手動実行（`Run workflow`）します：
   - パラメータ未指定：未登録の差分（`motherboard_master.json` に未収録のID）のみ取得します。
   - `target_id` 指定：特定の基板IDのみ強制再取得します。
   - `force_all: true`：既存レコードを含めて全件再取得します。
4. 差分がある場合、自動で Pull Request（ブランチ：`chore/update-beaver-boards`）が作成されます。抽出されたスロット数・寸法を確認し、問題なければマージしてください。差分がない場合は PR は作成されません。

## 出力スキーマ (`motherboard_master.json`) とバリデーション規約

- **トップレベル**: `schemaVersion` (`1`), `updatedAt` (ISO 8601 UTC), `meta` (`app`, `schema_version`, `updated_at`, `template_count`), `boards[]`
- **各基板レコード**:
  - `form_factor`: `"ATX"` | `"Micro-ATX"` | `"Mini-ITX"` | `"E-ATX"`
  - `width_mm`, `height_mm`: `100`〜`400` の整数（明記がない場合はフォームファクタ標準値で補完：ATX `305x244` / Micro-ATX `244x244` / Mini-ITX `170x170` / E-ATX `305x330`）
  - `slots`: 物理コネクタ数（排他利用による無効化注釈は無視）
    - `cpu_socket`: `1`
    - `cpu_8pin`: `1`〜`4`
    - `atx_24pin`: `1`
    - `dimm`: `1`〜`8`
    - `pcie_x16`: `1`〜`4`
    - `pcie_x1`: `0`〜`6`
    - `m2`: `0`〜`6`
    - `sata`: `0`〜`12`
- **エラー耐性**: HTTP 取得失敗・ボットブロック・バリデーションエラーが発生した基板は警告ログを出してスキップし、既存の `motherboard_master.json` にある前回値を維持します。

## GitHub Secrets 設定

ワークフローは `secrets.GEMINI_API_KEY`（未設定時は既存の `secrets.GEMINIAPI`）を参照して Gemini API（`gemini-2.5-flash` / Structured Outputs）を呼び出します。API キーは配信 JSON やログには一切出力されません。
