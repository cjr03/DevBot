#!/bin/zsh
# Start DevBot in a tmux session, or reattach to the one already running.
# Closing this window only detaches: DevBot keeps running. Press q in the dashboard to stop it.
cd "${0:A:h}"
exec tmux new-session -A -s devbot "uv run python -m devbot" \; set-option mouse off \; set-option status off
