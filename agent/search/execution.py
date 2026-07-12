"""Execution mode parameter shared across minimal and structured controllers.

The mode is decided once at startup from a CLI / configuration argument and is
immutable within a single run. The controller factory is the only place that
maps a mode to a concrete executor, so no runtime code path can switch modes
mid-run.
"""

from __future__ import annotations

from enum import Enum


class ExecutionMode(str, Enum):
    """由启动参数选择的执行模式，同一次运行内不可变。

    ``minimal`` 走线性 ``propose → check → remember → refine`` 循环；
    ``structured`` 从任务开始建立完整 ``ProofWorkspace``。
    """

    MINIMAL = "minimal"
    STRUCTURED = "structured"
