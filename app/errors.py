class AppError(Exception):
    def __init__(
        self,
        message: str,
        status: int = 400,
        code: str = "error",
        extra: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra or {}


class TransferStopped(Exception):
    def __init__(self, kind: str, written: int) -> None:
        super().__init__(kind)
        self.kind = kind
        self.written = written
