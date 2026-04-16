"""WebAA SDK exceptions."""


class WebAAError(Exception):
    """Base exception for all WebAA SDK errors."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code
