class ServiceError(Exception):
    """Error de negocio con un mensaje apto para el cliente."""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409
