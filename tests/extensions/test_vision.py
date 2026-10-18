from minisweagent.extensions import vision as vision_module
from minisweagent.extensions.config import VisionConfig
from minisweagent.extensions.vision import VisionClient, build_vision_tool


def test_describe_sends_data_url(monkeypatch, tmp_path):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"role": "assistant", "content": "A red square"}}]}

    monkeypatch.setattr(vision_module.litellm, "completion", fake_completion)
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    client = VisionClient(
        VisionConfig(enabled=True, model="openai/vision-model", model_kwargs={"max_tokens": 64}),
        tmp_path,
    )
    result = client.describe("shot.png", "What is in the image?")
    assert result["returncode"] == 0
    assert "A red square" in result["output"]
    assert captured["model"] == "openai/vision-model"
    assert captured["max_tokens"] == 64
    content = captured["messages"][0]["content"]
    assert content[0]["text"] == "What is in the image?"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_tool_errors(monkeypatch, tmp_path):
    client = VisionClient(VisionConfig(enabled=True, model=None), tmp_path)
    tool = build_vision_tool(client)

    missing_model = tool.handler({"image": "x.png"})
    assert missing_model["returncode"] == -1
    assert "vision.model" in missing_model["exception_info"]

    missing_image = tool.handler({})
    assert missing_image["returncode"] == -1
    assert "image is required" in missing_image["exception_info"]


def test_vision_handles_model_failure(monkeypatch, tmp_path):
    def boom(**kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(vision_module.litellm, "completion", boom)
    image = tmp_path / "x.png"
    image.write_bytes(b"x")
    client = VisionClient(VisionConfig(enabled=True, model="openai/vision-model"), tmp_path)
    result = client.describe("x.png")
    assert result["returncode"] == -1
    assert "Vision model failed" in result["exception_info"]
