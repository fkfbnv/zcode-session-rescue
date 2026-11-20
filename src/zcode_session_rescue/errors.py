class RescueError(Exception):
    """An expected safety or input error with a stable machine-readable code."""

    def __init__(self, message: str, code: str = "rescue_error"):
        super().__init__(message)
        self.code = code
