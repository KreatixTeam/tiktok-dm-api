"""Small synchronous integration client. Session credentials stay inside the bridge."""

import os
import uuid

import httpx


class TikTokDM:
    def __init__(self, base_url: str, token: str):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )

    def close(self):
        self.http.close()

    def conversations(self, after: str = "") -> dict:
        response = self.http.get("/v1/conversations", params={"after": after})
        response.raise_for_status()
        return response.json()

    def messages(self, conversation_id: str, after: int = 0) -> dict:
        response = self.http.get(
            f"/v1/conversations/{conversation_id}/messages", params={"after": after}
        )
        response.raise_for_status()
        return response.json()

    def reply(self, conversation_id: str, text: str, idempotency_key: str) -> dict:
        response = self.http.post(
            "/v1/messages",
            headers={"Idempotency-Key": idempotency_key},
            json={"conversation_id": conversation_id, "text": text},
        )
        response.raise_for_status()
        return response.json()

    def send_status(self, send_id: str) -> dict:
        response = self.http.get(f"/v1/sends/{send_id}")
        response.raise_for_status()
        return response.json()


if __name__ == "__main__":
    client = TikTokDM(os.getenv("TTDM_URL", "http://127.0.0.1:8000"), os.environ["TTDM_API_TOKEN"])
    try:
        print(client.conversations())
        # Persist an operation ID before calling reply. Reuse it after HTTP timeouts.
        operation_id = str(uuid.uuid4())
    finally:
        client.close()
