"""Stable CISRE kernel contracts.

Domain code and plugins depend on these ports, never on a concrete database,
queue, lease service or event journal implementation.
"""

from .ports import KERNEL_CONTRACT_VERSION, EventJournal, ExecutionLeaseStore, JobDispatcher, SnapshotStore
from .scale_profile import scalability_profile

__all__ = [
    "KERNEL_CONTRACT_VERSION",
    "EventJournal",
    "ExecutionLeaseStore",
    "JobDispatcher",
    "SnapshotStore",
    "scalability_profile",
]
