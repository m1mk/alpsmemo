"""VOICEVOX ENGINE の HTTP API クライアント。

リクエストは常に1件ずつ順番に送る（並列にしない）。
"""

from __future__ import annotations

import requests


class VoicevoxError(Exception):
    pass


class VoicevoxNotRunning(VoicevoxError):
    pass


class VoicevoxClient:
    def __init__(self, url: str = "http://127.0.0.1:50021", timeout: float = 120):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, method: str, path: str, *, params=None, json=None) -> requests.Response:
        try:
            r = self.session.request(method, self.url + path, params=params, json=json, timeout=self.timeout)
        except requests.ConnectionError as e:
            raise VoicevoxNotRunning(
                f"VOICEVOXに接続できません（{self.url}）。\n→ VOICEVOXを起動してください。起動済みなら config.toml の [voicevox] url を確認してください。"
            ) from e
        except requests.Timeout as e:
            raise VoicevoxError(f"VOICEVOXの応答がタイムアウトしました（{path}）。\n→ VOICEVOXが動作中か確認し、もう一度実行してください。") from e
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise VoicevoxError(f"VOICEVOXがエラーを返しました（{path}、HTTP {r.status_code}）: {detail}")
        return r

    # --- 情報取得 ---
    def version(self) -> str:
        return self._request("GET", "/version").json()

    def speakers(self) -> list[dict]:
        return self._request("GET", "/speakers").json()

    def singers(self) -> list[dict]:
        return self._request("GET", "/singers").json()

    # --- 読み上げ ---
    def audio_query(self, text: str, speaker: int) -> dict:
        return self._request("POST", "/audio_query", params={"text": text, "speaker": speaker}).json()

    def accent_phrases_kana(self, kana: str, speaker: int) -> list[dict]:
        """AquesTalk風記法のカナからアクセント句を得る（モーラを確実に揃えたいとき用）。"""
        return self._request("POST", "/accent_phrases", params={"text": kana, "speaker": speaker, "is_kana": "true"}).json()

    def synthesis(self, query: dict, speaker: int) -> bytes:
        return self._request("POST", "/synthesis", params={"speaker": speaker}, json=query).content

    # --- 歌唱 ---
    def sing_frame_audio_query(self, notes: list[dict], speaker: int) -> dict:
        return self._request("POST", "/sing_frame_audio_query", params={"speaker": speaker}, json={"notes": notes}).json()

    def frame_synthesis(self, query: dict, speaker: int) -> bytes:
        return self._request("POST", "/frame_synthesis", params={"speaker": speaker}, json=query).content
