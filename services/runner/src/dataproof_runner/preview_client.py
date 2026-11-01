"""An API client bound to one preview: requests go through its trusted edge, with its token.

The client can only append a local path (validated as ``ApiRoute`` in the contract) to the
preview's own ``/api`` prefix, so an API check cannot reach anything but that preview.
"""

import json
import urllib.error
import urllib.request
from typing import Any

from dataproof_runner.backend import PreviewAccess
from dataproof_verifier import ApiRequestError

_TIMEOUT_SECONDS = 30
_MAX_BODY = 8 * 1024 * 1024


class PreviewApi:
    def __init__(self, access: PreviewAccess) -> None:
        self._access = access

    def get(self, path: str) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise ApiRequestError(f"not a local path: {path!r}")
        request = urllib.request.Request(  # noqa: S310 - loopback URL of a preview we started
            f"{self._access.url}/api{path}",
            headers={
                "Cookie": f"dataproof_preview={self._access.token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310
                body = response.read(_MAX_BODY + 1)
        except urllib.error.HTTPError as error:
            raise ApiRequestError(f"HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise ApiRequestError(str(error)) from error
        if len(body) > _MAX_BODY:
            raise ApiRequestError("response body is too large")
        try:
            return json.loads(body)
        except ValueError as error:
            raise ApiRequestError("response is not JSON") from error
