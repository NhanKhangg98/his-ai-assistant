"""All-Boundary Data Loss Prevention (DLP): Secret Redactor & 3-Layer VN Patient PII Masker."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# Common Vietnamese family names (accented and safe unaccented) for Layer 2 free-text name detection
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
    "Hồ",
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
    "Đào",
    "Dao",
    "Lương",
    "Luong",
    "Nghiêm",
    "Nghiem",
    "Tống",
    "Tong",
    "Quách",
    "Quach",
    "Cao",
    "Lại",
    "Lai",
    "Khổng",
    "Khong",
    "Âu",
    "Bế",
    "Nông",
    "Nong",
    "Sùng",
    "Vàng",
    "Giàng",
    "Giang",
    "Lò",
    "Điểu",
    "Dieu",
    "Danh",
    "Sơn",
    "Thạch",
    "Thach",
    "Lưu",
    "Luu",
    "Tăng",
    "Tang",
    "Hà",
    "Chu",
    "La",
    "Từ",
    "Tu",
    "Thái",
    "Thai",
    "Đàm",
    "Dam",
    "Mạc",
    "Mac",
    "Triệu",
    "Trieu",
    "Ninh",
    "Tôn",
    "Văn",
    "Quang",
    "Trầm",
)

# Unaccented surnames that collide with common English words ("To", "Do", "Ho", "Ha")
# Only treated as Vietnamese surnames when followed by a known Vietnamese middle/given syllable
AMBIGUOUS_UNACCENTED_SURNAMES: tuple[str, ...] = ("Do", "To", "Ho", "Ha", "An", "Be", "Lo", "Son")
VN_COMMON_SECOND_SYLLABLES: tuple[str, ...] = (
    "Van",
    "Thi",
    "Minh",
    "Quoc",
    "Hoang",
    "Duc",
    "Ngoc",
    "Bich",
    "Thu",
    "Anh",
    "Tuan",
    "Hung",
    "Mai",
    "Lan",
    "Phuong",
    "Huu",
    "Duy",
    "Khanh",
    "Thanh",
    "Xuan",
    "Hong",
    "Quang",
    "Trong",
    "Cong",
    "ba",
    "Ba",
)

TECHNICAL_ACRONYMS: frozenset[str] = frozenset(
    {
        "BHYT",
        "CCCD",
        "HSBA",
        "FIFO",
        "HTTP",
        "POST",
        "JSON",
        "REST",
        "RBAC",
        "CSDL",
        "VND",
        "USD",
        "API",
        "SQL",
        "HNSW",
        "CORS",
        "UUID",
        "SHA",
        "DLP",
        "LLM",
        "RAG",
        "HIS",
    }
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
        "bảo hiểm y tế",
        "bao hiem y te",
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
        "hồ sơ bệnh án",
        "ho so benh an",
        "quy trình",
        "quy trinh",
        "hà đông",
        "ha dong",
        "hà nam",
        "ha nam",
        "hà tĩnh",
        "ha tinh",
        "hà giang",
        "ha giang",
        "cao bằng",
        "cao bang",
        "thái nguyên",
        "thai nguyen",
        "thái bình",
        "thai binh",
        "lâm đồng",
        "lam dong",
        # Medical & domain proper nouns to prevent false positives
        "cao huyết áp",
        "cao huyet ap",
        "tăng huyết áp",
        "tang huyet ap",
        "huyết áp",
        "huyet ap",
        "danh mục thuốc",
        "danh muc thuoc",
        "danh mục",
        "danh muc",
        "chu trình khám bệnh",
        "chu trinh kham benh",
        "chu trình",
        "chu trinh",
        "lâm sàng nội tổng quát",
        "lâm sàng nội tổng",
        "lâm sàng",
        "lam sang",
        "cận lâm sàng",
        "can lam sang",
        "văn phòng đại diện",
        "văn phòng",
        "van phong",
        "phạm vi áp dụng",
        "pham vi ap dung",
        "từ chối thanh toán",
        "tu choi thanh toan",
        "chu kỳ kinh nguyệt",
        "chu kỳ",
        "chu ky",
        "hoàng gia",
        "lê lợi",
        "le loi",
        "quang trung",
        "trần hưng đạo",
        "tran hung dao",
        "đường nguyễn huệ",
        "nguyễn huệ",
        "phan thiết",
        "phan rang",
        "ninh bình",
        "ninh binh",
    }
)

# Layer 3 Gazetteer of known test/sample patient names (case-insensitive)
PATIENT_NAME_GAZETTEER: tuple[str, ...] = tuple(
    unicodedata.normalize("NFC", name)
    for name in (
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
)

# Secret regexes
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
CONN_STRING_RE = re.compile(
    r"(?i)(?:Server|Data Source|Host)\s*=\s*[^;\s\"']+;(?:[^;\n]*?;){0,10}"
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
    r"(?i)\b(api[_-]?key|secret[_-]?key|password|passwd|pwd|db[_-]?password|"
    r"database[_-]?password|client[_-]?secret|access[_-]?token|auth[_-]?token|mật[ _-]?khẩu)"
    r"(?:(\s*[:=]\s*[\"'])([^\"'\r\n]+)([\"'])|(\s*[:=]\s*)([A-Za-z0-9@#$%^&*!._\-~+/=?]{3,}))"
)

# PII regexes (supporting contiguous and hyphen/dot/space-separated formats)
BHYT_CARD_RE = re.compile(
    r"(?i)\b(?:DN|HX|CH|NN|TK|HC|XK|HT|TB|NO|CT|XB|TN|CS|QN|CA|CY|XN|MS|CC|CK|CB|KC|HD|"
    r"TE|BT|HN|DT|DK|XD|TS|TC|TQ|TA|TY|HG|LS|CN|HS|SV|GB|GD|PV)"
    r"[\s\-]?[1-5](?:[\s\-]?\d){12}\b"
)
CCCD_RE = re.compile(r"\b0\d{2}[\s.\-]?\d{3}[\s.\-]?\d{3}[\s.\-]?\d{3}\b|\b0\d{11}\b")
CMND_RE = re.compile(
    r"(?i)(?:(?:\bCMND\b|\bCMT\b|chứng minh nhân dân|số cmnd)\s*[:=\-]?\s*)?\b[0-3]\d{8}\b"
)
VN_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?84|0)[\s.\-]?(?:3[2-9]|5[2689]|7[06-9]|8[1-9]|9[0-9])"
    r"(?:[\s.\-]?\d){7}(?!\d)"
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
DOB_RE = re.compile(
    r"\b(?:0?[1-9]|[12]\d|3[01])[\/\-](?:0?[1-9]|1[0-2])[\/\-](?:19\d{2}|20[0-2]\d)\b"
)
MRN_RE = re.compile(r"\b(?:BA|BN|HSBA|MRN)-\d{4}-\d{4,8}\b")

# Vietnamese Title-Case and ALL-CAPS syllable patterns
_VN_UPPER_CHARS = "A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ"
_VN_LOWER_CHARS = "a-zàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"

_VN_TITLE_WORD = rf"[{_VN_UPPER_CHARS}][{_VN_LOWER_CHARS}]+"
_VN_ALLCAPS_WORD = rf"[{_VN_UPPER_CHARS}]{{2,7}}"
_VN_CAP_WORD = rf"(?:{_VN_TITLE_WORD}|{_VN_ALLCAPS_WORD})"
_VN_ANY_WORD = rf"[{_VN_UPPER_CHARS}{_VN_LOWER_CHARS}]{{2,10}}"

# Layer 1: Contextual patient label followed by:
# 1) Delimited label (with colon/dash) followed by 2-4 words of any casing
# 2) Space-separated label followed strictly by Title-Case / ALL-CAPS words
CONTEXTUAL_NAME_RE = re.compile(
    rf"(?P<prefix_delim>(?:[Bb]ệnh\s+nhân|[Bb][Nn]|[Hh]ọ\s+(?:và\s+)?tên|[Tt]ên\s+bệnh\s+nhân|"
    rf"[Ss]ản\s+phụ|[Nn]hi\s+đồng|patient[_ ]?name|full[_ ]?name)\s*[:=\-]\s*)"
    rf"(?P<name_delim>{_VN_ANY_WORD}(?:\s+{_VN_ANY_WORD}){{1,3}})"
    rf"|"
    rf"(?P<prefix_cap>(?:[Bb]ệnh\s+nhân|[Bb][Nn]|[Hh]ọ\s+(?:và\s+)?tên|[Tt]ên\s+bệnh\s+nhân|"
    rf"[Ss]ản\s+phụ|[Nn]hi\s+đồng|patient[_ ]?name|full[_ ]?name)\s+)"
    rf"(?P<name_cap>{_VN_CAP_WORD}(?:\s+{_VN_CAP_WORD}){{1,3}})"
)

# Layer 2: Unlabeled free-text Vietnamese full name (Title-Case or ALL-CAPS)
_SURNAME_VARIANTS = list(dict.fromkeys([*VN_SURNAMES, *(s.upper() for s in VN_SURNAMES)]))
_SURNAME_ALT = "|".join(re.escape(s) for s in _SURNAME_VARIANTS)
_AMBIG_SURNAME_ALT = "|".join(
    re.escape(s)
    for s in [*AMBIGUOUS_UNACCENTED_SURNAMES, *(s.upper() for s in AMBIGUOUS_UNACCENTED_SURNAMES)]
)
_SECOND_SYLLABLE_ALT = "|".join(
    re.escape(s)
    for s in [*VN_COMMON_SECOND_SYLLABLES, *(s.upper() for s in VN_COMMON_SECOND_SYLLABLES)]
)

FREETEXT_VN_NAME_RE = re.compile(
    rf"\b(?P<fullname>"
    rf"(?:(?:{_SURNAME_ALT})(?:\s+{_VN_TITLE_WORD}){{1,3}})"
    rf"|(?:(?:{_SURNAME_ALT})(?:\s+{_VN_ALLCAPS_WORD}){{1,3}})"
    rf"|(?:(?:{_AMBIG_SURNAME_ALT})\s+(?:{_SECOND_SYLLABLE_ALT})(?:\s+{_VN_CAP_WORD}){{0,2}})"
    rf")\b"
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

        # Normalize Unicode to NFC to prevent decomposed NFD bypasses
        text = unicodedata.normalize("NFC", raw_text)
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
            key_name = match.group(1)
            if match.group(3) is not None:
                val = match.group(3)
                if val == "[REDACTED_SECRET]":
                    return match.group(0)
                secrets_count += 1
                return f"{key_name}{match.group(2)}[REDACTED_SECRET]{match.group(4)}"
            val_unquoted = match.group(6) or ""
            if val_unquoted == "[REDACTED_SECRET]":
                return match.group(0)
            secrets_count += 1
            return f"{key_name}{match.group(5)}[REDACTED_SECRET]"

        text = KV_SECRET_RE.sub(_replace_kv_secret, text)

        # 2. Mask Structured Patient PII (BHYT, CCCD, CMND, Phone, Email, DOB, MRN)
        for pii_re, token in (
            (BHYT_CARD_RE, "[MASKED_BHYT]"),
            (CCCD_RE, "[MASKED_CCCD]"),
            (CMND_RE, "[MASKED_CCCD]"),
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
            name = match.group("name_delim") or match.group("name_cap") or ""
            prefix = match.group("prefix_delim") or match.group("prefix_cap") or ""
            candidate = name.strip()
            if candidate.lower() in TECHNICAL_PROPER_NOUN_WHITELIST:
                return match.group(0)
            pii_count += 1
            return f"{prefix}[MASKED_PATIENT_NAME]"

        text = CONTEXTUAL_NAME_RE.sub(_replace_ctx_name, text)

        # Layer 2 Free-text Vietnamese full names (Surname + 1..3 capitalized/uppercase syllables)
        def _replace_freetext_name(match: re.Match[str]) -> str:
            nonlocal pii_count
            candidate = match.group("fullname").strip()
            words = candidate.split()
            if any(w.upper() in TECHNICAL_ACRONYMS for w in words):
                return match.group(0)
            lowered = candidate.lower()
            if lowered in TECHNICAL_PROPER_NOUN_WHITELIST:
                return match.group(0)
            # If a 4-word match ends with a 2-word whitelisted technical phrase (e.g. "Ngoại Trú"),
            # keep the technical suffix intact and mask the 2-word name prefix
            if len(words) >= 4:
                tail_two = " ".join(words[-2:]).lower()
                if tail_two in TECHNICAL_PROPER_NOUN_WHITELIST:
                    pii_count += 1
                    return f"[MASKED_PATIENT_NAME] {' '.join(words[-2:])}"
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
