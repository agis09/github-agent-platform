"""python -m agent_platform.search_gateway — run the gateway (Deployment entry).

Env: SEARCH_GATEWAY_UPSTREAM (required; see search/upstreams.py for the
provider names and their SEARCH_GATEWAY_UPSTREAM_URL / SEARCH_GATEWAY_API_KEY
settings), SEARCH_GATEWAY_BIND (default 127.0.0.1; the Pod passes 0.0.0.0),
SEARCH_GATEWAY_PORT (default 8080).
"""

import os
import sys

from agent_platform.errors import AgentPlatformError
from agent_platform.search_gateway.app import gateway_from_environ, serve


def main() -> int:
    try:
        gateway = gateway_from_environ()
        port = int(os.environ.get("SEARCH_GATEWAY_PORT", "8080"))
    except (AgentPlatformError, ValueError) as exc:
        print(f"search-gateway: configuration error: {exc}", file=sys.stderr)
        return 2
    serve(gateway, port=port, bind=os.environ.get("SEARCH_GATEWAY_BIND", "127.0.0.1"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
