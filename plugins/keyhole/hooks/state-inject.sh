#!/bin/bash
# SessionStart: feed the explicit task state back in, so /clear costs nothing.
#
# This is the one mechanism from SKILL.state (arXiv 2608.26263) that a coding
# agent can adopt as-is. In that paper the model receives the immutable spec,
# the current structured state, and the latest observation — never the previous
# reasoning. Recovery after losing history took their stateful baselines 5-8
# steps and took the state-carrying runtime 0.
#
# Here the state is a file the session maintains itself (see the `state` skill),
# and this hook is the "fed back at every step" half. No file, no output.
state="${CLAUDE_PROJECT_DIR:-$PWD}/.claude/state/CURRENT.md"
[ -r "$state" ] || exit 0
echo "## Task state carried over (.claude/state/CURRENT.md)"
echo
cat "$state"
