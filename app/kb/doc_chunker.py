"""Document Chunker for .md, .docx, and .pdf files preserving headings, tables, and metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

HEADING_MD_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
PDF_SECTION_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+[A-Z0-9À-Ỹ].{3,})$")


@dataclass(slots=True)
class RawChunkDraft:
    """Intermediate representation of a document or code chunk before DLP & embedding."""

    chunk_type: str  # 'doc' | 'code_map' | 'source_code'
    his_module: str
    heading_or_symbol: str
    content: str
    page_number: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def infer_his_module(file_path: str, text_hint: str = "") -> str:
    """Infer HIS functional module from file path or content."""
    combined = f"{file_path} {text_hint}".lower()
    if any(k in combined for k in ("registration", "tiep_nhan", "tiep don", "outpatient-bhyt")):
        return "Registration"
    if any(k in combined for k in ("pharmacy", "ke_don", "cap_phat", "thuoc", "dispense")):
        return "Pharmacy"
    if any(k in combined for k in ("billing", "vien_phi", "thanh_toan", "settlement", "copay")):
        return "Billing"
    if any(k in combined for k in ("outpatient", "kham_benh")):
        return "Outpatient"
    return "Core"


class DocumentChunker:
    """Splits .md, .docx, and .pdf documents by heading and table boundaries."""

    @classmethod
    def chunk_file(cls, file_path: Path, rel_path: str) -> list[RawChunkDraft]:
        ext = file_path.suffix.lower()
        if ext == ".md":
            return cls._chunk_markdown(file_path, rel_path)
        if ext == ".docx":
            return cls._chunk_docx(file_path, rel_path)
        if ext == ".pdf":
            return cls._chunk_pdf(file_path, rel_path)
        raise ValueError(f"Unsupported document extension: {ext}")

    @classmethod
    def _chunk_markdown(cls, file_path: Path, rel_path: str) -> list[RawChunkDraft]:
        raw_text = file_path.read_text(encoding="utf-8")
        his_module = infer_his_module(rel_path, raw_text[:600])
        lines = raw_text.splitlines()

        sections: list[tuple[str, list[str], int, int]] = []
        current_heading = file_path.stem
        current_lines: list[str] = []
        sec_start = 1

        for idx, line in enumerate(lines, start=1):
            m = HEADING_MD_RE.match(line)
            if m:
                if current_lines and "".join(current_lines).strip():
                    sections.append((current_heading, current_lines, sec_start, idx - 1))
                current_heading = m.group(2).strip()
                current_lines = [line]
                sec_start = idx
            else:
                current_lines.append(line)

        if current_lines and "".join(current_lines).strip():
            sections.append((current_heading, current_lines, sec_start, len(lines)))

        drafts: list[RawChunkDraft] = []
        for heading, sec_lines, s_line, e_line in sections:
            body = "\n".join(sec_lines).strip()
            if not body:
                continue
            drafts.append(
                RawChunkDraft(
                    chunk_type="doc",
                    his_module=his_module,
                    heading_or_symbol=heading,
                    content=body,
                    page_number=1,
                    start_line=s_line,
                    end_line=e_line,
                    metadata={
                        "file": rel_path,
                        "heading": heading,
                        "page": 1,
                        "startLine": s_line,
                        "endLine": e_line,
                        "hisModule": his_module,
                    },
                )
            )
        return drafts

    @classmethod
    def _chunk_docx(cls, file_path: Path, rel_path: str) -> list[RawChunkDraft]:
        doc = DocxDocument(str(file_path))
        full_preview = "\n".join(p.text for p in doc.paragraphs[:10])
        his_module = infer_his_module(rel_path, full_preview)

        current_heading = file_path.stem
        current_blocks: list[str] = []
        sections: list[tuple[str, list[str]]] = []

        for child in doc.element.body.iterchildren():
            if isinstance(child, CT_P):
                para = Paragraph(child, doc)
                text = para.text.strip()
                if not text:
                    continue
                style_name = (para.style.name if para.style else "") or ""
                if style_name.lower().startswith("heading"):
                    if current_blocks:
                        sections.append((current_heading, current_blocks))
                    current_heading = text
                    current_blocks = [f"## {text}"]
                else:
                    current_blocks.append(text)
            elif isinstance(child, CT_Tbl):
                table = Table(child, doc)
                md_table = cls._docx_table_to_markdown(table)
                if md_table:
                    current_blocks.append(md_table)

        if current_blocks:
            sections.append((current_heading, current_blocks))

        drafts: list[RawChunkDraft] = []
        for heading, blocks in sections:
            content = "\n\n".join(blocks).strip()
            if not content:
                continue
            drafts.append(
                RawChunkDraft(
                    chunk_type="doc",
                    his_module=his_module,
                    heading_or_symbol=heading,
                    content=content,
                    page_number=1,
                    metadata={
                        "file": rel_path,
                        "heading": heading,
                        "page": 1,
                        "hisModule": his_module,
                    },
                )
            )
        return drafts

    @staticmethod
    def _docx_table_to_markdown(table: Table) -> str:
        rows_md: list[str] = []
        for r_idx, row in enumerate(table.rows):
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            rows_md.append("| " + " | ".join(cells) + " |")
            if r_idx == 0:
                rows_md.append("|" + "|".join(["---"] * len(cells)) + "|")
        return "\n".join(rows_md)

    @classmethod
    def _chunk_pdf(cls, file_path: Path, rel_path: str) -> list[RawChunkDraft]:
        reader = PdfReader(str(file_path))
        drafts: list[RawChunkDraft] = []
        his_module = infer_his_module(rel_path)

        for page_idx, page in enumerate(reader.pages):
            page_num = page_idx + 1
            page_text = (page.extract_text() or "").strip()
            if not page_text:
                continue
            if page_idx == 0:
                his_module = infer_his_module(rel_path, page_text[:500])

            lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
            heading = f"Page {page_num}"
            for ln in lines[:4]:
                if PDF_SECTION_RE.match(ln):
                    heading = ln
                    break

            drafts.append(
                RawChunkDraft(
                    chunk_type="doc",
                    his_module=his_module,
                    heading_or_symbol=heading,
                    content=page_text,
                    page_number=page_num,
                    metadata={
                        "file": rel_path,
                        "heading": heading,
                        "page": page_num,
                        "hisModule": his_module,
                    },
                )
            )
        return drafts
