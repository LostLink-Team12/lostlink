"""Real OpenCLIP inference; test vectors are injected only by tests."""

import base64
import binascii
import hashlib
import io
import math
import threading
import warnings
from pathlib import Path

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000


class EmbeddingError(Exception):
    def __init__(self, code="model_failure", retryable=True):
        self.code, self.retryable = code, retryable


def normalize(vector):
    try:
        values = [float(x) for x in vector]
    except (ValueError, TypeError, OverflowError) as exc:
        raise EmbeddingError() from exc
    if len(values) != 512 or any(not math.isfinite(x) for x in values):
        raise EmbeddingError()
    length = math.sqrt(sum(x * x for x in values))
    if not math.isfinite(length) or length < 1e-12:
        raise EmbeddingError()
    return [x / length for x in values]


def vector_literal(vector):
    return "[" + ",".join(str(x) for x in normalize(vector)) + "]"


def decode_photo(value):
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise EmbeddingError("invalid_image", False) from exc
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise EmbeddingError("invalid_image", False)
    return content


class ClipEncoder:
    def __init__(self, checkpoint, sha256, device="cpu"):
        self.checkpoint, self.sha256, self.device = checkpoint, sha256, device
        self.model = None
        self.lock = threading.Lock()
        self.model_version = f"clip-vit-b32-{sha256[:16]}" if sha256 else "unconfigured"

    def ready(self):
        with self.lock:
            self._load()

    def _load(self):
        if self.model is not None:
            return
        try:
            path = Path(self.checkpoint or "")
            if not path.is_file() or not self.sha256:
                raise ValueError("Checkpoint is required")
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != self.sha256:
                    raise ValueError("Checkpoint digest does not match")
            import open_clip
            import torch

            model, _, transform = open_clip.create_model_and_transforms(
                "ViT-B-32",
                pretrained=str(path),
                device=self.device,
                force_quick_gelu=True,
                # Load only a trusted operator-configured checkpoint with a verified SHA256.
                # The official OpenAI artifact is a TorchScript archive.
                weights_only=False,
            )
            model.eval()
            self.torch, self.transform = torch, transform
            self.tokenizer = open_clip.get_tokenizer("ViT-B-32")
            self.model = model
        except Exception as exc:
            raise EmbeddingError() from exc

    def encode(self, text=None, photo=None):
        with self.lock:
            self._load()
            try:
                vectors = []
                with self.torch.inference_mode():
                    if text:
                        encoded = self.model.encode_text(
                            self.tokenizer([text]).to(self.device), normalize=True
                        )
                        vectors.append(encoded[0].float().cpu().tolist())
                    if photo:
                        tensor = self.photo_tensor(photo)
                        encoded = self.model.encode_image(tensor.to(self.device), normalize=True)
                        vectors.append(encoded[0].float().cpu().tolist())
                if not vectors:
                    raise EmbeddingError("invalid_query", False)
                # Equal-weight normalized fusion keeps both modalities in one CLIP space.
                return normalize([sum(xs) for xs in zip(*vectors, strict=True)])
            except EmbeddingError:
                raise
            except Exception as exc:
                raise EmbeddingError() from exc

    def photo_tensor(self, content):
        from PIL import Image

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    if (
                        image.format not in {"JPEG", "PNG", "WEBP"}
                        or image.width * image.height > MAX_IMAGE_PIXELS
                        or getattr(image, "n_frames", 1) != 1
                    ):
                        raise EmbeddingError("invalid_image", False)
                    image.load()
                    return self.transform(image.convert("RGB")).unsqueeze(0)
        except (OSError, ValueError, Image.DecompressionBombError, Warning) as exc:
            raise EmbeddingError("invalid_image", False) from exc
