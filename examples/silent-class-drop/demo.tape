# VHS tape for docs/media/silent-class-drop.gif
#
# Regenerate from the repo root (needs vhs, ttyd, ffmpeg on PATH, and a python
# env with examples/silent-class-drop/requirements.txt installed — the hidden
# setup line below activates the plugin venv at ~/skill-workspace/.venv):
#
#   vhs examples/silent-class-drop/demo.tape
#
# Arc (~20s): v1 false success (green, exit 0) -> data contract MISMATCH with
# the missing `class` highlighted (the climax) -> revise through the backbone
# -> VERIFIED, purity 0.31 -> 0.91.

Output docs/media/silent-class-drop.gif

Set Shell "bash"
Set FontSize 15
Set Width 1000
Set Height 780
Set Padding 12
Set TypingSpeed 35ms

Hide
Type "source ~/skill-workspace/.venv/bin/activate && cd examples/silent-class-drop && export DEMO_PACE=1.1 && clear"
Enter
Show

Type "python run_demo.py"
Enter
Sleep 26
