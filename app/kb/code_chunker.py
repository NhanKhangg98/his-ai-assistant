"""State-Machine Lexer Code Chunker & Code Map Extractor for C# (.cs) and TypeScript (.ts/.tsx)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.kb.doc_chunker import RawChunkDraft, infer_his_module

IGNORED_DIRS: frozenset[str] = frozenset(
    {"bin", "obj", "node_modules", ".git", "dist", "build", "__pycache__", ".venv"}
)

CS_CLASS_RE = re.compile(r"\bpublic\s+(?:sealed\s+|abstract\s+|partial\s+)?class\s+(?P<name>\w+)")
CS_ROUTE_ATTR_RE = re.compile(r'\[Route\(\s*"(?P<route>[^"]+)"\s*\)\]')
CS_HTTP_ATTR_RE = re.compile(
    r'\[Http(?P<verb>Get|Post|Put|Delete|Patch)(?:\(\s*"(?P<subroute>[^"]*)"\s*\))?\]'
)
CS_METHOD_SIG_RE = re.compile(
    r"^\s*public\s+(?:async\s+|virtual\s+|override\s+|static\s+)*"
    r"(?:Task<[^>]+>|Task|IActionResult|ActionResult<[^>]+>|[\w<>.\[\]?]+)\s+"
    r"(?P<method>[A-Za-z_]\w*)\s*\("
)
CS_PROP_RE = re.compile(
    r"^\s*public\s+(?P<type>[\w<>?.\[\]]+)\s+(?P<prop>[A-Za-z_]\w*)\s*\{\s*get;"
)

TS_FUNC_COMP_RE = re.compile(r"^\s*export\s+(?:async\s+)?function\s+(?P<name>[A-Za-z_]\w*)\s*\(")
TS_INTERFACE_RE = re.compile(r"^\s*export\s+interface\s+(?P<name>[A-Za-z_]\w*)\b")

MAX_METHOD_CHARS = 4000


@dataclass(slots=True)
class CodeMapDraft:
    """Extracted Code Map entry (API route, entity, or UI component) without raw method body."""

    entry_type: str  # 'api_endpoint' | 'entity' | 'component'
    http_method: str | None
    route_path: str | None
    controller_or_class: str
    action_or_symbol: str
    his_module: str
    start_line: int
    end_line: int
    summary_text: str


def should_ignore_path(path: Path | str) -> bool:
    """Return True if any path segment is in ignored build/dep folders (bin, obj, node_modules)."""
    parts = Path(path).parts
    return any(p.lower() in IGNORED_DIRS for p in parts)


def compute_line_brace_deltas(lines: list[str]) -> list[int]:
    """State-Machine Lexer computing net '{' minus '}' per line, ignoring strings and comments."""
    state = "NORMAL"
    deltas: list[int] = []

    for line in lines:
        delta = 0
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            nxt = line[i + 1] if i + 1 < n else ""

            if state == "NORMAL":
                if ch == "/" and nxt == "/":
                    break  # Line comment ignores rest of line
                if ch == "/" and nxt == "*":
                    state = "BLOCK_COMMENT"
                    i += 2
                    continue
                if ch == "@" and nxt == '"':
                    state = "VERBATIM_STRING"
                    i += 2
                    continue
                if ch == "$" and nxt == '"':
                    state = "INTERP_STRING"
                    i += 2
                    continue
                if ch == '"':
                    state = "STRING"
                    i += 1
                    continue
                if ch == "'":
                    state = "CHAR"
                    i += 1
                    continue
                if ch == "`":
                    state = "TEMPLATE_LITERAL"
                    i += 1
                    continue
                if ch == "{":
                    delta += 1
                elif ch == "}":
                    delta -= 1
                i += 1

            elif state == "BLOCK_COMMENT":
                if ch == "*" and nxt == "/":
                    state = "NORMAL"
                    i += 2
                else:
                    i += 1

            elif state == "STRING":
                if ch == "\\":
                    i += 2
                elif ch == '"':
                    state = "NORMAL"
                    i += 1
                else:
                    i += 1

            elif state == "INTERP_STRING":
                # Inside C# $"...", ignore all braces so interpolation never skews method depth
                if ch == "\\":
                    i += 2
                elif ch == '"':
                    state = "NORMAL"
                    i += 1
                else:
                    i += 1

            elif state == "VERBATIM_STRING":
                if ch == '"' and nxt == '"':
                    i += 2
                elif ch == '"':
                    state = "NORMAL"
                    i += 1
                else:
                    i += 1

            elif state == "CHAR":
                if ch == "\\":
                    i += 2
                elif ch == "'":
                    state = "NORMAL"
                    i += 1
                else:
                    i += 1

            elif state == "TEMPLATE_LITERAL":
                if ch == "\\":
                    i += 2
                elif ch == "`":
                    state = "NORMAL"
                    i += 1
                else:
                    i += 1

        deltas.append(delta)
    return deltas


def _split_oversized_code_chunk(
    symbol: str,
    signature_header: str,
    lines: list[str],
    start_line: int,
    his_module: str,
    rel_path: str,
) -> list[RawChunkDraft]:
    """Keep methods <= 4000 chars intact; if larger, split by statement boundary with header."""
    full_text = "\n".join(lines)
    end_line = start_line + len(lines) - 1
    if len(full_text) <= MAX_METHOD_CHARS:
        return [
            RawChunkDraft(
                chunk_type="source_code",
                his_module=his_module,
                heading_or_symbol=symbol,
                content=full_text,
                start_line=start_line,
                end_line=end_line,
                metadata={
                    "file": rel_path,
                    "heading": symbol,
                    "startLine": start_line,
                    "endLine": end_line,
                    "hisModule": his_module,
                },
            )
        ]

    sub_chunks: list[RawChunkDraft] = []
    buf: list[str] = [f"// Signature Context: {signature_header}"]
    buf_chars = len(buf[0])
    sub_start = start_line
    part_no = 1

    for offset, ln in enumerate(lines):
        cur_line_no = start_line + offset
        if buf_chars + len(ln) + 1 > MAX_METHOD_CHARS and len(buf) > 1:
            part_symbol = f"{symbol} (part {part_no})"
            sub_chunks.append(
                RawChunkDraft(
                    chunk_type="source_code",
                    his_module=his_module,
                    heading_or_symbol=part_symbol,
                    content="\n".join(buf),
                    start_line=sub_start,
                    end_line=cur_line_no - 1,
                    metadata={
                        "file": rel_path,
                        "heading": part_symbol,
                        "startLine": sub_start,
                        "endLine": cur_line_no - 1,
                        "hisModule": his_module,
                    },
                )
            )
            part_no += 1
            sub_start = cur_line_no
            buf = [f"// Signature Context: {signature_header}", ln]
            buf_chars = sum(len(x) + 1 for x in buf)
        else:
            buf.append(ln)
            buf_chars += len(ln) + 1

    if len(buf) > 1:
        part_symbol = f"{symbol} (part {part_no})" if part_no > 1 else symbol
        sub_chunks.append(
            RawChunkDraft(
                chunk_type="source_code",
                his_module=his_module,
                heading_or_symbol=part_symbol,
                content="\n".join(buf),
                start_line=sub_start,
                end_line=end_line,
                metadata={
                    "file": rel_path,
                    "heading": part_symbol,
                    "startLine": sub_start,
                    "endLine": end_line,
                    "hisModule": his_module,
                },
            )
        )
    return sub_chunks


class CodeChunker:
    """Extracts complete method/class/component source chunks and Code Map entries."""

    @classmethod
    def chunk_code_file(
        cls, file_path: Path, rel_path: str
    ) -> tuple[list[RawChunkDraft], list[CodeMapDraft]]:
        if should_ignore_path(rel_path) or should_ignore_path(file_path):
            return [], []

        ext = file_path.suffix.lower()
        raw_text = file_path.read_text(encoding="utf-8")
        his_module = infer_his_module(rel_path, raw_text[:600])
        if ext == ".cs":
            return cls._chunk_csharp(raw_text, rel_path, his_module)
        if ext in (".ts", ".tsx"):
            return cls._chunk_typescript(raw_text, rel_path, his_module)
        raise ValueError(f"Unsupported code extension: {ext}")

    @classmethod
    def _chunk_csharp(
        cls, raw_text: str, rel_path: str, his_module: str
    ) -> tuple[list[RawChunkDraft], list[CodeMapDraft]]:
        lines = raw_text.splitlines()
        deltas = compute_line_brace_deltas(lines)

        class_route_prefix = ""
        current_class = Path(rel_path).stem
        for ln in lines:
            rm = CS_ROUTE_ATTR_RE.search(ln)
            if rm and not class_route_prefix:
                class_route_prefix = rm.group("route").strip("/")
            cm = CS_CLASS_RE.search(ln)
            if cm:
                current_class = cm.group("name")
                break

        chunks: list[RawChunkDraft] = []
        code_maps: list[CodeMapDraft] = []

        # Check if this file defines C# Entities (no HTTP methods)
        has_methods = any(CS_METHOD_SIG_RE.search(ln) for ln in lines)
        if not has_methods:
            cls._extract_csharp_entities(lines, deltas, rel_path, his_module, chunks, code_maps)
            return chunks, code_maps

        n = len(lines)
        i = 0
        while i < n:
            cm = CS_CLASS_RE.search(lines[i])
            if cm:
                current_class = cm.group("name")

            sig_match = CS_METHOD_SIG_RE.search(lines[i])
            if sig_match:
                method_name = sig_match.group("method")
                attr_start = i
                http_verb: str | None = None
                sub_route = ""
                while attr_start > 0 and (
                    lines[attr_start - 1].strip().startswith("[")
                    or lines[attr_start - 1].strip().startswith("//")
                ):
                    attr_start -= 1
                    hm = CS_HTTP_ATTR_RE.search(lines[attr_start])
                    if hm:
                        http_verb = hm.group("verb").upper()
                        sub_route = (hm.group("subroute") or "").strip("/")

                # Use brace-balancing from method signature line until method body closes
                brace_balance = 0
                opened = False
                end_idx = i
                for j in range(i, n):
                    if deltas[j] > 0:
                        opened = True
                    brace_balance += deltas[j]
                    end_idx = j
                    if opened and brace_balance <= 0:
                        break

                method_lines = lines[attr_start : end_idx + 1]
                symbol = f"{current_class}.{method_name}"
                sig_header = f"{current_class}.{method_name}(...)"
                chunks.extend(
                    _split_oversized_code_chunk(
                        symbol=symbol,
                        signature_header=sig_header,
                        lines=method_lines,
                        start_line=attr_start + 1,
                        his_module=his_module,
                        rel_path=rel_path,
                    )
                )

                if http_verb:
                    full_route = "/" + "/".join(p for p in (class_route_prefix, sub_route) if p)
                    summary = (
                        f"[API Endpoint] {http_verb} {full_route} -> "
                        f"{current_class}.{method_name} (Module: {his_module}, File: {rel_path})"
                    )
                    code_maps.append(
                        CodeMapDraft(
                            entry_type="api_endpoint",
                            http_method=http_verb,
                            route_path=full_route,
                            controller_or_class=current_class,
                            action_or_symbol=method_name,
                            his_module=his_module,
                            start_line=attr_start + 1,
                            end_line=end_idx + 1,
                            summary_text=summary,
                        )
                    )
                i = end_idx + 1
                continue
            i += 1

        return chunks, code_maps

    @classmethod
    def _extract_csharp_entities(
        cls,
        lines: list[str],
        deltas: list[int],
        rel_path: str,
        his_module: str,
        chunks: list[RawChunkDraft],
        code_maps: list[CodeMapDraft],
    ) -> None:
        n = len(lines)
        i = 0
        while i < n:
            cm = CS_CLASS_RE.search(lines[i])
            if cm:
                entity_name = cm.group("name")
                start_idx = i
                brace_balance = 0
                opened = False
                end_idx = i
                props: list[str] = []
                for j in range(i, n):
                    if deltas[j] > 0:
                        opened = True
                    brace_balance += deltas[j]
                    pm = CS_PROP_RE.search(lines[j])
                    if pm:
                        props.append(f"{pm.group('prop')}: {pm.group('type')}")
                    end_idx = j
                    if opened and brace_balance <= 0:
                        break

                entity_lines = lines[start_idx : end_idx + 1]
                chunks.extend(
                    _split_oversized_code_chunk(
                        symbol=entity_name,
                        signature_header=f"class {entity_name}",
                        lines=entity_lines,
                        start_line=start_idx + 1,
                        his_module=his_module,
                        rel_path=rel_path,
                    )
                )
                prop_str = ", ".join(props) if props else "Entity"
                summary = (
                    f"[Entity] {entity_name} ({prop_str}) in {rel_path} (Module: {his_module})"
                )
                code_maps.append(
                    CodeMapDraft(
                        entry_type="entity",
                        http_method=None,
                        route_path=None,
                        controller_or_class=entity_name,
                        action_or_symbol=entity_name,
                        his_module=his_module,
                        start_line=start_idx + 1,
                        end_line=end_idx + 1,
                        summary_text=summary,
                    )
                )
                i = end_idx + 1
                continue
            i += 1

    @classmethod
    def _chunk_typescript(
        cls, raw_text: str, rel_path: str, his_module: str
    ) -> tuple[list[RawChunkDraft], list[CodeMapDraft]]:
        lines = raw_text.splitlines()
        deltas = compute_line_brace_deltas(lines)
        chunks: list[RawChunkDraft] = []
        code_maps: list[CodeMapDraft] = []

        n = len(lines)
        i = 0
        while i < n:
            fm = TS_FUNC_COMP_RE.search(lines[i])
            im = TS_INTERFACE_RE.search(lines[i])
            if fm or im:
                symbol_name = fm.group("name") if fm else im.group("name")  # type: ignore[union-attr]
                is_interface = im is not None
                start_idx = i
                brace_balance = 0
                opened = False
                end_idx = i
                for j in range(i, n):
                    if deltas[j] > 0:
                        opened = True
                    brace_balance += deltas[j]
                    end_idx = j
                    if opened and brace_balance <= 0:
                        break

                block_lines = lines[start_idx : end_idx + 1]
                chunks.extend(
                    _split_oversized_code_chunk(
                        symbol=symbol_name,
                        signature_header=symbol_name,
                        lines=block_lines,
                        start_line=start_idx + 1,
                        his_module=his_module,
                        rel_path=rel_path,
                    )
                )
                entry_type = "entity" if is_interface else "component"
                label = "TS Interface" if is_interface else "React/TS Function"
                summary = f"[{label}] {symbol_name} in {rel_path} (Module: {his_module})"
                code_maps.append(
                    CodeMapDraft(
                        entry_type=entry_type,
                        http_method=None,
                        route_path=None,
                        controller_or_class=Path(rel_path).stem,
                        action_or_symbol=symbol_name,
                        his_module=his_module,
                        start_line=start_idx + 1,
                        end_line=end_idx + 1,
                        summary_text=summary,
                    )
                )
                i = end_idx + 1
                continue
            i += 1

        return chunks, code_maps
