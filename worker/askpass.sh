#!/bin/sh
# git asks for a username, then a password: answer with the GitHub token.
case "$1" in
  Username*) echo "x-access-token" ;;
  *) echo "$GITHUB_TOKEN" ;;
esac
