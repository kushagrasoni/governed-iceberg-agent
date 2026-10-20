# README hero demo for governed-iceberg-agent, rendered with VHS (https://github.com/charmbracelet/vhs).
# Re-render from the repo root, with the project installed in .venv (pip install -e .).
# The run writes its evidence to ./.lakehouse/ (a forced re-init archives any earlier run there).
#   vhs docs/demo.tape
# Then build the README GIF from the MP4 (ffmpeg + gifsicle):
#   ffmpeg -i docs/demo.mp4 -vf "fps=8,split[a][b];[a]palettegen=max_colors=64:reserve_transparent=0[p];[b][p]paletteuse=dither=none" -gifflags -transdiff /tmp/demo.gif
#   gifsicle -O3 /tmp/demo.gif -o docs/demo.gif
# Story mode with --pause: an intro card, then for each of the 8 chapters a title card (~2.5 s)
# followed by its results (~7-8 s), then the scorecard. Wait lines keep the pacing independent of
# how fast the machine runs each chapter.
Output docs/demo.mp4

Require gia
Set Shell "bash"
Set FontSize 22
Set FontFamily "IBM Plex Mono, DejaVu Sans Mono"
Set Width 1520
Set Height 900
Set Padding 26
# Blueprint v2.0 palette C2 (house-theme/animations/terminal/vhs-blueprint-c2.json): navy, no window bar, square corners
Set Theme {"name": "Blueprint C2", "background": "#0F172A", "foreground": "#E2E8F0", "cursor": "#5EEAD4", "selection": "#0F766E", "black": "#0B1222", "red": "#E07A6B", "green": "#86EFAC", "yellow": "#FCD34D", "blue": "#A5B4FC", "magenta": "#D946EF", "cyan": "#5EEAD4", "white": "#E2E8F0", "brightBlack": "#94A3B8", "brightRed": "#E07A6B", "brightGreen": "#86EFAC", "brightYellow": "#FCD34D", "brightBlue": "#BAE6FD", "brightMagenta": "#D946EF", "brightCyan": "#5EEAD4", "brightWhite": "#F8FAFC"}
Set BorderRadius 0
Set Framerate 15
Set TypingSpeed 60ms
Set CursorBlink false

Hide
Type "unset NO_COLOR FORCE_COLOR GIA_HOME; export TERM=xterm-256color COLORTERM=truecolor GIA_LLM_API_KEY= OPENAI_API_KEY= && . .venv/bin/activate && PS1='$ ' && clear"
Enter
Sleep 1s
Show

Sleep 500ms
Type "gia demo --pause"
Sleep 700ms
Enter
Wait+Screen@30s /Enter to begin/
Sleep 4s
Enter

# chapter 1: Data and seeded issues
Wait+Screen@30s /Chapter 1 of 8/
Sleep 2.5s
Wait+Screen@30s /Enter to show the chapter/
Enter
Wait+Screen@30s /Enter for chapter 2/
Sleep 6.5s
Enter

# chapter 2: The agent proposes
Wait+Screen@30s /Chapter 2 of 8/
Sleep 2.5s
Wait+Screen@30s /Enter to show the chapter/
Enter
Wait+Screen@30s /Enter for chapter 3/
Sleep 6.5s
Enter

# chapter 3: Guardrails block risky plans
Wait+Screen@30s /Chapter 3 of 8/
Sleep 2.5s
Wait+Screen@30s /Enter to show the chapter/
Enter
Wait+Screen@30s /Enter for chapter 4/
Sleep 7.5s
Enter

# chapter 4: Branch dry-run and evals
Wait+Screen@30s /Chapter 4 of 8/
Sleep 2.5s
Wait+Screen@30s /Enter to show the chapter/
Enter
Wait+Screen@30s /Enter for chapter 5/
Sleep 7s
Enter

# chapter 5: Human approval gate
Wait+Screen@30s /Chapter 5 of 8/
Sleep 2.5s
Wait+Screen@30s /Enter to show the chapter/
Enter
Wait+Screen@30s /Enter for chapter 6/
Sleep 7.5s
Enter

# chapter 6: Document AI confidence routing
Wait+Screen@30s /Chapter 6 of 8/
Sleep 2.5s
Wait+Screen@30s /Enter to show the chapter/
Enter
Wait+Screen@30s /Enter for chapter 7/
Sleep 7s
Enter

# chapter 7: Evidence trail
Wait+Screen@30s /Chapter 7 of 8/
Sleep 2.5s
Wait+Screen@30s /Enter to show the chapter/
Enter
Wait+Screen@30s /Enter for chapter 8/
Sleep 8s
Enter

# chapter 8: Rollback
Wait+Screen@30s /Chapter 8 of 8/
Sleep 2.5s
Wait+Screen@30s /Enter to show the chapter/
Enter
Wait+Screen@30s /Enter for the scorecard/
Sleep 7.5s
Enter

# scorecard
Wait+Screen@30s /EVIDENCED/
Sleep 7s
