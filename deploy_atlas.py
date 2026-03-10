#!/usr/bin/env python3
"""
Atlas - Codebase Index Generator for LLM Coding Assistants
===========================================================

A single-file deployment script that creates a complete codebase index
for use with AI coding agents (Claude Code, ChatGPT Codex, etc.).

Usage:
    python deploy_atlas.py /path/to/project/root
    python deploy_atlas.py /path/to/project/root --yes  # Skip confirmation

This will create:
    PROJECT_ROOT/
    └── atlas/
        ├── bin/                    # Python tools
        │   ├── atlas_cli.py        # Main command router
        │   ├── query.py            # Query interface
        │   ├── translog.py         # Change tracking
        │   ├── query_translog.py   # Translog reports
        │   └── tool_common.py      # Shared utilities
        ├── index/                  # Generated index data (.toon files)
        ├── cache/                  # Runtime cache
        ├── translog/               # File mirrors and change history
        ├── instructions/           # LLM guidance
        │   └── llm.md
        └── index_state.toon        # Index metadata
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_EXTENSIONS = [
    # C/C++
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl", ".tpp", ".tcc",
    # Python
    ".py", ".pyi", ".pyw",
    # JavaScript/TypeScript
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    # Web
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    # Java/Kotlin
    ".java", ".kt", ".kts",
    # Go
    ".go",
    # Rust
    ".rs",
    # Ruby
    ".rb", ".erb",
    # PHP
    ".php",
    # Shell
    ".sh", ".bash", ".zsh", ".fish",
    # Config/Data
    ".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg",
    # Documentation
    ".md", ".rst", ".txt",
    # SQL
    ".sql",
    # Other
    ".swift", ".m", ".mm", ".scala", ".clj", ".lua", ".r", ".R",
    # Build files (by name)
    "Makefile", "CMakeLists.txt", "BUILD", "BUILD.bazel", "WORKSPACE",
    "Dockerfile", "docker-compose.yml", "package.json", "Cargo.toml",
    "pyproject.toml", "setup.py", "requirements.txt", "go.mod", "go.sum",
]

DEFAULT_SKIP_DIRS = [
    ".git", ".svn", ".hg", ".bzr",
    "node_modules", "vendor", "venv", ".venv", "env", ".env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".tox",
    "build", "dist", "target", "out", "bin", "obj",
    ".idea", ".vscode", ".vs",
    "coverage", ".coverage", "htmlcov",
    ".next", ".nuxt", ".output",
    "atlas",  # Don't index ourselves
]

DEFAULT_SKIP_GLOBS = [
    "*.min.js", "*.min.css", "*.map",
    "*.pyc", "*.pyo", "*.class", "*.o", "*.obj", "*.a", "*.so", "*.dylib", "*.dll",
    "*.exe", "*.bin", "*.dat", "*.db", "*.sqlite", "*.sqlite3",
    "*.log", "*.lock", "package-lock.json", "yarn.lock", "Cargo.lock",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.svg", "*.webp",
    "*.pdf", "*.doc", "*.docx", "*.xls", "*.xlsx", "*.ppt", "*.pptx",
    "*.zip", "*.tar", "*.gz", "*.rar", "*.7z",
    "*.woff", "*.woff2", "*.ttf", "*.eot",
    ".DS_Store", "Thumbs.db",
]

# =============================================================================
# TOON FORMAT HELPERS
# =============================================================================

TOON_ESCAPE_MAP = {"\\": "\\\\", "|": "\\p", ",": "\\c", "\n": "\\n"}
TOON_UNESC_MAP = {"\\p": "|", "\\c": ",", "\\n": "\n", "\\\\": "\\"}
RESERVED_TRANSLOG_DIRS = {"__history__", "__diffs__", "__meta__"}


def toon_esc(value: Any) -> str:
    s = "" if value is None else str(value)
    if not s:
        return ""
    out = []
    for ch in s:
        out.append(TOON_ESCAPE_MAP.get(ch, ch))
    return "".join(out).replace("\r", "")


def toon_unesc(value: str) -> str:
    if not value:
        return ""
    out = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            token = value[i:i+2]
            if token in TOON_UNESC_MAP:
                out.append(TOON_UNESC_MAP[token])
                i += 2
                continue
        out.append(value[i])
        i += 1
    return "".join(out)


def split_toon_row(line: str) -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf.append(line[i:i+2])
            i += 2
            continue
        if ch == "|":
            parts.append(toon_unesc("".join(buf)))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append(toon_unesc("".join(buf)))
    return parts


def toon_list(items: Iterable[Any]) -> str:
    values = []
    for item in items:
        if item is None:
            continue
        s = str(item)
        if s:
            values.append(toon_esc(s))
    return ",".join(values)


def parse_toon_list(value: str) -> List[str]:
    if not value:
        return []
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            buf.append(value[i:i+2])
            i += 2
            continue
        if ch == ",":
            parts.append(toon_unesc("".join(buf)))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append(toon_unesc("".join(buf)))
    return [p for p in parts if p != ""]


def toon_header(record_type: str, version: int, fields: Iterable[str]) -> str:
    return "@TOON|" + record_type + "|" + str(version) + "|" + "|".join(fields)


def read_toon_file(path: str) -> Tuple[List[str], List[Dict[str, str]], List[str]]:
    fields: List[str] = []
    rows: List[Dict[str, str]] = []
    comments: List[str] = []
    if not os.path.exists(path):
        return fields, rows, comments
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n\r")
            if not line:
                continue
            if line.startswith("#"):
                comments.append(line)
                continue
            if line.startswith("@TOON|"):
                header = line.split("|")
                fields = header[3:]
                continue
            parts = split_toon_row(line)
            row = {}
            for idx, field in enumerate(fields):
                row[field] = parts[idx] if idx < len(parts) else ""
            rows.append(row)
    return fields, rows, comments


def write_toon_file(path: str, record_type: str, version: int, fields: List[str], 
                    rows: List[Dict[str, Any]], comments: List[str] = None) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(toon_header(record_type, version, fields) + "\n")
        if comments:
            for c in comments:
                fh.write(c + "\n")
        for row in rows:
            values = [toon_esc(row.get(f, "")) for f in fields]
            fh.write("|".join(values) + "\n")


# =============================================================================
# UTILITY HELPERS
# =============================================================================

def ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def safe_rel_to_id(rel_path: str) -> str:
    rel_path = rel_path.replace("\\", "/").strip("/")
    if not rel_path:
        return "_root"
    out = []
    for ch in rel_path:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def normalize_rel_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 64), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def walk_project_files(
    root: str,
    extensions: Iterable[str],
    skip_dirs: Iterable[str] = None,
    skip_globs: Iterable[str] = None,
) -> List[str]:
    exts = {e.lower() for e in extensions}
    skip_dir_set = set(skip_dirs or [])
    skip_glob_list = list(skip_globs or [])
    results: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Filter directories
        filtered_dirs = []
        for d in dirnames:
            if d in skip_dir_set or d in RESERVED_TRANSLOG_DIRS:
                continue
            if d.startswith("."):
                continue
            filtered_dirs.append(d)
        dirnames[:] = filtered_dirs
        
        for fn in filenames:
            if any(fnmatch.fnmatch(fn, pat) for pat in skip_glob_list):
                continue
            fp = os.path.join(dirpath, fn)
            suffix = Path(fn).suffix.lower()
            if suffix in exts or fn in exts:
                results.append(fp)
    results.sort()
    return results


def print_kv(title: str, value: Any = "") -> None:
    print(f"  {title:<20} {value}")


def print_section(title: str) -> None:
    print()
    print("=" * 60)
    print(f" {title}")
    print("=" * 60)


# =============================================================================
# SYMBOL EXTRACTION (Regex-based heuristics)
# =============================================================================

# Common patterns for symbol extraction
PATTERNS = {
    # Classes/Structs
    "class": re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE),
    "struct": re.compile(r"^\s*(?:typedef\s+)?struct\s+(\w+)", re.MULTILINE),
    "interface": re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)", re.MULTILINE),
    "enum": re.compile(r"^\s*(?:export\s+)?enum\s+(\w+)", re.MULTILINE),
    
    # Functions
    "function_def": re.compile(r"^\s*(?:async\s+)?(?:export\s+)?(?:default\s+)?function\s+(\w+)", re.MULTILINE),
    "method_def": re.compile(r"^\s{2,}(?:async\s+)?(?:static\s+)?(?:public|private|protected)?\s*(\w+)\s*\([^)]*\)\s*[:{]", re.MULTILINE),
    "arrow_func": re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>", re.MULTILINE),
    "c_function": re.compile(r"^(?!.*\b(?:if|while|for|switch|return|else)\b)\s*(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:\w+[\s*&]+)+(\w+)\s*\([^;]*\)\s*\{", re.MULTILINE),
    "python_def": re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE),
    "python_class": re.compile(r"^\s*class\s+(\w+)\s*[:\(]", re.MULTILINE),
    
    # Imports
    "import_from": re.compile(r"^\s*(?:from\s+([^\s]+)\s+)?import\s+", re.MULTILINE),
    "import_require": re.compile(r"(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]", re.MULTILINE),
    "cpp_include": re.compile(r'^\s*#include\s*[<"]([^>"]+)[>"]', re.MULTILINE),
    "es_import": re.compile(r"^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    
    # Constants
    "const_def": re.compile(r"^\s*(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE),
    "define": re.compile(r"^\s*#define\s+([A-Z][A-Z0-9_]*)", re.MULTILINE),
    
    # TODO/FIXME
    "todo": re.compile(r"(?://|#|/\*)\s*(TODO|FIXME|HACK|XXX|BUG|NOTE)[\s:]+(.{0,100})", re.IGNORECASE),
    
    # Security patterns
    "security": re.compile(r"(password|secret|api[_-]?key|token|credential|auth|private[_-]?key)\s*[:=]", re.IGNORECASE),
}


def extract_symbols(content: str, file_path: str) -> Dict[str, Any]:
    """Extract symbols from file content using regex patterns."""
    ext = Path(file_path).suffix.lower()
    symbols = {
        "classes": [],
        "functions": [],
        "imports": [],
        "constants": [],
        "todos": [],
        "security_hints": [],
    }
    
    # Classes/Structs
    for m in PATTERNS["class"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "class"})
    for m in PATTERNS["struct"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "struct"})
    for m in PATTERNS["interface"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "interface"})
    for m in PATTERNS["enum"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "enum"})
    for m in PATTERNS["python_class"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "class"})
    
    # Functions
    for m in PATTERNS["function_def"].finditer(content):
        symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["arrow_func"].finditer(content):
        symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["python_def"].finditer(content):
        symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    if ext in (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"):
        for m in PATTERNS["c_function"].finditer(content):
            symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    
    # Imports
    for m in PATTERNS["cpp_include"].finditer(content):
        symbols["imports"].append({"target": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["es_import"].finditer(content):
        symbols["imports"].append({"target": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["import_from"].finditer(content):
        if m.group(1):
            symbols["imports"].append({"target": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["import_require"].finditer(content):
        symbols["imports"].append({"target": m.group(1), "line": content[:m.start()].count("\n") + 1})
    
    # Constants
    for m in PATTERNS["const_def"].finditer(content):
        symbols["constants"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["define"].finditer(content):
        symbols["constants"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    
    # TODOs
    for m in PATTERNS["todo"].finditer(content):
        symbols["todos"].append({
            "type": m.group(1).upper(),
            "text": m.group(2).strip(),
            "line": content[:m.start()].count("\n") + 1
        })
    
    # Security hints
    for m in PATTERNS["security"].finditer(content):
        symbols["security_hints"].append({
            "match": m.group(0)[:50],
            "line": content[:m.start()].count("\n") + 1
        })
    
    return symbols


def detect_probable_calls(content: str, known_functions: Set[str], max_functions: int = 500) -> List[str]:
    """Detect probable function calls based on known function names.
    
    Optimized: Uses a single regex with alternation for better performance.
    Limited to max_functions to prevent regex explosion on huge codebases.
    """
    if not known_functions:
        return []
    
    # Limit function count to prevent regex explosion
    func_list = list(known_functions)[:max_functions]
    
    # Build a single regex with alternation (much faster than N separate searches)
    # Group into batches to avoid regex size limits
    calls = set()
    batch_size = 100
    
    for i in range(0, len(func_list), batch_size):
        batch = func_list[i:i + batch_size]
        # Create pattern: \b(func1|func2|func3)\s*\(
        pattern_str = r"\b(" + "|".join(re.escape(f) for f in batch) + r")\s*\("
        try:
            pattern = re.compile(pattern_str)
            for m in pattern.finditer(content):
                calls.add(m.group(1))
        except re.error:
            # If regex fails (too complex), skip this batch
            pass
    
    return list(calls)


# =============================================================================
# INDEX BUILDER
# =============================================================================

class IndexBuilder:
    def __init__(self, project_root: str, atlas_root: str, config: Dict[str, Any]):
        self.project_root = os.path.abspath(project_root)
        self.atlas_root = atlas_root
        self.index_dir = os.path.join(atlas_root, "index")
        self.cache_dir = os.path.join(atlas_root, "cache")
        self.config = config
        
        # Collected data
        self.files: List[Dict[str, Any]] = []
        self.symbols: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.imports_map: Dict[str, List[str]] = defaultdict(list)
        self.importers_map: Dict[str, List[str]] = defaultdict(list)
        self.all_functions: Set[str] = set()
        self.call_graph: Dict[str, List[str]] = defaultdict(list)
        self.todos: List[Dict[str, Any]] = []
        self.security_hints: List[Dict[str, Any]] = []
        self.constants: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        
    def build(self, verbose: bool = False, skip_callgraph: bool = False) -> Dict[str, Any]:
        """Build the complete index."""
        print_section("Scanning Files")
        self._scan_files(verbose)
        
        print_section("Extracting Symbols")
        self._extract_all_symbols(verbose)
        
        if skip_callgraph:
            print_section("Skipping Call Graph (--skip-callgraph)")
            print("  Call graph analysis skipped for faster indexing.")
        else:
            print_section("Building Call Graph")
            self._build_call_graph(verbose)
        
        print_section("Writing Index Files")
        self._write_index_files()
        
        print_section("Writing Cache")
        self._write_cache()
        
        return self._get_summary()
    
    def _scan_files(self, verbose: bool) -> None:
        """Scan project for indexable files."""
        exts = self.config.get("extensions", DEFAULT_EXTENSIONS)
        skip_dirs = self.config.get("skip_dirs", DEFAULT_SKIP_DIRS)
        skip_globs = self.config.get("skip_globs", DEFAULT_SKIP_GLOBS)
        
        print("  Scanning directory tree...", flush=True)
        file_paths = walk_project_files(self.project_root, exts, skip_dirs, skip_globs)
        
        for fp in file_paths:
            rel = normalize_rel_path(os.path.relpath(fp, self.project_root))
            try:
                stat_info = os.stat(fp)
                self.files.append({
                    "path": rel,
                    "abs_path": fp,
                    "size": stat_info.st_size,
                    "mtime": stat_info.st_mtime,
                    "ext": Path(fp).suffix.lower(),
                })
            except OSError:
                continue
        
        print(f"  Found {len(self.files)} files to index")
    
    def _extract_all_symbols(self, verbose: bool) -> None:
        """Extract symbols from all files."""
        total = len(self.files)
        last_report = 0
        
        for idx, f in enumerate(self.files):
            # Always show progress for large codebases
            progress = idx + 1
            if progress == total or progress - last_report >= 200 or (total > 100 and progress % max(1, total // 10) == 0):
                print(f"  Processed {progress}/{total} files...", flush=True)
                last_report = progress
            
            try:
                content = read_text(f["abs_path"])
                symbols = extract_symbols(content, f["path"])
                
                # Store symbols by file
                f["line_count"] = content.count("\n") + 1
                f["classes"] = [c["name"] for c in symbols["classes"]]
                f["functions"] = [fn["name"] for fn in symbols["functions"]]
                f["import_count"] = len(symbols["imports"])
                
                # Aggregate
                for cls in symbols["classes"]:
                    cls["file"] = f["path"]
                    self.symbols["classes"].append(cls)
                
                for func in symbols["functions"]:
                    func["file"] = f["path"]
                    self.symbols["functions"].append(func)
                    self.all_functions.add(func["name"])
                
                for imp in symbols["imports"]:
                    self.imports_map[f["path"]].append(imp["target"])
                    self.importers_map[imp["target"]].append(f["path"])
                
                for const in symbols["constants"]:
                    const["file"] = f["path"]
                    self.constants[const["name"]].append(const)
                
                for todo in symbols["todos"]:
                    todo["file"] = f["path"]
                    self.todos.append(todo)
                
                for sec in symbols["security_hints"]:
                    sec["file"] = f["path"]
                    self.security_hints.append(sec)
                    
            except Exception as e:
                if verbose:
                    print(f"  Warning: Failed to process {f['path']}: {e}")
    
    def _build_call_graph(self, verbose: bool) -> None:
        """Build probable call graph (heuristic)."""
        if not self.all_functions:
            print("  No functions found, skipping call graph.")
            return
        
        func_count = len(self.all_functions)
        print(f"  Analyzing calls for {func_count} known functions...")
        if func_count > 500:
            print(f"  Note: Limiting to 500 functions for performance (found {func_count})")
            
        total = len(self.files)
        last_report = 0
        
        for idx, f in enumerate(self.files):
            # Always show progress
            progress = idx + 1
            if progress == total or progress - last_report >= 200 or (total > 100 and progress % max(1, total // 10) == 0):
                print(f"  Analyzed {progress}/{total} files...", flush=True)
                last_report = progress
            
            try:
                content = read_text(f["abs_path"])
                calls = detect_probable_calls(content, self.all_functions)
                if calls:
                    self.call_graph[f["path"]] = calls
            except Exception:
                continue
    
    def _write_index_files(self) -> None:
        """Write all index data to TOON files."""
        ensure_dir(self.index_dir)
        
        # 1. File inventory
        file_fields = ["path", "size", "line_count", "ext", "classes", "functions", "import_count"]
        file_rows = []
        for f in self.files:
            file_rows.append({
                "path": f["path"],
                "size": str(f.get("size", 0)),
                "line_count": str(f.get("line_count", 0)),
                "ext": f.get("ext", ""),
                "classes": toon_list(f.get("classes", [])),
                "functions": toon_list(f.get("functions", [])),
                "import_count": str(f.get("import_count", 0)),
            })
        write_toon_file(
            os.path.join(self.index_dir, "files.toon"),
            "file_inventory", 1, file_fields, file_rows,
            ["# File inventory - all indexed source files"]
        )
        
        # 2. Classes
        class_fields = ["name", "type", "file", "line"]
        class_rows = [{"name": c["name"], "type": c.get("type", "class"), "file": c["file"], "line": str(c["line"])}
                      for c in self.symbols["classes"]]
        write_toon_file(
            os.path.join(self.index_dir, "classes.toon"),
            "class_index", 1, class_fields, class_rows,
            ["# Class/struct/interface/enum definitions"]
        )
        
        # 3. Functions
        func_fields = ["name", "file", "line"]
        func_rows = [{"name": fn["name"], "file": fn["file"], "line": str(fn["line"])}
                     for fn in self.symbols["functions"]]
        write_toon_file(
            os.path.join(self.index_dir, "functions.toon"),
            "function_index", 1, func_fields, func_rows,
            ["# Function/method definitions"]
        )
        
        # 4. Imports
        import_fields = ["file", "imports"]
        import_rows = [{"file": f, "imports": toon_list(imps)} 
                       for f, imps in sorted(self.imports_map.items())]
        write_toon_file(
            os.path.join(self.index_dir, "imports.toon"),
            "import_map", 1, import_fields, import_rows,
            ["# File import/include relationships"]
        )
        
        # 5. Importers (reverse map)
        importer_fields = ["target", "importers"]
        importer_rows = [{"target": t, "importers": toon_list(imps)}
                         for t, imps in sorted(self.importers_map.items())]
        write_toon_file(
            os.path.join(self.index_dir, "importers.toon"),
            "importer_map", 1, importer_fields, importer_rows,
            ["# Reverse import map - which files import a given target"]
        )
        
        # 6. Call graph (heuristic)
        call_fields = ["file", "calls"]
        call_rows = [{"file": f, "calls": toon_list(calls)}
                     for f, calls in sorted(self.call_graph.items())]
        write_toon_file(
            os.path.join(self.index_dir, "calls.toon"),
            "call_graph", 1, call_fields, call_rows,
            ["# Probable call graph (heuristic, regex-based)"]
        )
        
        # 7. TODOs
        todo_fields = ["file", "line", "type", "text"]
        todo_rows = [{"file": t["file"], "line": str(t["line"]), "type": t["type"], "text": t["text"]}
                     for t in self.todos]
        write_toon_file(
            os.path.join(self.index_dir, "todos.toon"),
            "todo_index", 1, todo_fields, todo_rows,
            ["# TODO/FIXME/HACK markers"]
        )
        
        # 8. Security hints
        sec_fields = ["file", "line", "match"]
        sec_rows = [{"file": s["file"], "line": str(s["line"]), "match": s["match"]}
                    for s in self.security_hints]
        write_toon_file(
            os.path.join(self.index_dir, "security.toon"),
            "security_hints", 1, sec_fields, sec_rows,
            ["# Potential security-sensitive patterns"]
        )
        
        # 9. Constants
        const_fields = ["name", "file", "line"]
        const_rows = []
        for name, locs in sorted(self.constants.items()):
            for loc in locs:
                const_rows.append({"name": name, "file": loc["file"], "line": str(loc["line"])})
        write_toon_file(
            os.path.join(self.index_dir, "constants.toon"),
            "constant_index", 1, const_fields, const_rows,
            ["# Named constants and defines"]
        )
        
        # 10. Directory structure
        dir_tree = defaultdict(list)
        for f in self.files:
            parent = os.path.dirname(f["path"]) or "."
            dir_tree[parent].append(os.path.basename(f["path"]))
        
        struct_fields = ["directory", "files"]
        struct_rows = [{"directory": d, "files": toon_list(files)}
                       for d, files in sorted(dir_tree.items())]
        write_toon_file(
            os.path.join(self.index_dir, "structure.toon"),
            "directory_structure", 1, struct_fields, struct_rows,
            ["# Directory tree with file listings"]
        )
    
    def _write_cache(self) -> None:
        """Write cache files for faster querying."""
        ensure_dir(self.cache_dir)
        
        # Config
        with open(os.path.join(self.cache_dir, "config_effective.json"), "w") as fh:
            json.dump(self.config, fh, indent=2)
        
        # Function lookup
        func_lookup = defaultdict(list)
        for fn in self.symbols["functions"]:
            func_lookup[fn["name"]].append({"file": fn["file"], "line": fn["line"]})
        with open(os.path.join(self.cache_dir, "function_lookup.json"), "w") as fh:
            json.dump(func_lookup, fh)
        
        # Class lookup
        class_lookup = defaultdict(list)
        for cls in self.symbols["classes"]:
            class_lookup[cls["name"]].append({"file": cls["file"], "line": cls["line"], "type": cls.get("type", "class")})
        with open(os.path.join(self.cache_dir, "class_lookup.json"), "w") as fh:
            json.dump(class_lookup, fh)
        
        # Index state
        with open(os.path.join(self.cache_dir, "index_state.json"), "w") as fh:
            json.dump({
                "project_root": self.project_root,
                "atlas_root": self.atlas_root,
                "indexed_at": utc_now(),
                "file_count": len(self.files),
                "function_count": len(self.symbols["functions"]),
                "class_count": len(self.symbols["classes"]),
            }, fh, indent=2)
    
    def _get_summary(self) -> Dict[str, Any]:
        return {
            "files": len(self.files),
            "classes": len(self.symbols["classes"]),
            "functions": len(self.symbols["functions"]),
            "imports": sum(len(v) for v in self.imports_map.values()),
            "todos": len(self.todos),
            "security_hints": len(self.security_hints),
            "constants": sum(len(v) for v in self.constants.values()),
        }


# =============================================================================
# EMBEDDED TOOL SCRIPTS
# =============================================================================

TOOL_COMMON_PY = r'''#!/usr/bin/env python3
"""Shared helpers for Atlas tools."""
from __future__ import annotations

import json
import os
import re
import fnmatch
import glob
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

TOON_ESCAPE_MAP = {"\\": "\\\\", "|": "\\p", ",": "\\c", "\n": "\\n"}
TOON_UNESC_MAP = {"\\p": "|", "\\c": ",", "\\n": "\n", "\\\\": "\\"}
RESERVED_TRANSLOG_DIRS = {"__history__", "__diffs__", "__meta__"}


def toon_esc(value: Any) -> str:
    s = "" if value is None else str(value)
    if not s:
        return ""
    out = []
    for ch in s:
        out.append(TOON_ESCAPE_MAP.get(ch, ch))
    return "".join(out).replace("\r", "")


def toon_unesc(value: str) -> str:
    if not value:
        return ""
    out = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            token = value[i:i+2]
            if token in TOON_UNESC_MAP:
                out.append(TOON_UNESC_MAP[token])
                i += 2
                continue
        out.append(value[i])
        i += 1
    return "".join(out)


def split_toon_row(line: str) -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf.append(line[i:i+2])
            i += 2
            continue
        if ch == "|":
            parts.append(toon_unesc("".join(buf)))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append(toon_unesc("".join(buf)))
    return parts


def toon_list(items: Iterable[Any]) -> str:
    values = []
    for item in items:
        if item is None:
            continue
        s = str(item)
        if s:
            values.append(toon_esc(s))
    return ",".join(values)


def parse_toon_list(value: str) -> List[str]:
    if not value:
        return []
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            buf.append(value[i:i+2])
            i += 2
            continue
        if ch == ",":
            parts.append(toon_unesc("".join(buf)))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append(toon_unesc("".join(buf)))
    return [p for p in parts if p != ""]


def toon_header(record_type: str, version: int, fields: Iterable[str]) -> str:
    return "@TOON|" + record_type + "|" + str(version) + "|" + "|".join(fields)


def read_toon_file(path: str) -> Tuple[List[str], List[Dict[str, str]], List[str]]:
    fields: List[str] = []
    rows: List[Dict[str, str]] = []
    comments: List[str] = []
    if not os.path.exists(path):
        return fields, rows, comments
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n\r")
            if not line:
                continue
            if line.startswith("#"):
                comments.append(line)
                continue
            if line.startswith("@TOON|"):
                header = line.split("|")
                fields = header[3:]
                continue
            parts = split_toon_row(line)
            row = {}
            for idx, field in enumerate(fields):
                row[field] = parts[idx] if idx < len(parts) else ""
            rows.append(row)
    return fields, rows, comments


def find_atlas_root(explicit: str | None = None) -> str:
    candidates = []
    if explicit:
        candidates.append(os.path.abspath(explicit))
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.extend([
        os.path.dirname(script_dir),  # bin's parent = atlas
        script_dir,
        os.getcwd(),
        os.path.dirname(os.getcwd()),
    ])
    seen = set()
    for base in candidates:
        if not base or base in seen:
            continue
        seen.add(base)
        marker = os.path.join(base, "index_state.toon")
        if os.path.exists(marker):
            return base
        marker = os.path.join(base, "atlas", "index_state.toon")
        if os.path.exists(marker):
            return os.path.join(base, "atlas")
    raise FileNotFoundError("Unable to locate Atlas index (missing index_state.toon). Use --atlas.")


def load_index_state(atlas_root: str) -> Dict[str, Any]:
    state = {}
    path = os.path.join(atlas_root, "index_state.toon")
    if os.path.exists(path):
        fields, rows, _ = read_toon_file(path)
        if rows:
            state = rows[0]
    cache_path = os.path.join(atlas_root, "cache", "index_state.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                state.update(json.load(fh))
        except Exception:
            pass
    return state


def safe_rel_to_id(rel_path: str) -> str:
    rel_path = rel_path.replace("\\", "/").strip("/")
    if not rel_path:
        return "_root"
    out = []
    for ch in rel_path:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def normalize_rel_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def path_matches(rel_path: str, query: str, exact: bool = False, case_sensitive: bool = False) -> bool:
    rel_norm = normalize_rel_path(rel_path)
    q = normalize_rel_path(query)
    if not case_sensitive:
        rel_norm = rel_norm.lower()
        q = q.lower()
    if exact:
        return rel_norm == q
    return q in rel_norm


def ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def iter_toon_files(atlas_root: str, pattern: str = "**/*.toon") -> Iterable[str]:
    return glob.iglob(os.path.join(atlas_root, pattern), recursive=True)


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return items
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def walk_project_files(
    root: str,
    extensions: Iterable[str],
    skip_dirs: Iterable[str] | None = None,
    skip_globs: Iterable[str] | None = None,
) -> List[str]:
    exts = {e.lower() for e in extensions}
    skip_dir_set = set(skip_dirs or [])
    skip_glob_list = list(skip_globs or [])
    results: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        filtered_dirs = []
        for d in dirnames:
            if d in skip_dir_set or d in RESERVED_TRANSLOG_DIRS:
                continue
            filtered_dirs.append(d)
        dirnames[:] = filtered_dirs
        for fn in filenames:
            if any(fnmatch.fnmatch(fn, pat) for pat in skip_glob_list):
                continue
            fp = os.path.join(dirpath, fn)
            if Path(fn).suffix.lower() in exts or fn in exts:
                results.append(fp)
    results.sort()
    return results


def print_kv(title: str, value: Any = "") -> None:
    print(f"{title:<18} {value}")


def print_section(title: str) -> None:
    print()
    print("=" * len(title))
    print(title)
    print("=" * len(title))


def record_to_jsonable(record: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in record.items():
        if isinstance(v, (list, dict, str, int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out
'''

QUERY_PY = r'''#!/usr/bin/env python3
"""Atlas query interface - search and analyze the codebase index."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

from tool_common import (
    find_atlas_root,
    json_dump,
    load_index_state,
    normalize_rel_path,
    parse_toon_list,
    path_matches,
    print_kv,
    print_section,
    read_toon_file,
)


def load_toon(atlas_root: str, name: str) -> List[Dict[str, str]]:
    path = os.path.join(atlas_root, "index", f"{name}.toon")
    if not os.path.exists(path):
        return []
    _, rows, _ = read_toon_file(path)
    return rows


def load_json_cache(atlas_root: str, name: str) -> Any:
    path = os.path.join(atlas_root, "cache", f"{name}.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class QueryEngine:
    def __init__(self, atlas_root: str):
        self.atlas_root = atlas_root
        self.state = load_index_state(atlas_root)
        self._cache = {}
    
    def _get_toon(self, name: str) -> List[Dict[str, str]]:
        if name not in self._cache:
            self._cache[name] = load_toon(self.atlas_root, name)
        return self._cache[name]
    
    def _get_json(self, name: str) -> Any:
        key = f"json:{name}"
        if key not in self._cache:
            self._cache[key] = load_json_cache(self.atlas_root, name)
        return self._cache[key]
    
    def cmd_summary(self) -> Dict[str, Any]:
        files = self._get_toon("files")
        classes = self._get_toon("classes")
        functions = self._get_toon("functions")
        todos = self._get_toon("todos")
        
        return {
            "project_root": self.state.get("project_root", ""),
            "indexed_at": self.state.get("indexed_at", ""),
            "file_count": len(files),
            "class_count": len(classes),
            "function_count": len(functions),
            "todo_count": len(todos),
        }
    
    def cmd_capabilities(self) -> Dict[str, Any]:
        return {
            "commands": [
                "summary", "capabilities", "doctor",
                "structure", "search", "class", "func", "symbol",
                "imports", "importers", "calls", "callers", "callees",
                "impact", "impact-file", "related",
                "todo", "security", "constants", "hotspots",
            ],
            "extraction_mode": "regex-heuristic",
            "call_graph": "probable (heuristic)",
        }
    
    def cmd_doctor(self) -> Dict[str, Any]:
        issues = []
        files = self._get_toon("files")
        if not files:
            issues.append("No files indexed - index may be empty or corrupted")
        
        state_path = os.path.join(self.atlas_root, "index_state.toon")
        if not os.path.exists(state_path):
            issues.append("index_state.toon missing")
        
        return {
            "status": "ok" if not issues else "issues",
            "issues": issues,
            "file_count": len(files),
        }
    
    def cmd_structure(self, path: str) -> Dict[str, Any]:
        structure = self._get_toon("structure")
        matches = []
        for row in structure:
            if path_matches(row["directory"], path):
                matches.append({
                    "directory": row["directory"],
                    "files": parse_toon_list(row.get("files", "")),
                })
        return {"matches": matches}
    
    def cmd_search(self, pattern: str, regex: bool = False, path_filter: str = None) -> Dict[str, Any]:
        files = self._get_toon("files")
        classes = self._get_toon("classes")
        functions = self._get_toon("functions")
        
        results = {"files": [], "classes": [], "functions": []}
        
        if regex:
            try:
                pat = re.compile(pattern, re.IGNORECASE)
            except re.error:
                return {"error": f"Invalid regex: {pattern}"}
        
        for f in files:
            if path_filter and not path_matches(f["path"], path_filter):
                continue
            if regex:
                if pat.search(f["path"]):
                    results["files"].append(f["path"])
            else:
                if pattern.lower() in f["path"].lower():
                    results["files"].append(f["path"])
        
        for c in classes:
            if path_filter and not path_matches(c["file"], path_filter):
                continue
            if regex:
                if pat.search(c["name"]):
                    results["classes"].append({"name": c["name"], "file": c["file"], "line": c["line"]})
            else:
                if pattern.lower() in c["name"].lower():
                    results["classes"].append({"name": c["name"], "file": c["file"], "line": c["line"]})
        
        for fn in functions:
            if path_filter and not path_matches(fn["file"], path_filter):
                continue
            if regex:
                if pat.search(fn["name"]):
                    results["functions"].append({"name": fn["name"], "file": fn["file"], "line": fn["line"]})
            else:
                if pattern.lower() in fn["name"].lower():
                    results["functions"].append({"name": fn["name"], "file": fn["file"], "line": fn["line"]})
        
        return results
    
    def cmd_class(self, name: str) -> Dict[str, Any]:
        lookup = self._get_json("class_lookup")
        if name in lookup:
            return {"name": name, "locations": lookup[name]}
        
        # Partial match
        matches = []
        for k, v in lookup.items():
            if name.lower() in k.lower():
                matches.append({"name": k, "locations": v})
        return {"partial_matches": matches}
    
    def cmd_func(self, name: str) -> Dict[str, Any]:
        lookup = self._get_json("function_lookup")
        if name in lookup:
            return {"name": name, "locations": lookup[name]}
        
        matches = []
        for k, v in lookup.items():
            if name.lower() in k.lower():
                matches.append({"name": k, "locations": v})
        return {"partial_matches": matches}
    
    def cmd_symbol(self, name: str) -> Dict[str, Any]:
        class_result = self.cmd_class(name)
        func_result = self.cmd_func(name)
        return {
            "class_matches": class_result,
            "function_matches": func_result,
        }
    
    def cmd_imports(self, file: str) -> Dict[str, Any]:
        imports = self._get_toon("imports")
        file_norm = normalize_rel_path(file)
        for row in imports:
            if path_matches(row["file"], file_norm):
                return {
                    "file": row["file"],
                    "imports": parse_toon_list(row.get("imports", "")),
                }
        return {"file": file_norm, "imports": []}
    
    def cmd_importers(self, target: str) -> Dict[str, Any]:
        importers = self._get_toon("importers")
        target_norm = normalize_rel_path(target)
        for row in importers:
            if target_norm.lower() in row["target"].lower():
                return {
                    "target": row["target"],
                    "importers": parse_toon_list(row.get("importers", "")),
                }
        return {"target": target_norm, "importers": []}
    
    def cmd_calls(self, file: str) -> Dict[str, Any]:
        calls = self._get_toon("calls")
        file_norm = normalize_rel_path(file)
        for row in calls:
            if path_matches(row["file"], file_norm):
                return {
                    "file": row["file"],
                    "calls": parse_toon_list(row.get("calls", "")),
                    "note": "heuristic - regex-based detection",
                }
        return {"file": file_norm, "calls": [], "note": "heuristic"}
    
    def cmd_callers(self, symbol: str) -> Dict[str, Any]:
        calls = self._get_toon("calls")
        callers = []
        for row in calls:
            row_calls = parse_toon_list(row.get("calls", ""))
            if symbol in row_calls:
                callers.append(row["file"])
        return {
            "symbol": symbol,
            "callers": callers,
            "note": "heuristic - regex-based detection",
        }
    
    def cmd_callees(self, symbol: str) -> Dict[str, Any]:
        # Find files that define this symbol, then get their calls
        func_lookup = self._get_json("function_lookup")
        if symbol not in func_lookup:
            return {"symbol": symbol, "callees": [], "note": "symbol not found"}
        
        locs = func_lookup[symbol]
        all_callees = set()
        calls = self._get_toon("calls")
        
        for loc in locs:
            for row in calls:
                if row["file"] == loc["file"]:
                    all_callees.update(parse_toon_list(row.get("calls", "")))
        
        return {
            "symbol": symbol,
            "callees": list(all_callees),
            "note": "heuristic - all calls from files defining this symbol",
        }
    
    def cmd_impact(self, symbol: str) -> Dict[str, Any]:
        callers = self.cmd_callers(symbol)
        return {
            "symbol": symbol,
            "direct_callers": callers["callers"],
            "note": "heuristic - direct callers only",
        }
    
    def cmd_impact_file(self, file: str) -> Dict[str, Any]:
        importers = self._get_toon("importers")
        file_norm = normalize_rel_path(file)
        
        # Find who imports this file
        dependents = []
        for row in importers:
            if file_norm.lower() in row["target"].lower():
                dependents.extend(parse_toon_list(row.get("importers", "")))
        
        return {
            "file": file_norm,
            "dependents": list(set(dependents)),
        }
    
    def cmd_related(self, file: str) -> Dict[str, Any]:
        imports_result = self.cmd_imports(file)
        impact_result = self.cmd_impact_file(file)
        
        return {
            "file": file,
            "imports": imports_result.get("imports", []),
            "imported_by": impact_result.get("dependents", []),
        }
    
    def cmd_todo(self, term: str = None) -> Dict[str, Any]:
        todos = self._get_toon("todos")
        if term:
            todos = [t for t in todos if term.lower() in t.get("text", "").lower() 
                     or term.lower() in t.get("file", "").lower()]
        return {"todos": todos[:100]}  # Limit output
    
    def cmd_security(self, term: str = None) -> Dict[str, Any]:
        security = self._get_toon("security")
        if term:
            security = [s for s in security if term.lower() in s.get("match", "").lower()
                        or term.lower() in s.get("file", "").lower()]
        return {"hints": security}
    
    def cmd_constants(self, name: str = None) -> Dict[str, Any]:
        constants = self._get_toon("constants")
        if name:
            constants = [c for c in constants if name.lower() in c.get("name", "").lower()]
        return {"constants": constants[:100]}
    
    def cmd_hotspots(self, limit: int = 20) -> Dict[str, Any]:
        files = self._get_toon("files")
        
        # Score by complexity indicators
        scored = []
        for f in files:
            score = 0
            score += int(f.get("line_count", 0)) // 100
            score += len(parse_toon_list(f.get("functions", ""))) * 2
            score += len(parse_toon_list(f.get("classes", ""))) * 3
            score += int(f.get("import_count", 0))
            scored.append({"file": f["path"], "score": score, "lines": f.get("line_count", 0)})
        
        scored.sort(key=lambda x: x["score"], reverse=True)
        return {"hotspots": scored[:limit]}


def format_output(data: Any, as_json: bool = False) -> None:
    if as_json:
        print(json_dump(data))
        return
    
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                print(f"\n{k}:")
                for item in v[:50]:
                    if isinstance(item, dict):
                        print(f"  - {item}")
                    else:
                        print(f"  - {item}")
                if len(v) > 50:
                    print(f"  ... and {len(v) - 50} more")
            else:
                print(f"{k}: {v}")
    else:
        print(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the Atlas codebase index")
    parser.add_argument("command", help="Query command")
    parser.add_argument("args", nargs="*", help="Command arguments")
    parser.add_argument("--atlas", help="Path to atlas directory")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--regex", action="store_true", help="Use regex for search")
    parser.add_argument("--path", help="Filter by path")
    parser.add_argument("--limit", type=int, default=20, help="Limit results")
    ns = parser.parse_args()
    
    try:
        atlas_root = find_atlas_root(ns.atlas)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    
    engine = QueryEngine(atlas_root)
    cmd = ns.command.lower().replace("-", "_")
    
    handlers = {
        "summary": lambda: engine.cmd_summary(),
        "capabilities": lambda: engine.cmd_capabilities(),
        "doctor": lambda: engine.cmd_doctor(),
        "structure": lambda: engine.cmd_structure(ns.args[0] if ns.args else "."),
        "search": lambda: engine.cmd_search(ns.args[0] if ns.args else "", regex=ns.regex, path_filter=ns.path),
        "class": lambda: engine.cmd_class(ns.args[0] if ns.args else ""),
        "func": lambda: engine.cmd_func(ns.args[0] if ns.args else ""),
        "symbol": lambda: engine.cmd_symbol(ns.args[0] if ns.args else ""),
        "imports": lambda: engine.cmd_imports(ns.args[0] if ns.args else ""),
        "importers": lambda: engine.cmd_importers(ns.args[0] if ns.args else ""),
        "calls": lambda: engine.cmd_calls(ns.args[0] if ns.args else ""),
        "callers": lambda: engine.cmd_callers(ns.args[0] if ns.args else ""),
        "callees": lambda: engine.cmd_callees(ns.args[0] if ns.args else ""),
        "impact": lambda: engine.cmd_impact(ns.args[0] if ns.args else ""),
        "impact_file": lambda: engine.cmd_impact_file(ns.args[0] if ns.args else ""),
        "related": lambda: engine.cmd_related(ns.args[0] if ns.args else ""),
        "todo": lambda: engine.cmd_todo(ns.args[0] if ns.args else None),
        "security": lambda: engine.cmd_security(ns.args[0] if ns.args else None),
        "constants": lambda: engine.cmd_constants(ns.args[0] if ns.args else None),
        "hotspots": lambda: engine.cmd_hotspots(ns.limit),
    }
    
    if cmd not in handlers:
        print(f"Unknown command: {ns.command}", file=sys.stderr)
        print(f"Available: {', '.join(handlers.keys())}", file=sys.stderr)
        sys.exit(1)
    
    result = handlers[cmd]()
    format_output(result, as_json=ns.json)


if __name__ == "__main__":
    main()
'''

TRANSLOG_PY = r'''#!/usr/bin/env python3
"""Atlas translog - track file changes over time."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from tool_common import (
    RESERVED_TRANSLOG_DIRS,
    ensure_dir,
    find_atlas_root,
    json_dump,
    load_index_state,
    normalize_rel_path,
    read_toon_file,
    safe_rel_to_id,
    toon_esc,
    toon_header,
    walk_project_files,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 64), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def load_monitor_config(atlas_root: str) -> Tuple[str, List[str], List[str]]:
    state = load_index_state(atlas_root)
    project_root = state.get("project_root")
    if not project_root:
        raise RuntimeError("project_root missing from index state")
    
    exts = []
    skip_dirs = []
    config_path = os.path.join(atlas_root, "cache", "config_effective.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        exts = cfg.get("extensions", [])
        skip_dirs = cfg.get("skip_dirs", [])
    
    if not exts:
        exts = [".py", ".js", ".ts", ".cpp", ".c", ".h", ".hpp", ".java", ".go", ".rs"]
    
    return project_root, exts, skip_dirs


def translog_root(atlas_root: str) -> str:
    return os.path.join(atlas_root, "translog")


def manifest_paths(atlas_root: str) -> Dict[str, str]:
    root = translog_root(atlas_root)
    return {
        "root": root,
        "manifest": os.path.join(root, "__meta__", "manifest.toon"),
        "history_dir": os.path.join(root, "__history__"),
        "diff_dir": os.path.join(root, "__diffs__"),
        "meta_dir": os.path.join(root, "__meta__"),
    }


def ensure_translog_dirs(atlas_root: str) -> Dict[str, str]:
    paths = manifest_paths(atlas_root)
    ensure_dir(paths["root"])
    ensure_dir(paths["history_dir"])
    ensure_dir(paths["diff_dir"])
    ensure_dir(paths["meta_dir"])
    return paths


def baseline_path(atlas_root: str, rel_path: str) -> str:
    return os.path.join(translog_root(atlas_root), rel_path.replace("/", os.sep))


def history_path(atlas_root: str, rel_path: str) -> str:
    return os.path.join(translog_root(atlas_root), "__history__", safe_rel_to_id(rel_path) + ".jsonl")


def diff_dir_for(atlas_root: str, rel_path: str) -> str:
    return os.path.join(translog_root(atlas_root), "__diffs__", safe_rel_to_id(rel_path))


def append_history(atlas_root: str, rel_path: str, event: Dict[str, Any]) -> None:
    hp = history_path(atlas_root, rel_path)
    ensure_dir(os.path.dirname(hp))
    with open(hp, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def write_manifest(atlas_root: str, records: List[Dict[str, Any]]) -> None:
    paths = ensure_translog_dirs(atlas_root)
    out = paths["manifest"]
    fields = ["path", "status", "hash", "mtime_ns", "size_bytes", "last_snapshot_utc", "history_count", "last_event"]
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(toon_header("translog_manifest", 1, fields) + "\n")
        for rec in sorted(records, key=lambda x: x["path"]):
            values = [toon_esc(str(rec.get(field, ""))) for field in fields]
            fh.write("|".join(values) + "\n")


def read_manifest(atlas_root: str) -> Dict[str, Dict[str, str]]:
    path = manifest_paths(atlas_root)["manifest"]
    if not os.path.exists(path):
        return {}
    _, rows, _ = read_toon_file(path)
    return {row["path"]: row for row in rows}


def build_unified_diff(old_text: str, new_text: str, rel_path: str, timestamp: str) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{rel_path}@baseline",
        tofile=f"{rel_path}@current",
        lineterm="", n=3,
    ))
    
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
    op_summary = {"insert": 0, "delete": 0, "replace": 0}
    events = []
    
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        op_summary[tag] = op_summary.get(tag, 0) + 1
        events.append({
            "type": tag,
            "old_range": [i1 + 1, i2],
            "new_range": [j1 + 1, j2],
            "old_preview": old_lines[i1:i2][:4],
            "new_preview": new_lines[j1:j2][:4],
        })
    
    return "\n".join(diff_lines) + ("\n" if diff_lines else ""), op_summary, events


def snapshot(atlas_root: str, project_root: str, exts: List[str], skip_dirs: List[str], verbose: bool = False) -> Dict[str, Any]:
    files = walk_project_files(project_root, exts, skip_dirs=skip_dirs, skip_globs=[])
    records = []
    created = 0
    
    ensure_translog_dirs(atlas_root)
    
    for abs_path in files:
        rel = normalize_rel_path(os.path.relpath(abs_path, project_root))
        dst = baseline_path(atlas_root, rel)
        ensure_dir(os.path.dirname(dst))
        shutil.copy2(abs_path, dst)
        
        stat_info = os.stat(abs_path)
        records.append({
            "path": rel,
            "status": "tracked",
            "hash": file_hash(abs_path),
            "mtime_ns": str(int(stat_info.st_mtime * 1_000_000_000)),
            "size_bytes": str(stat_info.st_size),
            "last_snapshot_utc": utc_now(),
            "history_count": "0",
            "last_event": "snapshot",
        })
        created += 1
        
        if verbose and created % 100 == 0:
            print(f"  Snapshotted {created} files...")
    
    write_manifest(atlas_root, records)
    return {"tracked_files": created}


def poll(atlas_root: str, project_root: str, exts: List[str], skip_dirs: List[str], verbose: bool = False) -> Dict[str, Any]:
    ensure_translog_dirs(atlas_root)
    prev_manifest = read_manifest(atlas_root)
    current_files = {
        normalize_rel_path(os.path.relpath(p, project_root)): p
        for p in walk_project_files(project_root, exts, skip_dirs=skip_dirs, skip_globs=[])
    }
    
    all_paths = sorted(set(prev_manifest) | set(current_files))
    new_manifest = []
    modified = created = deleted = 0
    
    for rel in all_paths:
        now = utc_now()
        prod_path = current_files.get(rel)
        base_path = baseline_path(atlas_root, rel)
        
        # New file
        if prod_path and not os.path.exists(base_path):
            ensure_dir(os.path.dirname(base_path))
            shutil.copy2(prod_path, base_path)
            stat_info = os.stat(prod_path)
            
            event = {
                "timestamp_utc": now,
                "event": "created",
                "path": rel,
                "new_hash": file_hash(prod_path),
                "size_bytes": stat_info.st_size,
            }
            append_history(atlas_root, rel, event)
            created += 1
            
            new_manifest.append({
                "path": rel,
                "status": "tracked",
                "hash": event["new_hash"],
                "mtime_ns": str(int(stat_info.st_mtime * 1_000_000_000)),
                "size_bytes": str(stat_info.st_size),
                "last_snapshot_utc": now,
                "history_count": "1",
                "last_event": "created",
            })
            continue
        
        # Deleted file
        if not prod_path and os.path.exists(base_path):
            event = {
                "timestamp_utc": now,
                "event": "deleted",
                "path": rel,
                "old_hash": file_hash(base_path),
            }
            append_history(atlas_root, rel, event)
            os.remove(base_path)
            deleted += 1
            
            new_manifest.append({
                "path": rel,
                "status": "deleted",
                "hash": "",
                "mtime_ns": "",
                "size_bytes": "0",
                "last_snapshot_utc": now,
                "history_count": str(int(prev_manifest.get(rel, {}).get("history_count", 0)) + 1),
                "last_event": "deleted",
            })
            continue
        
        if not prod_path or not os.path.exists(base_path):
            continue
        
        # Check for modifications
        new_hash = file_hash(prod_path)
        old_hash = file_hash(base_path)
        
        if new_hash == old_hash:
            # Unchanged
            stat_info = os.stat(prod_path)
            new_manifest.append({
                "path": rel,
                "status": "tracked",
                "hash": new_hash,
                "mtime_ns": str(int(stat_info.st_mtime * 1_000_000_000)),
                "size_bytes": str(stat_info.st_size),
                "last_snapshot_utc": prev_manifest.get(rel, {}).get("last_snapshot_utc", now),
                "history_count": prev_manifest.get(rel, {}).get("history_count", "0"),
                "last_event": prev_manifest.get(rel, {}).get("last_event", "snapshot"),
            })
            continue
        
        # Modified
        old_text = read_text(base_path)
        new_text = read_text(prod_path)
        diff_text, op_summary, changes = build_unified_diff(old_text, new_text, rel, now)
        
        # Save diff
        ddir = diff_dir_for(atlas_root, rel)
        ensure_dir(ddir)
        diff_file = os.path.join(ddir, now.replace(":", "").replace("-", "") + ".diff")
        with open(diff_file, "w", encoding="utf-8") as fh:
            fh.write(diff_text)
        
        additions = sum(1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removals = sum(1 for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---"))
        
        event = {
            "timestamp_utc": now,
            "event": "modified",
            "path": rel,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "line_additions": additions,
            "line_removals": removals,
            "operation_summary": op_summary,
            "changes": changes[:10],
        }
        append_history(atlas_root, rel, event)
        
        # Update baseline
        shutil.copy2(prod_path, base_path)
        modified += 1
        
        stat_info = os.stat(prod_path)
        new_manifest.append({
            "path": rel,
            "status": "tracked",
            "hash": new_hash,
            "mtime_ns": str(int(stat_info.st_mtime * 1_000_000_000)),
            "size_bytes": str(stat_info.st_size),
            "last_snapshot_utc": now,
            "history_count": str(int(prev_manifest.get(rel, {}).get("history_count", 0)) + 1),
            "last_event": "modified",
        })
    
    write_manifest(atlas_root, new_manifest)
    return {"modified": modified, "created": created, "deleted": deleted, "tracked": len(new_manifest)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas translog - track file changes")
    parser.add_argument("--atlas", help="Path to atlas directory")
    parser.add_argument("--poll", action="store_true", help="Poll for changes since last snapshot")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    ns = parser.parse_args()
    
    atlas_root = find_atlas_root(ns.atlas)
    project_root, exts, skip_dirs = load_monitor_config(atlas_root)
    
    if ns.poll:
        result = poll(atlas_root, project_root, exts, skip_dirs, verbose=ns.verbose)
    else:
        result = snapshot(atlas_root, project_root, exts, skip_dirs, verbose=ns.verbose)
    
    if ns.json:
        print(json_dump(result))
    else:
        for k, v in result.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
'''

QUERY_TRANSLOG_PY = r'''#!/usr/bin/env python3
"""Query translog history for a specific file."""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter

from tool_common import (
    find_atlas_root,
    json_dump,
    normalize_rel_path,
    print_kv,
    print_section,
    read_jsonl,
    safe_rel_to_id,
)


def history_path(atlas_root: str, rel_path: str) -> str:
    return os.path.join(atlas_root, "translog", "__history__", safe_rel_to_id(rel_path) + ".jsonl")


def load_history(atlas_root: str, rel_path: str):
    return read_jsonl(history_path(atlas_root, rel_path))


def main() -> None:
    parser = argparse.ArgumentParser(description="Query translog history for a file")
    parser.add_argument("path", help="Project-relative file path")
    parser.add_argument("--atlas", help="Path to atlas directory")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--last", type=int, default=10, help="Number of recent events to show")
    ns = parser.parse_args()
    
    atlas_root = find_atlas_root(ns.atlas)
    rel = normalize_rel_path(ns.path)
    events = load_history(atlas_root, rel)
    
    if not events:
        payload = {"path": rel, "found": False, "message": "No translog history found for this file."}
        if ns.json:
            print(json_dump(payload))
        else:
            print(payload["message"])
        return
    
    counts = Counter(evt.get("event", "unknown") for evt in events)
    additions = sum(int(evt.get("line_additions", 0)) for evt in events)
    removals = sum(int(evt.get("line_removals", 0)) for evt in events)
    latest = events[-ns.last:]
    
    payload = {
        "path": rel,
        "found": True,
        "events_total": len(events),
        "first_event_utc": events[0].get("timestamp_utc", ""),
        "latest_event_utc": events[-1].get("timestamp_utc", ""),
        "event_counts": dict(counts),
        "line_additions": additions,
        "line_removals": removals,
        "recent_events": latest,
    }
    
    if ns.json:
        print(json_dump(payload))
        return
    
    print_section("Translog Report")
    print_kv("File", rel)
    print_kv("Events", len(events))
    print_kv("First event", payload["first_event_utc"])
    print_kv("Latest event", payload["latest_event_utc"])
    print_kv("Adds / Removes", f"{additions} / {removals}")
    
    print_section("Recent Changes")
    for idx, evt in enumerate(latest, 1):
        print(f"\n[{idx}] {evt.get('timestamp_utc', '')}  {evt.get('event', '').upper()}")
        if evt.get("line_additions"):
            print(f"    +{evt['line_additions']} / -{evt.get('line_removals', 0)} lines")
        changes = evt.get("changes", [])[:3]
        for block in changes:
            print(f"    {block.get('type')}: lines {block.get('old_range')} -> {block.get('new_range')}")


if __name__ == "__main__":
    main()
'''

REBUILD_PY = r'''#!/usr/bin/env python3
"""
Atlas rebuild - Refresh the codebase index in-place.

Rebuilds all index files from the current source tree without
re-deploying the tool scripts.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

from tool_common import (
    ensure_dir,
    find_atlas_root,
    json_dump,
    load_index_state,
    normalize_rel_path,
    parse_toon_list,
    print_kv,
    print_section,
    toon_esc,
    toon_header,
    toon_list,
)


# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_EXTENSIONS = [
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl", ".tpp", ".tcc",
    ".py", ".pyi", ".pyw",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".java", ".kt", ".kts", ".go", ".rs", ".rb", ".erb", ".php",
    ".sh", ".bash", ".zsh", ".fish",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg",
    ".md", ".rst", ".txt", ".sql",
    ".swift", ".m", ".mm", ".scala", ".clj", ".lua", ".r", ".R",
    "Makefile", "CMakeLists.txt", "BUILD", "BUILD.bazel", "WORKSPACE",
    "Dockerfile", "docker-compose.yml", "package.json", "Cargo.toml",
    "pyproject.toml", "setup.py", "requirements.txt", "go.mod", "go.sum",
]

DEFAULT_SKIP_DIRS = [
    ".git", ".svn", ".hg", ".bzr",
    "node_modules", "vendor", "venv", ".venv", "env", ".env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".tox",
    "build", "dist", "target", "out", "bin", "obj",
    ".idea", ".vscode", ".vs",
    "coverage", ".coverage", "htmlcov",
    ".next", ".nuxt", ".output",
    "atlas",
]

DEFAULT_SKIP_GLOBS = [
    "*.min.js", "*.min.css", "*.map",
    "*.pyc", "*.pyo", "*.class", "*.o", "*.obj", "*.a", "*.so", "*.dylib", "*.dll",
    "*.exe", "*.bin", "*.dat", "*.db", "*.sqlite", "*.sqlite3",
    "*.log", "*.lock", "package-lock.json", "yarn.lock", "Cargo.lock",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.svg", "*.webp",
    "*.pdf", "*.doc", "*.docx", "*.xls", "*.xlsx", "*.ppt", "*.pptx",
    "*.zip", "*.tar", "*.gz", "*.rar", "*.7z",
    "*.woff", "*.woff2", "*.ttf", "*.eot",
    ".DS_Store", "Thumbs.db",
]

RESERVED_TRANSLOG_DIRS = {"__history__", "__diffs__", "__meta__"}


# =============================================================================
# PATTERNS
# =============================================================================

PATTERNS = {
    "class": re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE),
    "struct": re.compile(r"^\s*(?:typedef\s+)?struct\s+(\w+)", re.MULTILINE),
    "interface": re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)", re.MULTILINE),
    "enum": re.compile(r"^\s*(?:export\s+)?enum\s+(\w+)", re.MULTILINE),
    "function_def": re.compile(r"^\s*(?:async\s+)?(?:export\s+)?(?:default\s+)?function\s+(\w+)", re.MULTILINE),
    "arrow_func": re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>", re.MULTILINE),
    "c_function": re.compile(r"^(?!.*\b(?:if|while|for|switch|return|else)\b)\s*(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:\w+[\s*&]+)+(\w+)\s*\([^;]*\)\s*\{", re.MULTILINE),
    "python_def": re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE),
    "python_class": re.compile(r"^\s*class\s+(\w+)\s*[:\(]", re.MULTILINE),
    "import_from": re.compile(r"^\s*(?:from\s+([^\s]+)\s+)?import\s+", re.MULTILINE),
    "import_require": re.compile(r"(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]", re.MULTILINE),
    "cpp_include": re.compile(r'^\s*#include\s*[<"]([^>"]+)[>"]', re.MULTILINE),
    "es_import": re.compile(r"^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    "const_def": re.compile(r"^\s*(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE),
    "define": re.compile(r"^\s*#define\s+([A-Z][A-Z0-9_]*)", re.MULTILINE),
    "todo": re.compile(r"(?://|#|/\*)\s*(TODO|FIXME|HACK|XXX|BUG|NOTE)[\s:]+(.{0,100})", re.IGNORECASE),
    "security": re.compile(r"(password|secret|api[_-]?key|token|credential|auth|private[_-]?key)\s*[:=]", re.IGNORECASE),
}


# =============================================================================
# HELPERS
# =============================================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def walk_project_files(root: str, extensions: Iterable[str], skip_dirs: Iterable[str], skip_globs: Iterable[str]) -> List[str]:
    exts = {e.lower() for e in extensions}
    skip_dir_set = set(skip_dirs or [])
    skip_glob_list = list(skip_globs or [])
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        filtered_dirs = []
        for d in dirnames:
            if d in skip_dir_set or d in RESERVED_TRANSLOG_DIRS or d.startswith("."):
                continue
            filtered_dirs.append(d)
        dirnames[:] = filtered_dirs
        for fn in filenames:
            if any(fnmatch.fnmatch(fn, pat) for pat in skip_glob_list):
                continue
            fp = os.path.join(dirpath, fn)
            suffix = Path(fn).suffix.lower()
            if suffix in exts or fn in exts:
                results.append(fp)
    results.sort()
    return results


def extract_symbols(content: str, file_path: str) -> Dict[str, Any]:
    ext = Path(file_path).suffix.lower()
    symbols = {"classes": [], "functions": [], "imports": [], "constants": [], "todos": [], "security_hints": []}
    
    for m in PATTERNS["class"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "class"})
    for m in PATTERNS["struct"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "struct"})
    for m in PATTERNS["interface"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "interface"})
    for m in PATTERNS["enum"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "enum"})
    for m in PATTERNS["python_class"].finditer(content):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "class"})
    
    for m in PATTERNS["function_def"].finditer(content):
        symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["arrow_func"].finditer(content):
        symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["python_def"].finditer(content):
        symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    if ext in (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"):
        for m in PATTERNS["c_function"].finditer(content):
            symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    
    for m in PATTERNS["cpp_include"].finditer(content):
        symbols["imports"].append({"target": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["es_import"].finditer(content):
        symbols["imports"].append({"target": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["import_from"].finditer(content):
        if m.group(1):
            symbols["imports"].append({"target": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["import_require"].finditer(content):
        symbols["imports"].append({"target": m.group(1), "line": content[:m.start()].count("\n") + 1})
    
    for m in PATTERNS["const_def"].finditer(content):
        symbols["constants"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    for m in PATTERNS["define"].finditer(content):
        symbols["constants"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
    
    for m in PATTERNS["todo"].finditer(content):
        symbols["todos"].append({"type": m.group(1).upper(), "text": m.group(2).strip(), "line": content[:m.start()].count("\n") + 1})
    
    for m in PATTERNS["security"].finditer(content):
        symbols["security_hints"].append({"match": m.group(0)[:50], "line": content[:m.start()].count("\n") + 1})
    
    return symbols


def detect_probable_calls(content: str, known_functions: Set[str], max_functions: int = 500) -> List[str]:
    if not known_functions:
        return []
    func_list = list(known_functions)[:max_functions]
    calls = set()
    batch_size = 100
    for i in range(0, len(func_list), batch_size):
        batch = func_list[i:i + batch_size]
        pattern_str = r"\b(" + "|".join(re.escape(f) for f in batch) + r")\s*\("
        try:
            pattern = re.compile(pattern_str)
            for m in pattern.finditer(content):
                calls.add(m.group(1))
        except re.error:
            pass
    return list(calls)


def write_toon_file(path: str, record_type: str, version: int, fields: List[str], rows: List[Dict[str, Any]], comments: List[str] = None) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(toon_header(record_type, version, fields) + "\n")
        if comments:
            for c in comments:
                fh.write(c + "\n")
        for row in rows:
            values = [toon_esc(row.get(f, "")) for f in fields]
            fh.write("|".join(values) + "\n")


# =============================================================================
# REBUILDER
# =============================================================================

def rebuild_index(atlas_root: str, skip_callgraph: bool = False, update_translog: bool = False, verbose: bool = False) -> Dict[str, Any]:
    state = load_index_state(atlas_root)
    project_root = state.get("project_root")
    
    if not project_root or not os.path.isdir(project_root):
        print(f"Error: Project root not found: {project_root}", file=sys.stderr)
        sys.exit(1)
    
    # Load config
    config_path = os.path.join(atlas_root, "cache", "config_effective.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    else:
        config = {"extensions": DEFAULT_EXTENSIONS, "skip_dirs": DEFAULT_SKIP_DIRS, "skip_globs": DEFAULT_SKIP_GLOBS}
    
    exts = config.get("extensions", DEFAULT_EXTENSIONS)
    skip_dirs = config.get("skip_dirs", DEFAULT_SKIP_DIRS)
    skip_globs = config.get("skip_globs", DEFAULT_SKIP_GLOBS)
    
    index_dir = os.path.join(atlas_root, "index")
    cache_dir = os.path.join(atlas_root, "cache")
    
    # Scan files
    print_section("Scanning Files")
    print("  Scanning directory tree...", flush=True)
    file_paths = walk_project_files(project_root, exts, skip_dirs, skip_globs)
    
    files = []
    for fp in file_paths:
        rel = normalize_rel_path(os.path.relpath(fp, project_root))
        try:
            stat_info = os.stat(fp)
            files.append({"path": rel, "abs_path": fp, "size": stat_info.st_size, "ext": Path(fp).suffix.lower()})
        except OSError:
            continue
    print(f"  Found {len(files)} files to index")
    
    # Extract symbols
    print_section("Extracting Symbols")
    symbols = defaultdict(list)
    imports_map = defaultdict(list)
    importers_map = defaultdict(list)
    all_functions = set()
    call_graph = defaultdict(list)
    todos = []
    security_hints = []
    constants = defaultdict(list)
    
    total = len(files)
    last_report = 0
    
    for idx, f in enumerate(files):
        progress = idx + 1
        if progress == total or progress - last_report >= 200 or (total > 100 and progress % max(1, total // 10) == 0):
            print(f"  Processed {progress}/{total} files...", flush=True)
            last_report = progress
        
        try:
            content = read_text(f["abs_path"])
            syms = extract_symbols(content, f["path"])
            
            f["line_count"] = content.count("\n") + 1
            f["classes"] = [c["name"] for c in syms["classes"]]
            f["functions"] = [fn["name"] for fn in syms["functions"]]
            f["import_count"] = len(syms["imports"])
            
            for cls in syms["classes"]:
                cls["file"] = f["path"]
                symbols["classes"].append(cls)
            
            for func in syms["functions"]:
                func["file"] = f["path"]
                symbols["functions"].append(func)
                all_functions.add(func["name"])
            
            for imp in syms["imports"]:
                imports_map[f["path"]].append(imp["target"])
                importers_map[imp["target"]].append(f["path"])
            
            for const in syms["constants"]:
                const["file"] = f["path"]
                constants[const["name"]].append(const)
            
            for todo in syms["todos"]:
                todo["file"] = f["path"]
                todos.append(todo)
            
            for sec in syms["security_hints"]:
                sec["file"] = f["path"]
                security_hints.append(sec)
                
        except Exception as e:
            if verbose:
                print(f"  Warning: Failed to process {f['path']}: {e}")
    
    # Build call graph
    if skip_callgraph:
        print_section("Skipping Call Graph (--skip-callgraph)")
    else:
        print_section("Building Call Graph")
        if all_functions:
            func_count = len(all_functions)
            print(f"  Analyzing calls for {func_count} known functions...")
            if func_count > 500:
                print(f"  Note: Limiting to 500 functions for performance")
            
            last_report = 0
            for idx, f in enumerate(files):
                progress = idx + 1
                if progress == total or progress - last_report >= 200 or (total > 100 and progress % max(1, total // 10) == 0):
                    print(f"  Analyzed {progress}/{total} files...", flush=True)
                    last_report = progress
                
                try:
                    content = read_text(f["abs_path"])
                    calls = detect_probable_calls(content, all_functions)
                    if calls:
                        call_graph[f["path"]] = calls
                except Exception:
                    continue
    
    # Write index files
    print_section("Writing Index Files")
    
    # Files
    file_fields = ["path", "size", "line_count", "ext", "classes", "functions", "import_count"]
    file_rows = [{"path": f["path"], "size": str(f.get("size", 0)), "line_count": str(f.get("line_count", 0)),
                  "ext": f.get("ext", ""), "classes": toon_list(f.get("classes", [])),
                  "functions": toon_list(f.get("functions", [])), "import_count": str(f.get("import_count", 0))}
                 for f in files]
    write_toon_file(os.path.join(index_dir, "files.toon"), "file_inventory", 1, file_fields, file_rows)
    
    # Classes
    class_fields = ["name", "type", "file", "line"]
    class_rows = [{"name": c["name"], "type": c.get("type", "class"), "file": c["file"], "line": str(c["line"])}
                  for c in symbols["classes"]]
    write_toon_file(os.path.join(index_dir, "classes.toon"), "class_index", 1, class_fields, class_rows)
    
    # Functions
    func_fields = ["name", "file", "line"]
    func_rows = [{"name": fn["name"], "file": fn["file"], "line": str(fn["line"])} for fn in symbols["functions"]]
    write_toon_file(os.path.join(index_dir, "functions.toon"), "function_index", 1, func_fields, func_rows)
    
    # Imports
    import_fields = ["file", "imports"]
    import_rows = [{"file": f, "imports": toon_list(imps)} for f, imps in sorted(imports_map.items())]
    write_toon_file(os.path.join(index_dir, "imports.toon"), "import_map", 1, import_fields, import_rows)
    
    # Importers
    importer_fields = ["target", "importers"]
    importer_rows = [{"target": t, "importers": toon_list(imps)} for t, imps in sorted(importers_map.items())]
    write_toon_file(os.path.join(index_dir, "importers.toon"), "importer_map", 1, importer_fields, importer_rows)
    
    # Calls
    call_fields = ["file", "calls"]
    call_rows = [{"file": f, "calls": toon_list(calls)} for f, calls in sorted(call_graph.items())]
    write_toon_file(os.path.join(index_dir, "calls.toon"), "call_graph", 1, call_fields, call_rows)
    
    # TODOs
    todo_fields = ["file", "line", "type", "text"]
    todo_rows = [{"file": t["file"], "line": str(t["line"]), "type": t["type"], "text": t["text"]} for t in todos]
    write_toon_file(os.path.join(index_dir, "todos.toon"), "todo_index", 1, todo_fields, todo_rows)
    
    # Security
    sec_fields = ["file", "line", "match"]
    sec_rows = [{"file": s["file"], "line": str(s["line"]), "match": s["match"]} for s in security_hints]
    write_toon_file(os.path.join(index_dir, "security.toon"), "security_hints", 1, sec_fields, sec_rows)
    
    # Constants
    const_fields = ["name", "file", "line"]
    const_rows = []
    for name, locs in sorted(constants.items()):
        for loc in locs:
            const_rows.append({"name": name, "file": loc["file"], "line": str(loc["line"])})
    write_toon_file(os.path.join(index_dir, "constants.toon"), "constant_index", 1, const_fields, const_rows)
    
    # Structure
    dir_tree = defaultdict(list)
    for f in files:
        parent = os.path.dirname(f["path"]) or "."
        dir_tree[parent].append(os.path.basename(f["path"]))
    struct_fields = ["directory", "files"]
    struct_rows = [{"directory": d, "files": toon_list(fls)} for d, fls in sorted(dir_tree.items())]
    write_toon_file(os.path.join(index_dir, "structure.toon"), "directory_structure", 1, struct_fields, struct_rows)
    
    # Update cache
    print_section("Updating Cache")
    
    func_lookup = defaultdict(list)
    for fn in symbols["functions"]:
        func_lookup[fn["name"]].append({"file": fn["file"], "line": fn["line"]})
    with open(os.path.join(cache_dir, "function_lookup.json"), "w") as fh:
        json.dump(func_lookup, fh)
    
    class_lookup = defaultdict(list)
    for cls in symbols["classes"]:
        class_lookup[cls["name"]].append({"file": cls["file"], "line": cls["line"], "type": cls.get("type", "class")})
    with open(os.path.join(cache_dir, "class_lookup.json"), "w") as fh:
        json.dump(class_lookup, fh)
    
    with open(os.path.join(cache_dir, "index_state.json"), "w") as fh:
        json.dump({
            "project_root": project_root,
            "atlas_root": atlas_root,
            "indexed_at": utc_now(),
            "file_count": len(files),
            "function_count": len(symbols["functions"]),
            "class_count": len(symbols["classes"]),
        }, fh, indent=2)
    
    # Update translog if requested
    if update_translog:
        print_section("Updating Translog Baseline")
        translog_dir = os.path.join(atlas_root, "translog")
        tracked = 0
        for f in files:
            dst = os.path.join(translog_dir, f["path"].replace("/", os.sep))
            ensure_dir(os.path.dirname(dst))
            try:
                shutil.copy2(f["abs_path"], dst)
                tracked += 1
            except Exception:
                pass
        print(f"  Updated {tracked} file baselines")
    
    summary = {
        "files": len(files),
        "classes": len(symbols["classes"]),
        "functions": len(symbols["functions"]),
        "imports": sum(len(v) for v in imports_map.values()),
        "todos": len(todos),
        "security_hints": len(security_hints),
    }
    
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the Atlas codebase index")
    parser.add_argument("--atlas", help="Path to atlas directory")
    parser.add_argument("--skip-callgraph", action="store_true", help="Skip call graph analysis (faster)")
    parser.add_argument("--update-translog", action="store_true", help="Also update translog baseline")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    ns = parser.parse_args()
    
    atlas_root = find_atlas_root(ns.atlas)
    summary = rebuild_index(atlas_root, skip_callgraph=ns.skip_callgraph, 
                           update_translog=ns.update_translog, verbose=ns.verbose)
    
    print_section("Rebuild Complete")
    
    if ns.json:
        print(json_dump(summary))
    else:
        print(f"""
Summary:
  Files indexed:     {summary['files']}
  Classes found:     {summary['classes']}
  Functions found:   {summary['functions']}
  Import edges:      {summary['imports']}
  TODOs found:       {summary['todos']}
  Security hints:    {summary['security_hints']}
""")


if __name__ == "__main__":
    main()
'''

ATLAS_CLI_PY = r'''#!/usr/bin/env python3
"""
Atlas CLI - Command router for the Atlas codebase index.

Usage:
    atlas query <command> [args...]
    atlas translog [--poll]
    atlas translog-report <file>
"""
from __future__ import annotations

import os
import subprocess
import sys


def get_bin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def run_tool(tool_name: str, args: list) -> int:
    bin_dir = get_bin_dir()
    tool_path = os.path.join(bin_dir, f"{tool_name}.py")
    
    if not os.path.exists(tool_path):
        print(f"Error: Tool not found: {tool_path}", file=sys.stderr)
        return 1
    
    cmd = [sys.executable, tool_path] + args
    return subprocess.call(cmd)


def print_usage():
    print("""
Atlas - Codebase Index for LLM Coding Assistants

Usage:
    atlas query <command> [args...]     Query the codebase index
    atlas rebuild [options]             Rebuild the index from current source
    atlas translog [--poll]             Snapshot or poll for file changes
    atlas translog-report <file>        Show change history for a file
    atlas help                          Show this help message

Rebuild Options:
    atlas rebuild                       Full rebuild including call graph
    atlas rebuild --skip-callgraph      Faster rebuild without call graph
    atlas rebuild --update-translog     Also refresh translog baseline

Query Commands:
    atlas query summary                 Show index summary
    atlas query capabilities            List available query capabilities
    atlas query search <term>           Search files, classes, functions
    atlas query structure <path>        Show directory structure
    atlas query class <name>            Find class definitions
    atlas query func <name>             Find function definitions
    atlas query symbol <name>           Find any symbol
    atlas query imports <file>          Show what a file imports
    atlas query importers <target>      Show what imports a target
    atlas query calls <file>            Show probable calls from a file
    atlas query callers <symbol>        Find callers of a symbol
    atlas query callees <symbol>        Find callees of a symbol
    atlas query impact <symbol>         Analyze impact of changing a symbol
    atlas query impact-file <file>      Analyze impact of changing a file
    atlas query related <file>          Show related files
    atlas query todo [term]             Find TODO/FIXME markers
    atlas query security [term]         Find security-sensitive patterns
    atlas query constants [name]        Find constants
    atlas query hotspots                Find complex/high-churn files

Examples:
    atlas query search socket
    atlas query impact-file src/core/engine.h
    atlas rebuild --skip-callgraph
    atlas translog --poll
    atlas translog-report src/main.cpp
""")


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(0)
    
    command = sys.argv[1].lower()
    args = sys.argv[2:]
    
    if command in ("help", "--help", "-h"):
        print_usage()
        sys.exit(0)
    
    if command == "query":
        sys.exit(run_tool("query", args))
    
    if command == "rebuild":
        sys.exit(run_tool("rebuild", args))
    
    if command == "translog":
        sys.exit(run_tool("translog", args))
    
    if command in ("translog-report", "translog_report"):
        sys.exit(run_tool("query_translog", args))
    
    print(f"Unknown command: {command}", file=sys.stderr)
    print("Run 'atlas help' for usage.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
'''

LLM_INSTRUCTIONS_MD = r'''# Atlas - LLM Coding Assistant Instructions

Use this document as system instructions for Claude Code, ChatGPT Codex, or similar AI coding assistants.

---

## Overview

You have access to **Atlas**, a pre-built codebase index. Use it as your primary navigation layer before reading source files directly.

Atlas reduces token overhead by letting you query symbols, dependencies, and structure without opening entire files.

---

## Quick Start

At the beginning of any coding session:

```bash
# Get index overview
python atlas/bin/atlas.py query summary

# See available commands
python atlas/bin/atlas.py query capabilities
```

---

## Core Rules

1. **Start with Atlas, not source files.** Query the index first to narrow your search.
2. **Use queries before grep.** Atlas is faster and more accurate for symbol lookup.
3. **Check impact before changing code.** Use `impact` and `impact-file` commands.
4. **Treat call graphs as heuristic.** Atlas uses regex-based detection, not compiler analysis.
5. **Consult translog for volatile files.** Check recent changes before major edits.

---

## Command Reference

### Summary & Diagnostics
```bash
python atlas/bin/atlas.py query summary          # Index overview
python atlas/bin/atlas.py query capabilities     # Available commands
python atlas/bin/atlas.py query doctor           # Check index health
```

### Finding Code
```bash
python atlas/bin/atlas.py query search <term>              # Search everywhere
python atlas/bin/atlas.py query search <term> --regex      # Regex search
python atlas/bin/atlas.py query search <term> --path src/  # Filter by path
python atlas/bin/atlas.py query structure <path>           # Directory listing
```

### Symbol Lookup
```bash
python atlas/bin/atlas.py query class <ClassName>     # Find class definitions
python atlas/bin/atlas.py query func <FunctionName>   # Find function definitions
python atlas/bin/atlas.py query symbol <Name>         # Find any symbol
python atlas/bin/atlas.py query constants <NAME>      # Find constants/defines
```

### Dependency Analysis
```bash
python atlas/bin/atlas.py query imports <file>        # What does this file import?
python atlas/bin/atlas.py query importers <target>    # What imports this target?
python atlas/bin/atlas.py query related <file>        # Both directions
```

### Call Graph (Heuristic)
```bash
python atlas/bin/atlas.py query calls <file>          # Probable calls from file
python atlas/bin/atlas.py query callers <symbol>      # Who calls this?
python atlas/bin/atlas.py query callees <symbol>      # What does this call?
```

### Impact Analysis
```bash
python atlas/bin/atlas.py query impact <symbol>       # Impact of changing symbol
python atlas/bin/atlas.py query impact-file <file>    # Impact of changing file
```

### Code Quality
```bash
python atlas/bin/atlas.py query todo [term]           # Find TODO/FIXME
python atlas/bin/atlas.py query security [term]       # Security-sensitive patterns
python atlas/bin/atlas.py query hotspots              # Complex files
```

### Change Tracking
```bash
python atlas/bin/atlas.py translog                    # Initialize snapshot
python atlas/bin/atlas.py translog --poll             # Check for changes
python atlas/bin/atlas.py translog-report <file>      # File change history
```

---

## Standard Workflow

### For Feature Work / Bug Fixes

1. **Locate the area:**
   ```bash
   python atlas/bin/atlas.py query search <keyword>
   python atlas/bin/atlas.py query structure <path>
   ```

2. **Find specific symbols:**
   ```bash
   python atlas/bin/atlas.py query class <ClassName>
   python atlas/bin/atlas.py query func <FunctionName>
   ```

3. **Check dependencies:**
   ```bash
   python atlas/bin/atlas.py query imports <file>
   python atlas/bin/atlas.py query importers <file>
   ```

4. **Analyze impact:**
   ```bash
   python atlas/bin/atlas.py query impact-file <file>
   python atlas/bin/atlas.py query callers <symbol>
   ```

5. **Check recent changes:**
   ```bash
   python atlas/bin/atlas.py translog-report <file>
   ```

6. **Open only necessary source files** and make changes.

7. **Re-verify impact** after changes.

---

## Interpretation Guidelines

### Confidence Levels
- **File inventory, imports, structure:** High confidence (direct extraction)
- **Class/function definitions:** High confidence (regex patterns)
- **Call graph, callers, callees:** Heuristic (regex-based, may have false positives)
- **Dead code detection:** Candidate only (requires manual verification)

### When to Verify
- Always verify call graph results by inspecting source
- Cross-check impact analysis with actual imports
- Treat security hints as starting points, not definitive findings

---

## Best Practices

1. **Query before reading.** Use Atlas to identify the 2-3 files you actually need.
2. **Use `--json` for parsing.** Add `--json` flag when you need structured output.
3. **Check impact before refactoring.** Run impact queries before touching shared code.
4. **Poll translog during sessions.** Track what changed during your work.
5. **Trust structure over inference.** Prefer import/importer data over call graph guesses.

---

## Refreshing the Index

If the codebase has changed significantly:

```bash
# Re-run deployment (from project root's parent or with full path)
python deploy_atlas.py /path/to/project --yes
```

This will rebuild the index from scratch.

---

## Troubleshooting

**"Unable to locate Atlas index"**
- Ensure you're running from within the project or specify `--atlas /path/to/atlas`

**Empty search results**
- Check if the file type is indexed (see config in `atlas/cache/config_effective.json`)
- Try broader search terms

**Stale data**
- Re-run `python deploy_atlas.py /path/to/project --yes` to rebuild

---

## File Locations

```
project_root/
└── atlas/
    ├── bin/                    # CLI tools (query, translog, etc.)
    ├── index/                  # TOON index files
    │   ├── files.toon          # File inventory
    │   ├── classes.toon        # Class definitions
    │   ├── functions.toon      # Function definitions
    │   ├── imports.toon        # Import relationships
    │   ├── importers.toon      # Reverse import map
    │   ├── calls.toon          # Probable call graph
    │   ├── todos.toon          # TODO/FIXME markers
    │   ├── security.toon       # Security hints
    │   └── structure.toon      # Directory structure
    ├── cache/                  # JSON caches for fast lookup
    ├── translog/               # File snapshots and change history
    └── instructions/
        └── llm.md              # This file
```
'''


# =============================================================================
# MAIN DEPLOYMENT LOGIC
# =============================================================================

def deploy_atlas(project_root: str, force: bool = False, verbose: bool = False, skip_callgraph: bool = False, python_path: str = None) -> None:
    """Deploy Atlas to a project root."""
    project_root = os.path.abspath(project_root)
    
    if not os.path.isdir(project_root):
        print(f"Error: Project root does not exist: {project_root}", file=sys.stderr)
        sys.exit(1)
    
    # Use provided python path or detect current interpreter
    if not python_path:
        python_path = sys.executable
    
    atlas_root = os.path.join(project_root, "atlas")
    
    # Check for existing installation
    if os.path.exists(atlas_root):
        if not force:
            print(f"Atlas already exists at: {atlas_root}")
            response = input("Overwrite? [y/N]: ").strip().lower()
            if response != "y":
                print("Aborted.")
                sys.exit(0)
        print(f"Removing existing Atlas installation...")
        shutil.rmtree(atlas_root)
    
    print_section("Creating Atlas Directory Structure")
    print_kv("Python path", python_path)
    
    # Create directories
    bin_dir = os.path.join(atlas_root, "bin")
    index_dir = os.path.join(atlas_root, "index")
    cache_dir = os.path.join(atlas_root, "cache")
    translog_dir = os.path.join(atlas_root, "translog")
    instructions_dir = os.path.join(atlas_root, "instructions")
    
    for d in [bin_dir, index_dir, cache_dir, translog_dir, instructions_dir]:
        ensure_dir(d)
        print_kv("Created", d)
    
    print_section("Writing Tool Scripts")
    
    # Build shebang line
    shebang = f"#!{python_path}"
    
    # Write tool scripts
    tools = {
        "tool_common.py": TOOL_COMMON_PY,
        "query.py": QUERY_PY,
        "translog.py": TRANSLOG_PY,
        "query_translog.py": QUERY_TRANSLOG_PY,
        "rebuild.py": REBUILD_PY,
        "atlas_cli.py": ATLAS_CLI_PY,
    }
    
    for name, content in tools.items():
        # Replace the generic shebang with the configured python path
        if content.startswith("#!/usr/bin/env python3"):
            content = content.replace("#!/usr/bin/env python3", shebang, 1)
        
        path = os.path.join(bin_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        # Make executable on Unix
        if os.name != "nt":
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print_kv("Wrote", name)
    
    print_section("Writing LLM Instructions")
    
    # Write LLM instructions
    llm_path = os.path.join(instructions_dir, "llm.md")
    with open(llm_path, "w", encoding="utf-8") as fh:
        fh.write(LLM_INSTRUCTIONS_MD)
    print_kv("Wrote", "llm.md")
    
    print_section("Building Codebase Index")
    
    # Build configuration
    config = {
        "extensions": DEFAULT_EXTENSIONS,
        "skip_dirs": DEFAULT_SKIP_DIRS,
        "skip_globs": DEFAULT_SKIP_GLOBS,
        "python_path": python_path,
    }
    
    # Build the index
    builder = IndexBuilder(project_root, atlas_root, config)
    summary = builder.build(verbose=verbose, skip_callgraph=skip_callgraph)
    
    # Write index state
    state_fields = ["project_root", "atlas_root", "indexed_at", "version", "python_path"]
    state_rows = [{
        "project_root": project_root,
        "atlas_root": atlas_root,
        "indexed_at": utc_now(),
        "version": "1.0.0",
        "python_path": python_path,
    }]
    write_toon_file(
        os.path.join(atlas_root, "index_state.toon"),
        "index_state", 1, state_fields, state_rows,
        ["# Atlas index state"]
    )
    
    # Create Python wrapper script in atlas/bin
    wrapper_content = f'''#!{python_path}
"""Atlas CLI wrapper - delegates to atlas_cli.py in the same directory."""
import os
import subprocess
import sys

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cli_path = os.path.join(script_dir, "atlas_cli.py")
    python_path = r"{python_path}"
    
    if not os.path.exists(cli_path):
        print(f"Error: Atlas CLI not found at {{cli_path}}", file=sys.stderr)
        sys.exit(1)
    
    cmd = [python_path, cli_path] + sys.argv[1:]
    sys.exit(subprocess.call(cmd))

if __name__ == "__main__":
    main()
'''
    wrapper_path = os.path.join(bin_dir, "atlas.py")
    with open(wrapper_path, "w", encoding="utf-8") as fh:
        fh.write(wrapper_content)
    if os.name != "nt":
        os.chmod(wrapper_path, os.stat(wrapper_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print_kv("Wrote", "atlas.py")
    
    # Create Windows batch file in atlas/bin
    bat_content = f'''@echo off
"{python_path}" "%~dp0atlas_cli.py" %*
'''
    bat_path = os.path.join(bin_dir, "atlas.bat")
    with open(bat_path, "w", encoding="utf-8") as fh:
        fh.write(bat_content)
    print_kv("Wrote", "atlas.bat")
    
    print_section("Initializing Translog")
    
    # Initialize translog snapshot
    exts = config["extensions"]
    skip_dirs = config["skip_dirs"]
    files = walk_project_files(project_root, exts, skip_dirs, config["skip_globs"])
    
    translog_meta = os.path.join(translog_dir, "__meta__")
    translog_history = os.path.join(translog_dir, "__history__")
    ensure_dir(translog_meta)
    ensure_dir(translog_history)
    
    tracked = 0
    for fp in files:
        rel = normalize_rel_path(os.path.relpath(fp, project_root))
        dst = os.path.join(translog_dir, rel.replace("/", os.sep))
        ensure_dir(os.path.dirname(dst))
        try:
            shutil.copy2(fp, dst)
            tracked += 1
        except Exception:
            pass
    
    print_kv("Tracked files", tracked)
    
    print_section("Deployment Complete")
    
    print(f"""
Atlas has been deployed to: {atlas_root}
Python interpreter: {python_path}

Summary:
  Files indexed:     {summary['files']}
  Classes found:     {summary['classes']}
  Functions found:   {summary['functions']}
  Import edges:      {summary['imports']}
  TODOs found:       {summary['todos']}
  Security hints:    {summary['security_hints']}

Quick start:
  cd {bin_dir}
  atlas query summary                  # Windows (using atlas.bat)
  python atlas.py query summary        # Cross-platform
  python atlas.py rebuild --skip-callgraph
  python atlas.py help

Or add {bin_dir} to your PATH for global access.

LLM Instructions:
  {os.path.join(instructions_dir, 'llm.md')}
""")


def get_python_path() -> str:
    """Get the path to the current Python interpreter."""
    return sys.executable


def main():
    parser = argparse.ArgumentParser(
        description="Deploy Atlas codebase index to a project",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python deploy_atlas.py /path/to/myproject
  python deploy_atlas.py /path/to/myproject --yes
  python deploy_atlas.py /path/to/myproject --skip-callgraph
  python deploy_atlas.py /path/to/myproject --python-path "C:\\Python312\\python.exe"
        """
    )
    parser.add_argument("project_root", help="Path to the project root directory")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--skip-callgraph", action="store_true", 
                        help="Skip call graph analysis (faster for large codebases)")
    parser.add_argument("--python-path", 
                        help="Path to Python interpreter (auto-detected if not specified)")
    
    args = parser.parse_args()
    
    # Determine Python path
    python_path = args.python_path or get_python_path()
    
    deploy_atlas(args.project_root, force=args.yes, verbose=args.verbose, 
                 skip_callgraph=args.skip_callgraph, python_path=python_path)


if __name__ == "__main__":
    main()
