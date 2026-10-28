"""Errors the HTTP layer turns into JSON."""


class TurboError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
