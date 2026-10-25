#!/bin/bash
# usage: publish.sh <branch> <file>...   -> force-pushes the files as a one-commit orphan branch (the only way to read CI output from outside)
set -e
branch=$1; shift
tmp=$(mktemp -d)
for f in "$@"; do [ -e "$f" ] && cp "$f" "$tmp/" || true; done
git config user.name "ci"; git config user.email "ci@users.noreply.github.com"
cd "$tmp" && git init -q && git checkout -q -b logs && git add -A && git commit -q -m "ci output $(date -u +%FT%TZ)" --allow-empty
git remote add origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
git push -q -f origin logs:refs/heads/"$branch"
