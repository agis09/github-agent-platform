# Security Policy

## 脆弱性の報告

脆弱性は公開 Issue に書かず、GitHub の **Private vulnerability reporting**（Security タブ → Report a vulnerability）で報告してください。

再現手順、影響範囲、想定される攻撃者（Issue を書ける外部ユーザー、リポジトリの collaborator、クラスタ内の Pod など）を含めてもらえると対応が早くなります。

## 対象

特に次の境界を破るものを重視します（設計は [docs/design.md §12](./docs/design.md#12-セキュリティモデル)）。

- 承認なしでのコード変更・PR 作成（承認ゲートの回避、plan_hash の偽装）
- Sandbox からの credential 取得、許可されていない宛先への通信
- Publisher の patch 検査の回避（範囲外ファイル、symlink、submodule など）
- webhook の署名検証・起動者判定の回避
- secret のログ・成果物・Issue コメントへの漏えい
- 検索クエリのサニタイザ回避による情報送信

## 利用者の責任範囲

- GitHub App の秘密鍵、LLM / 検索 API のキー、Terraform の state の保護
- NetworkPolicy を強制する CNI の利用
- `egress_level` や検索の有効化など、リスクを上げる設定の判断
