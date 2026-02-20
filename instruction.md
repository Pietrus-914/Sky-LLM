Token-Preserving Workflow
CRITICAL: MQ5 files are large (700-2400 lines). Reading entire files repeatedly exhausts the context window.

Recommended Practices
Consult Documentation First
Read *.md files before source code
Documentation has function lists, data structures, code flow

Use Agents for Analysis
mql5-codeflow-analyzer - understand code flow and method interactions
code-explorer - initial codebase exploration
Agents work in separate contexts, return only summaries

Localized Reads
Grep for function/variable names first
Read only relevant line ranges: Read(file, offset=500, limit=50)

Incremental Edits
Make targeted edits, avoid re-reading files
Trust edits succeeded unless error returned