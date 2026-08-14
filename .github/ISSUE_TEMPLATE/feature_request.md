---
name: Feature request
about: Something the platform should do and does not
labels: enhancement
---

**The problem**
What are you unable to do today? Describe the situation rather than the solution.

**Why it belongs in the control plane**
This project's first principle is *do not rebuild mature open-source technology*. If an
existing component already does this, the useful change is usually integration rather than
implementation.

**What you have considered**

**Does it have to work air-gapped?**
Almost everything here does. Anything requiring Internet access at runtime needs to be
opt-in and off by default.
