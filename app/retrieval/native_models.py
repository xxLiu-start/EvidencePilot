"""Native Transformers inference for reviewed BGE encoder bundles.

Only dense CLS embeddings and binary cross-encoder scores are supported.
Sparse and ColBERT weights are deliberately not loaded.
"""
from pathlib import Path


class NativeEncoder:
    def __init__(self, path, device, fp16, rerank=False):
        import torch
        from transformers import AutoTokenizer, AutoModel, AutoModelForSequenceClassification
        path = str(Path(path).resolve(strict=True))
        if not Path(path).is_dir():
            raise ValueError("Model must be an existing local directory")
        self.torch = torch
        self.device = device
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)  # nosec B615 - existing local directory only
        cls = AutoModelForSequenceClassification if rerank else AutoModel
        self.model = cls.from_pretrained(path, local_files_only=True, trust_remote_code=False,
                                         use_safetensors=True).to(device).eval()
        if fp16:
            self.model.half()
        if rerank and self.model.config.num_labels != 1:
            raise ValueError("Expected a single-logit cross encoder")
        if not rerank and self.model.config.hidden_size != 1024:
            raise ValueError("Expected 1024-dimensional dense encoder")

    def encode(self, texts, batch_size, max_length, **kwargs):
        inputs = self.tokenizer(texts, padding=True, truncation=True, max_length=max_length,
                                return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            vectors = self.model(**inputs).last_hidden_state[:, 0].float()
            vectors = self.torch.nn.functional.normalize(vectors, dim=-1)
        return {"dense_vecs": vectors.cpu().numpy()}

    def compute_score(self, pairs, batch_size, max_length, normalize=True):
        inputs = self.tokenizer([p[0] for p in pairs], [p[1] for p in pairs],
                                padding=True, truncation=True, max_length=max_length,
                                return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            logits = self.model(**inputs).logits.float().reshape(-1)
            if normalize:
                logits = logits.sigmoid()
        return logits.cpu().tolist()
