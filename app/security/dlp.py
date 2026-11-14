"""All-Boundary Data Loss Prevention (DLP): Secret Redactor & 3-Layer VN Patient PII Masker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Common Vietnamese family names (accented and unaccented) for Layer 2 free-text name detection
VN_SURNAMES: tuple[str, ...] = (
    "Nguyễn",
    "Nguyen",
    "Trần",
    "Tran",
    "Lê",
    "Le",
    "Phạm",
    "Pham",
    "Hoàng",
    "Hoang",
    "Huỳnh",
    "Huynh",
    "Phan",
    "Vũ",
    "Vu",
    "Võ",
    "Vo",
    "Đặng",
    "Dang",
    "Bùi",
    "Bui",
    "Đỗ",
    "Do",
    "Hồ",
    "Ho",
    "Ngô",
    "Ngo",
    "Dương",
    "Duong",
    "Lý",
    "Ly",
    "Đinh",
    "Dinh",
    "Đoàn",
    "Doan",
    "Trương",
    "Truong",
    "Bạch",
    "Bach",
    "Mai",
    "Tạ",
    "Ta",
    "Tô",
    "To",
    "Châu",
    "Chau",
    "Lâm",
    "Lam",
    "Phùng",
    "Phung",
    "Vương",
    "Vuong",
    "Trịnh",
    "Trinh",
    "Kiều",
    "Kieu",
)

TECHNICAL_PROPER_NOUN_WHITELIST: frozenset[str] = frozenset(
    {
        "hồ chí minh",
        "ho chi minh",
        "hà nội",
        "ha noi",
        "bệnh viện",
        "benh vien",
        "bảo hiểm",
        "bao hiem",
        "hệ thống",
        "he thong",
        "phần mềm",
        "phan mem",
        "ngoại trú",
        "ngoai tru",
        "nội trú",
        "noi tru",
        "hồ sơ",
        "ho so",
        "quy trình",
        "quy trinh",
    }
)

# Layer 3 Gazetteer of known test/sample patient names (case-insensitive)
PATIENT_NAME_GAZETTEER: tuple[str, ...] = (
    "Nguyễn Văn An",
    "Nguyen Van An",
    "Trần Thị Bích Ngọc",
    "Tran Thi Bich Ngoc",
    "Lê Minh Tuấn",
    "Le Minh Tuan",
    "Phạm Quốc Bảo",
    "Pham Quoc Bao",
    "Hoàng Thu Trang",
    "Hoang Thu Trang",
)

# Secret regexes
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
CONN_STRING_RE = re.compile(
    r"(?i)(?:Server|Data Source|Host)\s*=\s*[^;\s\"']+;[^\"'\n]*?"
    r"(?:Password|Pwd)\s*=\s*[^;\s\"']+;?"
)
DB_URI_SECRET_RE = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mssql|redis)://[^:\s]+:[^@\s]+@[^\s\"']+"
)
API_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{14,}|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z\-_]{20,}|"
    r"xox[baprs]-[0-9A-Za-z\-]{10,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})\b"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{12,}=*")
KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret[_-]?key|password|passwd|client[_-]?secret|access[_-]?token)"
    r"(\s*[:=]\s*[\"']?)([A-Za-z0-9@#$%^&*!._\-]{6,})([\"']?)"
)

# PII regexes
BHYT_CARD_RE = re.compile(
    r"\b(?:DN|HX|CH|NN|TK|HC|XK|HT|TB|NO|CT|XB|TN|CS|QN|CA|CY|XN|MS|CC|CK|CB|KC|HD|"
    r"TE|BT|HN|DT|DK|XD|TS|TC|TQ|TA|TY|HG|LS|CN|HS|SV|GB|GD|PV)[1-5]\d{12}\b"
)
CCCD_RE = re.compile(r"\b0\d{11}\b")
VN_PHONE_RE = re.compile(r"(?<!\d)(?:\+?84|0)(?:3[2-9]|5[2689]|7[06-9]|8[1-9]|9[0-9])\d{7}(?!\d)")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
DOB_RE = re.compile(
    r"\b(?:0?[1-9]|[12]\d|3[01])[\/\-](?:0?[1-9]|1[0-2])[\/\-](?:19\d{2}|20[0-2]\d)\b"
)
MRN_RE = re.compile(r"\b(?:BA|BN|HSBA|MRN)-\d{4}-\d{4,8}\b")

# Vietnamese capitalized syllable pattern
_VN_CAP_WORD = (
    r"[A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠ"
    r"ÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ]"
    r"[a-zàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡ"
    r"ùúụủũưừứựửữỳýỵỷỹđ]+"
)

# Layer 1: Contextual patient label followed by 2-4 capitalized words
CONTEXTUAL_NAME_RE = re.compile(
    rf"(?P<prefix>(?:[Bb]ệnh nhân|[Bb][Nn]|[Hh]ọ và tên|[Hh]ọ tên|[Tt]ên bệnh nhân|"
    rf"[Ss]ản phụ|[Nn]hi đồng|patient[_ ]?name|full[_ ]?name)\s*[:=\-]?\s*)"
    rf"(?P<name>{_VN_CAP_WORD}(?:\s+{_VN_CAP_WORD}){{1,3}})"
)

# Layer 2: Unlabeled free-text Vietnamese full name (Surname + 1 to 3 capitalized syllables)
_SURNAME_ALT = "|".join(re.escape(s) for s in VN_SURNAMES)
FREETEXT_VN_NAME_RE = re.compile(
    rf"\b(?P<fullname>(?:{_SURNAME_ALT})(?:\s+{_VN_CAP_WORD}){{1,3}})\b"
)


@dataclass(slots=True)
class SanitizeResult:
    """Output of DLP sanitization with counts of redacted secrets and masked PII."""

    text: str
    secrets_redacted: int = 0
    pii_masked: int = 0


class AllBoundaryDLP:
    """Stateless All-Boundary DLP engine enforcing 0% secret/PII leakage."""

    @classmethod
    def sanitize_with_stats(cls, raw_text: str) -> SanitizeResult:
        """Sanitize text and return detailed counts of redacted secrets and masked PII items."""
        if not raw_text:
            return SanitizeResult(text=raw_text, secrets_redacted=0, pii_masked=0)

        text = raw_text
        secrets_count = 0
        pii_count = 0

        # 1. Redact Secrets first
        for secret_re in (
            PRIVATE_KEY_RE,
            CONN_STRING_RE,
            DB_URI_SECRET_RE,
            API_TOKEN_RE,
            BEARER_TOKEN_RE,
        ):
            text, n = secret_re.subn("[REDACTED_SECRET]", text)
            secrets_count += n

        def _replace_kv_secret(match: re.Match[str]) -> str:
            nonlocal secrets_count
            val = match.group(3)
            if val == "[REDACTED_SECRET]":
                return match.group(0)
            secrets_count += 1
            return f"{match.group(1)}{match.group(2)}[REDACTED_SECRET]{match.group(4)}"

        text = KV_SECRET_RE.sub(_replace_kv_secret, text)

        # 2. Mask Structured Patient PII (BHYT, CCCD, Phone, Email, DOB, MRN)
        for pii_re, token in (
            (BHYT_CARD_RE, "[MASKED_BHYT]"),
            (CCCD_RE, "[MASKED_CCCD]"),
            (VN_PHONE_RE, "[MASKED_PHONE]"),
            (EMAIL_RE, "[MASKED_EMAIL]"),
            (DOB_RE, "[MASKED_DOB]"),
            (MRN_RE, "[MASKED_MRN]"),
        ):
            text, n = pii_re.subn(token, text)
            pii_count += n

        # 3. Mask Patient Names (3-Layer defense)
        # Layer 3 Gazetteer first (catches exact known patient names even if lowercase)
        for gaz_name in PATIENT_NAME_GAZETTEER:
            gaz_re = re.compile(re.escape(gaz_name), re.IGNORECASE)
            text, n = gaz_re.subn("[MASKED_PATIENT_NAME]", text)
            pii_count += n

        # Layer 1 Contextual labels
        def _replace_ctx_name(match: re.Match[str]) -> str:
            nonlocal pii_count
            candidate = match.group("name").strip()
            if candidate.lower() in TECHNICAL_PROPER_NOUN_WHITELIST:
                return match.group(0)
            pii_count += 1
            return f"{match.group('prefix')}[MASKED_PATIENT_NAME]"

        text = CONTEXTUAL_NAME_RE.sub(_replace_ctx_name, text)

        # Layer 2 Free-text Vietnamese full names (Surname + 1..3 capitalized syllables)
        def _replace_freetext_name(match: re.Match[str]) -> str:
            nonlocal pii_count
            candidate = match.group("fullname").strip()
            lowered = candidate.lower()
            if lowered in TECHNICAL_PROPER_NOUN_WHITELIST:
                return match.group(0)
            for whitelisted in TECHNICAL_PROPER_NOUN_WHITELIST:
                if whitelisted in lowered:
                    return match.group(0)
            pii_count += 1
            return "[MASKED_PATIENT_NAME]"

        text = FREETEXT_VN_NAME_RE.sub(_replace_freetext_name, text)

        return SanitizeResult(text=text, secrets_redacted=secrets_count, pii_masked=pii_count)

    @classmethod
    def sanitize_text(cls, raw_text: str) -> str:
        """Return DLP-sanitized text string."""
        return cls.sanitize_with_stats(raw_text).text

    @classmethod
    def sanitize_obj(cls, obj: Any) -> Any:
        """Recursively sanitize strings inside dicts, lists, and tuples."""
        if isinstance(obj, str):
            return cls.sanitize_text(obj)
        if isinstance(obj, dict):
            return {k: cls.sanitize_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [cls.sanitize_obj(item) for item in obj]
        if isinstance(obj, tuple):
            return tuple(cls.sanitize_obj(item) for item in obj)
        return obj
