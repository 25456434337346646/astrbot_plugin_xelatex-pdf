#!/usr/bin/env sh
set -eu

BRANCH="${1:-master}"
REMOTE="${REMOTE:-origin}"

if [ -n "${GITHUB_TOKEN:-}" ]; then
  git -c credential.helper="!f() { echo username=${GITHUB_USERNAME:-25456434337346646}; echo password=$GITHUB_TOKEN; }; f" push -u "$REMOTE" "$BRANCH"
else
  git push -u "$REMOTE" "$BRANCH"
fi
