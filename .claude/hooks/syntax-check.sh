#!/usr/bin/env bash
# PostToolUse (Write|Edit): syntax-check whatever was just written.
#
# This repo has no build and no test suite, so a broken script is otherwise only
# discovered on the next deploy. On failure this returns a block decision so the
# error goes straight back to Claude with the parser's own message.
#
# Deliberately limited to parse checks — no linters are installed on the target
# VMs, and this must stay fast enough to run on every edit.
f=$(jq -r '.tool_response.filePath // .tool_input.file_path // empty' 2>/dev/null)
[ -n "$f" ] && [ -f "$f" ] || exit 0

case "$f" in
  *.sh)
    out=$(bash -n "$f" 2>&1); rc=$? ;;
  *.py)
    out=$(python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$f" 2>&1); rc=$? ;;
  *.json)
    out=$(python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" 2>&1); rc=$? ;;
  *.yml|*.yaml)
    # PyYAML is not in the standard library; skip rather than fail if absent.
    out=$(python3 -c 'import sys
try:
    import yaml
except ImportError:
    sys.exit(0)
list(yaml.safe_load_all(open(sys.argv[1])))' "$f" 2>&1); rc=$? ;;
  *)
    exit 0 ;;
esac

[ "$rc" -eq 0 ] && exit 0
jq -nc --arg r "Syntax error in $f — fix before continuing:
$out" '{decision:"block", reason:$r}'
exit 0
