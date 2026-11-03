"""Controllers carved out of ``ui/main_window.py``.

Each takes the ``MainWindow`` as ``win`` and reads shared state off it
(``libraries``, ``adapters``, ``_writer``, ``settings``, the widgets). That back-reference is
deliberate: it made every extraction a mechanical move rather than a redesign,
which is what let the existing test suite stand as the specification. It is step
one of decoupling, not decoupling itself — passing explicit dependencies is a
later pass.

**A controller must never import ``main_window``.** It only ever receives the
window at runtime, which is what keeps the import graph acyclic.

Cross-controller calls go through the window (``self.win._clear_tabs_for(…)``),
which is already the hub every part of the UI is wired to.
"""
