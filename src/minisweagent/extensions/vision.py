"""Vision proxy: send images to a separate multimodal model.

This is useful when the main model has no vision or weak vision: the agent calls
``Vision`` with a screenshot path or URL and receives text, so the image never
enters the main model's context.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

import litellm

from minisweagent.extensions.config import VisionConfig
from minisweagent.extensions.registry import Tool, err, ok
from minisweagent.models.utils.text_generation import chat_text

logger = logging.getLogger("extensions.vision")


class VisionClient:
    def __init__(self, config: VisionConfig, workspace: Path) -> None:
        self.config = config
        self.workspace = Path(workspace)

    def _resolve(self, image: str) -> str:
        if image.startswith(("http://", "https://", "data:")):
            return image
        path = Path(image).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    def describe(self, image: str, question: str | None = None) -> dict:
        if not self.config.model:
            return err("vision.model is not configured; set it to a vision-capable model name")
        try:
            image_url = self._resolve(image)
        except OSError as exc:
            return err(f"Could not read image {image}: {exc}")
        prompt = (question or self.config.question).strip()
        try:
            response = litellm.completion(
                model=self.config.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                **self.config.model_kwargs,
            )
            text = chat_text(response).strip()
        except Exception as exc:  # noqa: BLE001 - a vision failure must not kill the run
            logger.exception("vision model failed")
            return err(f"Vision model failed: {type(exc).__name__}: {exc}")
        if len(text) > self.config.max_output_chars:
            text = text[: self.config.max_output_chars] + "\n...[truncated]"
        return ok(text or "(vision model returned no text)")

    def describe_file(self, path: str, question: str | None = None) -> dict:
        return self.describe(path, question)


def build_vision_tool(client: VisionClient) -> Tool:
    def handler(args: dict) -> dict:
        image = str(args.get("image") or "").strip()
        if not image:
            return err("image is required (a local path, URL, or data: URL)")
        return client.describe(image, args.get("question"))

    return Tool(
        name="Vision",
        description=(
            "Ask a separate vision-capable model about an image (screenshot, photo, diagram). "
            "Use this when the main model cannot see images well. Returns a text description; "
            "the image itself is never added to the conversation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "Local image path (relative to the workspace), http(s) URL, or data: URL",
                },
                "question": {
                    "type": "string",
                    "description": "What to ask about the image. Defaults to a detailed description.",
                },
            },
            "required": ["image"],
        },
        handler=handler,
        read_only=True,
        metadata={"vision_model": client.config.model},
    )
