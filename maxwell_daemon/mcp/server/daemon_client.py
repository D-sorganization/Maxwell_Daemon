from typing import Any

import httpx


class DaemonClient:
    def __init__(self, host: str, port: int, auth_token: str | None = None) -> None:
        self.base_url = f"http://{host}:{port}/api/v1"
        self.headers = {"Content-Type": "application/json"}
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, headers=self.headers) as client:
            response = await client.request(method, endpoint, **kwargs)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                err_text = response.text
                raise RuntimeError(f"Daemon error {response.status_code}: {err_text}") from e
            if response.status_code == 204:
                return None
            return response.json()

    async def get(self, endpoint: str, **kwargs: Any) -> Any:
        return await self._request("GET", endpoint, **kwargs)

    async def post(self, endpoint: str, **kwargs: Any) -> Any:
        return await self._request("POST", endpoint, **kwargs)
