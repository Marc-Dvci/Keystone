"""Figures for the README and writeup, from runs/sim/summary.json. White, minimal, no titles inside."""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
S = json.loads((ROOT / "runs/sim/summary.json").read_text())
scales = sorted(S, key=float)
INK, GREY, BLUE, RED = "#0b0d12", "#8a93a3", "#1434cb", "#c8321e"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.edgecolor": GREY, "axes.labelcolor": INK,
                     "xtick.color": INK, "ytick.color": INK, "axes.spines.top": False, "axes.spines.right": False})

fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), dpi=200)
for ax, scen, label in ((axes[0], "lisbon", "Lisbon trip, 4 legs"), (axes[1], "hardware", "Pilot run, 3 legs")):
    x = [float(s) for s in scales]
    seq = [S[s]["summary"][f"{scen}/sequential"]["failed_with_money_moved"] for s in scales]
    ks = [S[s]["summary"][f"{scen}/keystone"]["failed_with_money_moved"] for s in scales]
    n = S[scales[0]]["n"]
    ax.plot(x, seq, "-o", color=RED, lw=2, label="sequential agent")
    ax.plot(x, ks, "-o", color=BLUE, lw=2, label="Keystone")
    for xi, v in zip(x, seq):
        ax.annotate(f"{v}", (xi, v), textcoords="offset points", xytext=(0, 8), ha="center", color=RED, fontsize=10)
    ax.annotate("0", (x[-1], 0), textcoords="offset points", xytext=(0, 8), ha="center", color=BLUE, fontsize=10)
    ax.set_xticks(x, [f"x{v:g}" for v in x])
    ax.set_xlabel("race rate vs base assumption")
    ax.set_ylabel(f"failed after money moved (of {n})")
    ax.text(0.02, 0.98, label, transform=ax.transAxes, va="top", color=INK, fontsize=11, fontweight="bold")
    ax.set_ylim(-8, max(seq) * 1.2)
axes[0].legend(frameon=False, loc="center left")
fig.tight_layout()
fig.savefig(ROOT / "docs/figures/fig_sim_failed_with_money.png", facecolor="white")

fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=200)
labels, seq, ks = [], [], []
for s in scales:
    for scen in ("lisbon", "hardware"):
        labels.append(f"{'Lisbon' if scen == 'lisbon' else 'Pilot'} x{float(s):g}")
        seq.append(float(S[s]["summary"][f"{scen}/sequential"]["stranded_usd"]) / 1000)
        ks.append(float(S[s]["summary"][f"{scen}/keystone"]["stranded_usd"]) / 1000)
y = range(len(labels))
ax.barh([i + 0.2 for i in y], seq, height=0.38, color=RED, label="sequential agent")
ax.barh([i - 0.2 for i in y], ks, height=0.38, color=BLUE, label="Keystone")
for i, v in enumerate(seq):
    ax.text(v + 10, i + 0.2, f"${v:,.0f}k", va="center", fontsize=9, color=INK)
for i in y:
    ax.text(10, i - 0.2, "$0", va="center", fontsize=9, color=BLUE)
ax.set_yticks(list(y), labels)
ax.invert_yaxis()
ax.set_xlabel("stranded in non-refundable legs, $ thousands per 400 baskets")
ax.legend(frameon=False, loc="lower right")
fig.tight_layout()
fig.savefig(ROOT / "docs/figures/fig_sim_stranded.png", facecolor="white")
print("ok")
