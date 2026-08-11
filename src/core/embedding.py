"""In-process embedding engine — BGE-small-en-v1.5 via ONNX Runtime.

Replaces the legacy Docker embedding-service (:8896).
Same model, same dimensions (384), same normalization — zero behavior change.
"""

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384


class EmbeddingEngine:
    """In-process embedding using ONNX Runtime + HuggingFace tokenizer."""

    def __init__(self, model_dir: str = "data/models/bge-small-en-v1.5"):
        self.model_dir = Path(model_dir)
        self.dimensions = DIMENSIONS
        self._tokenizer = None
        self._session = None

    def _ensure_loaded(self) -> None:
        """Lazy-load model on first use."""
        if self._session is not None:
            return

        import time

        import onnxruntime as ort
        from transformers import AutoTokenizer

        onnx_path = self._find_onnx()
        if onnx_path is None:
            self._download_model()
            onnx_path = self._find_onnx()
            if onnx_path is None:
                raise RuntimeError(f"model.onnx not found in {self.model_dir} after download")

        logger.info(f"Loading {MODEL_NAME} (ONNX) from {onnx_path}...")
        start = time.time()

        self._tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 2
        opts.intra_op_num_threads = 2
        self._session = ort.InferenceSession(
            str(onnx_path), opts, providers=["CPUExecutionProvider"]
        )

        logger.info(f"Embedding model loaded in {time.time() - start:.1f}s")

    def _find_onnx(self) -> Path | None:
        """Find model.onnx — supports flat and onnx/ subdirectory layouts."""
        for candidate in [
            self.model_dir / "onnx" / "model.onnx",
            self.model_dir / "model.onnx",
        ]:
            if candidate.is_file():
                return candidate
        return None

    def _download_model(self) -> None:
        """Download the model on first run if not present."""
        logger.info(f"Model not found at {self.model_dir}, downloading {MODEL_NAME}...")
        from optimum.onnxruntime import ORTModelForFeatureExtraction
        from transformers import AutoTokenizer

        self.model_dir.parent.mkdir(parents=True, exist_ok=True)
        tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        tokenizer.save_pretrained(str(self.model_dir))
        model = ORTModelForFeatureExtraction.from_pretrained(MODEL_NAME, export=True)
        model.save_pretrained(str(self.model_dir))
        logger.info(f"Model downloaded to {self.model_dir}")

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed texts, returns (N, 384) normalized float32 array."""
        self._ensure_loaded()

        encoded = self._tokenizer(
            texts, padding=True, truncation=True, max_length=512, return_tensors="np"
        )
        inputs = {
            "input_ids": encoded["input_ids"].astype(np.int64),
            "attention_mask": encoded["attention_mask"].astype(np.int64),
        }
        # Add token_type_ids if model expects it
        input_names = [i.name for i in self._session.get_inputs()]
        if "token_type_ids" in input_names:
            inputs["token_type_ids"] = encoded.get(
                "token_type_ids", np.zeros_like(encoded["input_ids"])
            ).astype(np.int64)

        outputs = self._session.run(None, inputs)

        # Mean pooling with attention mask
        token_embeddings = outputs[0]
        mask = encoded["attention_mask"][..., np.newaxis].astype(np.float32)
        summed = np.sum(token_embeddings * mask, axis=1)
        counts = np.clip(mask.sum(axis=1), 1e-9, None)
        embeddings = summed / counts

        # L2 normalize
        norms = np.clip(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-9, None)
        return embeddings / norms

    def embed_one(self, text: str) -> np.ndarray:
        """Embed a single text, returns (384,) normalized vector."""
        return self.embed([text])[0]

    def similarity(self, a: str, b: str) -> float:
        """Cosine similarity between two texts."""
        embs = self.embed([a, b])
        return float(np.dot(embs[0], embs[1]))

    @staticmethod
    def to_blob(vector: np.ndarray) -> bytes:
        """Convert float32 vector to bytes for SQLite BLOB storage."""
        return vector.astype(np.float32).tobytes()

    @staticmethod
    def from_blob(blob: bytes) -> np.ndarray:
        """Convert SQLite BLOB back to float32 vector."""
        return np.frombuffer(blob, dtype=np.float32)

    @staticmethod
    def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity between two normalized vectors."""
        return float(np.dot(a, b))
