"""Deterministic 128-dim Embedding & CamelCase/Vietnamese Lexical Tokenizer for HIS-KB."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata

EMBEDDING_DIM = 128

CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
WORD_TOKEN_RE = re.compile(r"[a-zA-Z0-9à-ỹÀ-Ỹ_/\-]+")

# Domain concept clusters mapped to dedicated anchor dimensions (0..31)
DOMAIN_CONCEPT_CLUSTERS: tuple[tuple[str, ...], ...] = (
    ("bhyt", "baohiem", "thebhyt", "card", "cardno", "cardnumber", "dn4", "hx4", "gd4", "ch2"),
    ("registration", "tiepnhan", "tiepdon", "dangky", "register", "outpatient", "ngoaitru"),
    ("fiveyear", "5nam", "lientuc", "fiveyearcontinuous", "fiveyearcontinuousdate", "14040000"),
    ("copay", "dongchitra", "copayrate", "copayamount", "mien", "tylehuong", "quyenloi"),
    ("expired", "hethan", "cardvalidto", "cardvalidfrom", "15ngay", "thongtuyen"),
    ("emergency", "capcuu", "isemergency", "dungtuyen"),
    ("pharmacy", "duoc", "kedon", "prescription", "thuoc", "capphat", "dispense", "fifo"),
    ("interaction", "tuongtac", "chongchidinh", "contraindicated", "warfarin", "aspirin", "diung"),
    ("billing", "vienphi", "thanhtoan", "settlement", "doisoat", "calculatebhytcopayasync"),
    ("basesalary", "luongcoso", "2340000", "351000", "freecopaythreshold", "15%"),
    ("controller", "api", "endpoint", "post", "get", "route", "v1"),
    ("entity", "entities", "model", "outpatientvisit", "bhytcardinfo", "outpatientbhytpayload"),
    ("component", "react", "tsx", "form", "bhytregistrationform", "validatebhytcardformat"),
    ("hisApiClient", "submitoutpatientbhytregistration", "service", "frontend"),
)


def strip_vietnamese_accents(text: str) -> str:
    """Remove Vietnamese diacritics for accent-insensitive matching."""
    text = text.replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def extract_normalized_tokens(text: str) -> list[str]:
    """Extract lowercase tokens with CamelCase splitting and Vietnamese accent normalization."""
    if not text:
        return []
    raw_words = WORD_TOKEN_RE.findall(text)
    tokens: list[str] = []
    for word in raw_words:
        lower_w = word.lower().strip("_-/")
        if not lower_w:
            continue
        tokens.append(lower_w)
        unaccented = strip_vietnamese_accents(lower_w)
        if unaccented != lower_w:
            tokens.append(unaccented)

        # Split CamelCase / PascalCase / snake_case / slash paths
        sub_parts = re.split(r"[_/\-]+", word)
        for part in sub_parts:
            camel_pieces = CAMEL_SPLIT_RE.split(part)
            for piece in camel_pieces:
                p_low = strip_vietnamese_accents(piece.lower())
                if len(p_low) >= 2:
                    tokens.append(p_low)
    return tokens


def build_lexical_token_string(text: str) -> str:
    """Build space-separated token string for PostgreSQL FTS and lexical search."""
    return " ".join(extract_normalized_tokens(text))


class DeterministicEmbedding:
    """128-dim L2-normalized embedding combining HIS domain concept anchors and feature hashing."""

    @classmethod
    def encode(cls, text: str) -> list[float]:
        tokens = extract_normalized_tokens(text)
        vec = [0.0] * EMBEDDING_DIM
        if not tokens:
            vec[0] = 1.0
            return vec

        joined_unaccented = strip_vietnamese_accents(text.lower()).replace(" ", "")

        # 1. Domain concept anchor dimensions (dims 0..len(DOMAIN_CONCEPT_CLUSTERS)-1)
        for cluster_idx, keywords in enumerate(DOMAIN_CONCEPT_CLUSTERS):
            score = 0.0
            for kw in keywords:
                if kw in tokens or kw in joined_unaccented:
                    score += 1.5
            if score > 0:
                vec[cluster_idx] += score

        # 2. Feature hashing across dimensions 32..127 for unigrams and bigrams
        hash_space = EMBEDDING_DIM - 32
        for idx, tok in enumerate(tokens):
            digest = hashlib.md5(tok.encode("utf-8")).digest()
            dim_a = 32 + (int.from_bytes(digest[:4], "little") % hash_space)
            sign_a = 1.0 if (digest[4] & 1) == 0 else -1.0
            vec[dim_a] += sign_a * 1.0

            if idx + 1 < len(tokens):
                bigram = f"{tok}_{tokens[idx + 1]}"
                b_dig = hashlib.md5(bigram.encode("utf-8")).digest()
                dim_b = 32 + (int.from_bytes(b_dig[:4], "little") % hash_space)
                vec[dim_b] += 0.6

        # L2 normalize
        norm = math.sqrt(sum(v * v for v in vec))
        if norm <= 1e-12:
            vec[0] = 1.0
            return vec
        return [round(v / norm, 6) for v in vec]

    @staticmethod
    def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        """Compute cosine similarity between two normalized 128-d vectors."""
        return float(sum(a * b for a, b in zip(vec_a, vec_b, strict=False)))
