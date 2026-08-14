"""Replaceability boundaries for the whole platform (spec §28, Rule 8).

These abstractions exist *before* their implementations on purpose. §28 names the
single most important property of this codebase: the control plane knows how to
manage the engines, but every engine stays replaceable.

    vLLM       -> LLMProvider      -> SGLang, Ollama can replace it
    Docker     -> ComputeBackend   -> Kubernetes can replace it
    Qdrant     -> VectorStore      -> pgvector can replace it
    LangGraph  -> AgentRuntime     -> another agent runtime can replace it
    nvidia-smi -> GpuProbe         -> DCGM, or a fake with no hardware at all

Two rules keep these boundaries real, both enforced by import-linter in
``pyproject.toml`` rather than by review:

* Nothing in this package may import an implementation, a vendor SDK, a service
  or a repository. If an interface needs a vendor type, that is the signal the
  abstraction is leaking.
* Callers depend on these types, never on a concrete class.

The ``GpuProbe`` interface is an addition to the spec's list. Without it there is
no way to develop or test Phases 1-4 on a machine with no NVIDIA GPU, which
describes the reference development machine.
"""

from app.core.interfaces.agent import AgentRuntime
from app.core.interfaces.compute import ComputeBackend
from app.core.interfaces.container import ContainerRuntime
from app.core.interfaces.gpu import GpuProbe
from app.core.interfaces.llm import LLMProvider
from app.core.interfaces.scheduler import Scheduler
from app.core.interfaces.tools import ToolExecutor
from app.core.interfaces.vector import VectorStore

__all__ = [
    "AgentRuntime",
    "ComputeBackend",
    "ContainerRuntime",
    "GpuProbe",
    "LLMProvider",
    "Scheduler",
    "ToolExecutor",
    "VectorStore",
]
