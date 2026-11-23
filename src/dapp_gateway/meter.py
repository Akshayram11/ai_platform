"""Pull token counts out of Anthropic and OpenAI responses, streaming or not."""

from __future__ import annotations

import json
from typing import Any


class Meter:
    """Feed it the response (whole JSON, or SSE bytes chunk by chunk); read .usage at the end."""

    def __init__(self, model: str = ""):
        self.model = model
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0
        self._input_includes_cache = False
        self._buf = b""

    @property
    def usage(self) -> dict[str, Any]:
        # OpenAI's prompt/input token count already contains the cached tokens, Anthropic's does not. Report
        # input as the uncached part so cost = input*price + cache_read*cache_price never charges a token twice.
        uncached = max(self.input - self.cache_read, 0) if self._input_includes_cache else self.input
        return {
            "model": self.model,
            "input_tokens": uncached,
            "output_tokens": self.output,
            "cache_read_tokens": self.cache_read,
            "cache_write_tokens": self.cache_write,
        }

    def feed_json(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("model"), str) and obj["model"]:
            self.model = obj["model"]
        # Anthropic stream: message_start carries {"message": {...usage}}; OpenAI Responses: {"response": {...}}
        for nested in ("message", "response"):
            if isinstance(obj.get(nested), dict):
                self.feed_json(obj[nested])
        u = obj.get("usage")
        if not isinstance(u, dict):
            return
        # Anthropic
        self.input = max(self.input, int(u.get("input_tokens") or 0))
        self.output = max(self.output, int(u.get("output_tokens") or 0))
        self.cache_read = max(self.cache_read, int(u.get("cache_read_input_tokens") or 0))
        self.cache_write = max(self.cache_write, int(u.get("cache_creation_input_tokens") or 0))
        # OpenAI chat/embeddings
        self.input = max(self.input, int(u.get("prompt_tokens") or 0))
        self.output = max(self.output, int(u.get("completion_tokens") or 0))
        for details_key in ("prompt_tokens_details", "input_tokens_details"):
            d = u.get(details_key)
            if isinstance(d, dict):
                self._input_includes_cache = True
                self.cache_read = max(self.cache_read, int(d.get("cached_tokens") or 0))

    def feed_sse(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                self.feed_json(json.loads(payload))
            except ValueError:
                continue
