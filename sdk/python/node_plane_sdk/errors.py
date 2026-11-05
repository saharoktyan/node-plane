class ApiError(Exception):
    """A non-2xx response (`code` from its error body), or `backend_unavailable`
    (503) when the API could not be reached or answered something else."""

    def __init__(self, code: str, status: int, request_id: str | None = None):
        self.code, self.status, self.request_id = code, status, request_id
        super().__init__(code)
