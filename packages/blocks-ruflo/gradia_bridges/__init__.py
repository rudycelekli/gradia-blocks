"""Explicit, host-owned provider operation registries. No executable manifest loading."""
from .adapter import Bridge, BridgeError, Principal, Registry, RufloEdition, OperationSpec, LocalCliEdition, ProviderEdition
__all__ = ["Bridge", "BridgeError", "Principal", "Registry", "RufloEdition", "OperationSpec", "LocalCliEdition", "ProviderEdition"]
