# Atlas

**Offline codebase index generator for LLM coding assistants.**

Atlas extracts file structure, symbols, imports, call graphs, and architecture into a compact queryable format. It reduces token overhead by letting AI agents query the index first, then open only the files they need.

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Why Atlas?

When AI coding assistants work on large codebases, they face a fundamental problem: **they can't see everything at once**. Reading entire source trees burns through context windows and slows down responses.

Atlas solves this by creating a pre-built index that answers questions like:
- "Where is this class defined?"
- "What files import this module?"
- "What would break if I change this function?"

The AI queries the index first, then opens only the 2-3 files it actually needs.

---

## Features

| Feature | Description |
|---------|-------------|
| **Single-file deployment** | One Python script creates everything |
| **Zero dependencies** | Uses only Python standard library |
| **Multi-language support** | Python, JavaScript/TypeScript, C/C++, Java, Go, Rust, and more |
| **Symbol extraction** | Classes, functions, constants, imports |
| **Dependency mapping** | Import/include relationships in both directions |
| **Call graph analysis** | Heuristic detection of function calls |
| **Change tracking** | Translog records file modifications over time |
| **TODO/FIXME scanning** | Finds markers and technical debt |
| **Security hints** | Flags potential sensitive patterns |
| **LLM-ready instructions** | Pre-written system prompt for AI agents |

---

## Quick Start

### 1. Deploy Atlas to your project

```bash
# Download the deployment script
curl -O https://raw.githubusercontent.com/YOUR_USERNAME/atlas/main/deploy_atlas.py

# Deploy to your project
python deploy_atlas.py /path/to/your/project --yes
```

### 2. Query the index

```bash
cd /path/to/your/project

# Get an overview
python atlas/bin/atlas_cli.py query summary

# Search for code
python atlas/bin/atlas_cli.py query search "database"

# Find a class
python atlas/bin/atlas_cli.py query class UserService

# Check what imports a file
python atlas/bin/atlas_cli.py query importers "config.py"
```

### 3. Give instructions to your AI assistant

Copy the contents of `atlas/instructions/llm.md` into your AI coding assistant's system prompt or project instructions.

---

## Installation

### Requirements

- Python 3.8 or higher
- No external dependencies

### Method 1: Direct download

```bash
curl -O https://raw.githubusercontent.com/YOUR_USERNAME/atlas/main/deploy_atlas.py
python deploy_atlas.py /path/to/project
```

### Method 2: Clone repository

```bash
git clone https://github.com/YOUR_USERNAME/atlas.git
python atlas/deploy_atlas.py /path/to/project
```

---

## Directory Structure

After deployment, Atlas creates this structure in your project:

```
your_project/
└── atlas/
    ├── bin/                        # CLI tools
    │   ├── atlas_cli.py            # Main command router
    │   ├── query.py                # Index query engine
    │   ├── translog.py             # Change tracking
    │   ├── query_translog.py       # Translog reports
    │   └── tool_common.py          # Shared utilities
    │
    ├── index/                      # TOON index files
    │   ├── files.toon              # File inventory
    │   ├── classes.toon            # Class definitions
    │   ├── functions.toon          # Function definitions
    │   ├── imports.toon            # Import relationships
    │   ├── importers.toon          # Reverse import map
    │   ├── calls.toon              # Probable call graph
    │   ├── todos.toon              # TODO/FIXME markers
    │   ├── security.toon           # Security hints
    │   ├── constants.toon          # Named constants
    │   └── structure.toon          # Directory structure
    │
    ├── cache/                      # JSON caches for fast lookup
    │   ├── config_effective.json   # Active configuration
    │   ├── function_lookup.json    # Function name → locations
    │   ├── class_lookup.json       # Class name → locations
    │   └── index_state.json        # Index metadata
    │
    ├── translog/                   # File snapshots and history
    │   ├── __meta__/               # Manifest files
    │   ├── __history__/            # Per-file change logs
    │   ├── __diffs__/              # Stored diffs
    │   └── [mirrored source tree]  # Baseline snapshots
    │
    ├── instructions/
    │   └── llm.md                  # LLM system instructions
    │
    └── index_state.toon            # Index metadata
```

---

## Command Reference

### Basic Usage

```bash
# All commands start with:
python atlas/bin/atlas_cli.py <command> [subcommand] [args...]

# Or use the shorthand (if you add atlas/bin to PATH):
atlas <command> [subcommand] [args...]
```

### Query Commands

#### Summary & Diagnostics

```bash
atlas query summary              # Index overview (files, classes, functions)
atlas query capabilities         # List all available commands
atlas query doctor               # Check index health
```

#### Searching

```bash
atlas query search <term>                    # Search files, classes, functions
atlas query search <term> --regex            # Use regex pattern
atlas query search <term> --path src/core    # Filter by path
atlas query structure <path>                 # List directory contents
```

#### Symbol Lookup

```bash
atlas query class <ClassName>       # Find class definitions
atlas query func <FunctionName>     # Find function definitions
atlas query symbol <name>           # Find any symbol (class or function)
atlas query constants <NAME>        # Find constant definitions
```

#### Dependency Analysis

```bash
atlas query imports <file>          # What does this file import?
atlas query importers <target>      # What files import this target?
atlas query related <file>          # Both directions (imports + importers)
```

#### Call Graph (Heuristic)

```bash
atlas query calls <file>            # What functions does this file call?
atlas query callers <symbol>        # What files call this function?
atlas query callees <symbol>        # What does this function call?
```

#### Impact Analysis

```bash
atlas query impact <symbol>         # Who would be affected by changing this?
atlas query impact-file <file>      # Who depends on this file?
```

#### Code Quality

```bash
atlas query todo                    # List all TODO/FIXME markers
atlas query todo <term>             # Search within TODOs
atlas query security                # Find security-sensitive patterns
atlas query security <term>         # Search security hints
atlas query hotspots                # Find complex/high-churn files
atlas query hotspots --limit 10     # Limit results
```

### Translog Commands

```bash
atlas translog                      # Initialize/reset baseline snapshot
atlas translog --poll               # Check for changes since last snapshot
atlas translog-report <file>        # Show change history for a file
```

### Output Formats

```bash
atlas query summary --json          # Output as JSON
atlas query search foo --json       # Structured output for parsing
```

---

## Configuration

Atlas uses sensible defaults but can be customized by modifying the deployment script before running it.

### Default File Extensions

Atlas indexes these file types by default:

| Category | Extensions |
|----------|------------|
| C/C++ | `.c`, `.cc`, `.cpp`, `.cxx`, `.h`, `.hh`, `.hpp`, `.hxx`, `.ipp`, `.inl`, `.tpp`, `.tcc` |
| Python | `.py`, `.pyi`, `.pyw` |
| JavaScript/TypeScript | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` |
| Web | `.html`, `.htm`, `.css`, `.scss`, `.sass`, `.less` |
| Java/Kotlin | `.java`, `.kt`, `.kts` |
| Go | `.go` |
| Rust | `.rs` |
| Ruby | `.rb`, `.erb` |
| PHP | `.php` |
| Shell | `.sh`, `.bash`, `.zsh`, `.fish` |
| Config | `.json`, `.yaml`, `.yml`, `.toml`, `.xml`, `.ini`, `.cfg` |
| Documentation | `.md`, `.rst`, `.txt` |
| SQL | `.sql` |
| Other | `.swift`, `.m`, `.mm`, `.scala`, `.clj`, `.lua`, `.r`, `.R` |
| Build files | `Makefile`, `CMakeLists.txt`, `BUILD`, `Dockerfile`, `package.json`, `Cargo.toml`, etc. |

### Default Skip Directories

These directories are excluded from indexing:

```
.git, .svn, node_modules, vendor, venv, .venv, __pycache__, 
build, dist, target, out, bin, obj, .idea, .vscode, coverage,
.next, .nuxt, atlas
```

### Default Skip Patterns

These file patterns are excluded:

```
*.min.js, *.min.css, *.map, *.pyc, *.class, *.o, *.so, *.dll,
*.exe, *.log, *.lock, package-lock.json, yarn.lock, Cargo.lock,
*.png, *.jpg, *.pdf, *.zip, *.woff, .DS_Store
```

---

## TOON Format

Atlas stores index data in TOON (Tab-delimited Object Notation) format, a simple pipe-delimited text format optimized for:

- Human readability
- Easy parsing
- Git-friendly diffs
- Low overhead

### Example TOON file

```
@TOON|function_index|1|name|file|line
# Function/method definitions
calculate_score|src/utils/helpers.py|7
format_score|src/utils/helpers.py|11
create_engine|src/core/engine.py|37
```

### Format specification

- Line 1: Header with `@TOON|<type>|<version>|<field1>|<field2>|...`
- Lines starting with `#` are comments
- Data rows use `|` as delimiter
- Special characters are escaped: `\p` for `|`, `\c` for `,`, `\n` for newline

---

## Using with AI Assistants

### Claude Code / Anthropic Claude

Add to your project's `CLAUDE.md` or system instructions:

```markdown
## Codebase Navigation

This project uses Atlas for codebase indexing. Before reading source files:

1. Run `python atlas/bin/atlas_cli.py query summary` to orient yourself
2. Use `atlas query search <term>` to find relevant code
3. Use `atlas query impact-file <file>` before making changes
4. Only then open the specific source files you need

See `atlas/instructions/llm.md` for complete guidance.
```

### ChatGPT / OpenAI Codex

Add to your custom instructions or system prompt:

```
You have access to an Atlas codebase index. Always query the index before reading source files:

- atlas query summary: Get project overview
- atlas query search <term>: Find code
- atlas query class/func <name>: Locate definitions
- atlas query imports/importers <file>: Check dependencies
- atlas query impact-file <file>: Assess change impact

Query first, then open only necessary files.
```

### Cursor / Continue / Other AI IDEs

Copy the contents of `atlas/instructions/llm.md` into your IDE's AI configuration or rules file.

---

## Change Tracking (Translog)

Atlas includes a translog system that tracks file changes over time.

### Initialize baseline

```bash
atlas translog
```

This creates a snapshot of all tracked files.

### Poll for changes

```bash
atlas translog --poll
```

Output:
```
modified: 3
created: 1
deleted: 0
tracked: 127
```

### View file history

```bash
atlas translog-report src/core/engine.py
```

Output:
```
==================
Translog Report
==================
File               src/core/engine.py
Events             5
First event        2024-01-15T10:30:00Z
Latest event       2024-01-20T14:22:00Z
Adds / Removes     45 / 12

==================
Recent Changes
==================
[1] 2024-01-20T14:22:00Z  MODIFIED
    +8 / -3 lines
    replace: lines [15, 22] -> [15, 24]
```

### Use cases

- **Before editing**: Check if a file has been changing frequently
- **Code review**: See what changed in a file over time
- **Debugging**: Correlate bugs with recent modifications
- **Refactoring**: Identify stable vs. volatile code

---

## Best Practices

### For AI Assistants

1. **Always start with `query summary`** when entering a new codebase area
2. **Use `search` before `grep`** — Atlas is faster and more precise
3. **Check `impact-file` before modifying** shared code
4. **Treat call graph results as heuristic** — verify by reading source
5. **Consult `translog-report`** before editing frequently-changed files

### For Humans

1. **Re-deploy after major changes**: `python deploy_atlas.py /project --yes`
2. **Poll translog regularly**: `atlas translog --poll`
3. **Use `--json` for scripting**: Parse output programmatically
4. **Add `atlas/` to `.gitignore`** if you don't want to commit the index

---

## Limitations

| Limitation | Details |
|------------|---------|
| **Regex-based extraction** | Symbol detection uses regex patterns, not compiler/parser analysis. May miss complex cases. |
| **Heuristic call graph** | Function calls are detected by name matching, not semantic analysis. May have false positives. |
| **No incremental updates** | Re-running deployment rebuilds the entire index. |
| **Language-agnostic patterns** | Some language-specific constructs may not be detected. |

### When Atlas says "heuristic"

Results marked as heuristic should be verified:
- **Call graph**: A function name appearing doesn't guarantee it's actually called
- **Dead code**: "Unreferenced" means no references found, not definitely unused
- **Impact analysis**: Based on import relationships, not runtime behavior

---

## Troubleshooting

### "Unable to locate Atlas index"

```bash
# Specify the atlas directory explicitly
python atlas/bin/atlas_cli.py query summary --atlas /path/to/project/atlas
```

### Empty search results

1. Check if the file type is indexed:
   ```bash
   cat atlas/cache/config_effective.json | grep extensions
   ```
2. Verify the file exists in the index:
   ```bash
   atlas query structure .
   ```

### Stale data

Re-run deployment to rebuild:
```bash
python deploy_atlas.py /path/to/project --yes
```

### Index appears corrupted

```bash
# Check index health
atlas query doctor

# If issues found, rebuild
rm -rf atlas/
python deploy_atlas.py /path/to/project --yes
```

---

## Contributing

Contributions are welcome! Areas of interest:

- **Language-specific extractors**: Better symbol detection for specific languages
- **Incremental indexing**: Update only changed files
- **IDE integrations**: Plugins for VS Code, JetBrains, etc.
- **Additional query commands**: New analysis capabilities

### Development setup

```bash
git clone https://github.com/YOUR_USERNAME/atlas.git
cd atlas

# Test deployment
python deploy_atlas.py ./test_project --yes

# Run queries
python test_project/atlas/bin/atlas_cli.py query summary
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

## Acknowledgments

Inspired by the need for better AI-assisted code navigation. Built for use with Claude Code, ChatGPT Codex, and similar LLM-based coding assistants.

---

## Changelog

### v1.0.0

- Initial release
- Single-file deployment script
- Query engine with 20+ commands
- Translog change tracking
- LLM instruction templates
- Support for 30+ file extensions
