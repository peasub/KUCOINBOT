# KUCOINBOT

# start as below
 #cd ~/Desktop/kucoin_bot_project
python3 main.py

cd ~/Desktop/kucoin_bot_project
git add . && git commit -m “V7.3.5” && git push


Behavior to change	File to upload
Entry worker logic (DIP, TPB, MOMO, VBRK, SQMR)	strategy.py
TP targets, vol scaling, sizing	tp.py
Exit ladder, order placement	execution.py
All tuning knobs and thresholds	config.py
Regime classification	regime.py
Logging, heartbeat format	engine.py
State save/load	state.py
#

# first version was V7.3.3
# V7.3.3 improved framewrok error and 7 major fixes
# V7.3.5 improved , ODE GAPS (fixable):
  # - VBRK short_followthrough should be its own worker (100+ lines)
  # - SQMR triggers on name=="SQUEEZE" not probabilities
  # - Logger does sync file I/O under asyncio lock
  # - state_save() swallows all exceptions silently
  # - No private WS channel for instant fill detection
  # - ~20 dead/marginal config knobs need deprecation