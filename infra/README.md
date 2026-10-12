# infra

| ディレクトリ | 内容 |
|---|---|
| [`platform/`](./platform/) | プラットフォーム本体のデプロイ（クラウド非依存、kubernetes provider のみ）。**通常はここだけ使う** |
| [`clusters/kind/`](./clusters/kind/) | ローカル検証用クラスタの例 |
| [`clusters/sakura-k3s/`](./clusters/sakura-k3s/) | さくらのクラウド上に k3s クラスタを構築する例 |

Terraform の state には Secret の値が含まれます。チームで使う場合は暗号化されたリモート backend（S3 + ロック、GCS、Terraform Cloud など）を設定してください（`backend` ブロックは利用者ごとに追加します）。

`*.tfvars`（`*.tfvars.example` を除く）、`*.tfstate`、`.terraform/` は gitignore 済みです。
