# VHS tape for docs/media/phantom-uplift.gif
#
# Regenerate from the repo root (needs vhs, ttyd, ffmpeg on PATH, and a python
# env with examples/phantom-uplift/requirements.txt installed — the hidden
# setup line below activates the plugin venv at ~/skill-workspace/.venv):
#
#   vhs examples/phantom-uplift/demo.tape
#
# PRIVACY: keep variables unexpanded on screen — never print an expanded
# $HOME or a username.
#
# Arc (~20s): false good news (green, exit 0, pytest green, "+23.2% vs last
# week") -> data contract MISMATCH with the missing `promo_discount`
# highlighted (the climax) -> revise through the backbone -> VERIFIED,
# the real number is +3.5%.

Output docs/media/phantom-uplift.gif

Set Shell "bash"
Set FontSize 15
Set Width 1000
Set Height 780
Set Padding 12
Set TypingSpeed 35ms

Hide
Type "source ~/skill-workspace/.venv/bin/activate && cd examples/phantom-uplift && export DEMO_PACE=1.1 && clear"
Enter
Show

Type "python run_demo.py"
Enter
Sleep 26
