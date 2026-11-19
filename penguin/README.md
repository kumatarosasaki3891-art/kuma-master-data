# penguin マスターデータ

🐧簡単コンテナ管理（およびランタイム定義を共用する 🦅簡単サービス診断）が参照する定義ファイル群です。

## ファイル一覧と情報源

| ファイル | 内容 | 主な情報源 | 更新頻度 |
| --- | --- | --- | --- |
| `engines.json` | Docker Engine / Podman / nerdctl の最新安定版と直近リリース | GitHub Releases API (`moby/moby`, `containers/podman`, `containerd/nerdctl`) | 週1回（自動 PR） |
| `runtimes.json` | nginx / PHP / MySQL / MariaDB / PostgreSQL / Redis / Apache httpd のブランチ別最新パッチ・EOL | endoflife.date API (`https://endoflife.date/api/<product>.json`) | 週1回（自動 PR） |

## 緊急 advisory の運用方針（手動 PR のみ）

- `runtimes.json` の `advisories` 配列は GitHub Actions による自動書き換えの対象外です（更新スクリプトは既存の `advisories` をそのまま保持します）。
- NVD 全件の自動取り込みは行いません。主要ランタイムに対する未認証 RCE 級（CVSS 9 前後目安）かつ公式修正版が公開されている重大件のみを、公式セキュリティアドバイザリで裏取りした上で手動 PR により追加します。
- `affectedRanges` は `from` 以上・`toExclusive` 未満の半開区間で記載します。
