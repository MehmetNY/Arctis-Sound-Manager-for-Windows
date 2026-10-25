#!/bin/bash
# usage: publish.sh <branch> <file>...   -> force-pushes the files as a one-commit orphan branch
branch=$1; shift
tmp=$(mktemp -d)
for f in "$@"; do [ -e "$f" ] && cp "$f" "$tmp/"; done
cd "$tmp" || exit 0
out=$(git init -q 2>&1 && git checkout -q -b logs 2>&1 && git -c user.name=ci -c user.email=ci@users.noreply.github.com add -A 2>&1 && git -c user.name=ci -c user.email=ci@users.noreply.github.com commit -q -m "ci output" --allow-empty 2>&1 && git remote add origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" 2>&1 && git push -q -f origin logs:refs/heads/"$branch" 2>&1)
rc=$?
if [ $rc -ne 0 ]; then echo "::warning title=publish::rc=$rc ${out//$'\n'/ | }"; fi
exit 0
