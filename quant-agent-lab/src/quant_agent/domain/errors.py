class DomainError(Exception):
    """Base error for a safe, structured domain failure."""


class DataValidationError(DomainError):
    pass


class StateTransitionError(DomainError):
    pass


class ApprovalError(DomainError):
    pass


class RiskBlockedError(DomainError):
    pass


class ExecutionError(DomainError):
    pass


class LiveBrokerDisabledError(ExecutionError):
    pass


class ResearchError(DomainError):
    def __init__(self, message: str, code: str = "RESEARCH_ERROR") -> None:
        self.code = code
        super().__init__(message)
