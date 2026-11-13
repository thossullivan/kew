# VHS tape: kew checks its own record (README, "Built with itself").
#
# Re-record from the root of THIS repository, in a checkout whose ledger is
# kew's own development ledger, with the kew under test first on PATH:
#
#     vhs docs/vhs/demo-cli.tape
#
# The paths below are relative to that root. The typed comment says "kew's
# own ledger": it is true only for that ledger. It runs two read-only
# commands. Before you commit the result:
#   1. read every line the recording shows: it comes from that ledger;
#   2. set the recording date in the README caption under the clip.

Output docs/img/demo-cli.gif

Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 15
Set Width 1000
Set Height 300
Set Padding 28
Set TypingSpeed 45ms
Set CursorBlink false
Set Theme { "name": "kew", "black": "#0a0714", "red": "#ff4365", "green": "#2ee6a6", "yellow": "#ffc94a", "blue": "#05d9e8", "magenta": "#ff2a6d", "cyan": "#05d9e8", "white": "#b5acc8", "brightBlack": "#6a6082", "brightRed": "#ff4365", "brightGreen": "#2ee6a6", "brightYellow": "#ffc94a", "brightBlue": "#05d9e8", "brightMagenta": "#ff5c8f", "brightCyan": "#05d9e8", "brightWhite": "#ebe6f2", "background": "#0a0714", "foreground": "#ebe6f2", "selection": "#151025", "cursor": "#05d9e8" }

Hide
Type "source docs/vhs/demo-env.sh && clear" Enter
Sleep 1s
Show

Type "kew --version" Sleep 400ms Enter
Sleep 2.5s

Type "# Every recorded governance event is hash-chained. Check kew's own ledger:" Sleep 300ms Enter
Sleep 600ms
Type "kew audit verify" Sleep 400ms Enter
Sleep 7s
