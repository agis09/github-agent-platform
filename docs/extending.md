# 拡張ガイド

このプラットフォームの差し替え点は 3 つです。どれも「Protocol を実装し、レジストリに 1 行足す」形になっています。

| 差し替え点 | Protocol | レジストリ | 選択方法 |
|---|---|---|---|
| エージェント CLI | `agent_runtime.base.AgentRuntime` | `agent_runtime.RUNTIMES` + `config.AGENT_RUNTIMES` | `agent_runtime` |
| Web 検索 API | `search.base.SearchProvider` | `search.upstreams.UPSTREAMS` | `search_provider` |
| クラスタ / クラウド | （Kubernetes） | `infra/clusters/<name>/` | kubeconfig |

変更後は必ず `uv run pytest && uv run ruff check src tests && uv run mypy` を通してください。テストはネットワークなしで動くように書きます。

---

## エージェントランタイムを追加する

例として、架空の CLI `acme-agent` を追加します。

### 1. ランタイムを実装する

`src/agent_platform/agent_runtime/acme.py`:

```python
import os, shutil, subprocess
from pathlib import Path

from agent_platform.agent_runtime.base import RunMode, RunOutcome
from agent_platform.config import Settings
from agent_platform.errors import AdapterError, AdapterTimeoutError


class AcmeRuntime:
    name = "acme"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def run(self, *, prompt: str, mode: RunMode, cwd: Path | None = None,
            output_schema: Path | None = None) -> RunOutcome:
        s = self._settings
        binary = shutil.which(s.agent_binary)
        if binary is None:
            raise AdapterError(f"acme binary not found: {s.agent_binary!r}")
        cmd = [binary, "run", "--json", "--read-only" if mode is RunMode.READ_ONLY else "--write"]
        if s.agent_model:
            cmd += ["--model", s.agent_model]
        env = dict(os.environ)
        key = os.environ.get(s.agent_api_key_env)
        if key:
            env["ACME_API_KEY"] = key           # 値は環境変数でだけ渡す（argv に入れない）
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  cwd=cwd, timeout=s.agent_timeout_seconds, env=env)
        except subprocess.TimeoutExpired as exc:
            raise AdapterTimeoutError(f"acme exceeded {s.agent_timeout_seconds}s") from exc
        final = ...  # proc.stdout から最終メッセージを取り出す
        return RunOutcome(runtime=self.name, returncode=proc.returncode,
                          final_message=final, stdout=proc.stdout, stderr=proc.stderr,
                          audit={"turns": ...})  # 本文は入れない
```

守るべき契約（`base.py` の docstring）:

- API キーの値を argv・prompt・ログ・`RunOutcome.audit` に入れない。
- `READ_ONLY` ではファイルを変更できないようにする（CLI の権限設定で）。`WRITE` の隔離は Pod 境界が担う。
- CLI 自身の Web アクセス機能は無効にする（Web は検索 CLI と egress レベル経由のみ）。
- `output_schema` が渡されたら、`final_message` はそのスキーマに従う JSON 文字列にする（CLI に構造化出力機能がなければ prompt に埋め込み、出力から抽出する。`claude_code.py` の `extract_json_object` を参考に）。
- タイムアウトは `AdapterTimeoutError`、起動失敗は `AdapterError`。非ゼロ終了は例外にせず `RunOutcome` で返す。

### 2. 登録する

```python
# src/agent_platform/config.py
AGENT_RUNTIMES = {
    "codex": ("codex", "api.openai.com"),
    "claude-code": ("claude", "api.anthropic.com"),
    "acme": ("acme-agent", "api.acme.example"),   # 既定のバイナリ名と API ホスト
}

# src/agent_platform/agent_runtime/__init__.py
RUNTIMES = {..., "acme": AcmeRuntime}
```

API ホストは、`egress_level = allowlist` のときプロキシを経由させない（`NO_PROXY`）ために使われます。

Terraform の `agent_runtime` 変数の validation（`infra/platform/variables.tf`）にも名前を追加します。

### 3. イメージに CLI を入れる

`deploy/worker/Dockerfile` に、バージョンとハッシュを固定したインストール手順をビルド引数付きで追加し、`scripts/build-push.sh` から渡せるようにします。

### 4. テストを書く

`tests/contract/test_claude_code_runtime.py` と同じく、引数と環境変数を記録する fake バイナリを置いて、READ_ONLY / WRITE の権限、キーの受け渡し、エラー・タイムアウトの扱いを検証します。

---

## Web 検索 API を追加する

### 1. プロバイダを実装する

`src/agent_platform/search/acme.py`（`search/brave.py` が実例）:

```python
from agent_platform.search.base import SearchHit, SearchQuery, SearchResult
from agent_platform.search.http import check_endpoint, get_json

ENDPOINT = "https://api.acme-search.example/v1/search"


class AcmeSearchProvider:
    name = "acme"

    def __init__(self, api_key: str) -> None:
        check_endpoint(ENDPOINT, ("api.acme-search.example",))   # https + 接続先ホストを固定
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def search(self, query: SearchQuery) -> SearchResult:
        payload = get_json(ENDPOINT, {"q": query.query}, headers=self._headers)  # 1MiB 上限
        hits = [SearchHit(title=i["title"][:512], url=i["url"][:2048], snippet=i["text"][:2048])
                for i in payload.get("items", [])]
        return SearchResult(query=query.query, hits=hits, provider=self.name)
```

HTTP は必ず `search/http.py` の `check_endpoint` / `get_json` を使ってください（https のみ、接続先ホストの固定、応答サイズ上限、エラーに URL やヘッダを含めない）。

### 2. 登録する

```python
# src/agent_platform/search/upstreams.py
def _acme(settings: UpstreamSettings) -> SearchProvider:
    from agent_platform.search.acme import AcmeSearchProvider
    if not settings.api_key:
        raise UpstreamConfigError("the acme upstream needs SEARCH_GATEWAY_API_KEY")
    return AcmeSearchProvider(settings.api_key)

UPSTREAMS = {..., "acme": Upstream(_acme)}   # エンドポイントを設定で受けるなら uses_url=True
```

### 3. デプロイ

イメージをビルドし、`search_provider = "acme"`、`search_api_key` を設定して `terraform apply`。Terraform・NetworkPolicy・ワーカーの変更は不要です（実 API のプロバイダには Gateway Pod から公開アドレスの 443 が許可され、宛先ホストはプロバイダ自身が固定します）。

テストは `tests/contract/test_search_upstreams.py` のように `urlopen` を差し替えてネットワークなしで書きます。

---

## クラウド / クラスタを追加する

`infra/platform` は kubeconfig しか前提にしていないため、**クラスタさえ用意できればどのクラウドでも動きます**。新しい構築例を足す場合は `infra/clusters/<name>/` に置き、次を満たすようにしてください。

- NetworkPolicy を強制する CNI（マネージド K8s では Calico / Cilium の有効化が必要な場合があります。例: EKS の VPC CNI は network policy の有効化設定、GKE は Dataplane V2 / network policy）。
- Pod Security Admission が有効（Kubernetes 1.25+）。
- 既定の StorageClass（`ReadWriteOnce`）。
- webhook を公開する手段（LoadBalancer / Ingress / ルータのポート転送）。
- コンテナレジストリ（任意。プライベートなら `registry_*` 変数で pull secret を作成）。
- クラスタ構築用のコードは Kubernetes リソースを作らない（それは `infra/platform` の責務）。出力として、kubeconfig の取得方法・レジストリのホスト・webhook URL を返すと使いやすくなります。

README には「クラスタ作成 → kubeconfig 取得 → `infra/platform` の apply に必要な変数」の順で手順を書いてください。例: [infra/clusters/sakura-k3s/README.md](../infra/clusters/sakura-k3s/README.md)。

S3 互換ストア（AWS S3、GCS の XML API、さくらのクラウド オブジェクトストレージ、MinIO など）は `s3` 変数で接続できます。
