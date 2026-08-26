#!/usr/bin/env bash
# Lint everything in this repo. No build system, so this is the check.
#
#   ./lint.sh          # everything
#   ./lint.sh --quick  # parse checks only, skip shellcheck
#
# Uses a local shellcheck binary if there is one, otherwise the official
# container — so this works on a fresh VM with nothing installed.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

QUICK=false
[[ ${1:-} == --quick ]] && QUICK=true
FAIL=0
note() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=1; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

mapfile -t SH   < <(find . -name '*.sh'   -not -path './.git/*')
mapfile -t PY   < <(find . -name '*.py'   -not -path './.git/*' -not -path '*/__pycache__/*')
mapfile -t JSON < <(find . -name '*.json' -not -path './.git/*')
mapfile -t YML  < <(find . \( -name '*.yml' -o -name '*.yaml' \) -not -path './.git/*')

step "bash -n  (${#SH[@]} scripts)"
for f in "${SH[@]}"; do
  out=$(bash -n "$f" 2>&1) || { note "$f"; printf '       %s\n' "$out"; }
done
[[ $FAIL -eq 0 ]] && ok "all parse"

step "python  (${#PY[@]} files)"
for f in "${PY[@]}"; do
  out=$(python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$f" 2>&1) \
    || { note "$f"; printf '       %s\n' "$out"; }
done
ok "checked"

step "json  (${#JSON[@]} files)"
for f in "${JSON[@]}"; do
  out=$(python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" 2>&1) \
    || { note "$f"; printf '       %s\n' "$out"; }
done
ok "checked"

step "yaml  (${#YML[@]} files)"
if python3 -c 'import yaml' 2>/dev/null; then
  for f in "${YML[@]}"; do
    out=$(python3 -c 'import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))' "$f" 2>&1) \
      || { note "$f"; printf '       %s\n' "$out"; }
  done
  ok "checked"
else
  printf '  skipped — PyYAML not installed\n'
fi

if $QUICK; then
  step "shellcheck"; printf '  skipped (--quick)\n'
else
  step "shellcheck"
  if command -v shellcheck >/dev/null 2>&1; then
    SC=(shellcheck)
  elif command -v docker >/dev/null 2>&1; then
    SC=(docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable)
    printf '  using the koalaman/shellcheck container\n'
  else
    SC=()
    printf '  skipped — install shellcheck, or Docker to use the container\n'
  fi
  if [[ ${#SC[@]} -gt 0 ]]; then
    # SC1091: sourced files (.env, /etc/os-release) do not exist at lint time.
    if "${SC[@]}" --severity=warning --exclude=SC1091 "${SH[@]}"; then
      ok "clean at warning severity"
    else
      FAIL=1
    fi
  fi
fi

printf '\n'
[[ $FAIL -eq 0 ]] && printf '\033[32mAll checks passed.\033[0m\n' || printf '\033[31mSome checks failed.\033[0m\n'
exit $FAIL
