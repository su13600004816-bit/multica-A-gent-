#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DEFAULT_ENV_FILE = "/home/fleet/agent-control-plane/.control/.env"
PROVIDERS = {
    "qwen": {
        "key": "DASHSCOPE_API_KEY",
        "base": "DASHSCOPE_BASE_URL",
        "model": "QWEN_MODEL",
        "vision_model": "QWEN_VL_MODEL",
        "default_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "default_vision_model": "qwen-vl-max",
    },
    "deepseek": {
        "key": "DEEPSEEK_API_KEY",
        "base": "DEEPSEEK_BASE_URL",
        "model": "DEEPSEEK_MODEL",
        "vision_model": "",
        "default_base": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "default_vision_model": "",
    },
}


def load_env(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        return


def provider_config(provider: str) -> dict[str, str]:
    spec = PROVIDERS[provider]
    key = os.environ.get(spec["key"], "").strip()
    base = os.environ.get(spec["base"], spec["default_base"]).strip()
    model = os.environ.get(spec["model"], spec["default_model"]).strip()
    vision_model = os.environ.get(spec["vision_model"], spec["default_vision_model"]).strip() if spec["vision_model"] else ""
    return {"provider": provider, "key": key, "base": base, "model": model, "vision_model": vision_model}


def image_to_url(image: str) -> str:
    image = image.strip()
    if image.startswith(("http://", "https://", "data:")):
        return image
    if not os.path.exists(image):
        raise FileNotFoundError(f"image file not found: {image}")
    mime = mimetypes.guess_type(image)[0] or "image/jpeg"
    with open(image, "rb") as handle:
        data = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{mime};base64,{data}"


def call_chat(config: dict[str, str], system: str, user: str, max_tokens: int, timeout: int, images: list[str] | None = None) -> str:
    if not config["key"]:
        raise RuntimeError(f"missing {PROVIDERS[config['provider']]['key']}")
    images = images or []
    if images and config["provider"] != "qwen":
        raise RuntimeError(f"{config['provider']} local API does not support image input")
    model = config["vision_model"] if images else config["model"]
    if images and not model:
        raise RuntimeError("vision model is not configured")
    user_content: str | list[dict[str, Any]]
    if images:
        user_content = [{"type": "text", "text": user}]
        for image in images:
            user_content.append({"type": "image_url", "image_url": {"url": image_to_url(str(image))}})
    else:
        user_content = user
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system or "You are a concise assistant."},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max(1, min(int(max_tokens or 1200), 4000)),
    }
    req = urllib.request.Request(
        config["base"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + config["key"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=max(1, min(int(timeout or 90), 180))) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"]).strip()


def openai_messages_to_prompt(messages: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
    system_parts: list[str] = []
    user_parts: list[str] = []
    images: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        parts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    image_url = item.get("image_url")
                    if isinstance(image_url, dict):
                        url = str(image_url.get("url") or "").strip()
                    else:
                        url = str(image_url or "").strip()
                    if url:
                        images.append(url)
        else:
            parts.append(str(content or ""))
        text = "\n".join(part for part in parts if part).strip()
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        else:
            user_parts.append(f"{role}: {text}" if role not in {"user", "assistant"} else text)
    return "\n\n".join(system_parts), "\n\n".join(user_parts), images


class Handler(BaseHTTPRequestHandler):
    server_version = "pl1-model-api/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "remote": self.client_address[0],
            "message": fmt % args,
        }, ensure_ascii=False), flush=True)

    def send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @property
    def config(self) -> dict[str, str]:
        return self.server.config  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        if self.path in {"/v1/models", "/models"}:
            cfg = self.config
            models = [cfg["model"]]
            if cfg.get("vision_model"):
                models.append(cfg["vision_model"])
            self.send_json(200, {
                "object": "list",
                "data": [{"id": model, "object": "model", "owned_by": cfg["provider"]} for model in models if model],
            })
            return
        if self.path not in {"/", "/health"}:
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        cfg = self.config
        self.send_json(200, {
            "ok": bool(cfg["key"]),
            "provider": cfg["provider"],
            "model": cfg["model"],
            "vision_model": cfg.get("vision_model") or None,
            "base_url": cfg["base"],
            "has_api_key": bool(cfg["key"]),
        })

    def do_POST(self) -> None:
        if self.path in {"/v1/chat/completions", "/chat/completions"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(min(length, 1_000_000))
                data = json.loads(body.decode("utf-8") or "{}")
                messages = data.get("messages") or []
                if not isinstance(messages, list):
                    self.send_json(400, {"error": {"message": "messages must be a list", "type": "invalid_request_error"}})
                    return
                system, user, images = openai_messages_to_prompt(messages)
                if not user:
                    self.send_json(400, {"error": {"message": "missing user message", "type": "invalid_request_error"}})
                    return
                content = call_chat(
                    self.config,
                    system,
                    user,
                    int(data.get("max_tokens") or 1200),
                    int(data.get("timeout") or 90),
                    images,
                )
                created = int(time.time())
                self.send_json(200, {
                    "id": "chatcmpl-" + uuid.uuid4().hex,
                    "object": "chat.completion",
                    "created": created,
                    "model": self.config["vision_model"] if images else self.config["model"],
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                })
            except urllib.error.HTTPError as err:
                detail = err.read().decode("utf-8", errors="replace")[:800]
                self.send_json(502, {"error": {"message": detail, "type": "provider_http_error", "code": err.code}})
            except Exception as err:
                self.send_json(500, {"error": {"message": str(err)[:800], "type": type(err).__name__}})
            return
        if self.path != "/chat":
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(min(length, 1_000_000))
            data = json.loads(body.decode("utf-8") or "{}")
            user = str(data.get("user") or data.get("prompt") or "").strip()
            if not user:
                self.send_json(400, {"ok": False, "error": "missing user/prompt"})
                return
            content = call_chat(
                self.config,
                str(data.get("system") or ""),
                user,
                int(data.get("max_tokens") or 1200),
                int(data.get("timeout") or 90),
                list(data.get("images") or []),
            )
            self.send_json(200, {
                "ok": True,
                "provider": self.config["provider"],
                "model": self.config["vision_model"] if data.get("images") else self.config["model"],
                "content": content,
            })
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")[:800]
            self.send_json(502, {"ok": False, "error": "provider_http_error", "status": err.code, "detail": detail})
        except Exception as err:
            self.send_json(500, {"ok": False, "error": type(err).__name__, "detail": str(err)[:800]})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    args = parser.parse_args()

    load_env(args.env_file)
    config = provider_config(args.provider)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.config = config  # type: ignore[attr-defined]
    print(json.dumps({
        "event": "started",
        "provider": config["provider"],
        "host": args.host,
        "port": args.port,
        "model": config["model"],
        "vision_model": config.get("vision_model") or None,
        "base_url": config["base"],
        "has_api_key": bool(config["key"]),
    }, ensure_ascii=False), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
