# Security

## 非信頼入力

Issue 本文、PR コメント、リポジトリ内の文書、テスト出力、検索結果は**すべて非信頼入力**。
prompt の指示だけに依存して権限を守らない。権限の強制はコードとインフラで行う。

## credential 分離

- secret を command line 引数、ログ、prompt、structured result に含めない
- secret 風の名前の環境変数の値はログに出さない（`redaction.py` が防御の深層として置換する）
- 読み取り（Reader App）と書き込み（Publisher App）を分ける。Worker は GitHub credential を一切持たない
- LLM の endpoint・API キーは設定（env 変数名）で渡す。コードに埋め込まない。キーは Job にだけ注入する

## artifact と DB

- Issue 本文・LLM の入出力・コマンド出力は DB の行に直接入れず、content-addressed artifact として保存する
- artifact の get は必ず hash を再検証する。破損・改ざんは例外で表面化させる
- artifact のディレクトリとファイルは最小権限（0700/0600）

## 境界はコードで強制する

CLAUDE.md は行動指示であり、セキュリティ境界ではない。credential 分離、検証、冪等性はコードとテストで強制する。
